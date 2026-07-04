from pathlib import Path

from cms.features import build_features
from cms.graph_builder import build_graph
from cms.impact import analyze_impact, resolve_target
from cms.providers import MockProvider
from cms.scanner import scan

CORE = '''\
# @memory:feature:Storage
def save(data):
    return data
'''

SERVICE = '''\
from core import save


def process(x):
    return save(x)
'''

APP = '''\
from service import process


def main():
    process(1)
'''

TEST = '''\
from service import process


def test_process():
    assert process(1) == 1
'''


def _graph(tmp_path: Path):
    (tmp_path / "core.py").write_text(CORE, encoding="utf-8")
    (tmp_path / "service.py").write_text(SERVICE, encoding="utf-8")
    (tmp_path / "app.py").write_text(APP, encoding="utf-8")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_service.py").write_text(TEST, encoding="utf-8")
    graph = build_graph(scan(tmp_path))
    build_features(graph, MockProvider())
    return graph


def test_resolve_target_forms(tmp_path: Path) -> None:
    graph = _graph(tmp_path)
    assert resolve_target(graph, "core.py::save") == "func:core.py::save"
    assert resolve_target(graph, "core.py") == "file:core.py"
    assert resolve_target(graph, "save") == "func:core.py::save"
    assert resolve_target(graph, "nonexistent") is None


def test_impact_walks_upstream(tmp_path: Path) -> None:
    graph = _graph(tmp_path)
    impact = analyze_impact(graph, "core.py::save")
    assert impact is not None
    # transitive callers: process (direct), main (via process)
    assert "service.py::process" in impact.functions
    assert "app.py::main" in impact.functions
    # importing files ripple
    assert "service.py" in impact.files
    assert "app.py" in impact.files
    # tests that exercise the chain
    assert any("test_service" in t for t in impact.tests)
    # feature owning the target
    assert impact.features == ["Storage"]
