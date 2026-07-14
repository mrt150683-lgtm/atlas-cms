"""The Atlas Library — reusable, versioned agent-context assets.

An asset is a human-readable markdown file with a small frontmatter block
(id / name / type / description, optional tags / requires / conflicts_with,
and for profiles a pinned member list). Lifecycle state — draft/published
versions, content hashes, trust, enablement — lives in an ``index.json``
next to the files; every published version is frozen as a snapshot under
``.versions/<id>/vN.md``. Published content is frozen: changes ship as a
new version (see :func:`edit_published_guard`).

Three scopes layer over each other, ascending precedence:

    built-in   Atlas's own skills/ dir (read-only at runtime)
    user       ~/.cms/library
    project    <root>/skills

The same id at a higher scope shadows the lower one — that IS the override
mechanism (a project sharpening a user preference pack). Composition
(:func:`compose_context`) expands profiles, walks ``requires``, reports
conflicts (never auto-resolves), dedupes, orders by type, and estimates
size; its output carries exact ``{id, version, content_hash}`` provenance
so any agent run using library context is reproducible and auditable.

Publishing requires a human identity and is never exposed over MCP; agents
may only create drafts and attach annotations (target ``asset:<id>``).
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path

from . import config
from .semantic_state import atomic_write_json

INDEX_FILE = "index.json"
VERSIONS_DIR = ".versions"
SCHEMA_VERSION = 1

# Type is data, not a class hierarchy: adding a new asset type is one entry.
# `order` drives rendering (rules before guidance); composites render their
# members, contributing only a preamble of their own.
ASSET_TYPES: dict[str, dict] = {
    "constraint": {"composite": False, "order": 0},
    "preference": {"composite": False, "order": 1},
    "strategy": {"composite": False, "order": 2},
    "skill": {"composite": False, "order": 3},
    "profile": {"composite": True, "order": None},
}

TRUST_LEVELS = ("built-in", "user", "project", "agent", "imported")
STATUSES = ("draft", "published", "deprecated")
SCOPES = ("built-in", "user", "project")  # ascending precedence

_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{2,63}$")
_REF_RE = re.compile(r"^([a-z0-9][a-z0-9-]{2,63})(?:@(\d+))?$")
_LIST_FIELDS = ("tags", "requires", "conflicts_with", "assets")
# canonical frontmatter order for serialization / hashing
_FRONT_ORDER = ("id", "name", "type", "description",
                "tags", "requires", "conflicts_with", "assets")


class LibraryError(ValueError):
    """Any invalid library operation (bad asset, bad ref, wrong lifecycle)."""


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _hash_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:24]


def parse_ref(ref: str) -> tuple[str, int | None]:
    """``id`` or ``id@N`` -> (id, version|None)."""
    m = _REF_RE.match(str(ref or "").strip())
    if not m:
        raise LibraryError(f"invalid asset ref {ref!r} — expected `id` or `id@N` "
                           "(id: lowercase slug, 3-64 chars)")
    return m.group(1), int(m.group(2)) if m.group(2) else None


def slugify(name: str) -> str:
    slug = re.sub(r"-+", "-", re.sub(r"[^a-z0-9]+", "-", str(name).lower())).strip("-")
    slug = slug[:64]
    if not _ID_RE.match(slug):
        raise LibraryError(f"cannot derive a valid asset id from {name!r}")
    return slug


# --- frontmatter -------------------------------------------------------------

def _parse_list(value: str) -> list[str]:
    value = value.strip()
    if value.startswith("[") and value.endswith("]"):
        value = value[1:-1]
    return [p.strip().strip('"').strip("'") for p in value.split(",") if p.strip()]


def parse_asset_text(text: str) -> tuple[dict, str]:
    """Strict frontmatter parser: ``---`` fences, flat ``key: value`` lines,
    lists as ``[a, b]`` or comma-separated. Returns (meta, body). No YAML
    dependency — this grammar is deliberately tiny and fails loudly."""
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        raise LibraryError("asset file must start with a `---` frontmatter fence")
    meta: dict = {}
    end = None
    for i, line in enumerate(lines[1:], start=1):
        if line.strip() == "---":
            end = i
            break
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        key, sep, value = line.partition(":")
        if not sep or not key.strip():
            raise LibraryError(f"frontmatter line {i + 1} is not `key: value`: {line!r}")
        key = key.strip().lower()
        value = value.strip()
        meta[key] = _parse_list(value) if key in _LIST_FIELDS else value.strip('"').strip("'")
    if end is None:
        raise LibraryError("frontmatter fence `---` is never closed")
    body = "\n".join(lines[end + 1:]).strip("\n")
    return meta, body


def validate_meta(meta: dict) -> dict:
    """Validate + normalize frontmatter. Unknown keys are dropped (import
    tolerance); required keys fail loudly. Returns the clean meta."""
    asset_id = str(meta.get("id") or "").strip()
    if not _ID_RE.match(asset_id):
        raise LibraryError(f"invalid asset id {asset_id!r} — lowercase slug, 3-64 chars "
                           "([a-z0-9-], starts alphanumeric)")
    atype = str(meta.get("type") or "").strip()
    if atype not in ASSET_TYPES:
        raise LibraryError(f"unknown asset type {atype!r} — one of {sorted(ASSET_TYPES)}")
    name = str(meta.get("name") or "").strip()
    description = str(meta.get("description") or "").strip()
    if not name or not description:
        raise LibraryError("an asset needs both `name` and `description`")
    clean = {"id": asset_id, "type": atype, "name": name[:120], "description": description[:500]}
    for field in ("tags", "requires", "conflicts_with"):
        values = [str(v).strip() for v in (meta.get(field) or []) if str(v).strip()]
        if field == "requires":
            for ref in values:
                parse_ref(ref)
        if field == "conflicts_with":
            for ref in values:
                if not _ID_RE.match(ref):
                    raise LibraryError(f"conflicts_with entries are bare ids, got {ref!r}")
        if values:
            clean[field] = values[:20]
    members = [str(v).strip() for v in (meta.get("assets") or []) if str(v).strip()]
    if atype == "profile":
        if not members:
            raise LibraryError("a profile needs an `assets` list of pinned members (id@N)")
        for ref in members:
            _, version = parse_ref(ref)
            if version is None:
                raise LibraryError(f"profile members must be pinned (`id@N`), got {ref!r} — "
                                   "profiles reference exact versions, never copy content")
        clean["assets"] = members[:40]
    elif members:
        raise LibraryError("only profiles carry an `assets` member list")
    return clean


def serialize_asset(meta: dict, body: str) -> str:
    """Canonical text form: frontmatter in fixed key order + body. Hashing
    and snapshots always use this form, so key order never causes drift."""
    lines = ["---"]
    for key in _FRONT_ORDER:
        value = meta.get(key)
        if value is None or value == [] or value == "":
            continue
        if isinstance(value, list):
            lines.append(f"{key}: [{', '.join(value)}]")
        else:
            lines.append(f"{key}: {value}")
    lines.append("---")
    lines.append("")
    lines.append(body.strip("\n"))
    return "\n".join(lines).rstrip("\n") + "\n"


def canonical_text(text: str) -> tuple[dict, str, str]:
    """Parse raw asset text -> (meta, body, canonical serialization)."""
    meta, body = parse_asset_text(text)
    clean = validate_meta(meta)
    return clean, body, serialize_asset(clean, body)


def edit_published_guard(*_args, **_kwargs):
    """There is deliberately no way to edit a published version. Kept as an
    explicit guard so future maintainers find the rule, not an accident."""
    raise LibraryError("published content is frozen — edit the draft file and "
                       "publish a new version")


# --- one scope ----------------------------------------------------------------

# @memory:feature:AtlasLibrary
# @memory:summary:One scope's assets: markdown drafts + index.json lifecycle state + frozen .versions snapshots; publish requires a human identity, built-in scope rejects writes.
class LibraryStore:
    """One scope's assets: markdown files + index.json + .versions/ snapshots.

    ``read_only`` marks the built-in scope: writes are refused, and when no
    index.json is present every ``*.md`` file is served as published v1
    (a bare directory of skill files is a valid built-in library)."""

    def __init__(self, scope_dir: Path, scope: str, *, read_only: bool = False) -> None:
        self.dir = Path(scope_dir)
        self.scope = scope
        self.read_only = read_only
        self.index_path = self.dir / INDEX_FILE

    # -- index io ------------------------------------------------------------

    def _read_index(self) -> dict:
        try:
            data = json.loads(self.index_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = {}
        if not isinstance(data, dict):
            data = {}
        data.setdefault("schema_version", SCHEMA_VERSION)
        data.setdefault("assets", [])
        data.setdefault("overrides", {})
        return data

    def _write_index(self, data: dict) -> None:
        self._guard_writable()
        data["assets"] = sorted(data["assets"], key=lambda r: r.get("id", ""))
        self.dir.mkdir(parents=True, exist_ok=True)
        atomic_write_json(self.index_path, data)

    def _guard_writable(self) -> None:
        if self.read_only:
            raise LibraryError("the built-in scope is read-only — shadow the asset "
                               "at user or project scope instead")

    # -- paths (always derived from the id — input never becomes a path) ------

    def asset_path(self, asset_id: str) -> Path:
        if not _ID_RE.match(asset_id):
            raise LibraryError(f"invalid asset id {asset_id!r}")
        return self.dir / f"{asset_id}.md"

    def snapshot_path(self, asset_id: str, version: int) -> Path:
        if not _ID_RE.match(asset_id):
            raise LibraryError(f"invalid asset id {asset_id!r}")
        return self.dir / VERSIONS_DIR / asset_id / f"v{int(version)}.md"

    # -- reads -----------------------------------------------------------------

    def records(self) -> list[dict]:
        """Every asset this scope knows: index records plus unregistered
        ``*.md`` files (visible as drafts, one publish away from real).
        In a read-only scope without an index, files ARE published v1."""
        index = self._read_index()
        by_id = {r["id"]: dict(r) for r in index["assets"] if r.get("id")}
        rows: list[dict] = []
        for rec in by_id.values():
            rec["scope"] = self.scope
            rec["dirty"] = self._dirty(rec)
            rec["missing_file"] = not self.asset_path(rec["id"]).is_file()
            rows.append(rec)
        try:
            files = sorted(self.dir.glob("*.md"))
        except OSError:
            files = []
        for path in files:
            stem = path.stem
            if stem in by_id or not _ID_RE.match(stem):
                continue
            try:
                meta, body, canon = canonical_text(path.read_text(encoding="utf-8"))
            except (OSError, LibraryError):
                continue
            if meta["id"] != stem:
                continue  # the filename is the id; a mismatched file is not served
            if self.read_only and not self.index_path.is_file():
                rows.append(self._synthesized(meta, canon))
            else:
                rows.append({**self._blank_record(meta), "scope": self.scope,
                             "registered": False, "dirty": False, "missing_file": False})
        return sorted(rows, key=lambda r: r["id"])

    def get(self, asset_id: str) -> dict | None:
        for rec in self.records():
            if rec["id"] == asset_id:
                return rec
        return None

    def overrides(self) -> dict:
        return dict(self._read_index()["overrides"])

    def _dirty(self, rec: dict) -> bool:
        """Derived, never stored: does the working file differ from the last
        published snapshot?"""
        versions = rec.get("versions") or []
        if not versions:
            return False
        try:
            _, _, canon = canonical_text(self.asset_path(rec["id"]).read_text(encoding="utf-8"))
        except (OSError, LibraryError):
            return True
        return _hash_text(canon) != versions[-1].get("content_hash")

    def _synthesized(self, meta: dict, canon: str) -> dict:
        """A bare built-in file, served as published v1 with a live hash."""
        return {
            "id": meta["id"], "type": meta["type"], "name": meta["name"],
            "description": meta["description"], "tags": meta.get("tags", []),
            "path": f"{meta['id']}.md", "status": "published", "enabled": True,
            "trust": "built-in", "current_version": 1,
            "versions": [{"version": 1, "content_hash": _hash_text(canon),
                          "snapshot": f"{meta['id']}.md",
                          "published_at": None, "published_by": None}],
            "created_by": {"kind": "builtin", "identity": "atlas"},
            "created_at": None, "updated_at": None,
            "scope": self.scope, "dirty": False, "missing_file": False,
        }

    def _blank_record(self, meta: dict) -> dict:
        return {
            "id": meta["id"], "type": meta["type"], "name": meta["name"],
            "description": meta["description"], "tags": meta.get("tags", []),
            "path": f"{meta['id']}.md", "status": "draft", "enabled": True,
            "trust": "built-in" if self.scope == "built-in" else self.scope,
            "current_version": None, "versions": [],
            "created_by": {"kind": "user", "identity": "user"},
            "created_at": _now_iso(), "updated_at": _now_iso(),
        }

    def load_asset(self, rec: dict, version: int | None = None,
                   *, draft: bool = False) -> dict:
        """Resolve content for a record: a published snapshot (default latest,
        or the pinned version) or the working draft file. Returns
        {meta, body, content, content_hash, version, draft}."""
        if draft or not rec.get("versions"):
            path = self.asset_path(rec["id"])
            try:
                meta, body, canon = canonical_text(path.read_text(encoding="utf-8"))
            except OSError as exc:
                raise LibraryError(f"asset file missing for {rec['id']!r}: {exc}") from exc
            return {"meta": meta, "body": body, "content": canon,
                    "content_hash": _hash_text(canon), "version": None, "draft": True}
        versions = rec["versions"]
        if version is None:
            vrec = versions[-1]
        else:
            vrec = next((v for v in versions if v.get("version") == version), None)
            if vrec is None:
                raise LibraryError(f"{rec['id']!r} has no published version {version}")
        snap = self.dir / VERSIONS_DIR / rec["id"] / f"v{vrec['version']}.md"
        if not snap.is_file():  # synthesized built-in: snapshot IS the file
            snap = self.dir / vrec.get("snapshot", f"{rec['id']}.md")
        try:
            meta, body, canon = canonical_text(snap.read_text(encoding="utf-8"))
        except OSError as exc:
            raise LibraryError(f"snapshot missing for {rec['id']!r} v{vrec['version']}") from exc
        return {"meta": meta, "body": body, "content": canon,
                "content_hash": vrec.get("content_hash") or _hash_text(canon),
                "version": vrec["version"], "draft": False}

    # -- mutations ---------------------------------------------------------------

    def save_draft(self, text: str, *, created_by: dict | None = None,
                   trust: str | None = None, expect_id: str | None = None) -> dict:
        """Create or update a draft from raw asset text. The working file is
        the draft surface; published snapshots are untouched."""
        self._guard_writable()
        meta, body, canon = canonical_text(text)
        if expect_id and meta["id"] != expect_id:
            raise LibraryError(f"asset text declares id {meta['id']!r}, expected {expect_id!r}")
        author = dict(created_by or {})
        author.setdefault("kind", "user")
        author.setdefault("identity", author["kind"])
        if trust and trust not in TRUST_LEVELS:
            raise LibraryError(f"unknown trust level {trust!r}")
        index = self._read_index()
        rec = next((r for r in index["assets"] if r.get("id") == meta["id"]), None)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.asset_path(meta["id"]).write_text(canon, encoding="utf-8")
        if rec is None:
            rec = self._blank_record(meta)
            rec["trust"] = trust or ("agent" if author["kind"] == "model" else
                                     ("built-in" if self.scope == "built-in" else self.scope))
            rec["created_by"] = author
            index["assets"].append(rec)
        else:
            for key in ("type", "name", "description"):
                rec[key] = meta[key]
            rec["tags"] = meta.get("tags", [])
            rec["updated_at"] = _now_iso()
        self._write_index(index)
        return {**rec, "scope": self.scope}

    def register_file(self, asset_id: str, *, created_by: dict | None = None,
                      trust: str | None = None) -> dict:
        """Adopt an unregistered on-disk file into the index (as a draft).
        Adoption is always explicit — never silent."""
        path = self.asset_path(asset_id)
        if not path.is_file():
            raise LibraryError(f"no file {path.name} in the {self.scope} scope")
        return self.save_draft(path.read_text(encoding="utf-8"),
                               created_by=created_by, trust=trust, expect_id=asset_id)

    def publish(self, asset_id: str, published_by: str) -> dict:
        """Freeze the current draft as the next published version. Requires a
        human identity, exactly like decision approval."""
        self._guard_writable()
        if not str(published_by or "").strip():
            raise LibraryError("publishing requires a human identity (published_by)")
        index = self._read_index()
        rec = next((r for r in index["assets"] if r.get("id") == asset_id), None)
        if rec is None:
            self.register_file(asset_id)
            index = self._read_index()
            rec = next((r for r in index["assets"] if r.get("id") == asset_id), None)
        path = self.asset_path(asset_id)
        try:
            meta, body, canon = canonical_text(path.read_text(encoding="utf-8"))
        except OSError as exc:
            raise LibraryError(f"no draft file for {asset_id!r}") from exc
        chash = _hash_text(canon)
        versions = rec.get("versions") or []
        if versions and versions[-1].get("content_hash") == chash:
            raise LibraryError(f"nothing changed since v{versions[-1]['version']} — "
                               "edit the draft first (published content is frozen)")
        number = (versions[-1]["version"] + 1) if versions else 1
        snap = self.snapshot_path(asset_id, number)
        snap.parent.mkdir(parents=True, exist_ok=True)
        snap.write_text(canon, encoding="utf-8")
        versions.append({"version": number, "content_hash": chash,
                         "snapshot": str(snap.relative_to(self.dir)).replace("\\", "/"),
                         "published_at": _now_iso(),
                         "published_by": str(published_by)[:120]})
        rec.update({"versions": versions, "current_version": number,
                    "status": "published", "type": meta["type"], "name": meta["name"],
                    "description": meta["description"], "tags": meta.get("tags", []),
                    "updated_at": _now_iso()})
        self._write_index(index)
        return {**rec, "scope": self.scope}

    def deprecate(self, asset_id: str) -> dict:
        self._guard_writable()
        index = self._read_index()
        rec = next((r for r in index["assets"] if r.get("id") == asset_id), None)
        if rec is None:
            raise LibraryError(f"unknown asset {asset_id!r} in the {self.scope} scope")
        if rec.get("status") != "published":
            raise LibraryError("only published assets can be deprecated")
        rec["status"] = "deprecated"
        rec["updated_at"] = _now_iso()
        self._write_index(index)
        return {**rec, "scope": self.scope}

    def set_enabled(self, asset_id: str, enabled: bool) -> dict:
        """Flip a local asset's enablement, or record an override for an
        asset inherited from a lower-precedence scope. Never deletes."""
        self._guard_writable()
        index = self._read_index()
        rec = next((r for r in index["assets"] if r.get("id") == asset_id), None)
        if rec is not None:
            rec["enabled"] = bool(enabled)
            rec["updated_at"] = _now_iso()
        else:
            if enabled:
                index["overrides"].pop(asset_id, None)
            else:
                index["overrides"][asset_id] = {"enabled": False}
        self._write_index(index)
        return rec or {"id": asset_id, "override": {"enabled": bool(enabled)}}

    def verify_integrity(self) -> list[dict]:
        """Re-hash every published snapshot against its recorded hash."""
        problems = []
        for rec in self.records():
            for vrec in rec.get("versions") or []:
                snap = self.dir / (vrec.get("snapshot") or "")
                try:
                    actual = _hash_text(snap.read_text(encoding="utf-8"))
                except OSError:
                    problems.append({"id": rec["id"], "version": vrec.get("version"),
                                     "problem": "snapshot-missing"})
                    continue
                if actual != vrec.get("content_hash"):
                    problems.append({"id": rec["id"], "version": vrec.get("version"),
                                     "problem": "hash-mismatch"})
        return problems


# --- merged view ---------------------------------------------------------------

# @memory:feature:AtlasLibrary
# @memory:connects:PromptExport, StructuredAnnotations
# @memory:summary:Merged built-in/user/project view with project-wins precedence; compose() expands profiles, walks requires, reports conflicts and shadowing, dedupes, orders, estimates size, and returns exact version provenance.
class LibraryView:
    """The three scopes merged, ascending precedence: built-in, user, project."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        project_dir = self.root / config.LIBRARY_DIR_NAME
        builtin_dir = config.library_builtin_dir()
        self.stores: list[LibraryStore] = []
        try:
            same = builtin_dir.resolve() == project_dir.resolve()
        except OSError:
            same = False
        if not same:
            self.stores.append(LibraryStore(builtin_dir, "built-in", read_only=True))
        self.stores.append(LibraryStore(config.LIBRARY_USER_DIR, "user"))
        self.stores.append(LibraryStore(project_dir, "project"))

    def store(self, scope: str) -> LibraryStore:
        for st in self.stores:
            if st.scope == scope:
                return st
        raise LibraryError(f"unknown scope {scope!r} — one of "
                           f"{[s.scope for s in self.stores]}")

    # -- queries -------------------------------------------------------------

    def _by_id(self) -> dict[str, list[tuple[LibraryStore, dict]]]:
        """id -> [(store, record)] in ascending precedence order."""
        out: dict[str, list[tuple[LibraryStore, dict]]] = {}
        for st in self.stores:
            for rec in st.records():
                out.setdefault(rec["id"], []).append((st, rec))
        return out

    def _effective_enabled(self, asset_id: str, rec: dict) -> bool:
        """Record enablement, then overrides ascending — highest scope wins."""
        enabled = bool(rec.get("enabled", True))
        for st in self.stores:
            ov = st.overrides().get(asset_id)
            if ov is not None and "enabled" in ov:
                enabled = bool(ov["enabled"])
        return enabled

    def effective(self, asset_id: str) -> tuple[dict, LibraryStore, bool] | None:
        """Highest-precedence record for an id (+ its store + enablement)."""
        rows = self._by_id().get(asset_id)
        if not rows:
            return None
        store, rec = rows[-1]
        return rec, store, self._effective_enabled(asset_id, rec)

    def list(self, *, type: str | None = None, tag: str | None = None,
             status: str | None = None, scope: str | None = None,
             q: str | None = None) -> list[dict]:
        """Every visible asset with shadowing marked; filters are ANDed."""
        rows: list[dict] = []
        for asset_id, entries in sorted(self._by_id().items()):
            winner_scope = entries[-1][0].scope
            for st, rec in entries:
                row = dict(rec)
                row["effective"] = st.scope == winner_scope
                row["shadowed_by"] = winner_scope if st.scope != winner_scope else None
                row["enabled_effective"] = self._effective_enabled(asset_id, rec)
                rows.append(row)
        if type:
            rows = [r for r in rows if r.get("type") == type]
        if tag:
            rows = [r for r in rows if tag in (r.get("tags") or [])]
        if status:
            rows = [r for r in rows if r.get("status") == status]
        if scope:
            rows = [r for r in rows if r.get("scope") == scope]
        if q:
            needle = q.lower()
            rows = [r for r in rows
                    if needle in r.get("id", "").lower()
                    or needle in (r.get("name") or "").lower()
                    or needle in (r.get("description") or "").lower()
                    or any(needle in t.lower() for t in r.get("tags") or [])]
        return rows

    def get(self, asset_id: str, version: int | None = None) -> dict:
        """Full inspection payload for one asset (effective record)."""
        hit = self.effective(asset_id)
        if hit is None:
            raise LibraryError(f"unknown asset {asset_id!r}")
        rec, store, enabled = hit
        loaded = store.load_asset(rec, version,
                                  draft=version is None and not rec.get("versions"))
        entries = self._by_id().get(asset_id, [])
        return {**rec, "enabled_effective": enabled, "content": loaded["content"],
                "body": loaded["body"], "meta": loaded["meta"],
                "resolved_version": loaded["version"], "draft": loaded["draft"],
                "shadowed_scopes": [st.scope for st, _ in entries[:-1]]}

    # -- composition -----------------------------------------------------------

    def compose(self, selection: list[str], *, include_drafts: bool = False) -> dict:
        """Resolve a selection of refs into an ordered, deduped, size-estimated
        context with warnings and conflicts reported, never auto-resolved."""
        warnings: list[dict] = []
        pins: dict[str, tuple[int | None, int]] = {}   # id -> (version, depth)
        visited: set[str] = set()
        merged = self._by_id()

        def merge_pin(asset_id: str, version: int | None, depth: int) -> None:
            if asset_id not in pins:
                pins[asset_id] = (version, depth)
                return
            old_v, old_d = pins[asset_id]
            if version is not None and old_v is not None and version != old_v:
                keep, lose = ((version, depth), (old_v, old_d)) if depth < old_d \
                    else ((old_v, old_d), (version, depth))
                warnings.append({"kind": "version-pin-clash", "id": asset_id,
                                 "kept": keep[0], "dropped": lose[0]})
                pins[asset_id] = keep
            elif old_v is None and version is not None:
                pins[asset_id] = (version, depth)

        def resolution_meta(asset_id: str) -> dict | None:
            hit = self.effective(asset_id)
            if hit is None:
                return None
            rec, store, _ = hit
            try:
                loaded = store.load_asset(rec, None, draft=not rec.get("versions"))
            except LibraryError:
                return None
            return loaded["meta"]

        def walk(ref: str, depth: int, chain: tuple[str, ...]) -> None:
            try:
                asset_id, version = parse_ref(ref)
            except LibraryError:
                warnings.append({"kind": "invalid-ref", "ref": str(ref)})
                return
            if asset_id in chain:
                warnings.append({"kind": "circular-reference", "id": asset_id,
                                 "chain": list(chain)})
                return
            merge_pin(asset_id, version, depth)
            if asset_id in visited:
                return
            visited.add(asset_id)
            if asset_id not in merged:
                warnings.append({"kind": "missing-selection" if depth == 0
                                 else "missing-dependency", "id": asset_id})
                return
            meta = resolution_meta(asset_id)
            if meta is None:
                warnings.append({"kind": "unreadable-asset", "id": asset_id})
                return
            for dep in meta.get("requires") or []:
                walk(dep, depth + 1, chain + (asset_id,))
            if meta.get("type") == "profile":
                for member in meta.get("assets") or []:
                    walk(member, depth + 1, chain + (asset_id,))

        for ref in selection:
            walk(ref, 0, ())

        assets: list[dict] = []
        shadowed: list[dict] = []
        for asset_id in sorted(visited):
            entries = merged.get(asset_id)
            if not entries:
                continue
            if len(entries) > 1:
                shadowed.append({"id": asset_id, "winning_scope": entries[-1][0].scope,
                                 "shadowed_scopes": [st.scope for st, _ in entries[:-1]]})
            rec, store, enabled = self.effective(asset_id)  # type: ignore[misc]
            if not enabled:
                warnings.append({"kind": "disabled-dependency", "id": asset_id})
                continue
            pin, _depth = pins.get(asset_id, (None, 0))
            published = bool(rec.get("versions"))
            if not published:
                if not include_drafts:
                    warnings.append({"kind": "unpublished-asset", "id": asset_id,
                                     "status": rec.get("status")})
                    continue
            elif rec.get("status") == "deprecated" and pin is None:
                warnings.append({"kind": "deprecated-dependency", "id": asset_id})
                continue
            try:
                loaded = store.load_asset(rec, pin, draft=not published)
            except LibraryError as exc:
                warnings.append({"kind": "unresolvable-version", "id": asset_id,
                                 "detail": str(exc)})
                continue
            assets.append({
                "id": asset_id, "version": loaded["version"],
                "content_hash": loaded["content_hash"], "scope": store.scope,
                "trust": rec.get("trust"), "type": loaded["meta"]["type"],
                "name": loaded["meta"]["name"],
                "description": loaded["meta"]["description"],
                "conflicts_with": loaded["meta"].get("conflicts_with", []),
                "content": loaded["body"], "draft": loaded["draft"],
            })

        conflicts: list[dict] = []
        for i, a in enumerate(assets):
            for b in assets[i + 1:]:
                declared = []
                if b["id"] in a.get("conflicts_with", []):
                    declared.append(a["id"])
                if a["id"] in b.get("conflicts_with", []):
                    declared.append(b["id"])
                if declared:
                    conflicts.append({"a": a["id"], "b": b["id"],
                                      "declared_by": declared})

        def sort_key(asset: dict) -> tuple:
            order = ASSET_TYPES.get(asset["type"], {}).get("order")
            return (0 if asset["type"] == "profile" else 1,
                    order if order is not None else -1, asset["id"])

        assets.sort(key=sort_key)
        est_chars = sum(len(a["content"]) for a in assets)
        return {
            "assets": assets, "shadowed": shadowed, "warnings": warnings,
            "conflicts": conflicts, "est_chars": est_chars,
            "est_tokens": est_chars // 4,
            "oversized": est_chars > config.LIBRARY_WARN_CHARS,
        }

    def verify_integrity(self) -> list[dict]:
        problems = []
        for st in self.stores:
            for p in st.verify_integrity():
                problems.append({**p, "scope": st.scope})
        return problems


