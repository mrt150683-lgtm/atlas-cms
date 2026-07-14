"""Semantic hierarchy — the pyramid levels above features.

Builds ``system:`` and ``component:`` nodes over the existing feature layer so
the Human View can render the codebase at low resolution:

    system  ->  component  ->  feature  ->  file  ->  function  ->  source

Components group features (PART_OF edges, matching the member->feature
convention); systems group components. With a real provider the grouping is
one LLM call over the feature list + top-level directories; under mock a
deterministic structural grouping (one component per top-level source dir)
keeps the Human View usable and is clearly labelled as such.

Evidence rules mirror feature discovery (`update._run_discovery`): the stage
records positive evidence in ``semantic_state.json`` including the full
hierarchy spec, so a completed grouping is re-applied from durable state on
every rebuild — never re-charged while its input hash is unchanged. Mock
never writes completion markers.
"""

from __future__ import annotations

import json
import re
import threading
from pathlib import Path

from . import semantic_state as ss
from .providers import SummaryProvider

STAGE = "hierarchy"
# bump when the prompt / grouping semantics change enough that old
# hierarchies should be considered non-current
HIERARCHY_SCHEMA_VERSION = 1
RETRY_COOLDOWN_S = 600
MAX_FEATURE_DESC = 160

_hierarchy_lock = threading.Lock()

STRUCTURAL_NOTE = ("Structural grouping only - run `cms update` with an API key "
                   "for a semantic hierarchy.")

_PROMPT = """You are organising a mapped codebase into a semantic pyramid for human comprehension.

PROJECT: {project}
TOP-LEVEL DIRECTORIES: {dirs}

FEATURES (name | where its code lives | what it does):
{features}

Group these features into components (major responsibilities / subsystems), and the components into systems (top-level architectural areas). Rules:
- 1 to 3 systems; 3 to 8 components in total.
- Every feature listed above appears in EXACTLY ONE component. Use the exact feature names given.
- Each component lists the top-level directories its code mostly lives in (from the directory list above).
- Names are short PascalCase labels (e.g. MemoryPipeline, AgentInterface).
- Descriptions are 1-2 plain-language sentences about responsibility, not implementation trivia.

Return ONLY a JSON object, no commentary, shaped exactly like:
{{"systems": [{{"name": "...", "description": "...", "components": [{{"name": "...", "description": "...", "features": ["FeatureA"], "dirs": ["cms"]}}]}}]}}
"""


class HierarchyError(RuntimeError):
    """Provider failure or unusable grouping output."""


def _sanitize(name: str) -> str:
    clean = re.sub(r"[^A-Za-z0-9]+", " ", str(name)).strip()
    return "".join(w[:1].upper() + w[1:] for w in clean.split()) or "Unnamed"


def top_dirs(graph) -> list[str]:
    """Sorted top-level directory names of all scanned files ('' -> '.')."""
    dirs = set()
    for _, a in graph.nodes(data=True):
        if a.get("type") == "file" and a.get("path"):
            parts = a["path"].replace("\\", "/").split("/")
            dirs.add(parts[0] if len(parts) > 1 else ".")
    return sorted(dirs)


def hierarchy_input_hash(graph) -> str:
    """The hierarchy depends on the feature set and the directory shape."""
    return ss._sha([HIERARCHY_SCHEMA_VERSION, ss.feature_set_hash(graph), top_dirs(graph)])


def _feature_rows(graph) -> list[dict]:
    rows = []
    for node_id, a in graph.nodes(data=True):
        if a.get("type") != "feature":
            continue
        dirs = set()
        for m in a.get("members") or []:
            if graph.has_node(m):
                path = (graph.nodes[m].get("path") or "").replace("\\", "/")
                if path:
                    parts = path.split("/")
                    dirs.add(parts[0] if len(parts) > 1 else ".")
        rows.append({"name": a.get("name", ""), "node_id": node_id,
                     "dirs": sorted(dirs),
                     "description": (a.get("description") or "").strip()[:MAX_FEATURE_DESC]})
    return sorted(rows, key=lambda r: r["name"])


# ── spec construction ──────────────────────────────────────────────────────

