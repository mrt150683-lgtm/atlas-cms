---
id: ui-preferences
name: UI Preferences (personal)
type: preference
description: The owner's standing interface taste — dark, high-contrast, calm, premium, information-dense without clutter. Load this alongside any frontend or design skill whenever you build or restyle a UI, pick a palette, or choose typography, so the result looks like this product and not a generic dashboard template.
tags: [ui, design, personal, frontend]
---

# UI Preferences (personal)

These are standing preferences, not requirements for one screen. They tell you
what "good" looks like here, so you don't have to ask every time. A technical
frontend skill tells you *how* to build; this tells you *what the owner will
actually accept*.

## The feel

Dark, high-contrast, quiet, and premium — closer to a precision instrument than
a SaaS dashboard. The interface should feel like it was designed for someone who
looks at it for hours: calm surfaces, sharp text, nothing vibrating for
attention. Confidence, not decoration.

## Non-negotiables

- **Dark by default.** Near-black background, layered surfaces to build depth —
  not boxes with borders everywhere.
- **Contrast where it carries meaning.** Primary text near-white; secondary and
  tertiary text step down in weight. If everything is bright, nothing is.
- **One accent colour, used sparingly.** The accent means "this is live / this
  is selected / act here". Colour is signal, never garnish.
- **Readable typography.** System sans for prose, monospace for anything the
  machine produced (ids, hashes, paths, code). The monospace/prose split is how
  the eye separates *what Atlas knows* from *what Atlas says*.
- **Low clutter.** Prefer one dense, well-ordered view over three sparse panels.
  Whitespace comes from rhythm and alignment, not from padding everything apart.
- **Status is earned, not decorative.** A chip or dot must mean something
  verifiable. Never colour something green because it looks finished.

## Patterns the owner dislikes

- Big gradient hero numbers with a small label under them — the template answer.
- Rounded pastel cards floating on a grey page.
- Emoji as UI chrome (a single meaningful glyph, e.g. an authorship marker, is
  fine — a row of decorative icons is not).
- Modal dialogs for things that could be inline.
- Animation that delays information. Motion should confirm a change, never gate
  it.
- Placeholder/lorem states shipped as if real.

## Accessibility (treat as part of the aesthetic, not a tax)

Body text at a genuine contrast ratio, never grey-on-grey for anything the user
must read. Keyboard reachable, visible focus. Never encode meaning in colour
alone — pair it with a label, shape, or position. Respect
`prefers-reduced-motion`.

## Reference implementation

The Atlas viewer is the house style — read
`cms/ui_assets/index.html` (the `:root` token block) before inventing new
values. Reuse its tokens rather than introducing a parallel palette:
near-black background, layered surfaces, a single blue accent, a restrained
ink scale, 8px radii, system sans + monospace.

If a design decision is genuinely ambiguous, choose the version that a careful
engineer would still find calm at 2am.
