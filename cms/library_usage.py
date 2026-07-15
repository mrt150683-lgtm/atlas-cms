"""Evidence and human feedback for Atlas Library asset use.

The usage ledger is deliberately separate from canonical Library assets.  Agents
may append observations, while human ratings remain visibly human-authored and
never silently rewrite the agent's original assessment.
"""

from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import secrets
import threading
from typing import Any, Iterable

from .semantic_state import atomic_write_json


OUTCOMES = {"success", "partial", "failure", "unknown"}
_LEDGER_LOCK = threading.RLock()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _score(value: Any, field: str) -> int | None:
    if value in (None, ""):
        return None
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field} must be an integer from 1 to 5") from exc
    if number < 1 or number > 5:
        raise ValueError(f"{field} must be between 1 and 5")
    return number


def _metric(value: Any, field: str) -> int | None:
    if value in (None, ""):
        return None
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field} must be a non-negative integer") from exc
    if number < 0:
        raise ValueError(f"{field} must be a non-negative integer")
    return number


def _average(values: Iterable[int | float | None]) -> float | None:
    present = [float(value) for value in values if value is not None]
    return round(sum(present) / len(present), 2) if present else None


class LibraryUsageStore:
    """Append-oriented use ledger with separate human feedback entries."""

    def __init__(self, memory_dir: Path):
        self.memory_dir = Path(memory_dir)
        self.path = self.memory_dir / "library_usage.json"

    def _read(self) -> dict[str, Any]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8")) if self.path.exists() else {}
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError(f"Library usage ledger is unreadable: {self.path}") from exc
        if not isinstance(data, dict):
            data = {}
        events = data.get("events")
        if not isinstance(events, list):
            events = []
        return {"schema": 1, "events": events}

    def _write(self, data: dict[str, Any]) -> None:
        self.memory_dir.mkdir(parents=True, exist_ok=True)
        atomic_write_json(self.path, data)

    def record(
        self,
        assets: list[dict[str, Any]],
        *,
        task: str,
        outcome: str = "unknown",
        effectiveness: Any = None,
        efficiency: Any = None,
        duration_ms: Any = None,
        input_tokens: Any = None,
        output_tokens: Any = None,
        model: str | None = None,
        notes: str | None = None,
        source: str = "agent",
        client: str | None = None,
    ) -> dict[str, Any]:
        if not assets:
            raise ValueError("at least one resolved asset is required")
        task = str(task or "").strip()
        if not task:
            raise ValueError("task is required")
        outcome = str(outcome or "unknown").strip().lower()
        if outcome not in OUTCOMES:
            raise ValueError(f"outcome must be one of: {', '.join(sorted(OUTCOMES))}")

        resolved: list[dict[str, Any]] = []
        for asset in assets:
            asset_id = str(asset.get("id", "")).strip()
            if not asset_id:
                raise ValueError("every resolved asset must include id")
            resolved.append({
                "id": asset_id,
                "version": asset.get("version"),
                "hash": str(asset.get("content_hash") or asset.get("hash") or ""),
                "scope": str(asset.get("scope", "")),
                "trust": str(asset.get("trust", "")),
                "type": str(asset.get("type", "")),
            })

        event = {
            "id": f"use-{secrets.token_hex(8)}",
            "created_at": _now(),
            "task": task,
            "outcome": outcome,
            "assets": resolved,
            "agent_assessment": {
                "effectiveness": _score(effectiveness, "effectiveness"),
                "efficiency": _score(efficiency, "efficiency"),
                "notes": str(notes or "").strip(),
            },
            "metrics": {
                "duration_ms": _metric(duration_ms, "duration_ms"),
                "input_tokens": _metric(input_tokens, "input_tokens"),
                "output_tokens": _metric(output_tokens, "output_tokens"),
            },
            "model": str(model or "").strip(),
            "source": str(source or "agent").strip(),
            "client": str(client or "").strip(),
            "human_feedback": [],
        }
        with _LEDGER_LOCK:
            data = self._read()
            data["events"].append(event)
            self._write(data)
        return event

    def rate(
        self,
        use_id: str,
        *,
        rating: Any = None,
        effectiveness: Any = None,
        efficiency: Any = None,
        comment: str | None = None,
        rated_by: str = "user",
    ) -> dict[str, Any]:
        comment = str(comment or "").strip()
        feedback = {
            "id": f"rating-{secrets.token_hex(8)}",
            "created_at": _now(),
            "rated_by": str(rated_by or "user").strip(),
            "rating": _score(rating, "rating"),
            "effectiveness": _score(effectiveness, "effectiveness"),
            "efficiency": _score(efficiency, "efficiency"),
            "comment": comment,
        }
        if not comment and all(feedback[key] is None for key in ("rating", "effectiveness", "efficiency")):
            raise ValueError("provide a rating, effectiveness, efficiency, or comment")

        with _LEDGER_LOCK:
            data = self._read()
            for event in data["events"]:
                if event.get("id") != use_id:
                    continue
                entries = event.setdefault("human_feedback", [])
                if not isinstance(entries, list):
                    entries = []
                    event["human_feedback"] = entries
                entries.append(feedback)
                self._write(data)
                return {"use_id": use_id, "feedback": feedback}
        raise ValueError(f"unknown Library use event: {use_id}")

    def events(self, asset_id: str | None = None, *, limit: int = 30) -> list[dict[str, Any]]:
        rows = self._read()["events"]
        if asset_id:
            rows = [
                event for event in rows
                if any(asset.get("id") == asset_id for asset in event.get("assets", []))
            ]
        return list(reversed(rows[-max(1, int(limit)):]))

    def summary(self, asset_id: str | None = None) -> dict[str, Any]:
        events = self._read()["events"]
        if asset_id:
            events = [event for event in events
                      if any(asset.get("id") == asset_id for asset in event.get("assets", []))]
        return self._summarize(events, asset_id)

    @staticmethod
    def _summarize(events: list[dict[str, Any]], asset_id: str | None) -> dict[str, Any]:
        feedback = [
            rating
            for event in events
            for rating in event.get("human_feedback", [])
            if isinstance(rating, dict)
        ]
        outcomes = {name: 0 for name in sorted(OUTCOMES)}
        for event in events:
            name = event.get("outcome", "unknown")
            outcomes[name if name in outcomes else "unknown"] += 1
        return {
            "asset_id": asset_id,
            "uses": len(events),
            "rated_uses": sum(1 for event in events if event.get("human_feedback")),
            "outcomes": outcomes,
            "agent": {
                "effectiveness": _average(event.get("agent_assessment", {}).get("effectiveness") for event in events),
                "efficiency": _average(event.get("agent_assessment", {}).get("efficiency") for event in events),
            },
            "human": {
                "rating": _average(item.get("rating") for item in feedback),
                "effectiveness": _average(item.get("effectiveness") for item in feedback),
                "efficiency": _average(item.get("efficiency") for item in feedback),
                "ratings": len(feedback),
            },
            "recent": list(reversed(events[-10:])),
        }

    def summaries(self) -> dict[str, dict[str, Any]]:
        groups: dict[str, list[dict[str, Any]]] = {}
        for event in self._read()["events"]:
            for asset_id in {asset.get("id") for asset in event.get("assets", [])
                             if asset.get("id")}:
                groups.setdefault(asset_id, []).append(event)
        return {asset_id: self._summarize(events, asset_id)
                for asset_id, events in sorted(groups.items())}
