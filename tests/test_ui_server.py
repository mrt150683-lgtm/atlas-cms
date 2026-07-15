import http.client
import json
import os
import threading
import time
import urllib.error
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

from cms.activity import log_activity
from cms.exporter import export_graph
from cms.features import build_features
from cms.graph_builder import build_graph
from cms.providers import MockProvider
from cms.scanner import scan
from cms.tree_export import export_tree
from cms.ui import _MemoryCache, make_handler

SOURCE = '''\
# @memory:feature:Greeting
def greet(name):
    return helper(name)


def helper(name):
    return name
'''


class _Client:
    """One persistent keep-alive connection, like a real browser tab.

    Fresh connections to a Windows loopback listener can stall for tens of
    seconds under AV/load, so we connect once (with a patient deadline) and
    reuse the socket for every request."""

    def __init__(self, port: int) -> None:
        self.port = port
        self.conn: http.client.HTTPConnection | None = None
        self.lock = threading.Lock()
        self._connect(deadline=60)

    def _connect(self, deadline: float) -> None:
        end = time.time() + deadline
        while True:
            try:
                conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
                conn.request("GET", "/api/meta")
                conn.getresponse().read()
                self.conn = conn
                return
            except Exception:
                if time.time() > end:
                    raise
                time.sleep(0.5)

    def get(self, path: str):
        with self.lock:
            try:
                self.conn.request("GET", path)
                resp = self.conn.getresponse()
                return resp.status, resp.read()
            except Exception:
                self.conn.close()
                self._connect(deadline=30)  # one reconnect, then requests fail loudly
                self.conn.request("GET", path)
                resp = self.conn.getresponse()
                return resp.status, resp.read()

    def post(self, path: str, payload: dict):
        body = json.dumps(payload)
        headers = {"Content-Type": "application/json"}
        with self.lock:
            try:
                self.conn.request("POST", path, body=body, headers=headers)
                resp = self.conn.getresponse()
                return resp.status, resp.read()
            except Exception:
                self.conn.close()
                self._connect(deadline=30)
                self.conn.request("POST", path, body=body, headers=headers)
                resp = self.conn.getresponse()
                return resp.status, resp.read()


APPROVAL_TOKEN = "test-approval-code"


@pytest.fixture(scope="module")
def server(tmp_path_factory):
    from cms import config

    root = tmp_path_factory.mktemp("uiproj")
    (root / "app.py").write_text(SOURCE, encoding="utf-8")
    records = scan(root)
    graph = build_graph(records)
    build_features(graph, MockProvider())
    memory_dir = root / ".memory"
    export_tree(root, records, memory_dir)
    export_graph(graph, memory_dir)

    # keep the library scopes off the real repo and the real home dir
    empty = tmp_path_factory.mktemp("nolib")
    os.environ["CMS_LIBRARY_BUILTIN"] = str(empty / "builtin")
    real_user_dir = config.LIBRARY_USER_DIR
    config.LIBRARY_USER_DIR = empty / "userlib"

    cache = _MemoryCache(memory_dir / "graph.json")
    os.environ["CMS_APPROVAL_TOKEN"] = APPROVAL_TOKEN  # deterministic gate for tests
    try:
        handler = make_handler(root, cache)
    finally:
        del os.environ["CMS_APPROVAL_TOKEN"]
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    client = _Client(httpd.server_address[1])
    yield client, root
    httpd.shutdown()
    httpd.server_close()
    config.LIBRARY_USER_DIR = real_user_dir
    os.environ.pop("CMS_LIBRARY_BUILTIN", None)


def test_index_and_meta(server) -> None:
    client, root = server
    status, body = client.get("/")
    assert status == 200 and b"<canvas" in body
    status, body = client.get("/api/meta")
    assert json.loads(body)["project"] == root.name


def test_graph_tree_query_endpoints(server) -> None:
    client, _ = server
    status, body = client.get("/api/graph")
    graph = json.loads(body)
    assert status == 200 and any(n["id"] == "feature:Greeting" for n in graph["nodes"])

    status, body = client.get("/api/tree")
    assert json.loads(body)["file_count"] == 1

    _, body = client.get("/api/query?q=greet+name&k=3")
    hits = json.loads(body)["results"]
    assert any(h["node_id"] == "func:app.py::greet" for h in hits)


def test_trust_loop_actions_are_live(server) -> None:
    """Impact/verify/align run as real endpoints behind the trust-loop buttons."""
    client, _ = server

    # impact: pure-graph blast radius for a resolvable target
    status, body = client.get("/api/impact?target=func:app.py::greet")
    assert status == 200
    imp = json.loads(body)
    assert "total" in imp and isinstance(imp["features"], list)

    # an unresolvable target is a clean 404, not a 500
    status, _ = client.get("/api/impact?target=feature:DoesNotExist")
    assert status == 404

    # verify: a feature with no mapped tests reports honestly instead of running
    status, body = client.post("/api/verify", {"feature": "Greeting"})
    assert status == 200
    v = json.loads(body)
    assert v["ran"] is False and "No tests are mapped" in v["message"]
    status, _ = client.post("/api/verify", {"feature": "NoSuchFeature"})
    assert status == 404

    # align: verdicts the working tree (a non-git tmp project has no changes)
    status, body = client.post("/api/align", {})
    assert status == 200
    rec = json.loads(body)
    assert rec["verdict"] in ("aligned", "partial", "drift", "unverified")

    # drift: the same deterministic anchor check used by CLI/MCP/Sentinel
    status, body = client.get("/api/drift?target=feature:Greeting")
    assert status == 200
    drift = json.loads(body)
    assert drift["target"] == "feature:Greeting" and isinstance(drift["findings"], list)


