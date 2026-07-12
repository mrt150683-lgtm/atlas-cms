"""Scout — hunt plan documents across a directory tree and mass-review them.

``cms scout scan <dir>`` finds every ``*plan*.md`` (pruning node_modules,
VCS, venvs, build output), summarizes each NEW or CHANGED one with a real
provider into a compact **plan card** — one deep description sentence,
feature tags, goals, and whether it looks like an Atlas-mappable project —
cached by content hash in ``~/.cms/scout/plans.json`` so nothing is ever
re-charged unchanged.

``cms scout review`` then reads ALL cards at once (plus the constellation
registry of already-mapped projects) and generates: new idea concepts,
cross-plan patterns pointing at goals, project pairings, and
Atlas-onboarding candidates. Every suggestion is persisted with a status —
``proposed | accepted | rejected | ignored`` — and anything you reject or
ignore is fed back as a DO-NOT-REPROPOSE list, so dismissed ideas stay
dismissed.

Same honesty contract as fusion: real provider only; per-file failures are
recorded as failed cards and retried next scan; malformed mass-review
output raises instead of overwriting state; all LLM output is plan material.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
from pathlib import Path

from . import semantic_state as ss
from .providers import SummaryProvider
from .sources import _HARD_PRUNE

SCOUT_DIR = Path.home() / ".cms" / "scout"
PLAN_TEXT_LIMIT = 6000
CARD_MAX_TOKENS = 800
REVIEW_MAX_TOKENS = 4000
SUGGESTION_STATUSES = ("proposed", "accepted", "rejected", "ignored")
_EXTRA_PRUNE = {".memory", "dist", "out", "release", "build", "coverage", "htmlcov"}

CARD_PROMPT = """You are cataloguing one project-plan document for a cross-project idea index.
Return ONLY JSON:
{{"one_liner": "<ONE deep, information-dense sentence (<=170 chars) capturing what this plan is really about>",
 "tags": ["3-6 lowercase feature/domain tags"],
 "goals": ["up to 3 concrete goals the plan aims at"],
 "atlas_candidate": true/false (is this a real software project that could be code-mapped?),
 "reason": "<why / why not, one clause>"}}

PLAN DOCUMENT ({name}, truncated):
{text}
"""

REVIEW_PROMPT = """You are reviewing EVERY project plan one person has scattered across their machine, indexed below as one-line cards, alongside the codebases they already have mapped in Atlas.

Generate, as ONLY JSON:
{{"concepts": [{{"title": str, "description": str, "builds_on": [plan/project names]}}],
 "patterns": [{{"title": str, "description": "what recurring goal/theme these plans point at", "builds_on": [names]}}],
 "pairings": [{{"title": str, "description": "why these projects/plans belong together", "builds_on": [names]}}],
 "atlas_candidates": [{{"title": plan name, "description": "why Atlas should map this next", "builds_on": [plan name]}}]}}

Max {max_items} per list. Ground every item in the named cards — no inventions.
DO NOT RE-PROPOSE any of these previously dismissed ideas (or trivial variants of them):
{dismissed}

PLAN CARDS:
{cards}

