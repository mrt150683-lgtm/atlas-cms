"""Scope + portable bundle tests."""

import json
import zipfile
from pathlib import Path

import pytest

from cms.bundle import export_bundle, open_bundle, read_manifest
from cms.scanner import scan
from cms.scope import clear_scope, dir_in_scope, file_in_scope, load_scope, save_scope


def _project(tmp_path: Path) -> Path:
    (tmp_path / "cms").mkdir()
    (tmp_path / "tests").mkdir()
    (tmp_path / "docs").mkdir()
    (tmp_path / "cms" / "a.py").write_text("def a(): pass\n", encoding="utf-8")
    (tmp_path / "tests" / "test_a.py").write_text("def test_a(): pass\n", encoding="utf-8")
    (tmp_path / "docs" / "guide.md").write_text("# guide\n", encoding="utf-8")
    return tmp_path


def test_no_scope_scans_everything(tmp_path):
    root = _project(tmp_path)
    assert {r.rel_path for r in scan(root)} == {"cms/a.py", "tests/test_a.py", "docs/guide.md"}
    assert load_scope(root) is None


def test_scope_limits_scan(tmp_path):
    root = _project(tmp_path)
    save_scope(root, ["cms/", "docs/guide.md"])
    assert load_scope(root) == {"cms/", "docs/guide.md"}
    scanned = {r.rel_path for r in scan(root)}
    assert scanned == {"cms/a.py", "docs/guide.md"}
    assert "tests/test_a.py" not in scanned


def test_scope_predicates():
    scope = {"cms/", "docs/guide.md"}
    assert file_in_scope("cms/a.py", scope) and file_in_scope("docs/guide.md", scope)
    assert not file_in_scope("tests/test_a.py", scope)
    assert dir_in_scope("cms/", scope) and dir_in_scope("docs/", scope)  # docs holds a selected file
    assert not dir_in_scope("tests/", scope)
    assert file_in_scope("anything", None)  # None => all


def test_scope_clear(tmp_path):
    root = _project(tmp_path)
    save_scope(root, ["cms/"])
    assert clear_scope(root) is True
    assert load_scope(root) is None
    assert clear_scope(root) is False


def _seed_memory(root: Path) -> None:
    mem = root / ".memory"
    mem.mkdir(exist_ok=True)
    (mem / "graph.json").write_text('{"nodes": []}', encoding="utf-8")
    (mem / "summaries").mkdir(exist_ok=True)
    (mem / "summaries" / "a.md").write_text("summary", encoding="utf-8")


def test_bundle_requires_memory(tmp_path):
    root = _project(tmp_path)
    with pytest.raises(FileNotFoundError):
        export_bundle(root, out_path=tmp_path / "x.cmsbundle")


def test_bundle_memory_only(tmp_path):
    root = _project(tmp_path)
    _seed_memory(root)
    out = export_bundle(root, out_path=tmp_path / "share.cmsbundle", include_source=False)
    man = read_manifest(out)
    assert man["name"] == root.name and man["has_source"] is False and man["memory_file_count"] == 2
    dest = open_bundle(out, tmp_path / "recv")
    assert (dest / ".memory" / "graph.json").is_file()
    assert not (dest / "cms").exists()  # no source in this bundle


def test_bundle_with_scoped_source_roundtrip(tmp_path):
    root = _project(tmp_path)
    _seed_memory(root)
    save_scope(root, ["cms/", "docs/guide.md"])
    out = export_bundle(root, out_path=tmp_path / "share.cmsbundle", include_source=True)
    man = read_manifest(out)
    assert man["has_source"] and man["source_file_count"] == 2 and man["scope"] == ["cms/", "docs/guide.md"]
    dest = open_bundle(out, tmp_path / "recv")
    assert (dest / "cms" / "a.py").is_file()       # source restored to real path
    assert not (dest / "tests").exists()           # scoped out of the bundle
    assert (dest / ".cmsscope.json").is_file()


def test_open_bundle_rejects_zip_slip(tmp_path):
    bad = tmp_path / "evil.cmsbundle"
    with zipfile.ZipFile(bad, "w") as z:
        z.writestr("manifest.json", json.dumps({"atlas_bundle": 1, "name": "x"}))
        z.writestr("../../escape.txt", "pwned")
    with pytest.raises(ValueError):
        open_bundle(bad, tmp_path / "recv")
