# PLAN.md — The Atlas Library

Implementation plan for a central library of reusable, versioned, inspectable assets
(skills, strategies, preferences, constraints, and composite **profiles**) that Atlas
composes into task-specific context for AI agents.

Status: **planning document — no production code implemented yet.**
Branch context: `codex/atlas-audit-remediation`. All file/line citations were verified
against the working tree on 2026-07-14.

---

## 1. Executive summary

Atlas gains a **Library**: first-class, versioned assets whose canonical content is
human-readable markdown-with-frontmatter, whose lifecycle state (draft/published,
versions, content hashes, trust, enable/disable) lives in a small per-scope
`index.json`, and whose composition into an agent's working context is a pure,
deterministic function that records exact `{id, version, content_hash}` provenance.

Three scopes layer over each other — built-in (shipped with Atlas), user
(`~/.cms/library/`), project (`<repo>/skills/`) — with project > user > built-in
precedence. Profiles reference member assets by pinned `id@version`, never copy them.
Dependencies and conflicts are declared explicitly and **warned about, never silently
resolved**. Publishing is human-only (session approval code over HTTP, terminal over
CLI); agents may propose drafts and attach notes via MCP but can never touch published
canonical content. Human views reuse the existing Comprehension Lens; provider-specific
rendering is a single adapter seam over a provider-neutral canonical form.

The design deliberately reuses Atlas's proven mechanisms rather than inventing new
ones: the `DecisionStore` approve/supersede lifecycle, the `AnnotationStore`
provenance/immutability rules, `semantic_state`'s hash-based derived staleness, the
`build_task_pack` assembly seam, the per-session approval token, and the Sentinel
contract checks that force every new surface to ship with its paired docs.

One new module (`cms/library.py`), one new UI page (`library.html`), one new CLI
sub-app (`cms library`), three new MCP tools plus one extended tool, delivered in four
independently reviewable phases.

## 2. Current architecture findings

Verified by direct inspection (three exploration passes over `cms/`, `tests/`,
`docs/`, `cms/ui_assets/`):

- **Greenfield.** There is no existing skill/asset/library subsystem. Repo-wide
  search for "skill", "asset", "library", "bundle" finds only incidental hits
  (`align.py:62` treats `skill.md` as a doc file; `bundle.py` is the memory archive).
  Root `SKILL.md` is the manually synced Atlas agent manual (copy workflow documented
  at `docs/ATLAS_OPERATIONS.md:65-72`); it is read by Sentinel contract checks, never
  loaded by runtime code. A `skills/` dir briefly existed with one untracked seed
  asset (`getting_rich.md`, flat `name:`/`description:` frontmatter — the intended
  format) but has since been removed; Phase 1 creates the dir with the seed
  built-ins.
- **Dependencies.** `pyproject.toml` runtime deps are only `networkx`, `pathspec`,
  `typer` — **no YAML library**, so frontmatter parsing must be hand-rolled (a strict
  `key: value` subset is sufficient and matches the Claude-skill format).
- **Naming collision.** `cms/bundle.py`, CLI group `cms bundle` (`cli.py:1076`), and
  `POST /api/bundle/export` (`ui.py:279`) already mean "portable `.cmsbundle` memory
  archive". The brief's composite asset type therefore gets a different name (§4).
- **Lifecycle prior art.**
  - `cms/decisions.py::DecisionStore` — proposed → approved → superseded; human-only
    `approve` (`:159`) requiring an identity and refusing to shadow an existing
    approved decision; `update_intent_guard` (`:226`) always raises: change means
    supersede, never edit.
  - `cms/annotations.py::AnnotationStore` — author kinds user/model/analyzer;
    model-authored bodies immutable (edit = supersede, `:255-264`); canonical targets
    via `normalize_target` (`:63`); `active_for_context` (`:150`) feeds only active
    records into model context.
  - `cms/feature_discovery.py` — LLM `propose_feature` never persists; only human
    `confirm_feature` writes canonical state; the MCP surface has a propose tool and
    deliberately no confirm tool.
  - `cms/semantic_state.py` — `SCHEMA_VERSION` stamping, sha256[:24] content hashes,
    staleness always **derived** (never stored), and `atomic_write_json` (`:70`) as
    the shared Windows-safe write helper.
- **Context assembly seam.** `cms/prompt_export.py::build_task_pack` (`:43-142`)
  assembles ranked code targets, features, blast radius, active annotations, related
  suggestions, a **hardcoded conventions list** (`:127-134`), and verification steps;
  `render_prompt` (`:145`) renders markdown; `export_prompt` (`:217`) writes
  `.memory/prompts/<slug>.{md,json}`. `cms/intent.py::capture_intent` reuses the pack
  and `cms/align.py::build_alignment` consumes it — anything embedded in the pack
  automatically reaches captured intents and the alignment gate.
- **Human views.** Two systems exist: the structural Human View resolution slider
  (client-only) and the **Comprehension Lens** (`cms/lens.py::LEVELS` —
  schoolchild/tech/uni/specialist/tldr/adhd; server-side `rewrite_batch` `:227` at
  `POST /api/lens`; cache `.memory/lens/<level>.json` keyed by text hash `:132-157`;
  persona levels need a real provider, `tldr`/`adhd` have deterministic fallbacks).
  `rewrite_batch` is generic over `{id, text}` items and works on asset text unchanged.
