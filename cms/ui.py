"""Local web UI for the memory layer — `cms ui`.

Serves a single-page viewer (file tree + knowledge graph + inspector) from
``ui_assets/index.html`` plus a tiny JSON API over the ``.memory/`` artifacts.
Binds to 127.0.0.1 only; no external dependencies.
"""

from __future__ import annotations

import json
import os
import secrets
import sys
import threading
import webbrowser
from dataclasses import asdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from . import config
from .config import LANGUAGE_BY_EXTENSION
from .library import ASSET_TYPES as _ASSET_TYPES
from .memory import CodebaseMemory

_CLIENT_DISCONNECT_ERRORS = (BrokenPipeError, ConnectionAbortedError, ConnectionResetError)

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
    # Decision approvals need something a local agent cannot trivially replay:
    # a per-session code printed only to the launching terminal (the human's
    # channel). Env override exists for tests/headless setups. This is the
    # mechanism behind "approval is human-only" — not just tool-surface omission.
    approval_token = os.environ.get("CMS_APPROVAL_TOKEN") or secrets.token_hex(3)
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
                elif url.path in ("/library", "/library.html"):
                    page = (_ASSETS_DIR / "library.html").read_bytes()
                    self._send(200, page, "text/html; charset=utf-8")
                elif url.path in ("/ideas", "/ideas.html"):
                    page = (_ASSETS_DIR / "ideas.html").read_bytes()
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
                    from .chat import list_sessions, load_transcript

                    sid = (query.get("session") or [""])[0]
                    if sid:
                        from .chat import session_history

                        self._json({"transcript": session_history(root, sid, limit=50)})
                    else:
                        self._json({"sessions": list_sessions(root),
                                    "transcript": load_transcript(root)})
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
                                "flags": config.flags(),
                                "stale": _newest_code_mtime() > _boot_code_mtime + 1.0})
                elif url.path == "/api/semantic":
                    self._semantic()
                elif url.path == "/api/query":
                    self._query(query)
                elif url.path == "/api/impact":
                    self._impact(query)
                elif url.path == "/api/drift":
                    self._drift(query)
                elif url.path == "/api/activity":
                    self._activity(query)
                elif url.path == "/api/prompt":
                    self._prompt(query)
                elif url.path == "/api/source":
                    self._source(query)
                elif url.path == "/api/notes":
                    self._notes_list(query)
                elif url.path == "/api/annotations":
                    if not config.flags()["annotations"]:
                        self._error(403, "annotations are disabled (CMS_ANNOTATIONS=0)")
                        return
                    self._annotations_list(query)
                elif url.path == "/api/fidelity":
                    self._fidelity(query)
                elif url.path == "/api/decisions":
                    from .decisions import DecisionStore

                    store = DecisionStore(memory_dir, root=root)
                    self._json({"decisions": store.list(
                        feature=(query.get("feature") or [None])[0],
                        active_only=(query.get("active") or ["0"])[0] == "1")})
                elif url.path == "/api/flowreview":
                    if not config.flags()["flow_review"]:
                        self._error(403, "flow review is disabled (CMS_FLOW_REVIEW=0)")
                        return
                    self._flowreview_get(query)
                elif url.path == "/api/library":
                    self._library_list(query)
                elif url.path == "/api/library/asset":
                    self._library_asset(query)
                elif url.path == "/api/library/export":
                    self._library_export(query)
                elif url.path == "/api/ideas":
                    self._ideas_list(query)
                elif url.path == "/api/ideas/item":
                    self._ideas_item(query)
                elif url.path == "/api/ideas/map":
                    from .ideas import default_journal

                    self._json(default_journal().map_data(
                        include_features=(query.get("features") or ["1"])[0] != "0",
                        feature_limit=int((query.get("limit") or ["120"])[0])))
                elif url.path == "/api/ideas/source":
                    from .ideas import default_journal

                    source = default_journal().get_source((query.get("id") or [""])[0])
                    self._json({"source": source} if source else {"error": "source not found"},
                               200 if source else 404)
                elif url.path == "/api/ideas/export":
                    from .ideas import default_journal

                    payload = json.dumps(default_journal().snapshot(), indent=2,
                                         ensure_ascii=False).encode("utf-8")
                    self._send(200, payload, "application/json; charset=utf-8")
                else:
                    self._error(404, "not found")
            except _CLIENT_DISCONNECT_ERRORS:
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
                elif url.path == "/api/annotations":
                    if not config.flags()["annotations"]:
                        self._error(403, "annotations are disabled (CMS_ANNOTATIONS=0)")
                        return
                    self._annotations_add(body)
                elif url.path == "/api/annotations/update":
                    self._annotations_update(body)
                elif url.path == "/api/annotations/archive":
                    self._annotations_archive(body)
                elif url.path in ("/api/decisions", "/api/decisions/approve",
                                  "/api/decisions/close"):
                    self._decisions_post(url.path, body)
                elif url.path == "/api/flowreview":
                    if not config.flags()["flow_review"]:
                        self._error(403, "flow review is disabled (CMS_FLOW_REVIEW=0)")
                        return
                    self._flowreview_post(body)
                elif url.path == "/api/verify":
                    self._verify(body)
                elif url.path == "/api/align":
                    self._align(body)
                elif url.path == "/api/feature/discover":
                    self._feature_discover(body)
                elif url.path == "/api/feature/confirm":
                    self._feature_confirm(body)
                elif url.path in ("/api/library/asset", "/api/library/publish",
                                  "/api/library/status", "/api/library/compose",
                                  "/api/library/import", "/api/library/register",
                                  "/api/library/import-directory", "/api/library/rating"):
                    self._library_post(url.path, body)
                elif url.path.startswith("/api/ideas/"):
                    self._ideas_post(url.path, body)
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

                    from .chat import session_history

                    sid = str(body.get("session") or "default")
                    try:
                        entry = ask(root, str(body.get("question") or ""),
                                    get_provider(None),
                                    history=session_history(root, sid),
                                    session=sid)
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
                elif url.path == "/api/lens":
                    from .lens import LensError, rewrite_batch
                    from .providers import get_provider

                    try:
                        self._json(rewrite_batch(root, str(body.get("level") or ""),
                                                 body.get("items") or [],
                                                 get_provider(None)))
                    except LensError as exc:
                        self._json({"error": str(exc)}, 400)
                elif url.path == "/api/explain":
                    from .explain import ExplainError, explain_nodes
                    from .providers import get_provider

                    memory = cache.get()
                    if memory is None:
                        self._error(404, "graph.json not found — run `cms run-all` first")
                        return
                    try:
                        self._json(explain_nodes(root, memory.graph,
                                                 body.get("items") or [],
                                                 get_provider(None),
                                                 force=bool(body.get("force"))))
                    except ExplainError as exc:
                        self._json({"error": str(exc)}, 400)
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
            except _CLIENT_DISCONNECT_ERRORS:
                pass
            except json.JSONDecodeError:
                self._error(400, "invalid JSON body")
            except Exception as exc:
                self._error(500, f"{type(exc).__name__}: {exc}")

        # ── Hermes Sentinel API ──────────────────────────────────────

        def _ideas_list(self, query: dict) -> None:
            from .ideas import default_journal

            journal = default_journal()
            self._json({
                "ideas": journal.search(
                    (query.get("q") or [""])[0],
                    project=(query.get("project") or [None])[0],
                    kind=(query.get("kind") or [None])[0],
                    status=(query.get("status") or [None])[0],
                    limit=int((query.get("limit") or ["100"])[0])),
                "candidates": journal.list_candidates(
                    status=(query.get("candidate_status") or [None])[0], limit=100),
                "events": journal.events(limit=30),
            })

        def _ideas_item(self, query: dict) -> None:
            from .ideas import default_journal

            idea_id = (query.get("id") or [""])[0]
            idea = default_journal().get_idea(idea_id)
            self._json({"idea": idea} if idea else {"error": "idea not found"},
                       200 if idea else 404)

        def _ideas_post(self, path: str, body: dict) -> None:
            from .ideas import IdeaError, default_journal

            journal = default_journal()
            try:
                if path == "/api/ideas/capture":
                    result = {"idea": journal.create_idea(
                        str(body.get("title") or ""),
                        overview=str(body.get("overview") or ""),
                        body=str(body.get("body") or ""),
                        kind=str(body.get("kind") or "concept"),
                        status=str(body.get("status") or "inbox"),
                        parent_id=body.get("parent_id") or None,
                        actor_kind="human")}
                elif path == "/api/ideas/update":
                    values = {key: body[key] for key in
                              ("title", "overview", "body", "kind", "status", "parent_id")
                              if key in body}
                    result = {"idea": journal.update_idea(
                        str(body.get("id") or ""), actor_kind="human", **values)}
                elif path == "/api/ideas/source":
                    result = {"source": journal.add_source(
                        str(body.get("content") or ""), idea_id=body.get("idea_id") or None,
                        source_type=str(body.get("source_type") or "brainstorm"),
                        title=str(body.get("title") or ""), uri=str(body.get("uri") or ""),
                        actor_kind="human")}
                elif path == "/api/ideas/relationship":
                    result = {"relationship": journal.add_relationship(
                        str(body.get("idea_id") or ""), str(body.get("target_type") or ""),
                        str(body.get("target_ref") or ""),
                        str(body.get("relation_type") or "relates_to"),
                        metadata=body.get("metadata") or {}, actor_kind="human")}
                elif path == "/api/ideas/generate":
                    from .providers import get_provider

                    result = journal.generate(
                        get_provider(None), mode=str(body.get("mode") or "journal"),
                        direction=str(body.get("direction") or ""),
                        project_roots=body.get("project_roots") or [],
                        feature_refs=body.get("feature_refs") or [],
                        idea_ids=body.get("idea_ids") or [],
                        surprise=float(body.get("surprise", 0.5)),
                        count=int(body.get("count", 6)), seed=body.get("seed"))
                elif path == "/api/ideas/join":
                    from .providers import get_provider

                    result = journal.join_dots(
                        get_provider(None), body.get("node_ids") or [],
                        points=body.get("points") or [],
                        surprise=float(body.get("surprise", 0.7)),
                        direction=str(body.get("direction") or ""),
                        seed=body.get("seed"), count=int(body.get("count", 4)))
                elif path == "/api/ideas/candidate":
                    result = {"candidate": journal.decide_candidate(
                        str(body.get("id") or ""), str(body.get("verdict") or ""),
                        parent_id=body.get("parent_id") or None,
                        merge_into=body.get("merge_into") or None)}
                else:
                    self._error(404, "not found")
                    return
            except IdeaError as exc:
                self._json({"error": str(exc)}, 400)
                return
            self._json(result)

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

        def _ann_store(self):
            from .annotations import AnnotationStore

            return AnnotationStore(memory_dir, root=root)

        def _annotations_list(self, query: dict) -> None:
            store = self._ann_store()
            rows = store.list(
                target=(query.get("target") or [None])[0],
                feature=(query.get("feature") or [None])[0],
                status=(query.get("status") or [None])[0],
                include_archived=(query.get("include_archived") or ["0"])[0] == "1",
            )
            self._json({"annotations": rows, "counts": store.counts()})

        def _annotations_add(self, body: dict) -> None:
            author = body.get("author") if isinstance(body.get("author"), dict) else {}
            # provenance honesty: the transport is recorded server-side, never
            # caller-asserted — a claimed author kind can be judged against it
            author = {**author, "via": "http"}
            try:
                entry = self._ann_store().add(
                    body.get("target"),
                    str(body.get("type") or "note"),
                    str(body.get("body") or ""),
                    author=author,
                    payload=body.get("payload"),
                    confidence=body.get("confidence"),
                    priority=str(body.get("priority") or "normal"),
                    evidence=body.get("evidence"),
                    feature=body.get("feature"),
                    tags=body.get("tags"),
                    supersedes=body.get("supersedes"),
                    parent_id=body.get("parent_id"),
                )
            except ValueError as exc:
                self._error(400, str(exc))
                return
            self._json({"annotation": entry})

        def _annotations_update(self, body: dict) -> None:
            store = self._ann_store()
            ann_id = str(body.get("id") or "")
            try:
                if body.get("status"):
                    updated = store.set_status(ann_id, str(body["status"]),
                                               reason=str(body.get("reason") or ""))
                elif body.get("body") is not None:
                    updated = store.edit_body(ann_id, str(body["body"]))
                else:
                    self._error(400, "nothing to update — pass status or body")
                    return
            except ValueError as exc:
                self._error(400, str(exc))
                return
            if updated is None:
                self._error(404, "no such annotation")
                return
            self._json({"annotation": updated})

        def _annotations_archive(self, body: dict) -> None:
            updated = self._ann_store().set_status(str(body.get("id") or ""), "archived")
            if updated is None:
                self._error(404, "no such annotation")
                return
            self._json({"annotation": updated})

        def _fidelity(self, query: dict) -> None:
            from .fidelity import intent_fidelity

            memory = cache.get()
            if memory is None:
                self._error(404, "graph.json not found — run `cms run-all` first")
                return
            try:
                self._json(intent_fidelity(root, memory.graph,
                                           (query.get("feature") or [""])[0]))
            except ValueError as exc:
                self._error(400, str(exc))

        def _feature_discover(self, body: dict) -> None:
            from .feature_discovery import FeatureDiscoveryError, propose_feature
            from .providers import get_provider

            memory = cache.get()
            if memory is None:
                self._error(404, "graph.json not found — run `cms run-all` first")
                return
            try:
                self._json(propose_feature(root, memory,
                                           str(body.get("description") or ""),
                                           get_provider(None)))
            except FeatureDiscoveryError as exc:
                self._error(400, str(exc))

        def _feature_confirm(self, body: dict) -> None:
            from .feature_discovery import FeatureDiscoveryError, confirm_feature

            try:
                self._json(confirm_feature(root, str(body.get("name") or ""),
                                           str(body.get("description") or ""),
                                           body.get("members") or []))
            except FeatureDiscoveryError as exc:
                self._error(400, str(exc))

        def _flowreview_get(self, query: dict) -> None:
            from .flowreview import FlowReviewError, read_flow_review

            memory = cache.get()
            if memory is None:
                self._error(404, "graph.json not found — run `cms run-all` first")
                return
            try:
                stored = read_flow_review(root, memory.graph,
                                          (query.get("feature") or [""])[0])
            except FlowReviewError as exc:
                self._error(400, str(exc))
                return
            self._json({"review": stored})  # null when never generated

        def _flowreview_post(self, body: dict) -> None:
            from .flowreview import FlowReviewError, build_flow_review
            from .providers import get_provider

            memory = cache.get()
            if memory is None:
                self._error(404, "graph.json not found — run `cms run-all` first")
                return
            try:
                review = build_flow_review(root, memory.graph, get_provider(None),
                                           str(body.get("feature") or ""),
                                           force=bool(body.get("force")))
            except FlowReviewError as exc:
                self._error(400, str(exc))
                return
            memory.save(memory_dir / "graph.json")  # persist onto the feature node
            self._json({"review": review})

        def _decisions_post(self, path: str, body: dict) -> None:
            from .decisions import DecisionStore

            store = DecisionStore(memory_dir, root=root)
            try:
                if path.endswith(("/approve", "/close")):
                    # Human-authority gate: approving and closing/rejecting both
                    # change the durable decision lifecycle. The code is printed
                    # to the launching terminal, a channel HTTP agents don't see.
                    if str(body.get("token") or "") != approval_token:
                        self._error(403, "decision authority requires the session code "
                                         "shown in the terminal that launched Atlas")
                        return
                if path.endswith("/approve"):
                    dec = store.approve(str(body.get("id") or ""),
                                        str(body.get("approved_by") or ""))
                elif path.endswith("/close"):
                    dec = store.close(str(body.get("id") or ""),
                                      str(body.get("status") or ""),
                                      reason=str(body.get("reason") or ""))
                else:
                    author = body.get("created_by") if isinstance(body.get("created_by"), dict) else {}
                    dec = store.propose(body.get("feature"), str(body.get("title") or ""),
                                        body.get("intent") or {},
                                        created_by={**author, "via": "http"},
                                        supersedes=body.get("supersedes"),
                                        evidence=body.get("evidence"))
            except ValueError as exc:
                self._error(400, str(exc))
                return
            self._json({"decision": dec})

        # --- Library ------------------------------------------------------
        # Publishing and deprecation change what every future agent is told;
        # they sit behind the same terminal-only session code as decision
        # approval. Browsing, drafting and composing are open.

        def _library_list(self, query: dict) -> None:
            from .library import LibraryView
            from .library_usage import LibraryUsageStore

            def one(key):
                return (query.get(key) or [None])[0] or None

            view = LibraryView(root)
            rows = view.list(type=one("type"), tag=one("tag"),
                             status=one("status"), scope=one("scope"), q=one("q"))
            summaries = LibraryUsageStore(memory_dir).summaries()
            for row in rows:
                row["evidence"] = summaries.get(
                    row["id"], {"uses": 0, "human": {"ratings": 0}})
            self._json({"assets": rows,
                        "types": list(_ASSET_TYPES), "scopes": [s.scope for s in view.stores]})

        def _library_asset(self, query: dict) -> None:
            from .library import LibraryError, LibraryView
            from .library_usage import LibraryUsageStore

            asset_id = (query.get("id") or [""])[0]
            raw_version = (query.get("version") or [""])[0]
            try:
                version = int(raw_version) if raw_version else None
                asset = LibraryView(root).get(asset_id, version)
            except (LibraryError, ValueError) as exc:
                self._error(404, str(exc))
                return
            annotations = []
            try:
                from .annotations import AnnotationStore
                annotations = AnnotationStore(memory_dir, root=root).list(
                    target=f"asset:{asset_id}")
            except Exception:
                pass
            self._json({"asset": asset, "annotations": annotations,
                        "evidence": LibraryUsageStore(memory_dir).summary(asset_id)})

        def _library_export(self, query: dict) -> None:
            from .library import LibraryError, export_asset

            asset_id = (query.get("id") or [""])[0]
            raw_version = (query.get("version") or [""])[0]
            try:
                version = int(raw_version) if raw_version else None
                text = export_asset(root, asset_id, version)
            except (LibraryError, ValueError) as exc:
                self._error(404, str(exc))
                return
            self._send(200, text.encode("utf-8"), "text/markdown; charset=utf-8")

        def _library_post(self, path: str, body: dict) -> None:
            from .library import (
                LibraryError,
                LibraryView,
                compose_context,
                import_asset,
                import_skill_directory,
                serialize_asset,
                slugify,
                validate_meta,
            )

            scope = str(body.get("scope") or "project")
            try:
                if path.endswith("/rating"):
                    from .library_usage import LibraryUsageStore
                    rated = LibraryUsageStore(memory_dir).rate(
                        str(body.get("use_id") or ""), rating=body.get("rating"),
                        effectiveness=body.get("effectiveness"),
                        efficiency=body.get("efficiency"), comment=body.get("comment"),
                        rated_by="user")
                    self._json(rated)
                    return

                if path.endswith("/compose"):
                    selection = [str(r) for r in (body.get("selection") or [])]
                    self._json(compose_context(
                        root, selection,
                        include_drafts=bool(body.get("include_drafts"))))
                    return

                if path.endswith(("/publish", "/status")):
                    # deprecate/publish rewrite what agents will be handed;
                    # enable/disable is a local view toggle and stays open.
                    gated = path.endswith("/publish") or \
                        str(body.get("status") or "") == "deprecated"
                    if gated and str(body.get("token") or "") != approval_token:
                        self._error(403, "publishing requires the session code shown "
                                         "in the terminal that launched Atlas")
                        return

                view = LibraryView(root)
                store = view.store(scope)
                if path.endswith("/publish"):
                    rec = store.publish(str(body.get("id") or ""),
                                        str(body.get("published_by") or ""))
                elif path.endswith("/status"):
                    status = str(body.get("status") or "")
                    asset_id = str(body.get("id") or "")
                    if status == "deprecated":
                        rec = store.deprecate(asset_id)
                    elif status in ("enabled", "disabled"):
                        rec = store.set_enabled(asset_id, status == "enabled")
                    else:
                        self._error(400, "status must be deprecated | enabled | disabled")
                        return
                elif path.endswith("/register"):
                    # adopt a file the human dropped into the library folder
                    rec = store.register_file(
                        str(body.get("id") or ""),
                        created_by={"kind": "user", "identity": "viewer", "via": "http"})
                elif path.endswith("/import-directory"):
                    result = import_skill_directory(
                        root, str(body.get("directory") or ""), scope=scope,
                        source_name=str(body.get("source_name") or ""),
                        created_by={"kind": "user", "identity": "viewer", "via": "http"})
                    self._json(result)
                    return
                elif path.endswith("/import"):
                    rec = import_asset(root, str(body.get("content") or ""),
                                       scope=scope,
                                       filename=str(body.get("filename") or ""),
                                       created_by={"kind": "user", "identity": "viewer",
                                                   "via": "http"})
                else:  # create / update a draft
                    text = body.get("text")
                    if not text:
                        meta = validate_meta({
                            "id": body.get("id") or slugify(str(body.get("name") or "")),
                            "name": body.get("name"), "type": body.get("type"),
                            "description": body.get("description"),
                            "tags": body.get("tags") or [],
                            "requires": body.get("requires") or [],
                            "conflicts_with": body.get("conflicts_with") or [],
                            "assets": body.get("assets") or [],
                        })
                        text = serialize_asset(meta, str(body.get("content") or ""))
                    rec = store.save_draft(
                        str(text),
                        created_by={"kind": "user", "identity": "viewer", "via": "http"})
            except (LibraryError, ValueError) as exc:
                self._error(400, str(exc))
                return
            self._json({"asset": rec})

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
            from .providers import provider_identity

            state = sstate.load_state(memory_dir)
            payload = {
                "project": root.name, "root": str(root),
                "schema_version": state.get("schema_version"),
                "stages": {name: sstate.stage(state, name) for name in sstate.STAGES},
                "pipeline": sstate.pipeline_status(state),
                "artifacts": sstate.artifact_provenance(state),
                "build_running": bool(build_state.get("running")),
                "build_message": build_state.get("message", ""),
            }
            try:
                payload["provider"] = provider_identity(None)
            except Exception:
                payload["provider"] = {"name": "unavailable", "model": None, "real": False}
            memory = cache.get()
            if memory is not None:
                payload["counts"] = sstate.feature_counts(memory.graph)
                payload["live"] = sstate.derive_staleness(state, memory.graph)
                payload["pipeline"] = sstate.live_pipeline_status(state, memory.graph)
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

        # ── trust-loop actions (impact / verify / align run from the UI) ──

        def _impact(self, query: dict) -> None:
            """Blast radius of a target — pure graph traversal, no LLM."""
            target = (query.get("target") or [""])[0].strip()
            if not target:
                self._json({"error": "no target given"}, 400)
                return
            memory = cache.get()
            if memory is None:
                self._json({"error": "no graph.json — run `cms run-all` first"}, 404)
                return
            from .impact import analyze_impact

            result = analyze_impact(memory.graph, target)
            if result is None:
                self._json({"error": f"could not resolve {target!r} in the graph"}, 404)
                return
            data = asdict(result)
            data["total"] = result.total
            self._json(data)

        # @memory:feature:AnchorDrift
        # @memory:feature:MemoryViewer
        # @memory:summary:Serves the current anchor-integrity report to the file and feature inspectors.
        def _drift(self, query: dict) -> None:
            """Per-anchor intent integrity, computed from current source and graph evidence."""
            target = (query.get("target") or [""])[0].strip() or None
            memory = cache.get()
            if memory is None:
                self._json({"error": "no graph.json — run `cms run-all` first"}, 404)
                return
            from .anchor_drift import detect_anchor_drift

            try:
                report = detect_anchor_drift(memory.graph, root, target=target)
            except ValueError as exc:
                self._json({"error": str(exc)}, 404)
                return
            self._json(report.to_dict())

        def _verify(self, body: dict) -> None:
            """Run exactly the tests mapped as exercising one feature."""
            feature = str(body.get("feature") or "").strip()
            if not feature:
                self._json({"error": "select a feature first, then run verify"}, 400)
                return
            memory = cache.get()
            if memory is None:
                self._json({"error": "no graph.json — run `cms run-all` first"}, 404)
                return
            from .features import get_features

            matches = [f for f in get_features(memory.graph)
                       if f["name"].lower() == feature.lower()]
            if not matches:
                self._json({"error": f"unknown feature {feature!r}"}, 404)
                return
            name = matches[0]["name"]
            tests = matches[0].get("exercised_by") or []
            if not tests:
                self._json({"feature": name, "ran": False, "tests": [],
                            "message": "No tests are mapped to this feature yet — run "
                                       "`cms verify` (no args) to collect coverage first."})
                return
            from .verify import verify_feature

            passed, output = verify_feature(root, tests)
            self._json({"feature": name, "ran": True, "passed": passed,
                        "tests": tests, "output": output})

        def _align(self, body: dict) -> None:
            """Capture intent and verdict the working diff against it."""
            memory = cache.get()
            if memory is None:
                self._json({"error": "no graph.json — run `cms run-all` first"}, 404)
                return
            from .align import AlignStore, build_alignment
            from .intent import capture_intent

            goal = str(body.get("goal") or "").strip() or None
            base = str(body.get("base") or "HEAD") or "HEAD"
            scan = bool(body.get("scan"))
            pack = capture_intent(root, goal=goal, base=base)
            record = build_alignment(memory, root, pack, base=base, scan=scan)
            AlignStore(memory_dir).save_alignment(record)
            self._json(record)

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

    Handler.approval_token = approval_token  # serve() prints it to the terminal
    return Handler


# @memory:feature:MemoryViewer
# @memory:connects:QueryEngine, GitHistoryLayer, ActivityPulse, FeatureTracing
# @memory:summary:Local web UI — explorer, force-directed knowledge graph with heat overlay and MCP pulses, inspector with summaries/anchors/flows, intent search.
def serve(root: Path, port: int = 7717, open_browser: bool = True, open_path: str = "/") -> None:
    root = root.resolve()
    cache = _MemoryCache(root / config.MEMORY_DIR_NAME / "graph.json")
    handler = make_handler(root, cache)
    server = ThreadingHTTPServer(("127.0.0.1", port), handler)
    url = f"http://127.0.0.1:{port}"
    print(f"CMS UI serving {root.name} at {url}  (Ctrl+C to stop)")
    print(f"Decision-authority code for this session: {handler.approval_token}  "
          "(the UI asks for it when you approve or reject an intent)")
    if open_browser:
        threading.Timer(0.4, webbrowser.open, args=(url + open_path,)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nCMS UI stopped.")
    finally:
        server.server_close()
