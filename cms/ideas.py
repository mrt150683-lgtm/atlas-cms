"""Idea Journal - durable personal ideas across every Atlas project.

The journal is canonical user-owned thought.  Model output lands in a separate
candidate table and only becomes an idea through an explicit human action.
Raw source material is append-only and generation runs retain exact context,
provider, model, input hash, and seed provenance.
"""

from __future__ import annotations

import hashlib
import json
import os
import random
import re
import secrets
import sqlite3
import time
from pathlib import Path

from . import config
from .providers import SummaryProvider

SCHEMA_VERSION = 1
IDEA_KINDS = ("project", "feature", "tool", "module", "agent_flow",
              "experiment", "question", "concept")
IDEA_STATUSES = ("inbox", "exploring", "promising", "planned", "building",
                 "shipped", "parked", "rejected")
CANDIDATE_STATUSES = ("new", "accepted", "merged", "parked", "rejected")
RELATION_TYPES = ("contains", "builds_on", "combines_with", "enables",
                  "depends_on", "conflicts_with", "alternative_to",
                  "inspired_by", "solves_gap", "relates_to")
TARGET_TYPES = ("idea", "project", "feature", "source")
GENERATION_MODES = ("project", "feature", "cross_project", "gap_finder",
                    "journal", "join_dots", "wild")
GEN_MAX_TOKENS = 3600
_UNSET = object()

GEN_PROMPT = """You are working inside Atlas Idea Journal: a durable record of one builder's
actual thinking. Generate NEW candidate ideas, not edits to canonical journal entries.

MODE: {mode}
DIRECTION: {direction}
SURPRISE: {surprise:.2f} (0 = coherent extension, 1 = bold but still useful)

EVIDENCE PACK:
{context}

Avoid near-duplicates of existing ideas and respect rejected/parked directions.
Use the named projects and feature capabilities exactly; do not invent evidence.
Return ONLY a JSON array of {count} objects with these keys:
title, kind, overview, rationale, contributions (array of strings),
missing_capability, risks (array of strings), first_experiment.
Kinds: project, feature, tool, module, agent_flow, experiment, question, concept.
Each overview should explain a useful concept in 2-4 concrete sentences.
"""


class IdeaError(ValueError):
    """Idea Journal request could not be completed without corrupting intent."""


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _id(prefix: str) -> str:
    raw = f"{prefix}:{time.time_ns()}:{secrets.token_hex(8)}"
    return f"{prefix}-{hashlib.sha1(raw.encode()).hexdigest()[:16]}"


def _clean_text(value, limit: int, *, required: bool = False, label: str = "text") -> str:
    text = str(value or "").strip()
    if required and not text:
        raise IdeaError(f"{label} is required")
    return text[:limit]


