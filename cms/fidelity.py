"""Intent fidelity — does a feature match what was agreed, per the evidence?

Computed on demand from artifacts that already exist (review verdicts,
coverage mapping, verify outcomes, flow reviews, decisions, annotations) —
never stored, so it can never go stale, and never a bare percentage: every
dimension carries the reason for its value, and thin evidence yields an
explicit ``insufficient_evidence`` instead of an invented score.
"""

from __future__ import annotations

from pathlib import Path

from . import config

DIMENSIONS = ("implemented", "tests_present", "tests_passing", "approved_intent",
              "intent_match", "open_contradictions", "stale_evidence")
OVERALL = ("on_track", "attention", "insufficient_evidence")


def _feature(graph, name: str) -> dict | None:
    for _, a in graph.nodes(data=True):
        if a.get("type") == "feature" and a.get("name", "").lower() == name.lower():
            return a
    return None


# @memory:feature:IntentFidelity
# @memory:connects:FeatureExpectationReview, ApprovedDecisions, ExactFlowReview, FeatureVerification
# @memory:summary:Decomposable, evidence-backed fidelity assessment per feature — every dimension explains itself, thin evidence says so instead of scoring, nothing is stored so nothing can silently go stale.
def intent_fidelity(root: Path, graph, feature_name: str) -> dict:
    feat = _feature(graph, feature_name)
    if feat is None:
        raise ValueError(f"unknown feature {feature_name!r}")
    name = feat.get("name", feature_name)
    dims: dict[str, object] = {}
    why: dict[str, str] = {}

    review = feat.get("review") or {}
    verdict = review.get("verdict")
    if verdict == "aligned":
        dims["implemented"] = "yes"
        why["implemented"] = "AI review judged built behaviour aligned with the expectation"
    elif verdict == "partial":
        dims["implemented"] = "partial"
        why["implemented"] = "AI review found the core intent met with gaps: " + \
            "; ".join((review.get("gaps") or [])[:3])
    elif verdict == "drift":
        dims["implemented"] = "no"
        why["implemented"] = "AI review found built behaviour contradicting the expectation"
    else:
        dims["implemented"] = "unknown"
        why["implemented"] = "no valid AI review for this feature (run `cms review`)"

    tests = feat.get("exercised_by") or []
    dims["tests_present"] = bool(tests)
    why["tests_present"] = (f"{len(tests)} mapped test(s) execute this feature's lines"
                            if tests else "no coverage-mapped tests (run `cms verify`)")

    vr = feat.get("verify_result") or {}
    if vr.get("at"):
        dims["tests_passing"] = "passed" if vr.get("passed") else "failed"
        why["tests_passing"] = (f"cms verify ran {vr.get('tests', '?')} mapped test(s) "
                                f"at {vr['at']} — {'all passed' if vr.get('passed') else 'failures'}")
    else:
        dims["tests_passing"] = "not_recorded"
        why["tests_passing"] = "mapped tests have not been executed via `cms verify <Feature>`"

    decision = None
    try:
        from .decisions import DecisionStore

        decision = DecisionStore(Path(root) / config.MEMORY_DIR_NAME,
                                 root=root).approved_for(name)
    except Exception:
        pass
    dims["approved_intent"] = "present" if decision else "absent"
    why["approved_intent"] = (f"approved decision {decision['id']} ({decision['title']})"
                              if decision else
                              "no human-approved intended-behaviour decision exists")

    flow = feat.get("flow_review") or {}
    flow_status = flow.get("status")
    if decision is None:
        dims["intent_match"] = "unknown"
        why["intent_match"] = "nothing to match against — no approved intent"
    elif flow_status == "differs_from_intent":
        dims["intent_match"] = "differs"
        why["intent_match"] = "the exact-flow review found behaviour differing from the approved intent"
    elif flow_status in ("verified", "partially_verified") or verdict == "aligned":
        dims["intent_match"] = "match"
        why["intent_match"] = f"flow review ({flow_status or 'none'}) and review verdict ({verdict or 'none'}) show no contradiction with the approved intent"
    else:
        dims["intent_match"] = "unknown"
        why["intent_match"] = "no verifying evidence connects the implementation to the approved intent yet"

    contradictions = 0
    try:
        from .annotations import AnnotationStore

        store = AnnotationStore(Path(root) / config.MEMORY_DIR_NAME, root=root)
        contradictions = sum(
            1 for a in store.active_for_context(feature=name, limit=8)
            if a["type"] in ("contradiction", "bug_suspicion"))
    except Exception:
        pass
    dims["open_contradictions"] = contradictions
    why["open_contradictions"] = (f"{contradictions} open contradiction/bug-suspicion annotation(s)"
                                  if contradictions else "no open contradictions recorded")

    stale = False
    stale_bits = []
    if flow:
        try:
            from .flowreview import content_hash as flow_hash

            if flow.get("content_hash") != flow_hash(graph, Path(root), name):
                stale = True
                stale_bits.append("flow review predates the current code/decision")
        except Exception:
            pass
    dims["stale_evidence"] = stale
    why["stale_evidence"] = "; ".join(stale_bits) or "all evidence is current for the mapped code"

    # overall: explicit honesty about thin evidence before any judgment
    has_any_evidence = (verdict in ("aligned", "partial", "drift")
                        or bool(tests) or bool(decision) or bool(flow))
    if not has_any_evidence:
        overall = "insufficient_evidence"
        headline = "No review, no mapped tests, no decision, no flow review — nothing to assess."
    elif (dims["implemented"] == "no" or dims["intent_match"] == "differs"
          or dims["tests_passing"] == "failed" or contradictions):
        overall = "attention"
        headline = "Evidence points at a gap between intent and reality — see the failing dimensions."
    elif dims["implemented"] in ("yes", "partial") and dims["tests_present"] and not stale:
        overall = "on_track"
        headline = "Implemented per review, exercised by tests, no open contradictions."
    else:
        overall = "insufficient_evidence"
        headline = "Some evidence exists but not enough to call it — see what is missing below."

    assert overall in OVERALL
    return {"feature": name, "overall": overall, "headline": headline,
            "dimensions": dims, "explanations": why}
