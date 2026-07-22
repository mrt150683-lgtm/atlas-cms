import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import ANY

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


def test_run_app_syncs_then_starts_watcher_and_ui(tmp_path: Path, monkeypatch) -> None:
    memory_dir = tmp_path / ".memory"
    memory_dir.mkdir()
    (memory_dir / "graph.json").write_text("{}", encoding="utf-8")
    provider = SimpleNamespace(name="mock")
    events = []

    monkeypatch.setattr(app_mod, "get_provider", lambda name: provider)
    monkeypatch.setattr(
        app_mod,
        "incremental_update",
        lambda root, actual_provider, echo: events.append(
            ("update", root, actual_provider)
        ) or SimpleNamespace(changed=[]),
    )
    monkeypatch.setattr(
        app_mod,
        "ensure_judgment",
        lambda root, actual_provider, echo: events.append(
            ("judgment", root, actual_provider)
        ),
    )

    class _Thread:
        def __init__(self, *, target, args, kwargs, daemon, name):
            events.append(("thread", target, args, kwargs, daemon, name))

        def start(self):
            events.append(("watcher-start",))

    monkeypatch.setattr(app_mod.threading, "Thread", _Thread)
    monkeypatch.setattr(
        app_mod,
        "serve",
        lambda root, *, port, open_browser, open_path: events.append(
            ("serve", root, port, open_browser, open_path)
        ),
    )

    app_mod.run_app(
        tmp_path,
        port=7788,
        provider_name="mock",
        interval=0.25,
        open_browser=False,
        echo=lambda *args: None,
    )

    assert events[0] == ("update", tmp_path.resolve(), provider)
    assert events[1] == ("judgment", tmp_path.resolve(), provider)
    assert events[2] == (
        "thread",
        app_mod.watch,
        (tmp_path.resolve(), provider),
        {"interval": 0.25, "echo": ANY},
        True,
        "cms-watch",
    )
    assert events[3] == ("watcher-start",)
    assert events[4] == ("serve", tmp_path.resolve(), 7788, False, "/")