def _json(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _parse_json_array(raw: str) -> list:
    match = re.search(r"\[[\s\S]*\]", raw or "")
    if match is None:
        raise IdeaError("provider returned no JSON array")
    try:
        value = json.loads(match.group(0))
    except json.JSONDecodeError as exc:
        raise IdeaError(f"provider returned invalid JSON: {exc}") from exc
    if not isinstance(value, list):
        raise IdeaError("provider output was not a JSON array")
    return value


# @memory:feature:IdeaJournal
# @memory:connects:Constellation, AtlasLibrary, AgentMemoryAccess
# @memory:summary:sqlite3-backed global idea store separating user-owned ideas from model candidates while preserving sources, typed links, generation provenance, and project-feature staleness.
class IdeaJournal:
    """Canonical idea store with migrations and append-only evidence records."""

    def __init__(self, directory: Path | None = None):
        self.directory = Path(directory or config.IDEAS_USER_DIR)
        self.path = self.directory / "journal.db"
        self.directory.mkdir(parents=True, exist_ok=True)
        self._migrate()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA journal_mode = WAL")
        return conn

    def _migrate(self) -> None:
        with self._connect() as conn:
            version = int(conn.execute("PRAGMA user_version").fetchone()[0])
            if version > SCHEMA_VERSION:
                raise IdeaError(f"journal schema {version} is newer than this Atlas build")
            if version < 1:
                conn.executescript("""
                    CREATE TABLE ideas (
                        id TEXT PRIMARY KEY,
                        title TEXT NOT NULL,
                        overview TEXT NOT NULL DEFAULT '',
                        body TEXT NOT NULL DEFAULT '',
                        kind TEXT NOT NULL,
                        status TEXT NOT NULL,
                        parent_id TEXT REFERENCES ideas(id),
                        author_kind TEXT NOT NULL,
                        origin TEXT NOT NULL,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL
                    );
                    CREATE INDEX ideas_parent_idx ON ideas(parent_id);
                    CREATE INDEX ideas_status_idx ON ideas(status, updated_at DESC);
                    CREATE VIRTUAL TABLE idea_fts USING fts5(
                        id UNINDEXED, title, overview, body,
                        tokenize='unicode61 remove_diacritics 2'
                    );
                    CREATE TABLE sources (
                        id TEXT PRIMARY KEY,
                        idea_id TEXT REFERENCES ideas(id),
                        source_type TEXT NOT NULL,
                        title TEXT NOT NULL DEFAULT '',
                        content TEXT NOT NULL,
                        content_hash TEXT NOT NULL,
                        uri TEXT NOT NULL DEFAULT '',
                        author_kind TEXT NOT NULL,
                        created_at TEXT NOT NULL,
                        UNIQUE(idea_id, content_hash)
                    );
                    CREATE TABLE relationships (
                        id TEXT PRIMARY KEY,
                        source_idea_id TEXT NOT NULL REFERENCES ideas(id),
                        target_type TEXT NOT NULL,
                        target_ref TEXT NOT NULL,
                        relation_type TEXT NOT NULL,
                        metadata_json TEXT NOT NULL DEFAULT '{}',
                        created_at TEXT NOT NULL,
                        UNIQUE(source_idea_id, target_type, target_ref, relation_type)
                    );
                    CREATE INDEX relationships_source_idx ON relationships(source_idea_id);
                    CREATE TABLE generation_runs (
                        id TEXT PRIMARY KEY,
                        mode TEXT NOT NULL,
                        direction TEXT NOT NULL,
                        provider TEXT NOT NULL,
                        model TEXT,
                        temperature REAL NOT NULL,
                        seed INTEGER NOT NULL,
                        context_json TEXT NOT NULL,
                        input_hash TEXT NOT NULL,
                        created_at TEXT NOT NULL
                    );
                    CREATE TABLE candidates (
                        id TEXT PRIMARY KEY,
                        title TEXT NOT NULL,
                        overview TEXT NOT NULL,
                        kind TEXT NOT NULL,
                        status TEXT NOT NULL,
                        payload_json TEXT NOT NULL,
                        origin TEXT NOT NULL,
                        generation_id TEXT REFERENCES generation_runs(id),
                        accepted_idea_id TEXT REFERENCES ideas(id),
                        created_at TEXT NOT NULL,
                        decided_at TEXT
                    );
                    CREATE INDEX candidates_status_idx ON candidates(status, created_at DESC);
                    CREATE TABLE join_paths (
                        id TEXT PRIMARY KEY,
                        generation_id TEXT NOT NULL REFERENCES generation_runs(id),
                        seed INTEGER NOT NULL,
                        points_json TEXT NOT NULL,
                        nodes_json TEXT NOT NULL,
                        surprise REAL NOT NULL,
                        created_at TEXT NOT NULL
                    );
                    CREATE TABLE events (
                        id TEXT PRIMARY KEY,
                        entity_type TEXT NOT NULL,
                        entity_id TEXT NOT NULL,
                        action TEXT NOT NULL,
                        actor_kind TEXT NOT NULL,
                        payload_json TEXT NOT NULL,
                        created_at TEXT NOT NULL
                    );
                    CREATE INDEX events_entity_idx ON events(entity_type, entity_id, created_at);
                    PRAGMA user_version = 1;
                """)

    def _event(self, conn: sqlite3.Connection, entity_type: str, entity_id: str,
               action: str, actor_kind: str, payload: dict | None = None) -> None:
        conn.execute(
            "INSERT INTO events VALUES (?, ?, ?, ?, ?, ?, ?)",
            (_id("evt"), entity_type, entity_id, action, actor_kind,
             _json(payload or {}), _now()),
        )

    def _idea_exists(self, conn: sqlite3.Connection, idea_id: str) -> bool:
        return conn.execute("SELECT 1 FROM ideas WHERE id = ?", (idea_id,)).fetchone() is not None

    def _would_cycle(self, conn: sqlite3.Connection, idea_id: str, parent_id: str) -> bool:
        current = parent_id
        seen = {idea_id}
        while current:
            if current in seen:
                return True
            seen.add(current)
            row = conn.execute("SELECT parent_id FROM ideas WHERE id = ?", (current,)).fetchone()
            current = row[0] if row else None
        return False

    def create_idea(self, title: str, *, overview: str = "", body: str = "",
                    kind: str = "concept", status: str = "inbox",
                    parent_id: str | None = None, origin: str = "user",
                    actor_kind: str = "human") -> dict:
        """Create user-owned canonical thought; models must use propose_candidate."""
        if actor_kind == "model":
            raise IdeaError("models create candidates; only a human action creates a canonical idea")
        if kind not in IDEA_KINDS:
            raise IdeaError(f"kind must be one of: {', '.join(IDEA_KINDS)}")
        if status not in IDEA_STATUSES:
            raise IdeaError(f"status must be one of: {', '.join(IDEA_STATUSES)}")
        iid = _id("idea")
        now = _now()
        title = _clean_text(title, 180, required=True, label="title")
        overview = _clean_text(overview, 4000)
        body = _clean_text(body, 100_000)
        with self._connect() as conn:
            if parent_id and not self._idea_exists(conn, parent_id):
                raise IdeaError(f"unknown parent idea {parent_id!r}")
            conn.execute("INSERT INTO ideas VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                         (iid, title, overview, body, kind, status, parent_id,
                          actor_kind, _clean_text(origin, 80) or "user", now, now))
            conn.execute("INSERT INTO idea_fts VALUES (?, ?, ?, ?)",
                         (iid, title, overview, body))
            self._event(conn, "idea", iid, "created", actor_kind,
                        {"title": title, "origin": origin})
        return self.get_idea(iid)

    def update_idea(self, idea_id: str, *, title=None, overview=None, body=None,
                    kind=None, status=None, parent_id=_UNSET,
                    actor_kind: str = "human") -> dict:
        if actor_kind == "model":
            raise IdeaError("models cannot rewrite canonical ideas; propose a candidate or append a source")
        current = self.get_idea(idea_id, include_events=False)
        if current is None:
            raise IdeaError(f"unknown idea {idea_id!r}")
        values = {
            "title": _clean_text(title if title is not None else current["title"], 180,
                                 required=True, label="title"),
            "overview": _clean_text(overview if overview is not None else current["overview"], 4000),
            "body": _clean_text(body if body is not None else current["body"], 100_000),
            "kind": kind if kind is not None else current["kind"],
            "status": status if status is not None else current["status"],
            "parent_id": current.get("parent_id") if parent_id is _UNSET else parent_id,
        }
        if values["kind"] not in IDEA_KINDS or values["status"] not in IDEA_STATUSES:
            raise IdeaError("invalid idea kind or status")
        if values["parent_id"] == idea_id:
            raise IdeaError("an idea cannot be its own parent")
        with self._connect() as conn:
            if values["parent_id"] and not self._idea_exists(conn, values["parent_id"]):
                raise IdeaError(f"unknown parent idea {values['parent_id']!r}")
            if values["parent_id"] and self._would_cycle(conn, idea_id, values["parent_id"]):
                raise IdeaError("parent change would create an idea cycle")
            conn.execute("""UPDATE ideas SET title=?, overview=?, body=?, kind=?, status=?,
                            parent_id=?, updated_at=? WHERE id=?""",
                         (values["title"], values["overview"], values["body"],
                          values["kind"], values["status"], values["parent_id"],
                          _now(), idea_id))
            conn.execute("DELETE FROM idea_fts WHERE id = ?", (idea_id,))
            conn.execute("INSERT INTO idea_fts VALUES (?, ?, ?, ?)",
                         (idea_id, values["title"], values["overview"], values["body"]))
            self._event(conn, "idea", idea_id, "updated", actor_kind,
                        {k: v for k, v in values.items() if v != current.get(k)})
        return self.get_idea(idea_id)

    def add_source(self, content: str, *, idea_id: str | None = None,
                   source_type: str = "brainstorm", title: str = "",
                   uri: str = "", actor_kind: str = "human") -> dict:
        content = _clean_text(content, 1_000_000, required=True, label="source content")
        digest = hashlib.sha256(content.encode()).hexdigest()
        sid = _id("src")
        now = _now()
        with self._connect() as conn:
            if idea_id and not self._idea_exists(conn, idea_id):
                raise IdeaError(f"unknown idea {idea_id!r}")
            existing = conn.execute(
                "SELECT * FROM sources WHERE idea_id IS ? AND content_hash = ?",
                (idea_id, digest),
            ).fetchone()
            if existing:
                return dict(existing)
            conn.execute("INSERT INTO sources VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                         (sid, idea_id, _clean_text(source_type, 60) or "brainstorm",
                          _clean_text(title, 240), content, digest,
                          _clean_text(uri, 1000), actor_kind, now))
            self._event(conn, "source", sid, "attached", actor_kind,
                        {"idea_id": idea_id, "content_hash": digest})
        return self.get_source(sid)

    def add_relationship(self, source_idea_id: str, target_type: str,
                         target_ref: str, relation_type: str = "relates_to",
                         metadata: dict | None = None,
                         actor_kind: str = "human") -> dict:
        if target_type not in TARGET_TYPES:
            raise IdeaError(f"target_type must be one of: {', '.join(TARGET_TYPES)}")
        if relation_type not in RELATION_TYPES:
            raise IdeaError(f"relation_type must be one of: {', '.join(RELATION_TYPES)}")
        target_ref = _clean_text(target_ref, 2000, required=True, label="target_ref")
        rid = _id("rel")
        recorded = dict(metadata or {})
        recorded.update({k: v for k, v in self._target_snapshot(target_type, target_ref).items()
                         if k not in recorded})
        with self._connect() as conn:
            if not self._idea_exists(conn, source_idea_id):
                raise IdeaError(f"unknown idea {source_idea_id!r}")
            if target_type == "idea" and not self._idea_exists(conn, target_ref):
                raise IdeaError(f"unknown target idea {target_ref!r}")
            try:
                conn.execute("INSERT INTO relationships VALUES (?, ?, ?, ?, ?, ?, ?)",
                             (rid, source_idea_id, target_type, target_ref,
                              relation_type, _json(recorded), _now()))
            except sqlite3.IntegrityError:
                row = conn.execute("""SELECT * FROM relationships WHERE source_idea_id=?
                                    AND target_type=? AND target_ref=? AND relation_type=?""",
                                   (source_idea_id, target_type, target_ref,
                                    relation_type)).fetchone()
                return self._relationship_dict(row)
            self._event(conn, "idea", source_idea_id, "relationship_added", actor_kind,
                        {"target_type": target_type, "target_ref": target_ref,
                         "relation_type": relation_type})
            row = conn.execute("SELECT * FROM relationships WHERE id=?", (rid,)).fetchone()
        return self._relationship_dict(row)

    @staticmethod
    def _relationship_dict(row: sqlite3.Row | None) -> dict | None:
        if row is None:
            return None
        out = dict(row)
        out["metadata"] = json.loads(out.pop("metadata_json") or "{}")
        return out

    @staticmethod
    def _target_snapshot(target_type: str, target_ref: str) -> dict:
        """Capture enough Atlas identity to detect later project/feature drift."""
        if target_type not in ("project", "feature"):
            return {}
        root_str, feature_name = target_ref, None
        if target_type == "feature":
            root_str, sep, feature_name = target_ref.rpartition("::")
            if not sep:
                return {"target_present": False}
        try:
            from .fuse import build_card
            card = build_card(Path(root_str))
        except (OSError, ValueError, json.JSONDecodeError):
            return {"target_present": False}
        present = bool(card.get("ready"))
        if feature_name is not None:
            present = present and any(f.get("name") == feature_name
                                      for f in card.get("features", []))
        return {"target_present": present,
                "project_feature_set_hash": card.get("feature_set_hash")}

    def _relationship_state(self, relationship: dict) -> dict:
        if relationship["target_type"] not in ("project", "feature"):
            return relationship
        current = self._target_snapshot(relationship["target_type"],
                                        relationship["target_ref"])
        captured = relationship.get("metadata") or {}
        old_hash = captured.get("project_feature_set_hash")
        new_hash = current.get("project_feature_set_hash")
        relationship["target_present"] = bool(current.get("target_present"))
        relationship["stale"] = (not relationship["target_present"] or
                                  bool(old_hash and new_hash and old_hash != new_hash))
        relationship["current_feature_set_hash"] = new_hash
        return relationship

    def get_source(self, source_id: str) -> dict | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM sources WHERE id=?", (source_id,)).fetchone()
        return dict(row) if row else None

    def get_idea(self, idea_id: str, *, include_events: bool = True) -> dict | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM ideas WHERE id=?", (idea_id,)).fetchone()
            if row is None:
                return None
            out = dict(row)
            out["children"] = [dict(r) for r in conn.execute(
                "SELECT id, title, kind, status, updated_at FROM ideas WHERE parent_id=? ORDER BY updated_at DESC",
                (idea_id,))]
            out["sources"] = [dict(r) for r in conn.execute(
                "SELECT id, source_type, title, content_hash, uri, author_kind, created_at FROM sources WHERE idea_id=? ORDER BY created_at DESC",
                (idea_id,))]
            out["relationships"] = [self._relationship_state(self._relationship_dict(r))
                                    for r in conn.execute(
                "SELECT * FROM relationships WHERE source_idea_id=? ORDER BY created_at",
                (idea_id,))]
            if include_events:
                out["events"] = [dict(r) | {"payload": json.loads(r["payload_json"])}
                                 for r in conn.execute(
                    "SELECT * FROM events WHERE entity_type='idea' AND entity_id=? ORDER BY created_at DESC LIMIT 100",
                    (idea_id,))]
                for event in out["events"]:
                    event.pop("payload_json", None)
        return out

    def search(self, query: str = "", *, project: str | None = None,
               kind: str | None = None, status: str | None = None,
               limit: int = 50) -> list[dict]:
        limit = min(200, max(1, int(limit)))
        clauses, params = [], []
        if kind:
            clauses.append("i.kind = ?")
            params.append(kind)
        if status:
            clauses.append("i.status = ?")
            params.append(status)
        if project:
            clauses.append("EXISTS (SELECT 1 FROM relationships r WHERE r.source_idea_id=i.id AND r.target_type='project' AND (r.target_ref=? OR r.metadata_json LIKE ?))")
            params += [project, f'%"name": "{project}"%']
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        with self._connect() as conn:
            if query.strip():
                tokens = re.findall(r"[\w-]+", query, flags=re.UNICODE)[:12]
                match = " AND ".join(f'"{t.replace(chr(34), "")}"' for t in tokens)
                try:
                    rows = conn.execute(
                        f"""SELECT i.* FROM idea_fts f JOIN ideas i ON i.id=f.id
                            {where + (' AND ' if where else ' WHERE ')} idea_fts MATCH ?
                            ORDER BY bm25(idea_fts), i.updated_at DESC LIMIT ?""",
                        (*params, match, limit),
                    ).fetchall()
                except sqlite3.OperationalError:
                    like = f"%{query.strip()}%"
                    rows = conn.execute(
                        f"""SELECT i.* FROM ideas i
                            {where + (' AND ' if where else ' WHERE ')}
                            (i.title LIKE ? OR i.overview LIKE ? OR i.body LIKE ?)
                            ORDER BY i.updated_at DESC LIMIT ?""",
                        (*params, like, like, like, limit),
                    ).fetchall()
            else:
                rows = conn.execute(
                    f"SELECT i.* FROM ideas i{where} ORDER BY i.updated_at DESC LIMIT ?",
                    (*params, limit),
                ).fetchall()
        return [dict(r) for r in rows]

    def list_candidates(self, status: str | None = None, limit: int = 100) -> list[dict]:
        with self._connect() as conn:
            if status:
                rows = conn.execute("SELECT * FROM candidates WHERE status=? ORDER BY created_at DESC LIMIT ?",
                                    (status, min(500, limit))).fetchall()
            else:
                rows = conn.execute("SELECT * FROM candidates ORDER BY created_at DESC LIMIT ?",
                                    (min(500, limit),)).fetchall()
        out = []
        for row in rows:
            item = dict(row)
            item["payload"] = json.loads(item.pop("payload_json") or "{}")
            out.append(item)
        return out

    def propose_candidate(self, title: str, overview: str, *, kind: str = "concept",
                          payload: dict | None = None, origin: str = "agent",
                          generation_id: str | None = None,
                          actor_kind: str = "model") -> dict:
        if kind not in IDEA_KINDS:
            kind = "concept"
        cid, now = _id("cand"), _now()
        title = _clean_text(title, 180, required=True, label="title")
        overview = _clean_text(overview, 8000, required=True, label="overview")
        record = dict(payload or {})
        record.update({"title": title, "overview": overview, "kind": kind})
        with self._connect() as conn:
            conn.execute("INSERT INTO candidates VALUES (?, ?, ?, ?, 'new', ?, ?, ?, NULL, ?, NULL)",
                         (cid, title, overview, kind, _json(record),
                          _clean_text(origin, 80) or "agent", generation_id, now))
            self._event(conn, "candidate", cid, "proposed", actor_kind,
                        {"origin": origin, "generation_id": generation_id})
        return self.get_candidate(cid)

    def get_candidate(self, candidate_id: str) -> dict | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM candidates WHERE id=?", (candidate_id,)).fetchone()
        if not row:
            return None
        out = dict(row)
        out["payload"] = json.loads(out.pop("payload_json") or "{}")
        return out

    def decide_candidate(self, candidate_id: str, verdict: str, *,
                         parent_id: str | None = None,
                         merge_into: str | None = None) -> dict:
        if verdict not in CANDIDATE_STATUSES[1:]:
            raise IdeaError("candidate verdict must be accepted | merged | parked | rejected")
        candidate = self.get_candidate(candidate_id)
        if candidate is None:
            raise IdeaError(f"unknown candidate {candidate_id!r}")
        accepted_id = None
        if verdict == "accepted":
            idea = self.create_idea(candidate["title"], overview=candidate["overview"],
                                    body=_json(candidate["payload"]), kind=candidate["kind"],
                                    status="inbox", parent_id=parent_id,
                                    origin=candidate["origin"], actor_kind="human")
            accepted_id = idea["id"]
        elif verdict == "merged":
            if not merge_into or self.get_idea(merge_into) is None:
                raise IdeaError("merge_into must name an existing idea")
            accepted_id = merge_into
            self.add_source(_json(candidate["payload"]), idea_id=merge_into,
                            source_type="candidate", title=candidate["title"],
                            actor_kind="human")
        with self._connect() as conn:
            conn.execute("UPDATE candidates SET status=?, accepted_idea_id=?, decided_at=? WHERE id=?",
                         (verdict, accepted_id, _now(), candidate_id))
            self._event(conn, "candidate", candidate_id, verdict, "human",
                        {"idea_id": accepted_id})
        return self.get_candidate(candidate_id)

    def events(self, limit: int = 100) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM events ORDER BY created_at DESC LIMIT ?",
                                (min(500, max(1, limit)),)).fetchall()
        out = []
        for row in rows:
            item = dict(row)
            item["payload"] = json.loads(item.pop("payload_json") or "{}")
            out.append(item)
        return out

    def map_data(self, *, include_features: bool = True,
                 feature_limit: int = 120) -> dict:
        """One graph-shaped read model joining ideas to Atlas projects/features."""
        nodes, edges = [], []
        ideas = self.search(limit=200)
        for idea in ideas:
            nodes.append({"id": f"idea:{idea['id']}", "type": "idea",
                          "label": idea["title"], "kind": idea["kind"],
                          "status": idea["status"], "ref": idea["id"]})
            if idea.get("parent_id"):
                edges.append({"source": f"idea:{idea['parent_id']}",
                              "target": f"idea:{idea['id']}", "type": "contains"})
        try:
            from .fuse import build_card, load_registry

            remaining = max(0, int(feature_limit))
            for root_str, meta in (load_registry().get("projects") or {}).items():
                card = build_card(Path(root_str))
                pid = f"project:{root_str}"
                nodes.append({"id": pid, "type": "project", "label": card.get("name") or meta.get("name"),
                              "ref": root_str, "ready": bool(card.get("ready")),
                              "feature_set_hash": card.get("feature_set_hash")})
                if include_features and card.get("ready"):
                    for feature in card.get("features", [])[:remaining]:
                        fid = f"feature:{root_str}::{feature['name']}"
                        nodes.append({"id": fid, "type": "feature", "label": feature["name"],
                                      "ref": f"{root_str}::{feature['name']}",
                                      "project": root_str,
                                      "description": feature.get("description", "")})
                        edges.append({"source": pid, "target": fid, "type": "contains"})
                    remaining -= min(remaining, len(card.get("features", [])))
        except (OSError, ValueError, json.JSONDecodeError):
            pass
        with self._connect() as conn:
            for row in conn.execute("SELECT * FROM relationships"):
                rel = self._relationship_state(self._relationship_dict(row))
                target = (f"idea:{rel['target_ref']}" if rel["target_type"] == "idea"
                          else f"{rel['target_type']}:{rel['target_ref']}")
                edges.append({"source": f"idea:{rel['source_idea_id']}", "target": target,
                              "type": rel["relation_type"], "metadata": rel["metadata"]})
        return {"nodes": nodes, "edges": edges, "generated_at": _now()}

    def resolve_nodes(self, node_ids: list[str]) -> list[dict]:
        graph = self.map_data(feature_limit=300)
        by_id = {n["id"]: n for n in graph["nodes"]}
        missing = [nid for nid in node_ids if nid not in by_id]
        if missing:
            raise IdeaError(f"unknown map node(s): {', '.join(missing[:4])}")
        return [by_id[nid] for nid in node_ids]

    def build_context(self, *, direction: str = "", project_roots: list[str] | None = None,
                      feature_refs: list[str] | None = None,
                      idea_ids: list[str] | None = None,
                      selected_nodes: list[dict] | None = None) -> dict:
        evidence = {"direction": direction, "ideas": [], "projects": [],
                    "features": [], "selected_nodes": selected_nodes or [],
                    "feedback": {"liked": [], "avoid": []}, "fusion": [], "scout": []}
        selected = set(idea_ids or [])
        # Generation must always see recent journal history, even when the user's
        # direction uses entirely new vocabulary. Directional matches are ranked
        # first, then recent entries fill the bounded evidence pack.
        hits = self.search(direction, limit=30) if direction else []
        seen = {hit["id"] for hit in hits}
        hits.extend(hit for hit in self.search(limit=30) if hit["id"] not in seen)
        for hit in hits:
            if hit["id"] in selected or len(evidence["ideas"]) < 20:
                evidence["ideas"].append({k: hit[k] for k in
                                          ("id", "title", "overview", "kind", "status", "parent_id")})
        try:
            from .fuse import build_card, load_fusion, load_registry

            roots = project_roots or []
            if not roots and selected_nodes:
                roots = [n["ref"] for n in selected_nodes if n["type"] == "project"]
                roots += [n.get("project") for n in selected_nodes if n["type"] == "feature"]
            registry = load_registry().get("projects") or {}
            for root_str in dict.fromkeys(r for r in roots if r):
                if root_str in registry or Path(root_str).exists():
                    card = build_card(Path(root_str))
                    evidence["projects"].append(card)
            report = load_fusion() or {}
            for section in ("integrations", "emergent", "conflicts"):
                for item in report.get(section) or []:
                    if not roots or set(item.get("projects") or []) & {Path(r).name for r in roots}:
                        evidence["fusion"].append({"section": section, **item})
        except (OSError, ValueError, json.JSONDecodeError):
            pass
        for ref in feature_refs or []:
            root_str, sep, name = ref.rpartition("::")
            if sep:
                evidence["features"].append({"project": root_str, "name": name})
        if selected_nodes:
            evidence["features"] += [n for n in selected_nodes if n["type"] == "feature"]
            for n in selected_nodes:
                if n["type"] == "idea":
                    idea = self.get_idea(n["ref"], include_events=False)
                    if idea and not any(i["id"] == idea["id"] for i in evidence["ideas"]):
                        evidence["ideas"].append({k: idea.get(k) for k in
                                                  ("id", "title", "overview", "kind", "status", "parent_id")})
        try:
            from .scout import load_cards, load_suggestions
            evidence["scout"] = [
                {"type": "plan", "text": c.get("one_liner"), "features": c.get("features", [])}
                for c in load_cards().values() if c.get("status") == "ok"
            ][:20]
            for suggestion in load_suggestions().values():
                bucket = "avoid" if suggestion.get("status") in ("rejected", "ignored") else "liked"
                evidence["feedback"][bucket].append(suggestion.get("title"))
        except (OSError, ValueError, json.JSONDecodeError):
            pass
        for candidate in self.list_candidates(limit=100):
            bucket = "avoid" if candidate["status"] in ("rejected", "parked") else "liked"
            if candidate["status"] != "new":
                evidence["feedback"][bucket].append(candidate["title"])
        return evidence

    # @memory:feature:IdeaGenerator
    # @memory:connects:IdeaJournal, Constellation, ProjectPlanDiscovery
    # @memory:summary:Creates structured, provenance-stamped candidates from bounded journal and Atlas evidence packs; provider failures leave journal state untouched.
    def generate(self, provider: SummaryProvider, *, mode: str = "journal",
                 direction: str = "", project_roots: list[str] | None = None,
                 feature_refs: list[str] | None = None,
                 idea_ids: list[str] | None = None,
                 selected_nodes: list[dict] | None = None,
                 surprise: float = 0.5, count: int = 6,
                 seed: int | None = None) -> dict:
        if provider.name == "mock":
            raise IdeaError("idea generation needs a real provider (configure an API key)")
        if mode not in GENERATION_MODES:
            raise IdeaError(f"mode must be one of: {', '.join(GENERATION_MODES)}")
        surprise = min(1.0, max(0.0, float(surprise)))
        count = min(10, max(1, int(count)))
        seed = int(seed if seed is not None else random.SystemRandom().randrange(1, 2_147_483_647))
        context = self.build_context(direction=direction, project_roots=project_roots,
                                     feature_refs=feature_refs, idea_ids=idea_ids,
                                     selected_nodes=selected_nodes)
        context_text = json.dumps(context, ensure_ascii=False, indent=2)[:70_000]
        prompt = GEN_PROMPT.format(mode=mode, direction=direction or "Discover worthwhile directions",
                                   surprise=surprise, context=context_text, count=count)
        try:
            raw = provider.summarize(prompt, {"max_tokens": GEN_MAX_TOKENS,
                                              "temperature": 0.25 + surprise * 0.75,
                                              "seed": seed})
        except Exception as exc:
            raise IdeaError(f"provider call failed: {type(exc).__name__}: {exc}") from exc
        parsed = _parse_json_array(raw)
        normalized = []
        for item in parsed[:count]:
            if not isinstance(item, dict):
                continue
            title = _clean_text(item.get("title"), 180)
            overview = _clean_text(item.get("overview"), 8000)
            if not title or not overview:
                continue
            item = dict(item)
            item["title"], item["overview"] = title, overview
            item["kind"] = item.get("kind") if item.get("kind") in IDEA_KINDS else "concept"
            item["contributions"] = [str(v)[:500] for v in (item.get("contributions") or [])][:12]
            item["risks"] = [str(v)[:500] for v in (item.get("risks") or [])][:12]
            normalized.append(item)
        if not normalized:
            raise IdeaError("provider returned no usable structured candidates")
        run_id, now = _id("gen"), _now()
        input_hash = hashlib.sha256((prompt + raw).encode()).hexdigest()[:24]
        with self._connect() as conn:
            conn.execute("INSERT INTO generation_runs VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                         (run_id, mode, _clean_text(direction, 4000), provider.name,
                          getattr(provider, "model", None), surprise, seed,
                          _json(context), input_hash, now))
            self._event(conn, "generation", run_id, "completed", "model",
                        {"mode": mode, "count": len(normalized), "input_hash": input_hash})
        candidates = [self.propose_candidate(
            item["title"], item["overview"], kind=item["kind"], payload=item,
            origin=mode, generation_id=run_id, actor_kind="model") for item in normalized]
        return {"generation_id": run_id, "mode": mode, "seed": seed,
                "input_hash": input_hash, "context": context, "candidates": candidates}

    # @memory:feature:JoinTheDots
    # @memory:connects:IdeaGenerator, IdeaJournal, Constellation
    # @memory:summary:Turns an ordered user-drawn map path into reproducible selected nodes plus seeded wildcard nodes before structured candidate synthesis.
    def join_dots(self, provider: SummaryProvider, node_ids: list[str], *,
                  points: list | None = None, surprise: float = 0.7,
                  direction: str = "", seed: int | None = None,
                  count: int = 4) -> dict:
        ordered = list(dict.fromkeys(str(n) for n in node_ids if str(n)))
        if len(ordered) < 2:
            raise IdeaError("Join the Dots needs at least two distinct nodes")
        seed = int(seed if seed is not None else random.SystemRandom().randrange(1, 2_147_483_647))
        selected = self.resolve_nodes(ordered)
        surprise = min(1.0, max(0.0, float(surprise)))
        wildcard_count = 2 if surprise >= 0.85 else (1 if surprise >= 0.5 else 0)
        if wildcard_count:
            graph = self.map_data(feature_limit=300)
            available = [n for n in graph["nodes"] if n["id"] not in ordered]
            rng = random.Random(seed)
            rng.shuffle(available)
            selected += available[:wildcard_count]
        path_text = " -> ".join(n["label"] for n in selected)
        steer = (direction + "\n" if direction else "") + \
            f"Join these dots in this exact order: {path_text}. Explain what every dot contributes."
        result = self.generate(provider, mode="join_dots", direction=steer,
                               selected_nodes=selected, surprise=surprise,
                               count=count, seed=seed)
        path_id = _id("path")
        with self._connect() as conn:
            conn.execute("INSERT INTO join_paths VALUES (?, ?, ?, ?, ?, ?, ?)",
                         (path_id, result["generation_id"], seed, _json(points or []),
                          _json([n["id"] for n in selected]), surprise, _now()))
            self._event(conn, "join_path", path_id, "generated", "human",
                        {"nodes": [n["id"] for n in selected], "seed": seed})
        result.update({"path_id": path_id, "selected_nodes": selected,
                       "path": path_text, "surprise": surprise})
        return result

    def snapshot(self) -> dict:
        return {"schema_version": SCHEMA_VERSION, "exported_at": _now(),
                "ideas": self.search(limit=10_000),
                "candidates": self.list_candidates(limit=10_000),
                "map": self.map_data(feature_limit=500),
                "events": self.events(limit=10_000)}