# --- module-level conveniences ---------------------------------------------------

def compose_context(root: Path, selection: list[str], *,
                    include_drafts: bool = False) -> dict:
    return LibraryView(root).compose(selection, include_drafts=include_drafts)


def render_assets(result: dict, flavor: str = "markdown") -> str:
    """Render a compose result for an agent. The canonical form is
    provider-neutral markdown; other flavors are future adapter branches."""
    if flavor != "markdown":
        raise LibraryError(f"unknown render flavor {flavor!r} (markdown only for now)")
    lines: list[str] = []
    for w in result.get("warnings") or []:
        detail = ", ".join(f"{k}={v}" for k, v in w.items() if k != "kind")
        lines.append(f"> WARNING [{w['kind']}] {detail}")
    for c in result.get("conflicts") or []:
        lines.append(f"> CONFLICT: {c['a']} vs {c['b']} (declared by "
                     f"{', '.join(c['declared_by'])}) — both included, resolve by hand")
    if lines:
        lines.append("")
    for asset in result.get("assets") or []:
        version = f"v{asset['version']}" if asset.get("version") else "draft"
        lines.append(f"### [{asset['type']}] {asset['name']} "
                     f"({asset['id']}@{version}, {asset['scope']}, {asset['trust']})")
        if asset.get("content"):
            lines.append(asset["content"])
        lines.append("")
    return "\n".join(lines).strip() + "\n" if lines else ""


