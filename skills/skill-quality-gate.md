---
id: skill-quality-gate
name: Gate Skill Quality
type: skill
description: Audit, test, and improve a Library asset or agent skill before publication or broad use. Load when reviewing a SKILL.md, comparing skill versions, checking trigger quality, reducing context bloat, validating composition, or deciding whether an imported or agent-authored asset is trustworthy enough to publish.
tags: [skills, quality-gate, evals, triggering, composition]
---

# Gate Skill Quality

A skill earns publication by producing repeatable leverage, not by sounding wise.

## Inspect

Check:

- identity: short unique id and accurate name;
- description: says what it does and concrete situations that should trigger it;
- procedure: adds non-obvious actions, decision rules, or resources;
- output: defines what a successful invocation leaves behind;
- scope: clear boundaries, permissions, and failure behavior;
- economy: no generic model knowledge, repeated motivation, or unused ceremony;
- portability: avoids assuming tools, paths, credentials, or runtimes unless declared;
- composition: dependencies are necessary, conflicts are symmetric, and content does not duplicate another asset;
- provenance: source, author, trust, version, and freshness are visible;
- safety: instructions cannot surprise the user or bypass human gates.

## Test

Create realistic should-trigger, should-not-trigger, and compositional prompts. Include near misses. For procedural skills, compare outputs with and without the skill on representative tasks. Evaluate:

- task success;
- evidence quality;
- user correction required;
- unsafe or unauthorized action;
- token and time cost;
- variance across cases;
- whether the skill overfits its examples.

Use deterministic assertions where possible and human judgment for taste or domain nuance. Inspect raw traces for wasted work.

## Decide

Classify:

- publish: clear incremental value and no critical gap;
- revise: useful but trigger, procedure, safety, or composition needs work;
- split: distinct jobs are entangled;
- merge: another asset already supplies the leverage;
- deprecate: stale, harmful, or consistently unused;
- quarantine: untrusted origin or surprising behavior.

## Output

Return evidence, score by dimension, blocking issues, proposed revision, eval cases, and publication verdict. Preserve published history by proposing a new version rather than rewriting it.