- **Human-authority gate.** Per-session `approval_token` (`ui.py:60`, printed only to
  the launching terminal at `serve` `:897`, `CMS_APPROVAL_TOKEN` env for tests),
  enforced in `_decisions_post` (`:771-783`); `author.via` is force-stamped
  server-side (`:652`, `:795`), never trusted from the caller. UI double-prompt UX at
  `index.html` `initIntentPanel` (`:3330-3350`).
- **Sentinel contracts** (`cms/sentinel/contracts.py`): UI fetches must have handlers
  and vice-versa (`:25-56`); MCP `TOOLS` entries must match same-named `MCPServer`
  methods and schemas (`:59-106`); README must document CLI commands (`:123-150`);
  README and SKILL.md must name every MCP tool and the SKILL "MCP tools (N)" count
  must equal `len(TOOLS)` (`:153-189`).
- **Gotchas.** Durable feature-node attributes must join the carry-over tuple in
  `cms/update.py` or incremental updates wipe them — the Library avoids graph-node
  storage entirely. Sentinel `static_risk` flags fake/force/bypass vocabulary in
  `cms/` docstrings. Windows console is cp1252: CLI output stays ASCII. Repo-root
  `skills/` is outside `packages = ["cms", "cms.sentinel"]`, so built-ins will not
  ship in a wheel or the PyInstaller exe without a packaging step (§26/§27).

**Assumptions / unconfirmed:** no token-accurate counting exists anywhere (verified:
char/line budgets only); SKILL.md has no generator (verified by grep + the documented
manual copy workflow).

## 3. Product scope

**In the MVP:** five asset types (skill, strategy, preference, constraint, profile);
markdown+frontmatter canonical files; per-scope index with versions, hashes, trust and
enablement; draft → published lifecycle with supersession and deprecation; explicit
dependencies and conflicts (warn only); three scopes with precedence and
disable-without-delete; manual selection plus profile expansion; composition into task
packs with exact version provenance; size estimation with an oversize warning; trust
badges; a Library UI page with lens views, inspection, publishing, and a compose
preview; search/filter; markdown import/export; a `cms library` CLI; three MCP tools
plus an `assets` parameter on `export_task_prompt`; agent notes and agent-created
drafts.

**Deferred:** goal/playbook/template types (registry is additive — one dict entry
each), automatic asset recommendation (manual → saved profiles → rules → AI-assisted,
in that order, later), usage scoring/analytics (the index leaves room; nothing blocks
it), marketplace/cross-user sharing, provider-specific adapters beyond the single
renderer seam, tokenizer-accurate counting, semver, autonomous publishing, automatic
conflict resolution, large-scale external syncing.

## 4. Recommended terminology

| Term | Meaning |
|---|---|
| **Asset** | One Library item: a markdown file + its index record. |
| **Library** | The whole system (stores, composition, surfaces). |
| **Profile** | The composite asset type: a pinned list of member assets. |
| **Scope** | Where an asset lives: `built-in`, `user`, `project`. |
| **Trust** | Who created it: `built-in`, `user`, `project`, `agent`, `imported`. |
| **Status** | Lifecycle: `draft`, `published`, `deprecated` (+ orthogonal `enabled`). |

"Profile" was chosen (owner decision) over the brief's "bundle" because `cms bundle` /
`.cmsbundle` / `/api/bundle/export` already mean the memory-archive export in the CLI,
docs, README and the agent-facing SKILL.md; a second "bundle" concept in the same
agent-read documentation is exactly the vocabulary drift Sentinel exists to catch.

## 5. Asset-type model

A data registry, not a class hierarchy, in `cms/library.py`:

```python
ASSET_TYPES = {
    "constraint": {"composite": False, "order": 0},
    "preference": {"composite": False, "order": 1},
    "strategy":   {"composite": False, "order": 2},
    "skill":      {"composite": False, "order": 3},
    "profile":    {"composite": True,  "order": None},  # renders members, not itself
}
```

`order` drives rendering (constraints first — rules before guidance). Adding
`goal`/`playbook`/`template` later is one dict entry plus optional render tweaks;
type-specific behaviour is looked up, never subclassed. This satisfies the brief's
"add more asset types later without redesign".

## 6. Minimum viable schema

**Frontmatter (canonical file):**

| Field | Required | Notes |
|---|---|---|
| `id` | yes | slug `[a-z0-9-]{3,64}`; unique per scope; same id across scopes = shadowing |
| `name` | yes | human title (Claude-skill compatible) |
| `type` | yes | one of `ASSET_TYPES` |
| `description` | yes | one-liner, used for lists/search/lens (Claude-skill compatible) |
| `tags` | no | list |
| `requires` | no | refs `id` or `id@N` |
| `conflicts_with` | no | refs `id` |
| `assets` | profiles only | pinned refs `id@N` (pin required — profiles reference, never copy) |

