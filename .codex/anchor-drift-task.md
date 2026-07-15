# Codex task — build "Anchor Drift" for Atlas, using Atlas to do it

You are working in the **Atlas / CodeCrawl** repo (CLI + Python package `cms`): a
Codebase Memory System that sits over a codebase as an intent-vs-reality
alignment layer for AI agents. It exposes three surfaces you must treat as
first-class: an **agent surface** (MCP tools + `cms` CLI), a **human surface**
(a localhost web UI viewer), and a **memory layer** (`.memory/`).

This task is two things at once, and both are graded:
1. **Ship a genuinely useful new tool** (spec below), and
2. **Test Atlas by using it as an agent is meant to** — consult the memory layer
   before you grep, drive the alignment loop, and then report candidly on whether
   Atlas actually earned its keep.

## The core idea you're building: Anchor Drift

Atlas lets humans encode intent the AST can't infer, as `# @memory:` comments —
`@memory:summary:<one-line intent>` and `@memory:connects:<FeatureA, FeatureB>`.
These land on graph nodes as an `anchors` dict (`cms/graph_builder.py:~269`,
parsed by `cms/anchors.py`). Over time the **code moves and the anchor doesn't** —
the human's stated intent silently rots. Nothing in Atlas catches this today
(Sentinel's "memory is stale" is whole-file mtime; review `drift` is
feature-level built-vs-expected — neither inspects individual anchors).

**Anchor Drift** finds, per anchored node, where the declared intent no longer
matches reality. This is Atlas's own thesis at the finest grain: the model sees
the code from one side; the human's intent sits on the other; you flag the gap.

### MVP (must ship, deterministic — no LLM required, must work under `CMS_PROVIDER=mock`)
Detect and report these signals with **high precision — under-report before you
cry wolf** (the product's whole credibility is "prove, don't claim"):

- **`summary-symbol-drift`**: a `summary` anchor names a code identifier
  (backticked, or a clear `snake_case`/`CamelCase` symbol) that no longer appears
  anywhere in that node's *current* source range. Read the source via the same
  path `get_source`/`cms/sources.py` uses. Be conservative: only flag tokens you
  can confidently classify as code symbols, never prose words.
- **`connect-without-evidence`**: a `@memory:connects:` declared feature link
  (a `CONNECTS` edge) with **no** supporting code-level evidence — i.e. no
  `RELATES` edge (and no member-level CALLS/IMPORTS) between the two features'
  members. The human declared a relationship the code doesn't back.

### Stretch (only if the MVP is solid and green)
- **`unstated-strong-coupling`**: the inverse — features with strong code
  coupling (`RELATES` with a high `via` count) but **no** declared `connects:`.
- **`summary-staleness`**: the anchored member's body was last changed in a
  commit *newer* than the commit that introduced the anchor line. Note: there is
  **no per-line blame helper yet** — you'd add one to `cms/githistory.py` (it has
  `_git()` and history collection, but no `git blame`). Keep it optional.

## The surfaces to wire (mirror existing patterns — do not invent new ones)

- **Detector**: a new pure module `cms/anchordrift.py` returning structured,
  JSON-able findings (follow the dataclass style of `cms/impact.py`).