def structural_spec(graph, project: str) -> dict:
    """No-LLM fallback: one system, one component per top-level source dir,
    features assigned to the dir holding most of their members."""
    rows = _feature_rows(graph)
    by_dir: dict[str, list[str]] = {}
    for r in rows:
        primary = r["dirs"][0] if r["dirs"] else "."
        # majority dir: count member dirs, first alphabetically wins ties
        counts: dict[str, int] = {}
        for d in r["dirs"]:
            counts[d] = counts.get(d, 0) + 1
        if counts:
            primary = max(sorted(counts), key=lambda d: counts[d])
        by_dir.setdefault(primary, []).append(r["name"])
    components = [
        {"name": _sanitize(d if d != "." else "Root"),
         "description": f"Code under `{d}/`. {STRUCTURAL_NOTE}" if d != "." else
                        f"Code at the project root. {STRUCTURAL_NOTE}",
         "features": sorted(names), "dirs": [d]}
        for d, names in sorted(by_dir.items())
    ] or [{"name": "Root", "description": STRUCTURAL_NOTE, "features": [], "dirs": ["."]}]
    return {"systems": [{
        "name": _sanitize(project),
        "description": f"The {project} codebase. {STRUCTURAL_NOTE}",
        "components": components,
    }]}


def _parse_spec(reply: str, known_features: set[str]) -> dict:
    start, end = reply.find("{"), reply.rfind("}")
    if start < 0 or end <= start:
        raise HierarchyError("no JSON object in the grouping reply")
    try:
        data = json.loads(reply[start:end + 1])
    except json.JSONDecodeError as exc:
        raise HierarchyError(f"unparseable grouping JSON: {exc}") from exc
    systems = data.get("systems")
    if not isinstance(systems, list) or not systems:
        raise HierarchyError("grouping reply has no systems")
    seen: set[str] = set()
    out_systems = []
    for sys_raw in systems[:3]:
        comps = []
        for c in (sys_raw.get("components") or []):
            feats = [f for f in (c.get("features") or [])
                     if f in known_features and f not in seen]
            seen.update(feats)
            comps.append({
                "name": _sanitize(c.get("name", "")),
                "description": str(c.get("description") or "").strip()[:400],
                "features": sorted(feats),
                "dirs": [str(d) for d in (c.get("dirs") or [])][:8],
            })
        if comps:
            out_systems.append({
                "name": _sanitize(sys_raw.get("name", "")),
                "description": str(sys_raw.get("description") or "").strip()[:400],
                "components": comps,
            })
    if not out_systems:
        raise HierarchyError("grouping reply had no usable components")
    unassigned = sorted(known_features - seen)
    if unassigned:
        out_systems[0]["components"].append({
            "name": "Other", "description": "Features the grouping pass did not place.",
            "features": unassigned, "dirs": [],
        })
    return {"systems": out_systems}


def llm_spec(graph, provider: SummaryProvider, project: str) -> dict:
    rows = _feature_rows(graph)
    lines = [f"- {r['name']} | dirs: {', '.join(r['dirs']) or '?'} | {r['description'] or '(no description)'}"
             for r in rows]
    prompt = _PROMPT.format(project=project, dirs=", ".join(top_dirs(graph)) or ".",
                            features="\n".join(lines) or "(none)")
    try:
        reply = provider.summarize(prompt, {"max_tokens": 2500})
    except Exception as exc:
        raise HierarchyError(f"provider error: {exc}") from exc
    return _parse_spec(reply, {r["name"] for r in rows})


# ── graph writing ──────────────────────────────────────────────────────────

def clear_hierarchy(graph) -> None:
    stale = [n for n, a in graph.nodes(data=True)
             if a.get("type") in ("system", "component")]
    graph.remove_nodes_from(stale)


def write_hierarchy(graph, spec: dict, provenance: str) -> dict:
    """Materialize the spec as system:/component: nodes + PART_OF edges.
    Idempotent: clears previous hierarchy nodes first. Returns counts."""
    clear_hierarchy(graph)
    systems = components = 0
    for sys_raw in spec.get("systems", []):
        sys_id = f"system:{sys_raw['name']}"
        comp_ids = [f"component:{c['name']}" for c in sys_raw.get("components", [])]
        graph.add_node(sys_id, type="system", name=sys_raw["name"], path="",
                       description=sys_raw.get("description", ""),
                       members=comp_ids, provenance=provenance)
        systems += 1
        for c in sys_raw.get("components", []):
            comp_id = f"component:{c['name']}"
            feat_ids = [f"feature:{f}" for f in c.get("features", [])
                        if graph.has_node(f"feature:{f}")]
            graph.add_node(comp_id, type="component", name=c["name"], path="",
                           description=c.get("description", ""),
                           members=feat_ids, dirs=list(c.get("dirs", [])),
                           provenance=provenance)
            graph.add_edge(comp_id, sys_id, type="PART_OF", provenance=provenance)
            components += 1
            for fid in feat_ids:
                graph.add_edge(fid, comp_id, type="PART_OF", provenance=provenance)
    return {"systems": systems, "components": components}


