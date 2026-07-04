from pathlib import Path

from cms.scanner import scan


def _touch(root: Path, rel: str, content: str = "x\n") -> None:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")


def test_scan_filters_junk_and_non_source(tmp_path: Path) -> None:
    _touch(tmp_path, "src/main.py", "print('hi')\nprint('bye')\n")
    _touch(tmp_path, "README.md", "# readme\n")
    _touch(tmp_path, "node_modules/lib/junk.js")
    _touch(tmp_path, "__pycache__/main.cpython-311.pyc")
    _touch(tmp_path, ".git/config")
    _touch(tmp_path, "app.log")
    _touch(tmp_path, "binary.exe")
    _touch(tmp_path, ".venv/pkg/thing.py")
    _touch(tmp_path, ".memory/graph.json")

    records = scan(tmp_path)
    paths = {r.rel_path for r in records}

    assert paths == {"src/main.py", "README.md"}


def test_scan_metadata(tmp_path: Path) -> None:
    _touch(tmp_path, "a.py", "line1\nline2\nline3")
    (rec,) = scan(tmp_path)
    assert rec.language == "python"
    assert rec.line_count == 3
    assert rec.size_bytes == (tmp_path / "a.py").stat().st_size
    assert rec.mtime > 0


def test_cmsignore_extends_defaults(tmp_path: Path) -> None:
    _touch(tmp_path, "keep.py")
    _touch(tmp_path, "secret/hidden.py")
    _touch(tmp_path, ".cmsignore", "secret/\n")

    paths = {r.rel_path for r in scan(tmp_path)}
    assert "keep.py" in paths
    assert "secret/hidden.py" not in paths
