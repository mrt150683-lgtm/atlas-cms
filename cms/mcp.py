"""MCP server — expose the memory layer as native tools for AI agents.

Speaks Model Context Protocol over stdio (newline-delimited JSON-RPC 2.0,
stdlib only). Register with e.g.:

    claude mcp add cms -- cms mcp --root /path/to/project

Agents then get query_codebase / get_feature_trace / who_calls / get_impact /
get_source etc. as first-class tools — memory consulted before grep.
"""

from __future__ import annotations

import json
import sys
from dataclasses import asdict
from pathlib import Path

from . import config
from .activity import log_activity
from .anchor_drift import detect_anchor_drift
from .config import LANGUAGE_BY_EXTENSION
from .impact import analyze_impact
from .memory import CodebaseMemory

PROTOCOL_VERSION = "2024-11-05"
HISTORY_FOR_MCP = 6  # transcript turns fed back for conversational continuity

TOOLS = [
    {
        "name": "query_codebase",
        "description": "Search the codebase memory by intent (natural language). Returns ranked files/functions/classes/features with paths, line ranges, summaries and call connections. Use this BEFORE grepping or reading files.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "What you're looking for, e.g. 'where is retry logic for API calls?'"},
                "top_k": {"type": "integer", "default": 5},
            },
            "required": ["query"],
        },
    },
    {
        "name": "get_file_summary",
        "description": "Get the AI summary, components (functions/classes with line ranges), anchors and git stats for one file.",
        "inputSchema": {
            "type": "object",
            "properties": {"path": {"type": "string", "description": "Relative path, e.g. src/auth.py"}},
            "required": ["path"],
        },
    },
    {
        "name": "list_features",
        "description": "List all traced features (named capabilities) with member counts and connections.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "get_feature_trace",
        "description": "Full trace of one feature: members, entry points, call flows with file:line steps, narrative with verification checklist, and tests that verify it.",
        "inputSchema": {
            "type": "object",
            "properties": {"name": {"type": "string"}},
            "required": ["name"],
        },
    },
    {
        "name": "who_calls",
        "description": "List callers of a function (by bare name or path::qualname).",
        "inputSchema": {
            "type": "object",
            "properties": {"function": {"type": "string"}},
            "required": ["function"],
        },
    },
    {
        "name": "who_imports",
        "description": "List files that import the given file.",
        "inputSchema": {
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
        },
    },
    {
        "name": "get_impact",
        "description": "Blast radius of changing a target (file, function, or path::qualname): affected functions, files, features and tests, computed by reverse graph traversal.",
        "inputSchema": {
            "type": "object",
            "properties": {"target": {"type": "string"}},
            "required": ["target"],
        },
    },
    {
        "name": "get_anchor_drift",
        "description": "Find high-confidence drift in @memory summaries and declared feature connections. Deterministic and LLM-free; omit target for the whole project.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "target": {"type": "string", "description": "Optional file path, canonical node id, or feature name."},
            },
        },
    },
    {
        "name": "get_review",
        "description": "The AI alignment review: per-feature verdicts (aligned/partial/drift/unverified) on whether the built code matches declared intent, with expected-vs-built explanations and gaps, plus the app-level rollup.",
        "inputSchema": {
            "type": "object",
            "properties": {"feature": {"type": "string", "description": "Optional: one feature's review; omit for all + overall."}},
        },
    },
    {
        "name": "get_suggestions",
        "description": "Planned next features/improvements ranked by return on investment (value 1-5 vs effort 1-5), grounded in review gaps, untested features, git hotspots and hidden coupling.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "export_task_prompt",
        "description": "Turn a plan ('what I intend to do') into an ultra-detailed task brief from the memory: relevant code with file:line + summaries, owning feature traces, blast radius, gaps to respect, conventions, verification steps. Pass `assets` to compose Library skills/strategies/preferences/constraints into the brief (their exact versions are recorded). Use before starting any non-trivial change.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "task": {"type": "string", "description": "The planned work, in plain words."},
                "as_json": {"type": "boolean", "default": False},
                "assets": {"type": "array", "items": {"type": "string"}, "description": "Library asset refs to load: 'id' or 'id@N'; profiles expand to their members (see list_assets)."},
            },
            "required": ["task"],
        },
    },
    {
        "name": "get_sentinel_report",
        "description": "Hermes Sentinel results: latest bug-finding/completion-audit scan with quality-gate verdict, workflow check outcomes, and active findings (severity, file:line, risk, recommended fix). Run `cms sentinel` to refresh.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "severity": {"type": "string", "description": "Optional filter: critical | high | medium | low | info"},
            },
        },
    },
    {
        "name": "get_source",
        "description": "Read an exact source snippet by path and line range (surgical read — prefer this over whole files).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "start_line": {"type": "integer"},
                "end_line": {"type": "integer"},
            },
            "required": ["path"],
        },
    },
    {
        "name": "declare_intent",
        "description": "Record what the current change is meant to do BEFORE you start. Returns a memory-grounded brief (relevant code, features, blast radius, how to verify). Pass `assets` to load Library context for the change — the exact asset versions are recorded in the intent, so the alignment history shows what you were working from. Call this first, then check_alignment when done.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "goal": {"type": "string", "description": "What you plan to do, in plain words. If omitted, inferred from the git branch or last commit."},
                "assets": {"type": "array", "items": {"type": "string"}, "description": "Library asset refs to work under: 'id' or 'id@N'; profiles expand (see list_assets)."},
            },
        },
    },
    {
        "name": "switch_project",
        "description": "Point this memory server at a different project root mid-session ('let's work on X now'). Accepts a directory; walks up to the nearest mapped root (.memory/graph.json). If the project has no memory layer yet, tells you the exact command to build it — run that in a shell, then query again (the new graph is picked up automatically).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Directory of (or inside) the project to switch to, e.g. C:/repos/other-app"},
            },
            "required": ["path"],
        },
    },
    {
        "name": "ask_codebase",
        "description": "Discuss the codebase in plain language: flows, features, bugs, connections, and whether something does what it's SUPPOSED to do (declared intent vs built reality). Assembles evidence from every memory layer (query hits, feature traces, reviews, Sentinel, pipeline state) and answers simply, citing features and path:lines. If the declared intent is missing it says what IS built and asks what you expect. Needs a real provider.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "question": {"type": "string", "description": "Plain-language question, e.g. 'Is the Constellation feature fully aligned with the core idea behind it?'"},
            },
            "required": ["question"],
        },
    },
    {
        "name": "list_projects",
        "description": "The constellation: every Atlas-mapped project on this machine, with readiness (does it have positively recorded feature discovery?), feature counts and hashes. Use before fusing or when the user mentions their other codebases.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "get_fusion_report",
        "description": "The latest cross-project fusion report (integrations, emergent features, conflicts across mapped codebases), plus which member projects have drifted since it was written and the refinement history. Discuss it with the user, then refine_fusion with their direction.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "refine_fusion",
        "description": "Revise the fusion report per the user's direction ('focus on the transcription->memory pipeline, drop the podcast angle'). This is the conversational loop: get_fusion_report -> discuss -> refine_fusion -> repeat until the plan is right. Real provider required; each refinement is recorded.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "direction": {"type": "string", "description": "What to change, focus, deepen or drop — consolidated from the conversation."},
            },
            "required": ["direction"],
        },
    },
    {
        "name": "add_annotation",
        "description": "Attach a structured, typed annotation to a canonical graph object (any node id like feature:X / file:p / func:p::q, an edge, or a source range). Use for observations, bug suspicions, contradictions, questions, intended changes. Model-authored annotations are provenance-stamped and immutable — correcting one means superseding it, so the record survives.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "target": {"type": "string", "description": "Canonical target: a node id (feature:Name, file:path, func:path::qualname, component:Name, system:Name), 'edge:src|dst', 'range:path#start-end', or 'asset:<library-asset-id>'."},
                "type": {"type": "string", "description": "One of: note, observation, intended_change, instruction, bug_suspicion, contradiction, security_concern, performance_concern, architecture_concern, question, decision_link, verification_result."},
                "body": {"type": "string", "description": "The annotation text — concrete and evidence-grounded."},
                "confidence": {"type": "number", "description": "0-1 confidence in the claim (optional)."},
                "evidence": {"type": "array", "items": {"type": "string"}, "description": "Node ids or path:line refs backing the claim (optional)."},
                "feature": {"type": "string", "description": "Related feature name (optional)."},
                "supersedes": {"type": "string", "description": "Annotation id this replaces (optional)."},
            },
            "required": ["target", "type", "body"],
        },
    },
    {
        "name": "list_annotations",
        "description": "Structured annotations on canonical objects — user notes, model observations, contradictions, questions — with status lifecycle. Default excludes archived/superseded. Check these before editing a feature: unresolved contradictions and bug suspicions are declared gaps.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "target": {"type": "string", "description": "Filter to one canonical target id (optional)."},
                "feature": {"type": "string", "description": "Filter to one feature name (optional)."},
                "include_archived": {"type": "boolean", "description": "Include archived/superseded annotations (default false)."},
            },
        },
    },
    {
        "name": "propose_decision",
        "description": "Propose a structured intended-behaviour statement (a decision) for a feature. Decisions are versioned: once a human approves one its intent is locked forever — change means proposing a successor with supersedes set. Agents can propose and read decisions but NEVER approve them (approval is human-only, in the UI).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "feature": {"type": "string", "description": "Feature name the intent belongs to (omit for an app-level decision)."},
                "title": {"type": "string", "description": "Short imperative title, e.g. 'Reject unscanned paths in get_source'."},
                "behaviour": {"type": "string", "description": "What should happen, in plain testable words."},
                "constraints": {"type": "array", "items": {"type": "string"}, "description": "Hard requirements the implementation must honour (optional)."},
                "prohibited": {"type": "array", "items": {"type": "string"}, "description": "Behaviours that must NOT occur (optional)."},
                "supersedes": {"type": "string", "description": "Decision id this proposal replaces (optional)."},
            },
            "required": ["title", "behaviour"],
        },
    },
    {
        "name": "get_decisions",
        "description": "Read the decision trail: proposed and approved intended-behaviour statements, with supersession history. The approved decision for a feature is the ground truth to implement and verify against — check it before coding and cite it in check_alignment.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "feature": {"type": "string", "description": "Filter to one feature (optional)."},
                "active_only": {"type": "boolean", "description": "Only proposed/approved decisions (default true)."},
            },
        },
    },
    {
        "name": "discover_feature",
        "description": "Map a plain-language behaviour description ('users upload a document and it becomes searchable') to a candidate feature: intent-ranked code evidence plus, with a real provider, ONE proposed mapping with per-member reasons. Proposals are never auto-accepted — a human confirms (and can rename) in the UI, which makes the mapping a durable discovered feature.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "description": {"type": "string", "description": "The behaviour in plain words, one or two sentences."},
            },
            "required": ["description"],
        },
    },
    {
        "name": "review_exact_flow",
        "description": "Structured, evidence-classified review of a feature's exact execution flow: the statically traced call skeleton with per-step evidence (static CALLS edges, coverage from mapped tests), and — with a real provider — a step-by-step analysis of inputs, outputs, side effects, async boundaries and error paths, each claim classified proven/observed/inferred/intended. The overall status is computed from evidence ('verified' requires full static+coverage proof); the model can never assert it. Cached per content hash; served stale-flagged when inputs drifted.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "feature": {"type": "string", "description": "Feature name to review (see list_features)."},
                "force": {"type": "boolean", "description": "Regenerate even when a current cached review exists (default false)."},
            },
            "required": ["feature"],
        },
    },
    {
        "name": "check_alignment",
        "description": "Did the change do what was declared? Judges the git diff against the active intent — returns a verdict (aligned/partial/drift/unverified), concrete gaps, Sentinel findings on changed files, and the exact tests to run. Call after making changes.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "base": {"type": "string", "description": "Git base to diff against (default HEAD; use e.g. main for a branch)."},
                "scan": {"type": "boolean", "description": "Refresh Sentinel before judging (default false)."},
            },
        },
    },
    {
        "name": "list_assets",
        "description": "The Library: reusable, versioned agent-context assets (skills, strategies, preferences, constraints, behavioural modes, and profiles). Find the right specialist context using use/rating evidence when available, then load it with get_asset or record it in a task intent.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "type": {"type": "string", "description": "Filter: skill | strategy | preference | constraint | mode | profile."},
                "tag": {"type": "string", "description": "Filter by tag."},
                "status": {"type": "string", "description": "Filter: draft | published | deprecated."},
                "q": {"type": "string", "description": "Substring over id/name/description/tags."},
            },
        },
    },
    {
        "name": "get_asset",
        "description": "One Library asset in full: canonical agent-facing content, metadata, declared dependencies and conflicts, trust level, and version history. The canonical content is what you follow — read it before acting on an asset you were told to load.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "id": {"type": "string", "description": "Asset id (see list_assets)."},
                "version": {"type": "integer", "description": "A specific published version (default: latest)."},
            },
            "required": ["id"],
        },
    },
    {
        "name": "propose_asset",
        "description": "Propose a NEW Library asset (or a revision of one, by reusing its id) as a DRAFT — reusable knowledge worth keeping as a skill, strategy, preference, constraint, or behavioural mode. Publishing is human-only.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Human title, e.g. 'React Component Conventions'."},
                "type": {"type": "string", "description": "skill | strategy | preference | constraint | mode."},
                "description": {"type": "string", "description": "One line: what it does and when to load it."},
                "content": {"type": "string", "description": "The canonical agent-facing content (markdown) — the instructions an agent would follow verbatim."},
                "id": {"type": "string", "description": "Asset id (lowercase slug). Omit for a new asset (derived from the name); pass an existing id to propose a revision of it."},
                "tags": {"type": "array", "items": {"type": "string"}, "description": "Tags for search (optional)."},
                "requires": {"type": "array", "items": {"type": "string"}, "description": "Asset ids this depends on (optional)."},
                "conflicts_with": {"type": "array", "items": {"type": "string"}, "description": "Asset ids this must not be combined with (optional)."},
            },
            "required": ["name", "type", "description", "content"],
        },
    },
    {
        "name": "record_asset_use",
        "description": "After actually using Library assets, append an evidence event pinned to their exact versions and hashes. Agent effectiveness and efficiency are provisional self-assessments; human ratings remain separate.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "assets": {"type": "array", "items": {"type": "string"}, "description": "Published asset ids or pinned refs actually used."},
                "task": {"type": "string", "description": "Short description of the work these assets supported."},
                "outcome": {"type": "string", "description": "success | partial | failure | unknown", "default": "unknown"},
                "effectiveness": {"type": "integer", "minimum": 1, "maximum": 5},
                "efficiency": {"type": "integer", "minimum": 1, "maximum": 5},
                "duration_ms": {"type": "integer", "minimum": 0},
                "input_tokens": {"type": "integer", "minimum": 0},
                "output_tokens": {"type": "integer", "minimum": 0},
                "model": {"type": "string"},
                "notes": {"type": "string"}
            },
            "required": ["assets", "task"]
        }
    },
    {
        "name": "get_asset_feedback",
        "description": "Read aggregate and recent effectiveness evidence for one Library asset, or overall. Human ratings and agent self-assessments are reported separately.",
        "inputSchema": {
            "type": "object",
            "properties": {"id": {"type": "string", "description": "Optional Library asset id."}}
        }
    },
    {
        "name": "search_ideas",
        "description": "Project Idea skill: search the owner's durable Idea Journal before brainstorming. Returns canonical human-owned ideas only; use propose_idea for model suggestions.",
        "inputSchema": {"type": "object", "properties": {
            "query": {"type": "string"},
            "project": {"type": "string", "description": "Optional Atlas project root or name."},
            "kind": {"type": "string"}, "status": {"type": "string"},
            "limit": {"type": "integer", "default": 50}
        }}
    },
    {
        "name": "get_idea",
        "description": "Project Idea skill: inspect one canonical idea with sub-ideas, sources, typed project/feature links, drift state, and its human/model event trail.",
        "inputSchema": {"type": "object", "properties": {
            "id": {"type": "string"}
        }, "required": ["id"]}
    },
    {
        "name": "get_idea_map",
        "description": "Project Idea skill: graph-ready ideas, mapped Atlas projects, features, and their typed connections. Use this to find combinations and missing links.",
        "inputSchema": {"type": "object", "properties": {
            "include_features": {"type": "boolean", "default": True},
            "feature_limit": {"type": "integer", "default": 120}
        }}
    },
    {
        "name": "propose_idea",
        "description": "Project Idea skill: place a model-authored concept in the review inbox. It never edits the owner's canonical journal; a human must accept, merge, park, or reject it.",
        "inputSchema": {"type": "object", "properties": {
            "title": {"type": "string"}, "overview": {"type": "string"},
            "kind": {"type": "string", "default": "concept"},
            "payload": {"type": "object"}
        }, "required": ["title", "overview"]}
    },
    {
        "name": "generate_idea_candidates",
        "description": "Project Idea skill: use the configured real model to generate structured candidates from journal history plus selected Atlas projects/features. Results remain in the review inbox.",
        "inputSchema": {"type": "object", "properties": {
            "mode": {"type": "string", "default": "journal"},
            "direction": {"type": "string"},
            "project_roots": {"type": "array", "items": {"type": "string"}},
            "feature_refs": {"type": "array", "items": {"type": "string"}},
            "idea_ids": {"type": "array", "items": {"type": "string"}},
            "surprise": {"type": "number", "default": 0.5},
            "count": {"type": "integer", "default": 6},
            "seed": {"type": "integer"}
        }}
    },
    {
        "name": "join_idea_dots",
        "description": "Project Idea skill: turn an ordered squiggle path through idea/project/feature node ids into reproducible cross-project candidates, preserving the path, seed, and surprise level.",
        "inputSchema": {"type": "object", "properties": {
            "node_ids": {"type": "array", "items": {"type": "string"}},
            "points": {"type": "array", "items": {"type": "array", "items": {"type": "number"}}},
            "surprise": {"type": "number", "default": 0.7},
            "direction": {"type": "string"}, "seed": {"type": "integer"},
            "count": {"type": "integer", "default": 6}
        }, "required": ["node_ids"]}
    },
]


