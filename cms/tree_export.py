"""Phase 1: render the clean tree as clean_tree.md (human/AI) and clean_tree.json (machine)."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from .scanner import FileRecord


def _nest(records: list[FileRecord]) -> dict:
    """Build {"dirs": {name: subtree}, "files": [FileRecord]} from flat records."""
    tree: dict = {"dirs": {}, "files": []}
    for rec in records:
        parts = rec.rel_path.split("/")
        node = tree
        for part in parts[:-1]:
            node = node["dirs"].setdefault(part, {"dirs": {}, "files": []})
        node["files"].append(rec)
    return tree


def _fmt_size(size: int) -> str:
    if size < 1024:
        return f"{size} B"
    if size < 1024 * 1024:
        return f"{size / 1024:.1f} KB"
    return f"{size / (1024 * 1024):.1f} MB"


def _render_lines(node: dict, indent: str) -> list[str]:
    lines: list[str] = []
    dirs = sorted(node["dirs"].items())
    files = sorted(node["files"], key=lambda r: r.rel_path)
    entries: list[tuple[str, object]] = [(name, sub) for name, sub in dirs]
    entries += [(f.rel_path.rsplit("/", 1)[-1], f) for f in files]
    for i, (name, item) in enumerate(entries):
        last = i == len(entries) - 1
        branch = "└── " if last else "├── "
        cont = "    " if last else "│   "
        if isinstance(item, dict):
            lines.append(f"{indent}{branch}{name}/")
            lines.extend(_render_lines(item, indent + cont))
        else:
            rec: FileRecord = item  # type: ignore[assignment]
            meta = f"({rec.line_count} lines · {_fmt_size(rec.size_bytes)} · {rec.language})"
            lines.append(f"{indent}{branch}{name}  {meta}")
    return lines


def render_tree_md(root: Path, records: list[FileRecord]) -> str:
    tree = _nest(records)
    total_lines = sum(r.line_count for r in records)
    generated = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    body = "\n".join(_render_lines(tree, ""))
    return (
        f"# Clean Directory Tree — {root.name}\n\n"
        f"Generated: {generated}  \n"
        f"Files: {len(records)} · Total lines: {total_lines}\n\n"
        f"```text\n{root.name}/\n{body}\n```\n"
    )


def tree_json(root: Path, records: list[FileRecord]) -> dict:
    def serialise(node: dict) -> dict:
        return {
            "dirs": {name: serialise(sub) for name, sub in sorted(node["dirs"].items())},
            "files": [r.to_dict() for r in node["files"]],
        }

    return {
        "root": str(root),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "file_count": len(records),
        "total_lines": sum(r.line_count for r in records),
        "files": [r.to_dict() for r in records],
        "tree": serialise(_nest(records)),
    }


def export_tree(root: Path, records: list[FileRecord], memory_dir: Path) -> None:
    memory_dir.mkdir(parents=True, exist_ok=True)
    (memory_dir / "clean_tree.md").write_text(render_tree_md(root, records), encoding="utf-8")
    (memory_dir / "clean_tree.json").write_text(
        json.dumps(tree_json(root, records), indent=2), encoding="utf-8"
    )