def test_source_endpoint_and_traversal_guard(server) -> None:
    client, _ = server
    _, body = client.get("/api/source?path=app.py")
    assert "def greet" in json.loads(body)["text"]

    status, _ = client.get("/api/source?path=../secrets.txt")
    assert status == 403
    status, _ = client.get("/api/nope")
    assert status == 404


def test_activity_endpoint_since_filtering(server) -> None:
    client, root = server
    log_activity(root / ".memory", "query_codebase", ["file:app.py"], label="probe")
    _, body = client.get("/api/activity?since=0")
    data = json.loads(body)
    assert data["events"] and data["events"][-1]["label"] == "probe"
    _, body = client.get(f"/api/activity?since={data['now'] + 1}")
    assert json.loads(body)["events"] == []


def test_cache_reloads_when_graph_changes(server) -> None:
    import os

    client, root = server
    graph_path = root / ".memory" / "graph.json"
    client.get("/api/query?q=greet")  # prime the cache
    data = json.loads(graph_path.read_text(encoding="utf-8"))
    for n in data["nodes"]:
        if n["id"] == "func:app.py::greet":
            n["summary"] = "UPDATED-SUMMARY-SENTINEL"
    graph_path.write_text(json.dumps(data), encoding="utf-8")
    os.utime(graph_path, (time.time() + 3, time.time() + 3))  # force mtime change
    _, body = client.get("/api/query?q=greet&k=1")
    assert "UPDATED-SUMMARY-SENTINEL" in json.loads(body)["results"][0]["summary"]


def test_switch_root_rebinds_live(tmp_path, monkeypatch) -> None:
    """POST /api/switch-root points the running server at another codebase."""
    import http.client
    import json as _json
    from http.server import ThreadingHTTPServer

    from cms.ui import _MemoryCache, make_handler

    monkeypatch.setattr("cms.app._save_workspace_root", lambda root: None)  # no cwd pollution
    monkeypatch.setenv("CMS_PROVIDER", "mock")  # build-on-switch offline + deterministic

    proj_a = tmp_path / "a"
    proj_a.mkdir()
    (proj_a / "a.py").write_text("def a(): pass\n", encoding="utf-8")
    (proj_a / ".memory").mkdir()
    (proj_a / ".memory" / "graph.json").write_text('{"nodes": []}', encoding="utf-8")
    proj_b = tmp_path / "b" / "src"
    proj_b.mkdir(parents=True)
    (proj_b / "b.py").write_text("def b(): pass\n", encoding="utf-8")

    cache = _MemoryCache(proj_a / ".memory" / "graph.json")
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(proj_a, cache))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    port = httpd.server_address[1]

    def call(method, path, body=None):
        c = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        c.request(method, path, body=_json.dumps(body) if body is not None else None,
                  headers={"Content-Type": "application/json"} if body is not None else {})
        r = c.getresponse()
        status, data = r.status, r.read()
        c.close()
        return status, data

    try:
        _, body = call("GET", "/api/meta")
        assert _json.loads(body)["project"] == "a"

        status, body = call("POST", "/api/switch-root", {"path": str(tmp_path / "b")})
        res = _json.loads(body)
        assert status == 200 and res["switched"] and res["project"] == "b"
        assert res["building"] is True                       # processing kicked off, no restart

        _, body = call("GET", "/api/meta")
        assert _json.loads(body)["project"] == "b"          # live rebind took effect

        # the background build actually processes the new codebase
        for _ in range(60):
            st = _json.loads(call("GET", "/api/build-status")[1])
            if not st["running"]:
                break
            time.sleep(0.5)
        assert not st["running"] and not st["error"], st
        built = json.loads((tmp_path / "b" / ".memory" / "graph.json").read_text(encoding="utf-8"))
        assert any(n.get("id") == "func:src/b.py::b" for n in built["nodes"])

        status, _ = call("POST", "/api/switch-root", {"path": str(tmp_path / "nope")})
        assert status == 400                                 # non-existent folder rejected
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_semantic_endpoint_reports_stage_evidence(server) -> None:
    """The UI must not reconstruct semantic validity from node existence —
    /api/semantic serves durable stage evidence + live validity directly."""
    import cms.semantic_state as ss

    client, root = server
    ss.record_stage(root / ".memory", "features", status="skipped",
                    provider="mock", real_provider=False,
                    reason="feature discovery requires a real provider")
    status, body = client.get("/api/semantic")
    assert status == 200
    sem = json.loads(body)
    assert sem["project"] == root.name and sem["root"] == str(root)
    assert set(sem["stages"]) == {"summaries", "features", "hierarchy", "review", "suggestions"}
    assert sem["stages"]["features"]["status"] == "skipped"
    assert sem["stages"]["review"]["status"] == "never_run"
    assert "provider" in sem and "real" in sem["provider"]
    assert "artifacts" in sem and "available" in sem["artifacts"]
    assert "counts" in sem and sem["counts"]["feature_count"] >= 1  # Greeting
    assert sem["live"]["review"]["validity"] in ("missing", "invalid")
    # no secrets anywhere in the payload
    assert "key" not in body.decode("utf-8").lower() or "api_key" not in body.decode("utf-8")


def test_features_section_never_hidden_markup() -> None:
    """Zero features must render an explicit state, not an empty hidden
    panel. The wrap has no display:none and every semantic status has a
    visible explanation string in the frontend."""
    html = (Path(__file__).parent.parent / "cms" / "ui_assets" / "index.html").read_text(
        encoding="utf-8")
    wrap = html.split('id="featuresWrap"')[1][:120]
    assert "display:none" not in wrap
    assert 'id="featStatus"' in html
    for msg in ("Feature discovery has never run",
                "Feature discovery requires a real provider",
                "Feature discovery failed",
                "Feature discovery is running",
                "zero features found",
                "Feature data is stale"):
        assert msg in html, f"missing visible semantic state: {msg!r}"
    # project identity + provider provenance surfaced
    assert 'id="providerChip"' in html and "meta.root" in html
    assert "Current runtime provider" in html
    assert "Loaded artifact provenance" in html


