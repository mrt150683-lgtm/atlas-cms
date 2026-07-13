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
import re
import shlex
import time
from pathlib import Path

from . import config
from . import semantic_state as ss
from .memory import CodebaseMemory
from .providers import SummaryProvider

CHAT_MAX_TOKENS = 1600
HISTORY_TURNS = 6
TRANSCRIPT = "chat.jsonl"
_CMS_COMMAND_RE = re.compile(
    r"(?m)(?:`(?P<inline>cms\s+[^`\r\n]+)`|^[ \t]*(?:[$>]\s*)?(?P<line>cms\s+[^\r\n]+)$)"
)

CHAT_PROMPT = """You are Atlas, the memory layer of the codebase "{project}", talking to its OWNER.
Answer their question in SIMPLE, plain language — short sentences, no jargon walls, explain like a sharp colleague, not a compiler. Max ~300 words.

Hard rules:
- Ground EVERY claim in the evidence below; cite features by name and code as path:lines. Never invent files, functions or behavior.
- If the question is about whether something does what it's SUPPOSED to do: compare its DECLARED intent (description / review "expected") against what is BUILT (members, flows, review "built", gaps). State clearly where they match and where they don't.
- If the declared intent is missing or too thin to judge, say what the thing ACTUALLY does and ASK the owner what they expect it to do — do not guess their intent.
- If the evidence is insufficient, say exactly what to run (e.g. `cms review`, `cms update`) instead of padding.
- Only recommend commands present in the LIVE CLI CONTRACT below. Do not invent flags or positional arguments.
- Honest verdicts only: reviews/summaries below are AI-generated evidence, not gospel — say so when leaning on them.

LIVE CLI CONTRACT:
{cli_contract}

{history_block}EVIDENCE PACK:
{evidence}

OWNER'S QUESTION: {question}
"""


class ChatError(RuntimeError):
    """Real-provider chat failed; never replaced by confident guesswork."""


def _cli_surface():
    """Return Click's live command tree without invoking a command callback."""
    import typer

    from .cli import app

    return typer.main.get_command(app)


def cli_contract() -> str:
    """Compact, live syntax guide injected into Ask Atlas prompts."""
    root = _cli_surface()
    lines = []
    for name, command in sorted(root.commands.items()):
        try:
            ctx = command.make_context(name, [], resilient_parsing=True)
            usage = command.get_usage(ctx).replace("Usage: ", "").strip()
        except Exception:
            usage = f"{name} --help"
        lines.append(f"cms {usage}")
    return "\n".join(lines)


def _command_error(command_text: str) -> str | None:
    """Validate one generated ``cms ...`` command against the live Click tree."""
    try:
        tokens = shlex.split(command_text, posix=True)
    except ValueError as exc:
        return str(exc)
    if len(tokens) < 2 or tokens[0].lower() != "cms":
        return "not a cms command"
    current = _cli_surface()
    args = tokens[1:]
    try:
        while hasattr(current, "commands"):
            if not args:
                current.make_context(current.name or "cms", [], resilient_parsing=False)
                return None
            if args[0].startswith("-"):
                current.make_context(current.name or "cms", args, resilient_parsing=False)
                return None
            name = args.pop(0)
            child = current.commands.get(name)
            if child is None:
                return f"unknown command '{name}'"
            current = child
        current.make_context(current.name or tokens[1], args, resilient_parsing=False)
    except SystemExit as exc:
        return None if exc.code == 0 else f"command parser exited {exc.code}"
    except Exception as exc:
        if getattr(exc, "exit_code", None) == 0:
            return None
        return str(exc)
    return None


def validate_answer_commands(answer: str) -> tuple[str, list[dict]]:
    """Block invalid generated commands before they reach UI, MCP, or transcript."""
    invalid = []

    def replace(match: re.Match) -> str:
        command = (match.group("inline") or match.group("line")).strip()
        error = _command_error(command)
        if error is None:
            return match.group(0)
        invalid.append({"command": command, "error": error})
        parts = shlex.split(command, posix=True)
        help_command = f"cms {parts[1]} --help" if len(parts) > 1 and not error.startswith("unknown command") else "cms --help"
        return (f"Atlas blocked an invalid generated command (`{command}`: {error}). "
                f"Use `{help_command}` for the live syntax.")

    return _CMS_COMMAND_RE.sub(replace, answer), invalid


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
        history: list[dict] | None = None, session: str | None = None) -> dict:
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
        cli_contract=cli_contract(), evidence=json.dumps(evidence, indent=1)[:14000],
        question=question[:600],
    )
    try:
        answer = provider.summarize(prompt, {"max_tokens": CHAT_MAX_TOKENS})
    except Exception as exc:
        raise ChatError(f"provider call failed: {type(exc).__name__}: {exc}") from exc
    if not answer.strip():
        raise ChatError("provider returned an empty answer")
    answer, invalid_commands = validate_answer_commands(answer.strip())

    entry = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "session": session or "default",
        "q": question, "a": answer,
        "provider": provider.name, "model": getattr(provider, "model", None),
        "evidence_nodes": nodes[:20],
        "matched_features": [m["feature"] for m in evidence["matched_features"]],
        "command_validation": {"checked": True, "blocked": invalid_commands},
    }
    try:
        path = root / config.MEMORY_DIR_NAME / TRANSCRIPT
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError:
        pass  # the transcript is convenience, never load-bearing
    return entry


def session_history(root: Path, session: str, limit: int = 6) -> list[dict]:
    """Continuity comes from the SAME session only — a fresh chat starts clean."""
    return [t for t in load_transcript(root, limit=400)
            if t.get("session") == session][-limit:]


def list_sessions(root: Path) -> list[dict]:
    """History index, newest first: id, name (first question), when, turns."""
    groups: dict[str, dict] = {}
    for seq, t in enumerate(load_transcript(root, limit=400)):
        sid = t.get("session") or "default"
        g = groups.setdefault(sid, {"id": sid, "turns": 0,
                                    "name": _trim(t.get("q"), 70),
                                    "started": t.get("ts")})
        g["turns"] += 1
        g["last"] = t.get("ts")
        g["_seq"] = seq  # tiebreak: ts has 1s resolution
    out = sorted(groups.values(),
                 key=lambda g: (g.get("last") or "", g["_seq"]), reverse=True)
    for g in out:
        g.pop("_seq", None)
    return out


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
