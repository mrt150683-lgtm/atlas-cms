# Contributors and provenance

Atlas is the result of a collaborative development process involving human
direction and several AI systems. This document records that process without
substituting acknowledgements for Git's authorship history.

## Project collaboration

- **mrt150683-lgtm (Singularity)** — product direction, requirements,
  acceptance decisions, and repository stewardship.
- **Grok** — early brainstorming and product exploration.
- **OpenAI Codex** — architecture planning, adversarial design review, and
  implementation guidance.
- **Claude Code** — implementation, regression testing, and live validation.

These roles describe how the project was developed; they do not imply that
every participant authored every change.

## Authorship policy

The Git commit history remains the source of truth for code authorship. An AI
identity should appear in GitHub's Contributors graph only when a commit
actually authored by that identity is merged. Maintainers must not impersonate
an AI or vendor account by rewriting local Git author details.

Human-authored commits may record assistance with a plain-text commit trailer,
for example `Assisted-by: OpenAI Codex`, when that context is useful. Such a
trailer is an acknowledgement, not a substitute for an authenticated commit
from the official account.

## Contributing

Contributions are welcome through focused pull requests. Describe the problem,
keep the change scoped, add proportionate verification, and report the exact
checks run. Changes affecting Atlas memory should follow the workflow in
[`SKILL.md`](SKILL.md): consult memory before broad source search, declare the
change intent, inspect impact, refresh memory, run the named tests, and check
alignment and Sentinel before claiming completion.
