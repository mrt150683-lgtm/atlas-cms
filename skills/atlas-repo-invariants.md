---
id: atlas-repo-invariants
name: Atlas Repo Invariants
type: constraint
description: Hard rules for working inside the Atlas/cms codebase itself — no new dependencies, ASCII CLI output, paired surfaces and docs, carried-over feature attributes, generated state is not hand-edited. Load this before changing anything under cms/, and treat every rule here as a gate rather than a suggestion.
tags: [atlas, repo, invariants]
requires: [atlas-conventions]
---

# Atlas Repo Invariants

Rules that are cheap to honour and expensive to discover the hard way. Each one
exists because breaking it produces a failure that does not look like its cause.

## Dependencies

**Do not add a runtime dependency.** The runtime is `networkx`, `pathspec`,
`typer` — nothing else. Atlas ships as a single local tool that must run offline
and build into a frozen exe; each new import is a new way for that to break.
There is no YAML parser here on purpose. If you need one, hand-roll the smallest
strict parser that does the job and fail loudly on anything it does not
understand.

## Surfaces come in pairs

Sentinel's contract checks fail the build when a surface and its documentation
drift apart. When you add:

- **an MCP tool** — add the `TOOLS` entry *and* the identically named
  `MCPServer` method (arguments must match the schema), then document it in
  `README.md` **and** `SKILL.md`, and bump the `MCP tools (N)` count in
  `SKILL.md`.
- **an HTTP route** — a page must actually call it, and a handler must exist.
- **a CLI command** — mention it in `README.md`.

The check is not bureaucracy: an undocumented tool is a tool agents never
discover, and a route no page calls is dead code that still answers.

## Generated state is generated

`.memory/` is derived, never hand-edited. After changing code, run `cms update`
(or keep `cms watch` running) or Sentinel will correctly report the memory layer
as stale. Never write a completion marker you did not earn — semantic stages are
recorded from positive evidence (a real provider actually ran), and mock output
is labelled as mock.

## Durable feature attributes must survive an update

Any new durable attribute on a feature node must be added to the carry-over
tuple in `cms/update.py` **and** to the Sentinel carry-over probe. Miss this and
incremental updates silently erase the data — the tests still pass, and the loss
only shows up later as an empty panel. Better still: keep new state in its own
store and stay off the graph nodes entirely.

## Vocabulary in docstrings

Sentinel's static-risk module scans `cms/` for words that signal a bypass
(fake, force, skip-the-check). Write docstrings that describe the guarantee, not
the loophole: "published content is frozen; changes ship as a new version"
rather than "you cannot force an overwrite".

## Windows is the primary platform

CLI output is plain ASCII — the console is cp1252 and fancy glyphs raise
`UnicodeEncodeError` on a user's machine, not yours. PowerShell 5.1 has no `&&`.
The exe locks its own file while running, so a rebuild needs the app stopped.

## Human authority is a mechanism, not a convention

Approving a decision, confirming a discovered feature, and publishing a library
asset are human acts. They are gated by a per-session code printed only to the
launching terminal — a channel an agent driving the HTTP API cannot see — and
they are absent from the MCP surface entirely. Do not add an agent-reachable
path to any of them, however convenient it would be.
