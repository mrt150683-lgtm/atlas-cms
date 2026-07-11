"""Git history layer — temporal understanding the static graph can't see.

Per file: commit count, distinct authors, churn (lines added+deleted), last
change timestamp. Across files: co-change coupling — pairs that repeatedly
change in the same commit *without* an import relationship are hidden coupling,
added as CO_CHANGES edges. Silently no-ops outside a git repository.
"""

from __future__ import annotations

import subprocess
import time
from collections import Counter, defaultdict
from pathlib import Path

import networkx as nx

MAX_COMMITS = 1000
MAX_FILES_PER_COMMIT = 30   # bigger commits are bulk moves, not coupling signal
MIN_COCHANGES = 3
MAX_COCHANGE_EDGES = 40


def _git(root: Path, *args: str) -> str | None:
    try:
        result = subprocess.run(
            ["git", *args], cwd=root, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=60,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return result.stdout if result.returncode == 0 else None


def collect_git_history(root: Path) -> dict | None:
    """Returns {"files": {rel_path: stats}, "cochanges": [(a, b, count)]} or None."""
    if _git(root, "rev-parse", "--is-inside-work-tree") is None:
        return None
    prefix = (_git(root, "rev-parse", "--show-prefix") or "").strip()
    log = _git(
        root, "log", f"--max-count={MAX_COMMITS}",
        "--numstat", "--no-renames", "--format=@@%H|%at|%an",
    )
    if log is None:
        return None

    files: dict[str, dict] = defaultdict(
        lambda: {"commits": 0, "authors": set(), "churn": 0, "last_ts": 0.0}
    )
    pair_counts: Counter = Counter()
    commit_files: list[str] = []
    author = ""
    timestamp = 0.0

    def flush_commit() -> None:
        if 1 < len(commit_files) <= MAX_FILES_PER_COMMIT:
            unique = sorted(set(commit_files))
            for i in range(len(unique)):
                for j in range(i + 1, len(unique)):
                    pair_counts[(unique[i], unique[j])] += 1
        commit_files.clear()

    for line in log.splitlines():
        if line.startswith("@@"):
            flush_commit()
            _, ts, author = line[2:].split("|", 2)
            timestamp = float(ts)
            continue
        parts = line.split("\t")
        if len(parts) != 3:
            continue
        added, deleted, path = parts
        if prefix:
            if not path.startswith(prefix):
                continue
            path = path[len(prefix):]
        stats = files[path]
        stats["commits"] += 1
        stats["authors"].add(author)
        stats["last_ts"] = max(stats["last_ts"], timestamp)
        if added.isdigit():
            stats["churn"] += int(added)
        if deleted.isdigit():
            stats["churn"] += int(deleted)
        commit_files.append(path)
    flush_commit()

    cochanges = [
        (a, b, n) for (a, b), n in pair_counts.most_common(200) if n >= MIN_COCHANGES
    ]
    return {
        "files": {p: dict(s, authors=sorted(s["authors"])) for p, s in files.items()},
        "cochanges": cochanges,
    }


# @memory:feature:GitHistoryLayer
# @memory:connects:KnowledgeGraphConstruction, MemoryViewer
# @memory:summary:Temporal layer — per-file commits/authors/churn/age plus CO_CHANGES edges for hidden coupling (files changing together without imports).
def enrich_graph_with_git(graph: nx.DiGraph, root: Path) -> dict | None:
    """Attach git stats to file nodes and add CO_CHANGES edges for hidden coupling."""
    history = collect_git_history(root)
    if history is None:
        return None
    now = time.time()
    matched = 0
    for path, stats in history["files"].items():
        node_id = f"file:{path}"
        if not graph.has_node(node_id):
            continue
        matched += 1
        graph.nodes[node_id]["git"] = {
            "commits": stats["commits"],
            "authors": stats["authors"],
            "churn": stats["churn"],
            "last_ts": stats["last_ts"],
            "age_days": round((now - stats["last_ts"]) / 86400, 1),
        }

    added_edges = 0
    for a, b, count in history["cochanges"]:
        if added_edges >= MAX_COCHANGE_EDGES:
            break
        na, nb = f"file:{a}", f"file:{b}"
        if not (graph.has_node(na) and graph.has_node(nb)):
            continue
        # only *hidden* coupling: no import relationship in either direction
        linked = any(
            graph.has_edge(x, y) and graph.edges[x, y].get("type") == "IMPORTS"
            for x, y in ((na, nb), (nb, na))
        )
        if linked:
            continue
        graph.add_edge(na, nb, type="CO_CHANGES", weight=count, provenance="git")
        added_edges += 1
    return {"files": matched, "cochange_edges": added_edges}
