"""Structured annotations on canonical graph objects.

Where ``notes.py`` anchors free-form highlights to quoted text inside one
file (a presentation concern of the viewer), this store attaches *typed,
lifecycled* annotations to canonical semantic objects — any graph node id
(``file:``/``func:``/``class:``/``feature:``/``component:``/``system:``),
an edge, or a source range. Annotations carry author provenance (user vs
model vs analyzer, with provider/model identity), a status lifecycle, and
immutable-via-supersession history so model observations can never be
silently rewritten.

This does not replace the viewer's quote notes: ``AnnotationStore.list``
merges them in read-only (``legacy: true``) so there is exactly one read
surface for "everything humans and models have said about this object".
Archived and superseded annotations stay on disk for audit but are excluded
from default listings and from model context packs.
"""

from __future__ import annotations

import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

from .notes import NotesStore
from .semantic_state import atomic_write_json

ANNOTATIONS_FILE = "annotations.json"

TYPES = (
    "note", "observation", "intended_change", "instruction", "bug_suspicion",
    "contradiction", "security_concern", "performance_concern",
    "architecture_concern", "question", "decision_link", "verification_result",
)
STATUSES = (
    "open", "under_review", "accepted", "rejected", "resolved",
    "archived", "superseded",
)
# statuses that belong in default listings and model context packs
ACTIVE_STATUSES = ("open", "under_review", "accepted")
AUTHOR_KINDS = ("user", "model", "analyzer")
PRIORITIES = ("low", "normal", "high")
MAX_BODY = 4000
MAX_LIST = 500


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _git_rev(root: Path) -> str:
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"], cwd=root,
            capture_output=True, text=True, timeout=5,
        )
        return out.stdout.strip() if out.returncode == 0 else ""
    except (OSError, subprocess.SubprocessError):
        return ""


def normalize_target(target) -> tuple[str, str]:
    """Accept a node id string, an edge dict/pair, or a source-range dict and
    return ``(target_key, target_kind)``. The key is a stable string so
    annotations are queryable without schema knowledge."""
    if isinstance(target, str) and target.strip():
        t = target.strip()
        if t.startswith("edge:"):
            return t, "edge"
        if t.startswith("range:"):
            return t, "source_range"
        return t, "node"
    if isinstance(target, dict):
        if target.get("edge") and isinstance(target["edge"], (list, tuple)) and len(target["edge"]) == 2:
            src, dst = target["edge"]
            return f"edge:{src}|{dst}", "edge"
        if target.get("path") is not None and target.get("start") is not None:
            end = target.get("end", target["start"])
            return f"range:{target['path']}#{int(target['start'])}-{int(end)}", "source_range"
    if isinstance(target, (list, tuple)) and len(target) == 2:
        return f"edge:{target[0]}|{target[1]}", "edge"
    raise ValueError(
        "target must be a canonical node id, {'edge': [src, dst]}, or "
        "{'path': p, 'start': n, 'end': n}"
    )


