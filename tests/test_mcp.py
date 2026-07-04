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
    init = _call(server, "initialize", {"clientInfo": {"name": "Claude Code", "version": "2.1"}})
    assert init["result"]["serverInfo"]["name"] == "cms"
    # the handshake announces the model in the activity feed
    line = (tmp_path / ".memory" / "activity.jsonl").read_text(encoding="utf-8").splitlines()[0]
    event = json.loads(line)
    assert event["tool"] == "connected"
    assert event["label"] == "Claude Code 2.1"
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


def test_rpc_plumbing_edge_cases(tmp_path: Path) -> None:
    server = _server(tmp_path)
    # notifications produce no response at all
    assert server.handle({"jsonrpc": "2.0", "method": "notifications/initialized"}) is None
    # ping answers with an empty result
    assert _call(server, "ping")["result"] == {}
    # unknown method with an id -> JSON-RPC error, not silence
    resp = _call(server, "resources/list")
    assert resp["error"]["code"] == -32601
    # a tool that raises inside is reported as isError content, not a crash
    resp = _call(server, "tools/call", {"name": "get_source", "arguments": {"path": 123}})
    assert resp["result"].get("isError") or "error" in json.loads(resp["result"]["content"][0]["text"])


def test_bad_arguments_surface_as_tool_error(tmp_path: Path) -> None:
    server = _server(tmp_path)
    resp = _call(server, "tools/call", {"name": "query_codebase", "arguments": {"nonsense": True}})
    assert resp["result"]["isError"] is True
    assert "error:" in resp["result"]["content"][0]["text"]


def test_serve_stdio_loop(tmp_path: Path, monkeypatch, capsys) -> None:
    """The actual server loop: newline-delimited JSON-RPC in, responses out,
    garbage and blank lines ignored."""
    import io

    server = _server(tmp_path)
    stdin = io.StringIO(
        json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                    "params": {"clientInfo": {"name": "TestClient"}}}) + "\n"
        + "\n"                       # blank line: skipped
        + "this is not json\n"       # garbage: skipped
        + json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}) + "\n"
        + json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                      "params": {"name": "list_features", "arguments": {}}}) + "\n"
    )
    monkeypatch.setattr("sys.stdin", stdin)
    server.serve()
    lines = [l for l in capsys.readouterr().out.splitlines() if l.strip()]
    assert len(lines) == 2           # initialize + tools/call; nothing for noise
    first, second = (json.loads(l) for l in lines)
    assert first["id"] == 1 and first["result"]["serverInfo"]["name"] == "cms"
    assert second["id"] == 2
    feats = json.loads(second["result"]["content"][0]["text"])
    assert feats[0]["name"] == "Greeting"
    # handshake was announced to the activity feed
    events = (tmp_path / ".memory" / "activity.jsonl").read_text(encoding="utf-8").splitlines()
    assert any('"connected"' in e and "TestClient" in e for e in events)
