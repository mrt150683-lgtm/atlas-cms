"""Human explanation cache — per-node explanations for the Human View.

Where the lens (``lens.py``) rewrites arbitrary on-screen text keyed by the
text itself, this module explains *canonical nodes* (system / component /
feature / file / func / class) and keys the cache by node identity + a
dependency-aware content hash, so an explanation is regenerated exactly when
the things it describes change:

- file: mtime + summary
- func/class: signature + line range + summary (+ file mtime)
- feature: members + narrative + review verdict
- component/system: description + members + each child's hash (so a change
  deep in the pyramid cascades upward, and *only* upward)

Entries live in ``.memory/explain.json``. A stale entry simply never
matches its key again; ``prune_explanations`` sweeps orphans after updates.
Mock/no-key runs return the node's existing stored text labelled as
structural (``real: false``) and never write cache entries.
"""

from __future__ import annotations

import hashlib
import json
import threading
from pathlib import Path

from . import config
from .providers import SummaryProvider

EXPLAIN_FILE = "explain.json"
PROMPT_VERSION = 1
MAX_ITEMS = 12
CHUNK_SIZE = 4
CHUNK_MAX_TOKENS = 3000

_lock = threading.Lock()

_KIND_DEPTH = {
    "system": "the whole-system level: responsibilities and how the parts serve the user; no file names",
    "component": "the component level: its job, what flows in and out, which features it owns",
    "feature": "the feature level: what a user or agent gets from it and how it works end to end, briefly",
    "file": "the module level: the file's purpose and its most important behaviour",
    "func": "the implementation level: what this callable does, its inputs/outputs and side effects",
    "class": "the implementation level: what this class models and how it is used",
}

_BATCH_PROMPT = """You are the Human View of a codebase-mapping tool, explaining parts of the "{project}" codebase to its owner in plain language.

For each numbered item, write a short explanation (2-4 sentences) pitched at {depth}.

Hard rules:
- Ground every statement in the FACTS given for the item. NEVER invent behaviour, files or functions.
- Keep code identifiers and paths verbatim in `backticks`.
- Plain, direct sentences. No filler like "This component is responsible for".

Return ONLY a JSON array of {count} strings in input order. No commentary, no markdown fence.

ITEMS:
{items}
"""


class ExplainError(RuntimeError):
    """Bad explain request (unknown node, malformed items)."""


