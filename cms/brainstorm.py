"""Brainstorm — temperature-adjusted idea generation that learns your taste.

Generates batches of NEW-concept one-liners that deliberately steer AWAY
from everything you're already working on (scout cards, fusion items,
mapped projects, and every previously generated idea). Optionally grounds
a batch in ONE chosen project's high-level card instead ("related to X").

The learning loop: every idea can be liked or disliked; the next batch
receives liked ideas as "more in these directions" and disliked ones as
"never anything like these" — the generator optimises toward your taste
batch over batch. Custom goals (revealed in the UI by clicking the logo
seven times) are standing directives injected into every generation, e.g.
"ideas that could help cure diseases" or "focus on £X/week products".

State: ``~/.cms/brainstorm/ideas.json`` + ``goals.json``. Honesty: real
provider only; malformed output raises (state untouched); every idea is
LLM plan material, provenance-stamped with provider/model/temperature.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from pathlib import Path

from . import semantic_state as ss
from .providers import SummaryProvider

BRAINSTORM_DIR = Path.home() / ".cms" / "brainstorm"
IDEAS_PER_BATCH = 10
GEN_MAX_TOKENS = 1500
DEFAULT_TEMPERATURE = 1.0

GEN_PROMPT = """You are a prolific concept generator producing GENUINELY NEW ideas for one builder.

{mode_block}
{goals_block}
TASTE SIGNAL (optimise toward this):
- The builder LIKED these earlier ideas — generate more in these directions (not duplicates):
{liked}
- The builder DISLIKED these — never produce anything resembling them:
{disliked}

DO NOT produce ideas resembling the builder's existing work or past batches:
{avoid}

