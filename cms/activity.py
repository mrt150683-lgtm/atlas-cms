"""MCP activity feed — lets the UI visualize live memory access.

The MCP server appends one JSON line per tool call (tool name, touched node
ids, timestamp) to ``.memory/activity.jsonl``; the UI polls it and renders
gentle glow pulses on the touched graph nodes. Append-only with size-capped
rotation; all failures are silent (the feed is cosmetic, never load-bearing).
"""

from __future__ import annotations

import json
import time
from pathlib import Path

ACTIVITY_FILE = "activity.jsonl"
MAX_BYTES = 128_000
KEEP_LINES = 120
MAX_NODES_PER_EVENT = 40


# @memory:feature:ActivityPulse
# @memory:connects:AgentMemoryAccess, MemoryViewer
# @memory:summary:Live visibility of agent memory access — MCP tool calls log touched nodes to activity.jsonl; the UI polls and renders glow pulses on those nodes.
def log_activity(memory_dir: Path, tool: str, nodes: list[str], label: str = "") -> None:
    try:
        memory_dir.mkdir(parents=True, exist_ok=True)
        path = memory_dir / ACTIVITY_FILE
        entry = {
            "ts": time.time(),
            "tool": tool,
            "nodes": nodes[:MAX_NODES_PER_EVENT],
            "label": label[:160],
        }
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")
        if path.stat().st_size > MAX_BYTES:
            lines = path.read_text(encoding="utf-8").splitlines()[-KEEP_LINES:]
            path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    except OSError:
        pass


def read_activity(memory_dir: Path, since: float) -> list[dict]:
    path = memory_dir / ACTIVITY_FILE
    if not path.is_file():
        return []
    events = []
    try:
        for line in path.read_text(encoding="utf-8").splitlines()[-KEEP_LINES:]:
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            if entry.get("ts", 0) > since:
                events.append(entry)
    except OSError:
        return []
    return events
