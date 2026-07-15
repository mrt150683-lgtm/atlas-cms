---
id: mixture-of-experts-mode
name: Mixture of Experts Mode
type: mode
description: Route separable parts of a difficult problem to distinct expert perspectives, then reconcile their evidence into one accountable answer.
tags: [experts, routing, synthesis, critique, complex-tasks]
---

# Mixture of Experts Mode

Use when a problem contains genuinely different disciplines or benefits from independent challenge. A single model may simulate the roles; use multiple agents or models only when the runtime actually provides them and parallel work is worthwhile.

## Expert routing

1. Decompose by distinct expertise or evidence source, not by arbitrary equal chunks.
2. Assign each expert a bounded question, expected output, and proof standard.
3. Include a challenger when blind spots or optimistic assumptions are a major risk.
4. Keep one synthesis owner responsible for contradictions, duplicated assumptions, and the final decision.
5. Compare claims against shared ground truth. Preserve material disagreement instead of averaging it away.
6. Prefer fewer strong, orthogonal experts over a crowd producing correlated paraphrases.
7. End with one integrated result, confidence boundaries, and the tests or observations that could overturn it.

## Efficiency guardrail

Do not use an expert mixture for a bounded task with a clear deterministic path. Record which experts or models were actually used and whether the mixture improved quality enough to justify its cost.
