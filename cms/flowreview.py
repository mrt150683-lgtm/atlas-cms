"""Exact-flow review — inspect what a feature's implementation actually does.

Builds a structured, evidence-classified account of a feature's execution
flows. Two layers:

1. **Static skeleton** (always available, any provider): the feature's traced
   flows (CALLS-walked step chains) extended with per-step evidence — the
   static call edge that proves the step, plus coverage evidence when mapped
   tests execute the feature. Every skeleton step is classified ``proven``
   (it IS a statically resolved call) and the review status is
   ``static_only``.

2. **LLM analysis** (real provider): the model reads each step's actual
   source (bounded), the feature narrative, the approved decision and open
   contradiction annotations, then explains per step: input/output,
   transformations, side effects, async boundaries, error paths — each
   claim classified ``proven | observed | inferred | intended``. The server
   clamps the overall status: ``verified`` is only allowed when every step
   carries both static and coverage evidence; otherwise the best the model
   can claim is ``partially_verified``. No evidence, no certainty.

The result is stored on the feature node as ``flow_review`` with a content
hash over everything it examined. On read, a hash mismatch marks the review
``stale`` — it is served (history is useful) but never presented as current,
and regeneration is on demand only.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

from . import config
from .providers import SummaryProvider

PROMPT_VERSION = 2
FLOW_STATUSES = (
    "verified",                      # every step statically traced AND itself exercised by tests
    "partially_verified",            # solid static skeleton, partial coverage/analysis
    "differs_from_intent",           # analysis contradicts the approved decision
    "insufficient_runtime_evidence", # static story is fine but nothing executes it
    "static_only",                   # skeleton only (no real provider / no analysis)
    "verification_failed",           # analysis ran and found the flow broken
)
# proven = AST-exact fact; static = heuristically resolved static relationship
# (name-matched call edges) — deliberately NOT the same word, per the honest
# vocabulary: a heuristic edge must never read like compiler-accurate proof.
CLASSIFICATIONS = ("proven", "static", "observed", "inferred", "intended")
MAX_FLOWS = 3
MAX_STEPS_PER_FLOW = 12
MAX_SOURCE_LINES_PER_STEP = 40
ANALYSIS_MAX_TOKENS = 3800

_PROMPT = """You are reviewing the EXACT execution flow of the feature "{feature}" in the "{project}" codebase. Below is the statically traced call skeleton with the real source of each step, plus the feature's context.

Your job, per step: explain what actually happens — input, output, transformation, side effects, async boundaries, error paths. Classify every step:
- "proven": an AST-exact fact fully visible in the given source
- "static": supported by a heuristically resolved static call edge (name-matched)
- "observed": the step's OWN lines are executed by the mapped tests listed for that step
- "inferred": likely but not certain from the given evidence — say why in uncertainty
- "intended": stated by intent/docs but NOT visible in the source given

Also judge the overall flow status, choosing EXACTLY one of:
- "partially_verified": the flow works as described per the evidence given (static + some coverage) — this is the STRONGEST claim you may make
- "differs_from_intent": the source contradicts the approved intent below
- "insufficient_runtime_evidence": the static story is coherent but no test exercises it
- "verification_failed": the source shows the flow is broken

Never claim more certainty than the evidence supports. If a step's source was truncated or missing, mark it inferred with the reason.

CONTEXT:
{context}

FLOWS (step ids are stable — reference them exactly):
{flows}

Return ONLY JSON, no commentary:
{{"status": "...", "narrative": "3-6 plain sentences describing the end-to-end flow and its risks",
 "steps": [{{"id": "step id", "explanation": "...", "input": "...", "output": "...",
            "side_effects": "... or null", "async_boundary": false,
            "error_path": "... or null", "uncertainty": "... or null",
            "classification": "proven|observed|inferred|intended"}}]}}
