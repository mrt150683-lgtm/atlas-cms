---
id: adversarial-edge-case-qa
name: Probe Adversarial Edge Cases
type: skill
description: Design and execute high-yield QA beyond the happy path for applications, APIs, agents, workflows, imports, state machines, and user interfaces. Load for bug hunting, release testing, regression planning, destructive testing, edge-case generation, or when existing tests are shallow.
tags: [qa, edge-cases, adversarial, state-machines, regression]
---

# Probe Adversarial Edge Cases

Attack assumptions, boundaries, and transitions while keeping the environment safe.

## Model the test surface

Map actors, inputs, states, transitions, trust boundaries, persistence, dependencies, outputs, and recovery actions. Identify invariants that must remain true.

## Generate cases

Cover high-yield families:

- empty, minimum, maximum, malformed, duplicate, reordered, and Unicode inputs;
- first run, repeat run, retry, cancel, timeout, crash, restart, and resume;
- concurrent action, double submit, stale tab, clock skew, and out-of-order response;
- missing, revoked, excessive, or cross-user permissions;
- partial provider response, quota, disconnect, slow response, and corrupt cache;
- old schema, migration, imported data, path/identifier collision, and rollback;
- keyboard, focus, reduced motion, zoom, narrow viewport, and assistive-label paths;
- hostile content crossing a parser, renderer, model, tool, or external boundary.

Use pairwise combinations for broad coverage, then full combinations around catastrophic risks.

## Execute safely

Prefer isolated fixtures and reversible actions. Record setup, exact input, actual result, expected invariant, and artifacts. Distinguish deterministic failures from flakiness. Minimize a failing case before reporting it.

## Prioritize

Rank by user impact, exploitability, reach, likelihood, detectability, and recovery cost. A rare destructive failure can outrank a common cosmetic one.

## Output

Return a compact risk matrix, reproducible cases, invariant violations, coverage gaps, and regression tests to add. Do not equate number of cases with quality.
