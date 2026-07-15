---
id: root-cause-bug-hunt
name: Hunt Root Causes, Not Symptoms
type: skill
description: Diagnose intermittent, cross-layer, stateful, performance, integration, or difficult-to-reproduce bugs. Load when a user reports broken behavior, flaky tests, stale state, a race, a regression, or asks to find the cause before fixing it.
tags: [bugs, diagnosis, root-cause, instrumentation, regression]
---

# Hunt Root Causes, Not Symptoms

Produce a falsifiable explanation that accounts for the observed behavior.

## Reproduce and bound

1. Preserve the exact symptom, environment, inputs, time, and last-known-good state.
2. Separate expected behavior from reported behavior.
3. Reproduce with the smallest real path available. Record negative results.
4. Bound the failure by version, platform, user, data shape, state transition, load, timing, and dependency.
5. Check recent changes and adjacent paths, but do not anchor on them.

## Form and test hypotheses

Build a short hypothesis table:

- proposed mechanism;
- evidence it predicts;
- observation that would falsify it;
- cheapest discriminating test;
- confidence.

Test high-information hypotheses first. Add targeted logging or probes at boundaries: input, transformation, persistence, async handoff, external call, render, and recovery.

## Look beyond the obvious

Check:

- stale caches and duplicated sources of truth;
- partial writes and retry re-entry;
- race and cancellation paths;
- time, locale, encoding, and identifier collisions;
- mock-versus-live divergence;
- permissions and sandbox differences;
- lifecycle paths that mint or mutate the same entity;
- errors swallowed by fallback behavior.

## Confirm

A root cause should explain the symptom, reproduction conditions, and why existing tests missed it. Where safe, create a failing regression test before fixing. If the request is diagnosis-only, stop at the evidence-backed cause and proposed validation.

## Output

Lead with cause and confidence, then reproduction, evidence, competing hypotheses rejected, blast radius, and the smallest proof of a future fix.