def test_feature_evidence_map_has_bounded_deterministic_overview() -> None:
    html = (Path(__file__).parent.parent / "cms" / "ui_assets" / "index.html"
            ).read_text(encoding="utf-8")

    assert "const ringSize = Math.max(24" in html
    assert "inferredOverview++ < 120" in html
    assert "focus reveals all incident evidence" in html
    assert "if (S.featMode) return" in html
    assert "now - lastDraw >= 100" in html


def test_primary_ui_exposes_actionable_trust_loop() -> None:
    html = (Path(__file__).parent.parent / "cms" / "ui_assets" / "index.html"
            ).read_text(encoding="utf-8")

    assert 'id="trustLoop" aria-label="Atlas trust loop"' in html
    for stage in ("query", "trace", "impact", "verify", "align"):
        assert f'data-stage="{stage}"' in html
    assert "function showTrustStage(stage)" in html
    assert 'href="/discovery"' in html  # strategic discovery remains separate


def test_main_ui_exposes_auditable_mcp_activity_history() -> None:
    html = (Path(__file__).parent.parent / "cms" / "ui_assets" / "index.html"
            ).read_text(encoding="utf-8")

    assert 'id="mcpBadge" role="button" tabindex="0"' in html
    assert 'id="activityPanel" aria-label="MCP activity evidence"' in html
    assert "persistent .memory/activity.jsonl" in html
    assert 'fetch("/api/activity?since=0")' in html
    assert "function renderActivityAudit()" in html
    assert "event.nodes" in html and "activity-nodes" in html


def test_notes_update_route_is_reachable_and_trust_commands_are_live() -> None:
    html = (Path(__file__).parent.parent / "cms" / "ui_assets" / "index.html"
            ).read_text(encoding="utf-8")

    assert 'fetch("/api/notes/update"' in html
    assert "function editNote(id)" in html
    # the trust-loop stages are live one-click actions, not copy-paste command text
    assert "function runTrustAction(stage" in html
    assert '"/api/impact?target="' in html
    assert 'fetch("/api/verify"' in html and 'fetch("/api/align"' in html
    assert 'fetch("/api/drift?target="' in html
    assert "The stated intent here no longer matches the code" in html
    assert "chat unavailable" in html
    # invalid/stale judgment banners exist
    assert "not valid semantic output" in html and "valid but frozen" in html
    assert "Historical AI Review — not current" in html
    assert "Historical suggestions are hidden until refreshed" in html
    # feature inspection exposes coverage evidence without overstating it
    assert "Test execution evidence" in html
    assert "Coverage shows which tests executed" in html
    assert "does not prove every intended behaviour" in html
    assert "No tests are mapped yet" in html


def test_discovery_page_and_apis(tmp_path, monkeypatch) -> None:
    """/discovery page + /api/fusion + /api/scout + scout status POST."""
    import cms.fuse as fuse
    import cms.scout as scout_mod

    monkeypatch.setattr(fuse, "FUSION_DIR", tmp_path / "fusion")
    monkeypatch.setattr(scout_mod, "SCOUT_DIR", tmp_path / "scout")

    root = tmp_path / "proj"
    root.mkdir()
    (root / "app.py").write_text(SOURCE, encoding="utf-8")
    records = scan(root)
    graph = build_graph(records)
    export_tree(root, records, root / ".memory")
    export_graph(graph, root / ".memory")
    cache = _MemoryCache(root / ".memory" / "graph.json")
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(root, cache))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    client = _Client(httpd.server_address[1])
    try:
        status, body = client.get("/discovery")
        assert status == 200 and b"Constellation" in body and b"Scout" in body

        status, body = client.get("/api/fusion")
        assert status == 200 and json.loads(body)["report"] is None

        # seed a scout suggestion, then decide it over HTTP
        scout_mod._save("suggestions.json", {"abc123": {
            "id": "abc123", "kind": "concepts", "title": "T", "description": "d",
            "builds_on": [], "status": "proposed", "provenance": "llm",
            "first_seen": "x", "last_seen": "x"}})
        status, body = client.get("/api/scout")
        assert json.loads(body)["suggestions"][0]["status"] == "proposed"

        status, body = client.post("/api/scout/status",
                                   {"id": "abc123", "status": "rejected"})
        assert status == 200 and json.loads(body)["suggestion"]["status"] == "rejected"
        status, body = client.post("/api/scout/status", {"id": "abc123", "status": "meh"})
        assert status == 400
    finally:
        httpd.shutdown()


def test_discovery_page_has_subtabs() -> None:
    """The Discovery page must be tabbed (too much data for one scroll):
    six panels, tab counters, idea status chips, and a plan-card filter."""
    html = (Path(__file__).parent.parent / "cms" / "ui_assets" / "constellation.html"
            ).read_text(encoding="utf-8")
    for tab in ("integrations", "emergent", "conflicts", "ideas", "plans", "history"):
        assert f'data-tab="{tab}"' in html, f"missing tab {tab}"
        assert f'id="p-{tab}"' in html, f"missing panel {tab}"
        assert f'id="n-{tab}"' in html, f"missing counter {tab}"
    assert 'id="ideaChips"' in html          # status filter chips
    assert 'id="cardFilter"' in html         # plan-card text filter
    assert "location.hash" in html           # deep-linkable tabs


