---
id: atlas-frontend-specialist
name: Atlas Frontend Specialist
type: profile
description: The working context for a UI task in this repo — design craft, the owner's interface taste, the repo's hard invariants, and the house conventions, composed into one specialist. Load this for any task that builds, restyles, or reviews a screen in the Atlas viewer.
tags: [frontend, ui, atlas, specialist]
assets: [frontend-design@1, ui-preferences@1, atlas-repo-invariants@1, atlas-conventions@1, no-silent-canonical-edits@1]
---

# Atlas Frontend Specialist

The specialist configuration for interface work in this codebase. It assembles
four things an agent would otherwise have to be told again every time:

- **Frontend Design** — how to make deliberate visual choices instead of
  templated defaults.
- **UI Preferences** — what the owner will actually accept: dark, high-contrast,
  calm, premium, low clutter.
- **Atlas Repo Invariants** — the hard rules (no new dependencies, a UI fetch
  needs a live handler, ASCII CLI output).
- **Atlas Conventions** + **No Silent Canonical Edits** — the house habits and
  the append-only discipline.

Design craft and personal taste stay separate assets on purpose: the same
preferences travel to a different framework, and the same design skill serves a
different owner. This profile is the pairing, not a merge — the members are
referenced at pinned versions and remain independently editable.

The viewer is a single-file, framework-free page. Match what is already there
before adding anything new: reuse the `:root` tokens, the chip and section
patterns, and the existing lens plumbing rather than introducing a parallel
system.
