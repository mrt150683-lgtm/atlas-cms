# Anchor Drift — Atlas self-hosting report

Date: 2026-07-14  
Provider used for the deterministic build and gates: `CMS_PROVIDER=mock`

## Outcome

Anchor Drift now checks developer-authored `@memory` intent at node level without
an LLM. It ships on all three Atlas surfaces:

- Agent: `cms drift [--json]` and MCP `get_anchor_drift(target=None)`.
- Human: `GET /api/drift` and a file/feature inspector card that says, in plain
  language, **“The stated intent here no longer matches the code.”**
- Memory/quality: the declared `AnchorDrift` feature plus fingerprint-keyed
  Sentinel findings from the new `anchor_drift` module.

The detector intentionally under-reports. It recognizes only backticked Python-like
identifiers and conservative `snake_case`/`CamelCase` tokens. Declared feature links
are accepted when Atlas has a `RELATES` edge, a member-level `CALLS`/`IMPORTS` edge
in either direction, or a shared member. Missing source is uncertainty and produces
no finding.

## Atlas call log

| Step | Atlas call and concrete result | Assessment | What I would have done without Atlas |
|---|---|---|---|
| Read the operating contract | Read repo `SKILL.md` and `README.md` before searching. | Accurate and useful. It established the intent → evidence → alignment loop and the three surfaces that had to stay consistent. | Read README, then grep for entry points and infer conventions manually. |
| Baseline build | `CMS_PROVIDER=mock cms run-all` mapped 128 files, summarized 128, traced 60 features, and enriched git history. | Accurate. It proved the offline path before implementation. | Run tests and inspect modules without knowing whether the memory layer itself was current. |
| Declare intent | `cms align --json "add anchor-drift detection across CLI/MCP/UI/Sentinel"` returned `partial` because pre-existing `.claude/settings.local.json` was outside the declared scope. | Honest. It did not pretend the dirty worktree was mine or aligned. | Record the goal only in notes and rely on a final diff review. |
| Conversational location | `cms ask "where are @memory anchors parsed and attached to graph nodes?"` failed: chat needs a real provider. | Accurate limitation, but unusable in the mandated mock workflow. | Use code search immediately. |
| Structural location | `cms query` ranked `build_graph` (`cms/graph_builder.py:217-406`) first and `parse_anchors` (`cms/anchors.py:82-112`) second, including calls/callers. | Accurate and high-value. This was faster and more grounded than a broad grep. | Search for `@memory`, `parse_anchors`, and graph node writes, then open several files. |
| Feature map | `cms features` listed 60 features. `cms trace MemoryAnchors` identified `parse_anchors` as the entry point and its three helper calls. | Accurate at the structural level. | Manually trace parser → graph builder → feature builder. |
| Blast radius | `cms impact cms/anchors.py::parse_anchors` returned 259 downstream items, 40+ production files, and most of the suite. | Safe but unhelpfully broad. It exposed that a core parser sits upstream of almost everything, but did not narrow the implementation plan. | Inspect direct imports/callers and select tests by surface. |
| Existing judgment | `cms review` under mock returned 60 `UNVERIFIED` verdicts; `MemoryAnchors` had 1 member, 3 flows, and initially 0 mapped tests. | Honest, not decision-making help. It correctly refused semantic judgment without a real provider. | Rely on tests and code review. |
| ROI suggestions | `cms suggest` prioritized coverage for AgentMemoryAccess/ActivityPulse/AppMode and hidden-coupling reviews. | Internally consistent but not a reason to replace the requested task. Anchor Drift fills a distinct missing intent-integrity layer. | Follow the task brief directly. |
| Surgical reads | Used query-provided ranges to read `graph_builder`, feature edge creation, CLI/MCP registrations, HTTP routing, inspector rendering, Sentinel runner, and contract checks. | Useful progressive disclosure. Atlas found the right regions; raw source was still required for exact behavior. | Grep each surface and open whole files. |
| Memory refresh | First post-build `cms update` scanned 132 files and traced 61 features, but reported `0 changed` and `0 re-summarized` even though new files had entered the graph. A later update correctly summarized the three newly edited adapter files. | Mixed. The topology updated, but the first change-accounting message was misleading and new-file summary behavior deserves investigation. | Rebuild the graph from scratch. |
| Real repo drift gate | First `cms drift --json` found 23 items, including my own unsupported `AnchorDrift → HermesSentinel` declaration. After adding honest feature membership to the MCP/UI/Sentinel adapters and updating memory, the final result was 22. | High-value. Atlas immediately caught drift in the feature I had just added; I fixed it before claiming completion. | I probably would have reviewed only the detector tests and missed the self-contradictory connection anchor. |
| Feature trace after build | `cms trace AnchorDrift` found the entry and helpers, but its checklist claimed findings have `location`, `reason`, and `evidence_gap` fields. They do not; the dataclass fields are `path`, `line`, `message`, and `evidence`. | Wrong in a material detail. The cached/generated narrative hallucinated an output contract despite a correct structural skeleton. | Read the dataclass and tests; no narrative layer to mislead me. |
| Execution mapping | `cms verify --refresh` ran all 321 tests under per-test coverage and mapped 7 tests to Anchor Drift. | Accurate and valuable, though slow (about 12 minutes for mapping on this host). | Run pytest and perhaps a conventional coverage report, without feature-level mapping. |
| Targeted proof | `cms verify AnchorDrift` ran the seven mapped tests; all passed. | Accurate, with the right caveat that coverage proves execution rather than behavioral completeness. | Run the known detector/MCP/UI/Sentinel tests manually. |
| Sentinel | `cms sentinel` ran inventory, static risk, anchor drift, ledger, contracts, workflows, domain rules, and providers. All workflows passed; 0 critical, 0 high, 1 medium, 21 low, 9 info. | Accurate. Contracts stayed consistent and the new detector became part of the persistent quality gate. | Run separate contract tests and inspect output formats manually. |
| Human UI | Launched `cms app --no-browser --port 7721 --provider mock`; `/api/meta` reported the live CodeCrawl project and `stale:false`. A real Edge render of `?file=cms/prompt_export.py` showed the red Anchor Integrity card, one mismatch, `summary-symbol-drift`, `content_hash`, and `cms/prompt_export.py:46`. | Accurate and legible. The user-facing wording explains the problem without requiring graph jargon. | Hit the API and inspect HTML/JS; visual layout confidence would be weaker. |

