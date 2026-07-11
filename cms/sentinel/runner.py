"""Sentinel runner — orchestrates every module and applies the quality gate.

``run_scan`` executes all Sentinel modules against a project root, records
which modules ran (and which errored — an errored module never silently
passes), merges findings into the persistent store, and evaluates the gate:
by default any *active* critical finding fails the scan (exit non-zero from
the CLI). Thresholds and ignore paths live in ``sentinel.config.json`` at the
repo root, if present.

Execution mode is recorded on every scan: CMS is a development tool whose
only live/mock split is the LLM provider, so mode is ``mock`` unless a real
provider is configured (``live``); workflow checks always run in mock mode
and label themselves accordingly.
"""

from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path

from . import ACTIVE_STATUSES, SEVERITIES, make_finding
from .. import config
from .store import SentinelStore, new_scan_id

CONFIG_FILENAME = "sentinel.config.json"
DEFAULT_CONFIG = {
    "fail_on": ["critical"],
    "warn_on": ["high", "medium"],
    # scanner ignores (node_modules, .git, dist, build, .memory, …) already
    # apply; these are extra sentinel-only exclusions
    "ignore_paths": [],
}

MODULES = ("inventory", "static_risk", "ledger", "contracts",
           "workflows", "domain_rules", "providers")


def load_config(root: Path) -> dict:
    cfg = dict(DEFAULT_CONFIG)
    path = root / CONFIG_FILENAME
    if path.is_file():
        try:
            user = json.loads(path.read_text(encoding="utf-8"))
            for key in DEFAULT_CONFIG:
                if key in user:
                    cfg[key] = user[key]
        except (OSError, json.JSONDecodeError):
            pass
    return cfg


def _execution_mode() -> str:
    provider = os.environ.get(config.ENV_PROVIDER, "").lower()
    if provider and provider != "mock":
        return "live"
    if not provider and os.environ.get("ANTHROPIC_API_KEY"):
        return "live"
    return "mock"


def evaluate_gate(findings: dict[str, dict], cfg: dict) -> dict:
    """Gate over ACTIVE findings only (resolved / false_positive don't count)."""
    fail_on = set(cfg.get("fail_on") or [])
    warn_on = set(cfg.get("warn_on") or [])
    reasons, warnings = [], []
    counts = {s: 0 for s in SEVERITIES}
    for f in findings.values():
        if f.get("status") not in ACTIVE_STATUSES:
            continue
        sev = f.get("severity", "info")
        counts[sev] = counts.get(sev, 0) + 1
        label = f"{f.get('bug_id') or f.get('id')}: {f.get('summary', '')[:100]}"
        if sev in fail_on:
            reasons.append(label)
        elif sev in warn_on:
            warnings.append(label)
    return {
        "failed": bool(reasons),
        "fail_on": sorted(fail_on),
        "reasons": reasons[:20],
        "warnings": warnings[:20],
        "active_counts": counts,
    }


# @memory:feature:HermesSentinel
# @memory:connects:CleanDirectoryScanner, IncrementalUpdates
# @memory:summary:Sentinel orchestrator — runs every scanner module (errors recorded, never silently passed), merges findings into the persistent store, and applies the configurable quality gate (fail on active criticals).
def run_scan(root: Path, modules: tuple[str, ...] = MODULES, echo=lambda *_: None) -> tuple[dict, dict[str, dict]]:
    """Run Sentinel. Returns (scan, all persistent findings after merge)."""
    root = root.resolve()
    cfg = load_config(root)
    started = time.time()
    scan: dict = {
        "scan_id": new_scan_id(),
        "created_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "repo_root": str(root),
        "execution_mode": _execution_mode(),
        "config": cfg,
        "modules_run": [],
        "module_errors": {},
        "findings": [],
        "workflow_checks": [],
    }

    ignored = tuple(cfg.get("ignore_paths") or [])

    def run(name: str, fn) -> None:
        if name not in modules:
            return
        echo(f"  sentinel [{name}] …")
        try:
            fn()
            scan["modules_run"].append(name)
        except Exception as exc:
            scan["modules_run"].append(name)
            scan["module_errors"][name] = f"{type(exc).__name__}: {exc}"

    def add(findings: list[dict]) -> None:
        for f in findings:
            if ignored and f.get("file", "").startswith(ignored):
                continue
            f["execution_mode"] = f.get("execution_mode") or scan["execution_mode"]
            scan["findings"].append(f)

    from ..scanner import scan as scan_files

    records = scan_files(root)

    def _inventory():
        from .inventory import build_inventory

        scan["inventory"] = build_inventory(root, records)
        scan["detected_stack"] = scan["inventory"]["detected_stack"]
        add([
            make_finding("inventory", "medium", w, area="inventory", pattern="inventory-warning")
            for w in scan["inventory"].get("warnings", [])
        ])

    def _static():
        from .static_risk import scan_static_risks

        add(scan_static_risks(root, records))

    def _ledger():
        from .ledger import audit_ledger

        add(audit_ledger(root))

    def _contracts():
        from .contracts import check_contracts

        add(check_contracts(root))

    def _workflows():
        from .workflows import run_workflow_checks

        results, findings = run_workflow_checks(root)
        scan["workflow_checks"] = results
        add(findings)

    def _rules():
        from .domain_rules import check_domain_rules

        add(check_domain_rules(root))

    def _providers():
        from .providers_check import check_providers

        add(check_providers(root))

    run("inventory", _inventory)
    run("static_risk", _static)
    run("ledger", _ledger)
    run("contracts", _contracts)
    run("workflows", _workflows)
    run("domain_rules", _rules)
    run("providers", _providers)

    for name, err in scan["module_errors"].items():
        scan["findings"].append(make_finding(
            "runner", "high", f"sentinel module {name} crashed: {err}",
            area="sentinel_self", pattern=f"module-error-{name}",
            risk="A scanner that cannot run cannot clear anything — treat its area as unaudited.",
            recommendation="Fix the module error; a bug-finding system that cannot prove its own work is just another bug.",
        ))

    scan["duration_s"] = round(time.time() - started, 2)
    store = SentinelStore(root / config.MEMORY_DIR_NAME)
    scan["gate"] = {}  # placeholder so merge writes history with a gate key
    merged = store.merge_scan(scan)
    gate = evaluate_gate(merged, cfg)
    scan["gate"] = gate
    # persist the gate result on the saved artifacts too
    store._write(store.latest_path, scan)
    history = store.scan_history()
    if history:
        history[-1]["gate"] = gate
        store._write(store.scans_path, history)
    return scan, merged
