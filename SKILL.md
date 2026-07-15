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

## 3. MCP tools (31) — the primary agent surface

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
- `get_anchor_drift(target=None)` — deterministic per-anchor integrity check.
  Flags a summary that names a vanished code symbol and a declared feature link
  with no RELATES/CALLS/IMPORTS evidence. Omit `target` for the whole project.
- `get_review(feature=None)` — the alignment audit verdict(s):
  `aligned | partial | drift | unverified` with headline, expected-vs-built, and
  concrete gaps. Read the touched feature's gaps *before* editing it.
- `get_suggestions()` — the ROI-ranked plan of what's worth building next
  (value/effort, highest return first). Pick your next task by ROI.
- `get_sentinel_report(severity=None)` — latest quality-gate scan: gate status,
  workflow checks, and active findings (bugs/risks). Check this before claiming
  done.
- `export_task_prompt(task, as_json=False, assets=None)` — assemble a full,
  memory-grounded task brief (where to work, features involved, blast radius,
  related planned work, verification steps) for a described task. `assets`
  composes Library context into the brief (§12) and records the exact versions
  used.

**Discuss**
- `ask_codebase(question)` — plain-language Q&A over the WHOLE memory layer:
  flows, features, bugs, connections, and intent-vs-reality ("is Constellation
  fully aligned with the core idea behind it?"). Atlas assembles the evidence
  pack itself (ranked hits, matched feature traces + reviews, app review,
  Sentinel gate, pipeline state) and answers simply, citing features and
  path:lines. When the declared intent is missing it states what IS built and
  asks what the owner expects — it never invents intent. Conversational: the
  project transcript (`.memory/chat.jsonl`) feeds continuity. Real provider
  required. Use the granular tools when you need raw data; use this when the
  human (or you) wants a judgment explained in plain words.

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

**Annotate (structured, canonical-target)**
- `add_annotation(target, type, body, confidence=None, evidence=None,
  feature=None, supersedes=None)` — attach a typed annotation (observation,
  bug_suspicion, contradiction, question, intended_change, …) to any canonical
  object: node id, `edge:src|dst`, or `range:path#start-end`. Model authorship
  is provenance-stamped (client + provider/model) and **immutable** — correct a
  model claim by superseding it, never rewriting it.
- `list_annotations(target=None, feature=None, include_archived=False)` — read
  what humans and models have recorded about an object (the viewer's quote
  notes are merged in read-only). Check a feature's open contradictions and
  bug suspicions before editing it; archived/superseded stay out by default.

**Discover (the feature hunt)**
- `discover_feature(description)` — hunt the graph for behaviour the mapping
  may have missed. Returns a verdict (`already_covered | partial_overlap |
  new | not_found`), the overlapping existing features (deterministic
  name/member overlap always included, provenance-tagged), a candidate
  mapping with per-member roles (entry/core/support) and reasons, connections
  to existing features (`provenance: graph` when grounded in a real edge,
  `llm` when inferred), and an ordered step-by-step mechanism explanation.
  The complete feature catalog is used, headline verdicts are reconciled after
  invalid claims are removed, and explanation steps survive only when their
  backticked code references resolve to the evidence (`llm_grounded`).
  Check `existing` before proposing anything — never duplicate a mapped
  feature. Proposals are never auto-accepted; a human confirms/renames in the
  UI feature list, which makes it a durable discovered feature.

**Verify flows**
- `review_exact_flow(feature, force=False)` — the evidence-classified account
  of how a feature actually executes: static CALLS skeleton with per-step
  evidence (static edges + STEP-granular coverage: only tests executing that
  step's own lines count), plus (real provider) a step-by-step read of the
  actual source: inputs, outputs, side effects, async boundaries, error paths
  — each claim classified `proven | static | observed | inferred | intended`
  (`proven` is reserved for AST-exact facts; heuristic name-resolved edges are
  `static`). The overall status (`verified | partially_verified |
  differs_from_intent | insufficient_runtime_evidence | static_only |
  verification_failed`) is computed from evidence — `verified` requires every
  in-feature step's own lines exercised — and carries a `scope` (flows/steps
  reviewed vs traced) so it never reads wider than it is. The model can never
  assert `verified`. Cached per content hash; a drifted review is served
  flagged `stale`, never as current.

**Decide (versioned approved intent)**
- `propose_decision(title, behaviour, feature=None, constraints=None,
  prohibited=None, supersedes=None)` — propose a structured intended-behaviour
  statement. Once a human approves it in the UI the intent is **locked**:
  change means proposing a successor (`supersedes`), never editing, and a
  feature's approved intent can never be shadowed — approving a second,
  unlinked decision is refused. You cannot approve your own proposals —
  approval and closure/rejection are human-authority actions gated by a
  per-session code printed only to the terminal that launched Atlas (not
  merely absent from this tool surface). A successor must name the current
  approved predecessor in the same feature scope; cross-feature and stale
  supersession links are refused.
- `get_decisions(feature=None, active_only=True)` — the decision trail. The
  approved decision for a feature is the ground truth to implement and verify
  against; `check_alignment` reports it for touched features.

**Library (reusable agent-context assets)**
- `list_assets(type=None, tag=None, status=None, q=None)` — the Library: skills,
  strategies, preferences, constraints, behavioural modes, and profiles (composites that pin
  members by `id@N`). Find the right specialist context for the task instead of
  loading one oversized prompt.
- `get_asset(id, version=None)` — one asset in full: canonical agent-facing
  content, declared `requires`/`conflicts_with`, trust, scope, version history.
  The canonical content is what you follow.
- `propose_asset(name, type, description, content, id=None, tags=None,
  requires=None, conflicts_with=None)` — propose reusable knowledge worth
  keeping, as a **draft** (reuse an existing id to propose a revision — the
  published version is untouched). Drafts are stamped agent-authored and never
  enter any agent's context until a human publishes them. **You cannot publish**
  — that is human-only, like decision approval. To comment on an asset instead
  of changing it, use `add_annotation` with `target="asset:<id>"`.
- `record_asset_use(assets, task, outcome="unknown", ...)` — after real work,
  append exact resolved versions/hashes plus the actual model and available
  duration/token evidence. Agent effectiveness/efficiency scores are provisional.
- `get_asset_feedback(id=None)` — read recent and aggregate outcome evidence.
  Human ratings are reported separately from agent self-assessment.

**The alignment loop (intent → verdict)**
- `declare_intent(goal=None, assets=None)` — record what the current change is
  meant to do (if `goal` omitted, inferred from git branch / last commit).
  Returns a grounded brief and stores the active intent. Paths written literally
  in the goal are recorded as mandatory targets; semantic hits and blast-radius
  files are related context, not an instruction to edit every match. `assets`
  names the Library refs the change runs under — their exact versions are
  recorded in the intent, so the trail shows what you worked from. **Call this
  first.**
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
- `cms ask "<question>"` — the chat surface as a command: grounded
  plain-language answer, evidence features named (mirrors `ask_codebase`).

**Judge / verify**
- `cms drift [--json]` — high-confidence `@memory` anchor-drift gate; exits
  non-zero when stale summary symbols or unsupported declared links are found.
- `cms review [Feature]` — build/print the built-vs-expected alignment audit.
  Full real-provider refreshes are atomic: incomplete/malformed provider output
  records `failed`, exits non-zero, and preserves the last complete review.
- `cms suggest` — ROI-ranked plan of what to build next.
- `cms verify` — map tests → features via coverage contexts (`exercised_by`;
  coverage proves the tests *execute* the feature, not that behaviour is correct).
  `cms verify <Feature>` runs exactly the tests mapped as exercising that feature;
  coverage proves execution, not complete behavioural correctness.
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

**Brainstorm (Discovery UI tab)** — temperature-adjusted generation of ten
single-sentence NEW concepts per batch: unconstrained mode deliberately
avoids everything the builder already works on (projects, plans, fusion
items, past batches); project mode grounds the batch in one chosen
project's card instead. Like/dislike verdicts persist and steer every
following batch (liked = more such directions, disliked = never again).
Standing goals (revealed by clicking the Atlas logo 7× inside the tab)
are injected into every generation. State at `~/.cms/brainstorm/`; real
provider only; all output is LLM plan material.

**Library — reusable agent-context assets**
- `cms library list|show <id>` — browse assets (shadowing, trust and version
  marked); `cms library new <id> --type skill|strategy|preference|constraint`.
- **Drop a `name`+`description` markdown file into `skills/` and it is picked
  up** (filename = id, type defaults to `skill`); `cms library register <id>`
  adopts it, or publish it straight away. Unreadable files are listed with the
  reason, never silently skipped.
- `cms library publish <id> --by "<name>"` — freeze the draft as an immutable
  version. Human act: the MCP surface cannot publish.
- `cms library compose <ref…>` — preview the composed context for a selection
  (`id` or `id@N`; profiles expand): ordered content, warnings, conflicts, size.
- `cms library import <file>` / `export <id>` — markdown skill files in and out
  (imports land as drafts, trust `imported`).
- `cms library enable|disable|deprecate <id>` — disable never deletes; deprecate
  keeps pinned refs resolving. `cms library verify` re-hashes every snapshot.
- `cms prompt "<plan>" --asset <ref>` — compose assets into a task brief.

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
(full AST) and TypeScript/JavaScript (lightweight: declarations, imports, and
best-effort CALLS/`extends` INHERITS edges resolved through named imports —
provenance `heuristic`); other files get summaries only.

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
├── chat.jsonl                        # Ask-Atlas transcript (grounded Q&A, evidence nodes per answer)
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
`get_sentinel_report`, UI `/sentinel`). Eight modules run: `inventory`,
`static_risk`, `anchor_drift` (checks individual human-authored intent against
current source/graph evidence), `ledger` (audits `docs/feature_ledger.json` completion claims vs
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
`intent-target-untouched` applies only to a path explicitly written in the
goal. Conventional companion artifacts (tests, requested docs, CI workflows,
dependency/security policy, UI assets, and proof ledgers) are accepted only
when the goal or a related canonical file justifies them; unrelated changes
still produce `unstated-change`. `related_not_touched` is advisory context and
does not lower the verdict.

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
- Localhost-only UI; structural parsing = Python (full AST) + TS/JS (declarations,
  imports, heuristic call/extends edges via named-import resolution), other files
  summary-only; call resolution is best-effort static; query ranking is
  keyword-based (embeddings are future work).
- **The FINISHED contract**: `/api/semantic` exposes `pipeline.status` —
  `finished` (all stages positively complete; watcher just waits for changes),
  `in_progress` (stages remain; any build continues from the evidence), or
  `attention` (a stage failed; retries on input change or cooldown). Derived
  from stage evidence, never a stored flag.

## 12. The Library — reusable context, composed per task

Instead of one oversized prompt (or the same generic instructions for every
agent), Atlas keeps **assets**: `skill`, `strategy`, `preference`, `constraint`,
`mode` (a behavioural operating mode), and `profile` (a composite that references members by pinned `id@N` — it never
copies them). Canonical content is markdown with a small frontmatter block;
lifecycle state (versions, hashes, trust, enablement) lives in an `index.json`
beside it, and every published version is frozen under `.versions/<id>/vN.md`.

Three scopes layer, highest wins: **built-in** (Atlas's own `skills/`,
read-only) → **user** (`~/.cms/library/`) → **project** (`<repo>/skills/`).
The same id at a higher scope *shadows* the lower one — that is the override
mechanism (a project sharpening a user preference pack).

The folder is the interface: a markdown file with `name` + `description` (the
format skills are already written in) is a complete asset — the filename is the
id, the type defaults to `skill`, and unmodelled frontmatter is preserved. Files
that can't be read are surfaced with the reason, not skipped.

Composition (`export_task_prompt(assets=…)`, `declare_intent(assets=…)`,
`cms library compose`) expands profiles, walks `requires`, and **reports
conflicts rather than resolving them** — both sides land in your context with a
CONFLICT banner, and reconciling them deliberately is your job. Deduped,
ordered constraints → modes → preferences → strategies → skills, size-estimated, and
stamped with each asset's exact `{id, version, content_hash}` so the run is
reproducible.

Rules that bind you:
- **Published content is frozen.** Never edit a published asset; propose a
  revision (`propose_asset` with the existing id) and let a human publish it.
- **You cannot publish.** Drafts you create are agent-trust and invisible to
  other agents' context until a human approves them in the Library screen.
- **Comment beside, not inside:** `add_annotation(target="asset:<id>", …)` for
  observations, gaps, failure cases — never smuggle notes into canonical text.
- **Close the feedback loop:** after genuine use, call `record_asset_use` with
  the exact assets, outcome, model and available time/token evidence. Agent scores
  are provisional; user ratings in the Library remain separate. Consult
  `get_asset_feedback` when selecting between relevant assets.
- **Resolve package resources from their root.** Imported skill collections keep
  scripts/references/templates beside the source package. Licence and notice
  files are provenance-only excluded context, not working instructions.

## 13. Quick recipes

- **Understand an unfamiliar area:** `query_codebase("<topic>")` →
  `get_file_summary(path)` → `get_source(path, a, b)`.
- **Load the right context for a task:** `list_assets(q="react")` →
  `declare_intent("<goal>", assets=["atlas-default", "react-skill"])` — the
  brief comes back with those assets composed in and their versions recorded.
- **Explain something to the human (or sanity-check a feature):**
  `ask_codebase("is <Feature> doing what it's supposed to?")` — grounded
  intent-vs-reality answer in plain words, evidence named.
- **Plan a safe edit:** `get_impact("<target>")` (blast radius + tests) →
  `get_review("<feature>")` (respect gaps).
- **Do a change end-to-end:** `declare_intent("<goal>")` → edit →
  `check_alignment()` → run `tests_to_run` → fix any `gaps`/`drift` → re-check.
- **Pick the next task:** `get_suggestions()` (highest ROI first).
- **Change codebases mid-session:** `switch_project("C:/repos/other")` → if
  `memory: missing`, run the returned `cms run-all` command in a shell → query.
- **Gate a PR (human/CI):** `cms align "<goal>" --base main --scan` — non-zero on
  drift; `cms sentinel` — non-zero on active criticals.
