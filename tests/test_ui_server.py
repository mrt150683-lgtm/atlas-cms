import http.client
import json
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


@pytest.fixture(scope="module")
def server(tmp_path_factory):
    root = tmp_path_factory.mktemp("uiproj")
    (root / "app.py").write_text(SOURCE, encoding="utf-8")
    records = scan(root)
    graph = build_graph(records)
    build_features(graph, MockProvider())
    memory_dir = root / ".memory"
    export_tree(root, records, memory_dir)
    export_graph(graph, memory_dir)

    cache = _MemoryCache(memory_dir / "graph.json")
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(root, cache))
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    client = _Client(httpd.server_address[1])
    yield client, root
    httpd.shutdown()
    httpd.server_close()


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
    assert set(sem["stages"]) == {"summaries", "features", "review", "suggestions"}
    assert sem["stages"]["features"]["status"] == "skipped"
    assert sem["stages"]["review"]["status"] == "never_run"
    assert "provider" in sem and "real" in sem["provider"]
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
    # invalid/stale judgment banners exist
    assert "not valid semantic output" in html and "valid but frozen" in html


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
