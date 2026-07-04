"""Feature tracing — first-class features with flows, narratives, and traceability.

A *feature* is a named capability spanning components (e.g. CleanDirectoryScanner).
Sources:
  1. Declared — ``@memory:feature:Name`` anchors on components/files.
  2. Discovered — an LLM pass over file summaries proposes additional features
     (skipped for the mock provider).

For each feature we compute members, entry points, and *flows* (call chains
walked through the CALLS graph with file:line at every step), then generate a
narrative + verification checklist so a human can confirm the implementation
matches intent. Everything lands in the graph as ``feature:{Name}`` nodes with
``PART_OF`` / ``CONNECTS`` edges, so the query engine finds features too.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

import networkx as nx

from .providers import SummaryProvider

MAX_FLOW_DEPTH = 6
MAX_FLOWS_PER_FEATURE = 8


@dataclass
class Feature:
    name: str
    description: str = ""
    source: str = "declared"          # "declared" | "discovered"
    members: list[str] = field(default_factory=list)       # node ids
    entry_points: list[str] = field(default_factory=list)  # node ids
    flows: list[list[dict]] = field(default_factory=list)  # step dicts
    connects: list[str] = field(default_factory=list)      # feature names
    narrative: str = ""
    narrative_provider: str = ""

    @property
    def node_id(self) -> str:
        return f"feature:{self.name}"


# ── 1. declared features (anchors) ─────────────────────────────────────────

def collect_declared_features(graph: nx.DiGraph) -> dict[str, Feature]:
    features: dict[str, Feature] = {}
    for node_id, attrs in graph.nodes(data=True):
        anchors = attrs.get("anchors") or {}
        for name in anchors.get("feature", []):
            feat = features.setdefault(name, Feature(name=name))
            feat.members.append(node_id)
            for other in anchors.get("connects", []):
                if other not in feat.connects:
                    feat.connects.append(other)
            for desc in anchors.get("summary", []):
                if not feat.description:
                    feat.description = desc
    return features


# ── 2. discovered features (LLM over file summaries) ───────────────────────

DISCOVERY_PROMPT = """You are mapping a codebase into named FEATURES (user-facing or architectural capabilities).

Below are one-line purposes of every source file, plus already-known features.
Identify up to {max_new} ADDITIONAL features that are clearly present. Skip anything
already known, skip test-only concerns, skip vague umbrella names.

Files:
{file_lines}

Already known features: {known}

