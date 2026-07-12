---
name: atlas
description: >-
  Full operating manual for Atlas (the Codebase Memory System; CLI/package
  `cms`) — the structural + semantic memory layer that sits over a codebase for
  AI agents: every codebase, mapped; ground truth for AI agents. Read this
  before working in any repo that has a `.memory/` directory or a `cms` MCP
  server attached. Covers every MCP tool, CLI command, the knowledge-graph
  model, memory anchors, feature tracing, AI review, ROI suggestions, the
  Hermes Sentinel quality gate, and the Change-Alignment loop. Use it to
  consult memory before grep, to ground edits, and to prove a change did what
  it was meant to.
---

# ▲ Atlas — Codebase Memory System: the agent's manual

*Product: **Atlas**. Command line & Python package: **`cms`** (e.g. `cms run-all`, `cms mcp`). MCP server id: `cms`.*

## 1. What Atlas is (mental model)

Atlas is **not** a code browser and **not** RAG-over-code. It is an
**intent-vs-reality alignment engine**: a small, always-current map that sits
*over* a codebase so an AI agent can reason about *where things are, how they
connect, what they do, and whether the code matches its stated intent* — before
(or instead of) reading raw files.

Two directions of value:

- **Context in (anti-hallucination):** query the graph + summaries for tiny,
  precise context (file paths, line ranges, call edges, intent summaries)
  instead of grepping and blowing up your context window.
- **Judgment out (anti-false-completion):** every layer measures the gap
  between what code *claims* (declared features, docs, anchors, completion
  claims) and what it *is* (AST, traced flows, passing tests, graph evidence),
  and closes that loop around a *unit of work* with the alignment check.

**The one rule:** *consult memory before grep.* If a `cms` MCP server is
attached or a `.memory/` dir exists, use the tools below first.

## 2. The golden agent workflow

```
1. declare_intent("what I'm about to do")   ← records intent, returns a grounded brief
2. query_codebase / get_file_summary / get_feature_trace / get_impact   ← find where + how, read only what matters
3. get_source(path, start, end)             ← surgical raw reads (never whole files first)
4. …make the change…
5. check_alignment()                         ← verdict: did it land, covered, without drift? + exact tests to run
6. run the tests it names; if drift/partial, fix and re-check
```

This is the loop Atlas exists to serve. `declare_intent` + `check_alignment` are
the input and the honest-finish of a change; everything else is grounding.

## 3. MCP tools (18) — the primary agent surface

Server id `cms` (stdio JSON-RPC). Every call is logged to
`.memory/activity.jsonl` and rendered live in the UI (glow pulses + a badge —
the human can literally watch you think). Tools:

