import json
from pathlib import Path

from cms.features import build_features
from cms.graph_builder import build_graph
from cms.mcp import MCPServer, discover_root
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
    assert {"query_codebase", "get_feature_trace", "get_impact", "get_anchor_drift", "get_source",
            "search_ideas", "get_idea", "get_idea_map", "propose_idea",
            "generate_idea_candidates", "join_idea_dots"} <= names


def test_project_idea_tools_preserve_candidate_boundary(tmp_path: Path, monkeypatch) -> None:
    from cms import config

    monkeypatch.setattr(config, "IDEAS_USER_DIR", tmp_path / "global-ideas")
    server = _server(tmp_path)
    proposed = _tool(server, "propose_idea", {
        "title": "Cross-project memory prompts",
        "overview": "Combine Atlas project cards with the journal.",
        "kind": "tool",
    })
    assert proposed["candidate"]["status"] == "new"
    assert _tool(server, "search_ideas", {"query": "memory"})["ideas"] == []
    graph = _tool(server, "get_idea_map", {"include_features": False})
    assert "nodes" in graph and "edges" in graph


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

    drift = _tool(server, "get_anchor_drift", {"target": "feature:Greeting"})
    assert drift["target"] == "feature:Greeting" and drift["findings"] == []

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


def test_discover_root_walks_up_to_memory_layer(tmp_path: Path) -> None:
    _server(tmp_path)  # builds .memory/graph.json at tmp_path
    nested = tmp_path / "src" / "deep"
    nested.mkdir(parents=True)
    assert discover_root(nested) == tmp_path
    assert discover_root(tmp_path) == tmp_path
    # nothing to find -> the start dir comes back unchanged
    bare = tmp_path / "src"
    (tmp_path / ".memory" / "graph.json").rename(tmp_path / ".memory" / "graph.bak")
    try:
        assert discover_root(bare) == bare
    finally:
        (tmp_path / ".memory" / "graph.bak").rename(tmp_path / ".memory" / "graph.json")


def test_no_memory_layer_serves_gracefully(tmp_path: Path) -> None:
    """A repo with no .memory/: handshake + tools/list still work, tool calls
    return a helpful error, and nothing is written into the repo."""
    server = MCPServer(tmp_path)
    init = _call(server, "initialize", {"clientInfo": {"name": "TestClient"}})
    assert init["result"]["serverInfo"]["name"] == "cms"
    tools = _call(server, "tools/list")["result"]["tools"]
    assert len(tools) >= 14
    resp = _call(server, "tools/call", {"name": "query_codebase", "arguments": {"query": "anything"}})
    assert resp["result"]["isError"] is True
    assert "no memory layer" in resp["result"]["content"][0]["text"]
    assert "run-all" in resp["result"]["content"][0]["text"]
    # the cosmetic activity feed must not scribble on an un-mapped repo
    assert not (tmp_path / ".memory").exists()


def _make_memory(root: Path, source_name: str, source: str) -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / source_name).write_text(source, encoding="utf-8")
    graph = build_graph(scan(root))
    build_features(graph, MockProvider())
    (root / ".memory").mkdir(exist_ok=True)
    CodebaseMemory(graph).save(root / ".memory" / "graph.json")


def test_switch_project_flips_root_mid_session(tmp_path: Path) -> None:
    a = tmp_path / "proj_a"
    a.mkdir()
    server = _server(a)
    b = tmp_path / "proj_b"
    _make_memory(b, "other.py", "# @memory:feature:Widgets\ndef widget():\n    return 1\n")

    out = _tool(server, "switch_project", {"path": str(b)})
    assert out["memory"] == "loaded" and out["root"] == str(b)
    assert out["files"] == 1 and out["nodes"] > 1
    hits = _tool(server, "query_codebase", {"query": "widget"})
    assert any("other.py" in h["node_id"] for h in hits)
    # the source guard is now anchored to the NEW root
    src = _tool(server, "get_source", {"path": "../proj_a/app.py"})
    assert "error" in src
    src = _tool(server, "get_source", {"path": "other.py"})
    assert "def widget" in src["source"]