## Verification evidence

- Ordinary regression suite: **321 passed** in 408.83 seconds.
- Instrumented verification suite: **321 passed** in 554.57 seconds; reusable
  coverage mapping completed in 714.8 seconds.
- `cms verify AnchorDrift`: **7 passed**.
- New deterministic fixtures include a deliberately stale `old_handler` summary,
  an honest `current_handler` summary that must not flag, an unsupported feature
  link, and the inverse-link evidence case that must not flag.
- Sentinel regression proves a fingerprint-keyed finding appears while source is
  drifted and becomes `resolved` after the source matches again without rebuilding
  graph.json.
- `git diff --check`: clean.
- Final alignment result is recorded below after the report itself entered the diff.

## Genuine Anchor Drift findings in this repository

Final `cms drift --json` result: **22 high-confidence findings across 57 anchored
nodes** — 1 `summary-symbol-drift` and 21 `connect-without-evidence` findings.
The command exits 1 by design because it is a gate.

### Stale summary symbol

- `func:cms/prompt_export.py::_library_section` — the summary names
  `content_hash`, but that identifier is absent from the function’s current source
  (`cms/prompt_export.py:46`).

### Declared feature links with no current static evidence

- CodebaseChat → FeatureExpectationReview
- ComprehensionLens → CodebaseChat
- ExactFlowReview → FeatureVerification
- FeatureDiscoveryByDescription → ComprehensionLens
- FeatureTracing → KnowledgeGraphConstruction
- FeatureTracing → MemoryAnchors
- FeatureVerification → ImpactAnalysis
- GitHistoryLayer → KnowledgeGraphConstruction
- GitHistoryLayer → MemoryViewer
- HermesSentinel → FeatureVerification
- HumanViewResolution → ComprehensionLens
- HumanViewResolution → FeatureTracing
- ImpactAnalysis → FeatureTracing
- ImpactAnalysis → KnowledgeGraphConstruction
- IntentFidelity → FeatureExpectationReview
- IntentFidelity → FeatureVerification
- KnowledgeGraphConstruction → SummaryGenerator
- MemoryViewer → GitHistoryLayer
- QueryEngine → SummaryGenerator
- SummaryGenerator → KnowledgeGraphConstruction
- SummaryGenerator → QueryEngine

