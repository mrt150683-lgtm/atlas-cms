---
id: skill-selector-mode
name: Skill Selector Mode
type: mode
description: Inspect available capabilities and evidence, then load the smallest high-value set of skills, modes, tools, and profiles for the task.
tags: [routing, skills, tools, context-efficiency]
---

# Skill Selector Mode

Use at the beginning of a substantial task or whenever the current capability set is no longer working.

## Selection procedure

1. Translate the request into required capabilities, risks, artefact types, and verification needs.
2. Inspect the actual available Library and tool inventory. Never invent a skill, model, connector, or tool.
3. Prefer assets with relevant human ratings and successful outcomes. Treat agent self-scores as weaker evidence.
4. Select the minimum set that covers the work. Add a second asset only when it contributes a distinct capability.
5. Check requirements, conflicts, trust, publication status, package resources, and context cost.
6. Load canonical instructions before acting. Resolve relative resources from the asset's package root.
7. If results degrade, re-evaluate the selection rather than repeatedly applying an ill-fitting skill.
8. After real use, record the exact versions, outcome, model, available cost metrics, effectiveness, and efficiency.

## Selection note

When useful, state the chosen assets in one short sentence and why each is necessary. Avoid tool theatre and large context bundles whose relevance cannot be explained.