# @memory:feature:AgentMemoryAccess
# @memory:summary:Root discovery for a globally-configured MCP server — walk up from the launch dir to the nearest project holding a memory layer, so one config entry serves every repo.
def discover_root(start: Path) -> Path:
    """Nearest ancestor (incl. start) holding .memory/graph.json; else start."""
    for candidate in (start, *start.parents):
        if (candidate / config.MEMORY_DIR_NAME / "graph.json").is_file():
            return candidate
    return start


# @memory:feature:AgentMemoryAccess
# @memory:connects:QueryEngine, FeatureTracing, ImpactAnalysis, ActivityPulse
# @memory:summary:MCP server (stdio JSON-RPC) exposing the memory as native agent tools — query, summaries, feature traces, impact, surgical source reads.
class MCPServer:
    def __init__(self, root: Path) -> None:
        self.root = root.resolve()
        self.graph_path = self.root / config.MEMORY_DIR_NAME / "graph.json"
        self._memory: CodebaseMemory | None = None
        self._mtime = 0.0
        self._client = ""  # clientInfo label from initialize — annotation provenance

    def memory(self) -> CodebaseMemory:
        if not self.graph_path.is_file():
            raise RuntimeError(
                f"no memory layer at {self.root} — run `cms run-all` (or `cms app`) there first"
            )
        mtime = self.graph_path.stat().st_mtime
        if self._memory is None or mtime != self._mtime:
            self._memory = CodebaseMemory.load(self.graph_path)
            self._mtime = mtime
        return self._memory

    def _log(self, tool: str, nodes: list[str], label: str = "") -> None:
        # Never create .memory/ in a project that has no memory layer yet —
        # the feed is cosmetic and must not scribble on un-mapped repos.
        if self.graph_path.is_file():
            log_activity(self.root / config.MEMORY_DIR_NAME, tool, nodes, label=label)

    # ── tool implementations ────────────────────────────────────────────

    def query_codebase(self, query: str, top_k: int = 5) -> list[dict]:
        return [asdict(r) for r in self.memory().query_intent(query, top_k=top_k)]

    def get_file_summary(self, path: str) -> dict:
        graph = self.memory().graph
        node_id = f"file:{path}"
        if not graph.has_node(node_id):
            return {"error": f"unknown file {path!r}"}
        attrs = graph.nodes[node_id]
        components = [
            {
                "kind": c["type"], "name": c.get("qualname", c["name"]),
                "lines": f"{c.get('start_line')}-{c.get('end_line')}",
                "summary": c.get("summary", ""),
            }
            for _, child, d in graph.out_edges(node_id, data=True)
            if d.get("type") == "CONTAINS"
            for c in [graph.nodes[child]]
        ]
        return {
            "path": path,
            "language": attrs.get("language"),
            "line_count": attrs.get("line_count"),
            "summary": attrs.get("summary", ""),
            "anchors": attrs.get("anchors") or {},
            "git": attrs.get("git") or {},
            "components": sorted(components, key=lambda c: c["lines"]),
        }

    def list_features(self) -> list[dict]:
        from .features import get_features

        return [
            {
                "name": f["name"], "source": f.get("source"),
                "description": f.get("description", ""),
                "members": len(f.get("members", [])),
                "connects": f.get("connects", []),
                "exercised_by": len(f.get("exercised_by", [])),
            }
            for f in get_features(self.memory().graph)
        ]

    def get_feature_trace(self, name: str) -> dict:
        from .features import get_features

        for f in get_features(self.memory().graph):
            if f["name"].lower() == name.lower():
                return {
                    "name": f["name"],
                    "description": f.get("description", ""),
                    "members": f.get("members", []),
                    "entry_points": f.get("entry_points", []),
                    "flows": f.get("flows", []),
                    "connects": f.get("connects", []),
                    "narrative": f.get("summary", ""),
                    "exercised_by": f.get("exercised_by", []),
                    # deprecated alias, kept one release for older agent docs
                    "verified_by": f.get("exercised_by", []),
                }
        return {"error": f"unknown feature {name!r}"}

    def who_calls(self, function: str) -> list[str]:
        return self.memory().who_calls(function)

    def who_imports(self, path: str) -> list[str]:
        return self.memory().who_imports(path)

    def get_impact(self, target: str) -> dict:
        impact = analyze_impact(self.memory().graph, target)
        if impact is None:
            return {"error": f"could not resolve {target!r}"}
        return asdict(impact)

    # @memory:feature:AnchorDrift
    # @memory:feature:AgentMemoryAccess
    # @memory:summary:Exposes the deterministic anchor-integrity report to agents, optionally scoped to one canonical target.
    def get_anchor_drift(self, target: str | None = None) -> dict:
        try:
            return detect_anchor_drift(self.memory().graph, self.root, target=target).to_dict()
        except ValueError as exc:
            return {"error": str(exc)}

    def get_review(self, feature: str | None = None) -> dict:
        from .features import get_features

        graph = self.memory().graph
        feats = get_features(graph)
        if feature:
            for f in feats:
                if f["name"].lower() == feature.lower():
                    return f.get("review") or {"error": "no review yet — run `cms review`"}
            return {"error": f"unknown feature {feature!r}"}
        app = dict(graph.nodes["review:app"]) if graph.has_node("review:app") else None
        return {
            "app": app,
            "features": {f["name"]: f["review"] for f in feats if f.get("review")},
        }

    def get_suggestions(self) -> dict:
        graph = self.memory().graph
        if not graph.has_node("suggestions:app"):
            return {"error": "no suggestions yet — run `cms suggest`"}
        node = graph.nodes["suggestions:app"]
        return {"provider": node.get("provider"), "items": node.get("items") or []}

    def export_task_prompt(self, task: str, as_json: bool = False,
                           assets: list | None = None) -> dict:
        from .prompt_export import export_prompt

        content, out = export_prompt(self.root, task, as_json=as_json,
                                     assets=[str(a) for a in (assets or [])])
        return {"path": str(out), "content": content}

    # @memory:feature:ChangeAlignment
    # @memory:connects:AgentMemoryAccess, ImpactAnalysis, HermesSentinel, PromptExport
    # @memory:summary:Agent intent-input channel — records the goal of the current change and returns a memory-grounded brief to work against.
    def declare_intent(self, goal: str | None = None, assets: list | None = None) -> dict:
        from .intent import capture_intent

        pack = capture_intent(self.root, goal=goal,
                              assets=[str(a) for a in (assets or [])])
        library = pack.get("library") or {}
        return {
            "intent": pack.get("task"),
            "source": pack.get("intent_source"),
            "relevant_code": [
                {"kind": t["kind"], "name": t["name"], "path": t.get("path"), "lines": t.get("lines")}
                for t in pack.get("relevant_code", [])
            ],
            "features": [f["name"] for f in pack.get("features", [])],
            "impact": pack.get("impact"),
            "library": {
                "assets": [{"id": a["id"], "version": a["version"],
                            "content_hash": a["content_hash"], "type": a["type"],
                            "scope": a["scope"], "trust": a["trust"]}
                           for a in library.get("assets", [])],
                "warnings": library.get("warnings", []),
                "conflicts": library.get("conflicts", []),
            } if library else None,
            "verification": pack.get("verification", []),
        }

    # @memory:feature:ChangeAlignment
    # @memory:summary:Honest-finish check — verdicts the working diff against the declared intent (aligned/partial/drift/unverified) with gaps, findings and tests to run.
    def check_alignment(self, base: str = "HEAD", scan: bool = False) -> dict:
        from .align import AlignStore, build_alignment

        store = AlignStore(self.root / config.MEMORY_DIR_NAME)
        pack = store.load_intent()
        if pack is None:
            return {"error": "no intent declared — call declare_intent first"}
        record = build_alignment(self.memory(), self.root, pack, base=base, scan=scan)
        store.save_alignment(record)
        return record

    def get_sentinel_report(self, severity: str | None = None) -> dict:
        from .sentinel import ACTIVE_STATUSES
        from .sentinel.store import SentinelStore

        store = SentinelStore(self.root / config.MEMORY_DIR_NAME)
        scan = store.latest_scan()
        if scan is None:
            return {"error": "no Sentinel scan yet — run `cms sentinel`"}
        findings = [
            f for f in store.load_findings().values()
            if f.get("status") in ACTIVE_STATUSES
            and (not severity or f.get("severity") == severity)
        ]
        return {
            "scan_id": scan.get("scan_id"),
            "created_at": scan.get("created_at"),
            "execution_mode": scan.get("execution_mode"),
            "gate": scan.get("gate"),
            "workflow_checks": [
                {k: c.get(k) for k in ("name", "passed", "actual", "mode")}
                for c in scan.get("workflow_checks", [])
            ],
            "active_findings": sorted(
                findings, key=lambda f: ("critical high medium low info".split().index(f["severity"])
                                          if f["severity"] in ("critical", "high", "medium", "low", "info") else 9),
            )[:40],
        }

    # @memory:feature:AgentMemoryAccess
    # @memory:summary:Mid-session project flipping — rebinds the server to another project root so one agent session can move between codebases without a restart.
    def switch_project(self, path: str) -> dict:
        target = Path(path).expanduser()
        if not target.is_absolute():
            target = self.root / target
        try:
            target = target.resolve()
        except OSError as exc:
            return {"error": f"cannot resolve {path!r}: {exc}"}
        if not target.is_dir():
            return {"error": f"not a directory: {path!r}"}
        root = discover_root(target)
        mapped = (root / config.MEMORY_DIR_NAME / "graph.json").is_file()
        # Only rebind onto things that are actually projects — never a home dir
        # or drive root just because it exists (source access follows the root).
        if not mapped and not (root / ".git").is_dir():
            return {"error": f"{root} doesn't look like a project (no .memory/ and no .git); "
                             "point me at a repo root"}
        self.root = root
        self.graph_path = root / config.MEMORY_DIR_NAME / "graph.json"
        self._memory = None
        self._mtime = 0.0
        if not mapped:
            return {
                "root": str(root), "memory": "missing",
                "next_step": f"run `cms run-all --root {root}` in a shell (mock provider: add `-p mock`), "
                             "then query again — the new graph is picked up automatically",
            }
        graph = self.memory().graph
        return {
            "root": str(root), "memory": "loaded",
            "files": sum(1 for _, a in graph.nodes(data=True) if a.get("type") == "file"),
            "nodes": graph.number_of_nodes(), "edges": graph.number_of_edges(),
        }

    # @memory:feature:CodebaseChat
    # @memory:summary:ask_codebase over MCP — external agents get the same grounded plain-language Q&A as the UI popup, including intent-vs-reality judgment.
    def ask_codebase(self, question: str) -> dict:
        from .chat import ChatError, ask, session_history
        from .providers import get_provider

        try:
            entry = ask(self.root, question, get_provider(None),
                        history=session_history(self.root, "agent", HISTORY_FOR_MCP),
                        session="agent")
        except ChatError as exc:
            return {"error": str(exc)}
        return {"answer": entry["a"], "evidence_nodes": entry["evidence_nodes"],
                "matched_features": entry["matched_features"],
                "provider": entry["provider"], "model": entry["model"]}

    # @memory:feature:Constellation
    # @memory:connects:AgentMemoryAccess, FeatureTracing
    # @memory:summary:Multi-project discovery over MCP — list mapped projects, read the fusion report, and conversationally refine it; the IDE agent is the chat surface.
    def list_projects(self) -> list[dict]:
        from .fuse import build_card, load_registry

        out = []
        for root_str, meta in (load_registry().get("projects") or {}).items():
            card = build_card(Path(root_str))
            entry = {"name": card["name"], "root": root_str,
                     "ready": bool(card.get("ready")),
                     "last_built": meta.get("last_built")}
            if card.get("ready"):
                entry["features"] = len(card["features"])
                entry["feature_set_hash"] = card["feature_set_hash"]
            else:
                entry["reason"] = card.get("reason")
            out.append(entry)
        return sorted(out, key=lambda e: e["name"])

    # @memory:feature:Constellation
    # @memory:summary:Serve the latest cross-project fusion report with drift status and the refinement trail.
    def get_fusion_report(self) -> dict:
        from .fuse import fusion_history, fusion_staleness, load_fusion

        report = load_fusion()
        if report is None:
            return {"error": "no fusion report yet — run `cms fuse` "
                             "(needs >= 2 mapped projects with recorded discovery)"}
        return {"report": report,
                "stale_members": fusion_staleness(report),
                "refinements": fusion_history()}

    # @memory:feature:Constellation
    # @memory:summary:Conversational refinement — revise the fusion report per the owner's direction; failures preserve last-known-good.
    def refine_fusion(self, direction: str) -> dict:
        from .fuse import FusionError, fusion_staleness
        from .fuse import refine_fusion as _refine
        from .providers import get_provider

        try:
            report = _refine(direction, get_provider(None))
        except FusionError as exc:
            return {"error": str(exc)}
        return {"refined": True, "direction": direction,
                "report": report, "stale_members": fusion_staleness(report)}

    # @memory:feature:StructuredAnnotations
    # @memory:summary:Agents attach and read typed annotations on canonical graph objects over MCP — author provenance auto-stamped from the connected client and configured provider.
    def add_annotation(self, target: str, type: str, body: str,
                       confidence: float | None = None,
                       evidence: list | None = None,
                       feature: str | None = None,
                       supersedes: str | None = None) -> dict:
        from .annotations import AnnotationStore
        from .providers import provider_identity

        ident = provider_identity()
        author = {"kind": "model", "identity": self._client or "mcp-agent",
                  "provider": ident.get("name"), "model": ident.get("model"),
                  "via": "mcp"}
        try:
            entry = AnnotationStore(self.root / config.MEMORY_DIR_NAME, root=self.root).add(
                target, type, body, author=author, confidence=confidence,
                evidence=evidence, feature=feature, supersedes=supersedes,
            )
        except ValueError as exc:
            return {"error": str(exc)}
        return {"annotation": entry}

    def list_annotations(self, target: str | None = None,
                         feature: str | None = None,
                         include_archived: bool = False) -> dict:
        from .annotations import AnnotationStore

        store = AnnotationStore(self.root / config.MEMORY_DIR_NAME, root=self.root)
        return {"annotations": store.list(target=target, feature=feature,
                                          include_archived=include_archived)}

    # @memory:feature:ApprovedDecisions
    # @memory:summary:Agents propose and read versioned intended-behaviour decisions over MCP; approval stays human-only by design.
    def propose_decision(self, title: str, behaviour: str,
                         feature: str | None = None,
                         constraints: list | None = None,
                         prohibited: list | None = None,
                         supersedes: str | None = None) -> dict:
        from .decisions import DecisionStore
        from .providers import provider_identity

        ident = provider_identity()
        try:
            dec = DecisionStore(self.root / config.MEMORY_DIR_NAME, root=self.root).propose(
                feature, title,
                {"behaviour": behaviour, "constraints": constraints or [],
                 "prohibited": prohibited or []},
                created_by={"kind": "model", "identity": self._client or "mcp-agent",
                            "provider": ident.get("name"), "model": ident.get("model"),
                            "via": "mcp"},
                supersedes=supersedes,
            )
        except ValueError as exc:
            return {"error": str(exc)}
        return {"decision": dec,
                "next_step": "a human must approve this in the UI before it becomes locked intent"}

    def get_decisions(self, feature: str | None = None, active_only: bool = True) -> dict:
        from .decisions import DecisionStore

        store = DecisionStore(self.root / config.MEMORY_DIR_NAME, root=self.root)
        return {"decisions": store.list(feature=feature, active_only=active_only)}

    # @memory:feature:AtlasLibrary
    # @memory:summary:Agents browse the Library over MCP to find the right reusable context for a task.
    def list_assets(self, type: str | None = None, tag: str | None = None,
                    status: str | None = None, q: str | None = None) -> dict:
        from .library import LibraryError, LibraryView
        from .library_usage import LibraryUsageStore

        try:
            rows = LibraryView(self.root).list(type=type, tag=tag, status=status, q=q)
        except LibraryError as exc:
            return {"error": str(exc)}
        summaries = LibraryUsageStore(self.root / config.MEMORY_DIR_NAME).summaries()
        return {"assets": [
            {"id": r["id"], "name": r.get("name"), "type": r.get("type"),
             "description": r.get("description"), "tags": r.get("tags") or [],
             "version": r.get("current_version"), "status": r.get("status"),
             "scope": r.get("scope"), "trust": r.get("trust"),
             "effective": r.get("effective"), "enabled": r.get("enabled_effective", True),
             "evidence": summaries.get(r["id"], {"uses": 0, "human": {"ratings": 0}})}
            for r in rows]}

    # @memory:feature:AtlasLibrary
    # @memory:summary:Full canonical content of one Library asset for the agent that must follow it.
    def get_asset(self, id: str, version: int | None = None) -> dict:
        from .library import LibraryError, LibraryView
        from .library_usage import LibraryUsageStore

        try:
            asset = LibraryView(self.root).get(id, version)
        except LibraryError as exc:
            return {"error": str(exc)}
        meta = asset.get("meta") or {}
        return {"asset": {
            "id": asset["id"], "name": asset.get("name"), "type": asset.get("type"),
            "description": asset.get("description"), "tags": asset.get("tags") or [],
            "requires": meta.get("requires") or [],
            "conflicts_with": meta.get("conflicts_with") or [],
            "members": meta.get("assets") or [],
            "source": meta.get("source"), "source_path": meta.get("source_path"),
            "resource_root": meta.get("resource_root"),
            "excluded_context": meta.get("excluded_context") or [],
            "status": asset.get("status"), "scope": asset.get("scope"),
            "trust": asset.get("trust"), "version": asset.get("resolved_version"),
            "draft": asset.get("draft"),
            "versions": [{"version": v["version"], "content_hash": v["content_hash"],
                          "published_at": v.get("published_at"),
                          "published_by": v.get("published_by")}
                         for v in asset.get("versions") or []],
            "content": asset.get("body"),
            "evidence": LibraryUsageStore(self.root / config.MEMORY_DIR_NAME).summary(id),
        }}

    # @memory:feature:LibraryFeedbackLoop
    # @memory:connects:AtlasLibrary, ActivityPulse
    # @memory:summary:Agents append exact-version Library use evidence and read human ratings separately from their own effectiveness assessment.
    def record_asset_use(self, assets: list, task: str, outcome: str = "unknown",
                         effectiveness: int | None = None,
                         efficiency: int | None = None,
                         duration_ms: int | None = None,
                         input_tokens: int | None = None,
                         output_tokens: int | None = None,
                         model: str | None = None, notes: str | None = None) -> dict:
        from .library import LibraryError, LibraryView
        from .library_usage import LibraryUsageStore
        from .providers import provider_identity

        try:
            refs = [str(ref) for ref in assets]
            result = LibraryView(self.root).compose(refs)
            if result.get("warnings") or not result.get("assets"):
                return {"error": "all recorded assets must resolve to enabled published versions",
                        "composition": result}
            ident = provider_identity()
            event = LibraryUsageStore(self.root / config.MEMORY_DIR_NAME).record(
                result["assets"], task=task, outcome=outcome,
                effectiveness=effectiveness, efficiency=efficiency,
                duration_ms=duration_ms, input_tokens=input_tokens,
                output_tokens=output_tokens, model=model or ident.get("model"),
                notes=notes, source="agent", client=self._client or "mcp-agent")
        except (LibraryError, ValueError) as exc:
            return {"error": str(exc)}
        return {"use": event,
                "next_step": "the user may add a separate human rating in the Library screen"}

    # @memory:feature:LibraryFeedbackLoop
    def get_asset_feedback(self, id: str | None = None) -> dict:
        from .library_usage import LibraryUsageStore

        return {"feedback": LibraryUsageStore(
            self.root / config.MEMORY_DIR_NAME).summary(id)}

    # @memory:feature:AtlasLibrary
    # @memory:connects:StructuredAnnotations
    # @memory:summary:Agents propose Library assets as drafts only — provenance-stamped agent authorship, human publishing; canonical published content is never touched by a model.
    def propose_asset(self, name: str, type: str, description: str, content: str,
                      id: str | None = None, tags: list | None = None,
                      requires: list | None = None,
                      conflicts_with: list | None = None) -> dict:
        from .library import (
            LibraryError,
            LibraryView,
            serialize_asset,
            slugify,
            validate_meta,
        )
        from .providers import provider_identity

        ident = provider_identity()
        author = {"kind": "model", "identity": self._client or "mcp-agent",
                  "provider": ident.get("name"), "model": ident.get("model"),
                  "via": "mcp"}
        try:
            meta = validate_meta({
                "id": id or slugify(name), "name": name, "type": type,
                "description": description, "tags": tags or [],
                "requires": requires or [], "conflicts_with": conflicts_with or [],
            })
            store = LibraryView(self.root).store("project")
            existing = store.get(meta["id"])
            rec = store.save_draft(serialize_asset(meta, str(content)),
                                   created_by=author, trust="agent")
        except LibraryError as exc:
            return {"error": str(exc)}
        revision = bool(existing and existing.get("versions"))
        return {
            "asset": {"id": rec["id"], "type": rec["type"], "status": rec["status"],
                      "trust": rec["trust"], "scope": rec["scope"],
                      "revision_of_published": revision},
            "next_step": ("this is a DRAFT revision — the published version is unchanged; "
                          "a human reviews the diff and publishes it in the Library screen"
                          if revision else
                          "a human must review and publish this draft in the Library "
                          "screen before any agent loads it"),
        }

    # @memory:feature:FeatureDiscoveryByDescription
    # @memory:summary:discover_feature over MCP — NL behaviour description to evidence-backed candidate mapping; confirmation stays human-only in the UI.
    def discover_feature(self, description: str) -> dict:
        from .feature_discovery import FeatureDiscoveryError, propose_feature
        from .providers import get_provider

        try:
            out = propose_feature(self.root, self.memory(), description,
                                  get_provider(None))
        except FeatureDiscoveryError as exc:
            return {"error": str(exc)}
        if out.get("candidate"):
            out["next_step"] = ("ask the human to confirm/rename this mapping in the "
                                "UI feature list — proposals are never auto-accepted")
        return out

    # @memory:feature:ExactFlowReview
    # @memory:summary:review_exact_flow over MCP — agents get the evidence-classified flow account and its verification status, cache-first with honest staleness.
    def review_exact_flow(self, feature: str, force: bool = False) -> dict:
        from .flowreview import FlowReviewError, build_flow_review, read_flow_review
        from .providers import get_provider

        mem = self.memory()
        try:
            if not force:
                stored = read_flow_review(self.root, mem.graph, feature)
                if stored and not stored["stale"]:
                    return {**stored, "reused": True}
            review = build_flow_review(self.root, mem.graph, get_provider(None),
                                       feature, force=force)
        except FlowReviewError as exc:
            return {"error": str(exc)}
        mem.save(self.graph_path)
        self._mtime = self.graph_path.stat().st_mtime  # our own write isn't a reload
        return review

    # @memory:feature:IdeaJournal
    # @memory:connects:AgentMemoryAccess, AtlasLibrary
    # @memory:summary:Project Idea skill tools give agents full journal search, map, proposal, generation, and join-path access while preserving human authority over canonical ideas.
    def search_ideas(self, query: str = "", project: str | None = None,
                     kind: str | None = None, status: str | None = None,
                     limit: int = 50) -> dict:
        from .ideas import default_journal

        return {"ideas": default_journal().search(query, project=project, kind=kind,
                                                   status=status, limit=limit)}

    def get_idea(self, id: str) -> dict:
        from .ideas import default_journal

        idea = default_journal().get_idea(id)
        return {"idea": idea} if idea else {"error": f"unknown idea {id!r}"}

    def get_idea_map(self, include_features: bool = True,
                     feature_limit: int = 120) -> dict:
        from .ideas import default_journal

        return default_journal().map_data(include_features=include_features,
                                          feature_limit=feature_limit)

    def propose_idea(self, title: str, overview: str, kind: str = "concept",
                     payload: dict | None = None) -> dict:
        from .ideas import IdeaError, default_journal

        try:
            candidate = default_journal().propose_candidate(
                title, overview, kind=kind, payload=payload, origin="project-idea-skill",
                actor_kind="model")
        except IdeaError as exc:
            return {"error": str(exc)}
        return {"candidate": candidate,
                "next_step": "a human reviews this in the Idea Journal inbox"}

    def generate_idea_candidates(self, mode: str = "journal", direction: str = "",
                                 project_roots: list | None = None,
                                 feature_refs: list | None = None,
                                 idea_ids: list | None = None,
                                 surprise: float = 0.5, count: int = 6,
                                 seed: int | None = None) -> dict:
        from .ideas import IdeaError, default_journal
        from .providers import get_provider

        try:
            return default_journal().generate(
                get_provider(None), mode=mode, direction=direction,
                project_roots=project_roots, feature_refs=feature_refs,
                idea_ids=idea_ids, surprise=surprise, count=count, seed=seed)
        except IdeaError as exc:
            return {"error": str(exc)}

    def join_idea_dots(self, node_ids: list, points: list | None = None,
                       surprise: float = 0.7, direction: str = "",
                       seed: int | None = None, count: int = 6) -> dict:
        from .ideas import IdeaError, default_journal
        from .providers import get_provider

        try:
            return default_journal().join_dots(
                get_provider(None), node_ids, points=points, surprise=surprise,
                direction=direction, seed=seed, count=count)
        except IdeaError as exc:
            return {"error": str(exc)}

    def get_source(self, path: str, start_line: int = 1, end_line: int | None = None) -> dict:
        target = (self.root / path).resolve()
        if self.root not in target.parents and target != self.root:
            return {"error": "path outside project root"}
        if target.suffix.lower() not in LANGUAGE_BY_EXTENSION or not target.is_file():
            return {"error": "not a scanned source file"}
        lines = target.read_text(encoding="utf-8", errors="replace").splitlines()
        end = min(end_line or len(lines), len(lines))
        start = max(1, start_line)
        body = "\n".join(f"{i}\t{lines[i - 1]}" for i in range(start, end + 1))
        return {"path": path, "lines": f"{start}-{end}", "source": body}

    # ── JSON-RPC plumbing ───────────────────────────────────────────────

    def handle(self, message: dict) -> dict | None:
        method = message.get("method", "")
        msg_id = message.get("id")
        if method == "initialize":
            client = (message.get("params") or {}).get("clientInfo") or {}
            label = client.get("name") or "AI model"
            if client.get("version"):
                label += f" {client['version']}"
            self._client = label
            self._log("connected", [], label=label)
            return self._result(msg_id, {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "cms", "version": "0.1.0"},
            })
        if method in ("notifications/initialized", "notifications/cancelled"):
            return None
        if method == "ping":
            return self._result(msg_id, {})
        if method == "tools/list":
            return self._result(msg_id, {"tools": TOOLS})
        if method == "tools/call":
            params = message.get("params", {})
            name = params.get("name", "")
            args = params.get("arguments", {}) or {}
            fn = getattr(self, name, None)
            if not callable(fn) or name not in {t["name"] for t in TOOLS}:
                return self._error(msg_id, -32602, f"unknown tool {name!r}")
            try:
                payload = fn(**args)
            except Exception as exc:
                return self._result(msg_id, {
                    "content": [{"type": "text", "text": f"error: {type(exc).__name__}: {exc}"}],
                    "isError": True,
                })
            self._log(
                name, self._touched_nodes(name, args, payload),
                label=str(args.get("query") or args.get("name") or args.get("target")
                          or args.get("path") or args.get("function") or ""),
            )
            return self._result(msg_id, {
                "content": [{"type": "text", "text": json.dumps(payload, indent=2)}]
            })
        if msg_id is not None:
            return self._error(msg_id, -32601, f"method {method!r} not found")
        return None

    def _touched_nodes(self, tool: str, args: dict, payload) -> list[str]:
        """Node ids a tool call read — feeds the UI's live activity pulses."""
        nodes: list[str] = []
        try:
            if tool == "query_codebase" and isinstance(payload, list):
                nodes = [r["node_id"] for r in payload]
            elif tool in ("get_file_summary", "get_source") and "path" in args:
                nodes = [f"file:{args['path']}"]
            elif tool == "who_imports" and "path" in args:
                nodes = [f"file:{args['path']}", *payload]
            elif tool == "who_calls" and isinstance(payload, list):
                nodes = list(payload)
            elif tool == "get_feature_trace" and isinstance(payload, dict):
                nodes = [f"feature:{payload.get('name', '')}", *payload.get("members", [])]
            elif tool == "list_features" and isinstance(payload, list):
                nodes = [f"feature:{f['name']}" for f in payload]
            elif tool == "get_review" and isinstance(payload, dict):
                nodes = [f"feature:{n}" for n in (payload.get("features") or {})] or \
                        ([f"feature:{args['feature']}"] if "feature" in args else [])
            elif tool in ("add_annotation", "list_annotations") and args.get("target"):
                nodes = [str(args["target"])]
            elif tool == "get_impact" and isinstance(payload, dict):
                nodes = [payload.get("target", "")]
                nodes += [f"file:{p}" for p in payload.get("files", [])]
                nodes += [f"file:{f.split('::')[0]}" for f in payload.get("functions", [])]
            elif tool == "get_anchor_drift" and isinstance(payload, dict):
                nodes = [f.get("node_id", "") for f in payload.get("findings", [])]
            elif tool == "list_assets" and isinstance(payload, dict):
                nodes = [f"asset:{a['id']}" for a in payload.get("assets", []) if a.get("id")]
            elif tool in ("get_asset", "propose_asset") and isinstance(payload, dict):
                asset = payload.get("asset") or {}
                nodes = [f"asset:{asset['id']}"] if asset.get("id") else []
            elif tool == "record_asset_use" and isinstance(payload, dict):
                nodes = [f"asset:{a['id']}" for a in (payload.get("use") or {}).get("assets", [])
                         if a.get("id")]
            elif tool == "get_asset_feedback" and args.get("id"):
                nodes = [f"asset:{args['id']}"]
            elif tool == "search_ideas" and isinstance(payload, dict):
                nodes = [f"idea:{i['id']}" for i in payload.get("ideas", []) if i.get("id")]
            elif tool == "get_idea" and isinstance(payload, dict):
                idea = payload.get("idea") or {}
                nodes = [f"idea:{idea['id']}"] if idea.get("id") else []
            elif tool == "get_idea_map" and isinstance(payload, dict):
                nodes = [n["id"] for n in payload.get("nodes", [])[:100] if n.get("id")]
            elif tool == "propose_idea" and isinstance(payload, dict):
                cand = payload.get("candidate") or {}
                nodes = [f"candidate:{cand['id']}"] if cand.get("id") else []
            elif tool in ("generate_idea_candidates", "join_idea_dots") and isinstance(payload, dict):
                nodes = [f"candidate:{c['id']}" for c in payload.get("candidates", [])
                         if c.get("id")]
        except (KeyError, TypeError):
            pass
        return [n for n in dict.fromkeys(nodes) if n]

    @staticmethod
    def _result(msg_id, result: dict) -> dict:
        return {"jsonrpc": "2.0", "id": msg_id, "result": result}

    @staticmethod
    def _error(msg_id, code: int, text: str) -> dict:
        return {"jsonrpc": "2.0", "id": msg_id, "error": {"code": code, "message": text}}

    def serve(self) -> None:
        for line in sys.stdin:
            line = line.strip()
            if not line:
                continue
            try:
                message = json.loads(line)
            except json.JSONDecodeError:
                continue
            response = self.handle(message)
            if response is not None:
                sys.stdout.write(json.dumps(response) + "\n")
                sys.stdout.flush()
