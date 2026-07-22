import json
from pathlib import Path

from typer.testing import CliRunner

from cms.cli import app
from cms.exporter import export_graph
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
    assert result["status"] == "complete"
    assert review["evidence_kind"] == "semantic"
    assert graph.nodes["review:app"]["counts"]["aligned"] == 1


def test_review_rollup_excludes_reference_only_features(tmp_path: Path) -> None:
    graph = _graph(tmp_path)
    member = "file:docs/office-authoring.md"
    graph.add_node(member, type="file", path="docs/office-authoring.md")
    graph.add_node(
        "feature:OfficeDocumentAuthoring", type="feature",
        name="OfficeDocumentAuthoring", source="discovered", members=[member],
        entry_points=[], connects=[], aliases=[], description="Reference skill",
    )

    result = build_review(graph, tmp_path, JsonProvider())

    assert set(result["features"]) == {"Greeting"}
    assert result["excluded_features"] == ["OfficeDocumentAuthoring"]
    assert result["app"]["excluded_reference_features"] == 1
    assert graph.nodes["review:app"]["counts"]["aligned"] == 1


def test_mock_review_is_unverified_and_exports(tmp_path: Path) -> None:
    graph = _graph(tmp_path)
    build_review(graph, tmp_path, MockProvider())
    review = graph.nodes["feature:Greeting"]["review"]
    assert review["verdict"] == "unverified"
    assert review["structural"] is True
    assert review["evidence_kind"] == "structural"
    assert "No tests currently exercise" in review["gaps"][0]
    out = export_review(graph, tmp_path)
    text = out.read_text(encoding="utf-8")
    assert "# App Review" in text and "Greeting" in text


def test_bad_llm_json_falls_back(tmp_path: Path) -> None:
    class BrokenProvider(JsonProvider):
        def summarize(self, prompt, context):
            return "not json at all"

    graph = _graph(tmp_path)
    result = build_review(graph, tmp_path, BrokenProvider())
    assert result["status"] == "failed"
    assert result["features"]["Greeting"]["structural"] is True
    assert result["app"]["verdict"] == "unverified"
    assert result["app"]["fallback_features"] == 1
    assert "review" not in graph.nodes["feature:Greeting"]
    assert not graph.has_node("review:app")


def test_provider_exception_is_reported_and_transactional(tmp_path: Path) -> None:
    class FailingProvider(JsonProvider):
        def summarize(self, prompt, context):
            raise ConnectionError("offline")

    graph = _graph(tmp_path)
    graph.nodes["feature:Greeting"]["review"] = {"verdict": "aligned", "headline": "old"}
    graph.add_node("review:app", type="review", verdict="aligned", headline="old", summary="old")
    result = build_review(graph, tmp_path, FailingProvider())
    assert result["status"] == "failed"
    assert "ConnectionError: offline" in result["provider_errors"][0]
    assert graph.nodes["feature:Greeting"]["review"]["headline"] == "old"
    assert graph.nodes["review:app"]["headline"] == "old"


def test_app_rollup_failure_does_not_commit_feature_reviews(tmp_path: Path) -> None:
    class BrokenRollupProvider(JsonProvider):
        def summarize(self, prompt, context):
            if "top-level review" in prompt:
                return "not json"
            return super().summarize(prompt, context)

    graph = _graph(tmp_path)
    result = build_review(graph, tmp_path, BrokenRollupProvider())
    assert result["status"] == "failed"
    assert result["app"]["verdict"] == "unverified"
    assert "review" not in graph.nodes["feature:Greeting"]


def test_json_parser_accepts_fences_and_ignores_later_objects(tmp_path: Path) -> None:
    class FencedProvider(JsonProvider):
        def summarize(self, prompt, context):
            return "```json\n" + super().summarize(prompt, context) + "\n```\nExtra {broken}"

    graph = _graph(tmp_path)
    result = build_review(graph, tmp_path, FencedProvider())
    assert result["status"] == "complete"
    assert graph.nodes["feature:Greeting"]["review"]["verdict"] == "aligned"


def test_cli_failed_provider_exits_nonzero_and_records_failed(tmp_path: Path, monkeypatch) -> None:
    class FailingProvider(JsonProvider):
        model = "test-model"

        def summarize(self, prompt, context):
            raise ConnectionError("offline")

    graph = _graph(tmp_path)
    export_graph(graph, tmp_path / ".memory")
    monkeypatch.setattr("cms.cli.get_provider", lambda *_: FailingProvider())

    result = CliRunner().invoke(
        app, ["review", "--root", str(tmp_path), "--provider", "fake-llm"])

    assert result.exit_code == 1
    assert "Overall: UNVERIFIED" in result.output
    assert "Existing review artifacts were not overwritten" in result.output
    state = json.loads((tmp_path / ".memory" / "semantic_state.json").read_text(encoding="utf-8"))
    assert state["stages"]["review"]["status"] == "failed"
    assert "ConnectionError: offline" in state["stages"]["review"]["error"]
