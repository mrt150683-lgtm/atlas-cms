"""Prompt export — turn "here's what I plan to do" into an ultra-detailed,
ready-to-paste task prompt grounded in the memory.

Given a task description, assembles: the most relevant files/functions with
line ranges and summaries, the owning feature traces and connections, the
blast radius of the likely change target, review gaps to respect, related
ROI suggestions, project conventions, and concrete verification steps.
Markdown for pasting into any AI chat; ``as_json`` for the full data pack.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path

from . import config
from .impact import analyze_impact
from .memory import CodebaseMemory


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:60] or "task"


def _declared_paths(task: str) -> list[str]:
    """Paths written literally in the user's goal.

    Semantic search hits are useful context, but they are not promises that
    every returned file must change. Literal paths are the only safe source of
    mandatory file targets for the deterministic alignment gate.
    """
    normalised = str(task or "").replace("\\", "/")
    paths = re.findall(
        r"(?<![A-Za-z0-9_.-])((?:[A-Za-z0-9_.-]+/)*[A-Za-z0-9_.-]+\.[A-Za-z0-9]+)"
        r"(?![A-Za-z0-9_.-])",
        normalised,
    )
    return sorted({p.strip("`'\"") for p in paths if "://" not in p})


# @memory:feature:PromptExport
# @memory:connects:AtlasLibrary, ChangeAlignment
# @memory:summary:Assembles a memory-grounded task pack; selected Library assets are composed in with exact id/version/content_hash provenance, so intents and alignment inherit which reusable context an agent actually ran on.
def _library_section(root: Path, assets: list[str] | None) -> dict | None:
    """Compose the selected Library assets. A composition failure must never
    take down the brief — the pack records the problem instead."""
    if not assets:
        return None
    from .library import compose_context

    try:
        result = compose_context(root, list(assets))
    except Exception as exc:  # keep the brief useful even if the library errs
        return {"selection": list(assets), "error": f"{type(exc).__name__}: {exc}",
                "assets": [], "warnings": [], "conflicts": []}
    return {"selection": list(assets), **result}


def build_task_pack(mem: CodebaseMemory, root: Path, task: str, top_k: int = 8,
                    assets: list[str] | None = None) -> dict:
    """Everything an AI (or human) needs to approach `task`, as structured data.

    ``assets`` selects Library refs (`id` or `id@N`; profiles expand) whose
    composed context joins the pack. The provenance of every asset used is
    recorded, so a run can be reproduced and audited later.
    """
    graph = mem.graph
    hits = mem.query_intent(task, top_k=top_k)

    targets = []
    feature_names: set[str] = set()
    files_seen: set[str] = set()
    for h in hits:
        if h.kind == "feature":
            feature_names.add(h.name)
            continue
        entry = {
            "kind": h.kind, "name": h.name, "path": h.path,
            "lines": h.lines or None, "score": h.score,
            "summary": h.summary, "calls": h.calls[:6], "called_by": h.called_by[:6],
        }
        node = graph.nodes.get(h.node_id, {})
        if node.get("anchors"):
            entry["anchors"] = node["anchors"]
        targets.append(entry)
        if h.path and h.path not in files_seen:
            files_seen.add(h.path)
            for _, feat, d in graph.out_edges(h.node_id, data=True):
                if d.get("type") == "PART_OF":
                    feature_names.add(graph.nodes[feat]["name"])

    features = []
    from .features import get_features

    for f in get_features(graph):
        if f["name"] in feature_names:
            features.append({
                "name": f["name"], "description": f.get("description", ""),
                "connects": f.get("connects", []),
                "flows": f.get("flows", [])[:3],
                "exercised_by": f.get("exercised_by", [])[:8],
                "review": (f.get("review") or {}),
            })

    impact = None
    primary = next((h for h in hits if h.kind in ("func", "class")), None) or \
              next((h for h in hits if h.kind == "file"), None)
    if primary:
        result = analyze_impact(graph, primary.node_id)
        if result:
            impact = {
                "target": result.target,
                "functions": result.functions[:12], "files": result.files[:12],
                "features": result.features, "tests": result.tests[:12],
            }

    annotations: list[dict] = []
    try:
        from . import config
        from .annotations import AnnotationStore

        store = AnnotationStore(root / config.MEMORY_DIR_NAME, root=root)
        for name in sorted(feature_names):
            annotations.extend(store.active_for_context(feature=name, limit=3))
        if not annotations and targets:
            node_ids = [f"file:{t['path']}" for t in targets if t.get("path")]
            annotations = store.active_for_context(targets=node_ids, limit=6)
    except Exception:
        pass

    words = set(re.findall(r"[a-z0-9]+", task.lower()))
    suggestions = []
    if graph.has_node("suggestions:app"):
        for s in graph.nodes["suggestions:app"].get("items") or []:
            text = (s["title"] + " " + s["description"]).lower()
            if words & set(re.findall(r"[a-z0-9]+", text)):
                suggestions.append(s)

    library = _library_section(root, assets)

    return {
        "task": task,
        "declared_paths": _declared_paths(task),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "project": root.name,
        "library": library,
        "relevant_code": targets,
        "features": features,
        "impact": impact,
        "active_annotations": annotations[:8],
        "related_suggestions": suggestions[:4],
        "conventions": [
            "Tag significant new functions/classes with @memory: anchors "
            "(# @memory:feature:Name / # @memory:connects:A, B / # @memory:summary:...).",
            "After changing code run `cms update` (or keep `cms watch` running) so the memory stays current.",
            "Run `cms verify <Feature>` after the change to confirm the mapped tests still pass; "
            "coverage proves execution, not complete behavioural correctness.",
            "Query the memory before grepping: `cms query \"...\"` or the MCP tools.",
        ],
        "verification": (
            [f"Run the mapped tests: pytest {' '.join(sorted({t.split('::')[0] for t in impact['tests']}))}"]
            if impact and impact["tests"] else ["Run the full suite: pytest tests/"]
        ) + [
            "Re-run `cms update` and check the affected feature traces still read correctly.",
            "If behaviour changed on purpose, update the @memory anchors so intent matches.",
        ],
    }


def _render_library(library: dict | None) -> list[str]:
    """The composed Library context: the reusable rules, preferences and skills
    this task runs under, with their exact versions named."""
    if not library:
        return []
    lines = ["## Library context (the assets this task runs under)"]
    if library.get("error"):
        return lines + [f"> WARNING: the library could not be composed — "
                        f"{library['error']}", ""]
    used = ", ".join(f"{a['id']}@{'v' + str(a['version']) if a['version'] else 'draft'}"
                     for a in library["assets"]) or "(nothing resolved)"
    lines.append(f"Loaded from the selection `{', '.join(library['selection'])}`: {used}.")
    if library.get("oversized"):
        lines.append(f"> WARNING: this context is large (~{library['est_tokens']} tokens) — "
                     "consider a narrower selection.")
    for w in library.get("warnings") or []:
        detail = ", ".join(f"{k}={v}" for k, v in w.items() if k != "kind")
        lines.append(f"> WARNING [{w['kind']}] {detail}")
    for c in library.get("conflicts") or []:
        lines.append(f"> CONFLICT: `{c['a']}` and `{c['b']}` declare each other "
                     f"incompatible (by {', '.join(c['declared_by'])}). Both are included "
                     "below — reconcile them deliberately, do not silently pick one.")
    lines.append("")
    for asset in library.get("assets") or []:
        version = f"v{asset['version']}" if asset.get("version") else "draft"
        lines.append(f"### [{asset['type']}] {asset['name']} "
                     f"({asset['id']}@{version} · {asset['scope']} · {asset['trust']})")
        if asset.get("content"):
            lines.append(asset["content"])
        lines.append("")
    return lines


def render_prompt(pack: dict) -> str:
    lines = [
        f"# Task: {pack['task']}",
        "",
        f"You are working on the **{pack['project']}** codebase. Everything below was "
        "generated from its live memory layer (structure, summaries, features, tests) — "
        "treat it as ground truth and read the referenced lines before editing.",
        "",
    ]
    lines += _render_library(pack.get("library"))
    lines.append("## Where to work")
    for t in pack["relevant_code"]:
        loc = f"{t['path']}:{t['lines']}" if t.get("lines") else t["path"]
        lines.append(f"### [{t['kind']}] `{t['name']}` — {loc}")
        if t.get("summary"):
            lines.append(t["summary"].strip())
        if t.get("anchors"):
            lines.append(f"- Declared intent: {json.dumps(t['anchors'])}")
        if t.get("called_by"):
            lines.append(f"- Called by: {', '.join(t['called_by'])}")
        if t.get("calls"):
            lines.append(f"- Calls: {', '.join(t['calls'])}")
        lines.append("")

    if pack["features"]:
        lines.append("## Features involved")
        for f in pack["features"]:
            lines.append(f"### {f['name']}")
            if f.get("description"):
                lines.append(f["description"])
            if f.get("connects"):
                lines.append(f"- Connects: {', '.join(f['connects'])}")
            for flow in f.get("flows", []):
                lines.append("- Flow: " + " -> ".join(f"{s['name']} ({s['path']}:{s['line']})" for s in flow))
            review = f.get("review") or {}
            if review.get("gaps"):
                lines.append("- Known gaps to respect: " + "; ".join(review["gaps"]))
            if f.get("exercised_by"):
                lines.append(f"- Exercised by: {', '.join(f['exercised_by'])}")
            lines.append("")

    if pack.get("impact"):
        imp = pack["impact"]
        lines += [
            "## Blast radius (what your change can break)",
            f"Changing `{imp['target']}` ripples into:",
            f"- Functions: {', '.join(imp['functions']) or '(none)'}",
            f"- Files: {', '.join(imp['files']) or '(none)'}",
            f"- Features: {', '.join(imp['features']) or '(none)'}",
            f"- Tests covering the chain: {', '.join(imp['tests']) or '(none — add some)'}",
            "",
        ]

    if pack.get("active_annotations"):
        lines.append("## Open annotations on this area (address or respect)")
        for a in pack["active_annotations"]:
            who = "model" if a.get("author") == "model" else "user"
            lines.append(f"- [{a['type']} · {who} · {a['status']}] {a['target']}: {a['body']}")
        lines.append("")

    if pack.get("related_suggestions"):
        lines.append("## Related planned work (align, don't duplicate)")
        for s in pack["related_suggestions"]:
            lines.append(f"- [ROI {s['roi']}x] {s['title']} — {s['description']}")
        lines.append("")

    lines.append("## Project conventions")
    lines += [f"- {c}" for c in pack["conventions"]]
    lines += ["", "## Verify when done"]
    lines += [f"{i}. {v}" for i, v in enumerate(pack["verification"], 1)]
    return "\n".join(lines)


def export_prompt(root: Path, task: str, as_json: bool = False, top_k: int = 8,
                  assets: list[str] | None = None) -> tuple[str, Path]:
    """Build and persist the prompt; returns (content, written_path)."""
    root = root.resolve()
    memory_dir = root / config.MEMORY_DIR_NAME
    mem = CodebaseMemory.load(memory_dir / "graph.json")
    pack = build_task_pack(mem, root, task, top_k=top_k, assets=assets)
    content = json.dumps(pack, indent=2) if as_json else render_prompt(pack)
    out_dir = memory_dir / "prompts"
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / f"{_slug(task)}.{'json' if as_json else 'md'}"
    out.write_text(content, encoding="utf-8")
    return content, out
