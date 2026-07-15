---
id: agentic-system-red-team
name: Red-Team Agentic Systems
type: skill
description: Threat-model and safely red-team AI agents, tool-using assistants, MCP ecosystems, autonomous workflows, memory systems, and multi-agent applications. Load for agent security reviews, prompt-injection testing, permission audits, tool misuse, memory poisoning, supply-chain risk, or before granting an agent broader autonomy.
tags: [security, agents, red-team, owasp, prompt-injection]
---

# Red-Team Agentic Systems

Assume untrusted content can influence planning and that every tool expands the consequence surface.

## Scope the system

Map:

- goals, planners, models, prompts, and policy layers;
- users, agents, identities, credentials, and delegation;
- tools and their full functionality, not only the intended call;
- memory, retrieval, files, messages, and external content;
- trust boundaries and data flow;
- approval, authorization, monitoring, and recovery.

Record assets and unacceptable impacts across confidentiality, integrity, availability, safety, financial cost, and user trust.

## Test threat classes

Cover at least:

- direct and indirect goal hijacking;
- tool misuse and parameter smuggling;
- excessive functionality, permission, or autonomy;
- identity and privilege abuse across users or agents;
- malicious or compromised skills, MCP servers, packages, models, and retrieved content;
- unexpected code execution and unsafe output handling;
- memory poisoning, false persistence, and cross-session contamination;
- insecure inter-agent communication and confused-deputy behavior;
- sensitive data leakage through prompts, logs, outputs, or tools;
- denial of service, runaway loops, budget exhaustion, and cascading failure;
- deceptive or untraceable action, repudiation, and missing audit evidence.

## Exercise safely

Use an isolated environment, synthetic secrets, reversible fixtures, explicit authorization, and bounded budgets. Test whether hostile instructions survive quoting, encoding, document ingestion, summarization, multi-hop delegation, and memory recall. Do not execute destructive payloads against real systems.

## Judge controls

Prefer prevention outside the model:

- minimize available tools and each tool's functionality;
- enforce least privilege and user-scoped identities downstream;
- validate every consequential request at the action boundary;
- separate untrusted data from instructions;
- require human approval for high-impact actions;
- constrain output schemas and validate them deterministically;
- log tool intent, parameters, identity, result, and provenance;
- rate-limit, sandbox, monitor, revoke, and recover.

Prompt instructions alone are not a security boundary.

## Output

Return the threat model, attack cases, evidence, exploit preconditions, impact, control gaps, and prioritized mitigations with retest steps.

## Current baseline

Map findings to the OWASP Top 10 for Agentic Applications 2026 and the current OWASP GenAI guidance. Check for newer revisions when the assessment is consequential.
