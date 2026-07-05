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
