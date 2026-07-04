"""MCP server — expose the memory layer as native tools for AI agents.

Speaks Model Context Protocol over stdio (newline-delimited JSON-RPC 2.0,
stdlib only). Register with e.g.:

    claude mcp add cms -- cms mcp --root /path/to/project

Agents then get query_codebase / get_feature_trace / who_calls / get_impact /
get_source etc. as first-class tools — memory consulted before grep.
"""

from __future__ import annotations

import json
import sys
from dataclasses import asdict
from pathlib import Path

from . import config
from .activity import log_activity
from .config import LANGUAGE_BY_EXTENSION
from .impact import analyze_impact
from .memory import CodebaseMemory

PROTOCOL_VERSION = "2024-11-05"

TOOLS = [
    {
        "name": "query_codebase",
        "description": "Search the codebase memory by intent (natural language). Returns ranked files/functions/classes/features with paths, line ranges, summaries and call connections. Use this BEFORE grepping or reading files.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "What you're looking for, e.g. 'where is retry logic for API calls?'"},
                "top_k": {"type": "integer", "default": 5},
            },
            "required": ["query"],
        },
    },
    {
        "name": "get_file_summary",
        "description": "Get the AI summary, components (functions/classes with line ranges), anchors and git stats for one file.",
        "inputSchema": {
            "type": "object",
            "properties": {"path": {"type": "string", "description": "Relative path, e.g. src/auth.py"}},
            "required": ["path"],
        },
    },
    {
        "name": "list_features",
        "description": "List all traced features (named capabilities) with member counts and connections.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "get_feature_trace",
        "description": "Full trace of one feature: members, entry points, call flows with file:line steps, narrative with verification checklist, and tests that verify it.",
        "inputSchema": {
            "type": "object",
            "properties": {"name": {"type": "string"}},
            "required": ["name"],
        },
    },
    {
        "name": "who_calls",
        "description": "List callers of a function (by bare name or path::qualname).",
        "inputSchema": {
            "type": "object",
            "properties": {"function": {"type": "string"}},
            "required": ["function"],
        },
    },
    {
        "name": "who_imports",
        "description": "List files that import the given file.",
        "inputSchema": {
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
        },
    },
    {
        "name": "get_impact",
        "description": "Blast radius of changing a target (file, function, or path::qualname): affected functions, files, features and tests, computed by reverse graph traversal.",
        "inputSchema": {
            "type": "object",
            "properties": {"target": {"type": "string"}},
            "required": ["target"],
        },
    },
    {
        "name": "get_review",
        "description": "The AI alignment review: per-feature verdicts (aligned/partial/drift/unverified) on whether the built code matches declared intent, with expected-vs-built explanations and gaps, plus the app-level rollup.",
        "inputSchema": {
            "type": "object",
            "properties": {"feature": {"type": "string", "description": "Optional: one feature's review; omit for all + overall."}},
        },
    },
    {
        "name": "get_source",
        "description": "Read an exact source snippet by path and line range (surgical read — prefer this over whole files).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "start_line": {"type": "integer"},
                "end_line": {"type": "integer"},
            },
            "required": ["path"],
        },
    },
]


