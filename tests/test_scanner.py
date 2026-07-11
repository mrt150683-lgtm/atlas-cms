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


def test_ignores_build_output_and_lockfiles(tmp_path: Path) -> None:
    _touch(tmp_path, "src/app.ts", "export const x = 1;\n")
    _touch(tmp_path, "dist-lib/index.d.ts", "export declare const x: number;\n")  # dist-*/
    _touch(tmp_path, "dist-electron/main.js", "console.log(1)\n")                 # dist-*/
    _touch(tmp_path, "package-lock.json", "{}\n")                                  # lockfile
    _touch(tmp_path, "yarn.lock", "# lock\n")                                      # lockfile
    rels = {r.rel_path for r in scan(tmp_path)}
    assert "src/app.ts" in rels
    assert not any(r.startswith("dist-lib/") or r.startswith("dist-electron/") for r in rels)
    assert "package-lock.json" not in rels and "yarn.lock" not in rels


def test_honors_project_gitignore(tmp_path: Path) -> None:
    _touch(tmp_path, "src/keep.ts", "export const k = 1;\n")
    _touch(tmp_path, "generated/out.ts", "export const g = 2;\n")
    _touch(tmp_path, "secret.ts", "export const s = 3;\n")
    (tmp_path / ".gitignore").write_text("generated/\nsecret.ts\n", encoding="utf-8")
    rels = {r.rel_path for r in scan(tmp_path)}
    assert "src/keep.ts" in rels
    assert "generated/out.ts" not in rels   # .gitignore dir honored
    assert "secret.ts" not in rels          # .gitignore file honored


def test_cmsignore_can_reinclude_over_gitignore(tmp_path: Path) -> None:
    _touch(tmp_path, "generated/keep.ts", "export const g = 1;\n")
    (tmp_path / ".gitignore").write_text("generated/\n", encoding="utf-8")
    (tmp_path / ".cmsignore").write_text("!generated/\n", encoding="utf-8")  # user override wins
    rels = {r.rel_path for r in scan(tmp_path)}
    assert "generated/keep.ts" in rels
