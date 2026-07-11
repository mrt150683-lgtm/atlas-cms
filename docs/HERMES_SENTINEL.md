# Hermes Sentinel

Hermes Sentinel is CMS's built-in bug-finding, feature-auditing,
workflow-verification and completion-gate system. It inspects the codebase the
memory layer describes and answers one question honestly: **is what we claim
built actually complete, wired, tested and consistent?**

A bug-finding system that cannot prove its own work is just another bug — so
every Sentinel check runs against real files, real imports, or the real
pipeline, and Sentinel's own logic is covered by `tests/test_sentinel.py` and
`tests/test_sentinel_ui.py`.

## Running a scan

```bash
cms sentinel                  # full scan + quality gate (exit 1 on active criticals)
cms sentinel run --json       # same, machine-readable
```

From the viewer (`cms ui` / `cms app`): open **/sentinel** (the "Sentinel"
link in the header) and press **Run scan**. Agents can read results through
the MCP tool `get_sentinel_report`.

Results persist under `.memory/sentinel/` (`findings.json`, `latest.json`,
`scans.json`, `reports/`) and survive restarts.

## The modules

| Module | What it checks |
|---|---|
| **inventory** (Project Scanner) | Real inventory: scanned files, live typer CLI commands, HTTP routes parsed from `cms/ui.py`, UI pages and the `/api/` calls they make, MCP tools, graph features with evidence counts. Nothing hardcoded. |
| **static_risk** (Static Risk Scanner) | Pattern sweep (TODO/FIXME/HACK, fake/force/bypass verbs, placeholder/dummy markers) classified by context — production `cms/` code ranks above tests and docs; Sentinel's own pattern vocabulary is info-level. Plus two AST passes: *trivial validators* — a function named like a check whose body is a bare `return True/False` (critical); and *weak path guards* — a traversal guard written as a `'..' in path` substring test on a path-typed variable, which is bypassable by absolute/encoded paths (high), flagged without executing. |
| **ledger** (Feature Ledger Auditor) | Audits `docs/feature_ledger.json` claims against evidence: listed files must exist, `complete` requires exercising tests (graph `exercised_by` or real test files) and no `drift` review verdict; graph features missing from the ledger are surfaced. |
| **contracts** (Contract Checker) | UI fetches vs handled HTTP routes (a dead button is an unhandled fetch), MCP tool schemas vs actual `MCPServer` method signatures, README command docs vs the live CLI. |
| **workflows** (Workflow Test Runner) | Executes the real pipeline end-to-end in throwaway fixture projects, plus negative checks: querying without memory must block, path traversal must be refused (a **payload family** — relative, backslash, absolute, resolved, percent-encoded — not just `../`, so a guard that only blocks `../` is caught), unknown MCP tools rejected, the activity log must stay bounded, mock output must be labelled — and the **carry-over check**: `exercised_by`/`review` on feature nodes must survive an incremental update (this repo's known silent-wipe regression). Unsupported workflows report `missing`, never a fake pass. |
| **domain_rules** (Domain Rule Validator) | CMS invariants: every summary carries provider provenance and mock output says it is mock; feature members exist in the graph; review verdicts stay in vocabulary and ROI = value/effort; the memory must not be stale; the UI server binds loopback only and keeps its traversal guard. |
| **providers** (Driver/Plugin Validator) | CMS's "driver" layer is the LLM provider protocol. Mapping from the generic driver spec: `get_identity → name`, `run_measurement → summarize(prompt, context)`, connect/disconnect → N/A (stateless HTTP), error simulation → the pipeline's provider-failure fallbacks. Live checks exercise the mock provider (deterministic, self-labelling); network providers are validated structurally only. |
| **reports** (Bug Report Generator) | Findings become structured bug reports (`bug_id`, severity, area, evidence, risk, likely cause, recommended fix, required regression test), exportable as Markdown/JSON. |
| **store** (persistence + Regression Tracker) | Findings are fingerprinted by *problem*, not by scan or line number, so statuses survive rescans. A resolved finding that is re-detected reopens automatically. |

## Severity and the quality gate

Severities: `critical` > `high` > `medium` > `low` > `info`. Classification is
contextual — a TODO in docs is info; a bypassable guard in production logic is
critical.

The gate is configured by `sentinel.config.json` in the repo root:

```json
{
  "fail_on": ["critical"],
  "warn_on": ["high", "medium"],
  "ignore_paths": []
}
```

`cms sentinel` exits non-zero when any **active** finding matches `fail_on` —
suitable for CI or a pre-release check. Resolved and false-positive findings
never count.

## Finding lifecycle

```text
open -> acknowledged -> fixed_pending_verification -> resolved
                                            \-> false_positive (reason required)
```

- Change a status: `cms sentinel status BUG-000012 acknowledged` or the
  dropdown on any finding in the UI.
- `false_positive` **requires a reason** (`--reason "..."`); the reason is
  stored and shown with the finding.
- A finding that stops being detected while its module ran clean is
  auto-resolved; if it comes back, it reopens with its original bug id
  (regression tracking).

## The feature ledger

`docs/feature_ledger.json` tracks per-feature completion:

```json
{
  "feature": "QueryEngine",
  "status": "complete",
  "evidence": { "files": ["cms/memory.py"], "tests": ["tests/test_query.py::..."],
                "api": [], "ui": [], "database": [".memory/graph.json"],
                "manual_verification": "" },
  "known_limitations": [],
  "last_verified": "2026-07-05"
}
```

`cms sentinel ledger-init` generates it from real graph evidence with
conservative statuses (`complete` only when tests verify the feature). Edit it
by hand as features evolve; the auditor keeps it honest.

## Execution modes

Every scan records its mode: **mock** (no real LLM configured) or **live**.
Workflow checks always run with the mock provider — deterministic and
network-free — and each check is labelled `mode: mock`. Mock results are never
presented as live output; the domain rules fail any mock artifact that does
not identify itself as mock.

## Adding a new check

1. Emit findings with `cms.sentinel.make_finding(module, severity, summary, …)`
   — pass `fingerprint_of=` when the summary contains unstable text (line
   numbers), so statuses persist across scans.
2. Domain rules: add a `_rule_*` function in `cms/sentinel/domain_rules.py`
   and call it from `check_domain_rules`.
3. Workflows: add a `_wf_*` function in `cms/sentinel/workflows.py` and
   register it in `CHECKS`. Raise `_Missing("…")` when the codebase doesn't
   support the workflow yet — it reports as missing instead of failing.
4. Cover it in `tests/test_sentinel.py`.

## CLI reference

```bash
cms sentinel                        # scan + gate
cms sentinel run [--json]           # explicit form
cms sentinel findings [-s critical] [--status open]
cms sentinel show BUG-000003
cms sentinel status BUG-000003 false_positive --reason "pattern registry"
cms sentinel export -f md|json      # .memory/sentinel/reports/
cms sentinel ledger-init [--overwrite]
```
