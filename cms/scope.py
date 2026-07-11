"""Scope selection — limit which subdirs/files the memory layer processes.

Persisted as ``.cmsscope.json`` at the project root (alongside ``.cmsignore``).
When present, only files under a selected directory (posix prefix ending in
``/``) or matching a selected exact file are scanned/summarised — so a user can
restrict expensive AI processing to the parts of a codebase they care about,
saving API cost and time. Absent or empty ⇒ the whole codebase (default).

``scanner.scan`` loads this automatically, so every pipeline stage honours the
scope without threading a parameter through the codebase.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

SCOPE_FILENAME = ".cmsscope.json"


def scope_path(root: Path) -> Path:
    return Path(root) / SCOPE_FILENAME


def _norm(x: str) -> str:
    return str(x).replace("\\", "/").strip().lstrip("./")


def load_scope(root: Path) -> set[str] | None:
    """Return the include set (dir prefixes end in ``/``), or None for 'all'."""
    p = scope_path(root)
    if not p.is_file():
        return None
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    includes = {_norm(x) for x in (data.get("include") or []) if str(x).strip()}
    includes.discard("")
    return includes or None


def save_scope(root: Path, includes: list[str]) -> Path:
    norm = sorted({_norm(x) for x in includes if str(x).strip()} - {""})
    p = scope_path(root)
    p.write_text(json.dumps(
        {"include": norm, "saved_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")},
        indent=2), encoding="utf-8")
    return p


def clear_scope(root: Path) -> bool:
    p = scope_path(root)
    if p.is_file():
        p.unlink()
        return True
    return False


def file_in_scope(rel: str, scope: set[str] | None) -> bool:
    if not scope:
        return True
    rel = rel.replace("\\", "/")
    if rel in scope:
        return True
    return any(rel.startswith(pref) for pref in scope if pref.endswith("/"))


def build_dir_tree(root: Path) -> dict:
    """Nested tree of source dirs/files under ``root`` (junk pruned via the
    ignore spec, scope NOT applied) for the scope-picker UI. Empty branches are
    dropped; each node carries a recursive source-file ``count``."""
    import os

    from .config import LANGUAGE_BY_EXTENSION
    from .scanner import load_ignore_spec

    root = Path(root).resolve()
    spec = load_ignore_spec(root)
    tree = {"name": root.name, "path": "", "dirs": [], "files": [], "count": 0}
    index = {"": tree}
    for dirpath, dirnames, filenames in os.walk(root):
        rel = Path(dirpath).relative_to(root).as_posix()
        rel = "" if rel == "." else rel
        prefix = "" if rel == "" else rel + "/"
        dirnames[:] = sorted(d for d in dirnames if not spec.match_file(f"{prefix}{d}/"))
        node = index.get(rel)
        if node is None:
            continue
        for d in dirnames:
            drel = f"{prefix}{d}"
            child = {"name": d, "path": drel + "/", "dirs": [], "files": [], "count": 0}
            node["dirs"].append(child)
            index[drel] = child
        for name in sorted(filenames):
            frel = f"{prefix}{name}"
            if spec.match_file(frel):
                continue
            if Path(name).suffix.lower() in LANGUAGE_BY_EXTENSION:
                node["files"].append({"name": name, "path": frel})

    def finish(n: dict) -> int:
        n["dirs"] = [d for d in n["dirs"] if finish(d) > 0]
        n["count"] = len(n["files"]) + sum(d["count"] for d in n["dirs"])
        return n["count"]

    finish(tree)
    return tree


def dir_in_scope(dir_rel: str, scope: set[str] | None) -> bool:
    """``dir_rel`` is posix with a trailing slash. Keep the directory if it
    intersects the scope at all (it is under a selected dir, an ancestor of one,
    or contains a selected file) so os.walk still reaches selected leaves."""
    if not scope:
        return True
    for pref in scope:
        if pref.endswith("/"):
            if dir_rel.startswith(pref) or pref.startswith(dir_rel):
                return True
        elif pref.startswith(dir_rel):  # a selected file living under this dir
            return True
    return False