def _sha(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8", "replace")).hexdigest()[:16]


def content_hash(graph, node_id: str, _depth: int = 0) -> str:
    """Dependency-aware identity of what an explanation would describe."""
    a = graph.nodes.get(node_id)
    if a is None:
        return "missing"
    kind = a.get("type", "")
    if kind == "file":
        return _sha(f"{a.get('mtime', '')}|{a.get('summary', '')}")
    if kind in ("func", "class"):
        parent = graph.nodes.get(f"file:{a.get('path', '')}", {})
        return _sha(f"{a.get('signature', '')}|{a.get('start_line', '')}-{a.get('end_line', '')}"
                    f"|{a.get('summary', '')}|{parent.get('mtime', '')}")
    if kind == "feature":
        review = a.get("review") or {}
        return _sha(f"{sorted(a.get('members') or [])}|{a.get('summary', '')}"
                    f"|{a.get('description', '')}|{review.get('verdict', '')}")
    if kind in ("component", "system") and _depth < 3:
        kids = "|".join(content_hash(graph, m, _depth + 1)
                        for m in sorted(a.get("members") or []))
        return _sha(f"{a.get('description', '')}|{kids}")
    return _sha(json.dumps({k: str(v)[:200] for k, v in sorted(a.items())}, sort_keys=True))


def cache_key(node_id: str, chash: str) -> str:
    return _sha(f"{node_id}|{chash}|{PROMPT_VERSION}")


def _cache_path(root: Path) -> Path:
    return Path(root) / config.MEMORY_DIR_NAME / EXPLAIN_FILE


def load_cache(root: Path) -> dict:
    try:
        return json.loads(_cache_path(root).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _save_cache(root: Path, cache: dict) -> None:
    path = _cache_path(root)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(cache, ensure_ascii=False), encoding="utf-8")
        tmp.replace(path)
    except OSError:
        pass  # a convenience cache, never load-bearing


def _facts(graph, node_id: str) -> str:
    """The evidence pack one node's explanation may draw from."""
    a = graph.nodes[node_id]
    kind = a.get("type", "")
    rows = [f"kind: {kind}", f"name: {a.get('name', '')}"]
    if a.get("path"):
        rows.append(f"path: {a['path']}")
    if a.get("description"):
        rows.append(f"description: {a['description'][:400]}")
    if a.get("summary"):
        rows.append(f"summary: {str(a['summary'])[:500]}")
    if kind in ("func", "class") and a.get("signature"):
        rows.append(f"signature: {a['signature']}")
    if kind in ("system", "component"):
        names = [graph.nodes[m].get("name", m) for m in (a.get("members") or [])
                 if graph.has_node(m)]
        rows.append(f"contains: {', '.join(names[:12])}")
    if kind == "feature":
        members = [m.split("::")[-1] for m in (a.get("members") or [])][:10]
        rows.append(f"member code: {', '.join(members)}")
        review = a.get("review") or {}
        if review.get("verdict"):
            rows.append(f"review verdict: {review['verdict']}")
    return "\n".join(rows)


def _parse_reply(reply: str, expected: int) -> list[str] | None:
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


def _structural_text(a: dict) -> str:
    base = (a.get("description") or a.get("summary") or "").strip()
    label = "(structural data — no AI explanation generated yet)"
    return f"{base}\n\n{label}" if base else label


# @memory:feature:HumanViewResolution
# @memory:connects:ComprehensionLens, MemoryViewer
# @memory:summary:Cache-first per-node human explanations keyed by node id + dependency-aware content hash — one LLM batch per set of misses, honest structural fallback under mock, stale entries never match again.
def explain_nodes(root: Path, graph, items: list[dict],
                  provider: SummaryProvider, force: bool = False) -> dict:
    """Explain canonical nodes ``[{id}]``. Returns ``{real, results: {id:
    {text, status, provider}}}`` where status is cached|generated|structural."""
    if not isinstance(items, list) or len(items) > MAX_ITEMS:
        raise ExplainError(f"items must be a list of at most {MAX_ITEMS}")
    ids = []
    for it in items:
        node_id = it.get("id") if isinstance(it, dict) else None
        if not node_id or not graph.has_node(str(node_id)):
            raise ExplainError(f"unknown node {node_id!r}")
        ids.append(str(node_id))

    root = Path(root).resolve()
    project = root.name
    real = provider.name != "mock"
    results: dict[str, dict] = {}
    with _lock:
        cache = load_cache(root)
        misses: list[tuple[str, str]] = []  # (node_id, key)
        for node_id in ids:
            key = cache_key(node_id, content_hash(graph, node_id))
            hit = None if force else cache.get(key)
            if hit:
                results[node_id] = {"text": hit["text"], "status": "cached",
                                    "provider": hit.get("provider")}
            else:
                misses.append((node_id, key))

        if misses and real:
            generated = 0
            for i in range(0, len(misses), CHUNK_SIZE):
                chunk = misses[i:i + CHUNK_SIZE]
                numbered = "\n\n".join(
                    f"{n + 1}. [{graph.nodes[nid].get('type')}] {graph.nodes[nid].get('name')}\n"
                    f"{_facts(graph, nid)}"
                    for n, (nid, _) in enumerate(chunk))
                depths = {graph.nodes[nid].get("type", "") for nid, _ in chunk}
                depth = _KIND_DEPTH.get(depths.pop() if len(depths) == 1 else "feature",
                                        _KIND_DEPTH["feature"])
                prompt = _BATCH_PROMPT.format(project=project, depth=depth,
                                              count=len(chunk), items=numbered)
                try:
                    reply = provider.summarize(prompt, {"max_tokens": CHUNK_MAX_TOKENS})
                    texts = _parse_reply(reply, len(chunk))
                except Exception:
                    texts = None
                for (nid, key), text in zip(chunk, texts or [None] * len(chunk)):
                    if text:
                        cache[key] = {"node_id": nid, "text": text,
                                      "provider": provider.name,
                                      "model": getattr(provider, "model", None)}
                        results[nid] = {"text": text, "status": "generated",
                                        "provider": provider.name}
                        generated += 1
                    else:  # kept uncached so a later request retries
                        results[nid] = {"text": _structural_text(graph.nodes[nid]),
                                        "status": "structural", "provider": None}
            if generated:
                _save_cache(root, cache)
        elif misses:  # mock: honest structural text, never cached
            for nid, _key in misses:
                results[nid] = {"text": _structural_text(graph.nodes[nid]),
                                "status": "structural", "provider": None}

    return {"real": real, "results": results}


def prune_explanations(root: Path, graph) -> int:
    """Drop cache entries whose node vanished or whose content drifted;
    returns how many were removed. Called after updates — cheap hygiene."""
    with _lock:
        cache = load_cache(root)
        keep = {}
        for key, entry in cache.items():
            nid = entry.get("node_id", "")
            if graph.has_node(nid) and cache_key(nid, content_hash(graph, nid)) == key:
                keep[key] = entry
        removed = len(cache) - len(keep)
        if removed:
            _save_cache(Path(root).resolve(), keep)
    return removed
