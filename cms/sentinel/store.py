"""Sentinel persistence — findings, scans and bug ids under .memory/sentinel/.

Findings are keyed by fingerprint so their status (open / acknowledged /
fixed_pending_verification / resolved / false_positive) and false-positive
reasons survive across scans and app restarts. Scan summaries are kept as a
capped history; the latest full scan is stored whole. This doubles as the
Regression Tracker: a finding that disappears while a module ran clean is
auto-resolved, and one that comes back is reopened with its history intact.
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path

from . import ACTIVE_STATUSES, FINDING_STATUSES

SENTINEL_DIR = "sentinel"
MAX_SCAN_HISTORY = 20


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class SentinelStore:
    def __init__(self, memory_dir: Path) -> None:
        self.dir = memory_dir / SENTINEL_DIR
        self.findings_path = self.dir / "findings.json"
        self.scans_path = self.dir / "scans.json"
        self.latest_path = self.dir / "latest.json"

    # -- raw io -----------------------------------------------------------

    def _read(self, path: Path, default):
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return default

    def _write(self, path: Path, data) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=1), encoding="utf-8")
        tmp.replace(path)

    # -- findings ---------------------------------------------------------

    def load_findings(self) -> dict[str, dict]:
        """fingerprint -> finding (with status/first_seen/last_seen/bug_id)."""
        return self._read(self.findings_path, {})

    def merge_scan(self, scan: dict) -> dict[str, dict]:
        """Fold a scan's findings into the persistent set and save everything.

        New fingerprints open with a fresh bug id; re-detected ones keep their
        status/history (a resolved or false_positive finding that reappears is
        reopened — regression). Findings NOT re-detected are auto-resolved only
        when the module that owns them completed without error this scan.
        """
        stored = self.load_findings()
        seen = set()
        next_bug = 1 + max(
            (int(f.get("bug_id", "BUG-0").split("-")[-1]) for f in stored.values()),
            default=0,
        )
        for finding in scan.get("findings", []):
            fp = finding["fingerprint"]
            seen.add(fp)
            if fp in stored:
                old = stored[fp]
                finding = {**old, **finding,
                           "status": old.get("status", "open"),
                           "status_reason": old.get("status_reason", ""),
                           "first_seen": old.get("first_seen", _now_iso()),
                           "bug_id": old.get("bug_id", "")}
                if old.get("status") == "resolved":  # false_positive stays put
                    finding["status"] = "open"  # regression: it came back
                    finding["status_reason"] = "reopened — re-detected after being resolved"
            else:
                finding = {**finding, "status": "open", "status_reason": "",
                           "first_seen": _now_iso(), "bug_id": f"BUG-{next_bug:06d}"}
                next_bug += 1
            finding["last_seen"] = _now_iso()
            stored[fp] = finding

        clean_modules = {
            m for m in scan.get("modules_run", [])
            if m not in (scan.get("module_errors") or {})
        }
        for fp, finding in stored.items():
            if (fp not in seen and finding.get("module") in clean_modules
                    and finding.get("status") in ACTIVE_STATUSES):
                finding["status"] = "resolved"
                finding["status_reason"] = "no longer detected (module ran clean)"

        self._write(self.findings_path, stored)
        self._save_scan(scan, stored)
        return stored

    def set_status(self, finding_id: str, status: str, reason: str = "") -> dict | None:
        """Update one finding's status by id or fingerprint. False positives
        require a reason. Returns the updated finding, or None if unknown."""
        if status not in FINDING_STATUSES:
            raise ValueError(f"invalid status {status!r}; expected one of {FINDING_STATUSES}")
        if status == "false_positive" and not reason.strip():
            raise ValueError("marking a finding false_positive requires a reason")
        stored = self.load_findings()
        for fp, finding in stored.items():
            if finding_id in (fp, finding.get("id"), finding.get("bug_id")):
                finding["status"] = status
                finding["status_reason"] = reason.strip()
                finding["status_changed"] = _now_iso()
                self._write(self.findings_path, stored)
                return finding
        return None

    # -- scans --------------------------------------------------------------

    def _save_scan(self, scan: dict, findings: dict[str, dict]) -> None:
        counts = {s: 0 for s in ("critical", "high", "medium", "low", "info")}
        for f in findings.values():
            if f.get("status") in ACTIVE_STATUSES and f.get("severity") in counts:
                counts[f["severity"]] += 1
        scan["active_counts"] = counts
        self._write(self.latest_path, scan)
        history = self._read(self.scans_path, [])
        history.append({
            "scan_id": scan.get("scan_id"),
            "created_at": scan.get("created_at"),
            "execution_mode": scan.get("execution_mode"),
            "modules_run": scan.get("modules_run", []),
            "module_errors": scan.get("module_errors", {}),
            "new_findings": len(scan.get("findings", [])),
            "active_counts": counts,
            "gate": scan.get("gate", {}),
            "duration_s": scan.get("duration_s"),
        })
        self._write(self.scans_path, history[-MAX_SCAN_HISTORY:])

    def latest_scan(self) -> dict | None:
        return self._read(self.latest_path, None)

    def scan_history(self) -> list[dict]:
        return self._read(self.scans_path, [])


def new_scan_id() -> str:
    return f"scan-{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}-{int(time.time() * 1000) % 1000:03d}"
