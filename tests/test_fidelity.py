"""Intent fidelity: evidence-backed dimensions with explanations, honest
insufficient-evidence handling, and attention on contradictions."""

from pathlib import Path

import pytest

from cms.annotations import AnnotationStore
from cms.decisions import DecisionStore
from cms.features import build_features
from cms.fidelity import DIMENSIONS, OVERALL, intent_fidelity
from cms.graph_builder import build_graph
from cms.providers import MockProvider
from cms.scanner import scan

SOURCE = '''\
# @memory:feature:Greeting
def greet(name):
    return name
'''


@pytest.fixture()
def proj(tmp_path):
    (tmp_path / "app.py").write_text(SOURCE, encoding="utf-8")
    graph = build_graph(scan(tmp_path))
    build_features(graph, MockProvider())
    (tmp_path / ".memory").mkdir()
    return tmp_path, graph


def test_insufficient_evidence_when_thin(proj) -> None:
    root, graph = proj
    f = intent_fidelity(root, graph, "Greeting")
    assert f["overall"] == "insufficient_evidence"
    assert set(f["dimensions"]) == set(DIMENSIONS)
    # decomposability: every dimension explains itself
    assert all(f["explanations"][k] for k in DIMENSIONS)
    assert f["dimensions"]["implemented"] == "unknown"
    assert f["dimensions"]["tests_passing"] == "not_recorded"


def test_on_track_with_review_and_tests(proj) -> None:
    root, graph = proj
    node = graph.nodes["feature:Greeting"]
    node["review"] = {"verdict": "aligned", "headline": "ok"}
    node["exercised_by"] = ["tests/test_app.py::test_greet"]
    node["verify_result"] = {"passed": True, "tests": 1, "at": "2026-07-13T00:00:00Z"}
    f = intent_fidelity(root, graph, "Greeting")
    assert f["overall"] == "on_track"
    assert f["dimensions"]["implemented"] == "yes"
    assert f["dimensions"]["tests_passing"] == "passed"
    assert "1 mapped test" in f["explanations"]["tests_present"]


def test_attention_on_contradiction(proj) -> None:
    root, graph = proj
    node = graph.nodes["feature:Greeting"]
    node["review"] = {"verdict": "aligned", "headline": "ok"}
    node["exercised_by"] = ["tests/test_app.py::test_greet"]
    AnnotationStore(root / ".memory", root=root).add(
        "feature:Greeting", "contradiction", "docs say X, code does Y",
        feature="Greeting")
    f = intent_fidelity(root, graph, "Greeting")
    assert f["overall"] == "attention"
    assert f["dimensions"]["open_contradictions"] == 1


def test_intent_match_against_approved_decision(proj) -> None:
    root, graph = proj
    store = DecisionStore(root / ".memory", root=root)
    dec = store.propose("Greeting", "t", {"behaviour": "returns name unchanged"})
    store.approve(dec["id"], "alex")

    node = graph.nodes["feature:Greeting"]
    f = intent_fidelity(root, graph, "Greeting")
    assert f["dimensions"]["approved_intent"] == "present"
    assert f["dimensions"]["intent_match"] == "unknown"  # no verifying evidence yet

    node["flow_review"] = {"status": "differs_from_intent", "content_hash": "x"}
    f = intent_fidelity(root, graph, "Greeting")
    assert f["dimensions"]["intent_match"] == "differs"
    assert f["overall"] == "attention"
    assert f["dimensions"]["stale_evidence"] is True  # hash 'x' no longer matches


def test_unknown_feature_raises(proj) -> None:
    root, graph = proj
    with pytest.raises(ValueError):
        intent_fidelity(root, graph, "Ghost")
    assert set(OVERALL) == {"on_track", "attention", "insufficient_evidence"}
