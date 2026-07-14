"""NL feature discovery: evidence-grounded proposals (never auto-accepted),
honest mock degradation, and durable confirmation through semantic state."""

import json
from pathlib import Path

import pytest

from cms import semantic_state as ss
from cms.exporter import export_graph
from cms.feature_discovery import (
    FeatureDiscoveryError,
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