# @memory:feature:HumanViewResolution
# @memory:connects:FeatureTracing, IncrementalUpdates, MemoryViewer
# @memory:summary:Evidence-gated construction of the system/component pyramid over features — one LLM grouping call per input-hash change, durable spec recovery from semantic state, labelled structural fallback under mock.
def ensure_hierarchy(memory_dir: Path, graph, provider: SummaryProvider,
                     echo=print, force: bool = False) -> bool:
    """Make sure the graph carries a hierarchy; charge the LLM only when the
    durable evidence says the grouping is missing or its inputs changed.
    Returns True when a fresh LLM grouping ran."""
    project = Path(memory_dir).resolve().parent.name
    input_hash = hierarchy_input_hash(graph)

    with _hierarchy_lock:
        state = ss.load_state(memory_dir)
        rec = ss.stage(state, STAGE)

        # positively recorded grouping over identical input: re-apply from
        # state, never re-charge (survives full rebuilds — the spec is durable)
        if (not force and rec.get("status") == "complete"
                and rec.get("input_hash") == input_hash and rec.get("hierarchy_spec")):
            write_hierarchy(graph, rec["hierarchy_spec"],
                            "llm" if rec.get("real_provider") else "heuristic")
            return False

        if provider.name == "mock":
            write_hierarchy(graph, structural_spec(graph, project), "heuristic")
            if rec.get("status") == "never_run":
                ss.record_stage(
                    memory_dir, STAGE, status="skipped",
                    provider="mock", real_provider=False, input_hash=input_hash,
                    reason="semantic hierarchy requires a real provider "
                           "(structural grouping shown instead)",
                )
            echo("  hierarchy: structural grouping (mock provider; not recorded complete)")
            return False

        rerun = force or rec.get("status") in ("never_run", "skipped")
        if rec.get("status") == "failed":
            if rec.get("input_hash") != input_hash:
                rerun = True
            elif _older_than(rec.get("generated_at"), RETRY_COOLDOWN_S):
                rerun = True
        if rec.get("status") == "complete" and rec.get("input_hash") != input_hash:
            rerun = True

        if not rerun:
            # keep the last known grouping visible while we wait to retry
            last = rec if rec.get("hierarchy_spec") else (rec.get("last_success") or {})
            spec = last.get("hierarchy_spec")
            write_hierarchy(graph, spec or structural_spec(graph, project),
                            "llm" if (spec and last.get("real_provider")) else "heuristic")
            return False

        echo("  hierarchy: grouping features into components/systems (LLM)")
        try:
            spec = llm_spec(graph, provider, project)
        except HierarchyError as exc:
            keep = {"last_success": rec} if rec.get("status") == "complete" else {}
            ss.record_stage(
                memory_dir, STAGE, status="failed",
                provider=provider.name, model=getattr(provider, "model", None),
                real_provider=True, input_hash=input_hash, error=str(exc)[:300], **keep,
            )
            last = rec.get("hierarchy_spec") or (rec.get("last_success") or {}).get("hierarchy_spec")
            write_hierarchy(graph, last or structural_spec(graph, project),
                            "llm" if last and rec.get("real_provider") else "heuristic")
            echo(f"  hierarchy: FAILED - {exc} (recorded; a later update will retry)")
            return False

        counts = write_hierarchy(graph, spec, "llm")
        ss.record_stage(
            memory_dir, STAGE, status="complete",
            provider=provider.name, model=getattr(provider, "model", None),
            real_provider=True, input_hash=input_hash,
            hierarchy_spec=spec, **counts,
        )
        echo(f"  hierarchy: {counts['systems']} system(s), {counts['components']} component(s) (recorded)")
        return True


def _older_than(iso_ts, seconds: float) -> bool:
    from .update import _older_than as impl

    return impl(iso_ts, seconds)