Respond with ONLY a JSON array, no prose:
[{{"name": "PascalCaseName", "description": "one sentence", "files": ["rel/path.py", ...]}}]
Use exact rel paths from the list. Return [] if nothing new is clear.
"""


def discover_features_llm(
    graph: nx.DiGraph, provider: SummaryProvider, known: list[str], max_new: int = 6
) -> list[Feature]:
    if provider.name == "mock":
        return []
    file_lines = []
    for node_id, attrs in sorted(graph.nodes(data=True)):
        if attrs.get("type") == "file" and attrs.get("summary"):
            purpose = attrs["summary"].strip().splitlines()
            head = next((l.strip() for l in purpose if l.strip() and not l.strip().startswith("#")), "")
            file_lines.append(f"- {attrs['path']}: {head[:160]}")
    prompt = DISCOVERY_PROMPT.format(
        max_new=max_new, file_lines="\n".join(file_lines), known=", ".join(known) or "(none)"
    )
    try:
        raw = provider.summarize(prompt, {})
        match = re.search(r"\[[\s\S]*\]", raw)
        items = json.loads(match.group(0)) if match else []
    except Exception:
        return []
    features = []
    for item in items[:max_new]:
        name = str(item.get("name", "")).strip()
        if not name or name in known:
            continue
        members = [
            f"file:{p}" for p in item.get("files", []) if graph.has_node(f"file:{p}")
        ]
        if not members:
            continue
        features.append(
            Feature(
                name=name,
                description=str(item.get("description", "")),
                source="discovered",
                members=members,
            )
        )
    return features


# ── 3. flow tracing over the CALLS graph ────────────────────────────────────

def _expand_members(graph: nx.DiGraph, members: list[str]) -> list[str]:
    """File members expand to their contained functions/classes for flow tracing."""
    out: list[str] = []
    seen = set()

    def add(nid: str) -> None:
        if nid not in seen and graph.has_node(nid):
            seen.add(nid)
            out.append(nid)

    for m in members:
        add(m)
        if m.startswith("file:"):
            for _, child, d in graph.out_edges(m, data=True):
                if d.get("type") == "CONTAINS":
                    add(child)
                    for _, grand, d2 in graph.out_edges(child, data=True):
                        if d2.get("type") == "CONTAINS":
                            add(grand)
    return out


def _step(graph: nx.DiGraph, node_id: str, in_feature: bool) -> dict:
    a = graph.nodes[node_id]
    return {
        "id": node_id,
        "name": a.get("qualname", a.get("name", node_id)),
        "path": a.get("path", ""),
        "line": a.get("start_line"),
        "in_feature": in_feature,
    }


def trace_flows(graph: nx.DiGraph, members: list[str]) -> tuple[list[str], list[list[dict]]]:
    """Returns (entry_points, flows). Entry points are member callables not
    called by other members; each flow walks CALLS edges depth-first."""
    callables = {m for m in _expand_members(graph, members) if m.split(":", 1)[0] in ("func", "class")}
    if not callables:
        return [], []

    def member_callers(nid: str) -> int:
        return sum(
            1 for s, _, d in graph.in_edges(nid, data=True)
            if d.get("type") == "CALLS" and s in callables
        )

    entries = sorted(n for n in callables if member_callers(n) == 0) or sorted(callables)[:1]

    def callees(nid: str, visited: set[str]) -> list[str]:
        found = [
            t for _, t, d in graph.out_edges(nid, data=True)
            if d.get("type") == "CALLS" and t not in visited
        ]
        # source order (line number), inside-feature first on ties
        return sorted(found, key=lambda t: (graph.nodes[t].get("start_line") or 0, t not in callables))

    flows: list[list[dict]] = []
    for entry in entries:
        if len(flows) >= MAX_FLOWS_PER_FEATURE:
            break
        first_hops = callees(entry, {entry})
        if not first_hops:
            flows.append([_step(graph, entry, True)])
            continue
        # one flow per direct callee of the entry point, each extended depth-first
        for hop in first_hops[:4]:
            if len(flows) >= MAX_FLOWS_PER_FEATURE:
                break
            flow = [_step(graph, entry, True), _step(graph, hop, hop in callables)]
            visited = {entry, hop}
            frontier = hop
            for _ in range(MAX_FLOW_DEPTH - 1):
                nxt = next(iter(callees(frontier, visited)), None)
                if nxt is None:
                    break
                visited.add(nxt)
                flow.append(_step(graph, nxt, nxt in callables))
                frontier = nxt
            flows.append(flow)
    return entries, flows


# ── 4. narrative generation ────────────────────────────────────────────────

NARRATIVE_PROMPT = """You are writing a FEATURE TRACE for engineers verifying that an implementation matches intent.

Feature: {name}
Description: {description}
Cross-feature connections: {connects}

Member components:
{member_lines}

Traced call flows (entry -> calls, with file:line):
{flow_lines}

Write a concise trace document with EXACTLY these sections:

## Purpose
(1-2 sentences: what this feature does for the user/system.)

## Flow
(Numbered steps following the traced call chains. Each step: `name` (file:line) — what happens and what data moves. Merge flows that overlap.)

## Inputs & Outputs
(What enters the feature, what it produces/mutates.)

## Verification Checklist
(3-6 concrete, observable checks a human can run to confirm the feature behaves as intended — commands to run, outputs to inspect, edge cases to try.)

