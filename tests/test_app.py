import json
from pathlib import Path

import pytest

import cms.app as app_mod
from cms.app import resolve_root


@pytest.fixture(autouse=True)
def _no_native_dialog(monkeypatch):
    """First-run setup tries a native folder dialog; never let a real window
    open during tests (it would block on a headed machine)."""
    monkeypatch.setattr("cms.picker.pick_folder", lambda *a, **k: None)


def test_explicit_root_wins(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    target = tmp_path / "proj"
    target.mkdir()
    assert resolve_root(target) == target.resolve()


def test_cwd_with_source_is_used(tmp_path: Path, monkeypatch) -> None:
    (tmp_path / "main.py").write_text("pass\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    assert resolve_root(None) == tmp_path.resolve()


def test_saved_workspace_used_when_cwd_empty(tmp_path: Path, monkeypatch) -> None:
    empty = tmp_path / "empty"
    empty.mkdir()
    project = tmp_path / "project"
    project.mkdir()
    (project / "code.py").write_text("pass\n", encoding="utf-8")
    monkeypatch.chdir(empty)
    (empty / "cms.workspace.json").write_text(
        json.dumps({"root": str(project)}), encoding="utf-8"
    )
    assert resolve_root(None, echo=lambda *a: None) == project.resolve()


def test_first_run_setup_prompts_and_saves(tmp_path: Path, monkeypatch) -> None:
    empty = tmp_path / "empty"
    empty.mkdir()
    project = tmp_path / "project"
    project.mkdir()
    (project / "code.py").write_text("pass\n", encoding="utf-8")
    monkeypatch.chdir(empty)

    answers = iter([str(tmp_path / "nonexistent"), str(project)])
    monkeypatch.setattr("builtins.input", lambda *a: next(answers))
    monkeypatch.setattr(app_mod.sys.stdin, "isatty", lambda: True, raising=False)

    root = resolve_root(None, echo=lambda *a: None)
    assert root == project.resolve()
    saved = json.loads((empty / "cms.workspace.json").read_text(encoding="utf-8"))
    assert Path(saved["root"]) == project.resolve()
    # second launch skips the prompt entirely
    monkeypatch.setattr("builtins.input", lambda *a: (_ for _ in ()).throw(AssertionError("prompted again")))
    assert resolve_root(None, echo=lambda *a: None) == project.resolve()


def test_non_interactive_without_workspace_returns_none(tmp_path: Path, monkeypatch) -> None:
    empty = tmp_path / "empty"
    empty.mkdir()
    monkeypatch.chdir(empty)
    monkeypatch.setattr(app_mod.sys.stdin, "isatty", lambda: False, raising=False)
    assert resolve_root(None, echo=lambda *a: None) is None


def test_saved_workspace_beats_cwd_with_source(tmp_path, monkeypatch) -> None:
    """A saved workspace (last switched codebase) wins over the current dir —
    so relaunching CMS.bat (which runs from the repo) stays on the chosen root."""
    here = tmp_path / "here"
    here.mkdir()
    (here / "here.py").write_text("pass\n", encoding="utf-8")   # cwd is itself a project
    there = tmp_path / "there"
    there.mkdir()
    (there / "there.py").write_text("pass\n", encoding="utf-8")
    monkeypatch.chdir(here)
    (here / "cms.workspace.json").write_text(json.dumps({"root": str(there)}), encoding="utf-8")
    assert resolve_root(None, echo=lambda *a: None) == there.resolve()
