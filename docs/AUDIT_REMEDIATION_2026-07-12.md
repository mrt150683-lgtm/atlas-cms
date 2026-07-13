# Atlas hands-on audit remediation

This ledger tracks every actionable item from the 2026-07-12 hands-on product audit.
An item is marked **complete** only after focused automated verification and its own
commit. Items that have not met that bar remain **open**.

| ID | Status | Item | Completion evidence |
|---|---|---|---|
| AR-001 | complete | Keep verification claims proportional to coverage evidence. | CLI, prompts, reviews, suggestions, Sentinel labels, and manuals now say mapped tests prove execution rather than complete behavioural correctness; 39 focused tests pass. |
| AR-002 | complete | Prevent stale review and suggestion artifacts from presenting as current advice. | Live pipeline status remains unfinished while judgments need refresh; stale review is labelled historical, invalid review is withheld, and stale suggestions are hidden in sidebar and overlay; semantic-state and UI tests pass. |
| AR-003 | complete | Detect and recover from a present-but-broken Python launcher/runtime. | `CMS.bat` import-probes an explicit override, project venv, `py -3.11`, and PATH Python; broken venvs are rejected with exact repair instructions; launcher contract tests pass. |
| AR-004 | complete | Add observable progress and reduce avoidable cost in full coverage mapping. | Full mapping streams pytest output, reports three timed stages, caches context evidence behind a source/test fingerprint, invalidates on change, and supports `--refresh`; cache/progress/CLI tests pass. |
| AR-005 | complete | Canonicalize duplicate AI-discovered features before they enter the graph. | Discovered features are compared against declared and previously accepted discoveries, structural synonyms collapse to a stable canonical node, aliases persist through graph/state and remain searchable; feature tests pass. |
| AR-006 | complete | Keep the large feature map responsive and make the graph an evidence view rather than a blocking navigation surface. | Deterministic multi-ring layout replaces force simulation; the overview draws every declared connection plus a stable inferred sample, focus reveals all incident evidence, idle drawing is throttled, and the UI contract test passes. |
| AR-007 | complete | Distinguish current runtime provider, artifact provenance, freshness, and unavailable capabilities. | Semantic API now reports durable artifact provenance separately from runtime provider; the header names runtime, loaded artifact providers/models, chat availability, stage timestamps, and freshness remains governed by live pipeline state; focused tests pass. |
| AR-008 | complete | Show mapped-test evidence and its limits in the feature inspector. | Feature inspector now shows mapped-test count, exact test IDs, empty state, refresh action, and the coverage-evidence limitation; UI integration tests pass. |
| AR-009 | complete | Validate CLI commands emitted by Ask Atlas against the live command surface. | Ask Atlas receives a live Click-derived CLI contract; every inline or shell-line `cms` command is parsed against that command tree before display or persistence, invalid suggestions are blocked with the correct help route, and chat tests cover valid and invented syntax. |
| AR-010 | complete | Reduce Sentinel information noise from detector definitions, documentation examples, and test fixtures. | Lexical risks now scan executable project source rather than reference text; docs, fixtures, and detector definitions no longer create active information findings, while Sentinel's own AST security checks remain active; focused tests cover all exclusions. |
| AR-011 | complete | Make the Query → Trace → Impact → Verify → Align trust loop the primary product hierarchy while keeping Discovery a separate strategic workspace. | The main workspace now has a persistent five-stage trust-loop bar with actionable query, trace, impact, verification, and alignment guidance; Discovery remains a distinct top-level strategic route; the UI contract test covers hierarchy and actions. |
| AR-012 | complete | Make MCP/activity access evidence explicit and auditable in the main UI. | The always-visible MCP activity control opens persistent history from `.memory/activity.jsonl`, showing exact timestamp, tool, memory level, label, and touched node IDs; entries navigate back to mapped evidence and live events update the panel; the UI contract test covers the audit surface. |

## Status rules

- **complete**: implementation, focused tests, and a dedicated commit exist.
- **open**: work remains or verification has not yet met the completion bar.
- No item is silently omitted; discoveries added during remediation receive a new ID.
