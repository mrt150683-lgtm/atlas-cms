"""Sentinel HTTP endpoints — the /sentinel page and /api/sentinel/* API."""

import http.client
import json
import threading
import time
from http.server import ThreadingHTTPServer

import pytest

from cms.exporter import export_graph
from cms.graph_builder import build_graph
from cms.providers import MockProvider
from cms.scanner import scan
from cms.summarizer import generate_summaries
from cms.tree_export import export_tree
from cms.ui import _MemoryCache, make_handler

SOURCE = '''\
# @memory:feature:Greeting
def greet(name):
    return name
'''


class _Client:
    """One persistent keep-alive connection (see test_ui_server for rationale)."""

    def __init__(self, port: int) -> None:
        self.port = port
        self.conn = None
        self.lock = threading.Lock()
        self._connect(deadline=60)

    def _connect(self, deadline: float) -> None:
        end = time.time() + deadline
        while True:
            try:
                conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=15)
                conn.request("GET", "/api/meta")
                conn.getresponse().read()
                self.conn = conn
                return
            except Exception:
                if time.time() > end:
                    raise
                time.sleep(0.5)

    def request(self, method: str, path: str, body: dict | None = None):
        payload = json.dumps(body).encode() if body is not None else None
        headers = {"Content-Type": "application/json"} if payload else {}
        with self.lock:
            try:
                self.conn.request(method, path, body=payload, headers=headers)
                resp = self.conn.getresponse()
                return resp.status, resp.read()
            except Exception:
                self.conn.close()
                self._connect(deadline=30)
                self.conn.request(method, path, body=payload, headers=headers)
                resp = self.conn.getresponse()
                return resp.status, resp.read()

    def get(self, path: str):
        return self.request("GET", path)

    def post(self, path: str, body: dict | None = None):
        return self.request("POST", path, body or {})


@pytest.fixture(scope="module")
def server(tmp_path_factory):
    root = tmp_path_factory.mktemp("sentinelproj")
    (root / "app.py").write_text(SOURCE, encoding="utf-8")
    records = scan(root)
    graph = build_graph(records)
    generate_summaries(graph, root, MockProvider())
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


def test_sentinel_page_served(server):
    client, _ = server
    status, body = client.get("/sentinel")
    assert status == 200 and b"HERMES SENTINEL" in body


def test_notes_crud_roundtrip(server):
    client, _ = server
    # empty to start
    status, body = client.get("/api/notes?path=app.py")
    assert status == 200 and json.loads(body)["notes"] == []
    # create
    status, body = client.post("/api/notes", {
        "path": "app.py", "quote": "def greet", "before": "# x\n",
        "note": "entry point", "color": "blue", "mode": "source"})
    assert status == 200
    note = json.loads(body)["note"]
    assert note["id"] and note["color"] == "blue" and note["quote"] == "def greet"
    # empty quote is rejected
    status, _ = client.post("/api/notes", {"path": "app.py", "quote": "  ", "note": "x"})
    assert status == 400
    # listed for the file and in the global counts
    _, body = client.get("/api/notes?path=app.py")
    assert len(json.loads(body)["notes"]) == 1
    _, body = client.get("/api/notes")
    assert json.loads(body)["counts"].get("app.py") == 1
    # update
    status, body = client.post("/api/notes/update", {"id": note["id"], "note": "revised", "color": "green"})
    assert status == 200 and json.loads(body)["note"]["note"] == "revised"
    # delete
    status, _ = client.post("/api/notes/delete", {"id": note["id"]})
    assert status == 200
    _, body = client.get("/api/notes?path=app.py")
    assert json.loads(body)["notes"] == []
    status, _ = client.post("/api/notes/delete", {"id": "nope"})
    assert status == 404


def test_notes_persist_across_store_instances(tmp_path):
    from cms.notes import NotesStore

    store = NotesStore(tmp_path / ".memory")
    store.add(path="a.py", quote="x = 1", note="magic number")
    # a fresh instance (simulating restart) still sees it
    assert len(NotesStore(tmp_path / ".memory").for_path("a.py")) == 1


def test_latest_empty_before_any_scan(server):
    client, _ = server
    status, body = client.get("/api/sentinel/latest")
    data = json.loads(body)
    assert status == 200 and data["scan"] is None and data["findings"] == {}


def test_scan_runs_findings_update_and_export(server):
    client, _ = server
    status, body = client.post("/api/sentinel/scan")
    assert status == 200 and json.loads(body)["started"] is True

    deadline = time.time() + 180
    while time.time() < deadline:
        _, body = client.get("/api/sentinel/scan-status")
        state = json.loads(body)
        if not state["running"]:
            break
        time.sleep(1)
    assert not state["running"], "scan did not finish in time"
    assert not state["error"], state["error"]

    _, body = client.get("/api/sentinel/latest")
    data = json.loads(body)
    assert data["scan"]["scan_id"].startswith("scan-")
    assert data["scan"]["gate"] is not None
    assert data["history"]
    assert isinstance(data["findings"], dict) and data["findings"]

    # finding status update: false_positive requires a reason
    some_id = next(iter(data["findings"].values()))["id"]
    status, _ = client.post("/api/sentinel/finding",
                            {"id": some_id, "status": "false_positive", "reason": ""})
    assert status == 400
    status, body = client.post("/api/sentinel/finding",
                               {"id": some_id, "status": "acknowledged", "reason": ""})
    assert status == 200 and json.loads(body)["finding"]["status"] == "acknowledged"
    status, _ = client.post("/api/sentinel/finding",
                            {"id": "SEN-nope", "status": "resolved", "reason": ""})
    assert status == 404

    # exports
    status, body = client.get("/api/sentinel/export?format=md")
    assert status == 200 and b"Hermes Sentinel Report" in body
    status, body = client.get("/api/sentinel/export?format=json")
    assert status == 200 and json.loads(body)["bug_reports"]
