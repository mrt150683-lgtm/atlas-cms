---
id: premortem-failure-mapping
name: Map Failure Before It Happens
type: strategy
description: Run a pre-mortem and structured failure-mode review before a consequential build, launch, migration, architecture choice, automation, simulation, or agent workflow. Load when success depends on hidden assumptions, multiple systems, long timelines, external providers, hardware, or irreversible decisions.
tags: [premortem, risk, failure-modes, resilience, planning]
---

# Map Failure Before It Happens

Assume the work has failed in a concrete future. Explain why without turning the exercise into vague pessimism.

## Procedure

1. Define the mission, time horizon, success measures, and unacceptable outcomes.
2. Write a short failure headline from the future: what users or operators experienced.
3. Generate causes independently across:
   - value and adoption;
   - requirements and misunderstood intent;
   - data and state;
   - people and operations;
   - dependencies, providers, hardware, and supply chain;
   - security, privacy, and permissions;
   - performance and scale;
   - recovery, observability, and maintenance;
   - incentives and second-order effects.
4. Convert causes into failure modes with:
   - trigger;
   - affected journey;
   - severity;
   - likelihood;
   - detectability;
   - earliest warning signal;
   - prevention;
   - containment or rollback;
   - owner or decision gate.
5. Identify correlated failures and single points of failure. Do not rank ten variants of the same root cause as ten risks.
6. Design the cheapest experiment or monitor that can expose each high-risk assumption early.
7. Revisit after major evidence, scope, or environment changes.

## Output

Provide a ranked failure map, early-warning dashboard, and pre-launch experiments. Mark what is evidence-backed versus speculative. A pre-mortem improves a plan; it does not prove the plan is unsafe.
