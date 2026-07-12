# Atlas hands-on audit remediation

This ledger tracks every actionable item from the 2026-07-12 hands-on product audit.
An item is marked **complete** only after focused automated verification and its own
commit. Items that have not met that bar remain **open**.

| ID | Status | Item | Completion evidence |
|---|---|---|---|
| AR-001 | complete | Keep verification claims proportional to coverage evidence. | CLI, prompts, reviews, suggestions, Sentinel labels, and manuals now say mapped tests prove execution rather than complete behavioural correctness; 39 focused tests pass. |
| AR-002 | open | Prevent stale review and suggestion artifacts from presenting as current advice. | — |
| AR-003 | open | Detect and recover from a present-but-broken Python launcher/runtime. | — |
| AR-004 | open | Add observable progress and reduce avoidable cost in full coverage mapping. | — |
| AR-005 | open | Canonicalize duplicate AI-discovered features before they enter the graph. | — |
| AR-006 | open | Keep the large feature map responsive and make the graph an evidence view rather than a blocking navigation surface. | — |
| AR-007 | open | Distinguish current runtime provider, artifact provenance, freshness, and unavailable capabilities. | — |
| AR-008 | complete | Show mapped-test evidence and its limits in the feature inspector. | Feature inspector now shows mapped-test count, exact test IDs, empty state, refresh action, and the coverage-evidence limitation; UI integration tests pass. |
| AR-009 | open | Validate CLI commands emitted by Ask Atlas against the live command surface. | — |
| AR-010 | open | Reduce Sentinel information noise from detector definitions, documentation examples, and test fixtures. | — |
| AR-011 | open | Make the Query → Trace → Impact → Verify → Align trust loop the primary product hierarchy while keeping Discovery a separate strategic workspace. | — |
| AR-012 | open | Make MCP/activity access evidence explicit and auditable in the main UI. | — |

## Status rules

- **complete**: implementation, focused tests, and a dedicated commit exist.
- **open**: work remains or verification has not yet met the completion bar.
- No item is silently omitted; discoveries added during remediation receive a new ID.
