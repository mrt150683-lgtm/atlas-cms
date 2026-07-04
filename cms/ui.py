"""Local web UI for the memory layer — `cms ui`.

Serves a single-page viewer (file tree + knowledge graph + inspector) from
``ui_assets/index.html`` plus a tiny JSON API over the ``.memory/`` artifacts.
Binds to 127.0.0.1 only; no external dependencies.
"""

from __future__ import annotations

import json
import sys
import threading
import webbrowser
from dataclasses import asdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from . import config
from .config import LANGUAGE_BY_EXTENSION
from .memory import CodebaseMemory

if getattr(sys, "frozen", False):  # PyInstaller bundle
    _ASSETS_DIR = Path(sys._MEIPASS) / "cms" / "ui_assets"  # type: ignore[attr-defined]
else:
    _ASSETS_DIR = Path(__file__).parent / "ui_assets"


class _MemoryCache:
    """Lazy-loaded CodebaseMemory, reloaded when graph.json changes on disk."""

    def __init__(self, graph_path: Path) -> None:
        self.graph_path = graph_path
        self._memory: CodebaseMemory | None = None
        self._mtime: float = 0.0
        self._lock = threading.Lock()

    def get(self) -> CodebaseMemory | None:
        with self._lock:
            try:
                mtime = self.graph_path.stat().st_mtime
            except OSError:
                return None
            if self._memory is None or mtime != self._mtime:
                self._memory = CodebaseMemory.load(self.graph_path)
                self._mtime = mtime
            return self._memory


def make_handler(root: Path, cache: _MemoryCache):
    memory_dir = root / config.MEMORY_DIR_NAME

    class Handler(BaseHTTPRequestHandler):
        server_version = "CMS-UI/0.1"
        protocol_version = "HTTP/1.1"  # keep-alive; every response sends Content-Length

        def log_message(self, *args) -> None:  # keep the terminal quiet
            pass

        def _send(self, status: int, body: bytes, content_type: str) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, data, status: int = 200) -> None:
            self._send(status, json.dumps(data).encode("utf-8"), "application/json; charset=utf-8")

        def _error(self, status: int, message: str) -> None:
            self._json({"error": message}, status)

        def do_GET(self) -> None:  # noqa: N802 (http.server API)
            url = urlparse(self.path)
            query = parse_qs(url.query)
            try:
                if url.path in ("/", "/index.html"):
                    page = (_ASSETS_DIR / "index.html").read_bytes()
                    self._send(200, page, "text/html; charset=utf-8")
                elif url.path == "/api/graph":
                    self._serve_memory_file("graph.json")
                elif url.path == "/api/tree":
                    self._serve_memory_file("clean_tree.json")
                elif url.path == "/api/meta":
                    self._json({"project": root.name, "root": str(root)})
                elif url.path == "/api/query":
                    self._query(query)
                elif url.path == "/api/activity":
                    self._activity(query)
                elif url.path == "/api/source":
                    self._source(query)
                else:
                    self._error(404, "not found")
            except BrokenPipeError:
                pass
            except Exception as exc:  # surface server bugs to the client, not a hang
                self._error(500, f"{type(exc).__name__}: {exc}")

        def _serve_memory_file(self, name: str) -> None:
            path = memory_dir / name
            if not path.is_file():
                self._error(404, f"{name} not found — run `cms run-all` first")
                return
            self._send(200, path.read_bytes(), "application/json; charset=utf-8")

        def _query(self, query: dict) -> None:
            text = (query.get("q") or [""])[0].strip()
            top_k = int((query.get("k") or ["8"])[0])
            if not text:
                self._json({"results": []})
                return
            memory = cache.get()
            if memory is None:
                self._error(404, "no graph.json — run `cms run-all` first")
                return
            results = memory.query_intent(text, top_k=top_k)
            self._json({"results": [asdict(r) for r in results]})

        def _activity(self, query: dict) -> None:
            import time as _time

            from .activity import read_activity

            since = float((query.get("since") or ["0"])[0])
            self._json({"now": _time.time(), "events": read_activity(memory_dir, since)})

        def _source(self, query: dict) -> None:
            rel = (query.get("path") or [""])[0]
            target = (root / rel).resolve()
            if root not in target.parents:
                self._error(403, "path outside project root")
                return
            if target.suffix.lower() not in LANGUAGE_BY_EXTENSION or not target.is_file():
                self._error(404, "not a scanned source file")
                return
            text = target.read_text(encoding="utf-8", errors="replace")
            self._json({"path": rel, "text": text})

    return Handler


# @memory:feature:MemoryViewer
# @memory:connects:QueryEngine, GitHistoryLayer, ActivityPulse, FeatureTracing
# @memory:summary:Local web UI — explorer, force-directed knowledge graph with heat overlay and MCP pulses, inspector with summaries/anchors/flows, intent search.
def serve(root: Path, port: int = 7717, open_browser: bool = True) -> None:
    root = root.resolve()
    cache = _MemoryCache(root / config.MEMORY_DIR_NAME / "graph.json")
    server = ThreadingHTTPServer(("127.0.0.1", port), make_handler(root, cache))
    url = f"http://127.0.0.1:{port}"
    print(f"CMS UI serving {root.name} at {url}  (Ctrl+C to stop)")
    if open_browser:
        threading.Timer(0.4, webbrowser.open, args=(url,)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nCMS UI stopped.")
    finally:
        server.server_close()
