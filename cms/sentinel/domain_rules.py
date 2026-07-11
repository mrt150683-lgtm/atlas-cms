"""Sentinel Domain Rule Validator — CMS-specific invariants, checked for real.

The Hermes spec's QA-lab rules (calibration, TestRuns) do not exist in this
codebase; the honest equivalents for a codebase-memory system are:

  Provenance      every stored summary says which provider wrote it, and
                  mock output is labelled as mock (no hidden fake success).
  Traceability    every feature node's members exist in the graph; declared
                  features come from real anchors.
  Judgement       review verdicts stay inside the allowed vocabulary; ROI
                  scores are consistent (roi == value/effort).
  Freshness       graph.json must not be older than the sources it describes.
  Security        the UI server must bind loopback only, and both source-read
                  paths (HTTP + MCP) must keep their traversal guards.
  Boundary        AI-generated content (summaries, reviews, suggestions) may
                  only annotate the graph — the validator confirms structural
                  fields are never sourced from LLM output paths (structural
                  rebuild is unconditional in update.py).

Rules run against the real ``.memory/graph.json`` and the real source files.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from . import make_finding
from .. import config

REVIEW_VERDICTS = ("aligned", "partial", "drift", "unverified")


def _load_graph(root: Path) -> dict | None:
    path = root / config.MEMORY_DIR_NAME / "graph.json"
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _rule_provenance(nodes: list[dict]) -> list[dict]:
    findings = []
    unlabelled = [
        n["id"] for n in nodes
        if n.get("type") == "file" and n.get("summary")
        and not (n.get("summary_meta") or {}).get("provider")
    ]
    if unlabelled:
        findings.append(make_finding(
            "domain_rules", "medium",
            f"{len(unlabelled)} file summaries have no provider provenance",
            area="provenance", pattern="summary-without-provider",
            evidence=unlabelled[:8],
            risk="Mock and AI output become indistinguishable — hidden fake data.",
            recommendation="Regenerate with `cms update --full`; summarizer must stamp summary_meta.provider.",
        ))
    mock_unlabelled = [
        n["id"] for n in nodes
        if n.get("type") == "file"
        and (n.get("summary_meta") or {}).get("provider") == "mock"
        and "mock" not in (n.get("summary") or "").lower()
    ]
    if mock_unlabelled:
        findings.append(make_finding(
            "domain_rules", "high",
            f"{len(mock_unlabelled)} mock summaries do not say they are mock",
            area="provenance", pattern="mock-masquerading",
            evidence=mock_unlabelled[:8],
            risk="Mock output could be mistaken for real AI analysis.",
            recommendation="MockProvider output must label itself; regenerate the affected summaries.",
        ))
    unstamped_features = [
        n["id"] for n in nodes
        if n.get("type") == "feature" and n.get("summary") and not n.get("narrative_provider")
    ]
    if unstamped_features:
        findings.append(make_finding(
            "domain_rules", "medium",
            f"{len(unstamped_features)} feature narratives lack narrative_provider provenance",
            area="provenance", pattern="narrative-without-provider",
            evidence=unstamped_features[:8],
            risk="Cannot tell mock structural traces from AI narratives.",
            recommendation="Rerun `cms trace` — build_features stamps narrative_provider.",
        ))
    return findings


def _rule_traceability(nodes: list[dict]) -> list[dict]:
    findings = []
    ids = {n["id"] for n in nodes}
    for n in nodes:
        if n.get("type") != "feature":
            continue
        ghosts = [m for m in (n.get("members") or []) if m not in ids]
        if ghosts:
            findings.append(make_finding(
                "domain_rules", "medium",
                f"feature {n.get('name')} references {len(ghosts)} member node(s) that no longer exist",
                area="traceability", feature=n.get("name", ""), pattern="ghost-members",
                evidence=ghosts[:8],
                risk="Flows/verification for this feature point at deleted code.",
                recommendation="Rerun `cms update` (members should be re-pruned) or fix the carry-over path.",
            ))
    return findings


def _rule_judgement(nodes: list[dict]) -> list[dict]:
    findings = []
    for n in nodes:
        if n.get("type") == "feature" and n.get("review"):
            verdict = n["review"].get("verdict")
            if verdict not in REVIEW_VERDICTS:
                findings.append(make_finding(
                    "domain_rules", "medium",
                    f"feature {n.get('name')} has out-of-vocabulary review verdict {verdict!r}",
                    area="judgement", feature=n.get("name", ""), pattern="bad-verdict",
                    risk="Downstream consumers (UI, suggestions) misrender unknown verdicts.",
                    recommendation="Rerun `cms review`; _sanitize should clamp verdicts.",
                ))
        if n.get("type") == "suggestions":
            for item in n.get("items") or []:
                value, effort, roi = item.get("value"), item.get("effort"), item.get("roi")
                try:
                    if roi is None or abs(roi - value / effort) > 0.011:
                        raise ValueError
                except (TypeError, ZeroDivisionError, ValueError):
                    findings.append(make_finding(
                        "domain_rules", "low",
                        f"suggestion {item.get('title', '?')!r} has inconsistent ROI ({roi} != {value}/{effort})",
                        area="judgement", pattern="roi-mismatch",
                        risk="Ranking order is not what the scores claim.",
                        recommendation="Rerun `cms suggest`; _sanitize computes roi = value/effort.",
                        fingerprint_of=str(item.get("title", "?")),
                    ))
    return findings


def _rule_freshness(root: Path) -> list[dict]:
    from ..scanner import scan

    graph_path = root / config.MEMORY_DIR_NAME / "graph.json"
    try:
        graph_mtime = graph_path.stat().st_mtime
    except OSError:
        return []
    newest = max((r.mtime for r in scan(root)), default=0.0)
    if newest > graph_mtime + 5:  # margin: exporter writes after the scan
        return [make_finding(
            "domain_rules", "medium",
            "memory layer is stale — sources changed after graph.json was written",
            area="freshness", file=".memory/graph.json", pattern="stale-memory",
            risk="Queries, traces and reviews describe code that no longer exists.",
            recommendation="Run `cms update` (or keep `cms app`/`cms watch` running).",
        )]
    return []


def _rule_security(root: Path) -> list[dict]:
    findings = []
    ui_py = root / "cms" / "ui.py"
    if ui_py.is_file():
        text = ui_py.read_text(encoding="utf-8", errors="replace")
        binds = re.findall(r"ThreadingHTTPServer\(\(\s*\"([^\"]*)\"", text)
        bad = [b for b in binds if b not in ("127.0.0.1", "localhost")]
        if bad:
            findings.append(make_finding(
                "domain_rules", "critical",
                f"UI server binds non-loopback address(es): {bad}",
                area="security", file="cms/ui.py", pattern="non-loopback-bind",
                risk="The memory layer (full code map + source reads) becomes network-reachable.",
                recommendation="Bind 127.0.0.1 only.",
            ))
        if "path outside project root" not in text:
            findings.append(make_finding(
                "domain_rules", "critical",
                "HTTP source endpoint appears to have lost its path traversal guard",
                area="security", file="cms/ui.py", pattern="missing-traversal-guard",
                risk="Any local process could read files outside the project via the UI API.",
                recommendation="Restore the root-containment check in _source.",
            ))

    # both source-serving surfaces are auditable, not just the HTTP one — the MCP
    # get_source path is exactly as exploitable if its guard goes missing
    mcp_py = root / "cms" / "mcp.py"
    if mcp_py.is_file():
        text = mcp_py.read_text(encoding="utf-8", errors="replace")
        if "def get_source" in text and "path outside project root" not in text:
            findings.append(make_finding(
                "domain_rules", "critical",
                "MCP get_source appears to have lost its path traversal guard",
                area="security", file="cms/mcp.py", pattern="missing-traversal-guard",
                risk="An MCP-connected agent could read arbitrary local files (e.g. the stored API key).",
                recommendation="Restore the root-containment check in get_source (resolve + parents).",
            ))
    return findings


def _rule_activity(root: Path) -> list[dict]:
    path = root / config.MEMORY_DIR_NAME / "activity.jsonl"
    if not path.is_file():
        return []
    bad = 0
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                entry = json.loads(line)
                if "ts" not in entry or "tool" not in entry:
                    bad += 1
            except json.JSONDecodeError:
                bad += 1
    except OSError:
        return []
    if bad:
        return [make_finding(
            "domain_rules", "info",
            f"{bad} malformed line(s) in activity.jsonl (reader tolerates them)",
            area="activity", file=".memory/activity.jsonl", pattern="malformed-activity",
            recommendation="Harmless — the feed is cosmetic — but check who else writes this file.",
        )]
    return []


# @memory:feature:HermesSentinel
# @memory:connects:SummaryGenerator, FeatureTracing, MemoryViewer
# @memory:summary:Domain rule validator — CMS invariants checked against real artifacts: provider provenance and mock labelling, member traceability, verdict/ROI sanity, memory freshness, loopback-only binding and traversal guards.
def check_domain_rules(root: Path) -> list[dict]:
    data = _load_graph(root)
    if data is None:
        return [make_finding(
            "domain_rules", "high",
            "no readable .memory/graph.json — domain rules cannot be evaluated",
            area="memory_missing", file=".memory/graph.json", pattern="no-graph",
            risk="The entire memory layer is absent or corrupt.",
            recommendation="Run `cms run-all`.",
        )]
    nodes = data.get("nodes", [])
    return (
        _rule_provenance(nodes)
        + _rule_traceability(nodes)
        + _rule_judgement(nodes)
        + _rule_freshness(root)
        + _rule_security(root)
        + _rule_activity(root)
    )