Body after the closing `---` fence = canonical agent-facing content, preserved
byte-for-byte.

**Index record (per scope, `index.json`):** `id`, `type`, `path` (relative), `status`,
`enabled`, `trust`, `current_version` (int or null), `versions: [{version,
content_hash (sha256[:24], the semantic_state convention), snapshot (relative path),
published_at, published_by}]`, `created_by {kind, identity, provider, model, via}`,
`created_at`, `updated_at`; plus top-level `overrides: {<id>: {"enabled": false}}`
(disabling assets inherited from lower-precedence scopes) and `schema_version: 1`.
Written with `atomic_write_json`, sorted keys/records for mergeable git diffs.

**Deferred fields from the brief and why:** quality score / usage history (needs
evaluation data that doesn't exist yet; index structure doesn't block adding them),
activation conditions (automatic selection is deferred), expected inputs/outputs and
evidence requirements (belongs with playbook/template types), supported
languages/frameworks (tags cover the MVP need), compatible/optional assets
(`requires`/`conflicts_with` are the smallest useful relation set), approval state
beyond status (the status + trust pair covers it), author/source (lives in
`created_by` + trust, not frontmatter), change history (the versions array is the
change history).

Frontmatter parser: hand-rolled and strict — `---` fences, flat `key: value`, lists as
`[a, b]` or comma-separated; unknown types and malformed ids fail with a clear error.
No YAML dependency added.

## 7. Scope and inheritance rules

```
Atlas repo skills/       -> built-in scope  (read-only at runtime; dev-editable source)
~/.cms/library/          -> user scope      (*.md + index.json + .versions/<id>/vN.md)
<project>/skills/        -> project scope   (*.md + index.json + .versions/<id>/vN.md)
```

- **Precedence: project > user > built-in.** For the same `id`, the
  highest-precedence enabled record wins; losers are reported in compose output as
  `shadowed` (informational, not a warning — shadowing is the designed override
  mechanism, e.g. a project sharpening a user preference pack).
- **Disable without deletion:** a scope's `overrides` map can set `enabled: false`
  for any asset id, including ones inherited from lower scopes. Disabled assets stay
  listable (dimmed in the UI) and their absence from composition produces a
  `disabled-dependency` warning if something requires them.
- **Inherited vs local display:** every asset in lists and compose output carries its
  `scope` chip; shadowing pairs are shown together in the UI.
- **Drift prevention:** projects are encouraged to *shadow* (same id, changed
  content) rather than fork under a new id; the UI shows shadowed pairs side by side
  so drift is visible. Version pinning (§8) is the mechanism for "stay on the old
  behaviour deliberately".
- Built-in dir resolved relative to the package with a `CMS_LIBRARY_BUILTIN` env
  override (mirrors existing flag conventions in `cms/config.py`). When the project
  root *is* the Atlas repo (its `skills/` doubles as the built-in source), the
  built-in store detects the identical directory and yields to the project store —
  no double-listing.
- `.versions/` joins `DEFAULT_IGNORE` in `cms/config.py` (~`:111`) so snapshots never
  enter the scanner; `skills/*.md` may keep being scanned (markdown is summary-only —
  harmless, and keeps the library visible in the map).

