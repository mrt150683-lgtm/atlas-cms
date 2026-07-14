"""Comprehension lens — re-express the memory layer's narrative text for
any audience.

The UI has a lens slider: default (raw data) plus six audience levels —
schoolchild, technician, uni student, domain specialist, TL;DR-only, and
ADHD/low-focus. Any narrative text (summaries, feature descriptions, review
verdicts, suggestions, chat answers) can be rewritten ONCE per (text, level)
via the configured LLM provider and cached in ``.memory/lens/`` keyed by a
hash of the source text, so moving the slider is cheap after the first look
and costs nothing for text you never view.

Rewrites are presentation only: they must keep every factual claim and all
code identifiers, and they are never written back into the graph — the
stored data stays the single source of truth. Without a real provider the
format levels (tldr / adhd) fall back to deterministic text transforms and
the persona levels return the original text with ``real: false`` so the UI
can say why nothing changed.
"""

from __future__ import annotations

import hashlib
import json
import re
import threading
from pathlib import Path

from . import config
from .providers import SummaryProvider

LENS_DIR = "lens"
MAX_ITEMS = 16           # per request — the UI batches with a debounce
MAX_TEXT_CHARS = 3000    # rewrite source truncation (inputs are short notes)
CHUNK_SIZE = 6           # texts per LLM call — keeps JSON output well under max_tokens
CHUNK_MAX_TOKENS = 2600

# Ordered as they appear on the slider, left to right; "default" (raw data,
# no rewrite) is the seventh, rightmost notch and lives only in the UI.
LEVELS: dict[str, dict] = {
    "schoolchild": {
        "label": "Schoolchild",
        "blurb": "Explained to a curious 10-year-old — everyday words and analogies.",
        "audience": "a curious 10-year-old who has never programmed",
        "rules": (
            "Use everyday words and one friendly analogy where it helps. "
            "Short sentences. No jargon — if a technical word is unavoidable, "
            "explain it in brackets right away. Keep it warm, not childish."
        ),
        "length": "Keep it about as long as the original, never more than 1.5x.",
    },
    "tech": {
        "label": "Technician",
        "blurb": "Practical hands-on terms — what it does and how you'd work with it.",
        "audience": "a hands-on IT technician / junior developer",
        "rules": (
            "Plain, practical language. Focus on what the thing does, what it "
            "touches, and what you would do with it. Expand acronyms once. "
            "No academic theory."
        ),
        "length": "Keep it about as long as the original.",
    },
    "uni": {
        "label": "Uni student",
        "blurb": "Precise terminology, tied back to CS fundamentals.",
        "audience": "a university computer-science student",
        "rules": (
            "Use correct terminology and briefly connect the design to CS "
            "concepts they know (data structures, patterns, complexity, "
            "architecture) where genuinely relevant. Precise but approachable."
        ),
        "length": "Keep it about as long as the original.",
    },
    "specialist": {
        "label": "Specialist",
        "blurb": "Dense and precise for a domain expert — decisions and tradeoffs.",
        "audience": "a senior engineer who is a specialist in this domain",
        "rules": (
            "Assume expert vocabulary. Be dense and precise; surface the design "
            "decisions, invariants and tradeoffs implied by the text. Zero filler."
        ),
        "length": "Same length or shorter than the original.",
    },
    "tldr": {
        "label": "TL;DR",
        "blurb": "One punchy sentence — the single most important point.",
        "audience": "someone who only ever reads the TL;DR",
        "rules": (
            "Exactly ONE sentence, maximum 25 words, capturing the single most "
            "important point. No preamble, no 'this file'."
        ),
        "length": "One sentence.",
    },
    "adhd": {
        "label": "ADHD / low focus",
        "blurb": "2–4 ultra-short bullets, most important first, key word bolded.",
        "audience": "a reader with ADHD and low focus right now",
        "rules": (
            "2 to 4 bullet lines, each starting with '- ', maximum 8 words per "
            "bullet, most important point FIRST. Bold the one key word of each "
            "bullet with **asterisks**. Nothing outside the bullets."
        ),
        "length": "2-4 bullets of up to 8 words.",
    },
}

_PERSONA_LEVELS = ("schoolchild", "tech", "uni", "specialist")

_BATCH_PROMPT = """You are the comprehension lens of a codebase-mapping tool. Rewrite each numbered technical note below for one specific audience.

AUDIENCE: {audience}
STYLE: {rules}
LENGTH: {length}

Hard rules:
- Keep every factual claim. NEVER invent facts, files, functions or behavior not in the source text.
- Keep file paths and code identifiers verbatim, wrapped in `backticks`.
- Each rewrite must stand alone (no "as above", no numbering inside the text).

Return ONLY a JSON array of {count} strings — the rewrites in the same order as the inputs. No commentary, no markdown fence.

TEXTS:
{texts}
"""

_cache_lock = threading.Lock()


class LensError(RuntimeError):
    """Bad lens request (unknown level, malformed items)."""


def lens_key(text: str) -> str:
    """Cache key: hash of the normalized source text (level is the filename)."""
    norm = re.sub(r"\s+", " ", str(text)).strip()
    return hashlib.sha1(norm.encode("utf-8")).hexdigest()


def _cache_path(root: Path, level: str) -> Path:
    return Path(root) / config.MEMORY_DIR_NAME / LENS_DIR / f"{level}.json"


