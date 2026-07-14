"""Describe-a-feature hunt — map a natural-language description to code.

The user (or an agent) describes behaviour the automatic passes may have
missed; Atlas hunts the canonical graph for it. The hunt has four stages:

1. **Already stated?** Deterministic overlap detection against every existing
   feature (name/alias tokens + member overlap with the ranked hits). If the
   behaviour is already mapped, the hunt says so and points at the feature
   instead of inventing a duplicate.
2. **Thorough search.** Intent-ranked hits expanded one hop through the graph
   (callers, callees, imports), each carrying its summary and its place in
   the semantic pyramid, so the model reasons over connections, not keywords.
3. **Grounded mapping.** One LLM pass proposes the candidate members (each
   with WHY it belongs and its role), the feature's connections to existing
   features, and an ordered plain-language explanation of the mechanism.
   Every claim is validated: unknown member ids and unknown feature names are
   dropped, and connections computed directly from graph edges are merged in
   with ``provenance: graph`` so the connection list never rests on model
   assertion alone.
4. **Human confirmation.** Nothing is auto-accepted; the UI renders the
   explanation through the comprehension lens (so it reads at whatever level
   the user selected) and the human confirms, renames, or rejects. Confirmed
   features are recorded in the durable discovery evidence so incremental
   updates re-inject them forever.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from . import config
from . import semantic_state as ss
from .memory import CodebaseMemory
from .providers import SummaryProvider

MAX_HITS = 14
MAX_NEIGHBOURS = 4
MAX_CATALOG = 40
PROPOSAL_MAX_TOKENS = 2800

_HUNT_PROMPT = """A user believes the "{project}" codebase contains a behaviour that the automatic feature mapping may have missed. Hunt for it in the evidence below and report honestly.

DESCRIPTION: {description}

EXISTING FEATURES (already mapped — do NOT re-invent these):
{catalog}

RANKED CODE EVIDENCE (id | path:lines | summary):
{hits}

GRAPH NEIGHBOURHOOD (one hop around the evidence — real edges, not guesses):
{edges}

Your job:
1. Verdict — exactly one of:
   - "already_covered": an existing feature above IS this behaviour (name it in "existing").
   - "partial_overlap": existing feature(s) cover part of it; the rest is unmapped (name them, and map only the unmapped part).
   - "new": the behaviour exists in the code but no feature covers it.
   - "not_found": the evidence does not support this behaviour existing at all.
2. Candidate mapping (for "new"/"partial_overlap"): choose ONLY ids from the evidence that implement the DESCRIBED behaviour; drop keyword coincidences. Give each member a role ("entry" = where it starts, "core" = the mechanism, "support" = helpers) and a concrete WHY.
3. Connections: which EXISTING features this behaviour touches, each with a concrete via ("X calls Y", "shares file Z"). Only name features from the list above.
4. Explanation: 3-7 ordered steps describing HOW the behaviour works end to end, in plain language, each step grounded in the evidence given (name the code in `backticks`). Never invent files, functions or behaviour not in the evidence.
5. Uncertainty: what you could not confirm from this evidence.

Return ONLY JSON:
{{"verdict": "...", "existing": [{{"feature": "ExactExistingName", "why": "..."}}],
 "name": "PascalCaseFeatureName", "description": "one-sentence plain description",
 "members": [{{"id": "id from evidence", "role": "entry|core|support", "why": "..."}}],
 "connections": [{{"feature": "ExactExistingName", "via": "concrete mechanism"}}],
 "explanation": ["step 1 ...", "step 2 ..."],
 "uncertainty": "..."}}
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


def _tokens(text: str) -> set[str]:
    """Lowercase word + split-camel tokens of length >= 4."""
    words = re.findall(r"[A-Z][a-z]{3,}|[a-z]{4,}", str(text or ""))
    return {w.lower() for w in words}


def _member_files(graph, feat_attrs: dict) -> set[str]:
    out = set()
    for m in feat_attrs.get("members") or []:
        if graph.has_node(m):
            path = graph.nodes[m].get("path") or ""
            if path:
                out.add(path)
    return out