# @memory:feature:StructuredAnnotations
# @memory:connects:MemoryViewer, CodebaseChat, MCPServerIntegration
# @memory:summary:Typed, lifecycled annotations on canonical graph targets with author provenance and immutable-via-supersession history; merges the viewer's legacy quote notes into one read surface and feeds only active annotations to model context.
class AnnotationStore:
    def __init__(self, memory_dir: Path, root: Path | None = None) -> None:
        self.memory_dir = Path(memory_dir)
        self.path = self.memory_dir / ANNOTATIONS_FILE
        self.root = Path(root) if root else self.memory_dir.parent

    # -- persistence -------------------------------------------------------

    def _read(self) -> list[dict]:
        import json
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []
        return data.get("annotations", []) if isinstance(data, dict) else []

    def _write(self, annotations: list[dict]) -> None:
        self.memory_dir.mkdir(parents=True, exist_ok=True)
        atomic_write_json(self.path, {"annotations": annotations})

    # -- queries -----------------------------------------------------------

    def get(self, ann_id: str) -> dict | None:
        for a in self._read():
            if a.get("id") == ann_id:
                return a
        return None

    def list(self, *, target: str | None = None, feature: str | None = None,
             status: str | None = None, include_archived: bool = False,
             include_legacy: bool = True) -> list[dict]:
        """The single read surface: structured annotations plus (by default)
        the viewer's quote notes surfaced read-only as ``legacy: true``."""
        out = list(self._read())
        if include_legacy:
            out.extend(self._legacy_notes())
        if target:
            out = [a for a in out if a.get("target") == target]
        if feature:
            out = [a for a in out if a.get("feature") == feature
                   or a.get("target") == f"feature:{feature}"]
        if status:
            out = [a for a in out if a.get("status") == status]
        elif not include_archived:
            out = [a for a in out if a.get("status") not in ("archived", "superseded")]
        out.sort(key=lambda a: a.get("created_at", ""), reverse=True)
        return out[:MAX_LIST]

    def counts(self) -> dict[str, int]:
        """target -> active annotation count, for inspector badges."""
        out: dict[str, int] = {}
        for a in self._read():
            if a.get("status") in ("archived", "superseded"):
                continue
            key = a.get("target", "")
            out[key] = out.get(key, 0) + 1
        return out

    def active_for_context(self, *, feature: str | None = None,
                           targets: list[str] | None = None,
                           limit: int = 8) -> list[dict]:
        """Trimmed active annotations for model context packs (chat, task
        prompts, alignment). Archived/resolved never leak in here."""
        rows = [a for a in self._read() if a.get("status") in ACTIVE_STATUSES]
        if feature:
            rows = [a for a in rows if a.get("feature") == feature
                    or a.get("target") == f"feature:{feature}"]
        elif targets:
            wanted = set(targets)
            rows = [a for a in rows if a.get("target") in wanted]
        rows.sort(key=lambda a: (a.get("priority") != "high", a.get("created_at", "")),
                  )
        return [
            {"id": a["id"], "target": a.get("target"), "type": a.get("type"),
             "status": a.get("status"), "body": (a.get("body") or "")[:400],
             "author": (a.get("author") or {}).get("kind"),
             "confidence": a.get("confidence")}
            for a in rows[:limit]
        ]

    def _legacy_notes(self) -> list[dict]:
        rows = []
        for n in NotesStore(self.memory_dir).all():
            rows.append({
                "id": n.get("id", ""),
                "target": f"file:{n.get('path', '')}",
                "target_kind": "source_range",
                "type": "note",
                "body": n.get("note", ""),
                "payload": {"quote": n.get("quote", ""), "before": n.get("before", ""),
                            "color": n.get("color"), "mode": n.get("mode")},
                "status": "open",
                "author": {"kind": "user", "identity": "viewer"},
                "created_at": n.get("created_at", ""),
                "legacy": True,
            })
        return rows

    # -- mutations ----------------------------------------------------------

    def add(self, target, type: str, body: str, *, author: dict | None = None,
            payload: dict | None = None, confidence: float | None = None,
            priority: str = "normal", evidence: list | None = None,
            feature: str | None = None, tags: list | None = None,
            supersedes: str | None = None, parent_id: str | None = None) -> dict:
        if not str(body or "").strip():
            raise ValueError("an annotation needs a non-empty body")
        target_key, target_kind = normalize_target(target)
        author = dict(author or {})
        if author.get("kind") not in AUTHOR_KINDS:
            author["kind"] = "user"
        author.setdefault("identity", author["kind"])
        entry = {
            "id": f"ann-{int(time.time() * 1000):x}-{len(self._read()) % 997:03d}",
            "target": target_key,
            "target_kind": target_kind,
            "type": type if type in TYPES else "note",
            "body": str(body)[:MAX_BODY],
            "payload": payload if isinstance(payload, dict) else {},
            "status": "open",
            "priority": priority if priority in PRIORITIES else "normal",
            "confidence": max(0.0, min(1.0, float(confidence))) if confidence is not None else None,
            "author": author,
            "evidence": [str(e) for e in (evidence or [])][:20],
            "feature": feature or None,
            "tags": [str(t) for t in (tags or [])][:10],
            "created_at": _now_iso(),
            "updated_at": None,
            "resolved_at": None,
            "archived_at": None,
            "revision": _git_rev(self.root),
            "supersedes": supersedes,
            "parent_id": parent_id,
        }
        annotations = self._read()
        if supersedes:
            old = next((a for a in annotations if a.get("id") == supersedes), None)
            if old is None:
                raise ValueError(f"cannot supersede unknown annotation {supersedes!r}")
            old["status"] = "superseded"
            old["updated_at"] = entry["created_at"]
        annotations.append(entry)
        self._write(annotations)
        return entry

    def set_status(self, ann_id: str, status: str, *, reason: str = "") -> dict | None:
        if status not in STATUSES:
            raise ValueError(f"unknown status {status!r}; expected one of {', '.join(STATUSES)}")
        annotations = self._read()
        for a in annotations:
            if a.get("id") == ann_id:
                a["status"] = status
                a["updated_at"] = _now_iso()
                if status == "resolved":
                    a["resolved_at"] = a["updated_at"]
                if status == "archived":
                    a["archived_at"] = a["updated_at"]
                if reason:
                    a.setdefault("payload", {})["status_reason"] = str(reason)[:400]
                self._write(annotations)
                return a
        return None

    def edit_body(self, ann_id: str, body: str) -> dict | None:
        """User-authored bodies may be edited in place; model-authored bodies
        are immutable — correcting a model observation means superseding it,
        so the record of what the model actually said survives."""
        annotations = self._read()
        for a in annotations:
            if a.get("id") == ann_id:
                if (a.get("author") or {}).get("kind") != "user":
                    raise ValueError(
                        "model-authored annotations are immutable — supersede instead")
                a["body"] = str(body)[:MAX_BODY]
                a["updated_at"] = _now_iso()
                self._write(annotations)
                return a
        return None
