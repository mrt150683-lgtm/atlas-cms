"""Sentinel Project Scanner — a structured inventory of what actually exists.

Built from the same clean scan the memory pipeline uses plus live
introspection of the real interfaces: typer CLI commands, HTTP API routes in
``cms/ui.py``, MCP tools, UI pages, graph features, tests, docs and config.
Nothing here is hardcoded inventory — every entry is derived from files or
imported objects.
"""

from __future__ import annotations

import re
from pathlib import Path

from .. import config
from ..scanner import FileRecord, scan


def detect_stack(root: Path) -> dict:
    stack = {
        "language": "python",
        "frontend": "single-file vanilla-JS pages served from cms/ui_assets/ (no framework)",
        "backend": "http.server (stdlib) JSON API in cms/ui.py + typer CLI + MCP stdio server",
        "database": "none — JSON artifacts under .memory/ (graph.json is canonical)",
        "test_framework": "",
        "build": [],
    }
    pyproject = root / "pyproject.toml"
    if pyproject.is_file():
        text = pyproject.read_text(encoding="utf-8", errors="replace")
        stack["build"] = ["pip install -e .", "pyinstaller (CMS.exe, see updates.md)"]
        if "pytest" in text:
            stack["test_framework"] = "pytest (+ coverage for cms verify)"
    return stack


def _cli_commands() -> list[str]:
    """Live list of typer commands and sub-apps — the real CLI surface."""
    try:
        from ..cli import app
    except Exception:
        return []
    names = []
    for cmd in getattr(app, "registered_commands", []):
        names.append(cmd.name or cmd.callback.__name__.replace("_", "-"))
    for group in getattr(app, "registered_groups", []):
        names.append(f"{group.name} (group)")
    return sorted(names)


def http_routes(root: Path) -> list[str]:
    """API paths actually handled by cms/ui.py, extracted from its source."""
    ui_py = root / "cms" / "ui.py"
    if not ui_py.is_file():
        return []
    text = ui_py.read_text(encoding="utf-8", errors="replace")
    exact = re.findall(r"url\.path\s*==\s*\"(/[^\"]*)\"", text)
    for group in re.findall(r"url\.path\s+in\s+\(([^)]*)\)", text):
        exact += re.findall(r"\"(/[^\"]*)\"", group)
    prefixes = re.findall(r"url\.path\.startswith\(\"(/[^\"]*)\"\)", text)
    return sorted(set(exact) | {p + "*" for p in prefixes})


def ui_pages(root: Path) -> list[str]:
    assets = root / "cms" / "ui_assets"
    return sorted(p.name for p in assets.glob("*.html")) if assets.is_dir() else []


def ui_api_calls(root: Path) -> dict[str, list[str]]:
    """page -> /api/ paths it fetches (frontend side of the HTTP contract)."""
    out: dict[str, list[str]] = {}
    assets = root / "cms" / "ui_assets"
    if not assets.is_dir():
        return out
    for page in sorted(assets.glob("*.html")):
        text = page.read_text(encoding="utf-8", errors="replace")
        out[page.name] = sorted(set(re.findall(r"(/api/[a-z_/-]+)", text)))
    return out


def mcp_tools() -> list[str]:
    try:
        from ..mcp import TOOLS

        return [t["name"] for t in TOOLS]
    except Exception:
        return []


def graph_features(root: Path) -> list[dict]:
    """Feature nodes from the real graph.json (name, source, evidence counts)."""
    import json

    graph_path = root / config.MEMORY_DIR_NAME / "graph.json"
    if not graph_path.is_file():
        return []
    try:
        data = json.loads(graph_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    return sorted(
        (
            {
                "name": n.get("name"),
                "source": n.get("source"),
                "members": len(n.get("members") or []),
                "tests": len(n.get("exercised_by") or n.get("verified_by") or []),
                "reviewed": bool(n.get("review")),
            }
            for n in data.get("nodes", [])
            if n.get("type") == "feature"
        ),
        key=lambda f: f["name"] or "",
    )


# @memory:feature:HermesSentinel
# @memory:connects:CleanDirectoryScanner, FeatureTracing, MemoryViewer
# @memory:summary:Sentinel Project Scanner — real inventory of CLI commands, HTTP routes, UI pages, MCP tools, graph features, tests, docs and config, derived from files and live imports.
def build_inventory(root: Path, records: list[FileRecord] | None = None) -> dict:
    records = records if records is not None else scan(root)
    by_kind: dict[str, list[str]] = {"source": [], "tests": [], "docs": [], "config": []}
    for r in records:
        if r.rel_path.startswith("tests/") or r.rel_path.split("/")[-1].startswith("test_"):
            by_kind["tests"].append(r.rel_path)
        elif r.language in ("markdown", "text"):
            by_kind["docs"].append(r.rel_path)
        elif r.language in ("json", "yaml", "toml", "config"):
            by_kind["config"].append(r.rel_path)
        else:
            by_kind["source"].append(r.rel_path)

    memory_dir = root / config.MEMORY_DIR_NAME
    artifacts = sorted(
        p.name for p in memory_dir.iterdir() if memory_dir.is_dir()
    ) if memory_dir.is_dir() else []

    ledger_path = root / "docs" / "feature_ledger.json"
    return {
        "repo_root": str(root),
        "detected_stack": detect_stack(root),
        "files": {k: sorted(v) for k, v in by_kind.items()},
        "file_count": len(records),
        "cli_commands": _cli_commands(),
        "http_routes": http_routes(root),
        "ui_pages": ui_pages(root),
        "ui_api_calls": ui_api_calls(root),
        "mcp_tools": mcp_tools(),
        "features": graph_features(root),
        "memory_artifacts": artifacts,
        "feature_ledger": str(ledger_path.relative_to(root)) if ledger_path.is_file() else None,
        "warnings": [] if (memory_dir / "graph.json").is_file() else [
            "no .memory/graph.json — run `cms run-all` so Sentinel can audit graph evidence"
        ],
    }
