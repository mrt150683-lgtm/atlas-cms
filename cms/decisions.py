"""Versioned approved intent — the decision lock.

A decision is a durable, human-approved statement of intended behaviour for
a feature (or the app). It moves through a fixed lifecycle:

    proposed -> approved -> implemented | partially_implemented | failed
             -> rejected                 (any active state) -> superseded

The lock: once a decision is APPROVED its ``intent`` payload can never be
edited — not by an agent, not by the UI. Correcting an approved intent means
proposing a NEW decision with ``supersedes`` set; approval of the successor
marks the ancestor superseded. The full chain stays on disk for audit, so
"what did we agree to, and when did it change?" always has an answer.

Approval is deliberately human-only: the MCP surface can propose and read
decisions but cannot approve them (an agent must never sign off on its own
intent). Store: ``.memory/decisions.json``.
"""

from __future__ import annotations

import json
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

from .semantic_state import atomic_write_json

DECISIONS_FILE = "decisions.json"

STATUSES = ("proposed", "approved", "rejected", "implemented",
            "partially_implemented", "failed", "superseded")
# statuses that count as "the current word" on a feature's intent
ACTIVE_STATUSES = ("proposed", "approved")
CLOSURE_STATUSES = ("implemented", "partially_implemented", "failed")
MAX_TEXT = 2000


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _git_rev(root: Path) -> str:
    try:
        out = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=root,
                             capture_output=True, text=True, timeout=5)
        return out.stdout.strip() if out.returncode == 0 else ""
    except (OSError, subprocess.SubprocessError):
        return ""


def _clean_intent(intent: dict) -> dict:
    if not isinstance(intent, dict) or not str(intent.get("behaviour") or "").strip():
        raise ValueError("a decision needs intent.behaviour — what should happen, in plain words")
    return {
        "behaviour": str(intent["behaviour"])[:MAX_TEXT],
        "inputs": str(intent.get("inputs") or "")[:MAX_TEXT],
        "outputs": str(intent.get("outputs") or "")[:MAX_TEXT],
        "constraints": [str(c)[:400] for c in (intent.get("constraints") or [])][:10],
        "prohibited": [str(p)[:400] for p in (intent.get("prohibited") or [])][:10],
    }