ALREADY ATLAS-MAPPED PROJECTS:
{mapped}
"""


class ScoutError(RuntimeError):
    """Real-provider scout stage failed; a failure stays a failure."""


def _read_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default


def load_cards() -> dict:
    return _read_json(SCOUT_DIR / "plans.json", {})


def load_suggestions() -> dict:
    return _read_json(SCOUT_DIR / "suggestions.json", {})


def _save(name: str, payload) -> None:
    SCOUT_DIR.mkdir(parents=True, exist_ok=True)
    ss.atomic_write_json(SCOUT_DIR / name, payload)


# ── hunting ──────────────────────────────────────────────────────────────

def find_plans(base: Path) -> list[Path]:
    """Every *plan*.md under base, junk directories pruned."""
    base = Path(base).resolve()
    hits: list[Path] = []
    prune = {p.lower() for p in _HARD_PRUNE} | _EXTRA_PRUNE
    for dirpath, dirnames, filenames in os.walk(base):
        dirnames[:] = [d for d in dirnames
                       if d.lower() not in prune and not d.startswith(".git")]
        for name in filenames:
            low = name.lower()
            if low.endswith(".md") and "plan" in low:
                hits.append(Path(dirpath) / name)
    return sorted(hits)


# ── plan cards ───────────────────────────────────────────────────────────

def scan_plans(base: Path, provider: SummaryProvider, echo=print,
               max_new: int = 60) -> dict:
    """Summarize new/changed plan files into cards. Returns stats."""
    if provider.name == "mock":
        raise ScoutError("scout needs a real provider to summarize plans "
                         "(configure an API key)")
    cards = load_cards()
    found = find_plans(base)
    stats = {"found": len(found), "new": 0, "unchanged": 0, "failed": 0}
    for path in found:
        if stats["new"] >= max_new:
            echo(f"  scout: --max {max_new} reached; re-run to continue")
            break
        key = str(path)
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            echo(f"  scout: unreadable {path.name}: {exc}")
            continue
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()[:24]
        prior = cards.get(key)
        if prior and prior.get("content_hash") == digest and prior.get("one_liner"):
            stats["unchanged"] += 1
            continue
        echo(f"  scout: reading {path.name}  ({path.parent.name}/)")
        prompt = CARD_PROMPT.format(name=path.name, text=text[:PLAN_TEXT_LIMIT])
        try:
            raw = provider.summarize(prompt, {"max_tokens": CARD_MAX_TOKENS})
            match = re.search(r"\{[\s\S]*\}", raw)
            parsed = json.loads(match.group(0)) if match else None
        except Exception as exc:  # noqa: BLE001 — per-file: record, continue
            parsed = None
            echo(f"  scout: card failed for {path.name}: {exc}")
        if not parsed or not str(parsed.get("one_liner", "")).strip():
            cards[key] = {"name": path.name, "project_dir": path.parent.name,
                          "content_hash": digest, "status": "failed"}
            stats["failed"] += 1
            continue
        cards[key] = {
            "name": path.name,
            "project_dir": path.parent.name,
            "content_hash": digest,
            "one_liner": str(parsed["one_liner"])[:220],
            "tags": [str(t).lower()[:30] for t in (parsed.get("tags") or [])[:6]],
            "goals": [str(g)[:160] for g in (parsed.get("goals") or [])[:3]],
            "atlas_candidate": bool(parsed.get("atlas_candidate")),
            "reason": str(parsed.get("reason", ""))[:160],
            "provider": provider.name, "model": getattr(provider, "model", None),
            "summarized_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "status": "ok",
        }
        stats["new"] += 1
    _save("plans.json", cards)
    return stats


# ── mass review ──────────────────────────────────────────────────────────

def _suggestion_id(kind: str, title: str) -> str:
    norm = re.sub(r"[^a-z0-9]+", " ", title.lower()).strip()
    return hashlib.sha1(f"{kind}:{norm}".encode()).hexdigest()[:12]


def mass_review(provider: SummaryProvider, max_items: int = 6) -> dict:
    """One call over every card: concepts, patterns, pairings, Atlas
    candidates. Dismissed suggestions are excluded and never re-proposed."""
    if provider.name == "mock":
        raise ScoutError("scout review needs a real provider")
    cards = {k: c for k, c in load_cards().items() if c.get("status") == "ok"}
    if not cards:
        raise ScoutError("no plan cards yet — run `cms scout scan <dir>` first")
    suggestions = load_suggestions()
    dismissed = [s["title"] for s in suggestions.values()
                 if s.get("status") in ("rejected", "ignored")]

    from .fuse import load_registry

    mapped = [meta.get("name") for meta in
              (load_registry().get("projects") or {}).values()]
    card_lines = [
        {"name": c["name"], "project": c["project_dir"], "one_liner": c["one_liner"],
         "tags": c.get("tags", []), "goals": c.get("goals", []),
         "atlas_candidate": c.get("atlas_candidate")}
        for c in cards.values()
    ]
    prompt = REVIEW_PROMPT.format(
        max_items=max_items,
        dismissed=json.dumps(sorted(dismissed)) if dismissed else "(none yet)",
        cards=json.dumps(card_lines, indent=1),
        mapped=json.dumps(sorted(filter(None, mapped))),
    )
    try:
        raw = provider.summarize(prompt, {"max_tokens": REVIEW_MAX_TOKENS})
    except Exception as exc:
        raise ScoutError(f"provider call failed: {type(exc).__name__}: {exc}") from exc
    match = re.search(r"\{[\s\S]*\}", raw)
    if match is None:
        raise ScoutError("provider returned no JSON object (malformed review)")
    try:
        parsed = json.loads(match.group(0))
    except json.JSONDecodeError as exc:
        raise ScoutError(f"provider returned invalid JSON: {exc}") from exc

    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    fresh: list[str] = []
    for kind in ("concepts", "patterns", "pairings", "atlas_candidates"):
        for item in (parsed.get(kind) or [])[:max_items]:
            if not isinstance(item, dict) or not str(item.get("title", "")).strip():
                continue
            sid = _suggestion_id(kind, item["title"])
            prior = suggestions.get(sid)
            if prior:  # keep the human's verdict, just note it resurfaced
                prior["last_seen"] = now
                continue
            suggestions[sid] = {
                "id": sid, "kind": kind, "title": str(item["title"])[:140],
                "description": str(item.get("description", ""))[:500],
                "builds_on": [str(b)[:80] for b in (item.get("builds_on") or [])[:6]],
                "status": "proposed", "provenance": "llm",
                "provider": provider.name, "model": getattr(provider, "model", None),
                "first_seen": now, "last_seen": now,
            }
            fresh.append(sid)
    _save("suggestions.json", suggestions)
    return {"cards_reviewed": len(cards), "new_suggestions": len(fresh),
            "dismissed_excluded": len(dismissed),
            "suggestions": [suggestions[s] for s in fresh]}


def set_suggestion_status(sid: str, status: str) -> dict:
    if status not in SUGGESTION_STATUSES:
        raise ScoutError(f"status must be one of {SUGGESTION_STATUSES}")
    suggestions = load_suggestions()
    if sid not in suggestions:
        raise ScoutError(f"unknown suggestion id {sid!r} (see `cms scout list`)")
    suggestions[sid]["status"] = status
    suggestions[sid]["decided_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    _save("suggestions.json", suggestions)
    return suggestions[sid]
