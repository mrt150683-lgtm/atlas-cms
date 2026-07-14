"""Exact-flow review: static skeleton evidence, honest mock degradation,
LLM merge with classification clamping, evidence-computed verified status,
content-hash staleness, and decision-change invalidation."""

import json
from pathlib import Path

import pytest

from cms.decisions import DecisionStore
from cms.features import build_features
from cms.flowreview import (
    FLOW_STATUSES,
    FlowReviewError,
    build_flow_review,
    content_hash,
    read_flow_review,
)
from cms.graph_builder import build_graph
from cms.providers import MockProvider
from cms.scanner import scan

SOURCE = '''\
# @memory:feature:Greeting
def greet(name):
    return helper(name)


def helper(name):
    return name.strip()
'''


@pytest.fixture()
def proj(tmp_path):
    (tmp_path / "app.py").write_text(SOURCE, encoding="utf-8")
    graph = build_graph(scan(tmp_path))
    build_features(graph, MockProvider())
    (tmp_path / ".memory").mkdir()
    return tmp_path, graph


class AnalysisProvider:
    """Real-provider stand-in: classifies every step and claims a status."""

    name = "fake"
    model = "fake-1"

    def __init__(self, status="partially_verified", classification="proven",
                 fail=False) -> None:
        self.status = status
        self.classification = classification
        self.fail = fail
        self.calls = 0
        self.last_prompt = ""

    def summarize(self, prompt: str, context: dict) -> str:
        self.calls += 1
        self.last_prompt = prompt
        if self.fail:
            raise RuntimeError("provider down")
        import re

        step_ids = re.findall(r"step id: (\S+)", prompt)
        return json.dumps({
            "status": self.status,
            "narrative": "greet delegates to helper which strips the name.",
            "steps": [{"id": sid, "explanation": f"handles {sid}",
                       "input": "name", "output": "text",
                       "classification": self.classification}
                      for sid in step_ids],
        })


def test_mock_gives_labelled_static_skeleton(proj) -> None:
    root, graph = proj
    rv = build_flow_review(root, graph, MockProvider(), "Greeting")
    assert rv["status"] == "static_only" and rv["real"] is False
    assert "Static call skeleton only" in rv["narrative"]
    steps = rv["flows"][0]
    assert steps[0]["id"] == "func:app.py::greet" and steps[0]["seq"] == 0
    # heuristic call resolution is "static", never "proven" (that word is
    # reserved for AST-exact facts)
    assert steps[0]["classification"] == "static"
    assert any(e["kind"] == "static" for e in steps[1]["evidence"])
    # scope travels with the status so it can never read wider than it is
    assert rv["scope"]["flows_reviewed"] >= 1
    assert rv["scope"]["flows_reviewed"] <= rv["scope"]["flows_traced"]
    # persisted onto the feature node
    assert graph.nodes["feature:Greeting"]["flow_review"]["status"] == "static_only"


def test_llm_merge_and_status_without_coverage(proj) -> None:
    root, graph = proj
    provider = AnalysisProvider(status="partially_verified")
    rv = build_flow_review(root, graph, provider, "Greeting")
    # no mapped tests -> the claim is downgraded to insufficient runtime evidence
    assert rv["status"] == "insufficient_runtime_evidence"
    step = rv["flows"][0][0]
    assert step["explanation"].startswith("handles func:app.py::greet")
    assert "APPROVED INTENT" not in provider.last_prompt  # none exists yet


