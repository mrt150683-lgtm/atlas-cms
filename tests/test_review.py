import json
from pathlib import Path

from cms.features import build_features
from cms.graph_builder import build_graph
from cms.providers import MockProvider
from cms.review import build_review, export_review
from cms.scanner import scan

SOURCE = '''\
# @memory:feature:Greeting
# @memory:summary:Greets users politely.
def greet(name):
    return name
'''


class JsonProvider(MockProvider):
    """Returns well-formed review JSON so the parse/sanitize path is exercised."""

    name = "fake-llm"

    def summarize(self, prompt: str, context: dict) -> str:
        if "top-level review" in prompt:
            return json.dumps({"verdict": "partial", "headline": "Mostly as expected.",
                               "summary": "One feature reviewed."})
        return json.dumps({
            "verdict": "aligned",
            "headline": "Greets people as promised.",
            "expected": "A polite greeting.",
            "built": "greet() returns the name.",
            "gaps": [],
            "education": "It is a single function.",
        })


def _graph(tmp_path: Path):
    (tmp_path / "app.py").write_text(SOURCE, encoding="utf-8")
    graph = build_graph(scan(tmp_path))
    build_features(graph, MockProvider())
    return graph


def test_llm_review_lands_on_nodes_and_rollup(tmp_path: Path) -> None:
    graph = _graph(tmp_path)
    result = build_review(graph, tmp_path, JsonProvider())
    review = graph.nodes["feature:Greeting"]["review"]
    assert review["verdict"] == "aligned"
    assert review["headline"] == "Greets people as promised."
    assert result["app"]["verdict"] == "partial"
    assert graph.nodes["review:app"]["counts"]["aligned"] == 1


def test_mock_review_is_unverified_and_exports(tmp_path: Path) -> None:
    graph = _graph(tmp_path)
    build_review(graph, tmp_path, MockProvider())
    review = graph.nodes["feature:Greeting"]["review"]
    assert review["verdict"] == "unverified"
    assert "No tests currently verify" in review["gaps"][0]
    out = export_review(graph, tmp_path)
    text = out.read_text(encoding="utf-8")
    assert "# App Review" in text and "Greeting" in text


def test_bad_llm_json_falls_back(tmp_path: Path) -> None:
    class BrokenProvider(JsonProvider):
        def summarize(self, prompt, context):
            return "not json at all"

    graph = _graph(tmp_path)
    build_review(graph, tmp_path, BrokenProvider())
    assert graph.nodes["feature:Greeting"]["review"]["verdict"] == "unverified"
