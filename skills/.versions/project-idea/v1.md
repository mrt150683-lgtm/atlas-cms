---
id: project-idea
name: Project Idea
type: skill
description: Use the Idea Journal and Atlas project memory together to recall existing thinking, find missing capabilities, combine projects, and propose reviewable new concepts without rewriting the owner's ideas.
tags: [ideas, brainstorming, projects, features, journal, atlas]
requires: [no-silent-canonical-edits@1]
---

# Project Idea

Use this skill whenever the user asks for a project idea, feature idea, tool,
module, agent flow, experiment, cross-project combination, or a review of what
they might build next.

## Start with the journal

1. Call `search_ideas` with the user's wording. If their request is broad, also
   search without a query so recent journal history is represented.
2. Call `get_idea_map` when projects, features, combinations, omissions, or
   “join the dots” are relevant. The map is the current capability inventory;
   stale links are evidence to revisit, not records to erase.
3. Call `get_idea` for the few entries that materially shape the answer. Use
   their overview, sources, children, relationships, and status. Do not flatten
   every stored thought into the prompt.

## Choose the useful mode

- **Project extension:** ground the proposal in one selected project and its
  mapped features.
- **Feature expansion:** start from a specific capability and find adjacent
  tools, workflows, or missing pieces.
- **Cross-project:** name exactly what each project contributes and what bridge
  is absent.
- **Gap finder:** compare accepted ideas, active projects, Scout plans, Fusion
  evidence, and rejected or parked feedback.
- **Join the Dots:** preserve the user's ordered node path. Use
  `join_idea_dots`; do not reorder the squiggle into a tidier story.
- **Wild:** allow surprise, but every candidate must still connect to evidence
  from the journal or Atlas map.

Use `generate_idea_candidates` when the configured model should perform the
structured synthesis. If you already developed a worthwhile concept during the
conversation, use `propose_idea` to place it in the review inbox.

## Protect ownership

Canonical journal ideas belong to the user. You may read them fully and append
model-authored candidates, but you never silently create, merge, retitle,
reparent, accept, reject, or rewrite an idea. Candidate status is not a cosmetic
label: acceptance and merging are explicit human transitions.

Present generated concepts with:

- a clear title and concrete overview;
- the projects, features, and earlier ideas that contribute;
- the missing bridge or capability;
- risks and near-duplicate concerns;
- one small first experiment.

Distinguish recalled journal content from your inference. If the journal is
empty or a linked project is stale, say so plainly and continue with bounded
Atlas evidence rather than inventing history.
