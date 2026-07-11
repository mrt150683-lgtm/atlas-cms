"""Sentinel Feature Ledger Auditor — claimed completion vs actual evidence.

The ledger is a human-maintained JSON file (``docs/feature_ledger.json``; the
repo had no completion ledger before Sentinel, so this is its smallest correct
structure). Each entry claims a status and lists evidence. The auditor checks
those claims against reality: evidence files must exist on disk, the feature
should exist in the memory graph, ``complete`` requires verifying tests
(graph ``exercised_by`` or listed test files) and no drift review verdict.
Features present in the graph but missing from the ledger are surfaced too.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

from . import make_finding
from .. import config

LEDGER_RELPATH = Path("docs") / "feature_ledger.json"
STATUSES = ("not_started", "in_progress", "blocked", "complete")


def ledger_path(root: Path) -> Path:
    return root / LEDGER_RELPATH


def load_ledger(root: Path) -> tuple[list[dict], list[str]]:
    """Returns (entries, parse_errors). Missing ledger -> ([], [])."""
    path = ledger_path(root)
    if not path.is_file():
        return [], []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return [], [f"{path.name}: {exc}"]
    entries = data.get("features") if isinstance(data, dict) else None
    if not isinstance(entries, list):
        return [], [f"{path.name}: expected an object with a 'features' list"]
    errors = []
    valid = []
    for i, entry in enumerate(entries):
        if not isinstance(entry, dict) or not entry.get("feature"):
            errors.append(f"entry {i}: missing 'feature' name")
            continue
        if entry.get("status") not in STATUSES:
            errors.append(f"{entry.get('feature')}: invalid status {entry.get('status')!r}")
        valid.append(entry)
    return valid, errors


def _tests_of(feat: dict | None) -> list:
    """Coverage evidence, accepting the pre-rename key from older graphs."""
    feat = feat or {}
    return feat.get("exercised_by") or feat.get("verified_by") or []


def _graph_features(root: Path) -> dict[str, dict]:
    graph_path = root / config.MEMORY_DIR_NAME / "graph.json"
    try:
        data = json.loads(graph_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return {
        n["name"]: n for n in data.get("nodes", [])
        if n.get("type") == "feature" and n.get("name")
    }


# @memory:feature:HermesSentinel
# @memory:connects:FeatureTracing, FeatureVerification
# @memory:summary:Feature ledger auditor — docs/feature_ledger.json claims are checked against graph evidence: files must exist, complete needs verifying tests and no drift verdict, unledgered graph features get flagged.
def audit_ledger(root: Path) -> list[dict]:
    entries, errors = load_ledger(root)
    findings = [
        make_finding(
            "ledger", "medium", f"feature ledger is malformed: {err}",
            area="ledger_integrity", file=str(LEDGER_RELPATH), pattern="parse-error",
            risk="Completion claims cannot be audited while the ledger is unreadable.",
            recommendation="Fix the JSON so every entry has a feature name and a valid status.",
        )
        for err in errors
    ]
    graph_feats = _graph_features(root)

    if not ledger_path(root).is_file():
        findings.append(make_finding(
            "ledger", "medium", "no feature completion ledger exists",
            area="ledger_missing", file=str(LEDGER_RELPATH), pattern="missing-ledger",
            risk="There is no auditable record of what is claimed complete.",
            recommendation="Run `cms sentinel ledger-init` to generate docs/feature_ledger.json from graph evidence.",
        ))
        return findings

    ledgered = set()
    for entry in entries:
        name = entry["feature"]
        ledgered.add(name)
        status = entry.get("status", "in_progress")
        evidence = entry.get("evidence") or {}
        listed_files = list(evidence.get("files") or [])
        listed_tests = list(evidence.get("tests") or [])

        missing_files = [f for f in listed_files if not (root / f).is_file()]
        real_files = [f for f in listed_files if f not in missing_files]
        if missing_files:
            findings.append(make_finding(
                "ledger", "high" if status == "complete" else "medium",
                f"{name}: {len(missing_files)} evidence file(s) do not exist",
                area="ledger_evidence", feature=name, file=str(LEDGER_RELPATH),
                pattern="missing-evidence-file", evidence=missing_files,
                risk="The ledger points at code that is not there — the claim is unbacked.",
                recommendation="Correct the paths or downgrade the feature status.",
            ))
        missing_tests = [t for t in listed_tests if not (root / t.split("::")[0]).is_file()]
        if missing_tests:
            findings.append(make_finding(
                "ledger", "high" if status == "complete" else "medium",
                f"{name}: {len(missing_tests)} listed test file(s) do not exist",
                area="ledger_evidence", feature=name, file=str(LEDGER_RELPATH),
                pattern="missing-evidence-test", evidence=missing_tests,
                risk="Claimed test coverage does not exist.",
                recommendation="Point at real test files or remove the claim.",
            ))

        graph_feat = graph_feats.get(name)
        if status == "complete":
            exercised = len(_tests_of(graph_feat))
            if not exercised and not (listed_tests and not missing_tests):
                findings.append(make_finding(
                    "ledger", "high",
                    f"{name} is marked complete but no test exercises it",
                    area="ledger_completion", feature=name, file=str(LEDGER_RELPATH),
                    pattern="complete-without-tests",
                    evidence=[f"graph exercised_by: {exercised}", f"ledger tests: {listed_tests or '(none)'}"],
                    risk="Completion is claimed on trust; regressions would go unnoticed.",
                    recommendation="Add tests and rerun `cms verify`, or set status to in_progress.",
                ))
            if graph_feat is None and not real_files:
                findings.append(make_finding(
                    "ledger", "high",
                    f"{name} is marked complete but has no integration evidence",
                    area="ledger_completion", feature=name, file=str(LEDGER_RELPATH),
                    pattern="complete-without-evidence",
                    risk="Nothing in the graph or on disk backs this feature.",
                    recommendation="Trace it (`cms trace`) or list its files, or downgrade the status.",
                ))
            verdict = ((graph_feat or {}).get("review") or {}).get("verdict")
            if verdict == "drift":
                findings.append(make_finding(
                    "ledger", "high",
                    f"{name} is marked complete but its AI review verdict is drift",
                    area="ledger_completion", feature=name, file=str(LEDGER_RELPATH),
                    pattern="complete-with-drift",
                    evidence=[((graph_feat or {}).get("review") or {}).get("headline", "")],
                    risk="Built behaviour contradicts declared intent.",
                    recommendation="Reconcile the implementation with the intent, then rerun `cms review`.",
                ))
        elif graph_feat is None and not real_files:
            findings.append(make_finding(
                "ledger", "low",
                f"{name} is in the ledger but not traced in the graph",
                area="ledger_evidence", feature=name, file=str(LEDGER_RELPATH),
                pattern="unbacked-entry",
                risk="Entry may be aspirational or stale.",
                recommendation="Add @memory:feature anchors and rerun `cms trace`, or remove the entry.",
            ))

    for name, feat in sorted(graph_feats.items()):
        if name not in ledgered:
            findings.append(make_finding(
                "ledger", "info",
                f"graph feature {name} has no ledger entry",
                area="ledger_coverage", feature=name, file=str(LEDGER_RELPATH),
                pattern="unledgered-feature",
                evidence=[f"source: {feat.get('source')}", f"tests: {len(_tests_of(feat))}"],
                recommendation="Add it to docs/feature_ledger.json (or `cms sentinel ledger-init` to regenerate).",
            ))
    return findings


def init_ledger(root: Path, overwrite: bool = False) -> Path:
    """Generate docs/feature_ledger.json from real graph evidence. Statuses are
    conservative: complete only when tests verify the feature, else in_progress."""
    path = ledger_path(root)
    if path.is_file() and not overwrite:
        raise FileExistsError(f"{path} already exists (use overwrite to regenerate)")
    entries = []
    for name, feat in sorted(_graph_features(root).items()):
        tests = _tests_of(feat)
        entries.append({
            "feature": name,
            "status": "complete" if tests else "in_progress",
            "evidence": {
                "files": sorted({
                    m.split(":", 1)[1].split("::")[0]
                    for m in (feat.get("members") or []) if ":" in m
                }),
                "database": [".memory/graph.json"],
                "api": [],
                "ui": [],
                "tests": tests[:12],
                "manual_verification": "",
            },
            "known_limitations": [] if tests else ["no verifying tests mapped yet"],
            "last_verified": date.today().isoformat(),
        })
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "about": "Feature completion ledger audited by Hermes Sentinel (cms sentinel). "
                 "Statuses: not_started | in_progress | blocked | complete.",
        "features": entries,
    }, indent=1), encoding="utf-8")
    return path
