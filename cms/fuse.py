"""Constellation — cross-project fusion: Atlas's multi-codebase discovery side.

Every project Atlas maps gets registered (``~/.cms/projects.json``). ``cms
fuse`` distils each mapped project into a compact **project card** from its
EXISTING artifacts (graph features, review headline, languages, external
deps — zero re-summarization), detects **structural overlaps**
deterministically, then asks a real provider for a **fusion report**: how the
codebases could integrate, what new features only the combination enables,
and where they conflict.

Honesty rules (same contract as the per-project semantic layer):
- only projects with positively recorded feature discovery are fused;
  excluded projects are listed with the reason, never silently dropped;
- a real provider is required — mock cannot author a fusion report;
- provider failure / malformed output raises ``FusionError``; it is never
  converted into an empty "success";
- the report records each member's ``feature_set_hash``, so it is
  verifiably stale once any member project's features drift;
- structural overlaps carry ``provenance: structural``; everything the LLM
  wrote is ``provenance: llm`` — plan material, not ground truth.
"""

from __future__ import annotations

import json
import re
import time
from collections import Counter
from pathlib import Path

from . import config
from . import semantic_state as ss
from .providers import SummaryProvider

REGISTRY_PATH = Path.home() / ".cms" / "projects.json"
FUSION_DIR = Path.home() / ".cms" / "fusion"
FUSION_MAX_TOKENS = 4000

FUSION_PROMPT = """You are a principal architect reviewing SEVERAL separate codebases owned by one team.
Below is one evidence card per project: its purpose, mapped features (with entry files), languages and key dependencies, plus deterministic structural overlaps already detected between them.

Propose how these codebases could work TOGETHER. Return ONLY a JSON object:
{{
 "integrations": [{{"title": str, "projects": [names], "features": [feature names used], "description": str, "first_step": str}}],
 "emergent": [{{"title": str, "projects": [names], "description": str}}],
 "conflicts": [{{"title": str, "projects": [names], "features": [names], "description": str, "resolution_hint": str}}]
}}

Rules: max {max_items} items per list. Every item must name >= 2 projects. Integrations must cite real feature names from the cards. Conflicts = overlapping/competing capabilities that would clash in a merged or interoperating system (include the structural overlaps below if they are real clashes). Be concrete and unsentimental; no marketing language.

PROJECT CARDS:
{cards}

STRUCTURAL OVERLAPS (deterministic):
{overlaps}
"""


class FusionError(RuntimeError):
    """Real-provider fusion failed (transport or malformed output)."""


# ── registry ─────────────────────────────────────────────────────────────

