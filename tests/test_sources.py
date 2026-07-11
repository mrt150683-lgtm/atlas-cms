"""Sources & exclusions review — accurate recommendations, gitignore reflection."""

from pathlib import Path

from cms.sources import add_ignore_pattern, analyze_sources


def test_included_and_defaults(tmp_path: Path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "a.ts").write_text("export const a = 1;\n", encoding="utf-8")
    (tmp_path / "node_modules" / "x").mkdir(parents=True)
    (tmp_path / "node_modules" / "x" / "j.js").write_text("1\n", encoding="utf-8")
    (tmp_path / "package-lock.json").write_text("{}\n", encoding="utf-8")
    r = analyze_sources(tmp_path)
    assert r["included_count"] == 1                          # only src/a.ts
    assert "node_modules/" in r["defaults"]["pruned_dirs"]   # huge dir noted, not walked
    assert r["recommendations"] == []                        # nothing generated slipped in


def test_recommendations_are_grounded(tmp_path: Path):
    # a directory that is mostly .d.ts (compiler output)
    (tmp_path / "types").mkdir()
    for i in range(3):
        (tmp_path / "types" / f"a{i}.d.ts").write_text("export declare const x: number;\n", encoding="utf-8")
    (tmp_path / "types" / "real.ts").write_text("export const y = 1;\n", encoding="utf-8")  # 3/4 => flag
    (tmp_path / "generated").mkdir()
    (tmp_path / "generated" / "g.ts").write_text("export const g = 1;\n", encoding="utf-8")
    (tmp_path / "big.json").write_text("x" * 250_000, encoding="utf-8")
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.ts").write_text("export const a = 1;\n", encoding="utf-8")

    recs = {r["kind"]: r for r in analyze_sources(tmp_path)["recommendations"]}
    assert recs["declarations"]["pattern"] == "types/" and recs["declarations"]["count"] == 3
    assert recs["generated-dir"]["pattern"] == "generated/"
    assert recs["large-data"]["pattern"] == "big.json"
    # every recommendation carries concrete evidence
    assert all(r["reason"] for r in recs.values())


def test_no_false_positive_on_clean_source(tmp_path: Path):
    (tmp_path / "src").mkdir()
    for name in ("app.ts", "util.ts", "view.tsx"):
        (tmp_path / "src" / name).write_text("export const x = 1;\n", encoding="utf-8")
    (tmp_path / "README.md").write_text("# hi\n", encoding="utf-8")
    assert analyze_sources(tmp_path)["recommendations"] == []


def test_gitignore_is_reflected(tmp_path: Path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "keep.ts").write_text("export const k = 1;\n", encoding="utf-8")
    (tmp_path / "gen").mkdir()
    (tmp_path / "gen" / "out.ts").write_text("export const g = 1;\n", encoding="utf-8")
    (tmp_path / ".gitignore").write_text("gen/\n# a comment\n", encoding="utf-8")
    r = analyze_sources(tmp_path)
    assert r["gitignore"]["present"] and "gen/" in r["gitignore"]["lines"]
    assert "# a comment" not in r["gitignore"]["lines"]           # comments filtered from the pattern list
    assert any(p.startswith("gen/") for p in r["gitignore"]["excluded"])
    assert r["included_count"] == 1


def test_add_ignore_pattern_idempotent(tmp_path: Path):
    assert add_ignore_pattern(tmp_path, "types/")
    assert add_ignore_pattern(tmp_path, "types/")  # again — no duplicate
    text = (tmp_path / ".cmsignore").read_text(encoding="utf-8")
    assert text.count("types/") == 1
    # the pattern actually takes effect on a subsequent scan
    (tmp_path / "types").mkdir()
    (tmp_path / "types" / "a.ts").write_text("export const a = 1;\n", encoding="utf-8")
    from cms.scanner import scan
    assert not any(rec.rel_path.startswith("types/") for rec in scan(tmp_path))
