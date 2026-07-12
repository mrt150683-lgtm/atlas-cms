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
from . import semantic_state as ss
from .exporter import export_features, export_graph, export_index, export_summaries
from .features import DiscoveryError, Feature, build_features, discover_features_llm, prepare_known
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
    # record the summaries stage (always reflects the latest sync)
    ss.record_stage(
        memory_dir, "summaries", status="complete",
        provider=provider.name, model=getattr(provider, "model", None),
        real_provider=provider.name != "mock",
        files=stats.files, summarized=stats.summarized,
        mock_labelled=sum(
            1 for _, a in graph.nodes(data=True)
            if a.get("type") == "file"
            and (a.get("summary_meta") or {}).get("provider") == "mock"
        ),
    )

    # Feature discovery is its own serialized, evidence-recorded phase.
    # Durable state — not incidental graph contents — decides whether it
    # runs, so legacy projects (real summaries, zero features, no state
    # record) migrate on a NORMAL update: their stage reads never_run.
    extra, discovery_ran = _run_discovery(
        memory_dir, graph, provider, extra,
        force=(old is None or new_files or mock_refresh), echo=echo,
    )
    feats = build_features(
        graph, provider,
        on_progress=lambda name, d, t: echo(f"  trace [{d}/{t}] {name}"),
        narrative_cache=narrative_cache,
        extra_features=extra,
        discover=False,  # discovery already handled above, with evidence
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

    if discovery_ran:
        # finalize the evidence with what actually landed on disk
        rec = ss.stage(ss.load_state(memory_dir), "features")
        ss.record_stage(memory_dir, "features", **{
            **rec, "feature_set_hash": ss.feature_set_hash(graph),
            **ss.feature_counts(graph),
        })
    return stats


_discovery_lock = threading.Lock()


def _features_from_state(rec: dict, extras: list, graph) -> list:
    """Recover discovered features carried in the durable state record when
    they are missing from the graph/extras (e.g. a concurrent writer or an
    interrupted export lost them). State is the source of truth."""
    have = {f.name for f in extras}
    out = []
    for item in rec.get("discovered_features", []):
        members = [m for m in item.get("members", []) if graph.has_node(m)]
        if item.get("name") and item["name"] not in have and members:
            out.append(Feature(
                name=item["name"], description=item.get("description", ""),
                source="discovered", members=members,
            ))
    return out


def _run_discovery(memory_dir: Path, graph, provider, extras: list,
                   force: bool, echo=print) -> tuple[list, bool]:
    """Run LLM feature discovery exactly when the durable evidence says it
    is needed; return (extra_features_to_build_with, ran_this_call).

    Rules (see semantic_state module docstring):
    - mock never discovers and never creates completion markers; it records
      an explicit `skipped` only when NO record exists yet (a mock run must
      never downgrade real evidence).
    - never_run / failed / skipped -> run (this is the legacy migration and
      the retry path). complete -> run only when forced (new files,
      mock->real upgrade, full rebuild); an unchanged complete record is
      never re-charged.
    - provider errors / malformed output record `failed` (with the prior
      complete record preserved under last_success) — never an empty
      'success'. A legitimate zero-feature result IS recorded complete.
    - serialized: concurrent updaters cannot double-charge; the loser
      re-reads the winner's record and recovers its features from state.
    """
    input_hash = ss.discovery_input_hash(graph)
    if provider.name == "mock":
        rec = ss.stage(ss.load_state(memory_dir), "features")
        if rec.get("status") == "never_run":
            ss.record_stage(
                memory_dir, "features", status="skipped",
                provider="mock", real_provider=False, input_hash=input_hash,
                reason="feature discovery requires a real provider "
                       "(configure an API key, then run cms update)",
            )
        return extras + _features_from_state(rec, extras, graph), False

    with _discovery_lock:
        state = ss.load_state(memory_dir)
        rec = ss.stage(state, "features")
        rerun = force or rec.get("status") in ("never_run", "skipped")
        if rec.get("status") == "failed" and rec.get("input_hash") != input_hash:
            rerun = True  # retry a recorded failure once something changed
        if rec.get("status") == "complete" and rec.get("input_hash") == input_hash:
            rerun = False  # positively recorded success over identical input: never re-charge
        if not rerun:
            return extras + _features_from_state(rec, extras, graph), False

        feats_map, known_files, is_dup = prepare_known(graph, extras)
        echo("  discover: mapping features (LLM)")
        try:
            found = [
                f for f in discover_features_llm(
                    graph, provider, known=list(feats_map), known_files=known_files)
                if not is_dup(f)
            ]
        except DiscoveryError as exc:
            keep = {"last_success": rec} if rec.get("status") == "complete" else {}
            ss.record_stage(
                memory_dir, "features", status="failed",
                provider=provider.name, model=getattr(provider, "model", None),
                real_provider=True, input_hash=input_hash,
                error=str(exc)[:300], **keep,
            )
            echo(f"  discover: FAILED — {exc} (recorded; a later update will retry)")
            return extras + _features_from_state(rec, extras, graph), False

        all_discovered = [f for f in extras if f.source == "discovered"] + found
        ss.record_stage(
            memory_dir, "features", status="complete",
            provider=provider.name, model=getattr(provider, "model", None),
            real_provider=True, input_hash=input_hash,
            discovered_features=[
                {"name": f.name, "description": f.description, "members": list(f.members)}
                for f in all_discovered
            ],
        )
        echo(f"  discover: {len(found)} new feature(s) found"
             if found else "  discover: complete — no new features (recorded)")
        return extras + found, True


# @memory:feature:IncrementalUpdates
# @memory:connects:FeatureExpectationReview, RankedSuggestionGeneration, AppMode
# @memory:summary:Completes a new project's judgment layer — builds the AI review and ROI suggestions once, when absent and a real provider is available, so app-mode first builds trigger every module (not just the map).
def ensure_judgment(root: Path, provider: SummaryProvider, echo=print) -> dict:
    """Initialization recovery for the judgment layer — validity-aware.

    Rebuilds review/suggestions when the existing artifacts are MISSING or
    INVALID (mock/structural output, no semantic-state evidence — the
    legacy case — or generated against an empty pre-discovery feature
    set). A VALID real-provider judgment is left alone even when its
    recorded feature-set hash no longer matches (deliberate review-freeze
    policy: staleness is exposed, refresh is the explicit `cms review` /
    `cms suggest`). Initialization recovery is not routine refresh.

    Requires: a graph, a real provider, and positively recorded feature
    discovery — judgment of a feature set that was never validly
    discovered is exactly the invalid artifact class this repairs.

    Serialized process-wide; validity is re-checked INSIDE the lock, so a
    losing concurrent caller reloads the winner's result and no-ops.
    Returns {"review": bool, "suggestions": bool} — what was built.
    """
    ran = {"review": False, "suggestions": False}
    graph_path = root / config.MEMORY_DIR_NAME / "graph.json"
    if provider.name == "mock" or not graph_path.is_file():
        return ran
    with _judgment_lock:
        return _ensure_judgment_locked(root, provider, echo, graph_path, ran)


_judgment_lock = threading.Lock()


def _ensure_judgment_locked(root, provider, echo, graph_path, ran) -> dict:
    import hashlib
    import json as _json

    mem = CodebaseMemory.load(graph_path)
    memory_dir = root / config.MEMORY_DIR_NAME
    state = ss.load_state(memory_dir)

    if ss.stage(state, "features").get("status") != "complete":
        echo("  judgment: waiting for a positively recorded feature discovery")
        return ran

    fsh = ss.feature_set_hash(mem.graph)
    counts = ss.feature_counts(mem.graph)
    common = dict(provider=provider.name, model=getattr(provider, "model", None),
                  real_provider=True, feature_set_hash=fsh, **counts)

    verdict, reason = ss.judgment_validity(state, mem.graph, "review:app", "review")
    if verdict in ("missing", "invalid"):
        from .review import build_review, export_review

        echo(f"  review: building alignment audit ({reason})")
        try:
            result = build_review(
                mem.graph, root, provider,
                on_progress=lambda name, d, t: echo(f"  review [{d}/{t}] {name}"))
        except Exception as exc:
            ss.record_stage(memory_dir, "review", status="failed",
                            error=str(exc)[:300], **common)
            echo(f"  review: FAILED — {exc} (recorded; artifacts unchanged)")
            return ran
        feature_reviews = result.get("features") or {}
        all_structural = counts["feature_count"] > 0 and feature_reviews and all(
            r.get("structural") for r in feature_reviews.values())
        if all_structural:
            # malformed provider output everywhere -> not a semantic review;
            # do NOT export over whatever exists, record the failure.
            ss.record_stage(memory_dir, "review", status="failed",
                            error="provider returned unusable review output for every feature",
                            **common)
            echo("  review: provider output unusable — recorded failed, artifacts unchanged")
            return ran
        export_review(mem.graph, memory_dir)
        out_hash = hashlib.sha256(_json.dumps(
            {n: r.get("verdict") for n, r in feature_reviews.items()},
            sort_keys=True).encode()).hexdigest()[:24]
        ss.record_stage(memory_dir, "review", status="complete",
                        output_hash=out_hash, **common)
        ran["review"] = True
    elif verdict == "stale":
        echo("  review: valid but frozen-stale (feature set changed) — refresh with `cms review`")

    verdict, reason = ss.judgment_validity(state, mem.graph, "suggestions:app", "suggestions")
    if verdict in ("missing", "invalid"):
        from .suggest import build_suggestions, export_suggestions

        echo(f"  suggest: ranking what to build next ({reason})")
        try:
            items = build_suggestions(mem.graph, root, provider)
        except Exception as exc:
            ss.record_stage(memory_dir, "suggestions", status="failed",
                            error=str(exc)[:300], **common)
            echo(f"  suggest: FAILED — {exc} (recorded; artifacts unchanged)")
            items = None
        if items is not None:
            if not items and counts["feature_count"] > 0:
                ss.record_stage(memory_dir, "suggestions", status="failed",
                                error="provider produced no usable suggestions "
                                      "for a non-empty feature set", **common)
                echo("  suggest: empty output for a non-empty feature set — recorded failed")
            else:
                export_suggestions(mem.graph, memory_dir)
                out_hash = hashlib.sha256(_json.dumps(
                    [s.get("title") for s in items], sort_keys=True
                ).encode()).hexdigest()[:24]
                ss.record_stage(memory_dir, "suggestions", status="complete",
                                output_hash=out_hash, items=len(items), **common)
                ran["suggestions"] = True
    elif verdict == "stale":
        echo("  suggest: valid but frozen-stale — refresh with `cms suggest`")

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
