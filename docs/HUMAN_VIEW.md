# Human View, Annotations, Decisions & Flow Verification — developer guide

Implements `docs/Resolution-Human-View-feature.md` on the existing Atlas
architecture: one canonical graph, two presentation modes, and an
evidence-first comprehension layer. This doc covers the data model, cache
identities, invalidation rules, and rollback.

## The semantic pyramid

```
system:<Name>        (graph node, type=system)
  └─ component:<Name>  (type=component, PART_OF -> system)
       └─ feature:<Name>  (existing, PART_OF -> component)
            └─ func:/class: members (existing PART_OF -> feature)
                 └─ file: (via path), source (viewer)
```

- Built by `cms/hierarchy.py::ensure_hierarchy`, wired into
  `update.incremental_update` after feature building. **One LLM call** per
  `hierarchy_input_hash` change (= feature_set_hash + top-level dirs +
  schema version). The full spec is stored in the `hierarchy` stage record of
  `.memory/semantic_state.json`, so rebuilds re-apply it from durable state
  without a provider.
- Mock/no key: deterministic structural grouping (one component per top-level
  dir), `provenance="heuristic"`, visibly labelled, recorded `skipped` —
  never a completion marker.
- File→component membership is computed at projection time from the
  component's `dirs` list (deliberately not materialized as edges).
- `graph.json` carries graph-level `schema_version: 2` (additive; v1 readers
  ignore hierarchy nodes).

## Human View (UI)

Pure client-side projection in `cms/ui_assets/index.html` — the canonical
graph is never mutated; AI View (toggle off) is the exact pre-existing
rendering path.

- `#humanBtn` toggles; `#resRange` (0–5) maps to
  System/Component/Feature/Module/Function/Source (`RES_LEVELS`).
- `rebuildView()` is the single dispatch seam; res 0/1 use
  `buildPyramidView` (deterministic layout, physics skipped via `flatView()`),
  res 2 reuses `buildFeatureView`, res 3 `buildGraphView(new Set())`
  (imports only), res 4/5 spill the selection's feature member files open,
  res 5 additionally opens the source viewer.
- Selection maps across levels via `S.parentOf`-style chains
  (`humanParent`/`trailChain`/`projectSelection`); the `#trail` breadcrumb is
  clickable. Double-click descends one level.
- **`←`/`→` step the resolution** (`nudgeResolution`): left broader, right
  deeper, clamped to 0–5; the first press turns Human View on, and the popover
  flashes so the level you landed on is named. The keys are deliberately bound
  to *resolution*, never to the comprehension lens — the lens is a separate
  control with its own slider. Guards: ignored while typing in a field, while
  the file viewer or chat is open, and while the activity drawer is open (the
  `#resRange` slider handles its own arrows natively when focused, so the
  window handler skips focused inputs to avoid double-stepping).
- Persistence: `localStorage cms.human.on/.res`, URL `?human=1&res=N`.

## Human explanation cache — `.memory/explain.json`

- Key: `sha1(node_id | content_hash | PROMPT_VERSION)`.
- `content_hash` is dependency-aware per node type (file: mtime+summary;
  func/class: signature+lines+summary+file mtime; feature:
  members+narrative+review verdict; component/system: description + child
  hashes → changes cascade upward only).
- A stale entry simply never matches again; `prune_explanations` sweeps
  orphans on every update. One file change invalidates that file, its
  functions, its feature, and its ancestors — nothing else.
- Mock: node's stored text labelled "(structural…)", `real:false`, never cached.

## Structured annotations — `.memory/annotations.json`

`cms/annotations.py::AnnotationStore`. Targets are canonical: node ids,
`edge:src|dst`, `range:path#a-b`. Lifecycle
`open → under_review/accepted/rejected/resolved/archived/superseded`.
Model-authored bodies are immutable (edit = supersede; provenance stamped
from the MCP clientInfo + configured provider). Legacy viewer quote-notes are
merged read-only into `list()` (`legacy: true`) — one read surface, no
competing note system. Only `open/under_review/accepted` enter model context
(chat evidence packs, task prompts), capped and trimmed.

## Decisions — `.memory/decisions.json`

