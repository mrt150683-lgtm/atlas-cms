"""NL feature discovery: evidence-grounded proposals (never auto-accepted),
honest mock degradation, and durable confirmation through semantic state."""

import json
from pathlib import Path

import pytest

from cms import semantic_state as ss
from cms.exporter import export_graph
from cms.feature_discovery import (
    FeatureDiscoveryError,
    _feature_catalog,
    confirm_feature,
    propose_feature,
)
from cms.features import build_features
from cms.graph_builder import build_graph
from cms.memory import CodebaseMemory
from cms.providers import MockProvider
from cms.scanner import scan
from cms.update import incremental_update

SOURCE = '''\
def upload_document(doc):
    """Store an uploaded document and index it for search."""
    return index_document(doc)


def index_document(doc):
    """Extract content and store search embeddings."""
    return doc
'''


@pytest.fixture()
def proj(tmp_path):
    (tmp_path / "docs_app.py").write_text(SOURCE, encoding="utf-8")
    graph = build_graph(scan(tmp_path))
    build_features(graph, MockProvider())
    memory_dir = tmp_path / ".memory"
    export_graph(graph, memory_dir)
    return tmp_path, CodebaseMemory(graph)


class MappingProvider:
    name = "fake"
    model = "fake-1"

    def summarize(self, prompt: str, context: dict) -> str:
        assert "upload" in prompt.lower()
        return json.dumps({
            "name": "DocumentSearch",
            "description": "Uploaded documents are indexed and searchable.",
            "members": [
                {"id": "func:docs_app.py::upload_document", "why": "entry point storing uploads"},
                {"id": "func:docs_app.py::invented_function", "why": "hallucinated"},
            ],
            "note": "no runtime evidence",
        })


def test_mock_returns_hits_only_never_a_mapping(proj) -> None:
    root, memory = proj
    out = propose_feature(root, memory, "users upload a document and it becomes searchable",
                          MockProvider())
    assert out["real"] is False and out["candidate"] is None
    assert out["hits"] and "NOT a verified mapping" in out["note"]


def test_real_proposal_drops_hallucinated_members(proj) -> None:
    root, memory = proj
    out = propose_feature(root, memory, "users upload a document and it becomes searchable",
                          MappingProvider())
    c = out["candidate"]
    assert c["name"] == "DocumentSearch"
    assert [m["id"] for m in c["members"]] == ["func:docs_app.py::upload_document"]


def test_short_description_rejected(proj) -> None:
    root, memory = proj
    with pytest.raises(FeatureDiscoveryError):
        propose_feature(root, memory, "upload", MockProvider())


ANCHORED = '''\
# @memory:feature:DocumentSearch
# @memory:summary:Uploaded documents are indexed and made searchable.
def upload_document(doc):
    return index_document(doc)


def index_document(doc):
    return doc
'''

HELPER = '''\
# @memory:feature:Notifications
def notify(user):
    return user
'''


@pytest.fixture()
def mapped_proj(tmp_path):
    """A project where the described behaviour IS already a declared feature."""
    (tmp_path / "docs_app.py").write_text(ANCHORED, encoding="utf-8")
    (tmp_path / "notify.py").write_text(HELPER, encoding="utf-8")
    graph = build_graph(scan(tmp_path))
    build_features(graph, MockProvider())
    export_graph(graph, tmp_path / ".memory")
    return tmp_path, CodebaseMemory(graph)


def test_already_stated_is_flagged_even_under_mock(mapped_proj) -> None:
    """The already-stated check is deterministic (names + shared members), so
    it must fire with no provider at all."""
    root, memory = mapped_proj
    out = propose_feature(root, memory,
                          "users upload a document and it becomes searchable",
                          MockProvider())
    assert out["real"] is False
    assert out["existing"] and out["existing"][0]["feature"] == "DocumentSearch"
    assert "already stated" in out["note"].lower()


class HuntProvider:
    """Returns the full hunt shape, with deliberate junk to be validated away."""

    name = "fake"
    model = "fake-1"

    def __init__(self, verdict="already_covered") -> None:
        self.verdict = verdict
        self.last_prompt = ""

    def summarize(self, prompt: str, context: dict) -> str:
        self.last_prompt = prompt
        return json.dumps({
            "verdict": self.verdict,
            "existing": [{"feature": "DocumentSearch", "why": "same behaviour"},
                         {"feature": "GhostFeature", "why": "hallucinated"}],
            "name": "DocSearch2",
            "description": "duplicate mapping",
            "members": [{"id": "func:docs_app.py::upload_document",
                         "role": "entry", "why": "starts the flow"},
                        {"id": "func:docs_app.py::index_document",
                         "role": "wizard", "why": "bad role degrades to core"}],
            "connections": [{"feature": "Notifications", "via": "asserted by model"},
                            {"feature": "GhostFeature", "via": "hallucinated"}],
            "explanation": [
                f"`upload_document` performs grounded step {i}" for i in range(1, 10)
            ] + ["fluent but unreferenced claim", "`GhostFunction` invents a step"],
            "uncertainty": "no runtime evidence",
        })