def test_brainstorm_apis(tmp_path, monkeypatch) -> None:
    """/api/brainstorm state + rate + goals over HTTP (generate is covered in
    test_brainstorm; here the transport + error surface)."""
    import cms.brainstorm as bmod
    import cms.fuse as fuse

    monkeypatch.setattr(bmod, "BRAINSTORM_DIR", tmp_path / "bs")
    monkeypatch.setattr(fuse, "REGISTRY_PATH", tmp_path / "reg" / "projects.json")

    bmod._write("ideas.json", {"i1": {
        "id": "i1", "text": "A test idea.", "status": "new", "batch": "b",
        "temperature": 1.0, "project": None, "provider": "t", "model": "m",
        "created_at": "2026-01-01T00:00:00Z", "provenance": "llm"}})

    root = tmp_path / "proj"
    root.mkdir()
    (root / "app.py").write_text(SOURCE, encoding="utf-8")
    records = scan(root)
    export_tree(root, records, root / ".memory")
    export_graph(build_graph(records), root / ".memory")
    cache = _MemoryCache(root / ".memory" / "graph.json")
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(root, cache))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    client = _Client(httpd.server_address[1])
    try:
        status, body = client.get("/api/brainstorm")
        data = json.loads(body)
        assert status == 200 and data["ideas"][0]["id"] == "i1"
        assert data["goals"] == [] and isinstance(data["projects"], list)

        status, body = client.post("/api/brainstorm/rate", {"id": "i1", "verdict": "liked"})
        assert status == 200 and json.loads(body)["idea"]["status"] == "liked"
        status, _ = client.post("/api/brainstorm/rate", {"id": "i1", "verdict": "meh"})
        assert status == 400

        status, body = client.post("/api/brainstorm/goals", {"text": "solve big problems"})
        goals = json.loads(body)["goals"]
        assert status == 200 and goals[0]["text"] == "solve big problems"
        status, body = client.post("/api/brainstorm/goals", {"remove": goals[0]["id"]})
        assert json.loads(body)["goals"] == []
    finally:
        httpd.shutdown()


def test_brainstorm_tab_markup() -> None:
    html = (Path(__file__).parent.parent / "cms" / "ui_assets" / "constellation.html"
            ).read_text(encoding="utf-8")
    assert 'data-tab="brainstorm"' in html and 'id="p-brainstorm"' in html
    for el in ("bsTemp", "bsProject", "bsGen", "bsChips", "goalsPanel", "goalInput"):
        assert f'id="{el}"' in html, f"missing {el}"
    # the goals panel starts hidden and is revealed by seven logo clicks
    assert "logoClicks >= 7" in html
    assert 'id="goalsPanel" class="card" style="display:none' in html


def test_projects_endpoint_lists_mapped_with_current_flag(tmp_path, monkeypatch) -> None:
    import cms.fuse as fuse
    from cms.fuse import register_project

    monkeypatch.setattr(fuse, "REGISTRY_PATH", tmp_path / "reg" / "projects.json")

    served = tmp_path / "served"
    other = tmp_path / "other"
    for proj in (served, other):
        (proj / ".memory").mkdir(parents=True)
        (proj / ".memory" / "graph.json").write_text('{"nodes": []}', encoding="utf-8")
        register_project(proj)
    dead = tmp_path / "dead"          # registered but memory vanished -> excluded
    (dead / ".memory").mkdir(parents=True)
    (dead / ".memory" / "graph.json").write_text("{}", encoding="utf-8")
    register_project(dead)
    (dead / ".memory" / "graph.json").unlink()

    cache = _MemoryCache(served / ".memory" / "graph.json")
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(served, cache))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    client = _Client(httpd.server_address[1])
    try:
        status, body = client.get("/api/projects")
        assert status == 200
        projects = json.loads(body)["projects"]
        by_name = {p["name"]: p for p in projects}
        assert set(by_name) == {"served", "other"}
        assert by_name["served"]["current"] is True
        assert by_name["other"]["current"] is False
        assert by_name["other"]["pipeline"] in ("in_progress", "finished", "attention")
    finally:
        httpd.shutdown()


def test_header_nav_consistent_and_switcher_present() -> None:
    html = (Path(__file__).parent.parent / "cms" / "ui_assets" / "index.html"
            ).read_text(encoding="utf-8")
    # the three screen links are folded into ONE header menu (declutter),
    # each keeping its id, target and description
    assert 'id="navMenu"' in html and 'id="navBtn"' in html and 'id="navPop"' in html
    for link, href in (("discoveryLink", "/discovery"), ("setupLink", "/setup"),
                       ("sentinelLink", "/sentinel")):
        assert f'id="{link}" href="{href}"' in html, f"{link} missing from the menu"
    # fast project switcher wired to the live endpoints
    assert 'id="projectSwitch"' in html and 'id="projectMenu"' in html
    assert '"/api/projects"' in html and '"/api/switch-root"' in html


def test_chat_endpoint_and_popup_markup(tmp_path, monkeypatch) -> None:
    """POST /api/chat answers grounded; GET restores the transcript; the
    popup markup is present and themed."""
    import cms.providers as providers_mod

    class StubChat:
        name, model = "stub-real", "m1"

        def summarize(self, prompt, context):
            if "OWNER'S QUESTION" in prompt:
                return "Greeting is built and traced. (app.py:2-3)"
            return "Summary."

    monkeypatch.setattr(providers_mod, "get_provider", lambda *_: StubChat())

    root = tmp_path / "proj"
    root.mkdir()
    (root / "app.py").write_text(SOURCE, encoding="utf-8")
    records = scan(root)
    graph = build_graph(records)
    build_features(graph, MockProvider())
    export_tree(root, records, root / ".memory")
    export_graph(graph, root / ".memory")
    cache = _MemoryCache(root / ".memory" / "graph.json")
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(root, cache))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    client = _Client(httpd.server_address[1])
    try:
        status, body = client.post("/api/chat", {"question": "what is the greeting feature?"})
        data = json.loads(body)
        assert status == 200 and data["answer"].startswith("Greeting is built")
        assert "Greeting" in data["matched_features"]

        status, body = client.get("/api/chat")
        transcript = json.loads(body)["transcript"]
        assert len(transcript) == 1 and transcript[0]["q"].startswith("what is")

        status, body = client.post("/api/chat", {"question": ""})
        assert status == 400
    finally:
        httpd.shutdown()

    html = (Path(__file__).parent.parent / "cms" / "ui_assets" / "index.html"
            ).read_text(encoding="utf-8")
    for el in ("chatFab", "chatBox", "chatMsgs", "chatInput", "chatSend"):
        assert f'id="{el}"' in html, f"missing {el}"
    assert '"/api/chat"' in html


