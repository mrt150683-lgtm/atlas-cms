"""Incremental updates — keep .memory/ in sync without re-paying LLM costs.

``incremental_update`` rebuilds the structural graph (fast, always), but
carries over summaries for files whose mtime is unchanged and feature
narratives whose members didn't change, so only genuinely changed nodes hit
the LLM. ``watch`` polls for changes and re-runs the update.
"""

from __future__ import annotations

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
            if attrs.get("language") == "python":
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
        changed = {r.rel_path for r in records if r.language == "python"}
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
    feats = build_features(
        graph, provider,
        on_progress=lambda name, d, t: echo(f"  trace [{d}/{t}] {name}"),
        narrative_cache=narrative_cache,
        extra_features=extra,
        discover=(old is None or new_files),  # discovery only on full builds / new files
    )
    stats.features = len(feats)

    if old is not None:
        # verified_by mapping and AI reviews survive updates for features whose
        # members didn't change (same freshness rule as narratives)
        for feat in feats:
            if old.has_node(feat.node_id) and feat.name in narrative_cache:
                for attr in ("verified_by", "review"):
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
