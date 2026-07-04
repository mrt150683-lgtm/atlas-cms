import json
from pathlib import Path

from cms.features import build_features
from cms.graph_builder import build_graph
from cms.providers import MockProvider
from cms.scanner import scan
from cms.suggest import build_suggestions, export_suggestions

SOURCE = '''\
# @memory:feature:Alpha
def alpha():
    pass
'''


class RankingProvider(MockProvider):
    name = "fake-llm"

    def summarize(self, prompt: str, context: dict) -> str:
        return json.dumps([
            {"title": "Low value slog", "kind": "improvement", "description": "d",
             "rationale": "r", "value": 2, "effort": 4, "builds_on": []},
            {"title": "Quick big win", "kind": "new-feature", "description": "d",
             "rationale": "r", "value": 5, "effort": 1, "builds_on": ["Alpha"]},
        ])


def _graph(tmp_path: Path):
    (tmp_path / "a.py").write_text(SOURCE, encoding="utf-8")
    graph = build_graph(scan(tmp_path))
    build_features(graph, MockProvider())
    return graph


def test_suggestions_ranked_by_roi(tmp_path: Path) -> None:
    graph = _graph(tmp_path)
    items = build_suggestions(graph, tmp_path, RankingProvider())
    assert items[0]["title"] == "Quick big win"
    assert items[0]["roi"] == 5.0
    assert items[-1]["roi"] == 0.5
    assert graph.nodes["suggestions:app"]["items"][0]["title"] == "Quick big win"


def test_mock_structural_suggestions_and_export(tmp_path: Path) -> None:
    graph = _graph(tmp_path)
    items = build_suggestions(graph, tmp_path, MockProvider())
    # Alpha has no verifying tests -> structural hardening suggestion
    assert any("Add tests verifying Alpha" in s["title"] for s in items)
    out = export_suggestions(graph, tmp_path)
    text = out.read_text(encoding="utf-8")
    assert "ranked by return on investment" in text and "ROI" in text
