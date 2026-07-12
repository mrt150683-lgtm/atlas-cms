import json
from pathlib import Path
from types import SimpleNamespace

from cms.verify import run_coverage


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
