---
id: long-horizon-simulation-qa
name: Test Long-Horizon Simulations
type: skill
description: Test simulations, agent societies, games, digital twins, event engines, and evolving worlds across many seeds and long time horizons. Load for determinism checks, emergent behavior, invariant testing, save/resume correctness, economy or population balance, duplicate entities, lifecycle bugs, or simulation realism.
tags: [simulation, long-horizon, determinism, invariants, emergence]
---

# Test Long-Horizon Simulations

A simulation can look convincing for ten minutes and be structurally broken by day three hundred.

## Define truth

List:

- deterministic inputs: seed, configuration, version, initial state;
- invariants that must never break;
- conservation rules and bounded quantities;
- lifecycle transitions;
- intended emergent patterns without prescribing exact stories;
- unacceptable absorbing states, runaway loops, or silent dead worlds.

## Build the harness

Run a matrix across seeds, configurations, populations, map sizes, and time horizons. Make execution headless and reproducible where possible. Capture event ledgers, periodic state hashes, key metrics, exceptions, and performance.

Test:

- same seed and inputs produce the same ledger or documented equivalence;
- save, close, resume matches uninterrupted execution;
- migration and version changes preserve or intentionally transform state;
- every entity-creation and deletion path maintains identity invariants;
- retries and partial ticks do not double-apply events;
- no impossible values, orphaned references, duplicate live identities, or time reversal;
- systems remain active and varied without unbounded growth, collapse, or repetition;
- AI-generated content does not overwrite deterministic mechanics or fabricate state;
- extreme and sparse configurations fail gracefully;
- rendering is a faithful view of engine state.

## Examine emergence

Use distributions and traces, not one anecdote. Compare diversity, repetition, inequality, network shape, churn, resource flow, conflict, recovery, and narrative coherence. Investigate outlier seeds and long quiet periods.

Separate:

- mechanical correctness;
- statistical plausibility;
- experiential interest;
- value judgment about the simulated society.

## Minimize and regress

When an invariant fails, capture seed, tick, prior snapshot, event chain, and smallest configuration. Add the failure to a permanent seed corpus.

## Output

Report run matrix, invariants, determinism result, long-horizon metrics, outlier traces, minimized failures, and what remains a subjective realism question.
