---
id: verify-before-done
name: Verify Before Done
type: strategy
description: Prove a change landed before claiming completion — declare intent, check alignment, run the named tests, close the gaps.
tags: [atlas, verification, alignment]
---

# Verify Before Done

A change is not done when the edit compiles — it is done when the evidence
says it did what it was meant to do.

1. `declare_intent("<goal>")` before you start: records what the change is
   meant to do and returns a grounded brief.
2. After editing, `check_alignment()` — one verdict
   (aligned / partial / drift / unverified) fusing the diff, feature reviews,
   blast radius, and Sentinel findings on your changed files.
3. Run exactly the tests it names (`tests_to_run`); fix any `gaps` and
   re-check. `unverified` means you cannot prove it landed — that is a stop
   sign, not a formality.
4. Check `get_sentinel_report()` before claiming done: the gate is failing on
   active criticals for a reason.

Report outcomes faithfully: if tests fail, say so with the output.