def load_registry() -> dict:
    try:
        return json.loads(REGISTRY_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def register_project(root: Path) -> None:
    """Record a mapped project (called after every successful build). Silent
    on failure — the registry is convenience, never load-bearing."""
    try:
        root = Path(root).resolve()
        reg = load_registry()
        projects = reg.setdefault("projects", {})
        projects[str(root)] = {
            "name": root.name,
            "last_built": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }
        REGISTRY_PATH.parent.mkdir(parents=True, exist_ok=True)
        ss.atomic_write_json(REGISTRY_PATH, reg)
    except OSError:
        pass


# ── project cards ────────────────────────────────────────────────────────

def build_card(root: Path) -> dict:
    """Compact evidence card for one mapped project, from existing artifacts.

    Returns {"ready": False, "reason": …} when the project lacks positively
    recorded feature discovery — fusion inputs must be evidenced, and an
    excluded project must be explainable."""
    root = Path(root).resolve()
    memory_dir = root / config.MEMORY_DIR_NAME
    graph_path = memory_dir / "graph.json"
    if not graph_path.is_file():
        return {"name": root.name, "root": str(root), "ready": False,
                "reason": "no memory layer (run cms run-all)"}
    state = ss.load_state(memory_dir)
    feat_rec = ss.stage(state, "features")
    if feat_rec.get("status") != "complete":
        return {"name": root.name, "root": str(root), "ready": False,
                "reason": f"feature discovery not positively recorded "
                          f"(state: {feat_rec.get('status')})"}
    try:
        data = json.loads(graph_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return {"name": root.name, "root": str(root), "ready": False,
                "reason": f"graph unreadable: {exc}"}

    nodes = data.get("nodes", [])
    features = []
    for n in nodes:
        if n.get("type") != "feature":
            continue
        entry_files = sorted({
            e.split(":", 1)[1].split("::")[0]
            for e in (n.get("entry_points") or []) if ":" in e
        })[:3]
        features.append({
            "name": n.get("name"), "source": n.get("source"),
            "description": (n.get("description") or "")[:220],
            "entry_files": entry_files,
        })
    langs = Counter(n.get("language") for n in nodes
                    if n.get("type") == "file" and n.get("language"))
    ext = sorted({n.get("name") for n in nodes if n.get("type") == "external"})
    review = next((n for n in nodes if n.get("id") == "review:app"), {})
    return {
        "name": root.name, "root": str(root), "ready": True,
        "purpose": (review.get("headline") or "")[:300],
        "verdict": review.get("verdict"),
        "features": sorted(features, key=lambda f: f["name"]),
        "languages": dict(langs.most_common(5)),
        "external_deps": ext[:12],
        "files": sum(1 for n in nodes if n.get("type") == "file"),
        "feature_set_hash": feat_rec.get("feature_set_hash"),
    }


# ── structural overlaps (deterministic, provenance: structural) ─────────

_GENERIC_TOKENS = {"system", "management", "integration", "engine", "service",
                   "application", "interface", "module", "component", "based"}


def _tokens(name: str) -> set[str]:
    return {t.lower() for t in re.findall(r"[A-Z][a-z]+|[a-z]{3,}|[A-Z]{2,}", name or "")
            if t.lower() not in _GENERIC_TOKENS and len(t) > 2}


def structural_overlaps(cards: list[dict]) -> list[dict]:
    """Deterministic cross-project overlap signals, each with evidence."""
    out: list[dict] = []
    ready = [c for c in cards if c.get("ready")]
    for i, a in enumerate(ready):
        for b in ready[i + 1:]:
            a_names = {f["name"] for f in a["features"]}
            b_names = {f["name"] for f in b["features"]}
            for name in sorted(a_names & b_names):
                out.append({"kind": "same-feature-name", "provenance": "structural",
                            "projects": [a["name"], b["name"]],
                            "evidence": f"both define a feature named {name!r}"})
            for fa in a["features"]:
                for fb in b["features"]:
                    if fa["name"] == fb["name"]:
                        continue
                    shared = _tokens(fa["name"]) & _tokens(fb["name"])
                    if len(shared) >= 1 and shared - {"data", "file", "user"}:
                        out.append({
                            "kind": "related-feature-domain", "provenance": "structural",
                            "projects": [a["name"], b["name"]],
                            "evidence": f"{a['name']}:{fa['name']} ~ {b['name']}:{fb['name']} "
                                        f"(shared domain: {', '.join(sorted(shared))})",
                        })
            shared_deps = set(a["external_deps"]) & set(b["external_deps"])
            if shared_deps:
                out.append({"kind": "shared-dependencies", "provenance": "structural",
                            "projects": [a["name"], b["name"]],
                            "evidence": f"both depend on: {', '.join(sorted(shared_deps)[:8])}"})
    return out[:40]


# ── fusion synthesis ─────────────────────────────────────────────────────

def build_fusion(roots: list[Path], provider: SummaryProvider,
                 max_items: int = 6) -> dict:
    """Build the cross-project fusion report. Raises FusionError on provider
    failure or malformed output; refuses to run under mock."""
    if provider.name == "mock":
        raise FusionError("fusion requires a real provider — mock cannot author "
                          "integration analysis (configure an API key)")
    cards = [build_card(r) for r in roots]
    ready = [c for c in cards if c.get("ready")]
    excluded = [c for c in cards if not c.get("ready")]
    if len(ready) < 2:
        raise FusionError(
            f"fusion needs >= 2 projects with recorded feature discovery; "
            f"ready: {[c['name'] for c in ready]}, excluded: "
            f"{[(c['name'], c['reason']) for c in excluded]}")

    overlaps = structural_overlaps(ready)
    prompt = FUSION_PROMPT.format(
        max_items=max_items,
        cards=json.dumps([{k: v for k, v in c.items() if k not in ("root", "ready")}
                          for c in ready], indent=1),
        overlaps=json.dumps(overlaps, indent=1) or "(none)",
    )
    try:
        raw = provider.summarize(prompt, {"max_tokens": FUSION_MAX_TOKENS})
    except Exception as exc:
        raise FusionError(f"provider call failed: {type(exc).__name__}: {exc}") from exc
    match = re.search(r"\{[\s\S]*\}", raw)
    if match is None:
        raise FusionError("provider returned no JSON object (malformed fusion output)")
    try:
        parsed = json.loads(match.group(0))
    except json.JSONDecodeError as exc:
        raise FusionError(f"provider returned invalid JSON: {exc}") from exc

    def _items(key):
        items = parsed.get(key) or []
        return [dict(i, provenance="llm") for i in items[:max_items] if isinstance(i, dict)]

    report = {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "provider": provider.name, "model": getattr(provider, "model", None),
        "projects": {c["name"]: {"root": c["root"],
                                 "feature_set_hash": c["feature_set_hash"],
                                 "features": len(c["features"])} for c in ready},
        "excluded": [{"name": c["name"], "reason": c["reason"]} for c in excluded],
        "structural_overlaps": overlaps,
        "integrations": _items("integrations"),
        "emergent": _items("emergent"),
        "conflicts": _items("conflicts"),
    }
    FUSION_DIR.mkdir(parents=True, exist_ok=True)
    ss.atomic_write_json(FUSION_DIR / "latest.json", report)
    (FUSION_DIR / "latest.md").write_text(render_fusion_md(report), encoding="utf-8")
    return report


def fusion_staleness(report: dict) -> list[str]:
    """Which member projects' feature sets drifted since the report."""
    stale = []
    for name, info in (report.get("projects") or {}).items():
        try:
            state = ss.load_state(Path(info["root"]) / config.MEMORY_DIR_NAME)
            current = ss.stage(state, "features").get("feature_set_hash")
            if current != info.get("feature_set_hash"):
                stale.append(name)
        except OSError:
            stale.append(name)
    return stale


def render_fusion_md(report: dict) -> str:
    lines = ["# Constellation — cross-project fusion report",
             f"\n*{report['generated_at']} · {report['provider']}"
             f"{' · ' + report['model'] if report.get('model') else ''} · "
             f"LLM sections are plan material, not ground truth*\n",
             "## Projects"]
    for name, info in report["projects"].items():
        lines.append(f"- **{name}** — {info['features']} features "
                     f"(`{info['feature_set_hash']}`)")
    for c in report.get("excluded", []):
        lines.append(f"- ~~{c['name']}~~ — excluded: {c['reason']}")
    sections = [("integrations", "Integration opportunities"),
                ("emergent", "Emergent features (only possible combined)"),
                ("conflicts", "Conflicts & overlaps")]
    for key, title in sections:
        lines.append(f"\n## {title}")
        items = report.get(key) or []
        if not items:
            lines.append("(none proposed)")
        for i in items:
            lines.append(f"- **{i.get('title', '?')}** [{', '.join(i.get('projects', []))}] — "
                         f"{i.get('description', '')}")
            if i.get("first_step"):
                lines.append(f"  - first step: {i['first_step']}")
            if i.get("resolution_hint"):
                lines.append(f"  - resolution: {i['resolution_hint']}")
    lines.append("\n## Structural overlaps (deterministic)")
    for o in report.get("structural_overlaps", []) or ["(none)"]:
        lines.append(f"- [{o['kind']}] {o['evidence']}" if isinstance(o, dict) else f"- {o}")
    return "\n".join(lines) + "\n"