These are genuine findings under the specified deterministic rule: the declaration
exists and the current graph has no supporting static edge. They are not all proven
product bugs. Some may be valid conceptual/runtime relationships that Atlas cannot
see; that is why Sentinel records them as **low**, while the more concrete vanished
summary symbol is **medium**.

## Bugs and friction found

1. **Broken checked-out virtual environment.**  
   Repro: `.venv\Scripts\python.exe -m pip install -e ".[dev]"`.  
   Result: it tries to launch removed `C:\Users\banan\AppData\Local\Programs\Python\Python311\python.exe`.
   I used the bundled Python 3.12 runtime instead.

2. **`cms align` option ordering is surprising.**  
   Repro: `cms align "goal" --json`.  
   Result: `No such command '--json'`. `cms align --json "goal"` works. The help
   presents `--json` as a normal option, so accepting it after the optional goal
   would be less error-prone.

3. **The advertised `ask`-first flow cannot run in the offline/mock path.**  
   Repro with `CMS_PROVIDER=mock`: `cms ask "where are anchors parsed?"`.  
   Result: `codebase chat needs a real provider`. The structural query fallback is
   good, but the golden workflow should state this branch explicitly.

4. **Impact can saturate on foundational code.**  
   Repro: `cms impact cms/anchors.py::parse_anchors`.  
   Result: 259 downstream items and most tests. Accurate reachability is not the
   same as an actionable blast radius; direct vs transitive tiers would help.

5. **Feature trace narrative hallucinated a schema.**  
   Repro: `cms trace AnchorDrift` after refresh.  
   Result: checklist names nonexistent `location`, `reason`, and `evidence_gap`
   fields. Structural steps were mostly right. Narrative claims should be checked
   against graph/source symbols the same way discovery explanations are grounded.

6. **Incremental update accounting was confusing for new files.**  
   Repro: add the detector/tests/docs, then `CMS_PROVIDER=mock cms update`.  
   Result: scan grew from 128 to 132 files and AnchorDrift became feature 61, while
   the command reported `0 changed, 0 re-summarized`. New-file accounting and
   summary completeness should be explicit.

7. **Desktop browser automation was unavailable, but Atlas itself rendered.**  
   The Codex in-app browser backend was absent in this session, so I used installed
   Edge headlessly against the same live localhost server. This is environment
   friction, not an Atlas defect. The rendered result and `/api/drift` response
   agreed.

## Verdict: did Atlas earn its keep?

**Yes, but unevenly.** “Consult memory before grep” made the first discovery step
faster and materially safer: one query found the exact parser/attachment sites,
the contract checker identified every surface that had to agree, and the finished
detector caught an unsupported anchor in its own new feature. That last point is
the strongest evidence that Anchor Drift expresses Atlas’s thesis usefully at a
finer grain.

Atlas did not make the whole task faster. Mock mode removes semantic chat/review,
impact was too broad to guide edits, coverage mapping was expensive, and the new
feature trace invented fields. The best current workflow is therefore hybrid:
use Atlas for grounded location, topology, contracts, persistent evidence, and the
finish gate; then verify semantic claims against surgical source reads and tests.
Atlas earned trust when it stayed structural and evidence-backed, and lost trust
when a narrative crossed beyond that evidence.

## Final alignment

`cms align --scan --json "add anchor-drift detection across CLI/MCP/UI/Sentinel"`
returned **`partial`**, not `drift`. It recognized the detector, CLI, MCP,
Sentinel adapter/runner, tests, HTTP server, and existing trust-loop UI as touched
or justified, and reported Anchor Drift with 7 exercising tests. The remaining
scope gaps were `.claude/settings.local.json` (pre-existing user state) plus the
requested report and first-class docs (`CODEX_ATLAS_REPORT.md`, `README.md`,
`SKILL.md`), which the alignment heuristic did not infer from the concise goal.
Its changed-file findings were nine pre-existing info-level ledger omissions; no
critical/high finding landed on this change. This is an honest non-drift result,
but also shows that alignment's support-artifact justification does not yet treat
an explicitly requested build report and contract docs as naturally in-scope.