# @memory:feature:AgentMemoryAccess
# @memory:connects:QueryEngine, FeatureTracing, ImpactAnalysis, ActivityPulse
# @memory:summary:MCP server (stdio JSON-RPC) exposing the memory as native agent tools — query, summaries, feature traces, impact, surgical source reads.
class MCPServer:
    def __init__(self, root: Path) -> None:
        self.root = root.resolve()
        self.graph_path = self.root / config.MEMORY_DIR_NAME / "graph.json"
        self._memory: CodebaseMemory | None = None
        self._mtime = 0.0

    def memory(self) -> CodebaseMemory:
        mtime = self.graph_path.stat().st_mtime
        if self._memory is None or mtime != self._mtime:
            self._memory = CodebaseMemory.load(self.graph_path)
            self._mtime = mtime
        return self._memory

    # ── tool implementations ────────────────────────────────────────────

    def query_codebase(self, query: str, top_k: int = 5) -> list[dict]:
        return [asdict(r) for r in self.memory().query_intent(query, top_k=top_k)]

    def get_file_summary(self, path: str) -> dict:
        graph = self.memory().graph
        node_id = f"file:{path}"
        if not graph.has_node(node_id):
            return {"error": f"unknown file {path!r}"}
        attrs = graph.nodes[node_id]
        components = [
            {
                "kind": c["type"], "name": c.get("qualname", c["name"]),
                "lines": f"{c.get('start_line')}-{c.get('end_line')}",
                "summary": c.get("summary", ""),
            }
            for _, child, d in graph.out_edges(node_id, data=True)
            if d.get("type") == "CONTAINS"
            for c in [graph.nodes[child]]
        ]
        return {
            "path": path,
            "language": attrs.get("language"),
            "line_count": attrs.get("line_count"),
            "summary": attrs.get("summary", ""),
            "anchors": attrs.get("anchors") or {},
            "git": attrs.get("git") or {},
            "components": sorted(components, key=lambda c: c["lines"]),
        }

    def list_features(self) -> list[dict]:
        from .features import get_features

        return [
            {
                "name": f["name"], "source": f.get("source"),
                "description": f.get("description", ""),
                "members": len(f.get("members", [])),
                "connects": f.get("connects", []),
                "verified_by": len(f.get("verified_by", [])),
            }
            for f in get_features(self.memory().graph)
        ]

    def get_feature_trace(self, name: str) -> dict:
        from .features import get_features

        for f in get_features(self.memory().graph):
            if f["name"].lower() == name.lower():
                return {
                    "name": f["name"],
                    "description": f.get("description", ""),
                    "members": f.get("members", []),
                    "entry_points": f.get("entry_points", []),
                    "flows": f.get("flows", []),
                    "connects": f.get("connects", []),
                    "narrative": f.get("summary", ""),
                    "verified_by": f.get("verified_by", []),
                }
        return {"error": f"unknown feature {name!r}"}

    def who_calls(self, function: str) -> list[str]:
        return self.memory().who_calls(function)

    def who_imports(self, path: str) -> list[str]:
        return self.memory().who_imports(path)

    def get_impact(self, target: str) -> dict:
        impact = analyze_impact(self.memory().graph, target)
        if impact is None:
            return {"error": f"could not resolve {target!r}"}
        return asdict(impact)

    def get_review(self, feature: str | None = None) -> dict:
        from .features import get_features

        graph = self.memory().graph
        feats = get_features(graph)
        if feature:
            for f in feats:
                if f["name"].lower() == feature.lower():
                    return f.get("review") or {"error": "no review yet — run `cms review`"}
            return {"error": f"unknown feature {feature!r}"}
        app = dict(graph.nodes["review:app"]) if graph.has_node("review:app") else None
        return {
            "app": app,
            "features": {f["name"]: f["review"] for f in feats if f.get("review")},
        }

    def get_source(self, path: str, start_line: int = 1, end_line: int | None = None) -> dict:
        target = (self.root / path).resolve()
        if self.root not in target.parents and target != self.root:
            return {"error": "path outside project root"}
        if target.suffix.lower() not in LANGUAGE_BY_EXTENSION or not target.is_file():
            return {"error": "not a scanned source file"}
        lines = target.read_text(encoding="utf-8", errors="replace").splitlines()
        end = min(end_line or len(lines), len(lines))
        start = max(1, start_line)
        body = "\n".join(f"{i}\t{lines[i - 1]}" for i in range(start, end + 1))
        return {"path": path, "lines": f"{start}-{end}", "source": body}

    # ── JSON-RPC plumbing ───────────────────────────────────────────────

    def handle(self, message: dict) -> dict | None:
        method = message.get("method", "")
        msg_id = message.get("id")
        if method == "initialize":
            return self._result(msg_id, {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "cms", "version": "0.1.0"},
            })
        if method in ("notifications/initialized", "notifications/cancelled"):
            return None
        if method == "ping":
            return self._result(msg_id, {})
        if method == "tools/list":
            return self._result(msg_id, {"tools": TOOLS})
        if method == "tools/call":
            params = message.get("params", {})
            name = params.get("name", "")
            args = params.get("arguments", {}) or {}
            fn = getattr(self, name, None)
            if not callable(fn) or name not in {t["name"] for t in TOOLS}:
                return self._error(msg_id, -32602, f"unknown tool {name!r}")
            try:
                payload = fn(**args)
            except Exception as exc:
                return self._result(msg_id, {
                    "content": [{"type": "text", "text": f"error: {type(exc).__name__}: {exc}"}],
                    "isError": True,
                })
            log_activity(
                self.root / config.MEMORY_DIR_NAME, name,
                self._touched_nodes(name, args, payload),
                label=str(args.get("query") or args.get("name") or args.get("target")
                          or args.get("path") or args.get("function") or ""),
            )
            return self._result(msg_id, {
                "content": [{"type": "text", "text": json.dumps(payload, indent=2)}]
            })
        if msg_id is not None:
            return self._error(msg_id, -32601, f"method {method!r} not found")
        return None

    def _touched_nodes(self, tool: str, args: dict, payload) -> list[str]:
        """Node ids a tool call read — feeds the UI's live activity pulses."""
        nodes: list[str] = []
        try:
            if tool == "query_codebase" and isinstance(payload, list):
                nodes = [r["node_id"] for r in payload]
            elif tool in ("get_file_summary", "get_source") and "path" in args:
                nodes = [f"file:{args['path']}"]
            elif tool == "who_imports" and "path" in args:
                nodes = [f"file:{args['path']}", *payload]
            elif tool == "who_calls" and isinstance(payload, list):
                nodes = list(payload)
            elif tool == "get_feature_trace" and isinstance(payload, dict):
                nodes = [f"feature:{payload.get('name', '')}", *payload.get("members", [])]
            elif tool == "list_features" and isinstance(payload, list):
                nodes = [f"feature:{f['name']}" for f in payload]
            elif tool == "get_review" and isinstance(payload, dict):
                nodes = [f"feature:{n}" for n in (payload.get("features") or {})] or \
                        ([f"feature:{args['feature']}"] if "feature" in args else [])
            elif tool == "get_impact" and isinstance(payload, dict):
                nodes = [payload.get("target", "")]
                nodes += [f"file:{p}" for p in payload.get("files", [])]
                nodes += [f"file:{f.split('::')[0]}" for f in payload.get("functions", [])]
        except (KeyError, TypeError):
            pass
        return [n for n in dict.fromkeys(nodes) if n]

    @staticmethod
    def _result(msg_id, result: dict) -> dict:
        return {"jsonrpc": "2.0", "id": msg_id, "result": result}

    @staticmethod
    def _error(msg_id, code: int, text: str) -> dict:
        return {"jsonrpc": "2.0", "id": msg_id, "error": {"code": code, "message": text}}

    def serve(self) -> None:
        for line in sys.stdin:
            line = line.strip()
            if not line:
                continue
            try:
                message = json.loads(line)
            except json.JSONDecodeError:
                continue
            response = self.handle(message)
            if response is not None:
                sys.stdout.write(json.dumps(response) + "\n")
                sys.stdout.flush()
