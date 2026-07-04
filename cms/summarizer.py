"""Phase 3: low-resolution AI summary generation.

For each Python file node, builds the spec's prompt with the file source,
calls the configured provider, stores the result on the file node, and maps
per-component lines onto child Function/Class nodes by name match.
"""

from __future__ import annotations

import re
from pathlib import Path

import networkx as nx

from . import config
from .providers import SummaryProvider

PROMPT_TEMPLATE = """You are an expert senior software architect creating a LOW-RESOLUTION structural map of code for another AI agent.

The goal is to give the agent a fast, accurate "mental model" of the codebase so it knows where to look and what things do, without needing to read every line.

File: {relative_path}
Language: {language}
Total lines: {line_count}

Here is the source:

```{language}
{source_code}
```

Task:
Produce a short, dense summary with these exact sections:

1. **File Purpose** (1-2 sentences max)
2. **Key Components** (for each important top-level function, class, or block):
   - Name + line range (e.g. `def process_directory() 45-112`)
   - One-sentence intent
   - Main control flow notes (loops, conditionals, key variables)
   - What it calls or depends on (from this file or imports)
3. **Important Connections**
   - Files/modules this imports or is imported by
   - Key data flow or shared state

Rules:
- Stay low-resolution. Focus on *why* and *how it fits*, not implementation details.
- Use relative line numbers.
- Be brutally concise. Total output should fit in ~15-25 lines for most files.
- If something is boilerplate or obvious, say so briefly.
- Never invent behaviour not visible in the provided code.
"""

ANCHORS_PROMPT_SECTION = """
Developer memory anchors were embedded in this file (authoritative intent notes
written by the developers — treat them as ground truth and weave them into the
relevant sections):

{anchor_lines}
"""


def _anchor_prompt_lines(context: dict) -> str:
    from .anchors import anchors_as_text

    lines: list[str] = []
    if context.get("anchors"):
        lines.append(f"- (file) {anchors_as_text(context['anchors'])}")
    for c in context.get("components", []):
        if c.get("anchors"):
            lines.append(f"- {c['name']} ({c['start_line']}-{c['end_line']}): {anchors_as_text(c['anchors'])}")
    return "\n".join(lines)


def _truncate_source(source: str, budget: int = config.MAX_SOURCE_CHARS) -> str:
    if len(source) <= budget:
        return source
    head = int(budget * 2 / 3)
    tail = budget - head
    return (
        source[:head]
        + "\n\n# ... [source truncated by cms: middle section omitted] ...\n\n"
        + source[-tail:]
    )


def _file_context(graph: nx.DiGraph, file_id: str) -> dict:
    """Structural facts about a file node, for prompts and the mock provider."""
    attrs = graph.nodes[file_id]
    components = []
    for _, child, edata in graph.out_edges(file_id, data=True):
        if edata.get("type") != "CONTAINS":
            continue
        c = graph.nodes[child]
        components.append(
            {
                "kind": c["type"],
                "name": c["name"],
                "qualname": c.get("qualname", c["name"]),
                "start_line": c.get("start_line", 0),
                "end_line": c.get("end_line", 0),
                "signature": c.get("signature", ""),
                "docstring": c.get("docstring", ""),
                "anchors": c.get("anchors") or {},
            }
        )
    components.sort(key=lambda c: c["start_line"])
    imports = sorted(
        graph.nodes[t].get("path", graph.nodes[t].get("name", t))
        for _, t, edata in graph.out_edges(file_id, data=True)
        if edata.get("type") == "IMPORTS"
    )
    return {
        "path": attrs["path"],
        "language": attrs.get("language", ""),
        "line_count": attrs.get("line_count", 0),
        "components": components,
        "imports": imports,
        "anchors": attrs.get("anchors") or {},
    }


def _component_slice(summary: str, name: str) -> str:
    """Pull the bullet block mentioning `name` out of a file summary."""
    lines = summary.splitlines()
    pattern = re.compile(rf"\b{re.escape(name)}\b")
    for i, line in enumerate(lines):
        if line.lstrip().startswith(("-", "*")) and pattern.search(line):
            block = [line.strip()]
            for follow in lines[i + 1 : i + 4]:
                stripped = follow.strip()
                if not stripped or stripped.startswith(("-", "*", "#")) or stripped[0].isdigit():
                    break
                block.append(stripped)
            return "\n".join(block)
    return ""


# @memory:feature:SummaryGenerator
# @memory:connects:KnowledgeGraphConstruction, MemoryAnchors, QueryEngine
# @memory:summary:Per-file LLM summarization with anchor-aware prompts; maps summary bullets onto child class/function nodes.
def generate_summaries(
    graph: nx.DiGraph,
    root: Path,
    provider: SummaryProvider,
    on_progress=None,
    only_paths: set[str] | None = None,
) -> int:
    """Summarize Python file nodes (and their components). Returns files processed.

    `only_paths` restricts work to those rel paths (incremental updates)."""
    file_ids = [
        n for n, a in graph.nodes(data=True)
        if a.get("type") == "file" and a.get("language") == "python"
        and (only_paths is None or a.get("path") in only_paths)
    ]
    done = 0
    for file_id in sorted(file_ids):
        attrs = graph.nodes[file_id]
        context = _file_context(graph, file_id)
        try:
            source = (root / attrs["path"]).read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        prompt = PROMPT_TEMPLATE.format(
            relative_path=attrs["path"],
            language=attrs.get("language", ""),
            line_count=attrs.get("line_count", 0),
            source_code=_truncate_source(source),
        )
        anchor_lines = _anchor_prompt_lines(context)
        if anchor_lines:
            prompt += ANCHORS_PROMPT_SECTION.format(anchor_lines=anchor_lines)
        summary = provider.summarize(prompt, context)
        attrs["summary"] = summary
        attrs["summary_meta"] = {"provider": provider.name}

        # descend one level: map component bullets to child class/func nodes
        for _, child, edata in graph.out_edges(file_id, data=True):
            if edata.get("type") != "CONTAINS":
                continue
            child_attrs = graph.nodes[child]
            piece = _component_slice(summary, child_attrs["name"])
            if piece:
                child_attrs["summary"] = piece
            # methods one level deeper inherit their class's slice as fallback
            for _, grandchild, gdata in graph.out_edges(child, data=True):
                if gdata.get("type") != "CONTAINS":
                    continue
                g_attrs = graph.nodes[grandchild]
                g_piece = _component_slice(summary, g_attrs["name"])
                if g_piece:
                    g_attrs["summary"] = g_piece

        done += 1
        if on_progress:
            on_progress(attrs["path"], done, len(file_ids))
    return done