def migrate_legacy_brainstorm(journal: IdeaJournal, directory: Path | None = None) -> dict:
    """Import old Brainstorm one-liners once as candidates, never canonical ideas."""
    marker = journal.directory / ".brainstorm-migrated-v1"
    source = Path(directory or (Path.home() / ".cms" / "brainstorm")) / "ideas.json"
    if marker.exists() or not source.is_file():
        return {"imported": 0, "skipped": True}
    try:
        rows = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"imported": 0, "skipped": True}
    imported = 0
    for item in (rows.values() if isinstance(rows, dict) else []):
        title = _clean_text(item.get("text"), 180)
        if not title:
            continue
        candidate = journal.propose_candidate(
            title, title, kind="concept", payload={"legacy": item},
            origin="legacy_brainstorm", actor_kind="import")
        status = item.get("status")
        if status == "disliked":
            journal.decide_candidate(candidate["id"], "rejected")
        elif status == "liked":
            journal.decide_candidate(candidate["id"], "parked")
        imported += 1
    marker.write_text(_now(), encoding="utf-8")
    return {"imported": imported, "skipped": False}


def default_journal() -> IdeaJournal:
    # The override keeps demos/tests isolated while the normal product remains
    # one personal journal shared by every Atlas project.
    override = os.environ.get("CMS_IDEAS_DIR")
    journal = IdeaJournal(Path(override) if override else None)
    migrate_legacy_brainstorm(journal)
    return journal