def test_switch_project_guardrails_and_lazy_build_pickup(tmp_path: Path) -> None:
    a = tmp_path / "proj_a"
    a.mkdir()
    server = _server(a)
    # a plain directory is not a project: refused, root unchanged
    plain = tmp_path / "not_a_project"
    plain.mkdir()
    out = _tool(server, "switch_project", {"path": str(plain)})
    assert "error" in out
    assert server.root == a
    # a git repo without memory: switches, reports how to build
    repo = tmp_path / "fresh_repo"
    (repo / ".git").mkdir(parents=True)
    out = _tool(server, "switch_project", {"path": str(repo)})
    assert out["memory"] == "missing" and "run-all" in out["next_step"]
    # once the memory is built (e.g. via the shell), tools work without a restart
    _make_memory(repo, "thing.py", "def thing():\n    return 2\n")
    hits = _tool(server, "query_codebase", {"query": "thing"})
    assert any("thing.py" in h["node_id"] for h in hits)


def test_constellation_tools_via_mcp(tmp_path: Path, monkeypatch) -> None:
    """list_projects / get_fusion_report / refine_fusion — the conversational
    fusion loop as an agent drives it over JSON-RPC."""
    import cms.fuse as fuse
    from cms.update import incremental_update

    monkeypatch.setattr(fuse, "REGISTRY_PATH", tmp_path / "reg" / "projects.json")
    monkeypatch.setattr(fuse, "FUSION_DIR", tmp_path / "reg" / "fusion")

    fusion_json = ('{"integrations": [{"title": "Wire A into B", "projects": ["p_a", "p_b"],'
                   ' "features": ["Alpha", "Beta"], "description": "d", "first_step": "s"}],'
                   ' "emergent": [], "conflicts": []}')

    class Prov:
        name, model = "stub-real", "m1"

        def __init__(self, disc):
            self.disc = disc

        def summarize(self, prompt, context):
            if "named FEATURES" in prompt:
                return self.disc
            if "principal architect" in prompt or "fusion report" in prompt:
                return fusion_json
            if "FEATURE TRACE" in prompt:
                return "## Purpose\nx\n## Flow\nx\n## Verification Checklist\n- x"
            return "Summary."

    for name, feat in (("p_a", "Alpha"), ("p_b", "Beta")):
        proj = tmp_path / name
        proj.mkdir()
        (proj / "m.py").write_text("def f():\n    pass\n", encoding="utf-8")
        incremental_update(proj, Prov(f'[{{"name": "{feat}", "description": "d", "files": ["m.py"]}}]'),
                           echo=lambda *a: None)

    server = _server(tmp_path)  # bound root is irrelevant: constellation is machine-level
    projects = _tool(server, "list_projects", {})
    assert [(p["name"], p["ready"]) for p in projects] == [("p_a", True), ("p_b", True)]

    # no report yet -> explicit error, not empty success
    assert "error" in _tool(server, "get_fusion_report", {})

    fuse.build_fusion([tmp_path / "p_a", tmp_path / "p_b"], Prov("[]"))
    got = _tool(server, "get_fusion_report", {})
    assert got["report"]["integrations"][0]["title"] == "Wire A into B"
    assert got["stale_members"] == [] and got["refinements"] == []

    monkeypatch.setattr("cms.providers.get_provider", lambda *_: Prov("[]"))
    out = _tool(server, "refine_fusion", {"direction": "deepen the A->B wiring"})
    assert out["refined"] is True
    assert _tool(server, "get_fusion_report", {})["refinements"][0]["direction"] == \
        "deepen the A->B wiring"

    monkeypatch.setattr("cms.providers.get_provider",
                        lambda *_: __import__("cms.providers", fromlist=["MockProvider"]).MockProvider())
    assert "real provider" in _tool(server, "refine_fusion", {"direction": "x"})["error"]


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


def test_annotation_tools_roundtrip(tmp_path: Path) -> None:
    server = _server(tmp_path)
    _call(server, "initialize", {"clientInfo": {"name": "Claude Code"}})
    added = _tool(server, "add_annotation", {
        "target": "feature:Greeting", "type": "bug_suspicion",
        "body": "greet may not handle empty names", "confidence": 0.6,
        "feature": "Greeting"})
    ann = added["annotation"]
    assert ann["author"]["kind"] == "model"
    assert ann["author"]["identity"] == "Claude Code"  # provenance from clientInfo
    listed = _tool(server, "list_annotations", {"feature": "Greeting"})
    assert [a["id"] for a in listed["annotations"]] == [ann["id"]]
    bad = _tool(server, "add_annotation", {"target": "  ", "type": "note", "body": "x"})
    assert "error" in bad