def load_cache(root: Path, level: str) -> dict:
    try:
        return json.loads(_cache_path(root, level).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _save_cache(root: Path, level: str, cache: dict) -> None:
    path = _cache_path(root, level)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(cache, ensure_ascii=False), encoding="utf-8")
        tmp.replace(path)
    except OSError:
        pass  # the lens cache is a convenience, never load-bearing


_SENTENCE_RE = re.compile(r"(?<=[.!?])\s+")


def _plainish(text: str) -> str:
    """Strip markdown noise so fallback output reads clean."""
    return re.sub(r"[*`#]", "", re.sub(r"\s+", " ", str(text))).strip()


def fallback_rewrite(text: str, level: str) -> str | None:
    """Deterministic no-LLM transform. Only the FORMAT levels have one —
    tldr/adhd are genuinely useful as pure text surgery; the persona levels
    need a real model and return None (caller keeps the original)."""
    plain = _plainish(text)
    if level == "tldr":
        first = _SENTENCE_RE.split(plain)[0].strip()
        return first[:137] + "…" if len(first) > 140 else first
    if level == "adhd":
        bullets = []
        for sentence in _SENTENCE_RE.split(plain):
            sentence = sentence.strip().rstrip(".")
            if not sentence:
                continue
            bullets.append("- " + (sentence[:57] + "…" if len(sentence) > 60 else sentence))
            if len(bullets) == 4:
                break
        return "\n".join(bullets) if bullets else None
    return None


def _parse_batch_reply(reply: str, expected: int) -> list[str] | None:
    """Pull the JSON array out of a model reply; None if it doesn't line up."""
    start, end = reply.find("["), reply.rfind("]")
    if start < 0 or end <= start:
        return None
    try:
        arr = json.loads(reply[start:end + 1])
    except json.JSONDecodeError:
        return None
    if not isinstance(arr, list) or len(arr) != expected:
        return None
    return [str(x).strip() for x in arr]


def _generate(texts: list[str], level: str, provider: SummaryProvider) -> list[str | None]:
    """LLM-rewrite texts at level, CHUNK_SIZE per call. A failed or misaligned
    chunk yields Nones for its texts — the caller keeps originals uncached so
    a later request can retry."""
    spec = LEVELS[level]
    out: list[str | None] = []
    for i in range(0, len(texts), CHUNK_SIZE):
        chunk = texts[i:i + CHUNK_SIZE]
        numbered = "\n\n".join(f"{n + 1}. {t}" for n, t in enumerate(chunk))
        prompt = _BATCH_PROMPT.format(
            audience=spec["audience"], rules=spec["rules"], length=spec["length"],
            count=len(chunk), texts=numbered,
        )
        try:
            reply = provider.summarize(prompt, {"max_tokens": CHUNK_MAX_TOKENS})
            out.extend(_parse_batch_reply(reply, len(chunk)) or [None] * len(chunk))
        except Exception:
            out.extend([None] * len(chunk))
    return out


# @memory:feature:ComprehensionLens
# @memory:connects:MemoryViewer, CodebaseChat
# @memory:summary:Rewrites a batch of UI narrative texts for one audience level — cache-first per (text-hash, level) under .memory/lens/, real-provider LLM rewrite for misses, deterministic tldr/adhd fallback under mock.
def rewrite_batch(root: Path, level: str, items: list[dict],
                  provider: SummaryProvider) -> dict:
    """Rewrite items ``[{id, text}]`` at ``level``. Returns
    ``{level, real, results: {id: text}, cached, generated}``; results fall
    back to the original text whenever a rewrite isn't available."""
    if level not in LEVELS:
        raise LensError(f"unknown lens level {level!r}; expected one of {', '.join(LEVELS)}")
    if not isinstance(items, list) or len(items) > MAX_ITEMS:
        raise LensError(f"items must be a list of at most {MAX_ITEMS}")
    clean: list[tuple[str, str]] = []
    for it in items:
        if not isinstance(it, dict) or not str(it.get("text") or "").strip():
            raise LensError("every item needs an 'id' and a non-empty 'text'")
        clean.append((str(it.get("id") or lens_key(it["text"])),
                      str(it["text"])[:MAX_TEXT_CHARS]))

    root = Path(root).resolve()
    real = provider.name != "mock"
    results: dict[str, str] = {}
    with _cache_lock:
        cache = load_cache(root, level)
        misses: list[tuple[str, str, str]] = []  # (item_id, key, text)
        for item_id, text in clean:
            key = lens_key(text)
            hit = cache.get(key)
            if hit:
                results[item_id] = hit
            else:
                misses.append((item_id, key, text))

        generated = 0
        if misses and real:
            rewrites = _generate([t for _, _, t in misses], level, provider)
            for (item_id, key, text), rewritten in zip(misses, rewrites):
                if rewritten:
                    cache[key] = rewritten
                    results[item_id] = rewritten
                    generated += 1
                else:
                    results[item_id] = text  # uncached — retried next request
            if generated:
                _save_cache(root, level, cache)
        elif misses:  # mock / no key: deterministic format levels, else original
            for item_id, key, text in misses:
                results[item_id] = fallback_rewrite(text, level) or text

    return {
        "level": level, "real": real, "results": results,
        "cached": len(clean) - len(misses), "generated": generated if real else 0,
    }