def existing_overlap(graph, description: str, hits: list[dict]) -> list[dict]:
    """Deterministic 'already stated?' check: does an existing feature's
    name/description overlap the request, or do its members overlap the
    evidence the hunt just found? Returns matches strongest-first."""
    desc_tokens = _tokens(description)
    hit_paths = {h["path"] for h in hits if h.get("path")}
    hit_ids = {h["id"] for h in hits}
    matches = []
    for node_id, a in graph.nodes(data=True):
        if a.get("type") != "feature":
            continue
        name_tokens = _tokens(a.get("name", "")) | _tokens(" ".join(a.get("aliases") or []))
        name_match = len(name_tokens & desc_tokens)
        text_match = len(_tokens(a.get("description", "")) & desc_tokens)
        members = set(a.get("members") or [])
        member_hits = len(members & hit_ids) + len(_member_files(graph, a) & hit_paths)
        score = name_match * 3 + text_match + member_hits * 2
        if (name_match and member_hits) or member_hits >= 3 or name_match >= 2:
            matches.append({
                "feature": a.get("name", ""), "source": a.get("source", ""),
                "description": (a.get("description") or "")[:200],
                "name_token_matches": name_match, "member_overlap": member_hits,
                "score": score,
            })
    return sorted(matches, key=lambda m: -m["score"])[:4]


def _feature_catalog(graph) -> list[dict]:
    rows = []
    for _, a in graph.nodes(data=True):
        if a.get("type") == "feature":
            rows.append({"name": a.get("name", ""),
                         "description": (a.get("description") or "")[:120]})
    return sorted(rows, key=lambda r: r["name"])[:MAX_CATALOG]


def _neighbourhood_edges(graph, hits: list[dict]) -> list[str]:
    """Real one-hop edges around the evidence — the model reasons over
    connections, never invents them."""
    hit_ids = {h["id"] for h in hits}
    rows: list[str] = []
    seen: set[str] = set()
    for hid in hit_ids:
        if not graph.has_node(hid):
            continue
        count = 0
        for src, dst, d in list(graph.out_edges(hid, data=True)) + \
                           list(graph.in_edges(hid, data=True)):
            kind = d.get("type")
            if kind not in ("CALLS", "IMPORTS", "PART_OF"):
                continue
            if kind == "PART_OF" and not dst.startswith("feature:"):
                continue  # pyramid levels above features aren't hunt evidence
            line = f"{src} -{kind.lower()}-> {dst}"
            if line in seen:
                continue
            seen.add(line)
            rows.append(line)
            count += 1
            if count >= MAX_NEIGHBOURS:
                break
    return rows[:60]


def graph_connections(graph, member_ids: list[str]) -> list[dict]:
    """Evidence-grounded connections: existing features whose members share
    edges (or files) with the candidate members. Never model-asserted."""
    owner: dict[str, str] = {}  # member id / file path -> feature name
    for _, a in graph.nodes(data=True):
        if a.get("type") != "feature":
            continue
        for m in a.get("members") or []:
            owner[m] = a["name"]
            if graph.has_node(m) and graph.nodes[m].get("path"):
                owner.setdefault("file:" + graph.nodes[m]["path"], a["name"])
    out: dict[str, str] = {}
    for mid in member_ids:
        if not graph.has_node(mid):
            continue
        for src, dst, d in list(graph.out_edges(mid, data=True)) + \
                           list(graph.in_edges(mid, data=True)):
            if d.get("type") not in ("CALLS", "IMPORTS"):
                continue
            other = dst if src == mid else src
            feat = owner.get(other)
            if not feat or feat in out:
                continue
            verb = "calls" if d.get("type") == "CALLS" else "imports"
            a, b = (mid, other) if src == mid else (other, mid)
            out[feat] = f"{a.split(':', 1)[-1]} {verb} {b.split(':', 1)[-1]}"
    return [{"feature": f, "via": via, "provenance": "graph"}
            for f, via in sorted(out.items())][:8]