def test_decision_tools_propose_but_never_approve(tmp_path: Path) -> None:
    server = _server(tmp_path)
    _call(server, "initialize", {"clientInfo": {"name": "Codex"}})
    out = _tool(server, "propose_decision", {
        "feature": "Greeting", "title": "Greet politely",
        "behaviour": "greet returns the name unchanged",
        "prohibited": ["mutating the name"]})
    dec = out["decision"]
    assert dec["status"] == "proposed"
    assert dec["created_by"]["kind"] == "model"
    assert "human must approve" in out["next_step"]
    # there is deliberately NO approve tool on the MCP surface
    from cms.mcp import TOOLS
    assert not any("approve" in t["name"] for t in TOOLS)
    listed = _tool(server, "get_decisions", {"feature": "Greeting"})
    assert [d["id"] for d in listed["decisions"]] == [dec["id"]]


def test_review_exact_flow_tool(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("CMS_PROVIDER", "mock")
    server = _server(tmp_path)
    out = _tool(server, "review_exact_flow", {"feature": "Greeting"})
    assert out["status"] == "static_only" and out["flows"]
    # cache-first on the second call (mock still, but reused path)
    again = _tool(server, "review_exact_flow", {"feature": "Greeting"})
    assert again.get("reused") is True
    missing = _tool(server, "review_exact_flow", {"feature": "Ghost"})
    assert "error" in missing


def test_discover_feature_tool_mock_degrades(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("CMS_PROVIDER", "mock")
    server = _server(tmp_path)
    out = _tool(server, "discover_feature",
                {"description": "greeting people by their given name"})
    assert out["real"] is False and out["candidate"] is None
    assert out["hits"]  # ranked evidence still served
    bad = _tool(server, "discover_feature", {"description": "hi"})
    assert "error" in bad


# --- Library tools ---------------------------------------------------------

def _library_server(tmp_path: Path, monkeypatch) -> MCPServer:
    from cms import config

    monkeypatch.setenv("CMS_LIBRARY_BUILTIN", str(tmp_path / "no-builtins"))
    monkeypatch.setattr(config, "LIBRARY_USER_DIR", tmp_path / "no-userlib")
    return _server(tmp_path)


def test_library_tools_browse_and_load(tmp_path: Path, monkeypatch) -> None:
    from cms.library import LibraryView

    server = _library_server(tmp_path, monkeypatch)
    store = LibraryView(tmp_path).store("project")
    store.save_draft("---\nid: tdd-flow\nname: TDD Flow\ntype: strategy\n"
                     "description: Test first.\ntags: [testing]\n---\n\nWrite the test first.")
    store.publish("tdd-flow", "alex")

    listed = _tool(server, "list_assets", {"type": "strategy"})
    assert [a["id"] for a in listed["assets"]] == ["tdd-flow"]
    assert listed["assets"][0]["version"] == 1
    assert _tool(server, "list_assets", {"q": "nothing-matches"})["assets"] == []

    got = _tool(server, "get_asset", {"id": "tdd-flow"})["asset"]
    assert got["content"] == "Write the test first."   # canonical content, verbatim
    assert got["version"] == 1 and got["status"] == "published"
    assert len(got["versions"][0]["content_hash"]) == 24
    assert "error" in _tool(server, "get_asset", {"id": "ghost-asset"})


def test_propose_asset_drafts_only_never_publishes(tmp_path: Path, monkeypatch) -> None:
    from cms.library import LibraryView
    from cms.mcp import TOOLS

    server = _library_server(tmp_path, monkeypatch)
    _call(server, "initialize", {"clientInfo": {"name": "Claude Code"}})
    out = _tool(server, "propose_asset", {
        "name": "React Conventions", "type": "skill",
        "description": "How we write React here.",
        "content": "Prefer function components.", "tags": ["react"]})
    assert out["asset"]["id"] == "react-conventions"
    assert out["asset"]["status"] == "draft"       # never published by an agent
    assert out["asset"]["trust"] == "agent"        # visibly untrusted
    assert "human must review and publish" in out["next_step"]

    rec = LibraryView(tmp_path).store("project").get("react-conventions")
    assert rec["created_by"]["kind"] == "model"
    assert rec["created_by"]["identity"] == "Claude Code"  # provenance from clientInfo
    assert rec["created_by"]["via"] == "mcp"

    # a draft is invisible to composed context until a human publishes it
    composed = LibraryView(tmp_path).compose(["react-conventions"])
    assert composed["assets"] == []
    assert any(w["kind"] == "unpublished-asset" for w in composed["warnings"])

    # there is deliberately NO publish tool on the MCP surface
    assert not any("publish" in t["name"] for t in TOOLS)
    assert "error" in _tool(server, "propose_asset", {
        "name": "Bad", "type": "sorcery", "description": "x", "content": "y"})


def test_propose_asset_revision_leaves_published_content_untouched(
        tmp_path: Path, monkeypatch) -> None:
    from cms.library import LibraryView

    server = _library_server(tmp_path, monkeypatch)
    _call(server, "initialize", {"clientInfo": {"name": "Codex"}})
    store = LibraryView(tmp_path).store("project")
    store.save_draft("---\nid: house-rules\nname: House Rules\ntype: constraint\n"
                     "description: The rules.\n---\n\nOriginal rule.")
    store.publish("house-rules", "alex")

    out = _tool(server, "propose_asset", {
        "id": "house-rules", "name": "House Rules", "type": "constraint",
        "description": "The rules.", "content": "Agent's revised rule."})
    assert out["asset"]["revision_of_published"] is True
    assert "published version is unchanged" in out["next_step"]

    # composition still serves the published v1 — the agent's draft is inert
    composed = LibraryView(tmp_path).compose(["house-rules"])
    assert composed["assets"][0]["content"] == "Original rule."
    assert composed["assets"][0]["version"] == 1
    assert LibraryView(tmp_path).store("project").get("house-rules")["dirty"] is True


def test_task_prompt_and_intent_record_asset_versions(tmp_path: Path, monkeypatch) -> None:
    from cms.library import LibraryView

    server = _library_server(tmp_path, monkeypatch)
    store = LibraryView(tmp_path).store("project")
    store.save_draft("---\nid: memory-first\nname: Memory First\ntype: strategy\n"
                     "description: Query before grep.\n---\n\nConsult memory before grep.")
    store.publish("memory-first", "alex")

    brief = _tool(server, "export_task_prompt",
                  {"task": "improve greet", "assets": ["memory-first"]})
    assert "Consult memory before grep." in brief["content"]
    assert "memory-first@v1" in brief["content"]

    declared = _tool(server, "declare_intent",
                     {"goal": "improve greet", "assets": ["memory-first"]})
    used = declared["library"]["assets"]
    assert used[0]["id"] == "memory-first" and used[0]["version"] == 1
    assert len(used[0]["content_hash"]) == 24  # reproducible: exact content pinned


def test_annotations_can_target_a_library_asset(tmp_path: Path, monkeypatch) -> None:
    server = _library_server(tmp_path, monkeypatch)
    _call(server, "initialize", {"clientInfo": {"name": "Claude Code"}})
    out = _tool(server, "add_annotation", {
        "target": "asset:memory-first", "type": "observation",
        "body": "This asset needs an example for TS repos."})
    assert out["annotation"]["target_kind"] == "asset"
    listed = _tool(server, "list_annotations", {"target": "asset:memory-first"})
    assert listed["annotations"][0]["body"].startswith("This asset needs")


def test_library_mode_and_use_evidence_tools(tmp_path: Path, monkeypatch) -> None:
    from cms.library import LibraryView

    server = _library_server(tmp_path, monkeypatch)
    proposed = _tool(server, "propose_asset", {
        "name": "Focus Mode", "type": "mode",
        "description": "Keep one objective visible.", "content": "Stay on target."})
    assert proposed["asset"]["type"] == "mode"
    store = LibraryView(tmp_path).store("project")
    store.publish("focus-mode", "human")

    used = _tool(server, "record_asset_use", {
        "assets": ["focus-mode"], "task": "Complete the investigation",
        "outcome": "success", "effectiveness": 4, "efficiency": 5,
        "duration_ms": 250, "model": "test-model"})
    assert used["use"]["assets"][0]["version"] == 1
    assert len(used["use"]["assets"][0]["hash"]) == 24

    feedback = _tool(server, "get_asset_feedback", {"id": "focus-mode"})["feedback"]
    assert feedback["uses"] == 1
    assert feedback["agent"]["efficiency"] == 5.0
    listed = _tool(server, "list_assets", {"type": "mode"})["assets"][0]
    assert listed["evidence"]["uses"] == 1
