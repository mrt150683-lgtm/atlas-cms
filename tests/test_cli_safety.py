from pathlib import Path

from typer.testing import CliRunner

from cms.cli import app


def test_update_refuses_windows_system_tree_without_writing_memory(
    tmp_path: Path, monkeypatch
) -> None:
    windows = tmp_path / "Windows"
    system32 = windows / "System32"
    system32.mkdir(parents=True)
    (system32 / "driver.py").write_text("pass\n", encoding="utf-8")
    monkeypatch.setenv("SystemRoot", str(windows))

    result = CliRunner().invoke(
        app, ["update", "--root", str(system32), "--provider", "mock"]
    )

    assert result.exit_code == 2
    assert "Refusing to scan operating-system directory" in result.output
    assert not (system32 / ".memory").exists()
