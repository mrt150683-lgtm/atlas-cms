---
id: memory-first
name: Memory First
type: strategy
description: Consult the Atlas memory layer before grepping or reading whole files — query, summarize, then read surgically.
tags: [atlas, workflow, context]
---

# Memory First

The one rule: consult memory before grep. If a `cms` MCP server is attached
or a `.memory/` dir exists:

1. `query_codebase("<topic>")` — find where things are, ranked with summaries.
2. `get_file_summary(path)` — a file's components, line ranges, and intent,
   cheaper than reading the file.
3. `get_source(path, start, end)` — surgical raw reads only after the summary
   told you where. Whole-file reads are a last resort.
4. `get_impact(target)` — blast radius and covering tests before you edit.

This keeps agent context small, precise, and grounded in the live graph
instead of guesses.