# @memory:feature:FeatureDiscoveryByDescription
# @memory:connects:QueryEngine, FeatureTracing, ComprehensionLens
# @memory:summary:The feature hunt — NL description checked against existing features (already-stated flag), graph searched with one-hop neighbourhood expansion, candidate mapping + connections validated against real edges, mechanism explained step by step; proposals only, human confirms.
def propose_feature(root: Path, memory: CodebaseMemory, description: str,
                    provider: SummaryProvider) -> dict:
    """Hunt the graph for a described behaviour. Returns verdict
    (already_covered | partial_overlap | new | not_found), the overlapping
    existing features, an evidence-validated candidate mapping with member
    roles, grounded connections, and an ordered mechanism explanation.
    Mock/no-key: ranked hits + deterministic overlap flags only,
    ``real: false`` — a human must judge manually."""
    description = str(description or "").strip()
    if len(description) < 12:
        raise FeatureDiscoveryError("describe the behaviour in at least a short sentence")
    graph = memory.graph
    hits = _hit_rows(memory, description)
    overlap = existing_overlap(graph, description, hits)
    if not hits:
        return {"real": provider.name != "mock", "verdict": "not_found",
                "candidate": None, "hits": [], "existing": overlap,
                "connections": [], "explanation": [],
                "note": "nothing in the mapped code matches this description"}
    if provider.name == "mock":
        flagged = (f"looks already stated as '{overlap[0]['feature']}' "
                   f"({overlap[0]['member_overlap']} shared member(s)) — verify there first. "
                   if overlap else "")
        return {"real": False, "verdict": None, "candidate": None, "hits": hits,
                "existing": overlap, "connections": [], "explanation": [],
                "note": (flagged + "no real provider — these are keyword-ranked hits "
                         "only, NOT a verified mapping; pick members manually or add an API key")}

    catalog = _feature_catalog(graph)
    prompt = _HUNT_PROMPT.format(
        project=Path(root).resolve().name, description=description[:600],
        catalog="\n".join(f"- {c['name']}: {c['description'] or '(no description)'}"
                          for c in catalog) or "(none mapped yet)",
        hits="\n".join(f"- {h['id']} | {h['path']}:{h['lines']} | "
                       f"{h['summary'] or '(no summary)'}" for h in hits),
        edges="\n".join(_neighbourhood_edges(graph, hits)) or "(no edges found)",
    )
    try:
        reply = provider.summarize(prompt, {"max_tokens": PROPOSAL_MAX_TOKENS})
    except Exception as exc:
        raise FeatureDiscoveryError(f"provider error: {exc}") from exc
    start, end = reply.find("{"), reply.rfind("}")
    if start < 0 or end <= start:
        raise FeatureDiscoveryError("no JSON in the hunt reply")
    try:
        data = json.loads(reply[start:end + 1])
    except json.JSONDecodeError as exc:
        raise FeatureDiscoveryError(f"unparseable hunt JSON: {exc}") from exc

    # validate every claim against the graph — nothing survives on assertion
    known_ids = {h["id"] for h in hits}
    known_features = {c["name"] for c in catalog}
    verdict = str(data.get("verdict") or "")
    if verdict not in ("already_covered", "partial_overlap", "new", "not_found"):
        verdict = "new" if data.get("members") else "not_found"
    existing = [{"feature": e["feature"], "why": str(e.get("why") or "")[:300],
                 "provenance": "llm"}
                for e in (data.get("existing") or [])
                if isinstance(e, dict) and e.get("feature") in known_features]
    # deterministic overlap findings always surface, model or not
    named = {e["feature"] for e in existing}
    existing += [{**o, "provenance": "graph"} for o in overlap
                 if o["feature"] not in named]

    members = [{"id": m["id"],
                "role": m.get("role") if m.get("role") in ("entry", "core", "support") else "core",
                "why": str(m.get("why") or "")[:300]}
               for m in (data.get("members") or [])
               if isinstance(m, dict) and m.get("id") in known_ids]
    candidate = None
    if members and verdict in ("new", "partial_overlap"):
        candidate = {
            "name": "".join(w for w in str(data.get("name") or "Described Feature")
                            if w.isalnum()) or "DescribedFeature",
            "description": str(data.get("description") or description)[:400],
            "members": members,
        }

    connections = [{"feature": c["feature"], "via": str(c.get("via") or "")[:200],
                    "provenance": "llm"}
                   for c in (data.get("connections") or [])
                   if isinstance(c, dict) and c.get("feature") in known_features]
    if candidate:  # merge edge-grounded connections; graph evidence wins
        claimed = {c["feature"] for c in connections}
        for gc in graph_connections(graph, [m["id"] for m in members]):
            if gc["feature"] in claimed:
                for c in connections:
                    if c["feature"] == gc["feature"]:
                        c.update(via=gc["via"], provenance="graph")
            else:
                connections.append(gc)

    explanation = [str(s)[:400] for s in (data.get("explanation") or [])
                   if str(s).strip()][:7]
    return {"real": True, "verdict": verdict, "candidate": candidate,
            "hits": hits, "existing": existing, "connections": connections,
            "explanation": explanation,
            "note": str(data.get("uncertainty") or "")[:400]}


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
