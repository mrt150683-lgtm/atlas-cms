"""Hermes Sentinel — CMS's built-in bug-finding and completion-gate system.

Sentinel audits the codebase the memory layer describes: it inventories the
project, scans for risky patterns, audits the feature completion ledger
against real graph evidence, checks frontend/backend/MCP contracts, executes
workflow checks against the actual pipeline, validates CMS domain invariants
and the LLM provider (driver) layer, and turns confirmed problems into
structured bug reports. Results persist under ``.memory/sentinel/`` and a
configurable quality gate fails on critical findings.

Modules map the Hermes Sentinel spec onto the real CMS architecture:
inventory (Project Scanner), ledger (Feature Ledger Auditor), static_risk
(Static Risk Scanner), contracts (Contract Checker), workflows (Workflow Test
Runner), domain_rules (Domain Rule Validator), providers_check (Driver/Plugin
Validator -> LLM providers), reports (Bug Report Generator), store
(persistence + Regression Tracker), runner (orchestration + quality gate).
"""

from __future__ import annotations

import hashlib

SEVERITIES = ("critical", "high", "medium", "low", "info")
FINDING_STATUSES = (
    "open",
    "acknowledged",
    "fixed_pending_verification",
    "resolved",
    "false_positive",
)
# statuses that still count against the quality gate
ACTIVE_STATUSES = ("open", "acknowledged", "fixed_pending_verification")


def make_finding(
    module: str,
    severity: str,
    summary: str,
    *,
    area: str = "",
    feature: str = "",
    file: str = "",
    line: int | None = None,
    pattern: str = "",
    evidence: list[str] | None = None,
    risk: str = "",
    recommendation: str = "",
    execution_mode: str = "development",
    fingerprint_of: str | None = None,
) -> dict:
    """One Sentinel finding. The fingerprint identifies the *problem* (not the
    scan or the line number, which shift between runs) so statuses persist.
    Pass ``fingerprint_of`` when the summary contains unstable text like line
    numbers — e.g. the matched line's content."""
    if severity not in SEVERITIES:
        severity = "info"
    fingerprint = hashlib.sha1(
        "|".join((module, area, pattern, file, fingerprint_of if fingerprint_of is not None else summary)).encode("utf-8")
    ).hexdigest()[:16]
    return {
        "id": f"SEN-{fingerprint[:10]}",
        "fingerprint": fingerprint,
        "module": module,
        "severity": severity,
        "area": area,
        "feature": feature,
        "file": file,
        "line": line,
        "pattern": pattern,
        "summary": summary[:400],
        "evidence": [str(e)[:400] for e in (evidence or [])[:10]],
        "risk": risk[:400],
        "recommendation": recommendation[:400],
        "execution_mode": execution_mode,
    }
