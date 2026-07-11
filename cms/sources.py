"""Sources & exclusions review — what Atlas analyses, what it skips, and why.

Powers the Setup screen's transparency panel: the project's own ``.gitignore``
(displayed, not guessed), a breakdown of which layer excludes what, and
*accurate* recommendations. Every recommendation is grounded in a concrete file
fact (extension ratios, byte sizes, directory names) with the evidence attached
— we never assume; we surface a signal and let the user decide.
"""

from __future__ import annotations

import os
from collections import defaultdict
from pathlib import Path

import pathspec

from .config import CMSIGNORE_FILENAME, DEFAULT_IGNORES, LANGUAGE_BY_EXTENSION

# directories we never walk (huge / never source) — counted, not enumerated
_HARD_PRUNE = {"node_modules", ".git", ".hg", ".svn", "venv", ".venv", "env",
               "ENV", "__pycache__", ".tox", ".mypy_cache", ".pytest_cache"}
# directory names that conventionally hold generated / vendored code
_GENERATED_NAMES = {"generated", "__generated__", "vendor", "third_party", "third-party"}
_LARGE_DATA_BYTES = 200_000
_DATA_EXTS = {"json", "txt", "csv", "tsv", "xml"}


def _mkspec(lines: list[str]) -> pathspec.PathSpec:
    if hasattr(pathspec, "GitIgnoreSpec"):
        return pathspec.GitIgnoreSpec.from_lines(lines)
    return pathspec.PathSpec.from_lines("gitwildmatch", lines)


def _read_lines(path: Path) -> list[str]:
    if not path.is_file():
        return []
    return [ln for ln in path.read_text(encoding="utf-8", errors="ignore").splitlines()]


def _recommend(root: Path, included: list[str]) -> list[dict]:
    """High-confidence exclusion suggestions, each with its evidence."""
    recs: list[dict] = []

    by_dir: dict[str, list[str]] = defaultdict(list)
    for p in included:
        by_dir[p.rsplit("/", 1)[0] + "/" if "/" in p else ""].append(p)

    # 1) .d.ts-heavy directories — TypeScript declaration files are compiler output
    for d, files in sorted(by_dir.items()):
        dts = [f for f in files if f.endswith(".d.ts")]
        if len(dts) >= 2 and len(dts) / len(files) >= 0.6:
            recs.append({
                "pattern": d or "*.d.ts",
                "kind": "declarations",
                "reason": f"{len(dts)} of {len(files)} files here are .d.ts TypeScript declaration files (compiler output).",
                "count": len(dts),
            })

    # 2) generated/vendored directory names
    seen: set[str] = set()
    for p in included:
        parts = p.split("/")
        for i, seg in enumerate(parts[:-1]):
            if seg.lower() in _GENERATED_NAMES:
                pat = "/".join(parts[: i + 1]) + "/"
                if pat not in seen:
                    seen.add(pat)
                    recs.append({
                        "pattern": pat,
                        "kind": "generated-dir",
                        "reason": f"directory name '{seg}' conventionally holds generated or vendored code.",
                        "count": sum(1 for q in included if q.startswith(pat)),
                    })

    # 3) large data files — expensive to summarise, almost always generated
    for p in included:
        if p.rsplit(".", 1)[-1].lower() in _DATA_EXTS:
            try:
                size = (root / p).stat().st_size
            except OSError:
                continue
            if size >= _LARGE_DATA_BYTES:
                recs.append({
                    "pattern": p,
                    "kind": "large-data",
                    "reason": f"large data file ({size // 1024} KB) — summarising it wastes budget and adds little.",
                    "count": 1,
                })

    return recs


def analyze_sources(root: Path) -> dict:
    """Return the sources/exclusions review for ``root``."""
    root = Path(root).resolve()
    git_lines = _read_lines(root / ".gitignore")
    cms_lines = _read_lines(root / CMSIGNORE_FILENAME)

    default_spec = _mkspec(list(DEFAULT_IGNORES))
    git_spec = _mkspec(list(DEFAULT_IGNORES) + git_lines)
    full_spec = _mkspec(list(DEFAULT_IGNORES) + git_lines + cms_lines)

    included: list[str] = []
    excluded_by_gitignore: list[str] = []
    excluded_by_cmsignore: list[str] = []
    excluded_by_default = 0
    pruned_dirs: list[str] = []

    for dirpath, dirnames, filenames in os.walk(root):
        rel_dir = Path(dirpath).relative_to(root).as_posix()
        rel_dir = "" if rel_dir == "." else rel_dir
        prefix = "" if rel_dir == "" else rel_dir + "/"
        keep = []
        for d in sorted(dirnames):
            if d in _HARD_PRUNE:
                pruned_dirs.append(f"{prefix}{d}/")
                continue
            keep.append(d)
        dirnames[:] = keep
        for name in sorted(filenames):
            if Path(name).suffix.lower() not in LANGUAGE_BY_EXTENSION:
                continue
            rel = f"{prefix}{name}"
            if not full_spec.match_file(rel):
                included.append(rel)
            elif default_spec.match_file(rel):
                excluded_by_default += 1
            elif git_spec.match_file(rel):
                excluded_by_gitignore.append(rel)
            else:
                excluded_by_cmsignore.append(rel)

    return {
        "root": str(root),
        "included_count": len(included),
        "gitignore": {
            "present": bool(git_lines or (root / ".gitignore").is_file()),
            "lines": [ln for ln in git_lines if ln.strip() and not ln.lstrip().startswith("#")],
            "raw": "\n".join(git_lines),
            "excluded": sorted(excluded_by_gitignore)[:200],
            "excluded_count": len(excluded_by_gitignore),
        },
        "cmsignore": {
            "present": bool(cms_lines),
            "lines": [ln for ln in cms_lines if ln.strip() and not ln.lstrip().startswith("#")],
            "excluded": sorted(excluded_by_cmsignore)[:200],
            "excluded_count": len(excluded_by_cmsignore),
        },
        "defaults": {
            "excluded_count": excluded_by_default,
            "pruned_dirs": sorted(set(pruned_dirs))[:50],
            "summary": "node_modules, VCS, virtualenvs, build output (dist/, dist-*/), "
                       "dependency lockfiles, caches, and IDE/OS junk",
        },
        "recommendations": _recommend(root, included),
    }


def add_ignore_pattern(root: Path, pattern: str) -> bool:
    """Append a pattern to the project's .cmsignore (idempotent)."""
    pattern = pattern.strip()
    if not pattern:
        return False
    path = Path(root) / CMSIGNORE_FILENAME
    if pattern in [ln.strip() for ln in _read_lines(path)]:
        return True
    text = path.read_text(encoding="utf-8") if path.is_file() else ""
    if text and not text.endswith("\n"):
        text += "\n"
    path.write_text(text + pattern + "\n", encoding="utf-8")
    return True