def test_verified_is_computed_never_asserted(proj) -> None:
    root, graph = proj
    # feature-level tests alone are NOT enough anymore: coverage must land on
    # each step's own lines (the dual-review "verified-by-smearing" fix)
    graph.nodes["feature:Greeting"]["exercised_by"] = ["tests/test_app.py::test_greet"]
    rv_smear = build_flow_review(root, graph, AnalysisProvider(status="verified"),
                                 "Greeting")
    assert rv_smear["status"] == "insufficient_runtime_evidence"
    # the un-covered steps carry honest context, not coverage evidence
    assert any(e["kind"] == "context" for f in rv_smear["flows"]
               for s in f for e in s["evidence"])

    # now give every step its OWN coverage -> computed verified is reached
    graph.nodes["func:app.py::greet"]["exercised_by"] = ["tests/test_app.py::test_greet"]
    graph.nodes["func:app.py::helper"]["exercised_by"] = ["tests/test_app.py::test_greet"]
    graph.nodes["feature:Greeting"].pop("flow_review", None)
    rv = build_flow_review(root, graph, AnalysisProvider(status="verified"), "Greeting")
    assert rv["status"] == "verified"
    assert all(any(e["kind"] == "coverage" for e in s["evidence"])
               for f in rv["flows"] for s in f if s["in_feature"])

    # same claim WITHOUT any coverage: never verified
    del graph.nodes["feature:Greeting"]["exercised_by"]
    del graph.nodes["func:app.py::greet"]["exercised_by"]
    del graph.nodes["func:app.py::helper"]["exercised_by"]
    graph.nodes["feature:Greeting"].pop("flow_review", None)
    rv2 = build_flow_review(root, graph, AnalysisProvider(status="verified"), "Greeting")
    assert rv2["status"] == "insufficient_runtime_evidence"


def test_model_cannot_claim_proven_on_heuristic_edges(proj) -> None:
    root, graph = proj
    rv = build_flow_review(root, graph,
                           AnalysisProvider(classification="proven"), "Greeting")
    # every CALLS edge here is heuristic name resolution -> "static" at best
    assert all(s["classification"] == "static"
               for f in rv["flows"] for s in f)


def test_model_cannot_upgrade_to_observed_without_coverage(proj) -> None:
    root, graph = proj
    rv = build_flow_review(root, graph,
                           AnalysisProvider(classification="observed"), "Greeting")
    assert all(s["classification"] == "inferred"
               for f in rv["flows"] for s in f)  # downgraded: no coverage evidence


def test_cache_reuse_and_stale_flag(proj) -> None:
    root, graph = proj
    provider = AnalysisProvider()
    build_flow_review(root, graph, provider, "Greeting")
    rv2 = build_flow_review(root, graph, AnalysisProvider(fail=True), "Greeting")
    assert rv2["reused"] is True  # dead provider never reached

    stored = read_flow_review(root, graph, "Greeting")
    assert stored["stale"] is False
    # a member file change moves the hash -> served stale, not regenerated
    graph.nodes["file:app.py"]["mtime"] = 9999999.0
    stored = read_flow_review(root, graph, "Greeting")
    assert stored["stale"] is True


def test_approved_decision_feeds_prompt_and_invalidates(proj) -> None:
    root, graph = proj
    before = content_hash(graph, root, "Greeting")
    store = DecisionStore(root / ".memory", root=root)
    dec = store.propose("Greeting", "Strip politely",
                        {"behaviour": "greet returns the stripped name",
                         "prohibited": ["mutating input"]})
    store.approve(dec["id"], "alex")
    assert content_hash(graph, root, "Greeting") != before  # old review now stale

    provider = AnalysisProvider()
    build_flow_review(root, graph, provider, "Greeting")
    assert "APPROVED INTENT" in provider.last_prompt
    assert "PROHIBITED: mutating input" in provider.last_prompt


def test_unknown_feature_and_bad_reply(proj) -> None:
    root, graph = proj
    with pytest.raises(FlowReviewError):
        build_flow_review(root, graph, MockProvider(), "Ghost")

    class Garbage:
        name = "fake"
        model = None

        def summarize(self, prompt, context):
            return "not json at all"

    with pytest.raises(FlowReviewError):
        build_flow_review(root, graph, Garbage(), "Greeting")
    assert set(FLOW_STATUSES) >= {"verified", "static_only", "differs_from_intent"}


def test_flow_review_survives_incremental_update(tmp_path) -> None:
    from cms.memory import CodebaseMemory
    from cms.update import incremental_update

    (tmp_path / "app.py").write_text(SOURCE, encoding="utf-8")
    incremental_update(tmp_path, MockProvider(), echo=lambda *_: None)
    graph_path = tmp_path / ".memory" / "graph.json"
    mem = CodebaseMemory.load(graph_path)
    build_flow_review(tmp_path, mem.graph, MockProvider(), "Greeting")
    mem.save(graph_path)

    incremental_update(tmp_path, MockProvider(), echo=lambda *_: None)
    after = CodebaseMemory.load(graph_path).graph.nodes["feature:Greeting"]
    assert (after.get("flow_review") or {}).get("status") == "static_only"
