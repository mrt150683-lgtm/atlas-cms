---
id: adaptive-model-routing-mode
name: Adaptive Model Routing Mode
type: mode
description: Match model capability and cost to task complexity, uncertainty, consequence, and verification burden, escalating when evidence shows the current route is insufficient.
tags: [models, routing, cost, complexity, quality]
---

# Adaptive Model Routing Mode

Use when more than one model or reasoning tier is genuinely available.

## Route by need

Estimate four dimensions before routing:

- complexity: number and interaction of constraints;
- uncertainty: ambiguity, novelty, or missing evidence;
- consequence: cost of a subtle wrong answer;
- verification burden: how hard failure is to detect.

Use a lighter model for bounded transformations, deterministic extraction, familiar edits, and tasks with strong automatic checks. Use a stronger reasoning model for architecture, ambiguous multi-file diagnosis, safety-critical judgment, long-horizon planning, adversarial review, or synthesis across conflicting evidence.

## Escalation signals

Escalate when the model repeats failed approaches, misses cross-cutting constraints, produces inconsistent reasoning, cannot reconcile evidence, or when cheap verification is unavailable. De-escalate after the hard judgment is resolved and the remaining work is mechanical.

## Honesty and evidence

- Never claim a model was selected if the runtime cannot actually select it.
- Record the real model identifier when exposed by the provider.
- Compare outcomes, human ratings, elapsed time, and token use over repeated tasks.
- Optimise for reliable goal completion, not lowest token count or highest model tier in isolation.