def test_already_covered_verdict_flags_instead_of_duplicating(mapped_proj) -> None:
    root, memory = mapped_proj
    provider = HuntProvider(verdict="already_covered")
    out = propose_feature(root, memory,
                          "users upload a document and it becomes searchable", provider)
    assert out["verdict"] == "already_covered"
    assert out["candidate"] is None  # never proposes a duplicate
    names = [e["feature"] for e in out["existing"]]
    assert "DocumentSearch" in names and "GhostFeature" not in names
    # the model saw the catalog and the real neighbourhood, not just keywords
    assert "EXISTING FEATURES" in provider.last_prompt
    assert "GRAPH NEIGHBOURHOOD" in provider.last_prompt
    assert out["explanation"] and len(out["explanation"]) <= 7
    assert out["explanation_provenance"] == "llm_grounded"
    assert all("GhostFunction" not in step for step in out["explanation"])


def test_hunt_validates_members_roles_and_connections(mapped_proj) -> None:
    root, memory = mapped_proj
    out = propose_feature(root, memory,
                          "users upload a document and it becomes searchable",
                          HuntProvider(verdict="new"))
    c = out["candidate"]
    roles = {m["id"]: m["role"] for m in c["members"]}
    assert roles["func:docs_app.py::upload_document"] == "entry"
    assert roles["func:docs_app.py::index_document"] == "core"  # bad role degraded
    conn_names = {c2["feature"] for c2 in out["connections"]}
    assert "GhostFeature" not in conn_names  # unknown features dropped
    assert all(c2["provenance"] in ("llm", "graph") for c2 in out["connections"])


def test_graph_connections_are_edge_grounded(mapped_proj) -> None:
    from cms.feature_discovery import graph_connections

    root, memory = mapped_proj
    graph = memory.graph
    # upload_document calls index_document; both DocumentSearch. Add a real
    # cross-feature edge: upload_document -> notify (Notifications member)
    graph.add_edge("func:docs_app.py::upload_document", "func:notify.py::notify",
                   type="CALLS", provenance="heuristic")
    conns = graph_connections(graph, ["func:docs_app.py::upload_document"])
    assert {"feature": "Notifications",
            "via": "docs_app.py::upload_document calls notify.py::notify",
            "provenance": "graph"} in conns


def test_graph_connections_preserve_shared_feature_ownership(mapped_proj) -> None:
    from cms.feature_discovery import graph_connections

    _, memory = mapped_proj
    graph = memory.graph
    graph.add_node("feature:AuditTrail", type="feature", name="AuditTrail",
                   members=["func:notify.py::notify"])
    graph.add_edge("func:docs_app.py::upload_document", "func:notify.py::notify",
                   type="CALLS", provenance="heuristic")

    names = {c["feature"] for c in graph_connections(
        graph, ["func:docs_app.py::upload_document"])}

    assert {"Notifications", "AuditTrail"}.issubset(names)


def test_catalog_and_verdict_validation_use_all_features(proj) -> None:
    root, memory = proj
    for i in range(45):
        memory.graph.add_node(f"feature:Mapped{i:02d}", type="feature",
                              name=f"Mapped{i:02d}", description=f"Mapped feature {i}",
                              members=[])
    assert len(_feature_catalog(memory.graph)) == 45

    class UnsupportedProvider:
        name = "fake"
        model = "fake-1"

        def summarize(self, _prompt: str, _context: dict) -> str:
            return json.dumps({
                "verdict": "already_covered",
                "existing": [{"feature": "GhostFeature", "why": "invented"}],
                "members": [{"id": "func:docs_app.py::invented", "role": "core"}],
                "explanation": ["`GhostFunction` does everything"],
            })

    out = propose_feature(root, memory,
                          "users upload a document and it becomes searchable",
                          UnsupportedProvider())
    assert out["verdict"] == "not_found"
    assert out["candidate"] is None and out["existing"] == []
    assert out["explanation"] == [] and out["explanation_provenance"] == "none"


def test_confirm_writes_durable_feature(proj) -> None:
    root, _ = proj
    out = confirm_feature(root, "DocumentSearch", "Uploads become searchable.",
                          ["func:docs_app.py::upload_document"])
    assert out["confirmed"] is True

    graph = CodebaseMemory.load(root / ".memory" / "graph.json").graph
    node = graph.nodes["feature:DocumentSearch"]
    assert node["source"] == "discovered"
    assert node["members"] == ["func:docs_app.py::upload_document"]
    # durable: recorded in semantic state so updates re-inject it
    rec = ss.stage(ss.load_state(root / ".memory"), "features")
    assert any(f["name"] == "DocumentSearch" for f in rec["discovered_features"])

    # survives a full incremental update (mock: no re-discovery)
    incremental_update(root, MockProvider(), echo=lambda *_: None)
    after = CodebaseMemory.load(root / ".memory" / "graph.json").graph
    assert after.has_node("feature:DocumentSearch")

    with pytest.raises(FeatureDiscoveryError):
        confirm_feature(root, "DocumentSearch", "dup", ["func:docs_app.py::upload_document"])
    with pytest.raises(FeatureDiscoveryError):
        confirm_feature(root, "Ghosts", "x", ["func:docs_app.py::not_there"])
