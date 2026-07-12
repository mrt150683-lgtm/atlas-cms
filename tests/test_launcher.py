from pathlib import Path


def test_windows_launcher_probes_runtime_health_and_offers_recovery() -> None:
    launcher = (Path(__file__).parent.parent / "CMS.bat").read_text(encoding="utf-8")
    assert '-c "import cms.cli"' in launcher
    assert "if defined CMS_PYTHON" in launcher
    assert 'call :probe "%VENV_PY%"' in launcher
    assert 'call :probe "py" "-3.11"' in launcher
    assert 'call :probe "python"' in launcher
    assert "existing .venv Python is present but cannot import Atlas" in launcher
    assert "pip install -e .[dev,anthropic]" in launcher


def test_windows_launcher_never_uses_venv_on_existence_alone() -> None:
    launcher = (Path(__file__).parent.parent / "CMS.bat").read_text(encoding="utf-8")
    assert 'set "CMS_PY=%~dp0.venv\\Scripts\\python.exe"' not in launcher
    assert launcher.index('call :probe "%VENV_PY%"') < launcher.index(
        '"%CMS_PY%" %CMS_PY_ARGS% -m cms.cli'
    )
