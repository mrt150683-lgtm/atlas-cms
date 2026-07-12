"""Suggestions engine — notice and plan what's worth building next.

Studies the memory itself (feature verdicts and gaps, untested features, git
churn hotspots, hidden coupling) and proposes ranked suggestions, each scored
value (1-5) vs effort (1-5) with ROI = value/effort — highest return on
investment first. LLM-written when a provider is available; a deterministic
structural pass otherwise. Stored on a ``suggestions:app`` node and exported
to ``.memory/suggestions.md``.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import networkx as nx

from .features import get_features
from .providers import SummaryProvider

KINDS = ("new-feature", "improvement", "hardening")

SUGGEST_PROMPT = """You are a pragmatic principal engineer planning the next moves for this app.
Study the evidence and propose the suggestions with the HIGHEST return on investment.

App context:
{app_context}

Existing features (verdict · gaps · tests):
{feature_lines}

Git hotspots (most-changed files): {hotspots}
Hidden coupling (files that change together without imports): {coupling}
Features with no mapped exercising tests: {untested}

Propose 5-8 suggestions. Respond with ONLY a JSON array:
[{{
  "title": "<short imperative title>",
  "kind": "new-feature" | "improvement" | "hardening",
  "description": "<2-3 sentences: what to build and why the user benefits>",
  "rationale": "<1-2 sentences grounded ONLY in the evidence above>",
  "value": <1-5, user impact>,
  "effort": <1-5, cost to build>,
  "builds_on": ["<existing feature names it extends>"]
}}]

Rules: ground every suggestion in the evidence; never propose what already exists;
prefer high value / low effort; be specific, not generic advice.
"""


def _evidence(graph: nx.DiGraph, root: Path) -> dict:
    features = get_features(graph)
    feature_lines = []
    untested = []
    for f in features:
        r = f.get("review") or {}
        gaps = "; ".join((r.get("gaps") or [])[:2]) or "none noted"
        tests = len(f.get("exercised_by") or [])
        feature_lines.append(
            f"- {f['name']}: {r.get('verdict', 'unreviewed')} · gaps: {gaps} · {tests} test(s)"
        )
        if not tests:
            untested.append(f["name"])

    hot = sorted(
        ((a["path"], a["git"]["commits"], a["git"]["churn"])
         for _, a in graph.nodes(data=True)
         if a.get("type") == "file" and a.get("git")),
        key=lambda x: -x[1],
    )[:6]
    hotspots = ", ".join(f"{p} ({c} commits)" for p, c, _ in hot) or "(no git history)"

    coupling = ", ".join(
        f"{graph.nodes[u]['path']}↔{graph.nodes[v]['path']}"
        for u, v, d in graph.edges(data=True) if d.get("type") == "CO_CHANGES"
    )[:400] or "(none detected)"

    readme = root / "README.md"
    app_context = "\n".join(
        readme.read_text(encoding="utf-8", errors="replace").splitlines()[:25]
    ) if readme.is_file() else "(no README)"

    return {
        "app_context": app_context,
        "feature_lines": "\n".join(feature_lines) or "(no features)",
        "hotspots": hotspots,
        "coupling": coupling,
        "untested": ", ".join(untested) or "(all tested)",
        "untested_list": untested,
    }


def _sanitize(item: dict) -> dict | None:
    title = str(item.get("title", "")).strip()
    if not title:
        return None
    try:
        value = max(1, min(5, int(item.get("value", 3))))
        effort = max(1, min(5, int(item.get("effort", 3))))
    except (TypeError, ValueError):
        value, effort = 3, 3
    return {
        "title": title[:120],
        "kind": item.get("kind") if item.get("kind") in KINDS else "improvement",
        "description": str(item.get("description", ""))[:600],
        "rationale": str(item.get("rationale", ""))[:400],
        "value": value,
        "effort": effort,
        "roi": round(value / effort, 2),
        "builds_on": [str(b)[:60] for b in (item.get("builds_on") or [])[:4]],
    }


def _structural_suggestions(graph: nx.DiGraph, evidence: dict) -> list[dict]:
    """No-LLM fallback: deterministic suggestions from graph facts."""
    out = []
    features = {f["name"]: f for f in get_features(graph)}
    for name in evidence["untested_list"][:4]:
        conns = len(features.get(name, {}).get("connects", []))
        out.append(_sanitize({
            "title": f"Add tests exercising {name}",
            "kind": "hardening",
            "description": f"{name} has no tests mapped as exercising its implementation; "
                           "cms verify cannot show it is exercised until tests are mapped.",
            "rationale": f"0 mapped tests; {conns} declared connection(s) make it load-bearing.",
            "value": min(5, 2 + conns // 2), "effort": 2, "builds_on": [name],
        }))
    for u, v, d in graph.edges(data=True):
        if d.get("type") == "CO_CHANGES" and len(out) < 8:
            out.append(_sanitize({
                "title": f"Review hidden coupling {graph.nodes[u]['path']} <-> {graph.nodes[v]['path']}",
                "kind": "improvement",
                "description": "These files change together without an import relationship — "
                               "an undeclared contract worth making explicit or breaking.",
                "rationale": f"Co-changed in {d.get('weight', '?')} commits with no static dependency.",
                "value": 3, "effort": 2, "builds_on": ["GitHistoryLayer"],
            }))
    return [s for s in out if s]


def build_suggestions(graph: nx.DiGraph, root: Path, provider: SummaryProvider) -> list[dict]:
    evidence = _evidence(graph, root)
    suggestions: list[dict] = []
    if provider.name != "mock":
        try:
            raw = provider.summarize(SUGGEST_PROMPT.format(**{
                k: v for k, v in evidence.items() if k != "untested_list"
            }), {})
            match = re.search(r"\[[\s\S]*\]", raw)
            items = json.loads(match.group(0)) if match else []
            suggestions = [s for s in (_sanitize(i) for i in items[:8] if isinstance(i, dict)) if s]
        except Exception:
            suggestions = []
    if not suggestions:
        suggestions = _structural_suggestions(graph, evidence)
    suggestions.sort(key=lambda s: (-s["roi"], -s["value"], s["title"]))
    graph.add_node(
        "suggestions:app", type="suggestions", name="Suggested Features",
        items=suggestions, provider=provider.name,
        summary="; ".join(s["title"] for s in suggestions[:5]),
    )
    return suggestions


def export_suggestions(graph: nx.DiGraph, memory_dir: Path) -> Path | None:
    if not graph.has_node("suggestions:app"):
        return None
    items = graph.nodes["suggestions:app"].get("items") or []
    lines = ["# Suggested Next — ranked by return on investment\n"]
    for i, s in enumerate(items, 1):
        lines += [
            f"## {i}. {s['title']}  `ROI {s['roi']}×`",
            f"*{s['kind']} · value {s['value']}/5 · effort {s['effort']}/5"
            + (f" · builds on: {', '.join(s['builds_on'])}" if s["builds_on"] else "") + "*\n",
            s["description"], "",
            f"> {s['rationale']}", "",
        ]
    out = memory_dir / "suggestions.md"
    out.write_text("\n".join(lines), encoding="utf-8")
    return out