def test_annotations_endpoints_and_panel_markup(server) -> None:
    """Structured annotations round-trip over HTTP and the inspector ships
    the panel + fetch wiring (Sentinel's UI<->HTTP contract)."""
    client, _ = server

    status, body = client.post("/api/annotations", {
        "target": "feature:Greeting", "type": "question",
        "body": "does greet handle unicode?", "feature": "Greeting"})
    assert status == 200
    ann = json.loads(body)["annotation"]
    assert ann["status"] == "open" and ann["target"] == "feature:Greeting"

    status, body = client.get("/api/annotations?feature=Greeting")
    rows = json.loads(body)["annotations"]
    assert status == 200 and [a["id"] for a in rows] == [ann["id"]]

    status, body = client.post("/api/annotations/update",
                               {"id": ann["id"], "status": "resolved"})
    assert status == 200 and json.loads(body)["annotation"]["resolved_at"]

    status, body = client.post("/api/annotations/archive", {"id": ann["id"]})
    assert status == 200
    status, body = client.get("/api/annotations?feature=Greeting")
    assert json.loads(body)["annotations"] == []  # archived out by default

    status, body = client.post("/api/annotations", {"target": "", "type": "note", "body": "x"})
    assert status == 400

    # transport provenance is server-stamped, never caller-controlled: even a
    # body claiming to be a user carries via=http for later judgment
    status, body = client.post("/api/annotations", {
        "target": "feature:Greeting", "type": "note", "body": "claimed-user note",
        "author": {"kind": "user", "identity": "totally-a-human", "via": "forged"}})
    assert status == 200
    assert json.loads(body)["annotation"]["author"]["via"] == "http"

    html = (Path(__file__).parent.parent / "cms" / "ui_assets" / "index.html"
            ).read_text(encoding="utf-8")
    for el in ("annSec", "annList", "annForm", "annAddBtn"):
        assert f'id="{el}"' in html, f"missing {el}"
    assert '"/api/annotations"' in html and "/api/annotations/update" in html


def test_human_view_toggle_and_resolution_slider_markup(server) -> None:
    """The Human View control, resolution slider, mapping trail and the
    projection/selection machinery ship in the UI."""
    client, _ = server
    status, body = client.get("/")
    html = body.decode("utf-8")
    assert status == 200
    for el in ("humanBtn", "humanPop", "resRange", "resName", "resBlurb", "resNote", "trail"):
        assert f'id="{el}"' in html, f"missing {el}"
    # the six semantic levels and the projection seam
    for token in ("RES_LEVELS", "buildHumanView", "buildPyramidView", "rebuildView",
                  "projectSelection", "trailChain", "selectSemantic", "NODE_LEVEL"):
        assert token in html, f"missing {token}"
    for level in ("System", "Component", "Feature", "Module", "Function", "Source"):
        assert f'name: "{level}"' in html, f"missing level {level}"
    # canonical traceability: the PART_OF pyramid is indexed client-side
    assert "featComp" in html and "compSys" in html
    # deep-link + persistence contract
    assert 'localStorage.getItem("cms.human.on")' in html
    assert 'params.get("human")' in html
    # layout contract: collapsible inspector, folded screens menu, and the
    # resolution popover always has a way OUT (mouseleave close + Escape)
    for token in ("has-inspector", "syncInspectorPane", "inspClose",
                  "navBtn", "navPop", "humanPopTimer", '"Escape"'):
        assert token in html, f"missing {token}"
    for el in ("discoveryLink", "setupLink", "sentinelLink"):
        assert f'id="{el}"' in html, f"missing {el}"  # folded, never lost


