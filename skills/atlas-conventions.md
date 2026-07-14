---
id: atlas-conventions
name: Atlas Conventions
type: preference
description: House conventions for working in an Atlas-mapped codebase — anchors, memory upkeep, and honest completion habits.
tags: [atlas, conventions, memory]
---

# Atlas Conventions

- Tag significant new functions/classes with `@memory:` anchors
  (`# @memory:feature:Name`, `# @memory:connects:A, B`, `# @memory:summary:...`)
  so the memory layer stays honest about intent.
- After changing code run `cms update` (or keep `cms watch` running) so the
  memory stays current; stale memory is itself flagged.
- Run `cms verify <Feature>` after a change to confirm the mapped tests still
  pass; coverage proves execution, not complete behavioural correctness.
- Keep CLI output plain ASCII (Windows consoles are cp1252).
- Word docstrings carefully: the Sentinel static-risk module flags
  suspicious vocabulary in cms/ docstrings.
