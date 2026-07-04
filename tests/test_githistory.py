import subprocess
from pathlib import Path

import pytest

from cms.githistory import collect_git_history, enrich_graph_with_git
from cms.graph_builder import build_graph
from cms.scanner import scan


def _git(root: Path, *args: str) -> None:
    subprocess.run(
        ["git", *args], cwd=root, check=True, capture_output=True,
        env={"GIT_AUTHOR_NAME": "T", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "T",
             "GIT_COMMITTER_EMAIL": "t@t", "PATH": __import__("os").environ["PATH"],
             "HOME": str(root), "USERPROFILE": str(root)},
    )


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    _git(tmp_path, "init", "-q")
    (tmp_path / "a.py").write_text("def fa():\n    pass\n", encoding="utf-8")
    (tmp_path / "b.py").write_text("def fb():\n    pass\n", encoding="utf-8")
    _git(tmp_path, "add", "."); _git(tmp_path, "commit", "-qm", "c1")
    # a and b change together twice more -> co-change pair (no import edge)
    for i in range(2, 4):
        (tmp_path / "a.py").write_text(f"def fa():\n    return {i}\n", encoding="utf-8")
        (tmp_path / "b.py").write_text(f"def fb():\n    return {i}\n", encoding="utf-8")
        _git(tmp_path, "add", "."); _git(tmp_path, "commit", "-qm", f"c{i}")
    (tmp_path / "a.py").write_text("def fa():\n    return 9\n", encoding="utf-8")
    _git(tmp_path, "add", "."); _git(tmp_path, "commit", "-qm", "c4")
    return tmp_path


def test_collect_history(repo: Path) -> None:
    history = collect_git_history(repo)
    assert history is not None
    a = history["files"]["a.py"]
    assert a["commits"] == 4
    assert a["authors"] == ["T"]
    assert a["churn"] > 0
    assert ("a.py", "b.py", 3) in history["cochanges"]


def test_enrich_graph(repo: Path) -> None:
    graph = build_graph(scan(repo))
    info = enrich_graph_with_git(graph, repo)
    assert info is not None and info["files"] == 2
    assert graph.nodes["file:a.py"]["git"]["commits"] == 4
    assert graph.edges["file:a.py", "file:b.py"]["type"] == "CO_CHANGES"
    assert graph.edges["file:a.py", "file:b.py"]["weight"] == 3


def test_not_a_repo_is_none(tmp_path: Path) -> None:
    (tmp_path / "x.py").write_text("pass\n", encoding="utf-8")
    graph = build_graph(scan(tmp_path))
    assert enrich_graph_with_git(graph, tmp_path) is None