# @memory:feature:ApprovedDecisions
# @memory:connects:ChangeAlignment, StructuredAnnotations, CodebaseChat
# @memory:summary:Durable human-approved intent per feature with an immutability lock — approved intent is never edited, only superseded by an approved successor; agents propose and read but cannot approve.
class DecisionStore:
    def __init__(self, memory_dir: Path, root: Path | None = None) -> None:
        self.memory_dir = Path(memory_dir)
        self.path = self.memory_dir / DECISIONS_FILE
        self.root = Path(root) if root else self.memory_dir.parent

    def _read(self) -> list[dict]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []
        return data.get("decisions", []) if isinstance(data, dict) else []

    def _write(self, decisions: list[dict]) -> None:
        self.memory_dir.mkdir(parents=True, exist_ok=True)
        atomic_write_json(self.path, {"decisions": decisions})

    # -- queries -----------------------------------------------------------

    def get(self, dec_id: str) -> dict | None:
        for d in self._read():
            if d.get("id") == dec_id:
                return d
        return None

    def list(self, *, feature: str | None = None, active_only: bool = False) -> list[dict]:
        rows = self._read()
        if feature:
            rows = [d for d in rows if d.get("feature") == feature]
        if active_only:
            rows = [d for d in rows if d.get("status") in ACTIVE_STATUSES]
        return sorted(rows, key=lambda d: d.get("created_at", ""), reverse=True)

    def approved_for(self, feature: str) -> dict | None:
        """The single current approved intent for a feature (newest wins)."""
        approved = [d for d in self._read()
                    if d.get("feature") == feature and d.get("status") == "approved"]
        approved.sort(key=lambda d: d.get("approved_at", ""), reverse=True)
        return approved[0] if approved else None

    # -- mutations ----------------------------------------------------------

    def propose(self, feature: str | None, title: str, intent: dict, *,
                created_by: dict | None = None, supersedes: str | None = None,
                annotations: list | None = None, evidence: list | None = None) -> dict:
        if not str(title or "").strip():
            raise ValueError("a decision needs a title")
        decisions = self._read()
        if supersedes and not any(d.get("id") == supersedes for d in decisions):
            raise ValueError(f"cannot supersede unknown decision {supersedes!r}")
        author = dict(created_by or {})
        author.setdefault("kind", "user")
        author.setdefault("identity", author["kind"])
        entry = {
            "id": f"dec-{int(time.time() * 1000):x}-{len(decisions) % 997:03d}",
            "version": 1 + sum(1 for d in decisions
                               if d.get("feature") == feature) if feature else 1,
            "supersedes": supersedes,
            "feature": feature or None,
            "title": str(title)[:200],
            "intent": _clean_intent(intent),
            "status": "proposed",
            "created_by": author,
            "approved_by": None,
            "approved_at": None,
            "annotations": [str(a) for a in (annotations or [])][:20],
            "evidence": [str(e) for e in (evidence or [])][:20],
            "created_at": _now_iso(),
            "closed_at": None,
            "revision": _git_rev(self.root),
        }
        decisions.append(entry)
        self._write(decisions)
        return entry

    def approve(self, dec_id: str, approved_by: str) -> dict:
        """Human approval: locks the intent and supersedes the ancestor."""
        if not str(approved_by or "").strip():
            raise ValueError("approval requires a human identity (approved_by)")
        decisions = self._read()
        target = next((d for d in decisions if d.get("id") == dec_id), None)
        if target is None:
            raise ValueError(f"unknown decision {dec_id!r}")
        if target["status"] != "proposed":
            raise ValueError(f"only proposed decisions can be approved "
                             f"(this one is {target['status']!r})")
        # no intent shadowing: a feature has ONE operative approved intent.
        # Approving a second, unlinked decision would silently change the
        # ground truth while the first still reads "approved" — refuse unless
        # this proposal explicitly supersedes the current one.
        if target.get("feature"):
            shadowed = next(
                (d for d in decisions
                 if d.get("feature") == target["feature"]
                 and d.get("status") == "approved"
                 and d.get("id") != target.get("supersedes")), None)
            if shadowed:
                raise ValueError(
                    f"feature {target['feature']!r} already has approved decision "
                    f"{shadowed['id']} — propose with supersedes={shadowed['id']!r} "
                    "to replace it (approved intent is never silently shadowed)")
        target["status"] = "approved"
        target["approved_by"] = str(approved_by)[:120]
        target["approved_at"] = _now_iso()
        if target.get("supersedes"):
            for d in decisions:
                if d.get("id") == target["supersedes"] and d.get("status") != "superseded":
                    d["status"] = "superseded"
                    d["closed_at"] = target["approved_at"]
        self._write(decisions)
        return target

    def close(self, dec_id: str, status: str, *, reason: str = "") -> dict:
        """Verification outcome for an approved decision. The intent payload
        stays frozen — only the lifecycle state moves."""
        if status not in ("rejected", *CLOSURE_STATUSES):
            raise ValueError(f"closure status must be one of "
                             f"{('rejected', *CLOSURE_STATUSES)}, not {status!r}")
        decisions = self._read()
        target = next((d for d in decisions if d.get("id") == dec_id), None)
        if target is None:
            raise ValueError(f"unknown decision {dec_id!r}")
        if status == "rejected" and target["status"] != "proposed":
            raise ValueError("only proposed decisions can be rejected")
        if status in CLOSURE_STATUSES and target["status"] != "approved":
            raise ValueError("only approved decisions can be closed with an outcome")
        target["status"] = status
        target["closed_at"] = _now_iso()
        if reason:
            target["closure_reason"] = str(reason)[:400]
        self._write(decisions)
        return target

    def update_intent_guard(self, dec_id: str, *_args, **_kwargs):
        """There is deliberately no way to edit intent. Kept as an explicit
        guard so future maintainers find the rule, not an accident."""
        raise ValueError("approved intent is immutable — propose a superseding decision")
