---
id: capability-router
name: Route Capabilities Deliberately
type: strategy
description: Select the smallest effective combination of skills, tools, context, and permissions for a task. Load when work spans multiple modalities, when several tools could apply, when an agent is about to improvise a workflow, or when a task needs coding plus research, browser, document, simulation, or external-system work.
tags: [routing, tools, composition, least-privilege, workflow]
---

# Route Capabilities Deliberately

Treat capability selection as part of the work.

## Route

1. Restate the outcome and the proof the user will accept.
2. Split the task by modality: understand, decide, create, inspect, execute, verify, communicate.
3. Inventory capabilities actually available in this environment. Do not assume a named tool, login, runtime, browser, model, or skill exists.
4. Search the Library by the work's nouns and verbs. Load only assets that add non-obvious procedure or hard constraints.
5. Prefer the most specific safe tool:
   - structured connector over browser clicking;
   - read-only query over broad shell access;
   - domain skill over a generic prompt;
   - deterministic script over repeated free-form transformation;
   - real environment over a mock when the claim is about reality.
6. Use the least authority needed. Separate inspection from mutation and require the appropriate human gate for consequential external actions.
7. Compose dependencies, check conflicts, and keep the context budget visible. If two assets repeat the same advice, keep the more specific one.
8. Record exact asset versions and evidence-producing tools when the environment supports provenance.
9. Re-route when a tool fails or evidence contradicts the initial plan; never simulate the missing capability.

## Output

Before substantial execution, be able to name:

- outcome;
- selected capabilities and why each is needed;
- permissions or external effects;
- evidence plan;
- fallback if a capability is unavailable.

Keep this compact. Routing should reduce work, not become ceremony.