Task-level overrides (the brief's sixth level) are the `assets=[...]` selection passed
per task (§11) — explicit selection plus pins already covers "this task uses exactly
these"; no persistent task scope is needed in the MVP.

## 8. Versioning model

- **What creates a version:** publishing. `publish(id, published_by)` validates the
  file, normalises (canonical frontmatter order + body), computes `content_hash`,
  **refuses if the hash equals the current version's** (nothing changed), copies the
  file to `.versions/<id>/v<N+1>.md`, appends the version record, sets status
  `published`. Requires a human identity string, exactly like
  `DecisionStore.approve` (`decisions.py:159-162`).
- **Published versions are immutable.** Snapshots are never rewritten; the store
  exposes no mutation of version records; an `edit_published_guard` that always
  raises mirrors `update_intent_guard` (`decisions.py:226`) so the rule is
  discoverable in code. (Docstring wording: "published content is frozen; changes
  ship as a new version" — phrased to stay clear of Sentinel `static_risk`
  vocabulary triggers.)
- **Drafts vs published:** the working `.md` file is the draft surface, freely
  editable. `dirty = sha(file) != versions[-1].content_hash` — derived on read, never
  stored (the semantic_state idiom).
- **Referencing:** bare `id` resolves the latest published snapshot; `id@N` resolves
  that exact snapshot. Profiles must pin (`id@N`). Publishing a profile freezes its
  pin list.
- **Updates offered:** the UI marks profiles whose pins trail the latest published
  member versions ("update available"); accepting re-pins in a draft and requires
  re-publish. No auto-bumping.
- **Deprecated versions:** status flip; still resolvable when pinned; excluded from
  unpinned resolution with a `deprecated-dependency` warning.
- **Rollback:** publish a new version whose content is an old snapshot (one UI
  action: "restore v(N) as draft"), or pin `id@N` where consumed.
- **Change history:** the versions array + snapshot diffs, rendered in the asset
  inspector.
- **Reproducibility:** every agent run that used library context carries
  `{id, version, content_hash}` per asset in the exported pack, the captured intent,
  and the alignment record (§11) — behaviour is auditable and reproducible.
- **Integrity:** `verify_integrity(scope)` re-hashes snapshots against recorded
  hashes; surfaced in the UI and available to a later Sentinel module.

## 9. Dependency and conflict model

Declared explicitly in frontmatter: `requires` (hard dependencies; `id` or `id@N`)
and `conflicts_with` (symmetric incompatibility, either side may declare). No soft
"optional/compatible" relations in the MVP — tags and profiles cover grouping.

Resolution behaviour (inside `compose_context`, §11):

- Missing, disabled, deprecated, or unpublished dependencies produce **warnings** —
  the compose result is still returned; nothing is silently dropped or auto-added
  beyond the declared closure.
- Conflicts produce conflict entries `{a, b, declared_by}`; **both assets stay in the
  output** and the human (UI banner / CLI warning block / pack warning section)
  decides. Warn, never auto-resolve — per the brief.
- Version-pin clashes (two different pins for one id in the closure) warn; the pin
  nearest the explicit selection (BFS depth) wins and the loser is reported.
- Redundant combinations (same id reachable twice) dedupe silently by `(id, version)`.
- Cycles (profile including itself transitively, or `requires` loops) are cut with a
  `circular-reference` warning.

## 10. Composition and profile design

A profile's canonical file carries the pinned `assets:` list in frontmatter; its body
may explain *why* the composition exists and is rendered as a preamble. Profiles are
ordinary assets: same lifecycle, scoping, trust, versioning. Profiles may nest
(cycle-guarded). Example seed:

```markdown
---
id: atlas-default
name: Atlas Default
type: profile
assets: [atlas-conventions@1, memory-first@1, verify-before-done@1, no-silent-canonical-edits@1]
---
The baseline working agreement for any agent touching this codebase.
```

Saved profiles are the MVP's answer to "reusable bundles"; the frontend-specialist /
repository-investigator / QA-specialist examples from the brief are exactly this
shape. Bundles-in-first-version: **yes**, because a profile is just one more asset
type flowing through machinery that must exist anyway — the marginal cost is the
expansion step in `compose_context`.

## 11. Context-assembly pipeline

`compose_context(root, selection, *, include_drafts=False)` in `cms/library.py` — a
pure function over the three stores:

1. Merge scopes → effective record per id (after overrides) + `shadowed` list.
2. Expand profiles recursively (visited-set cycle guard → warning).
3. BFS the `requires` closure; unresolvable refs become warnings, never silent drops.
4. Apply version pins; clashes warn, nearest-to-selection pin wins.
5. Detect conflicts across the whole closure; keep both, report.
6. Dedupe by `(id, version)`; order by `ASSET_TYPES` order then id.
7. Estimate size: `est_chars = sum(len(content))`, `est_tokens ≈ chars / 4`,
   `oversized = est_chars > config.LIBRARY_WARN_CHARS` (24 000 proposed).

Returns `{assets: [{id, version, content_hash, scope, trust, type, name, content}],
shadowed, warnings, conflicts, est_chars, est_tokens, oversized}`.

**Integration:** `build_task_pack` (`prompt_export.py:43`) gains
`assets: list[str] | None = None`. When provided, the pack embeds
`pack["library"] = {assets (provenance fields only), warnings, conflicts, est_tokens}`
and `render_prompt` emits a `## Library context` section — grouped by type, each asset
headed `### [type] name (id@vN, scope, trust)` followed by its canonical body
verbatim, warnings/conflicts as a visible quoted block — placed before
`## Project conventions`. Because `capture_intent` reuses the pack and
`build_alignment` consumes the intent, **provenance flows into `.memory/prompts/*`,
`.memory/align/intent.json`, and alignment verdicts with no extra plumbing** — the
"record which asset versions an agent run used" requirement lands for free.

The hardcoded conventions list (`prompt_export.py:127-134`) stays in the MVP; Phase 3
ships the same text as built-in assets, and deleting the hardcoded list in favour of
an auto-attached `atlas-default` profile is a later, owner-approved flip (§27).

## 12. Token and caching strategy

- **No giant concatenation:** composition is structured (§11) — dedupe by identity,
  type-ordered sections, explicit size estimate, `oversized` warning surfaced in the
  UI compose drawer, CLI output, and the pack itself.
- **Estimation:** chars / 4 ≈ tokens. Consistent with the codebase's char-budget
  culture (`MAX_SOURCE_CHARS`, per-call `max_tokens`); a tokenizer dependency is not
  justified for a warning threshold.
- **Caching:** composition is deterministic and cheap (local file reads + pure
  resolution) — no compose cache needed; the published snapshots *are* the cache of
  canonical content. Lens rewrites of asset text are cached by the existing
  `.memory/lens/<level>.json` machinery automatically (keys are text hashes —
  unchanged assets never re-charge). Provider-side prompt caching benefits fall out
  of stable, deterministic section ordering: identical selections render
  byte-identical `## Library context` blocks.
- **Concise summaries:** the `description` field is the machine-readable summary;
  `list_assets`/`GET /api/library` return metadata + description only — full content
  loads only for selected assets.

## 13. Human abstraction-view integration

Reuse the Comprehension Lens wholesale — no new depth system. `library.html` marks
asset descriptions (and a body excerpt in the inspector) with `data-lens` and posts to
the existing `POST /api/lens` (`rewrite_batch` is already generic over `{id, text}`).

Mapping the brief's requested modes onto shipped levels: Glance → `tldr`, TLDR →
`adhd` (bullet form), Standard → raw canonical (the `default` lens), Detailed/Expert →
persona levels (`tech`/`uni`/`specialist`). The canonical body is always displayed
verbatim in the inspector and is never replaced by a lens rendering — canonical asset
→ selected lens → rendered explanation, exactly the principle the brief states. The
"what it does / when to use / requires / conflicts" explanation card is generated from
the *index record + frontmatter* (deterministic — no LLM needed), with the lens
applied only to prose.

## 14. Agent-generated proposal workflow

MVP capability (owner decision): **notes + drafts.**

- **Notes:** the existing annotation system, extended with an `asset:<id>` target in
  `normalize_target` (`annotations.py:63`). Agents attach observations, suggested
  changes, gaps, evidence, confidence via the existing MCP `add_annotation`; model
  bodies stay immutable (supersede-only); `active_for_context` keeps notes flowing
  into task packs. Zero new storage.
- **Drafts:** new MCP `propose_asset(name, type, description, content, tags?,
  requires?, conflicts_with?)` creates a **project-scope draft** with
  `trust="agent"`, `created_by` stamped exactly like annotations over MCP
  (`mcp.py:597-600`: kind=model, identity=clientInfo, provider, model, via="mcp").
  The response carries `next_step: "a human must review and publish this in the
  Library screen"` (the `propose_decision` precedent).
- **Proposed revisions:** an agent proposes a draft with the *same id* at project
  scope carrying changed content; the UI diffs it against the current published
  version; the human publishes (v+1) or discards. No separate change-request object.
- **No approval path for agents:** there is no publish/approve/status MCP tool
  (test-asserted, like the existing "no approve in TOOLS" test at
  `tests/test_mcp.py:287-302`), and HTTP publishing requires the session approval
  token an agent cannot see. Lifecycle: draft → human review (diff, lens, notes) →
  publish → deprecate/supersede — the brief's controlled lifecycle with
  validate/simulate deferred to a later phase.

## 15. UI and navigation proposal

A standalone page — `cms/ui_assets/library.html` at `GET /library` — following the
sentinel/setup/discovery pattern (`ui.py:107-115`), plus one nav entry in
`index.html` `#navPop` (`:885-889`). Dark-theme tokens copied from `index.html:8-22`
(`--bg #0d0d0d`, `--surface`, `--ink`, `--accent #3987e5`, `--radius 8px`); reuses the
established chip / popover / `.i-section` / approval double-prompt patterns. Not a
folder-of-files: a structured system of capabilities with lifecycle, trust and
relationships visible.

Layout:

- **Header:** back link, scope chips (All/Built-in/User/Project), type chips, status
  filter, search box, trust legend.
- **Left — asset list:** rows with name, type chip, `id@vN`, scope chip, trust chip
  (reusing the `.author-model` 🤖 styling for agent/imported), status dot, disabled
  dimming, "modified since publish" dot, shadowing indicator.
- **Right — inspector:** metadata card; `data-lens` description; canonical content in
  a `<pre>` (verbatim, always available); version history with hashes and restore;
  requires/conflicts chips linking to the referenced assets; annotations on
  `asset:<id>`; draft-vs-published diff for agent revisions; actions — New draft,
  Edit draft, **Publish** (approval-code double-prompt, `initIntentPanel` pattern
  `index.html:3330-3350`), **Deprecate** (token), Enable/Disable, Export, Import.
- **Compose drawer:** multi-select assets/profiles → live preview via
  `POST /api/library/compose`: ordered content, warnings/conflicts banner, size meter
  with `oversized` warning, "copy as markdown", and "export as task brief" (feeds
  `assets=` into the prompt exporter).

## 16. Search and organisation

Server-side filtering in `GET /api/library`: `type`, `tag`, `status`, `scope`, and
`q` (substring over id/name/description/tags). A linear scan over parsed
index+frontmatter is ample at the tens-of-assets scale; no search index or embeddings.
Draft/published and enabled/disabled views are status/scope filters, not separate
screens. Recently-used/favourites/most-used are deferred with the usage-analytics
work (§3) — the index leaves room. Missing-dependency and conflict discovery happen
in the compose drawer where they are actionable.

## 17. AI-provider integration

Canonical assets are provider-neutral markdown. The single rendering seam is
`render_assets(assets, flavor="markdown")` in `cms/library.py`; the MVP ships only the
markdown flavor (used by `render_prompt` and the compose drawer). Future flavors —
Claude-skill directory export, system-prompt block, Codex instruction file, MCP
resource — are additional branches behind the same seam and do not touch the
pipeline. Essential behaviour never lives in a provider-specific rendering (the brief's
requirement); the lens layer (LLM-dependent) is presentation-only and falls back per
the existing lens rules. Provider selection continues to flow through
`get_provider`/`provider_identity` (`providers.py:124-159`) untouched.

## 18. Multi-agent compatibility

- Any MCP client (Claude Code, Codex, future agents) consumes the same three tools
  and the same canonical assets; client identity is already stamped from the MCP
  handshake (`mcp.py:705-710`) into everything an agent writes.
- Different agents load different selections/profiles against the same project: each
  exported pack and captured intent records its own asset provenance, so "which agent
  used which assets, which versions, and why" is answerable from
  `.memory/prompts/*.json` and `.memory/align/` today, and the alignment history
  (`sessions.json`) gives the per-run trail.
- Conflicting instructions between agents surface because both agents' packs carry
  explicit conflict warnings from the same deterministic resolver.
- Handover: the next agent inherits assets by receiving the same selection (or
  profile id) — reference, not copy, so inheriting is one parameter. Full
  orchestration remains out of scope; nothing in the store or resolver assumes a
  single agent.

## 19. Security and trust model

- **Trust levels** are first-class and visible: `built-in`, `user`, `project`,
  `agent`, `imported` — chips in every list, inspector, and compose preview.
  Agent-generated and imported assets are **drafts** until a human publishes; drafts
  never enter composition unless `include_drafts=True` is explicitly passed (and the
  output labels them).
- **Prompt-injection containment:** imported/agent content is inert markdown — Atlas
  never executes asset content; it renders it into packs a human can inspect
  (`/api/library/compose` preview, exported prompt files) before any agent consumes
  it. Publishing is the trust boundary, and it is human-gated.
- **No traversal:** ids validated as slugs; asset/snapshot paths always derived from
  id, never taken from input (the `get_source`/bundle zip-slip guard tradition).
- **No self-modification:** published content immutable (§8); agents cannot publish;
  `via` transport stamped server-side (`ui.py:650-652` precedent) so provenance
  cannot be spoofed by callers.
- **Secrets:** assets are tracked files; the existing repo hygiene applies. A
  publish-time secret-pattern lint is a cheap later Sentinel module (deferred, noted
  in §27).
- **Read-only built-ins:** the built-in store rejects writes at runtime; overriding a
  built-in means shadowing it at user/project scope, which the UI displays as such.
- Project assets overriding safety constraints is visible by construction: shadowing
  is reported in every compose result.

## 20. Import and export strategy

Smallest useful interop, targeting the ecosystem the owner already uses:

- **Import:** Claude-skill-style markdown (frontmatter with at least
  `name`/`description`). Missing fields defaulted: `id = slug(name)`,
  `type = skill`, `trust = imported`, status draft. Body preserved byte-for-byte.
  Surfaces: UI import button (`POST /api/library/import`), `cms library import <file>
  --scope`.
- **Export:** regenerate frontmatter in canonical order + body — round-trip stable
  (test-enforced). Surfaces: UI download (`GET /api/library/export?id=`),
  `cms library export <id> [--out]`.
- The canonical internal format is not limited by the import format: extra Atlas
  fields simply don't survive export to plain Claude-skill form, and imports without
  Atlas fields get defaults. JSON/YAML asset import, repo rule files, and Atlas-native
  profile packs are deferred.

## 21. File-by-file implementation plan

Phases are independently reviewable; each lists MVP membership.

**New files**

| File | Phase | Purpose / contents |
|---|---|---|
| `cms/library.py` | 1 | Frontmatter parse/serialize; `LibraryStore(scope_dir, scope_name, read_only=False)`; `LibraryView(root)` merging built-in/user/project stores; `publish`, `deprecate`, `set_enabled`, `verify_integrity`, `edit_published_guard`; `compose_context`; `render_assets`; `import_asset`/`export_asset`; `ASSET_TYPES`. `@memory:feature:AtlasLibrary` anchors; ~450-550 lines. Types/interfaces: asset record dict per §6; compose result per §11. |
| `skills/atlas-conventions.md`, `skills/memory-first.md`, `skills/verify-before-done.md`, `skills/no-silent-canonical-edits.md`, `skills/atlas-default.md` | 1 | Seed built-ins (content sourced from `prompt_export.py:127-134` + SKILL.md doctrine); `atlas-default` is the seed profile. |
| `tests/test_library.py` | 1 | Full store/compose coverage (§24). |
| `cms/ui_assets/library.html` | 2 | The Library page (§15); ~600-800 lines, no framework, tokens from index.html. |

**Modified files**

| File | Phase | Change |
|---|---|---|
| `cms/config.py` | 1 | `LIBRARY_DIR_NAME = "skills"`, `LIBRARY_USER_DIR = ~/.cms/library`, `LIBRARY_WARN_CHARS = 24000`, built-in dir resolver + `CMS_LIBRARY_BUILTIN`; add `.versions/` to `DEFAULT_IGNORE` (~`:111`). |
| `cms/cli.py` | 1 | `library_app` Typer sub-app after the bundle group (`:1076-1115`): `list/show/new/publish --by/enable/disable/deprecate/compose/import/export`. ASCII-only output. |
| `README.md` | 1, 4 | `cms library` command docs (Phase 1, `_check_cli_docs`); MCP tool docs (Phase 4). |
| `cms/ui.py` | 2 | GET `/library` page route after `:113-115`; GET `/api/library`, `/api/library/asset`, `/api/library/export` in `do_GET`; POST `/api/library/asset`, `/publish`, `/status`, `/compose`, `/import` in `do_POST`; `_library_*` handlers near `_decisions_post` (`:771`), token gate copied from `:776-783`, `via:"http"` stamping from `:650-652`. Handlers thin (≤10 lines), logic in library.py. |
| `cms/ui_assets/index.html` | 2 | One `#navPop` entry linking `/library` (`:885-889`). |
| `cms/prompt_export.py` | 3 | `build_task_pack(assets=None)` + `pack["library"]` (`:43`, near `:117-142`); `render_prompt` `## Library context` section (`:145-214`); `export_prompt` passthrough (`:217`). |
| `cms/annotations.py` | 4 | `normalize_target` (`:63`): accept `asset:<id>` → `("asset:<id>", "asset")`. |
| `cms/mcp.py` | 4 | TOOLS + same-named methods: `list_assets(type?, tag?, status?)`, `get_asset(id, version?)`, `propose_asset(...)`; extend `export_task_prompt` schema/method with `assets`; `_touched_nodes` returns `[]` for asset tools (label = asset id). |
| `SKILL.md` | 4 | Document the three tools + Library section; bump "MCP tools (25)" → "(28)" (`_check_mcp_tool_docs`). |
| `tests/test_prompt_export.py` | 3 | Provenance + rendered section + warnings. |
| `tests/test_ui_server.py` | 2 | Routes, token gating, compose, import. |
| `tests/test_mcp.py` | 4 | Tool behaviour + no-publish-tool assertion. |
| `docs/ATLAS_OPERATIONS.md` | 2 | Library screen + daily commands table entry. |

**Data-flow changes:** selection → `compose_context` → pack `library` section →
prompts/intents/alignment (§11). **Migrations:** none — all storage is new, additive
files. **Dependencies:** none added.

## 22. Persistence changes

New, additive only:

- `<project>/skills/index.json`, `<project>/skills/.versions/<id>/vN.md` (tracked —
  lifecycle is a team artifact, like `docs/feature_ledger.json`).
- `~/.cms/library/*.md` + `index.json` + `.versions/` (per-user, the
  scout/brainstorm/fusion precedent).
- No database (Atlas has none), no `.memory/` schema changes, no graph-node storage,
  no `update.py` carry-over involvement, no changes to existing stores. Writes go
  through `atomic_write_json` (`semantic_state.py:70`).

## 23. API changes

**HTTP (all localhost, existing server):**

| Route | Method | Notes |
|---|---|---|
| `/library` | GET | serves `library.html` |
| `/api/library` | GET | list + filters (`type`,`tag`,`status`,`scope`,`q`); merged scopes, shadowing marked |
| `/api/library/asset?id=&version=` | GET | record + canonical content + versions + dirty flag |
| `/api/library/export?id=` | GET | canonical markdown download |
| `/api/library/asset` | POST | create/update draft (author `via:"http"` stamped server-side) |
| `/api/library/publish` | POST | **approval-token-gated** |
| `/api/library/status` | POST | deprecate (token-gated); enable/disable |
| `/api/library/compose` | POST | `{selection, include_drafts?}` → compose result |
| `/api/library/import` | POST | `{filename, content, scope}` → draft, `trust:"imported"` |

**MCP:** `list_assets`, `get_asset`, `propose_asset` (draft-only); `export_task_prompt`
gains optional `assets`. No publish/approve/status tools — deliberately absent.

**CLI:** `cms library list|show|new|publish|enable|disable|deprecate|compose|import|export`.

Every surface lands with its Sentinel contract obligations: UI fetches paired with
handlers, TOOLS paired with methods and README/SKILL docs, CLI documented in README.

## 24. Testing strategy

`tests/test_library.py` (tmp_path fixtures, per-file convention — no conftest):

- Frontmatter round-trip (lists, odd whitespace); invalid type/id rejected; traversal
  ids rejected.
- Draft create/edit; **publish freezes** (snapshot written, hash recorded); publish of
  unchanged content refused; edit-after-publish → dirty derived; publish v2 leaves v1
  snapshot untouched; pinned `id@1` still resolves v1 content.
- Deprecate: excluded unpinned (warning), resolvable pinned. Disable via overrides.
- **Precedence:** same id at project+user+built-in → project wins, shadowed reported.
- Requires closure incl. transitive; missing/disabled/deprecated dep → warning, never
  a silent drop. Conflicts in either direction → conflict entry, both assets present.
- Profile expansion, nested profiles, cycle warning, version-pin clash, oversized flag.
- Import of `name`/`description`-only file → defaults + `trust:"imported"`;
  export→import round-trip byte-stable body. `verify_integrity` catches a tampered
  snapshot.

Extensions: `test_prompt_export.py` — pack with `assets=` carries
id/version/content_hash provenance and the rendered section; warnings appear in
markdown. `test_ui_server.py` — `/library` 200; list/filter; publish 403 without
token / 200 with `CMS_APPROVAL_TOKEN`; status change; compose preview; import trust.
`test_mcp.py` — three tools respond; proposed draft has `created_by.kind == "model"`
and `via == "mcp"`; **no publish-like name in TOOLS**; `export_task_prompt` with
assets carries provenance. `cms sentinel` runs after each phase — the contract checks
are free integration tests for every surface touched.

## 25. Incremental rollout

1. **Phase 1 — core store + CLI** (useful alone): `cms/library.py`, config constants,
   seed built-ins, `cms library` sub-app, `tests/test_library.py`, README command
   docs.
2. **Phase 2 — inspection UI**: `library.html`, ui.py routes, nav entry, lens
   integration, token-gated publish/deprecate, UI tests, operations doc.
3. **Phase 3 — composition**: `prompt_export` changes, provenance through
   intent/align, conventions-as-assets dogfood on this repo.
4. **Phase 4 — agent surface**: MCP tools, `asset:` annotation target, SKILL/README
   docs + tool-count bump, MCP tests.

Each phase leaves the system consistent and Sentinel-clean; later phases can be
deferred indefinitely without stranding earlier ones.

## 26. Risks and trade-offs

- **Packaging of built-ins:** repo-root `skills/` ships in neither the wheel
  (`packages = ["cms", "cms.sentinel"]`) nor the PyInstaller exe. Mitigation options:
  copy into `cms/library_assets/` as package data at build time (recommended), or
  accept source-checkout-only built-ins for the MVP. The exe additionally snapshots
  code at build time — an exe rebuild is required either way.
- **Hand-rolled frontmatter parser:** kept deliberately tiny and strict-failing;
  lightly fuzzed in tests. Trade-off accepted to avoid a new dependency.
- **Tracked `index.json` merge conflicts** on teams: mitigated by stable ordering and
  indentation; residual risk accepted for MVP.
- **Growth of `ui.py` (906 lines) and `index.html` (4227 lines):** all logic lives in
  `library.py`; HTTP handlers stay thin; the Library UI is a separate page.
- **Project scope shares `skills/` with potential unrelated user content** (e.g.
  hand-written agent skills): the index only governs files it registers; unregistered
  `.md` files are listed as "unregistered" with one-click registration (see §27 Q2).
- **Sentinel `static_risk` docstring vocabulary** and **cp1252 CLI output**: known
  house rules, called out per file above.
- **Terminology:** "profile" decided; revisiting it after Phase 1 would touch CLI,
  API, docs — decided now precisely to avoid that churn.

## 27. Open questions

1. **Auto-attach `atlas-default`** to every task pack (replacing the hardcoded
   conventions list) once Phase 3 lands — recommended: opt-in first, flip after
   dogfooding on this repo. Owner call.
2. **Unregistered markdown in `skills/`:** auto-import as drafts, or list as
   "unregistered" with one-click register (recommended — never silently adopt
   content into the library)?
3. **Packaging strategy for built-ins** (§26) — package-data copy vs
   source-checkout-only for now.
4. **Is disable team-tracked** (project `index.json`, recommended default) or
   per-machine (would need a `.memory/` overlay)? MVP implements team-tracked only.
5. **Secret-pattern lint at publish time** as a Sentinel module — worth scheduling
   after Phase 2?

## 28. Definition of done

The initial Library implementation is complete when:

- Users can create, inspect, edit, publish, version, deprecate, disable, import and
  export assets via UI and CLI; built-ins are read-only.
- Canonical content is always displayed verbatim and is never replaced by lens
  renderings; lens views work on asset prose.
- Skill, strategy, preference, constraint and profile types exist; profiles reference
  members by pinned id@version, never copy.
- Assets declare `requires`/`conflicts_with`; composition warns on missing deps and
  conflicts and never auto-resolves; shadowing and precedence behave per §7
  (test-proven).
- Tasks can compose selected assets/profiles; the resolved context is inspectable
  (compose drawer / CLI / exported pack) **before** any agent consumes it; packs,
  intents and alignment records carry exact `{id, version, content_hash}` provenance.
- Agent notes and drafts never modify published canonical content; publishing is
  impossible over MCP and token-gated over HTTP (test-proven).
- Trust levels are visibly distinguished everywhere assets appear.
- The design remains provider-neutral; rendering goes through the single
  `render_assets` seam.
- Size is controlled through structured assembly, dedupe, and the oversize warning;
  lens caching prevents re-charging for unchanged asset text.
- `pytest tests/` is green including all new suites; `cms sentinel` reports no new
  contract findings (routes paired, tools documented, SKILL count correct, CLI
  documented).
- Documentation (README, SKILL.md, docs/ATLAS_OPERATIONS.md) explains the asset
  format, how to add new asset types (one `ASSET_TYPES` entry + optional render
  tweak), and how future agents integrate via the MCP tools.
