---
id: artifact-delivery-qa
name: Verify Deliverable Artifacts
type: skill
description: Create the final QA pass for documents, PDFs, spreadsheets, slide decks, installers, exports, images, reports, and other user-deliverable files. Load when a task produces an artifact whose layout, template fidelity, formulas, packaging, version, accessibility, or reopen behavior matters.
tags: [artifacts, documents, visual-qa, exports, packaging]
---

# Verify Deliverable Artifacts

Inspect the artifact the user will receive, not only the code or generation log.

## Identify the contract

Record file type, template or brand source, target application, audience, page or sheet constraints, required fields, formulas, naming, version, and delivery path.

## Validate structure

Use format-aware inspection to check:

- file opens in the target or a compatible application;
- required content exists exactly once and in the right place;
- tables, formulas, links, metadata, styles, numbering, and relationships are valid;
- dates, currencies, units, identifiers, and locale rules are correct;
- embedded assets and fonts resolve;
- exports preserve intended semantics;
- installer or package contains the current build and reports the intended version.

## Validate visually

Render every page, slide, sheet region, or screen that matters. Inspect at readable scale for clipping, overlap, overflow, broken wrapping, blank pages, orphaned headings, inconsistent alignment, low contrast, tiny text, and template drift. Re-render after fixes.

For interactive or packaged artifacts, install or open the final output and exercise the critical journey. A successful build command is not final verification.

## Cross-check

Compare the rendered result with source data and the reference template. Spot-check totals and formulas independently. Confirm filenames and final locations. Ensure temporary, stale, or intermediate files are not presented as final.

## Output

Report the final artifact path, structural checks, visual checks, representative spot checks, application or installer test, and any limitation in what could be opened or rendered.
