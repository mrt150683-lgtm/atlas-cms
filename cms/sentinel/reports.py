"""Sentinel Bug Report Generator — findings become structured, exportable bugs.

Every persistent finding already carries a stable ``bug_id`` (assigned by the
store on first detection). This module renders them as full bug reports with
likely cause and required regression tests, and exports the whole Sentinel
state as JSON or Markdown for humans, CI logs, or issue trackers.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from . import ACTIVE_STATUSES, SEVERITIES

_REGRESSION_HINTS = {
    "static_risk": "add a test that exercises the flagged code path with real inputs",
    "ledger": "rerun `cms verify` so exercised_by reflects reality, then re-audit the ledger",
    "contracts": "add a contract test asserting both sides agree (see tests/test_sentinel.py)",
    "workflows": "the failing check in cms/sentinel/workflows.py is the regression test — make it pass",
    "domain_rules": "keep `cms sentinel run` in the quality gate; the rule re-checks every scan",
    "providers": "extend tests/test_sentinel.py provider checks for the fixed behaviour",
}


def as_bug_report(finding: dict) -> dict:
    return {
        "bug_id": finding.get("bug_id", ""),
        "severity": finding.get("severity", "info"),
        "area": finding.get("area", ""),
        "feature": finding.get("feature", ""),
        "status": finding.get("status", "open"),
        "summary": finding.get("summary", ""),
        "evidence": {
            "module": finding.get("module", ""),
            "file": finding.get("file", ""),
            "line": finding.get("line"),
            "pattern": finding.get("pattern", ""),
            "details": finding.get("evidence", []),
            "execution_mode": finding.get("execution_mode", "development"),
        },
        "risk": finding.get("risk", ""),
        "likely_cause": f"{finding.get('file') or finding.get('module')} — {finding.get('pattern')}",
        "recommended_fix": finding.get("recommendation", ""),
        "required_regression_test": _REGRESSION_HINTS.get(finding.get("module", ""), ""),
        "status_reason": finding.get("status_reason", ""),
        "created_at": finding.get("first_seen", ""),
        "last_seen": finding.get("last_seen", ""),
    }


def _sorted(findings: dict[str, dict]) -> list[dict]:
    rank = {s: i for i, s in enumerate(SEVERITIES)}
    return sorted(
        findings.values(),
        key=lambda f: (rank.get(f.get("severity"), 99),
                       f.get("status") not in ACTIVE_STATUSES,
                       f.get("bug_id", "")),
    )


def export_json(scan: dict | None, findings: dict[str, dict]) -> str:
    return json.dumps({
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "scan": {k: v for k, v in (scan or {}).items() if k != "findings"},
        "bug_reports": [as_bug_report(f) for f in _sorted(findings)],
    }, indent=1)


# @memory:feature:HermesSentinel
# @memory:summary:Bug report generator — persistent findings rendered as structured bug reports (bug_id, severity, evidence, risk, fix, regression test) and exported as JSON or Markdown.
def export_markdown(scan: dict | None, findings: dict[str, dict]) -> str:
    scan = scan or {}
    counts = scan.get("active_counts") or {}
    lines = [
        "# Hermes Sentinel Report",
        "",
        f"*Scan `{scan.get('scan_id', '(none)')}` · {scan.get('created_at', '?')} · "
        f"mode: {scan.get('execution_mode', '?')}*",
        "",
        "| critical | high | medium | low | info |",
        "|---|---|---|---|---|",
        "| " + " | ".join(str(counts.get(s, 0)) for s in SEVERITIES) + " |",
        "",
    ]
    gate = scan.get("gate") or {}
    if gate:
        lines += [f"**Quality gate: {'FAILED' if gate.get('failed') else 'passed'}**"
                  + (f" — {'; '.join(gate.get('reasons', []))}" if gate.get("reasons") else ""), ""]
    for check in scan.get("workflow_checks") or []:
        mark = {True: "PASS", False: "FAIL", None: "MISSING"}[check.get("passed")]
        lines.append(f"- [{mark}] workflow `{check['name']}` — {check.get('actual', '')}")
    if scan.get("workflow_checks"):
        lines.append("")
    lines.append("## Bug reports")
    lines.append("")
    active = [f for f in _sorted(findings)]
    if not active:
        lines.append("(no findings on record)")
    for f in active:
        r = as_bug_report(f)
        lines += [
            f"### {r['bug_id'] or f.get('id')} · {r['severity'].upper()} · {r['status']}",
            "",
            r["summary"],
            "",
            f"- **area:** {r['area']}" + (f" · **feature:** {r['feature']}" if r["feature"] else ""),
            f"- **where:** {r['evidence']['file'] or r['evidence']['module']}"
            + (f":{r['evidence']['line']}" if r['evidence']['line'] else ""),
            f"- **mode:** {r['evidence']['execution_mode']}",
        ]
        if r["evidence"]["details"]:
            lines.append(f"- **evidence:** {'; '.join(str(d) for d in r['evidence']['details'][:3] if d)}")
        if r["risk"]:
            lines.append(f"- **risk:** {r['risk']}")
        if r["recommended_fix"]:
            lines.append(f"- **fix:** {r['recommended_fix']}")
        if r["required_regression_test"]:
            lines.append(f"- **regression test:** {r['required_regression_test']}")
        if r["status_reason"]:
            lines.append(f"- **status reason:** {r['status_reason']}")
        lines.append("")
    return "\n".join(lines)


def write_export(memory_dir: Path, scan: dict | None, findings: dict[str, dict],
                 fmt: str = "md") -> Path:
    out_dir = memory_dir / "sentinel" / "reports"
    out_dir.mkdir(parents=True, exist_ok=True)
    if fmt == "json":
        out = out_dir / "sentinel_report.json"
        out.write_text(export_json(scan, findings), encoding="utf-8")
    else:
        out = out_dir / "sentinel_report.md"
        out.write_text(export_markdown(scan, findings), encoding="utf-8")
    return out