Return ONLY a JSON array of exactly {count} strings. Each string is ONE
self-contained idea sentence (<= 160 chars), concrete enough to act on —
no numbering, no preamble, no marketing fluff. Be surprising; obvious
ideas are failures.
"""

MODE_RANDOM = """MODE: unconstrained. Ideas must be NEW CONCEPTS — unrelated to the
builder's current projects and past ideas listed below. Range wide: any
domain, any medium, any scale."""

MODE_PROJECT = """MODE: grounded in the project "{name}".
Project purpose: {purpose}
Its capabilities: {features}
Ideas must EXTEND or SPRING FROM this project — but must still be new
directions, not restatements of its existing capabilities."""


class BrainstormError(RuntimeError):
    """Real-provider generation failed; state is left untouched."""


def _read(name: str, default):
    try:
        return json.loads((BRAINSTORM_DIR / name).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default


def _write(name: str, payload) -> None:
    BRAINSTORM_DIR.mkdir(parents=True, exist_ok=True)
    ss.atomic_write_json(BRAINSTORM_DIR / name, payload)


def load_ideas() -> dict:
    return _read("ideas.json", {})


def load_goals() -> list[dict]:
    return _read("goals.json", [])


# ── goals (the seven-click panel) ────────────────────────────────────────

def add_goal(text: str) -> list[dict]:
    text = (text or "").strip()
    if not text:
        raise BrainstormError("goal text is empty")
    goals = load_goals()
    gid = hashlib.sha1(text.lower().encode()).hexdigest()[:10]
    if not any(g["id"] == gid for g in goals):
        goals.append({"id": gid, "text": text[:300],
                      "added_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())})
        _write("goals.json", goals)
    return goals


def remove_goal(gid: str) -> list[dict]:
    goals = [g for g in load_goals() if g["id"] != gid]
    _write("goals.json", goals)
    return goals


# ── the avoid-list: everything already in the builder's world ────────────

def _existing_work_lines(limit: int = 60) -> list[str]:
    lines: list[str] = []
    try:
        from .fuse import load_fusion, load_registry

        for meta in (load_registry().get("projects") or {}).values():
            if meta.get("name"):
                lines.append(f"project: {meta['name']}")
        report = load_fusion() or {}
        for key in ("integrations", "emergent", "conflicts"):
            lines += [f"fusion: {i.get('title')}" for i in report.get(key) or [] if i.get("title")]
    except Exception:
        pass
    try:
        from .scout import load_cards, load_suggestions

        lines += [f"plan: {c['one_liner']}" for c in load_cards().values()
                  if c.get("status") == "ok"][:30]
        lines += [f"idea: {s['title']}" for s in load_suggestions().values()]
    except Exception:
        pass
    return lines[:limit]


# ── generation ───────────────────────────────────────────────────────────

def generate_ideas(provider: SummaryProvider, temperature: float = DEFAULT_TEMPERATURE,
                   project_root: str | None = None,
                   count: int = IDEAS_PER_BATCH) -> list[dict]:
    """One batch of new-concept one-liners. Returns the new ideas (also
    persisted). Raises BrainstormError on mock/failure/malformed output."""
    if provider.name == "mock":
        raise BrainstormError("brainstorming needs a real provider (configure an API key)")
    temperature = min(1.0, max(0.0, float(temperature)))

    ideas = load_ideas()
    liked = [i["text"] for i in ideas.values() if i["status"] == "liked"]
    disliked = [i["text"] for i in ideas.values() if i["status"] == "disliked"]
    past = [i["text"] for i in ideas.values()]

    if project_root:
        from .fuse import build_card

        card = build_card(Path(project_root))
        if not card.get("ready"):
            raise BrainstormError(
                f"project {card.get('name')!r} isn't fused-ready: {card.get('reason')}")
        mode_block = MODE_PROJECT.format(
            name=card["name"], purpose=card.get("purpose") or "(no review yet)",
            features=", ".join(f["name"] for f in card["features"]) or "(none)")
        avoid = past  # grounded mode: only avoid repeating past ideas
    else:
        mode_block = MODE_RANDOM
        avoid = _existing_work_lines() + past

    goals = load_goals()
    goals_block = ("STANDING GOALS (every idea should serve at least one):\n"
                   + "\n".join(f"- {g['text']}" for g in goals) + "\n") if goals else ""

    def bullet(items, none_text):
        return "\n".join(f"  - {t}" for t in items[-25:]) or f"  ({none_text})"

    prompt = GEN_PROMPT.format(
        mode_block=mode_block, goals_block=goals_block,
        liked=bullet(liked, "none yet"), disliked=bullet(disliked, "none yet"),
        avoid=bullet(avoid, "nothing yet — free rein"), count=count,
    )
    try:
        raw = provider.summarize(prompt, {"max_tokens": GEN_MAX_TOKENS,
                                          "temperature": temperature})
    except Exception as exc:
        raise BrainstormError(f"provider call failed: {type(exc).__name__}: {exc}") from exc
    match = re.search(r"\[[\s\S]*\]", raw)
    if match is None:
        raise BrainstormError("provider returned no JSON array")
    try:
        texts = json.loads(match.group(0))
    except json.JSONDecodeError as exc:
        raise BrainstormError(f"provider returned invalid JSON: {exc}") from exc
    texts = [str(t).strip() for t in texts if str(t).strip()][:count]
    if not texts:
        raise BrainstormError("provider returned an empty idea list")

    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    batch = hashlib.sha1(f"{now}{temperature}{project_root}".encode()).hexdigest()[:8]
    new: list[dict] = []
    for text in texts:
        iid = hashlib.sha1(text.lower().encode()).hexdigest()[:12]
        if iid in ideas:
            continue  # provider repeated itself; keep the original + its verdict
        ideas[iid] = {
            "id": iid, "text": text[:220], "status": "new",
            "batch": batch, "temperature": temperature,
            "project": Path(project_root).name if project_root else None,
            "provider": provider.name, "model": getattr(provider, "model", None),
            "created_at": now, "provenance": "llm",
        }
        new.append(ideas[iid])
    _write("ideas.json", ideas)
    return new


def rate_idea(iid: str, verdict: str) -> dict:
    if verdict not in ("liked", "disliked", "new"):
        raise BrainstormError("verdict must be liked | disliked | new")
    ideas = load_ideas()
    if iid not in ideas:
        raise BrainstormError(f"unknown idea id {iid!r}")
    ideas[iid]["status"] = verdict
    ideas[iid]["rated_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    _write("ideas.json", ideas)
    return ideas[iid]
