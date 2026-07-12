"""Codebase chat — plain-language Q&A grounded in the whole memory layer.

One question in ("Is the Constellation feature fully aligned with the core
idea behind it?"), one evidence-grounded answer out. The evidence pack is
assembled from every layer Atlas already maintains — ranked query hits,
feature traces, reviews, Sentinel gate, semantic-state pipeline — so the
model reasons over the map, not over guesses.

The intent-vs-reality contract: when a question asks whether something does
what it's *supposed* to do, the answer must compare the DECLARED side
(feature description, anchors, review "expected") against the BUILT side
(members, flows, review "built", gaps, tests). When the declared side is
missing, the model must say what IS built and ask the user what it should
be doing — never invent the intent.

Surfaces: `ask_codebase` MCP tool (agents), POST /api/chat (the UI popup),
`cms ask` (CLI). Transcript appends to ``.memory/chat.jsonl``. Real provider
only; failures raise, they are never converted into confident prose.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from . import config
from . import semantic_state as ss
from .memory import CodebaseMemory
from .providers import SummaryProvider

CHAT_MAX_TOKENS = 1600
HISTORY_TURNS = 6
TRANSCRIPT = "chat.jsonl"

CHAT_PROMPT = """You are Atlas, the memory layer of the codebase "{project}", talking to its OWNER.
Answer their question in SIMPLE, plain language — short sentences, no jargon walls, explain like a sharp colleague, not a compiler. Max ~300 words.

Hard rules:
- Ground EVERY claim in the evidence below; cite features by name and code as path:lines. Never invent files, functions or behavior.
- If the question is about whether something does what it's SUPPOSED to do: compare its DECLARED intent (description / review "expected") against what is BUILT (members, flows, review "built", gaps). State clearly where they match and where they don't.
- If the declared intent is missing or too thin to judge, say what the thing ACTUALLY does and ASK the owner what they expect it to do — do not guess their intent.
- If the evidence is insufficient, say exactly what to run (e.g. `cms review`, `cms update`) instead of padding.
- Honest verdicts only: reviews/summaries below are AI-generated evidence, not gospel — say so when leaning on them.

{history_block}EVIDENCE PACK:
{evidence}

OWNER'S QUESTION: {question}
"""


class ChatError(RuntimeError):
    """Real-provider chat failed; never replaced by confident guesswork."""


def _trim(text, n=400):
    text = str(text or "").strip()
    return text[: n - 1] + "…" if len(text) > n else text


# @memory:feature:CodebaseChat
# @memory:connects:QueryEngine, FeatureTracing, FeatureExpectationReview, HermesSentinel
# @memory:summary:Assembles the cross-layer evidence pack one question needs — ranked hits, matched feature traces+reviews, app review, Sentinel gate, pipeline state — for grounded plain-language answers.
def build_evidence(root: Path, question: str) -> tuple[dict, list[str]]:
    """Evidence pack + touched node ids (for UI activity pulses)."""
    from .features import get_features

    memory_dir = root / config.MEMORY_DIR_NAME
    memory = CodebaseMemory.load(memory_dir / "graph.json")
    graph = memory.graph
    nodes: list[str] = []
    q_lower = question.lower()

    hits = []
    for h in memory.query_intent(question, top_k=6):
        hits.append({"node": h.node_id, "path": h.path, "lines": h.lines,
                     "summary": _trim(h.summary, 260)})
        nodes.append(h.node_id)

    feats = get_features(graph)
    matched = []
    for f in feats:
        name = f["name"]
        tokens = [t for t in
                  __import__("re").findall(r"[A-Z][a-z]+|[a-z]{4,}", name) if t]
        if name.lower() in q_lower or (
                tokens and all(t.lower() in q_lower for t in tokens)):
            review = f.get("review") or {}
            matched.append({
                "feature": name, "source": f.get("source"),
                "declared_intent": _trim(f.get("description"), 300) or "(none declared)",
                "members": (f.get("members") or [])[:10],
                "entry_points": (f.get("entry_points") or [])[:6],
                "narrative": _trim(f.get("summary"), 600),
                "review_verdict": review.get("verdict"),
                "review_expected": _trim(review.get("expected"), 300),
                "review_built": _trim(review.get("built"), 300),
                "review_gaps": (review.get("gaps") or [])[:5],
                "tests_exercising": len(f.get("exercised_by") or []),
            })
            nodes.append(f"feature:{name}")
    evidence = {
        "project": root.name,
        "features_total": len(feats),
        "matched_features": matched,
        "ranked_hits": hits,
    }

    if graph.has_node("review:app"):
        app = graph.nodes["review:app"]
        evidence["app_review"] = {"verdict": app.get("verdict"),
                                  "headline": _trim(app.get("headline"), 250)}
    state = ss.load_state(memory_dir)
    evidence["pipeline"] = ss.pipeline_status(state)
    try:
        from .sentinel.store import SentinelStore

        scan = SentinelStore(memory_dir).latest_scan()
        if scan:
            evidence["sentinel_gate"] = {
                "failed": (scan.get("gate") or {}).get("failed"),
                "active_counts": (scan.get("gate") or {}).get("active_counts"),
            }
    except Exception:
        pass
    return evidence, [n for n in dict.fromkeys(nodes)]


def _history_block(history) -> str:
    if not history:
        return ""
    lines = ["RECENT CONVERSATION (for continuity):"]
    for turn in history[-HISTORY_TURNS:]:
        lines.append(f"  Owner: {_trim(turn.get('q'), 200)}")
        lines.append(f"  Atlas: {_trim(turn.get('a'), 300)}")
    return "\n".join(lines) + "\n\n"


def ask(root: Path, question: str, provider: SummaryProvider,
        history: list[dict] | None = None) -> dict:
    """Answer one question; returns {answer, evidence_nodes, …} and appends
    to the project transcript. Raises ChatError on mock/failure."""
    question = (question or "").strip()
    if not question:
        raise ChatError("ask something — e.g. 'is Constellation aligned with its core idea?'")
    if provider.name == "mock":
        raise ChatError("codebase chat needs a real provider (configure an API key)")
    root = Path(root).resolve()
    if not (root / config.MEMORY_DIR_NAME / "graph.json").is_file():
        raise ChatError(f"no memory layer at {root} — run `cms run-all` first")

    evidence, nodes = build_evidence(root, question)
    prompt = CHAT_PROMPT.format(
        project=root.name, history_block=_history_block(history),
        evidence=json.dumps(evidence, indent=1)[:14000], question=question[:600],
    )
    try:
        answer = provider.summarize(prompt, {"max_tokens": CHAT_MAX_TOKENS})
    except Exception as exc:
        raise ChatError(f"provider call failed: {type(exc).__name__}: {exc}") from exc
    if not answer.strip():
        raise ChatError("provider returned an empty answer")

    entry = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "q": question, "a": answer.strip(),
        "provider": provider.name, "model": getattr(provider, "model", None),
        "evidence_nodes": nodes[:20],
        "matched_features": [m["feature"] for m in evidence["matched_features"]],
    }
    try:
        path = root / config.MEMORY_DIR_NAME / TRANSCRIPT
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError:
        pass  # the transcript is convenience, never load-bearing
    return entry


def load_transcript(root: Path, limit: int = 20) -> list[dict]:
    path = Path(root) / config.MEMORY_DIR_NAME / TRANSCRIPT
    out: list[dict] = []
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    except OSError:
        pass
    return out[-limit:]