def import_asset(root: Path, text: str, *, scope: str = "project",
                 filename: str = "", created_by: dict | None = None) -> dict:
    """Import a markdown skill file (Claude-skill style: at least name +
    description). Missing Atlas fields are defaulted; the result is a DRAFT
    with trust `imported` — imported content never auto-publishes."""
    view = LibraryView(root)
    store = view.store(scope)
    meta, body = parse_asset_text(text)
    name = str(meta.get("name") or "").strip() or Path(filename).stem.replace("_", " ").strip()
    if not name:
        raise LibraryError("imported asset needs a `name` (frontmatter or filename)")
    meta.setdefault("description", name)
    meta["name"] = name
    if not meta.get("id"):
        meta["id"] = slugify(name)
    if str(meta.get("type") or "") not in ASSET_TYPES:
        meta["type"] = "skill"
    clean = validate_meta(meta)
    if store.get(clean["id"]) is not None:
        raise LibraryError(f"asset {clean['id']!r} already exists in the {scope} scope — "
                           "delete it or import under a different id")
    return store.save_draft(serialize_asset(clean, body), created_by=created_by,
                            trust="imported")


def export_asset(root: Path, asset_id: str, version: int | None = None) -> str:
    """Canonical markdown for an asset (latest published, pinned version, or
    the draft when nothing is published). Round-trips through import."""
    view = LibraryView(root)
    return view.get(asset_id, version)["content"]


def new_asset_template(asset_id: str, atype: str, name: str = "") -> str:
    if atype not in ASSET_TYPES:
        raise LibraryError(f"unknown asset type {atype!r} — one of {sorted(ASSET_TYPES)}")
    title = name or asset_id.replace("-", " ").title()
    meta = {"id": asset_id, "name": title, "type": atype,
            "description": f"Describe what {title} does and when to load it."}
    if atype == "profile":
        raise LibraryError("write profiles by hand: set `assets: [id@1, other@2]` "
                           "to pin members, then publish")
    body = (f"# {title}\n\nCanonical agent-facing content. Write the actual "
            "instructions here — this body is what agents receive verbatim.")
    return serialize_asset(validate_meta(meta), body)