`cms/decisions.py::DecisionStore`. `proposed → approved` locks the `intent`
payload forever; change = `propose(supersedes=…)` + approval, which marks the
ancestor `superseded` (full chain kept for audit). A feature's approved intent
cannot be **shadowed**: approving a second decision that does not supersede
the current approved one is refused. A successor must name the current
approved predecessor in the same feature scope; cross-feature and stale links
are refused. Approval and closure/rejection require the per-session code
(printed only to the terminal that launched Atlas; env override
`CMS_APPROVAL_TOKEN` for tests) — the gate is a mechanism, not just tool-surface
omission; the MCP tools can propose and read, never approve or close.
Annotation/decision writes are transport-stamped server-side
(`author.via = "http" | "mcp"`), never caller-asserted. Consumers: `build_alignment`
(`approved_intent` for touched features), chat evidence, flow review prompt,
fidelity.

## Exact-flow review — feature-node attr `flow_review`

`cms/flowreview.py`. Static skeleton (existing traced `flows` + per-step
evidence: static CALLS edge with its provenance, plus STEP-granular coverage —
`verify.map_tests_to_features` writes `exercised_by` onto each func/class
node, so only tests executing a step's own lines count as that step's
evidence; feature-level tests that miss the step appear as honest `context`,
never coverage) is always available; a real provider adds per-step analysis
over bounded source reads (≤3 flows, ≤12 steps, ≤40 lines/step). Claims are
classified `proven/static/observed/inferred/intended` — `proven` is reserved
for AST-exact facts, heuristic name-resolved edges are `static`; the model
cannot upgrade a step to `observed` without step coverage nor to `proven`
without an AST-exact edge. `verified` status is **computed** (every step
statically traced + every in-feature step's own lines exercised) — never
asserted — and the stored review carries `scope` {flows_reviewed,
flows_traced, steps_reviewed, steps_truncated} shown beside the status.
Cache identity: step chain + member file mtimes + `exercised_by` + approved
decision id/version + prompt version; mismatch on read serves the stored
review flagged `stale: true`. Regeneration is explicit (`force`).

**Carry-over contract:** `flow_review` and `verify_result` are in the
carried-attr tuple in `update.incremental_update` and asserted by the
CRITICAL Sentinel workflow check `carry_over_preserves_verification`. Any new
durable feature-node attr must join both.

## Intent fidelity — computed, never stored

`cms/fidelity.py::intent_fidelity` — dimensions
(implemented / tests_present / tests_passing / approved_intent /
intent_match / open_contradictions / stale_evidence), each with a reason
string; overall `on_track / attention / insufficient_evidence`. Thin evidence
→ explicit `insufficient_evidence`, never an invented score.

## Feature discovery from description

`cms/feature_discovery.py`: `propose_feature` (intent-ranked hits + one LLM
mapping with per-member reasons; mock = hits only, clearly not-a-mapping).
The full feature catalog is both prompt context and validation allow-list;
verdicts are reconciled after invalid members/features are removed, shared
member ownership is preserved, and mechanism steps are kept only when every
backticked reference resolves to prompt evidence (`llm_grounded`).
`confirm_feature` (human): writes the discovered feature into the graph AND
appends it to the `features` stage's `discovered_features` in semantic state,
so `_features_from_state` re-injects it on every future update.

## Surfaces added

- HTTP: `/api/explain`, `/api/annotations(+/update,/archive)`,
  `/api/decisions(+/approve,/close)`, `/api/flowreview` (GET/POST),
  `/api/fidelity`, `/api/feature/discover`, `/api/feature/confirm`;
  `/api/meta` gained `flags`.
- MCP (19 → 25): `add_annotation`, `list_annotations`, `propose_decision`,
  `get_decisions`, `review_exact_flow`, `discover_feature`.
- CLI: `cms flow <Feature>`; `cms verify <Feature>` now persists
  `verify_result` on the feature node.

## Feature flags (rollback levers)

`CMS_HUMAN_VIEW / CMS_ANNOTATIONS / CMS_FLOW_REVIEW` = `0|false|off` disables
the surface at runtime (403 on endpoints, controls hidden via
`/api/meta.flags`). Defaults on.

## Rollback

Everything is additive:

- delete `.memory/annotations.json` / `decisions.json` / `explain.json` —
  those features reset, nothing else affected;
- hierarchy nodes, `flow_review`, `verify_result` are extra graph data v1
  readers ignore; remove `"hierarchy"` from `semantic_state.STAGES` to
  retire the stage;
- `cms update --full` regenerates all derived layers **except** decisions and
  annotations (deliberately durable).

Existing projects flip from FINISHED to `in_progress` until their next
`cms update` runs the hierarchy stage once (one cheap call) — honest,
self-healing.