"""


class FlowReviewError(RuntimeError):
    """Provider failure or unusable analysis output."""


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _sha(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8", "replace")).hexdigest()[:16]


def _feature_node(graph, feature_name: str) -> tuple[str, dict]:
    for node_id, a in graph.nodes(data=True):
        if a.get("type") == "feature" and a.get("name", "").lower() == feature_name.lower():
            return node_id, a
    raise FlowReviewError(f"unknown feature {feature_name!r}")


def content_hash(graph, root: Path, feature_name: str) -> str:
    """Identity of everything a flow review examines: the flow step chain,
    member file mtimes, coverage evidence, the approved decision version,
    and the prompt schema."""
    _, feat = _feature_node(graph, feature_name)
    steps = [(s["id"], s.get("line")) for flow in (feat.get("flows") or [])
             for s in flow]
    mtimes = []
    for flow in feat.get("flows") or []:
        for s in flow:
            fnode = graph.nodes.get(f"file:{s.get('path', '')}")
            if fnode:
                mtimes.append((s.get("path"), fnode.get("mtime")))
    decision = None
    try:
        from .decisions import DecisionStore

        dec = DecisionStore(Path(root) / config.MEMORY_DIR_NAME, root=root).approved_for(
            feat.get("name", ""))
        if dec:
            decision = (dec["id"], dec.get("approved_at"))
    except Exception:
        pass
    # step-level coverage is part of the evidence identity: a verify run that
    # changes which tests execute a step must stale the review
    step_cov = sorted(
        (s["id"], tuple((graph.nodes.get(s["id"]) or {}).get("exercised_by") or []))
        for flow in (feat.get("flows") or []) for s in flow)
    return _sha(json.dumps([PROMPT_VERSION, steps, sorted(set(mtimes)),
                            sorted(feat.get("exercised_by") or []),
                            step_cov, decision],
                           sort_keys=True, default=str))


# ── static skeleton ────────────────────────────────────────────────────────

def _step_evidence(graph, feat: dict, step: dict, prev_id: str | None) -> list[dict]:
    """Evidence for ONE step. Coverage is STEP-granular: only tests that
    executed this step's own lines count (written onto func/class nodes by
    `verify.map_tests_to_features`) — one test touching one member never
    vouches for the rest of the flow."""
    evidence = []
    if prev_id is not None and graph.has_edge(prev_id, step["id"]):
        prov = graph.edges[prev_id, step["id"]].get("provenance", "heuristic")
        evidence.append({"kind": "static",
                         "detail": f"CALLS edge from {prev_id.split('::')[-1]} ({prov})",
                         "provenance": prov})
    elif prev_id is None:
        evidence.append({"kind": "static",
                         "detail": "traced entry point (no member callers)",
                         "provenance": "heuristic"})
    step_tests = (graph.nodes.get(step["id"]) or {}).get("exercised_by") or []
    if step_tests:
        evidence.append({"kind": "coverage",
                         "detail": f"this step's lines executed by {len(step_tests)} mapped test(s)",
                         "tests": step_tests[:6]})
    feat_tests = feat.get("exercised_by") or []
    if feat_tests and not step_tests and step.get("in_feature"):
        # honest context, NOT step evidence: the feature is exercised
        # somewhere, just not through these lines
        evidence.append({"kind": "context",
                         "detail": f"feature has {len(feat_tests)} mapped test(s), "
                                   "but none execute this step's lines"})
    return evidence


def _base_classification(evidence: list[dict]) -> str:
    if any(e["kind"] == "coverage" for e in evidence):
        return "observed"
    static = next((e for e in evidence if e["kind"] == "static"), None)
    if static and static.get("provenance") == "ast":
        return "proven"
    return "static"


def build_skeleton(graph, feat: dict) -> list[list[dict]]:
    """The always-available structured flow: existing traced steps extended
    with sequence, operation, evidence and a conservative classification."""
    skeleton = []
    for flow in (feat.get("flows") or [])[:MAX_FLOWS]:
        steps = []
        prev_id = None
        for i, s in enumerate(flow[:MAX_STEPS_PER_FLOW]):
            evidence = _step_evidence(graph, feat, s, prev_id)
            steps.append({
                **s,
                "seq": i,
                "operation": "call",
                "evidence": evidence,
                "classification": _base_classification(evidence),
                "explanation": None, "input": None, "output": None,
                "side_effects": None, "async_boundary": False,
                "error_path": None, "uncertainty": None,
            })
            prev_id = s["id"]
        if steps:
            skeleton.append(steps)
    return skeleton


def _scope(feat: dict, skeleton: list[list[dict]]) -> dict:
    """How much of the traced surface this review actually examined —
    displayed with the status so 'verified' can never read wider than it is."""
    traced = len(feat.get("flows") or [])
    return {"flows_reviewed": len(skeleton), "flows_traced": traced,
            "steps_reviewed": sum(len(f) for f in skeleton),
            "steps_truncated": any(
                len(flow) > MAX_STEPS_PER_FLOW for flow in (feat.get("flows") or []))}


def _step_source(root: Path, graph, step: dict) -> str:
    a = graph.nodes.get(step["id"])
    if not a or not a.get("path") or not a.get("start_line"):
        return "(source not mapped)"
    try:
        lines = (Path(root) / a["path"]).read_text(
            encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return "(source unavailable)"
    start = a["start_line"] - 1
    end = min(a.get("end_line") or start + 1, start + MAX_SOURCE_LINES_PER_STEP)
    body = "\n".join(lines[start:end])
    truncated = (a.get("end_line") or 0) - a["start_line"] > MAX_SOURCE_LINES_PER_STEP
    return body + ("\n# ... truncated ..." if truncated else "")


def _analysis_context(root: Path, graph, feat: dict) -> str:
    rows = []
    if feat.get("description"):
        rows.append(f"DECLARED INTENT: {feat['description'][:400]}")
    review = feat.get("review") or {}
    if review.get("verdict"):
        rows.append(f"AI REVIEW VERDICT: {review['verdict']} — {review.get('headline', '')[:200]}")
    tests = feat.get("exercised_by") or []
    rows.append(f"MAPPED TESTS: {', '.join(tests[:8]) or '(none — no runtime evidence)'}")
    try:
        from .decisions import DecisionStore

        dec = DecisionStore(Path(root) / config.MEMORY_DIR_NAME, root=root).approved_for(
            feat.get("name", ""))
        if dec:
            rows.append(f"APPROVED INTENT (locked decision {dec['id']}): "
                        f"{dec['intent']['behaviour'][:400]}")
            if dec["intent"].get("prohibited"):
                rows.append(f"PROHIBITED: {'; '.join(dec['intent']['prohibited'][:5])}")
    except Exception:
        pass
    try:
        from .annotations import AnnotationStore

        anns = AnnotationStore(Path(root) / config.MEMORY_DIR_NAME, root=root)
        open_contra = [a for a in anns.active_for_context(feature=feat.get("name"), limit=4)
                       if a["type"] in ("contradiction", "bug_suspicion")]
        for a in open_contra:
            rows.append(f"OPEN {a['type'].upper()}: {a['body'][:250]}")
    except Exception:
        pass
    return "\n".join(rows)


def _flows_block(root: Path, graph, skeleton: list[list[dict]]) -> str:
    blocks = []
    for fi, flow in enumerate(skeleton):
        rows = [f"FLOW {fi + 1}:"]
        for s in flow:
            rows.append(f"  step id: {s['id']}  ({s['path']}:{s.get('line')})"
                        f"{'' if s['in_feature'] else '  [outside feature]'}")
            src = _step_source(root, graph, s)
            rows.append("  source:\n" + "\n".join("    " + ln for ln in src.splitlines()))
        blocks.append("\n".join(rows))
    return "\n\n".join(blocks)


def _parse_analysis(reply: str) -> dict:
    start, end = reply.find("{"), reply.rfind("}")
    if start < 0 or end <= start:
        raise FlowReviewError("no JSON object in the analysis reply")
    try:
        data = json.loads(reply[start:end + 1])
    except json.JSONDecodeError as exc:
        raise FlowReviewError(f"unparseable analysis JSON: {exc}") from exc
    if not isinstance(data.get("steps"), list):
        raise FlowReviewError("analysis reply carries no steps")
    return data


def _merge_analysis(skeleton: list[list[dict]], analysis: dict,
                    has_coverage: bool) -> tuple[list[list[dict]], str, str]:
    by_id = {}
    for a in analysis["steps"]:
        if isinstance(a, dict) and a.get("id"):
            by_id[str(a["id"])] = a
    for flow in skeleton:
        for s in flow:
            a = by_id.get(s["id"])
            if not a:
                s["uncertainty"] = "the analysis did not address this step"
                if s["classification"] in ("proven", "observed"):
                    pass  # static evidence stands on its own
                continue
            for key in ("explanation", "input", "output", "side_effects",
                        "error_path", "uncertainty"):
                if a.get(key):
                    s[key] = str(a[key])[:600]
            s["async_boundary"] = bool(a.get("async_boundary"))
            cls = str(a.get("classification") or "")
            if cls in CLASSIFICATIONS:
                # the model may only downgrade evidence, never upgrade:
                # no coverage on THIS step -> cannot be "observed";
                # no AST-exact edge -> cannot be "proven" (best: "static")
                if cls == "observed" and not any(
                        e["kind"] == "coverage" for e in s["evidence"]):
                    cls = "inferred"
                if cls == "proven" and not any(
                        e.get("provenance") == "ast" for e in s["evidence"]):
                    cls = "static"
                s["classification"] = cls

    status = str(analysis.get("status") or "")
    if status not in FLOW_STATUSES or status == "verified":
        # "verified" is never model-claimable; it is computed below
        status = "partially_verified"
    if status == "partially_verified" and not has_coverage:
        status = "insufficient_runtime_evidence"
    narrative = str(analysis.get("narrative") or "").strip()[:2000]
    return skeleton, status, narrative


# @memory:feature:ExactFlowReview
# @memory:connects:FeatureTracing, ApprovedDecisions, StructuredAnnotations, FeatureVerification
# @memory:summary:Structured, evidence-classified execution-flow review per feature — static CALLS skeleton always, bounded-source LLM step analysis with a real provider, verified status computed from evidence and never claimable by the model, content-hash staleness on read.
def build_flow_review(root: Path, graph, provider: SummaryProvider,
                      feature_name: str, force: bool = False) -> dict:
    """Build (or reuse) the flow review for one feature. Returns the stored
    payload; writes it onto the feature node (caller persists the graph)."""
    root = Path(root).resolve()
    node_id, feat = _feature_node(graph, feature_name)
    chash = content_hash(graph, root, feat["name"])

    existing = feat.get("flow_review")
    if existing and not force and existing.get("content_hash") == chash:
        return {**existing, "stale": False, "reused": True}

    skeleton = build_skeleton(graph, feat)
    tests = feat.get("exercised_by") or []
    # runtime evidence means STEP-level coverage somewhere in the reviewed
    # flows, not merely "the feature has tests" (dual-review Priority-0)
    has_coverage = any(
        any(e["kind"] == "coverage" for e in s["evidence"])
        for flow in skeleton for s in flow)
    base = {
        "feature": feat["name"],
        "flows": skeleton,
        "scope": _scope(feat, skeleton),
        "content_hash": chash,
        "prompt_version": PROMPT_VERSION,
        "generated_at": _now_iso(),
        "provider": provider.name,
        "model": getattr(provider, "model", None),
        "real": provider.name != "mock",
    }

    if not skeleton:
        base.update(status="insufficient_runtime_evidence", narrative=(
            "No call flows are traced for this feature, so there is nothing "
            "to review. Re-run `cms trace` after adding members."))
        graph.nodes[node_id]["flow_review"] = base
        return {**base, "stale": False, "reused": False}

    if provider.name == "mock":
        covered = sum(1 for flow in skeleton for s in flow
                      if any(e["kind"] == "coverage" for e in s["evidence"]))
        total_steps = sum(len(f) for f in skeleton)
        base.update(status="static_only", narrative=(
            "Static call skeleton only — the steps below are statically traced "
            "call edges (heuristic name resolution)"
            + (f"; {covered} of {total_steps} step(s) have their own lines "
               "executed by mapped tests" if tests else
               "; no mapped test executes this feature")
            + ". Run with an API key for a step-by-step analysis of inputs, "
              "outputs and failure paths."))
        graph.nodes[node_id]["flow_review"] = base
        return {**base, "stale": False, "reused": False}

    prompt = _PROMPT.format(
        feature=feat["name"], project=root.name,
        context=_analysis_context(root, graph, feat),
        flows=_flows_block(root, graph, skeleton),
    )
    try:
        reply = provider.summarize(prompt, {"max_tokens": ANALYSIS_MAX_TOKENS})
        analysis = _parse_analysis(reply)
    except FlowReviewError:
        raise
    except Exception as exc:
        raise FlowReviewError(f"provider error: {exc}") from exc

    flows, status, narrative = _merge_analysis(skeleton, analysis, has_coverage)

    # the only path to "verified": every step statically traced AND every
    # in-feature step's OWN lines exercised by a mapped test — computed,
    # never asserted, and scope-limited to the flows actually reviewed
    if status == "partially_verified" and has_coverage and all(
            any(e["kind"] == "static" for e in s["evidence"]) and
            (not s["in_feature"] or any(e["kind"] == "coverage" for e in s["evidence"]))
            for flow in flows for s in flow):
        status = "verified"

    base.update(status=status, narrative=narrative, flows=flows)
    graph.nodes[node_id]["flow_review"] = base
    return {**base, "stale": False, "reused": False}


def read_flow_review(root: Path, graph, feature_name: str) -> dict | None:
    """Serve the stored review with an honest staleness flag; never
    regenerates (that is an explicit, chargeable action)."""
    _, feat = _feature_node(graph, feature_name)
    stored = feat.get("flow_review")
    if not stored:
        return None
    current = content_hash(graph, Path(root).resolve(), feat["name"])
    return {**stored, "stale": stored.get("content_hash") != current}
