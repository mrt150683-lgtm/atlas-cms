"""Sentinel adapter for deterministic per-anchor intent drift findings."""

from __future__ import annotations

from pathlib import Path

from .. import config
from ..anchor_drift import detect_anchor_drift
from ..memory import CodebaseMemory
from . import make_finding


# @memory:feature:AnchorDrift
# @memory:feature:HermesSentinel
# @memory:summary:Adapts anchor-integrity mismatches into persistent fingerprint-keyed Sentinel findings.
def scan_anchor_drift(root: Path) -> list[dict]:
    graph_path = root / config.MEMORY_DIR_NAME / "graph.json"
    if not graph_path.is_file():
        return []
    report = detect_anchor_drift(CodebaseMemory.load(graph_path).graph, root)
    findings = []
    for drift in report.findings:
        summary = f"The stated intent no longer matches the code: {drift.message}"
        findings.append(make_finding(
            "anchor_drift",
            "medium" if drift.kind == "summary-symbol-drift" else "low",
            summary,
            area="anchor_integrity",
            feature=drift.feature,
            file=drift.path,
            line=drift.line,
            pattern=drift.kind,
            evidence=drift.evidence,
            risk=("Agents may trust a developer-authored statement that current source or "
                  "graph evidence no longer supports."),
            recommendation=("Update the anchor if intent changed, or restore code evidence "
                            "if the declared intent is still correct."),
            fingerprint_of="|".join((drift.node_id, drift.kind, drift.symbol,
                                     drift.feature, drift.related_feature)),
        ))
    return findings
