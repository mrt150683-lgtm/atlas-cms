"""Human explanation cache: dependency-aware keys, cache-first serving,
targeted invalidation, honest mock degradation, and pruning."""

import json
from pathlib import Path

import pytest

from cms.explain import (
    ExplainError,
    cache_key,
    content_hash,
    explain_nodes,
    load_cache,
    prune_explanations,
)
from cms.features import build_features
from cms.graph_builder import build_graph
from cms.hierarchy import ensure_hierarchy
from cms.providers import MockProvider
from cms.scanner import scan

SOURCE = '''\
# @memory:feature:Greeting
def greet(name):
    return helper(name)


def helper(name):
    return name
'''


class EchoProvider:
    """Real-provider stand-in: one JSON string per requested item."""

    name = "fake"
    model = "fake-1"

    def __init__(self, fail: bool = False) -> None:
        self.calls = 0
        self.fail = fail

    def summarize(self, prompt: str, context: dict) -> str:
        self.calls += 1
        if self.fail:
            raise RuntimeError("provider down")
        import re

        count = int(re.search(r"JSON array of (\d+) strings", prompt).group(1))
        return json.dumps([f"EXPLAINED {i}" for i in range(count)])


@pytest.fixture()
def proj(tmp_path):
    (tmp_path / "app.py").write_text(SOURCE, encoding="utf-8")
    graph = build_graph(scan(tmp_path))
    build_features(graph, MockProvider())
    (tmp_path / ".memory").mkdir()
    ensure_hierarchy(tmp_path / ".memory", graph, MockProvider(), echo=lambda *_: None)
    return tmp_path, graph


def test_generate_then_cache_hit_skips_provider(proj) -> None:
    root, graph = proj
    provider = EchoProvider()
    out = explain_nodes(root, graph, [{"id": "feature:Greeting"}], provider)
    assert out["real"] is True
    assert out["results"]["feature:Greeting"]["status"] == "generated"
    assert provider.calls == 1

    dead = EchoProvider(fail=True)
    out2 = explain_nodes(root, graph, [{"id": "feature:Greeting"}], dead)
    assert out2["results"]["feature:Greeting"]["status"] == "cached"
    assert out2["results"]["feature:Greeting"]["text"] == "EXPLAINED 0"
    assert dead.calls == 0


def test_content_change_invalidates_only_affected(proj) -> None:
    root, graph = proj
    explain_nodes(root, graph, [{"id": "feature:Greeting"}, {"id": "file:app.py"}],
                  EchoProvider())
    assert len(load_cache(root)) == 2

    # a summary change on the file: its hash moves, the feature entry whose
    # hash doesn't include file summaries stays valid
    graph.nodes["file:app.py"]["summary"] = "changed summary"
    out = explain_nodes(root, graph, [{"id": "feature:Greeting"}, {"id": "file:app.py"}],
                        EchoProvider())
    assert out["results"]["feature:Greeting"]["status"] == "cached"
    assert out["results"]["file:app.py"]["status"] == "generated"


def test_hierarchy_hash_cascades_upward(proj) -> None:
    _, graph = proj
    systems = [n for n, a in graph.nodes(data=True) if a.get("type") == "system"]
    before = content_hash(graph, systems[0])
    # a change to a feature inside the pyramid must move its ancestors' hashes
    graph.nodes["feature:Greeting"]["description"] = "reworded"
    assert content_hash(graph, systems[0]) != before


def test_mock_returns_structural_and_never_caches(proj) -> None:
    root, graph = proj
    out = explain_nodes(root, graph, [{"id": "feature:Greeting"}], MockProvider())
    assert out["real"] is False
    res = out["results"]["feature:Greeting"]
    assert res["status"] == "structural" and "no AI explanation" in res["text"]
    assert load_cache(root) == {}


def test_force_regenerates(proj) -> None:
    root, graph = proj
    explain_nodes(root, graph, [{"id": "file:app.py"}], EchoProvider())
    p2 = EchoProvider()
    out = explain_nodes(root, graph, [{"id": "file:app.py"}], p2, force=True)
    assert out["results"]["file:app.py"]["status"] == "generated" and p2.calls == 1


def test_provider_failure_keeps_uncached_for_retry(proj) -> None:
    root, graph = proj
    out = explain_nodes(root, graph, [{"id": "file:app.py"}], EchoProvider(fail=True))
    assert out["results"]["file:app.py"]["status"] == "structural"
    assert load_cache(root) == {}


def test_validation(proj) -> None:
    root, graph = proj
    with pytest.raises(ExplainError):
        explain_nodes(root, graph, [{"id": "file:nope.py"}], MockProvider())
    with pytest.raises(ExplainError):
        explain_nodes(root, graph, [{"id": f"n{i}"} for i in range(13)], MockProvider())


def test_prune_drops_orphans_and_drifted(proj) -> None:
    root, graph = proj
    explain_nodes(root, graph, [{"id": "feature:Greeting"}, {"id": "file:app.py"}],
                  EchoProvider())
    graph.nodes["file:app.py"]["summary"] = "moved on"
    removed = prune_explanations(root, graph)
    assert removed == 1
    kept = load_cache(root)
    assert len(kept) == 1
    entry = next(iter(kept.values()))
    assert entry["node_id"] == "feature:Greeting"
    assert cache_key("feature:Greeting", content_hash(graph, "feature:Greeting")) in kept
