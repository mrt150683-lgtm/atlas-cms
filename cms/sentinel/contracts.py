"""Sentinel Contract Checker — mismatches between the layers that must agree.

CMS has no ORM or REST schema files; its real contracts are:
  1. UI pages (ui_assets/*.html) fetch ``/api/...`` paths that cms/ui.py must
     handle — and handled routes should be reachable from some page.
  2. The MCP tool list (cms/mcp.py TOOLS) must match actual MCPServer methods,
     and each tool's required schema args must exist in the method signature.
  3. README-documented ``cms <command>``s must exist in the typer CLI (dead
     docs) and CLI commands should be documented (invisible features).

Each check compares two live sources of truth; nothing is asserted from a
hardcoded list.
"""

from __future__ import annotations

import inspect
import re
from pathlib import Path

from . import make_finding
from .inventory import http_routes, ui_api_calls


def _check_ui_api(root: Path) -> list[dict]:
    findings = []
    routes = http_routes(root)
    exact = {r for r in routes if not r.endswith("*")}
    prefixes = {r[:-1] for r in routes if r.endswith("*")}

    def handled(path: str) -> bool:
        return path in exact or any(path.startswith(p) for p in prefixes)

    called_anywhere: set[str] = set()
    for page, calls in ui_api_calls(root).items():
        for call in calls:
            called_anywhere.add(call)
            if not handled(call):
                findings.append(make_finding(
                    "contracts", "high",
                    f"{page} fetches {call} but cms/ui.py has no handler for it",
                    area="ui_api_contract", file=f"cms/ui_assets/{page}",
                    pattern="unhandled-endpoint", evidence=[f"handled routes: {', '.join(routes)}"],
                    risk="That part of the UI is a dead button — the request 404s.",
                    recommendation="Add the route to the handler in cms/ui.py or fix the fetch path.",
                ))
    for route in sorted(exact):
        if route.startswith("/api/") and route not in called_anywhere:
            findings.append(make_finding(
                "contracts", "info",
                f"route {route} is handled by cms/ui.py but no UI page calls it",
                area="ui_api_contract", file="cms/ui.py", pattern="uncalled-endpoint",
                risk="Possibly dead server code, or an API used only by external clients.",
                recommendation="Wire it into a page, document it, or remove it.",
            ))
    return findings


def _check_mcp() -> list[dict]:
    findings = []
    try:
        from ..mcp import MCPServer, TOOLS
    except Exception as exc:
        return [make_finding(
            "contracts", "high", f"cms.mcp failed to import: {exc}",
            area="mcp_contract", file="cms/mcp.py", pattern="import-error",
            risk="The MCP server cannot start; agents lose memory access.",
            recommendation="Fix the import error.",
        )]
    for tool in TOOLS:
        name = tool["name"]
        fn = getattr(MCPServer, name, None)
        if not callable(fn):
            findings.append(make_finding(
                "contracts", "high",
                f"MCP tool {name} is advertised but MCPServer has no such method",
                area="mcp_contract", file="cms/mcp.py", pattern="missing-tool-method",
                risk="Agents calling the advertised tool get an unknown-tool error.",
                recommendation=f"Implement MCPServer.{name} or drop it from TOOLS.",
            ))
            continue
        params = set(inspect.signature(fn).parameters) - {"self"}
        schema_props = set((tool.get("inputSchema") or {}).get("properties") or {})
        unknown = schema_props - params
        if unknown:
            findings.append(make_finding(
                "contracts", "high",
                f"MCP tool {name} schema declares argument(s) {sorted(unknown)} the method does not accept",
                area="mcp_contract", file="cms/mcp.py", pattern="schema-arg-mismatch",
                evidence=[f"method params: {sorted(params)}"],
                risk="A well-formed agent call crashes with a TypeError.",
                recommendation="Align the inputSchema with the method signature.",
            ))
        optional_missing = {
            p for p, sig in inspect.signature(fn).parameters.items()
            if p != "self" and sig.default is inspect.Parameter.empty and p not in schema_props
        }
        if optional_missing:
            findings.append(make_finding(
                "contracts", "medium",
                f"MCP tool {name} method requires {sorted(optional_missing)} but the schema does not offer it",
                area="mcp_contract", file="cms/mcp.py", pattern="schema-missing-required",
                risk="Every call to this tool fails — the agent cannot supply the argument.",
                recommendation="Add the parameter to the inputSchema (and to required).",
            ))
    return findings


