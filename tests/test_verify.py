import json
from pathlib import Path
from types import SimpleNamespace

from cms.features import build_features
from cms.graph_builder import build_graph
from cms.providers import MockProvider
from cms.scanner import scan
from cms.verify import map_tests_to_features, run_coverage


def _fake_coverage_runner(calls: list[list[str]]):
    def run(command, **_kwargs):
        calls.append(command)
        if "json" in command:
            output = Path(command[command.index("-o") + 1])
            output.write_text(json.dumps({"files": {}}), encoding="utf-8")
        return SimpleNamespace(returncode=0, stdout="", stderr="")
    return run


def test_coverage_cache_reuses_unchanged_evidence(tmp_path: Path, monkeypatch) -> None:
    (tmp_path / "app.py").write_text("def run():\n    return 1\n", encoding="utf-8")
    tests = tmp_path / "tests"
    tests.mkdir()
    (tests / "test_app.py").write_text("def test_run():\n    assert True\n", encoding="utf-8")
    calls: list[list[str]] = []
    messages: list[str] = []
    monkeypatch.setattr("cms.verify.subprocess.run", _fake_coverage_runner(calls))

    assert run_coverage(tmp_path, echo=messages.append) == {"files": {}}
    assert len(calls) == 2
    assert any("stage 1/3" in message for message in messages)
    assert any("stage 3/3" in message for message in messages)

    messages.clear()
    assert run_coverage(tmp_path, echo=messages.append) == {"files": {}}
    assert len(calls) == 2
    assert messages == ["Coverage cache is current — reusing mapped per-test contexts."]


def test_coverage_cache_invalidates_on_change_and_refresh(tmp_path: Path, monkeypatch) -> None:
    source = tmp_path / "app.py"
    source.write_text("value = 1\n", encoding="utf-8")
    calls: list[list[str]] = []
    monkeypatch.setattr("cms.verify.subprocess.run", _fake_coverage_runner(calls))

    run_coverage(tmp_path)
    source.write_text("value = 200\n", encoding="utf-8")
    run_coverage(tmp_path)
    assert len(calls) == 4

    run_coverage(tmp_path, refresh=True)
    assert len(calls) == 6


APP = '''\
import json

# @memory:feature:Greeting
def greet(name):
    return helper(name)


def helper(name):
    return name.strip()
'''


def _coverage(files: dict) -> dict:
    """coverage.py JSON shape: files -> contexts {line: [context, ...]}."""
    return {"files": {
        path: {"contexts": {str(ln): ctxs for ln, ctxs in lines.items()}}
        for path, lines in files.items()}}


def test_mapping_is_step_granular_and_ignores_import_lines(tmp_path: Path) -> None:
    """Dual-review Priority-0: exercised_by lands per component, and a file
    member's import-time lines are not behavioural evidence."""
    (tmp_path / "app.py").write_text(APP, encoding="utf-8")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_app.py").write_text(
        "def test_greet():\n    assert True\n", encoding="utf-8")
    graph = build_graph(scan(tmp_path))
    build_features(graph, MockProvider())
    # widen the feature to the whole file so the import-line rule is exercised
    graph.nodes["feature:Greeting"]["members"] = ["file:app.py"]

    ctx = "tests.test_app.test_greet"
    # only line 1 (the import) executed -> NOT evidence for the feature
    mapping = map_tests_to_features(
        graph, tmp_path, _coverage({"app.py": {1: [ctx]}}))
    assert mapping["Greeting"] == []
    assert "exercised_by" not in graph.nodes["func:app.py::greet"]

    # greet's body (line 5) executed -> feature evidence AND step evidence
    # on greet only; helper stays uncovered
    mapping = map_tests_to_features(
        graph, tmp_path, _coverage({"app.py": {1: [ctx], 5: [ctx]}}))
    assert mapping["Greeting"] == ["tests/test_app.py::test_greet"]
    assert graph.nodes["func:app.py::greet"]["exercised_by"] == [
        "tests/test_app.py::test_greet"]
    assert "exercised_by" not in graph.nodes["func:app.py::helper"]

    # a later run where the line is no longer executed clears the step evidence
    mapping = map_tests_to_features(
        graph, tmp_path, _coverage({"app.py": {1: [ctx]}}))
    assert "exercised_by" not in graph.nodes["func:app.py::greet"]