- **CLI**: `cms drift [--json]` (typer `@app.command()` in `cms/cli.py`, like
  `impact`/`verify`). Print per-node findings; **exit non-zero** if any
  high-confidence drift exists (it's a gate).
- **MCP tool**: `get_anchor_drift(target=None)` in `cms/mcp.py` (register it like
  `get_impact`); add it to the tool list in the docs.
- **HTTP + UI**: a `/api/drift` route in `cms/ui.py` and a **legible** indicator
  in `cms/ui_assets/index.html`. A recent commit added the trust-loop live
  actions (`/api/impact`, `/api/verify`, `/api/align` + inspector rendering) —
  copy that exact pattern for routing and rendering. The human-facing wording
  must read in plain language as "the stated intent here no longer matches the
  code," not as jargon.
- **Sentinel**: emit drift as a fingerprint-keyed finding so the quality gate
  covers it (add a module under `cms/sentinel/` alongside `contracts.py` /
  `static_risk.py`, wired into `runner.py`). Keep severity honest (medium/low).
- **Keep memory honest**: put `@memory:summary`/`@memory:feature` anchors on your
  new code, then run `cms update`. (Yes — your new feature must not itself drift.)

## The dual-perspective mandate (this is the grade, not a nicety)

Your output must be correct from **both** sides, and you must prove **both**:
- **Code side** — deterministic, tested, no false-positive noise. Add tests with
  at least one true-positive fixture (a deliberately drifted anchor) and one
  true-negative (an honest anchor that must NOT be flagged).
- **Human side** — actually launch the UI (`cms app`, or `cms ui --no-browser`),
  create a deliberately drifted anchor in a scratch fixture, and confirm it
  renders legibly. The MCP↔HTTP↔UI↔docs **contracts must stay consistent**
  (`cms sentinel`'s contracts module checks this). Record what you observed.

## Use Atlas to do this — and log it (this is the app test)

Read `SKILL.md` and `README.md` in the repo first. Bootstrap:
`pip install -e .[dev]` → `CMS_PROVIDER=mock cms run-all` → `cms app`.

Then follow the golden loop, and **record each step's Atlas call and what it
returned** for your report:
1. `cms ask "where are @memory anchors parsed and attached to graph nodes?"` and
   `cms query "..."` to locate the machinery — **before** grepping.
2. `cms features` / `cms trace <Feature>` / `cms impact <target>` to understand
   the blast radius of your edits.
3. `cms review` and `cms suggest` — reconcile your plan with what Atlas already
   thinks is worth doing. If `cms suggest` surfaces something you judge higher
   value than Anchor Drift, you may swap — but justify it in writing.
4. `get_source`/`cms` surgical reads only where a summary pointed you.
5. Build. Then close the loop: `cms align "add anchor-drift detection across
   CLI/MCP/UI/Sentinel" --scan` and act on its `gaps`/`tests_to_run`.

## Verification bar — all must hold before you call it done
- `pytest` green, including your new true-positive and true-negative fixtures.
- `cms drift` run against **this actual repo** — report the real drifted anchors
  it finds (that's a concrete result, not a demo).
- `cms sentinel` — no new active *criticals*; the new finding type appears when a
  drifted anchor is present and clears when fixed.
- `cms verify <YourNewFeature>` — its mapped tests execute it.
- `cms align ... --scan` — verdict is **not** `drift`.
- UI observed and described (a drifted anchor lights up; wording is human-legible).

## Deliverables
1. The code, tests, and `@memory` anchors on the new code.
2. **`CODEX_ATLAS_REPORT.md`** — the real payload of this exercise:
   - A per-step log: which Atlas tool you used, what it returned, and whether it
     was **accurate / stale / wrong / unhelpful** versus what a blind grep would
     have given you.
   - A verdict: did "consult memory before grep" actually make you faster and more
     grounded on Atlas's own codebase? Where did it hallucinate or mislead?
   - A concrete bug/friction list (repro steps) for anything in Atlas that
     surprised you — the UI, the CLI, the graph, the docs contracts.
   - The list of genuine anchor-drift findings `cms drift` produced on this repo.

## Guardrails
- Do **not** weaken or delete existing tests to go green; if one legitimately must
  change, say why.
- Conservative detection over noisy: a false positive here discredits the whole
  premise.
- Match existing idioms (dataclasses, the `cms/ui.py` route pattern, typer
  commands, Sentinel module shape). Localhost-only UI. Core must run under mock.
- The prebuilt `CMS.exe` snapshots code+UI at build time — you do **not** need to
  rebuild it; just note it if relevant.
