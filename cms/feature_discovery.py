"""Describe-a-feature discovery — map a natural-language description to code.

The user (or an agent) describes behaviour in plain words; Atlas searches the
canonical graph (intent-ranked hits + their import/call neighbourhoods) and —
with a real provider — proposes ONE candidate feature mapping with per-member
evidence. Keyword overlap alone is never auto-accepted: the candidate is a
*proposal* until a human confirms it (optionally renamed), at which point it
becomes a durable discovered feature, recorded in the semantic-state
discovery evidence exactly like LLM-discovered features so incremental
updates re-inject it forever.
"""

from __future__ import annotations

import json
from pathlib import Path

from . import config
from . import semantic_state as ss
from .memory import CodebaseMemory
from .providers import SummaryProvider

MAX_HITS = 12
PROPOSAL_MAX_TOKENS = 1800

_PROMPT = """A user describes a behaviour of the "{project}" codebase. Map the description to ONE candidate feature grounded in the ranked code evidence below.

DESCRIPTION: {description}

RANKED CODE EVIDENCE (name | path:lines | summary | connected-to):
{hits}

Rules:
- Choose ONLY files/functions from the evidence that plausibly implement the DESCRIBED behaviour; drop keyword-only coincidences.
- For each chosen member give concrete evidence: WHY it belongs (what in its summary/connections matches the described behaviour).
- If the evidence does not support the description, say so with an empty members list and explain in "note".

Return ONLY JSON:
{{"name": "PascalCaseFeatureName", "description": "one-sentence plain description",
 "members": [{{"id": "the node id given", "why": "concrete reason"}}],
 "note": "caveats / what could not be confirmed"}}
"""


class FeatureDiscoveryError(RuntimeError):
    pass


def _hit_rows(memory: CodebaseMemory, description: str) -> list[dict]:
    graph = memory.graph
    rows = []
    for h in memory.query_intent(description, top_k=MAX_HITS):
        neighbours = []
        for other in (h.calls or [])[:3]:
            neighbours.append(other)
        for _, t, d in graph.out_edges(h.node_id, data=True):
            if d.get("type") == "IMPORTS" and len(neighbours) < 6:
                neighbours.append(t)
        rows.append({"id": h.node_id, "kind": h.kind, "name": h.name,
                     "path": h.path, "lines": h.lines, "score": h.score,
                     "summary": (h.summary or "")[:240],
                     "connected": neighbours})
    return rows


# @memory:feature:FeatureDiscoveryByDescription
# @memory:connects:QueryEngine, FeatureTracing
# @memory:summary:NL description -> evidence-backed candidate feature mapping; proposals only (never auto-accepted), confirmation writes a durable discovered feature into graph + semantic state.
def propose_feature(root: Path, memory: CodebaseMemory, description: str,
                    provider: SummaryProvider) -> dict:
    """Candidate mapping for a described behaviour. Mock/no-key: ranked hits
    only, ``real: false`` — a human must pick members manually."""
    description = str(description or "").strip()
    if len(description) < 12:
        raise FeatureDiscoveryError("describe the behaviour in at least a short sentence")
    hits = _hit_rows(memory, description)
    if not hits:
        return {"real": provider.name != "mock", "candidate": None, "hits": [],
                "note": "nothing in the mapped code matches this description"}
    if provider.name == "mock":
        return {"real": False, "candidate": None, "hits": hits,
                "note": ("no real provider — these are keyword-ranked hits only, "
                         "NOT a verified mapping; pick members manually or add an API key")}

    rows = "\n".join(
        f"- {h['id']} | {h['path']}:{h['lines']} | {h['summary'] or '(no summary)'}"
        f" | connects: {', '.join(h['connected']) or '-'}"
        for h in hits)
    prompt = _PROMPT.format(project=Path(root).resolve().name,
                            description=description[:600], hits=rows)
    try:
        reply = provider.summarize(prompt, {"max_tokens": PROPOSAL_MAX_TOKENS})
    except Exception as exc:
        raise FeatureDiscoveryError(f"provider error: {exc}") from exc
    start, end = reply.find("{"), reply.rfind("}")
    if start < 0 or end <= start:
        raise FeatureDiscoveryError("no JSON in the mapping reply")
    try:
        data = json.loads(reply[start:end + 1])
    except json.JSONDecodeError as exc:
        raise FeatureDiscoveryError(f"unparseable mapping JSON: {exc}") from exc

    known = {h["id"] for h in hits}
    members = [{"id": m["id"], "why": str(m.get("why") or "")[:300]}
               for m in (data.get("members") or [])
               if isinstance(m, dict) and m.get("id") in known]
    candidate = None
    if members:
        candidate = {
            "name": "".join(w for w in str(data.get("name") or "Described Feature")
                            if w.isalnum()) or "DescribedFeature",
            "description": str(data.get("description") or description)[:400],
            "members": members,
        }
    return {"real": True, "candidate": candidate, "hits": hits,
            "note": str(data.get("note") or "")[:400]}


def confirm_feature(root: Path, name: str, description: str,
                    member_ids: list[str]) -> dict:
    """Human confirmation: write the feature into the graph and the durable
    discovery evidence so incremental updates re-inject it forever."""
    from .features import Feature, _write_to_graph, trace_flows

    root = Path(root).resolve()
    memory_dir = root / config.MEMORY_DIR_NAME
    graph_path = memory_dir / "graph.json"
    memory = CodebaseMemory.load(graph_path)
    graph = memory.graph

    name = "".join(w for w in str(name or "") if w.isalnum())
    if not name:
        raise FeatureDiscoveryError("the feature needs an alphanumeric name")
    if graph.has_node(f"feature:{name}"):
        raise FeatureDiscoveryError(f"a feature named {name!r} already exists")
    members = [m for m in (member_ids or []) if graph.has_node(m)]
    if not members:
        raise FeatureDiscoveryError("confirm at least one member that exists in the graph")

    feat = Feature(name=name, description=str(description or "")[:400],
                   source="discovered", members=members)
    feat.entry_points, feat.flows = trace_flows(graph, members)
    _write_to_graph(graph, feat)
    memory.save(graph_path)

    # durable evidence: append to the discovery record's feature list so
    # _features_from_state re-injects it on every future update
    rec = ss.stage(ss.load_state(memory_dir), "features")
    carried = list(rec.get("discovered_features", []))
    carried.append({"name": name, "description": feat.description,
                    "members": members, "aliases": []})
    ss.record_stage(memory_dir, "features", **{
        **rec, "discovered_features": carried,
        "feature_set_hash": ss.feature_set_hash(graph),
        **ss.feature_counts(graph),
    })
    return {"confirmed": True, "feature": name, "members": members,
            "note": "narrative and connections build on the next `cms update`"}
