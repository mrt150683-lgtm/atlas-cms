from pathlib import Path

import networkx as nx
from typer.testing import CliRunner

from cms.cli import app
from cms.memory import CodebaseMemory


def _verified_project(root: Path) -> None:
    graph = nx.DiGraph()
    graph.add_node(
        "feature:TruthLayer",
        type="feature",
        name="TruthLayer",
        source="declared",
        description="Keeps completion claims proportional to their evidence.",
        members=[],
        entry_points=[],
        flows=[],
        connects=[],
        exercised_by=["tests/test_truth.py::test_truth"],
    )
    memory_dir = root / ".memory"
    memory_dir.mkdir()
    CodebaseMemory(graph).save(memory_dir / "graph.json")


def test_targeted_verify_describes_coverage_honestly(tmp_path: Path, monkeypatch) -> None:
    _verified_project(tmp_path)
    monkeypatch.setattr("cms.verify.verify_feature", lambda root, tests: (True, "1 passed"))

    result = CliRunner().invoke(app, ["verify", "TruthLayer", "--root", str(tmp_path)])

    assert result.exit_code == 0
    assert "mapped as exercising TruthLayer" in result.output
    assert "coverage proves these tests executed the feature" in result.output
    assert "feature behaves as specified" not in result.output


def test_targeted_verify_failure_does_not_infer_design_divergence(tmp_path: Path, monkeypatch) -> None:
    _verified_project(tmp_path)
    monkeypatch.setattr("cms.verify.verify_feature", lambda root, tests: (False, "1 failed"))

    result = CliRunner().invoke(app, ["verify", "TruthLayer", "--root", str(tmp_path)])

    assert result.exit_code == 1
    assert "one or more tests mapped to this feature failed" in result.output
    assert "implementation diverges" not in result.output


def test_verify_help_exposes_forced_coverage_refresh() -> None:
    result = CliRunner().invoke(app, ["verify", "--help"])
    assert result.exit_code == 0
    assert "--refresh" in result.output
    assert "Ignore cached coverage" in result.output


def test_verify_help_exposes_forced_coverage_refresh() -> None:
    result = CliRunner().invoke(app, ["verify", "--help"])
    assert result.exit_code == 0
    assert "--refresh" in result.output
    assert "Ignore cached coverage" in result.output