**Grounding / read**
- `query_codebase(query, top_k=5)` — plain-language search ("where is the ignore
  filtering?"). Returns ranked nodes: `node_id, kind, name, path, lines, score,
  summary, calls, called_by`. Your first move on any unfamiliar area.
- `get_file_summary(path)` — a file's language, line count, AI summary, `@memory`
  anchors, git stats, and every component (funcs/classes) with line ranges +
  summaries. Cheaper than reading the file.
- `get_source(path, start_line=1, end_line=None)` — exact snippet by line range.
  The **only** correct way to read raw code — surgical, after the summary told
  you where. Guarded to the project root and scanned source extensions.
- `get_feature_trace(name)` — one feature's Purpose, Flow (call chain with
  `file:line` at each step), Inputs/Outputs, members, entry points, verification
  checklist.
- `list_features()` — all features (declared + AI-discovered) with member counts
  and connections.
- `who_calls(name)` — caller node ids of a function/method.
- `who_imports(path)` — files importing a given file.
- `get_impact(target)` — **blast radius**: everything downstream of changing
  `target` (accepts a node id, `path::qualname`, a rel path, or a bare name) —
  affected functions, files, features, and the tests that cover the chain.

**Judgment / plan**
- `get_review(feature=None)` — the alignment audit verdict(s):
  `aligned | partial | drift | unverified` with headline, expected-vs-built, and
  concrete gaps. Read the touched feature's gaps *before* editing it.
- `get_suggestions()` — the ROI-ranked plan of what's worth building next
  (value/effort, highest return first). Pick your next task by ROI.
- `get_sentinel_report(severity=None)` — latest quality-gate scan: gate status,
  workflow checks, and active findings (bugs/risks). Check this before claiming
  done.
- `export_task_prompt(task, as_json=False)` — assemble a full, memory-grounded
  task brief (where to work, features involved, blast radius, related planned
  work, verification steps) for a described task.

**Constellation (multi-project discovery) — the conversational fusion loop**
- `list_projects()` — every Atlas-mapped project on the machine with
  readiness, feature counts and hashes.
- `get_fusion_report()` — the latest cross-project fusion report
  (integrations / emergent features / conflicts) + drifted members +
  refinement history.
- `refine_fusion(direction)` — revise the report per the user's direction.
  **This is how you converse a hybrid-app plan into shape**: get the report,
  discuss it with the user, consolidate their steer into one direction,
  refine, repeat. Real provider required; every refinement is recorded;
  failures never clobber the last good report.

**The alignment loop (intent → verdict)**
- `declare_intent(goal=None)` — record what the current change is meant to do
  (if `goal` omitted, inferred from git branch / last commit). Returns a grounded
  brief and stores the active intent. **Call this first.**
- `check_alignment(base="HEAD", scan=False)` — judge the working diff against the
  declared intent. Returns `verdict` (aligned/partial/drift/unverified),
  `headline`, `changed` files, `touched_features`, `feature_reviews`, `impact`
  (blast radius), `tests_to_run`, Sentinel `findings` on changed files, and
  `gaps` (e.g. `intent-target-untouched`, `unstated-change`,
  `no-verifying-tests`). **Call this to prove you finished honestly.** Requires a
  prior `declare_intent`.

**Session control**
- `switch_project(path)` — flip this server to another project root mid-session
  ("let's work on X now"). Walks up from `path` to the nearest mapped root and
  rebinds every tool (including the `get_source` guard) to it. Only real
  projects are accepted (a dir with `.memory/` or `.git`). If the target has no
  memory layer yet it still switches and returns the exact `cms run-all` command
  — run it in a shell, then query again; the new graph is picked up
  automatically, no restart.

## 4. CLI reference (for humans + `CMS.exe`)

Everything the MCP exposes is also a command; the exe (`CMS.exe <cmd>`) mirrors
the CLI. `--root PATH` targets a project; the API key is read from
`~/.cms/config.json`.

**Build / maintain the memory**
- `cms run-all` — full pipeline: scan → graph → summaries → features → git →
  `.memory/`.
- `cms scan` / `cms build-graph` / `cms summarize` — individual stages.
- `cms trace [Feature]` — build/refresh feature traces (or print one).
- `cms update [--full] [-p provider]` — incremental: only changed files
  re-summarized/re-traced. Run after edits. A mock-built project is
  completed on the first real-provider update: mock summaries upgrade AND
  LLM feature discovery re-runs (mock builds skip discovery entirely).
- `cms watch` — keep `.memory/` in sync live as you edit.
- `cms app` (or bare `cms`) — sync memory → watcher → serve UI → open browser.
  With a real provider, a project's first build also triggers the judgment
  modules (AI review + ROI suggestions) so a new codebase gets every layer,
  not just the map; with mock, the skip is stated explicitly.

**Query / understand**
- `cms query "…"` — plain-language search (same engine as `query_codebase`).
- `cms features` — list features with member/entry counts.
- `cms impact <target>` — blast radius + suggested `pytest` line.
- `cms prompt "<plan>"` — export a memory-grounded task brief (`--json` for the
  data pack).

**Judge / verify**
- `cms review [Feature]` — build/print the built-vs-expected alignment audit.
- `cms suggest` — ROI-ranked plan of what to build next.
- `cms verify` — map tests → features via coverage contexts (`exercised_by`;
  coverage proves the tests *execute* the feature, not that behaviour is correct).
  `cms verify <Feature>` runs exactly the tests proving that feature.
- `cms sentinel` — the quality gate (see §8). Subcommands: `run`, `findings`,
  `show <id>`, `status <id> <status> --reason …`, `export`, `ledger-init`.
- `cms align "<goal>"` — the change-alignment gate (see §9). `--base <ref>` for
  branch/PR mode, `--scan` to refresh Sentinel first, `--json`. Subcommands:
  `status`, `history`. **Exits non-zero on a `drift` verdict.**

**Fuse (Constellation) — multi-project discovery**
- `cms fuse [ROOTS…] [--list] [--json]` — fuse ≥2 mapped projects into a
  cross-codebase report: integration opportunities, emergent features only
  possible in combination, and conflicts/overlaps — built from each project's
  existing memory (zero re-processing) plus deterministic structural overlap
  detection. Real provider required; only projects with positively recorded
  feature discovery are fused (others listed as excluded, with the reason).
  Report at `~/.cms/fusion/latest.md`; member `feature_set_hash`es are
  recorded so drift makes the report verifiably stale. LLM sections are
  labelled plan material — never ground truth.

**Scout — plan hunting & idea synthesis**
- `cms scout scan <dir>` — hunt every `*plan*.md` under a tree (junk pruned),
  card new/changed ones with a real provider: one deep description sentence,
  feature tags, goals, Atlas-candidate flag. Content-hash cached — unchanged
  plans are never re-charged.
- `cms scout review` — ONE call over all cards + the constellation registry:
  new idea concepts, cross-plan patterns pointing at goals, project pairings,
  Atlas-onboarding candidates. Suggestions persist with statuses
  (`proposed|accepted|rejected|ignored`); **rejected/ignored ideas are fed
  back as do-not-repropose and stay dismissed.**
- `cms scout list [--ideas]` / `cms scout status <id> <verdict>`.

**Scope / share**
- `cms scope show|set <paths…>|clear` — limit which subdirs/files get processed
  (persisted as `.cmsscope.json`); only selected paths are scanned + AI-summarised,
  saving API cost. Dirs end in `/`. Re-run `cms update` to apply.
- `cms bundle export [--source] [-o file]` — package the generated `.memory/`
  (optionally + a source snapshot) into a shareable `.cmsbundle`.
- `cms bundle open <file> [--dest] [--port]` — unpack a received bundle and view
  it — **no API key, no re-processing** (viewing needs only `graph.json`).
- `cms bundle info <file>` — show a bundle's manifest.

**Serve / integrate**
- `cms ui [--port 7717] [--no-browser]` — the memory viewer.
- `cms mcp` — run the MCP server (stdio) for agents. Auto-discovers the nearest
  mapped project: walks up from its launch dir (or `--root`) to the first
  ancestor holding `.memory/graph.json`, so one global MCP config entry serves
  every repo. In a repo with no memory layer it still serves — tools then
  return "no memory layer — run `cms run-all`" instead of the server dying.
- `cms config set <key> <value>` / `cms config show` / `cms config path`.

## 5. The knowledge graph (what the memory actually is)

A `networkx` DiGraph persisted at `.memory/graph.json`, summaries embedded in
nodes. **Node ids are structured** — use these forms directly with tools:

| id form | meaning |
|---|---|
| `file:<rel/path.py>` | a source file |
| `func:<path>::<qualname>` | a function/method |
| `class:<path>::<qualname>` | a class |
| `feature:<Name>` | a feature (declared or discovered) |
| `review:app`, `suggestions:app` | app-level review / suggestions rollups |

**Edge types:**
- `CONTAINS` — file → its components.
- `CALLS` — caller → callee (best-effort static resolution).
- `IMPORTS` — importing file → imported file.
- `PART_OF` — component → feature.
- `CONNECTS` — feature → feature, **declared** via `@memory:connects`.
- `RELATES` — feature → feature, **inferred** from code (a member of one
  imports/calls a member of another), each carrying a `via` reason.
- `CO_CHANGES` — file ↔ file that repeatedly change together in git with no
  import relationship (hidden coupling).

Query ranking is keyword + structure (name/anchor/summary/path matches with a
graph-degree boost) — not embedding-based yet. Structural parsing covers Python
(full AST) and TypeScript/JavaScript (lightweight: declarations + imports); other
files get summaries only.

## 6. Memory layer layout (`.memory/`)

```
.memory/
├── clean_tree.md / clean_tree.json   # junk-filtered file tree + per-file metadata
├── graph.json                        # the knowledge graph (summaries embedded)
├── index.md                          # what's here + how to query
├── summaries/                        # per-file markdown summaries, mirroring source layout
├── features/*.md                     # one feature trace per file
├── review.md                         # the AI alignment audit
├── suggestions.md                    # ROI-ranked plan
├── prompts/*.md                      # exported task briefs
├── semantic_state.json               # positive per-stage evidence: discovery/review/suggestions status, provider, hashes
├── notes.json                        # viewer annotations (quote-anchored)
├── activity.jsonl                    # every MCP tool call (drives UI pulses)
├── sentinel/                         # findings.json, scans.json, latest.json, reports/
└── align/                            # intent.json (active intent), latest.json, sessions.json (verdict history)
```

## 7. Memory anchors (how humans encode intent for you)

`# @memory:` comments are developer-curated intent the AST can't infer. They land
on graph nodes, enrich LLM prompts, and boost query ranking. **When you add
significant new code, add anchors so the memory stays honest:**

```python
# @memory:feature:UserAuthentication          # declares/attaches a feature
# @memory:connects:LoginFlow, TokenService     # declared feature links (CONNECTS edges)
# @memory:summary:Handles JWT issuance/refresh. # curated one-line intent
def login_user(...): ...

# === @memory:module:GraphLayer ===            # module-level tag (attaches to the file)
```

Line-form anchors attach to the next `def`/`class`; `module` tags attach to the
file. Only real comments count (anchor-like text in strings/docstrings is
ignored).

## 8. Hermes Sentinel — the quality gate

Built-in bug finding + completion gate (`cms sentinel`, MCP
`get_sentinel_report`, UI `/sentinel`). Seven modules run: `inventory`,
`static_risk`, `ledger` (audits `docs/feature_ledger.json` completion claims vs
graph evidence), `contracts` (UI↔HTTP↔MCP↔docs), `workflows` (end-to-end checks
against the real pipeline, incl. path-traversal + carry-over regression traps),
`domain_rules`, `providers`. Findings are **fingerprint-keyed** (survive line
shifts), with statuses `open | acknowledged | fixed_pending_verification |
resolved | false_positive` (a false-positive needs a reason) and auto
resolve/reopen regression tracking. Severities `critical | high | medium | low |
info`. The gate **fails on active criticals** (`sentinel.config.json` thresholds)
— `cms sentinel` exits non-zero. Full guide: `docs/HERMES_SENTINEL.md`.

Agent habit: after a change, `get_sentinel_report()` (or `check_alignment` which
folds in Sentinel findings on your changed files) before you claim done.

## 9. Change Alignment — did *this change* do what it was meant to?

The loop that closes intent → reality around a unit of work.
`declare_intent(goal)` captures the goal (explicit, else git branch / last
commit) and returns a grounded brief; after you edit, `check_alignment(base)`
diffs the change and fuses **impact** (blast radius + covering tests), **feature
review** verdicts, and **Sentinel** findings on changed files into one verdict:

- `aligned` — declared targets were touched, covered by tests, no active findings.
- `partial` — touched + covered but gaps remain (`unstated-change` scope creep,
  `intent-target-untouched`, or non-critical findings).
- `drift` — a touched feature is in drift, or a **critical** finding lands on a
  changed file. `cms align` exits non-zero here.
- `unverified` — no changes, changes unrelated to the intent, or **no covering
  test** (can't prove it landed).

Verdicts persist to `.memory/align/sessions.json` (trajectory history). Read
`gaps` and `tests_to_run` and act on them — that's the honest-finish contract.

## 10. Configuration

Config file `~/.cms/config.json` (secrets masked by `cms config show`); env vars
always override. Keys / env:

| key | env | default |
|---|---|---|
| `provider` | `CMS_PROVIDER` | anthropic if key present, else mock |
| `anthropic_api_key` | `ANTHROPIC_API_KEY` | — |
| `anthropic_model` | `CMS_ANTHROPIC_MODEL` | `claude-haiku-4-5-20251001` |
| `openai_api_key` | `CMS_OPENAI_API_KEY` / `OPENAI_API_KEY` | — |
| `openai_base_url` | `CMS_OPENAI_BASE_URL` | `http://localhost:11434/v1` |
| `openai_model` | `CMS_OPENAI_MODEL` | `llama3.1` |

Providers: **anthropic** (default when a key is set), **openai** (any
OpenAI-compatible endpoint — Ollama/LM Studio/xAI/OpenAI), **mock**
(deterministic, self-labelling structural summaries — automatic fallback so the
pipeline always runs, even offline). Mock output is explicitly labelled; never
present it as a real semantic summary.

**Semantic completion requires a real provider and positive evidence.**
`.memory/semantic_state.json` records, per stage (summaries / feature
discovery / review / suggestions), what ran, with which provider+model,
over which input hash, producing which output hash. Mock never writes
completion markers; provider failures record `failed` and retry after
inputs change; a legitimate zero-feature discovery IS recorded complete.
Judgment built before valid discovery (or without evidence) is invalid
and rebuilt automatically once; a valid judgment whose feature-set hash
drifted is frozen-stale — refresh via `cms review` / `cms suggest`.
Never infer a stage ran from node existence — read the state (UI shows
it; `GET /api/semantic` serves it).

## 11. Conventions & gotchas for agents

- **Memory before grep.** Query/summary/impact first; `get_source` for surgical
  reads; whole-file reads are a last resort.
- **Keep the memory honest after edits:** add `@memory` anchors to significant
  new code, then `cms update` (or rely on a running `cms watch`). Stale memory is
  itself flagged (Sentinel medium: "memory layer is stale").
- **Respect declared gaps:** check `get_review(feature)` gaps before touching a
  feature; don't reintroduce known issues.
- **Prove, don't claim:** run the tests `check_alignment`/`impact` name; a
  feature isn't "done" until `cms verify <Feature>` passes and Sentinel is clean.
- **The exe snapshots code + UI at build time.** After changing `cms/` or
  `ui_assets`, `CMS.exe` must be rebuilt or it serves stale behavior.
- Localhost-only UI; structural parsing = Python (full AST) + TS/JS (imports +
  declarations; no call/inherit edges yet), other files summary-only; call resolution is best-effort
  static; query ranking is keyword-based (embeddings are future work).

## 12. Quick recipes

- **Understand an unfamiliar area:** `query_codebase("<topic>")` →
  `get_file_summary(path)` → `get_source(path, a, b)`.
- **Plan a safe edit:** `get_impact("<target>")` (blast radius + tests) →
  `get_review("<feature>")` (respect gaps).
- **Do a change end-to-end:** `declare_intent("<goal>")` → edit →
  `check_alignment()` → run `tests_to_run` → fix any `gaps`/`drift` → re-check.
- **Pick the next task:** `get_suggestions()` (highest ROI first).
- **Change codebases mid-session:** `switch_project("C:/repos/other")` → if
  `memory: missing`, run the returned `cms run-all` command in a shell → query.
- **Gate a PR (human/CI):** `cms align "<goal>" --base main --scan` — non-zero on
  drift; `cms sentinel` — non-zero on active criticals.