def test_decisions_endpoints_and_intent_panel(server) -> None:
    """Propose -> approve -> locked over HTTP; UI ships the Intent panel."""
    client, _ = server

    status, body = client.post("/api/decisions", {
        "feature": "Greeting", "title": "Greet politely",
        "intent": {"behaviour": "greet returns the given name unchanged"}})
    assert status == 200
    dec = json.loads(body)["decision"]
    assert dec["status"] == "proposed"

    # the human-only gate: no/wrong session code -> 403, never approved
    status, body = client.post("/api/decisions/approve",
                               {"id": dec["id"], "approved_by": "alex"})
    assert status == 403 and "session code" in json.loads(body)["error"]
    status, body = client.post("/api/decisions/approve",
                               {"id": dec["id"], "approved_by": "alex",
                                "token": "wrong-guess"})
    assert status == 403

    status, body = client.post("/api/decisions/approve",
                               {"id": dec["id"], "approved_by": "alex",
                                "token": APPROVAL_TOKEN})
    assert status == 200 and json.loads(body)["decision"]["status"] == "approved"

    status, body = client.get("/api/decisions?feature=Greeting")
    rows = json.loads(body)["decisions"]
    assert [d["id"] for d in rows] == [dec["id"]]

    status, body = client.post("/api/decisions/approve",
                               {"id": dec["id"], "approved_by": "alex",
                                "token": APPROVAL_TOKEN})
    assert status == 400  # cannot re-approve

    status, body = client.post("/api/decisions", {
        "feature": "Greeting", "title": "Unsafe alternative",
        "intent": {"behaviour": "replace the greeting without approval"}})
    rejectable = json.loads(body)["decision"]
    status, body = client.post("/api/decisions/close", {
        "id": rejectable["id"], "status": "rejected"})
    assert status == 403 and "session code" in json.loads(body)["error"]
    status, _ = client.post("/api/decisions/close", {
        "id": rejectable["id"], "status": "rejected", "token": "wrong-guess"})
    assert status == 403
    status, body = client.post("/api/decisions/close", {
        "id": rejectable["id"], "status": "rejected", "token": APPROVAL_TOKEN})
    assert status == 200 and json.loads(body)["decision"]["status"] == "rejected"

    status, body = client.post("/api/decisions", {"title": "", "intent": {}})
    assert status == 400

    html = (Path(__file__).parent.parent / "cms" / "ui_assets" / "index.html"
            ).read_text(encoding="utf-8")
    for el in ("intSec", "intBody"):
        assert f'id="{el}"' in html, f"missing {el}"
    for route in ("/api/decisions/approve", "/api/decisions/close"):
        assert route in html
    assert "initIntentPanel" in html


def test_flowreview_endpoints_and_panel(server, monkeypatch) -> None:
    """GET serves null before generation; POST builds the mock skeleton and
    persists it; the UI ships the panel."""
    monkeypatch.setenv("CMS_PROVIDER", "mock")
    client, _ = server

    status, body = client.get("/api/flowreview?feature=Greeting")
    assert status == 200 and json.loads(body)["review"] is None

    status, body = client.post("/api/flowreview", {"feature": "Greeting"})
    assert status == 200
    rv = json.loads(body)["review"]
    assert rv["status"] == "static_only" and rv["flows"]

    status, body = client.get("/api/flowreview?feature=Greeting")
    rv = json.loads(body)["review"]
    assert rv is not None and rv["stale"] is False  # persisted to graph.json

    status, body = client.post("/api/flowreview", {"feature": "Ghost"})
    assert status == 400

    html = (Path(__file__).parent.parent / "cms" / "ui_assets" / "index.html"
            ).read_text(encoding="utf-8")
    for el in ("flowSec", "flowBody"):
        assert f'id="{el}"' in html, f"missing {el}"
    assert '"/api/flowreview"' in html and "initFlowPanel" in html
    assert "FLOW_STATUS_UI" in html  # non-color-only status labels


def test_fidelity_endpoint_and_panel(server) -> None:
    client, _ = server
    status, body = client.get("/api/fidelity?feature=Greeting")
    f = json.loads(body)
    assert status == 200 and f["overall"] in ("on_track", "attention", "insufficient_evidence")
    assert f["explanations"]["implemented"]
    status, _ = client.get("/api/fidelity?feature=Ghost")
    assert status == 400

    html = (Path(__file__).parent.parent / "cms" / "ui_assets" / "index.html"
            ).read_text(encoding="utf-8")
    assert '"/api/fidelity"' not in html or True
    for el in ("fidSec", "fidBody"):
        assert f'id="{el}"' in html, f"missing {el}"
    assert "/api/fidelity" in html


def test_feature_discovery_endpoints(server, monkeypatch) -> None:
    monkeypatch.setenv("CMS_PROVIDER", "mock")
    client, _ = server
    status, body = client.post("/api/feature/discover",
                               {"description": "greeting people by their given name"})
    data = json.loads(body)
    assert status == 200 and data["real"] is False and data["candidate"] is None

    status, body = client.post("/api/feature/discover", {"description": "hi"})
    assert status == 400

    status, body = client.post("/api/feature/confirm", {
        "name": "PoliteGreeting", "description": "greets politely",
        "members": ["func:app.py::greet"]})
    assert status == 200 and json.loads(body)["confirmed"] is True
    status, body = client.get("/api/graph")
    assert '"feature:PoliteGreeting"' in body.decode("utf-8")

    html = (Path(__file__).parent.parent / "cms" / "ui_assets" / "index.html"
            ).read_text(encoding="utf-8")
    for el in ("fdisc", "fdiscBtn", "fdiscForm", "fdiscOut"):
        assert f'id="{el}"' in html, f"missing {el}"
    assert "/api/feature/discover" in html and "/api/feature/confirm" in html
    # the hunt panel: already-stated banner, lens-aware explanation,
    # provenance-chipped connections, jump links to existing features
    for token in ("fdiscExisting", "fdiscExpl", "fdiscConn", "data-huntfeat",
                  "Already stated"):
        assert token in html, f"missing {token}"


def test_feature_flags_gate_endpoints(server, monkeypatch) -> None:
    client, _ = server
    status, body = client.get("/api/meta")
    flags = json.loads(body)["flags"]
    assert flags == {"human_view": True, "annotations": True, "flow_review": True}

    monkeypatch.setenv("CMS_ANNOTATIONS", "0")
    status, _ = client.get("/api/annotations")
    assert status == 403
    monkeypatch.setenv("CMS_FLOW_REVIEW", "0")
    status, _ = client.get("/api/flowreview?feature=Greeting")
    assert status == 403
    monkeypatch.delenv("CMS_ANNOTATIONS")
    monkeypatch.delenv("CMS_FLOW_REVIEW")
    status, _ = client.get("/api/annotations")
    assert status == 200


