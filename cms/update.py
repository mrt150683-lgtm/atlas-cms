"""Incremental updates — keep .memory/ in sync without re-paying LLM costs.

``incremental_update`` rebuilds the structural graph (fast, always), but
carries over summaries for files whose mtime is unchanged and feature
narratives whose members didn't change, so only genuinely changed nodes hit
the LLM. ``watch`` polls for changes and re-runs the update.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from pathlib import Path

import networkx as nx

from . import config
from .exporter import export_features, export_graph, export_index, export_summaries
from .features import build_features
from .githistory import enrich_graph_with_git
from .graph_builder import build_graph
from .memory import CodebaseMemory
from .providers import SummaryProvider
from .scanner import scan
from .summarizer import generate_summaries
from .tree_export import export_tree


@dataclass
class UpdateStats:
    files: int = 0
    changed: list[str] = None
    summarized: int = 0
    features: int = 0
    git_files: int = 0

    def __post_init__(self):
        if self.changed is None:
            self.changed = []


def _carry_over(old: nx.DiGraph, new: nx.DiGraph, upgrade_mock: bool = False) -> set[str]:
    """Copy summaries from old graph for unchanged files. Returns changed paths.

    With `upgrade_mock`, mock-generated summaries don't count as done — a real
    provider is available now, so those files re-enter the changed set."""
    changed: set[str] = set()
    for node_id, attrs in new.nodes(data=True):
        if attrs.get("type") != "file":
            continue
        path = attrs["path"]
        if not old.has_node(node_id):
            changed.add(path)
            continue
        old_attrs = old.nodes[node_id]
        stale_mock = (
            upgrade_mock and (old_attrs.get("summary_meta") or {}).get("provider") == "mock"
        )
        if old_attrs.get("mtime") != attrs.get("mtime") or not old_attrs.get("summary") or stale_mock:
            changed.add(path)
            continue
        # unchanged: reuse the file summary and every component summary
        attrs["summary"] = old_attrs.get("summary", "")
        if old_attrs.get("summary_meta"):
            attrs["summary_meta"] = old_attrs["summary_meta"]
        for other_id, other in new.nodes(data=True):
            if other.get("path") == path and other.get("type") in ("func", "class"):
                if old.has_node(other_id) and old.nodes[other_id].get("summary"):
                    other["summary"] = old.nodes[other_id]["summary"]
    return changed


def _narrative_cache(
    old: nx.DiGraph, changed: set[str], upgrade_mock: bool = False
) -> dict[str, tuple[str, str]]:
    """Feature narratives safe to reuse (as (text, original_provider)): no member
    file changed, and not a mock narrative when a real provider is now available."""
    cache: dict[str, tuple[str, str]] = {}
    for node_id, attrs in old.nodes(data=True):
        if attrs.get("type") != "feature" or not attrs.get("summary"):
            continue
        if upgrade_mock and attrs.get("narrative_provider", "mock") == "mock":
            continue
        member_paths = {
            old.nodes[m].get("path") for m in attrs.get("members", []) if old.has_node(m)
        }
        if not (member_paths & changed):
            cache[attrs["name"]] = (attrs["summary"], attrs.get("narrative_provider", "mock"))
    return cache


def _prior_discovered(old: nx.DiGraph) -> list:
    """Re-inject LLM-discovered features (not re-derivable from anchors)."""
    from .features import Feature

    return [
        Feature(
            name=a["name"], description=a.get("description", ""),
            source="discovered", members=list(a.get("members", [])),
            connects=list(a.get("connects", [])),
        )
        for _, a in old.nodes(data=True)
        if a.get("type") == "feature" and a.get("source") == "discovered"
    ]


# @memory:feature:IncrementalUpdates
# @memory:connects:CleanDirectoryScanner, SummaryGenerator, FeatureTracing, GitHistoryLayer
# @memory:summary:Keeps memory fresh cheaply — structure always rebuilt, summaries carried over for mtime-unchanged files, mock summaries upgraded when a real provider appears; watch mode polls and syncs live.
def incremental_update(
    root: Path, provider: SummaryProvider, echo=print, full: bool = False
) -> UpdateStats:
    root = root.resolve()
    memory_dir = root / config.MEMORY_DIR_NAME
    graph_path = memory_dir / "graph.json"
    old: nx.DiGraph | None = None
    if graph_path.is_file() and not full:
        old = CodebaseMemory.load(graph_path).graph

    stats = UpdateStats()
    records = scan(root)
    stats.files = len(records)
    export_tree(root, records, memory_dir)

    upgrade_mock = provider.name != "mock"
    graph = build_graph(records)
    if old is not None:
        changed = _carry_over(old, graph, upgrade_mock=upgrade_mock)
    else:
        changed = {r.rel_path for r in records}
    stats.changed = sorted(changed)

    stats.summarized = generate_summaries(
        graph, root, provider,
        on_progress=lambda p, d, t: echo(f"  summarize [{d}/{t}] {p}"),
        only_paths=changed,
    )

    narrative_cache = (
        _narrative_cache(old, changed, upgrade_mock=upgrade_mock) if old is not None else {}
    )
    extra = _prior_discovered(old) if old is not None else []
    new_files = old is not None and any(not old.has_node(f"file:{p}") for p in changed)
    # A build done with the mock provider never ran LLM feature discovery
    # (discover_features_llm returns [] for mock). When a real provider
    # appears, upgrading the summaries alone would leave the project
    # feature-less forever — so a mock->real upgrade also re-discovers.
    mock_refresh = upgrade_mock and old is not None and any(
        (old.nodes[f"file:{p}"].get("summary_meta") or {}).get("provider") == "mock"
        for p in changed if old.has_node(f"file:{p}")
    )
    feats = build_features(
        graph, provider,
        on_progress=lambda name, d, t: echo(f"  trace [{d}/{t}] {name}"),
        narrative_cache=narrative_cache,
        extra_features=extra,
        # discovery on full builds, new files, and mock->real upgrades
        discover=(old is None or new_files or mock_refresh),
    )
    stats.features = len(feats)

    if old is not None:
        # exercised_by mapping and AI reviews survive updates for features whose
        # members didn't change (same freshness rule as narratives)
        for feat in feats:
            if old.has_node(feat.node_id) and feat.name in narrative_cache:
                for attr in ("exercised_by", "review"):
                    value = old.nodes[feat.node_id].get(attr)
                    if value:
                        graph.nodes[feat.node_id][attr] = value
        # app-level rollups carry over wholesale (regenerate with cms review/suggest)
        for node_id in ("review:app", "suggestions:app"):
            if old.has_node(node_id) and not graph.has_node(node_id):
                graph.add_node(node_id, **dict(old.nodes[node_id]))

    git_info = enrich_graph_with_git(graph, root)
    if git_info:
        stats.git_files = git_info["files"]
        echo(f"  git: {git_info['files']} files enriched, {git_info['cochange_edges']} co-change edges")

    export_graph(graph, memory_dir)
    export_summaries(graph, memory_dir)
    export_features(graph, memory_dir)
    export_index(graph, memory_dir, file_count=len(records))
    return stats


# @memory:feature:IncrementalUpdates
# @memory:connects:FeatureExpectationReview, RankedSuggestionGeneration, AppMode
# @memory:summary:Completes a new project's judgment layer — builds the AI review and ROI suggestions once, when absent and a real provider is available, so app-mode first builds trigger every module (not just the map).
def ensure_judgment(root: Path, provider: SummaryProvider, echo=print) -> dict:
    """Build the review + suggestions rollups if the graph doesn't have them.

    New projects end their first build with a map but no judgment layer
    (review/suggestions only existed behind manual CLI commands). Called
    after a build: no-op when both rollups exist, when there is no graph,
    or when only the mock provider is available (mock output must never
    pose as an AI review — the caller should say so instead).
    Returns {"review": bool, "suggestions": bool} — what was built.

    Serialized process-wide: app startup sync, the UI build worker and the
    watcher can coexist, and concurrent callers must not both pay for an
    LLM review of the same project (the absence check re-runs under the
    lock, so the loser sees the winner's nodes and no-ops).
    """
    ran = {"review": False, "suggestions": False}
    graph_path = root / config.MEMORY_DIR_NAME / "graph.json"
    if provider.name == "mock" or not graph_path.is_file():
        return ran
    with _judgment_lock:
        return _ensure_judgment_locked(root, provider, echo, graph_path, ran)


_judgment_lock = threading.Lock()


def _ensure_judgment_locked(root, provider, echo, graph_path, ran) -> dict:
    mem = CodebaseMemory.load(graph_path)
    memory_dir = root / config.MEMORY_DIR_NAME

    if not mem.graph.has_node("review:app"):
        from .review import build_review, export_review

        echo("  review: building alignment audit (first run for this project)")
        build_review(mem.graph, root, provider,
                     on_progress=lambda name, d, t: echo(f"  review [{d}/{t}] {name}"))
        export_review(mem.graph, memory_dir)
        ran["review"] = True

    if not mem.graph.has_node("suggestions:app"):
        from .suggest import build_suggestions, export_suggestions

        echo("  suggest: ranking what to build next")
        build_suggestions(mem.graph, root, provider)
        export_suggestions(mem.graph, memory_dir)
        ran["suggestions"] = True

    if ran["review"] or ran["suggestions"]:
        export_graph(mem.graph, memory_dir)
    return ran


def _scan_signature(root: Path) -> tuple:
    return tuple(sorted((r.rel_path, r.mtime, r.size_bytes) for r in scan(root)))


def watch(root: Path, provider: SummaryProvider, interval: float = 2.0, echo=print) -> None:
    """Poll for changes and update incrementally. Ctrl+C to stop."""
    root = root.resolve()
    echo(f"cms watch: {root.name} (every {interval:g}s, Ctrl+C to stop)")
    last = _scan_signature(root)
    if not (root / config.MEMORY_DIR_NAME / "graph.json").is_file():
        echo("no memory layer yet — building initial one")
        incremental_update(root, provider, echo=echo)
    try:
        while True:
            time.sleep(interval)
            current = _scan_signature(root)
            if current == last:
                continue
            time.sleep(interval)  # debounce: let a save-burst settle
            current = _scan_signature(root)
            changed_count = len(set(current) ^ set(last))
            echo(f"\nchange detected ({changed_count} entries) — updating memory")
            stats = incremental_update(root, provider, echo=echo)
            echo(
                f"updated: {stats.summarized} files re-summarized, "
                f"{stats.features} features traced"
            )
            last = current
    except KeyboardInterrupt:
        echo("\ncms watch stopped.")
