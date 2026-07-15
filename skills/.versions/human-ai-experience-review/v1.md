---
id: human-ai-experience-review
name: Review Human-AI Experience
type: skill
description: Review or design the human experience of AI-assisted and agentic products. Load when an interface predicts, recommends, adapts, remembers, acts through tools, explains itself, fails probabilistically, or needs user trust, correction, feedback, and control.
tags: [human-ai, ux, trust, control, explainability]
---

# Review Human-AI Experience

Evaluate the whole interaction contract, not only output quality.

## Review by moment

### Before and at first use

- Make capabilities and important limitations concrete.
- Show what evidence, data, permissions, and external effects are involved.
- Calibrate expectations with representative examples, including normal failure.

### During interaction

- Use context only when relevant and permitted.
- Make agent activity, source material, uncertainty, and consequential tool use inspectable.
- Match timing and interruption to the user's task.
- Preserve a clear boundary between suggestion, draft, approval, and executed action.

### When wrong

- Make invocation, dismissal, correction, undo, and recovery efficient.
- Scope down or ask when ambiguity changes the outcome or risk.
- Explain why in terms the user can act on: inputs, rules, evidence, and limits.
- Never hide a real failure behind confident prose or a silent fallback.

### Over time

- Show what is remembered and let the user edit, forget, or globally control it.
- Learn gradually from behavior; distinguish inferred preference from confirmed preference.
- Adapt cautiously, expose consequences, request granular feedback, and notify meaningful behavior changes.

## Exercise the design

Test at least:

- confident correct;
- confident wrong;
- uncertain;
- missing context;
- conflicting user instruction;
- stale memory;
- revoked permission;
- partial tool failure;
- user correction and later reuse of that correction.

## Output

Return strengths, violations, affected journey, severity, evidence, and a concrete interaction pattern for each improvement. Mark trade-offs rather than treating guidelines as a mechanical checklist.

## Method basis

Use Microsoft's evidence-based Human-AI Interaction guidelines and HAX patterns as the baseline, extended for modern tool-using agents with visible activity, approval boundaries, and memory control.