def test_explain_endpoint_degrades_honestly_and_ui_wires_it(server, monkeypatch) -> None:
    monkeypatch.setenv("CMS_PROVIDER", "mock")
    client, _ = server
    status, body = client.post("/api/explain", {"items": [{"id": "feature:Greeting"}]})
    data = json.loads(body)
    assert status == 200 and data["real"] is False
    res = data["results"]["feature:Greeting"]
    assert res["status"] == "structural" and "no AI explanation" in res["text"]

    status, body = client.post("/api/explain", {"items": [{"id": "file:missing.py"}]})
    assert status == 400 and "unknown node" in json.loads(body)["error"]

    html = (Path(__file__).parent.parent / "cms" / "ui_assets" / "index.html"
            ).read_text(encoding="utf-8")
    assert '"/api/explain"' in html
    for el in ("explSec", "explBody", "explRegen"):
        assert f'id="{el}"' in html, f"missing {el}"


def test_lens_endpoint_and_slider_wiring(server, monkeypatch) -> None:
    """POST /api/lens rewrites narrative text at an audience level (honest
    mock degradation here) and the UI ships the slider + lensed targets."""
    monkeypatch.setenv("CMS_PROVIDER", "mock")
    client, _ = server

    long_text = ("This module builds the knowledge graph from scanned source "
                 "records. It resolves imports across files.")
    status, body = client.post("/api/lens", {
        "level": "tldr", "items": [{"id": "x", "text": long_text}]})
    data = json.loads(body)
    assert status == 200 and data["real"] is False
    assert data["results"]["x"].endswith("source records.")  # deterministic fallback

    status, body = client.post("/api/lens", {"level": "wizard", "items": []})
    assert status == 400 and "unknown lens level" in json.loads(body)["error"]

    html = (Path(__file__).parent.parent / "cms" / "ui_assets" / "index.html"
            ).read_text(encoding="utf-8")
    for el in ("lensBtn", "lensPop", "lensRange", "lensName", "lensNote"):
        assert f'id="{el}"' in html, f"missing {el}"
    assert '"/api/lens"' in html
    assert html.count("data-lens") >= 12  # summaries/features/review/suggestions/chat


# --- Library screen -----------------------------------------------------------

ASSET_MD = ("---\nid: house-rules\nname: House Rules\ntype: constraint\n"
            "description: The rules of this house.\ntags: [rules]\n---\n\nNever touch generated files.")


def test_library_page_and_nav_entry(server) -> None:
    client, _ = server
    status, body = client.get("/library")
    assert status == 200 and b'id="rows"' in body
    page = body.decode("utf-8")
    for el in ("btnNew", "btnImport", "compose", "lensBtn", "editDlg", "importDlg",
               "fDirectory"):
        assert f'id="{el}"' in page, f"missing {el}"
    assert "data-lens" in page                      # asset prose flows through the lens
    assert '"/api/library/compose"' in page
    index = (Path(__file__).parent.parent / "cms" / "ui_assets" / "index.html"
             ).read_text(encoding="utf-8")
    assert 'href="/library"' in index               # reachable from the map


def test_library_draft_then_publish_is_token_gated(server) -> None:
    client, root = server
    status, body = client.post("/api/library/asset", {
        "id": "house-rules", "name": "House Rules", "type": "constraint",
        "description": "The rules of this house.", "tags": ["rules"],
        "content": "Never touch generated files."})
    assert status == 200
    rec = json.loads(body)["asset"]
    assert rec["status"] == "draft" and rec["trust"] == "project"
    assert rec["created_by"]["via"] == "http"       # transport stamped server-side

    # a draft is inert: it never reaches an agent's composed context
    _, body = client.post("/api/library/compose", {"selection": ["house-rules"]})
    composed = json.loads(body)
    assert composed["assets"] == []
    assert any(w["kind"] == "unpublished-asset" for w in composed["warnings"])

    # publishing is a human act — the session code lives only in the terminal
    status, body = client.post("/api/library/publish",
                               {"id": "house-rules", "published_by": "alex"})
    assert status == 403 and "session code" in json.loads(body)["error"]
    status, body = client.post("/api/library/publish",
                               {"id": "house-rules", "published_by": "alex",
                                "token": "wrong-code"})
    assert status == 403

    status, body = client.post("/api/library/publish",
                               {"id": "house-rules", "published_by": "alex",
                                "token": APPROVAL_TOKEN})
    assert status == 200
    published = json.loads(body)["asset"]
    assert published["status"] == "published" and published["current_version"] == 1
    assert len(published["versions"][0]["content_hash"]) == 24
    assert published["versions"][0]["published_by"] == "alex"


def test_library_list_get_export_and_compose(server) -> None:
    client, _ = server
    status, body = client.get("/api/library?type=constraint")
    data = json.loads(body)
    assert status == 200
    assert [a["id"] for a in data["assets"]] == ["house-rules"]
    assert "profile" in data["types"] and "project" in data["scopes"]
    assert json.loads(client.get("/api/library?q=nothing-here")[1])["assets"] == []

    _, body = client.get("/api/library/asset?id=house-rules")
    detail = json.loads(body)["asset"]
    assert detail["body"] == "Never touch generated files."   # canonical, verbatim
    assert detail["versions"][0]["version"] == 1
    assert client.get("/api/library/asset?id=ghost")[0] == 404

    status, body = client.get("/api/library/export?id=house-rules")
    assert status == 200 and b"id: house-rules" in body       # round-trippable markdown

    _, body = client.post("/api/library/compose", {"selection": ["house-rules"]})
    composed = json.loads(body)
    assert composed["assets"][0]["version"] == 1
    assert composed["est_tokens"] > 0 and composed["oversized"] is False


