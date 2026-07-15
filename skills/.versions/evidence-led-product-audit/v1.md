---
id: evidence-led-product-audit
name: Run an Evidence-Led Product Audit
type: skill
description: Audit a product, feature, codebase, plan, or claim-heavy progress report against its intended outcome and real behavior. Load for deep reviews, concept-versus-implementation checks, feature-gap analysis, hands-on experimentation, readiness assessments, or any request asking whether something truly works rather than merely exists.
tags: [product, audit, evidence, experimentation, readiness]
---

# Run an Evidence-Led Product Audit

Preserve the ambition of the product while testing whether reality earns its claims.

## Establish the contract

Extract:

- intended user and job;
- promised outcome;
- explicit and implicit claims;
- non-negotiable constraints;
- what "working" and "world class" would look like.

Separate declared intent from your inference. Ask only when a missing choice would materially change the verdict.

## Build the evidence map

Use the cheapest trustworthy evidence first, then deepen where risk is highest:

1. approved decisions and product docs;
2. architecture and source paths;
3. tests, logs, stored state, and generated artifacts;
4. live or local behavior;
5. external claims and current primary sources.

Classify every material claim as:

- proven: directly demonstrated by deterministic evidence;
- observed: seen in a real run but not exhaustively established;
- static: supported by structure or source inspection;
- inferred: plausible synthesis;
- intended: stated goal without implementation proof;
- contradicted: evidence shows otherwise;
- unknown: not inspectable in the available environment.

## Experiment

Choose a small number of journeys that can falsify the product's strongest claims. Include a happy path, a recovery path, and one high-risk edge. Preserve raw evidence such as screenshots, traces, outputs, or commands where useful. Do not fix findings during a review-only request.

## Judge

Assess:

- value: does it solve the real job?
- completeness: which promised loops stop early?
- truth: are status and proof honest?
- usability: can the intended user succeed and recover?
- depth: is the behavior real or templated?
- operability: can it be monitored, maintained, and trusted?
- scope: does it work outside the easiest code path?
- leverage: what existing capability is underused?

## Deliver

Lead with the verdict. Then provide:

1. strongest demonstrated capabilities;
2. gaps ranked by user impact and risk;
3. evidence for each claim;
4. highest-leverage improvements, preserving original ambition;
5. experiments run and what remains unverified.

Never convert absent evidence into a positive claim.
