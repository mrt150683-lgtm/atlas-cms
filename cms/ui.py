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


# @memory:feature:MemoryViewer
# @memory:summary:HTTP request handler for the viewer — serves the single-page UI and the JSON API (graph, tree, query, source with traversal guard, activity feed, prompt export, sentinel scans and findings).
def make_handler(root: Path, cache: _MemoryCache):
    memory_dir = root / config.MEMORY_DIR_NAME
    # one Sentinel scan at a time, shared across requests
    sentinel_state = {"running": False, "error": "", "finished_at": 0.0}
    sentinel_lock = threading.Lock()
    build_state = {"running": False, "error": "", "message": "", "finished_at": 0.0}
    build_lock = threading.Lock()
    # staleness: if cms/*.py changes on disk after boot, this server is running
    # old code (HTML is disk-served and updates live, Python routes don't) — the
    # UI surfaces this so a relaunch is obvious instead of a cryptic 404.
    _pkg_dir = Path(__file__).resolve().parent

    def _newest_code_mtime() -> float:
        try:
            return max((p.stat().st_mtime for p in _pkg_dir.rglob("*.py")), default=0.0)
        except OSError:
            return 0.0

    _boot_code_mtime = _newest_code_mtime()

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
                elif url.path in ("/sentinel", "/sentinel.html"):
                    page = (_ASSETS_DIR / "sentinel.html").read_bytes()
                    self._send(200, page, "text/html; charset=utf-8")
                elif url.path in ("/setup", "/setup.html"):
                    page = (_ASSETS_DIR / "setup.html").read_bytes()
                    self._send(200, page, "text/html; charset=utf-8")
                elif url.path in ("/discovery", "/constellation"):
                    page = (_ASSETS_DIR / "constellation.html").read_bytes()
                    self._send(200, page, "text/html; charset=utf-8")
                elif url.path == "/api/fusion":
                    from .fuse import fusion_history, fusion_staleness, load_fusion

                    report = load_fusion()
                    if report is None:
                        self._json({"report": None,
                                    "reason": "no fusion report yet — run `cms fuse`"})
                    else:
                        self._json({"report": report,
                                    "stale_members": fusion_staleness(report),
                                    "refinements": fusion_history()})
                elif url.path == "/api/scout":
                    from .scout import load_cards, load_suggestions

                    self._json({"cards": sorted(load_cards().values(),
                                                key=lambda c: c.get("project_dir", "")),
                                "suggestions": sorted(load_suggestions().values(),
                                                      key=lambda s: (s["status"], s["kind"]))})
                elif url.path == "/api/chat":
                    from .chat import load_transcript

                    self._json({"transcript": load_transcript(root)})
                elif url.path == "/api/projects":
                    from . import semantic_state as sstate
                    from .fuse import load_registry

                    out = []
                    for root_str, meta in (load_registry().get("projects") or {}).items():
                        proj = Path(root_str)
                        if not (proj / config.MEMORY_DIR_NAME / "graph.json").is_file():
                            continue
                        st = sstate.load_state(proj / config.MEMORY_DIR_NAME)
                        out.append({
                            "name": meta.get("name") or proj.name,
                            "root": root_str,
                            "current": proj.resolve() == root.resolve(),
                            "pipeline": sstate.pipeline_status(st)["status"],
                            "last_built": meta.get("last_built"),
                        })
                    self._json({"projects": sorted(out, key=lambda p: p["name"].lower())})
                elif url.path == "/api/brainstorm":
                    from .brainstorm import load_goals, load_ideas
                    from .fuse import build_card, load_registry

                    projects = []
                    for root_str, meta in (load_registry().get("projects") or {}).items():
                        card = build_card(Path(root_str))
                        if card.get("ready"):
                            projects.append({"name": card["name"], "root": root_str})
                    self._json({
                        "ideas": sorted(load_ideas().values(),
                                        key=lambda i: i["created_at"], reverse=True),
                        "goals": load_goals(),
                        "projects": sorted(projects, key=lambda p: p["name"]),
                    })
                elif url.path == "/api/dirtree":
                    self._dirtree()
                elif url.path == "/api/scope":
                    self._scope_get()
                elif url.path == "/api/sources":
                    from .sources import analyze_sources
                    self._json(analyze_sources(root))
                elif url.path == "/api/build-status":
                    self._json(dict(build_state))
                elif url.path == "/api/sentinel/latest":
                    self._sentinel_latest()
                elif url.path == "/api/sentinel/scan-status":
                    self._json(dict(sentinel_state))
                elif url.path == "/api/sentinel/export":
                    self._sentinel_export(query)
                elif url.path == "/api/graph":
                    self._serve_memory_file("graph.json")
                elif url.path == "/api/tree":
                    self._serve_memory_file("clean_tree.json")
                elif url.path == "/api/meta":
                    self._json({"project": root.name, "root": str(root),
                                "stale": _newest_code_mtime() > _boot_code_mtime + 1.0})
                elif url.path == "/api/semantic":
                    self._semantic()
                elif url.path == "/api/query":
                    self._query(query)
                elif url.path == "/api/activity":
                    self._activity(query)
                elif url.path == "/api/prompt":
                    self._prompt(query)
                elif url.path == "/api/source":
                    self._source(query)
                elif url.path == "/api/notes":
                    self._notes_list(query)
                else:
                    self._error(404, "not found")
            except BrokenPipeError:
                pass
            except Exception as exc:  # surface server bugs to the client, not a hang
                self._error(500, f"{type(exc).__name__}: {exc}")

        def do_POST(self) -> None:  # noqa: N802 (http.server API)
            url = urlparse(self.path)
            try:
                length = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(length) or b"{}") if length else {}
                if url.path == "/api/sentinel/scan":
                    self._sentinel_scan()
                elif url.path == "/api/sentinel/finding":
                    self._sentinel_finding(body)
                elif url.path == "/api/notes":
                    self._notes_add(body)
                elif url.path == "/api/notes/update":
                    self._notes_update(body)
                elif url.path == "/api/notes/delete":
                    self._notes_delete(body)
                elif url.path == "/api/scope":
                    self._scope_set(body)
                elif url.path == "/api/build":
                    self._build()
                elif url.path == "/api/bundle/export":
                    self._bundle_export(body)
                elif url.path == "/api/pick-folder":
                    self._pick_folder()
                elif url.path == "/api/switch-root":
                    self._switch_root(body)
                elif url.path == "/api/ignore-add":
                    from .sources import add_ignore_pattern
                    ok = add_ignore_pattern(root, str(body.get("pattern") or ""))
                    self._json({"added": ok, "pattern": str(body.get("pattern") or "")})
                elif url.path == "/api/scout/status":
                    from .scout import ScoutError, set_suggestion_status

                    try:
                        s = set_suggestion_status(str(body.get("id") or ""),
                                                  str(body.get("status") or ""))
                        self._json({"updated": True, "suggestion": s})
                    except ScoutError as exc:
                        self._json({"updated": False, "error": str(exc)}, 400)
                elif url.path == "/api/chat":
                    from .activity import log_activity
                    from .chat import ChatError, ask, load_transcript
                    from .providers import get_provider

                    try:
                        entry = ask(root, str(body.get("question") or ""),
                                    get_provider(None),
                                    history=load_transcript(root, limit=6))
                        log_activity(memory_dir, "ask_codebase",
                                     entry["evidence_nodes"],
                                     label=entry["q"][:120])
                        self._json({"answer": entry["a"],
                                    "evidence_nodes": entry["evidence_nodes"],
                                    "matched_features": entry["matched_features"],
                                    "model": entry["model"]})
                    except ChatError as exc:
                        self._json({"error": str(exc)}, 400)
                elif url.path == "/api/brainstorm/generate":
                    from .brainstorm import BrainstormError, generate_ideas
                    from .providers import get_provider

                    try:
                        new = generate_ideas(
                            get_provider(None),
                            temperature=float(body.get("temperature", 1.0)),
                            project_root=body.get("project_root") or None,
                        )
                        self._json({"generated": len(new), "ideas": new})
                    except BrainstormError as exc:
                        self._json({"generated": 0, "error": str(exc)}, 400)
                elif url.path == "/api/brainstorm/rate":
                    from .brainstorm import BrainstormError, rate_idea

                    try:
                        idea = rate_idea(str(body.get("id") or ""),
                                         str(body.get("verdict") or ""))
                        self._json({"updated": True, "idea": idea})
                    except BrainstormError as exc:
                        self._json({"updated": False, "error": str(exc)}, 400)
                elif url.path == "/api/brainstorm/goals":
                    from .brainstorm import BrainstormError, add_goal, remove_goal

                    try:
                        if body.get("remove"):
                            goals = remove_goal(str(body["remove"]))
                        else:
                            goals = add_goal(str(body.get("text") or ""))
                        self._json({"goals": goals})
                    except BrainstormError as exc:
                        self._json({"error": str(exc)}, 400)
                else:
                    self._error(404, "not found")
            except BrokenPipeError:
                pass
            except json.JSONDecodeError:
                self._error(400, "invalid JSON body")
            except Exception as exc:
                self._error(500, f"{type(exc).__name__}: {exc}")

        # ── Hermes Sentinel API ──────────────────────────────────────

        def _sentinel_store(self):
            from .sentinel.store import SentinelStore

            return SentinelStore(memory_dir)

        def _sentinel_latest(self) -> None:
            store = self._sentinel_store()
            self._json({
                "scan": store.latest_scan(),
                "findings": store.load_findings(),
                "history": store.scan_history(),
                "state": dict(sentinel_state),
            })

        def _sentinel_scan(self) -> None:
            with sentinel_lock:
                if sentinel_state["running"]:
                    self._json({"started": False, "reason": "a scan is already running"}, 409)
                    return
                sentinel_state.update(running=True, error="")

            def worker() -> None:
                import time as _time

                try:
                    from .sentinel.runner import run_scan

                    run_scan(root)
                except Exception as exc:
                    sentinel_state["error"] = f"{type(exc).__name__}: {exc}"
                finally:
                    sentinel_state.update(running=False, finished_at=_time.time())

            threading.Thread(target=worker, daemon=True, name="cms-sentinel-scan").start()
            self._json({"started": True})

        def _sentinel_finding(self, body: dict) -> None:
            finding_id = str(body.get("id") or "")
            status = str(body.get("status") or "")
            reason = str(body.get("reason") or "")
            try:
                updated = self._sentinel_store().set_status(finding_id, status, reason)
            except ValueError as exc:
                self._error(400, str(exc))
                return
            if updated is None:
                self._error(404, f"no finding {finding_id!r}")
                return
            self._json({"finding": updated})

        def _sentinel_export(self, query: dict) -> None:
            from .sentinel.reports import export_json, export_markdown

            store = self._sentinel_store()
            fmt = (query.get("format") or ["md"])[0]
            scan, findings = store.latest_scan(), store.load_findings()
            if fmt == "json":
                self._send(200, export_json(scan, findings).encode("utf-8"),
                           "application/json; charset=utf-8")
            else:
                self._send(200, export_markdown(scan, findings).encode("utf-8"),
                           "text/markdown; charset=utf-8")

        # ── file annotations (notes) ────────────────────────────────

        # ── codebase scope + portable bundle ────────────────────────

        def _dirtree(self) -> None:
            from .scope import build_dir_tree, load_scope

            self._json({
                "project": root.name,
                "root": str(root),
                "tree": build_dir_tree(root),
                "scope": sorted(load_scope(root) or []),
                "has_memory": (memory_dir / "graph.json").is_file(),
            })

        def _scope_get(self) -> None:
            from .scope import load_scope

            self._json({"include": sorted(load_scope(root) or [])})

        def _scope_set(self, body: dict) -> None:
            from .scope import clear_scope, save_scope

            include = [str(x) for x in (body.get("include") or [])]
            if include:
                save_scope(root, include)
            else:
                clear_scope(root)
            self._json({"saved": True, "count": len(include)})

        def _kick_build(self, full: bool) -> bool:
            """Start a background pipeline build for the CURRENT root. Returns
            False if one is already running."""
            with build_lock:
                if build_state["running"]:
                    return False
                build_state.update(running=True, error="", message="starting…")
            target_root = root  # snapshot (root may be rebound by a later switch)

            def worker() -> None:
                import time as _time

                done_msg = "done"
                try:
                    from .providers import get_provider
                    from .update import ensure_judgment, incremental_update

                    def echo(msg: str) -> None:
                        build_state["message"] = str(msg)[:200]

                    provider = get_provider(None)
                    incremental_update(target_root, provider, echo=echo, full=full)
                    # a new project's first build should trigger EVERY module —
                    # review + ROI suggestions too, not just the map
                    ensure_judgment(target_root, provider, echo=echo)
                    if provider.name == "mock":
                        done_msg = ("done — mock provider: AI feature discovery, review and "
                                    "suggestions were skipped (set an API key, then rebuild)")
                except Exception as exc:
                    build_state["error"] = f"{type(exc).__name__}: {exc}"
                finally:
                    build_state.update(running=False, finished_at=_time.time(), message=done_msg)

            threading.Thread(target=worker, daemon=True, name="cms-build").start()
            return True

        def _build(self) -> None:
            if self._kick_build(full=True):
                self._json({"started": True})
            else:
                self._json({"started": False, "reason": "a build is already running"}, 409)

        def _bundle_export(self, body: dict) -> None:
            import tempfile

            from .bundle import default_bundle_name, export_bundle

            if not (memory_dir / "graph.json").is_file():
                self._error(409, "no memory yet — build it first")
                return
            include_source = bool(body.get("include_source"))
            tmp = Path(tempfile.gettempdir()) / default_bundle_name(root)
            try:
                out = export_bundle(root, out_path=tmp, include_source=include_source)
            except Exception as exc:
                self._error(500, f"{type(exc).__name__}: {exc}")
                return
            data = out.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "application/zip")
            self.send_header("Content-Disposition", f'attachment; filename="{out.name}"')
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            try:
                self.wfile.write(data)
            except BrokenPipeError:
                pass

        def _pick_folder(self) -> None:
            from .picker import pick_folder

            chosen = pick_folder()
            self._json({"path": chosen or ""})

        def _switch_root(self, body: dict) -> None:
            """Point this running server at a different codebase (live rebind)."""
            nonlocal root, memory_dir, cache
            from .scanner import scan

            path = str(body.get("path") or "").strip().strip('"').strip("'")
            if not path:
                self._json({"switched": False, "reason": "no folder given"})
                return
            new_root = Path(path).expanduser()
            if not new_root.is_dir():
                self._error(400, f"not a folder: {new_root}")
                return
            new_root = new_root.resolve()
            if not scan(new_root):
                self._error(400, "no recognisable source files in that folder")
                return
            root = new_root
            memory_dir = root / config.MEMORY_DIR_NAME
            cache = _MemoryCache(memory_dir / "graph.json")
            try:
                from .app import _save_workspace_root
                _save_workspace_root(root)  # persist so relaunch stays on this codebase
            except Exception:
                pass
            # process the new codebase now — no restart needed. Incremental: cheap
            # if already current, full build if it has no (or stale) memory.
            building = self._kick_build(full=False)
            self._json({
                "switched": True, "project": root.name, "root": str(root),
                "has_memory": (memory_dir / "graph.json").is_file(),
                "building": building,
            })

        def _notes_store(self):
            from .notes import NotesStore

            return NotesStore(memory_dir)

        def _notes_list(self, query: dict) -> None:
            path = (query.get("path") or [""])[0]
            store = self._notes_store()
            if path:
                self._json({"notes": store.for_path(path)})
            else:
                self._json({"notes": store.all(), "counts": store.counts()})

        def _notes_add(self, body: dict) -> None:
            try:
                note = self._notes_store().add(
                    path=str(body.get("path") or ""),
                    quote=str(body.get("quote") or ""),
                    note=str(body.get("note") or ""),
                    before=str(body.get("before") or ""),
                    color=str(body.get("color") or "amber"),
                    mode=str(body.get("mode") or "source"),
                )
            except ValueError as exc:
                self._error(400, str(exc))
                return
            self._json({"note": note})

        def _notes_update(self, body: dict) -> None:
            updated = self._notes_store().update(
                str(body.get("id") or ""),
                note=body.get("note"),
                color=body.get("color"),
            )
            if updated is None:
                self._error(404, "no such note")
                return
            self._json({"note": updated})

        def _notes_delete(self, body: dict) -> None:
            ok = self._notes_store().delete(str(body.get("id") or ""))
            self._json({"deleted": ok}, 200 if ok else 404)

        def _serve_memory_file(self, name: str) -> None:
            path = memory_dir / name
            if not path.is_file():
                self._error(404, f"{name} not found — run `cms run-all` first")
                return
            self._send(200, path.read_bytes(), "application/json; charset=utf-8")

        def _semantic(self) -> None:
            """Semantic-stage status for the UI: durable per-stage evidence
            plus live staleness/validity, so the frontend never reconstructs
            semantic completion from node existence. No secrets."""
            from . import semantic_state as sstate
            from .providers import get_provider

            state = sstate.load_state(memory_dir)
            payload = {
                "project": root.name, "root": str(root),
                "schema_version": state.get("schema_version"),
                "stages": {name: sstate.stage(state, name) for name in sstate.STAGES},
                "pipeline": sstate.pipeline_status(state),
                "build_running": bool(build_state.get("running")),
                "build_message": build_state.get("message", ""),
            }
            try:
                prov = get_provider(None)
                payload["provider"] = {"name": prov.name,
                                       "model": getattr(prov, "model", None),
                                       "real": prov.name != "mock"}
            except Exception:
                payload["provider"] = {"name": "unavailable", "model": None, "real": False}
            memory = cache.get()
            if memory is not None:
                payload["counts"] = sstate.feature_counts(memory.graph)
                payload["live"] = sstate.derive_staleness(state, memory.graph)
            self._json(payload)

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

        def _prompt(self, query: dict) -> None:
            from .prompt_export import export_prompt

            task = (query.get("task") or [""])[0].strip()
            if not task:
                self._error(400, "task parameter required")
                return
            as_json = (query.get("format") or [""])[0] == "json"
            content, out = export_prompt(root, task, as_json=as_json)
            content_type = "application/json" if as_json else "text/plain"
            self._send(200, content.encode("utf-8"), f"{content_type}; charset=utf-8")

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
def serve(root: Path, port: int = 7717, open_browser: bool = True, open_path: str = "/") -> None:
    root = root.resolve()
    cache = _MemoryCache(root / config.MEMORY_DIR_NAME / "graph.json")
    server = ThreadingHTTPServer(("127.0.0.1", port), make_handler(root, cache))
    url = f"http://127.0.0.1:{port}"
    print(f"CMS UI serving {root.name} at {url}  (Ctrl+C to stop)")
    if open_browser:
        threading.Timer(0.4, webbrowser.open, args=(url + open_path,)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nCMS UI stopped.")
    finally:
        server.server_close()