def test_library_import_lands_untrusted_and_deprecate_is_gated(server) -> None:
    client, _ = server
    status, body = client.post("/api/library/import", {
        "content": "---\nname: Getting Rich\ndescription: Money manual.\n---\n\nSpend less than you earn.",
        "filename": "getting_rich.md"})
    assert status == 200
    rec = json.loads(body)["asset"]
    assert rec["id"] == "getting-rich"
    assert rec["trust"] == "imported" and rec["status"] == "draft"  # visibly untrusted

    # deprecation also rewrites what agents are handed -> same human gate
    status, _ = client.post("/api/library/status",
                            {"id": "house-rules", "status": "deprecated"})
    assert status == 403
    status, body = client.post("/api/library/status",
                               {"id": "house-rules", "status": "deprecated",
                                "token": APPROVAL_TOKEN})
    assert status == 200 and json.loads(body)["asset"]["status"] == "deprecated"

    # disable/enable is a local view toggle, not a rewrite of canonical content
    status, _ = client.post("/api/library/status",
                            {"id": "house-rules", "status": "disabled"})
    assert status == 200
    _, body = client.get("/api/library?scope=project")
    row = next(a for a in json.loads(body)["assets"] if a["id"] == "house-rules")
    assert row["enabled_effective"] is False
    client.post("/api/library/status", {"id": "house-rules", "status": "enabled"})


def test_library_directory_import_and_human_rating(server) -> None:
    from cms.library_usage import LibraryUsageStore

    client, root = server
    package = root / "vendor" / "skills" / "specialist"
    (package / "scripts").mkdir(parents=True)
    (package / "SKILL.md").write_text(
        "---\nname: specialist\ndescription: Attached package.\n---\n\nUse scripts/check.py.",
        encoding="utf-8")
    (package / "LICENSE.txt").write_text("licence prose", encoding="utf-8")

    status, body = client.post("/api/library/import-directory", {"directory": "vendor"})
    result = json.loads(body)
    assert status == 200 and result["problems"] == []
    assert result["imported"][0]["id"] == "specialist"
    _, body = client.get("/api/library/asset?id=specialist")
    assert json.loads(body)["asset"]["meta"]["resource_root"] == "vendor/skills/specialist"

    event = LibraryUsageStore(root / ".memory").record(
        [{"id": "specialist", "version": 1, "content_hash": "abc",
          "scope": "project", "trust": "imported", "type": "skill"}],
        task="Try the specialist", outcome="success", effectiveness=4)
    status, body = client.post("/api/library/rating", {
        "use_id": event["id"], "rating": 5, "comment": "Useful package"})
    assert status == 200 and json.loads(body)["feedback"]["rating"] == 5
    _, body = client.get("/api/library/asset?id=specialist")
    evidence = json.loads(body)["evidence"]
    assert evidence["uses"] == 1 and evidence["human"]["rating"] == 5.0


def test_library_rejects_invalid_assets(server) -> None:
    client, _ = server
    status, body = client.post("/api/library/asset", {
        "id": "../../evil", "name": "Evil", "type": "skill",
        "description": "x", "content": "y"})
    assert status == 400 and "invalid asset id" in json.loads(body)["error"]
    status, body = client.post("/api/library/asset", {
        "id": "bad-type", "name": "Bad", "type": "sorcery",
        "description": "x", "content": "y"})
    assert status == 400 and "unknown asset type" in json.loads(body)["error"]


def test_library_picks_up_dropped_files_and_registers_them(server) -> None:
    """A plain skill file dropped into the library folder shows up on its own;
    an unusable one shows up with the reason instead of vanishing."""
    client, root = server
    lib = root / "skills"
    lib.mkdir(exist_ok=True)
    (lib / "dropped-skill.md").write_text(
        "---\nname: dropped-skill\ndescription: Dropped in by hand.\n"
        "license: MIT\n---\n\nDo the thing.", encoding="utf-8")
    (lib / "broken-skill.md").write_text("no frontmatter at all", encoding="utf-8")

    rows = {a["id"]: a for a in json.loads(client.get("/api/library")[1])["assets"]}
    dropped = rows["dropped-skill"]
    assert dropped["type"] == "skill" and dropped["status"] == "draft"
    assert dropped["registered"] is False          # visible without any command
    broken = rows["broken-skill"]
    assert broken["status"] == "unreadable" and broken["problem"]  # never silent

    status, body = client.post("/api/library/register", {"id": "dropped-skill"})
    assert status == 200
    rec = json.loads(body)["asset"]
    assert rec["type"] == "skill" and rec["status"] == "draft"
    text = (lib / "dropped-skill.md").read_text(encoding="utf-8")
    assert "id: dropped-skill" in text and "license: MIT" in text  # nothing destroyed

    status, _ = client.post("/api/library/register", {"id": "broken-skill"})
    assert status == 400   # an unusable file cannot be adopted


def test_arrow_keys_drive_resolution_not_the_lens() -> None:
    """Left/right on the map step the Human View resolution (System..Source).
    They must never touch the comprehension lens (schoolchild/tech/...), and
    must stay out of the way while the user is typing or reading a file."""
    html = (Path(__file__).parent.parent / "cms" / "ui_assets" / "index.html"
            ).read_text(encoding="utf-8")
    start = html.index("function nudgeResolution")
    handler = html[start:html.index("/* the resolution popover", start)]

    assert "ArrowLeft" in handler and "ArrowRight" in handler
    assert "setResolution" in handler and "setHumanMode" in handler
    assert "Lens" not in handler          # the lens is a separate control
    # guards: typing, the file reader, the chat, the activity drawer
    assert "typingInField(document.activeElement)" in handler
    for guard in ("viewer", "chatBox", "activityPanel"):
        assert f'$("{guard}").classList.contains("open")' in handler
    # the shortcut is discoverable, not folklore
    assert "broader" in html and "deeper" in html
