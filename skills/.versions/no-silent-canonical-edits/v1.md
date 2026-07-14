---
id: no-silent-canonical-edits
name: No Silent Canonical Edits
type: constraint
description: Never rewrite approved or model-authored canonical content in place — correct by superseding, so the audit trail survives.
tags: [atlas, trust, provenance]
---

# No Silent Canonical Edits

Canonical records are append-only ground truth:

- An approved decision's intent is immutable — propose a superseding decision
  instead of editing it.
- Model-authored annotation bodies are immutable — supersede them to correct
  a claim; the original stays visible in the chain.
- Published library assets are frozen per version — changes ship as a new
  published version, never as an in-place rewrite.
- Agents never approve or publish their own proposals: approval and
  publishing are human acts, gated outside the agent's reach.

If you find yourself wanting to edit history, the correct move is always the
same: write the successor, link it, and let a human accept it.