def _cli_command_names() -> set[str]:
    try:
        from ..cli import app
    except Exception:
        return set()
    names = set()
    for cmd in getattr(app, "registered_commands", []):
        names.add(cmd.name or cmd.callback.__name__.replace("_", "-"))
    for group in getattr(app, "registered_groups", []):
        if group.name:
            names.add(group.name)
    return names


def _check_cli_docs(root: Path) -> list[dict]:
    findings = []
    real = _cli_command_names()
    if not real:
        return findings
    documented: set[str] = set()
    for doc in (root / "README.md", root / "docs" / "HERMES_SENTINEL.md"):
        if doc.is_file():
            text = doc.read_text(encoding="utf-8", errors="replace")
            # `cms <command>` in shell examples; not `from cms import …` / `import cms`
            documented |= set(re.findall(r"(?<!from )(?<!import )\bcms\s+([a-z][a-z-]+)", text))
            documented -= {"import"}
    for name in sorted(documented - real):
        findings.append(make_finding(
            "contracts", "medium",
            f"README documents `cms {name}` but the CLI has no such command",
            area="cli_docs_contract", file="README.md", pattern="documented-missing-command",
            risk="Users follow the docs into an error.",
            recommendation="Implement the command or fix the docs.",
        ))
    for name in sorted(real - documented):
        findings.append(make_finding(
            "contracts", "info",
            f"CLI command `cms {name}` is not mentioned in README.md",
            area="cli_docs_contract", file="README.md", pattern="undocumented-command",
            recommendation="Add a line to the command reference.",
        ))
    return findings


def _check_mcp_tool_docs(root: Path) -> list[dict]:
    """The agent-facing docs must advertise the live tool surface, not a stale one.

    README.md and SKILL.md each have to mention every tool in cms/mcp.py TOOLS,
    and SKILL's '## … MCP tools (N)' headline count must equal len(TOOLS)."""
    findings = []
    try:
        from ..mcp import TOOLS
    except Exception:
        return findings  # _check_mcp already reports the import failure
    live = {t["name"] for t in TOOLS}
    for doc_name in ("README.md", "SKILL.md"):
        doc = root / doc_name
        if not doc.is_file():
            continue
        text = doc.read_text(encoding="utf-8", errors="replace")
        missing = sorted(n for n in live if n not in text)
        if missing:
            findings.append(make_finding(
                "contracts", "medium",
                f"{doc_name} does not mention MCP tool(s) {missing} that the server exposes",
                area="mcp_docs_contract", file=doc_name, pattern="undocumented-mcp-tool",
                evidence=[f"live tools: {len(live)}"],
                risk="Agents and humans reading the docs get a stale tool surface — "
                     "the exact drift Atlas exists to prevent.",
                recommendation="Regenerate the tool list from cms.mcp.TOOLS.",
            ))
        for claimed in re.findall(r"MCP tools \((\d+)\)", text):
            if int(claimed) != len(live):
                findings.append(make_finding(
                    "contracts", "medium",
                    f"{doc_name} claims {claimed} MCP tools but the server exposes {len(live)}",
                    area="mcp_docs_contract", file=doc_name, pattern="stale-mcp-tool-count",
                    risk="A wrong headline count makes every other doc claim suspect.",
                    recommendation="Update the count (or generate it) from len(cms.mcp.TOOLS).",
                ))
    return findings


# @memory:feature:HermesSentinel
# @memory:connects:MemoryViewer, AgentMemoryAccess
# @memory:summary:Contract checker — UI fetches vs HTTP routes, MCP tool schemas vs server methods, README command docs vs live typer CLI, and README/SKILL MCP tool lists vs the live TOOLS surface; every side read from the real code.
def check_contracts(root: Path) -> list[dict]:
    return (_check_ui_api(root) + _check_mcp() + _check_cli_docs(root)
            + _check_mcp_tool_docs(root))
