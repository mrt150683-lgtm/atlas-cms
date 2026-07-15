---
id: evaluation-driven-development
name: Develop Through Evaluations
type: skill
description: Turn fuzzy quality goals for AI systems, agents, prompts, automations, simulations, or complex features into contextual evaluations and an improvement loop. Load when defining 'good', comparing versions, building a golden set, designing graders, reducing regressions, or deciding whether an AI-enabled workflow is ready.
tags: [evals, quality, ai, golden-set, continuous-improvement]
---

# Develop Through Evaluations

Do not hope for great. Specify it, measure it under real conditions, and improve from the errors.

## Specify

1. State the workflow's purpose and affected user outcome in plain language.
2. Map the workflow end to end and identify important decisions.
3. Define success, unacceptable behavior, and severity at each step.
4. Build a small golden set from real or representative inputs. Include common cases, ambiguous cases, and rare costly failures.
5. Record expert judgments and rationale. Treat the set as living evidence, not immutable truth.

## Measure

Choose the strongest practical evaluator for each criterion:

- deterministic assertion for exact structure, invariants, permissions, and side effects;
- execution test for tool use and end state;
- human domain review for taste, nuance, and consequential judgment;
- model grader for scalable semantic review, regularly audited against human labels;
- outcome metric for real business or user impact.

Mirror production conditions: tool availability, data shape, latency, interruptions, permissions, long context, retries, and cost. Keep evaluation data separate from development examples where possible. Track uncertainty and inter-rater disagreement.

## Analyze errors

Create a failure taxonomy with frequency, severity, affected cohort, likely mechanism, and detectability. Inspect raw traces; aggregate scores can hide catastrophic minority failures or reward superficial formatting.

## Improve

Change one causal layer at a time when possible: requirements, prompt, context, tool contract, model, workflow, guardrail, UI, or evaluator. Re-run the full regression set, compare cost and latency, and add every genuinely new failure mode.

## Operate the flywheel

Sample real inputs and outcomes, route ambiguous or costly cases to experts, and feed validated judgments back into the golden set. Version the eval with the system it measures.

## Output

Provide the evaluation contract, dataset design, graders, thresholds, error taxonomy, results, limitations, and next experiment. Never let the system grade its own safety or correctness without independent checks.

## Method basis

Use the contextual-evaluation pattern: Specify, Measure, Improve; real-world conditions; expert-owned golden examples; error analysis; and continuous feedback.