Rules: ground every claim in the components/flows above; never invent behaviour; keep it under ~35 lines.
"""


def _member_lines(graph: nx.DiGraph, feat: Feature) -> str:
    lines = []
    for m in _expand_members(graph, feat.members)[:24]:
        a = graph.nodes[m]
        kind = a.get("type", "?")
        loc = f"{a.get('path', '?')}:{a.get('start_line', '?')}-{a.get('end_line', '?')}" if kind != "file" else a.get("path", "?")
        head = a.get("signature") or a.get("qualname") or a.get("name", m)
        doc = (a.get("summary") or a.get("docstring") or "").strip().splitlines()
        note = f" — {doc[0][:120]}" if doc else ""
        lines.append(f"- [{kind}] {head} ({loc}){note}")
    return "\n".join(lines)


def _flow_lines(flows: list[list[dict]]) -> str:
    out = []
    for flow in flows:
        chain = " -> ".join(
            f"{s['name']} ({s['path']}:{s['line']})" + ("" if s["in_feature"] else " [outside feature]")
            for s in flow
        )
        out.append(f"- {chain}")
    return "\n".join(out) or "- (no call flows traced)"


def narrate_feature(graph: nx.DiGraph, feat: Feature, provider: SummaryProvider) -> str:
    if provider.name == "mock":
        return _mock_narrative(graph, feat)
    prompt = NARRATIVE_PROMPT.format(
        name=feat.name,
        description=feat.description or "(none declared)",
        connects=", ".join(feat.connects) or "(none)",
        member_lines=_member_lines(graph, feat),
        flow_lines=_flow_lines(feat.flows),
    )
    return provider.summarize(prompt, {}).strip()


def _mock_narrative(graph: nx.DiGraph, feat: Feature) -> str:
    lines = [
        "## Purpose",
        feat.description or f"{feat.name} (no declared description — structural trace only).",
        "",
        "## Flow",
    ]
    for i, flow in enumerate(feat.flows, 1):
        for j, s in enumerate(flow):
            marker = "entry" if j == 0 else "then"
            outside = "" if s["in_feature"] else " (outside feature)"
            lines.append(f"{i}.{j + 1} [{marker}] `{s['name']}` ({s['path']}:{s['line']}){outside}")
    if not feat.flows:
        lines.append("(no call flows traced)")
    lines += [
        "",
        "## Verification Checklist",
        f"- Confirm each member above exists at the stated file:line.",
        f"- Run the entry point(s) and observe the traced call order.",
        "(Mock narrative — set an API key and rerun `cms trace` for a full AI trace.)",
    ]
    return "\n".join(lines)


# ── 5. orchestration ────────────────────────────────────────────────────────

# @memory:feature:FeatureTracing
# @memory:connects:MemoryAnchors, KnowledgeGraphConstruction, FeatureVerification
# @memory:summary:Features as first-class graph nodes — declared via anchors or LLM-discovered, with entry points, branching call flows, narratives and verification checklists.
def build_features(
    graph: nx.DiGraph, provider: SummaryProvider, on_progress=None,
    narrative_cache: dict[str, str] | None = None,
    extra_features: list[Feature] | None = None,
    discover: bool = True,
) -> list[Feature]:
    """Discover + trace + narrate all features, writing them into the graph.

    `narrative_cache` maps feature name -> (narrative, original_provider) to reuse; `extra_features`
    re-injects previously discovered features (whose source is not anchors);
    `discover=False` skips the LLM discovery pass (incremental updates)."""
    features = collect_declared_features(graph)
    for feat in extra_features or []:
        feat.members = [m for m in feat.members if graph.has_node(m)]
        if feat.members and feat.name not in features:
            features[feat.name] = feat
    if discover:
        for feat in discover_features_llm(graph, provider, known=list(features)):
            features[feat.name] = feat

    result = []
    for i, feat in enumerate(sorted(features.values(), key=lambda f: f.name), 1):
        feat.entry_points, feat.flows = trace_flows(graph, feat.members)
        try:
            cached = (narrative_cache or {}).get(feat.name)
            if cached:
                feat.narrative, feat.narrative_provider = cached
            else:
                feat.narrative = narrate_feature(graph, feat, provider)
                feat.narrative_provider = provider.name
        except Exception as exc:
            # LLM unreachable — keep the structural trace rather than dying,
            # and stop retrying the broken provider for the remaining features
            import sys

            print(f"cms: narrative failed for {feat.name} ({exc}); using structural trace.", file=sys.stderr)
            from .providers import MockProvider

            provider = MockProvider()
            feat.narrative = _mock_narrative(graph, feat)
            feat.narrative_provider = "mock"
        _write_to_graph(graph, feat)
        result.append(feat)
        if on_progress:
            on_progress(feat.name, i, len(features))
    # CONNECTS second pass: targets may have been written after their source
    for feat in result:
        for other in feat.connects:
            other_id = f"feature:{other}"
            if graph.has_node(other_id):
                graph.add_edge(feat.node_id, other_id, type="CONNECTS")
    return result


def _write_to_graph(graph: nx.DiGraph, feat: Feature) -> None:
    graph.add_node(
        feat.node_id,
        type="feature",
        name=feat.name,
        path="",
        source=feat.source,
        description=feat.description,
        summary=feat.narrative,
        narrative_provider=feat.narrative_provider or "mock",
        members=list(feat.members),
        entry_points=list(feat.entry_points),
        flows=feat.flows,
        connects=list(feat.connects),
    )
    for m in feat.members:
        if graph.has_node(m):
            graph.add_edge(m, feat.node_id, type="PART_OF")


def get_features(graph: nx.DiGraph) -> list[dict]:
    return sorted(
        (dict(a, id=n) for n, a in graph.nodes(data=True) if a.get("type") == "feature"),
        key=lambda a: a["name"],
    )
