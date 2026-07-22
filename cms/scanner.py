"""Phase 1: clean directory scanner.

Walks a root directory, prunes junk via gitignore-style patterns, keeps only
whitelisted source extensions, and returns FileRecord metadata for each file.
"""

from __future__ import annotations

import os
from dataclasses import asdict, dataclass
from pathlib import Path

import pathspec

from .config import CMSIGNORE_FILENAME, DEFAULT_IGNORES, LANGUAGE_BY_EXTENSION
from .scope import dir_in_scope, file_in_scope, load_scope


class UnsafeRootError(ValueError):
    """Raised before Atlas walks a filesystem or operating-system root."""


def validate_scan_root(root: Path | str) -> Path:
    """Resolve *root* and reject locations that are never codebase roots.

    A mistyped or omitted ``--root`` must not turn ``cms update`` into a walk
    of an entire drive or the Windows installation directory.  Project roots
    inside ordinary user locations remain valid, including non-git projects.
    """
    resolved = Path(root).expanduser().resolve()
    if resolved == Path(resolved.anchor):
        raise UnsafeRootError(
            f"Refusing to scan filesystem root {resolved}. "
            "Choose a project folder and pass it with --root."
        )

    protected: set[Path] = set()
    for env_name in ("SystemRoot", "WINDIR"):
        raw = os.environ.get(env_name)
        if not raw:
            continue
        candidate = Path(raw).expanduser()
        if candidate.is_absolute():
            protected.add(candidate.resolve())

    for system_root in protected:
        if resolved == system_root or system_root in resolved.parents:
            raise UnsafeRootError(
                f"Refusing to scan operating-system directory {resolved}. "
                "Choose a project folder and pass it with --root."
            )
    return resolved


@dataclass
class FileRecord:
    rel_path: str  # posix-style, relative to scan root
    abs_path: str
    size_bytes: int
    line_count: int
    mtime: float
    language: str

    def to_dict(self) -> dict:
        return asdict(self)


def load_ignore_spec(root: Path) -> pathspec.PathSpec:
    """Ignore rules, in increasing precedence: built-in defaults, then the
    project's own ``.gitignore`` (what IT declares as non-source — no guessing
    by us), then ``.cmsignore`` (user overrides, which can re-include with
    ``!pattern``)."""
    lines = list(DEFAULT_IGNORES)
    gitignore = root / ".gitignore"
    if gitignore.is_file():
        lines += gitignore.read_text(encoding="utf-8", errors="ignore").splitlines()
    cmsignore = root / CMSIGNORE_FILENAME
    if cmsignore.is_file():
        lines += cmsignore.read_text(encoding="utf-8", errors="ignore").splitlines()
    if hasattr(pathspec, "GitIgnoreSpec"):
        return pathspec.GitIgnoreSpec.from_lines(lines)
    return pathspec.PathSpec.from_lines("gitwildmatch", lines)


def _count_lines(path: Path) -> int:
    try:
        with open(path, "rb") as f:
            content = f.read()
    except OSError:
        return 0
    if not content:
        return 0
    return content.count(b"\n") + (0 if content.endswith(b"\n") else 1)


# @memory:feature:CleanDirectoryScanner
# @memory:connects:TreeExport, KnowledgeGraphConstruction
# @memory:summary:Single source of truth for what belongs to the codebase — walks the tree, prunes junk dirs in place, whitelists source extensions, records metadata.
def scan(root: Path | str) -> list[FileRecord]:
    root = validate_scan_root(root)
    spec = load_ignore_spec(root)
    scope = load_scope(root)  # None => whole codebase; else only selected dirs/files
    records: list[FileRecord] = []

    for dirpath, dirnames, filenames in os.walk(root):
        dir_rel = Path(dirpath).relative_to(root).as_posix()
        prefix = "" if dir_rel == "." else dir_rel + "/"
        # prune ignored / out-of-scope directories in place so os.walk skips them
        dirnames[:] = sorted(
            d for d in dirnames
            if not spec.match_file(f"{prefix}{d}/")
            and dir_in_scope(f"{prefix}{d}/", scope)
        )
        for name in sorted(filenames):
            rel = f"{prefix}{name}"
            if spec.match_file(rel):
                continue
            if not file_in_scope(rel, scope):
                continue
            ext = Path(name).suffix.lower()
            language = LANGUAGE_BY_EXTENSION.get(ext)
            if language is None:
                continue
            p = Path(dirpath) / name
            try:
                stat = p.stat()
            except OSError:
                continue
            records.append(
                FileRecord(
                    rel_path=rel,
                    abs_path=str(p),
                    size_bytes=stat.st_size,
                    line_count=_count_lines(p),
                    mtime=stat.st_mtime,
                    language=language,
                )
            )
    return records
