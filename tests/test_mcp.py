import json
from pathlib import Path

from cms.features import build_features
from cms.graph_builder import build_graph
from cms.mcp import MCPServer
from cms.memory import CodebaseMemory
from cms.providers import MockProvider
from cms.scanner import scan

SOURCE = '''\
# @memory:feature:Greeting
def greet(name):
    return helper(name)


def helper(name):
    return name
'''


def _server(tmp_path: Path) -> MCPServer:
    (tmp_path / "app.py").write_text(SOURCE, encoding="utf-8")
    graph = build_graph(scan(tmp_path))
    build_features(graph, MockProvider())
    memory_dir = tmp_path / ".memory"
    memory_dir.mkdir()
    CodebaseMemory(graph).save(memory_dir / "graph.json")
    return MCPServer(tmp_path)


def _call(server: MCPServer, method: str, params: dict | None = None, msg_id: int = 1) -> dict:
    return server.handle({"jsonrpc": "2.0", "id": msg_id, "method": method, "params": params or {}})


def _tool(server: MCPServer, name: str, args: dict) -> dict:
    resp = _call(server, "tools/call", {"name": name, "arguments": args})
    assert "error" not in resp, resp
    return json.loads(resp["result"]["content"][0]["text"])


def test_initialize_and_list_tools(tmp_path: Path) -> None:
    server = _server(tmp_path)
    init = _call(server, "initialize")
    assert init["result"]["serverInfo"]["name"] == "cms"
    tools = _call(server, "tools/list")["result"]["tools"]
    names = {t["name"] for t in tools}
    assert {"query_codebase", "get_feature_trace", "get_impact", "get_source"} <= names


def test_query_and_feature_tools(tmp_path: Path) -> None:
    server = _server(tmp_path)
    hits = _tool(server, "query_codebase", {"query": "greet name", "top_k": 3})
    assert any(h["node_id"] == "func:app.py::greet" for h in hits)

    feats = _tool(server, "list_features", {})
    assert feats[0]["name"] == "Greeting"

    trace = _tool(server, "get_feature_trace", {"name": "greeting"})  # case-insensitive
    assert trace["entry_points"] == ["func:app.py::greet"]
    assert trace["flows"]

    impact = _tool(server, "get_impact", {"target": "helper"})
    assert "app.py::greet" in impact["functions"]

    src = _tool(server, "get_source", {"path": "app.py", "start_line": 2, "end_line": 3})
    assert "def greet" in src["source"]


def test_unknown_tool_and_traversal_guard(tmp_path: Path) -> None:
    server = _server(tmp_path)
    resp = _call(server, "tools/call", {"name": "rm_rf", "arguments": {}})
    assert "error" in resp
    src = _tool(server, "get_source", {"path": "../outside.py"})
    assert "error" in src
