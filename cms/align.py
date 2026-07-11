"""Change alignment — "did *this change* do what it was meant to?"

Closes the intent→reality loop around a single unit of work. Given a captured
intent (see ``cms/intent.py``) and the set of files changed versus a git base,
it fuses the memory layer's existing judgments — impact (blast radius + the
tests that cover the change), feature review verdicts (expected-vs-built), and
the latest Sentinel findings — into one per-change verdict drawn from the same
``aligned / partial / drift / unverified`` vocabulary the AI review uses.

Pure reuse: no LLM call here. The intent pack is built once (via
``prompt_export.build_task_pack``) and reused as the "expected" side; the git
diff is the "actual" side. Verdicts are persisted to ``.memory/align/`` so the
codebase accrues a labelled history of intent→outcome (trajectory memory).
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import networkx as nx

from . import config
from .config import LANGUAGE_BY_EXTENSION
from .githistory import _git
from .impact import analyze_impact
from .memory import CodebaseMemory
from .review import VERDICTS

ALIGN_DIR = "align"
CRITICAL = "critical"


# ── git diff → changed source files ─────────────────────────────────────────

def git_changed_files(root: Path, base: str = "HEAD") -> list[str]:
    """Project-relative source paths changed vs ``base`` (incl. untracked).

    Paths are normalised to the project-root-relative, forward-slash form the
    knowledge graph uses, so they line up with ``file:<path>`` node ids. Only
    whitelisted source extensions are returned.
    """
    root = root.resolve()
    if _git(root, "rev-parse", "--is-inside-work-tree") is None:
        return []
    prefix = (_git(root, "rev-parse", "--show-prefix") or "").strip()

    raw: set[str] = set()
    tracked = _git(root, "diff", "--name-only", base)
    for line in (tracked or "").splitlines():
        if line.strip():
            raw.add(line.strip())
    # untracked, not-ignored files show up as "?? path" in porcelain status
    status = _git(root, "status", "--porcelain", "--untracked-files=all")
    for line in (status or "").splitlines():
        path = line[3:].strip() if len(line) > 3 else ""
        if path:
            raw.add(path)

    out: set[str] = set()
    for path in raw:
        path = path.replace("\\", "/").strip('"')
        if prefix and path.startswith(prefix):
            path = path[len(prefix):]
        if "." + path.rsplit(".", 1)[-1] in LANGUAGE_BY_EXTENSION:
            out.add(path)
    return sorted(out)


# ── graph helpers ───────────────────────────────────────────────────────────

def _touched_features(graph: nx.DiGraph, changed: list[str]) -> dict[str, dict]:
    """Feature name -> feature node attrs, for features whose members live in a
    changed file (via CONTAINS component -> PART_OF feature)."""
    from .features import get_features

    changed_set = set(changed)
    feats_by_name = {f["name"]: f for f in get_features(graph)}
    touched: dict[str, dict] = {}
    for fid in [f"file:{p}" for p in changed if graph.has_node(f"file:{p}")]:
        for _, comp, d in graph.out_edges(fid, data=True):
            if d.get("type") != "CONTAINS":
                continue
            for _, feat, fd in graph.out_edges(comp, data=True):
                if fd.get("type") == "PART_OF":
                    name = graph.nodes[feat].get("name")
                    if name in feats_by_name:
                        touched[name] = feats_by_name[name]
    # a changed file may sit directly under a feature too
    for p in changed:
        fid = f"file:{p}"
        if not graph.has_node(fid):
            continue
        for _, feat, fd in graph.out_edges(fid, data=True):
            if fd.get("type") == "PART_OF":
                name = graph.nodes[feat].get("name")
                if name in feats_by_name:
                    touched[name] = feats_by_name[name]
    _ = changed_set
    return touched


def _blast_radius(graph: nx.DiGraph, changed: list[str]) -> dict:
    """Union impact across changed files -> affected features + covering tests."""
    features: set[str] = set()
    tests: set[str] = set()
    files: set[str] = set()
    for p in changed:
        target = f"file:{p}" if graph.has_node(f"file:{p}") else p
        result = analyze_impact(graph, target)
        if result is None:
            continue
        features.update(result.features)
        tests.update(result.tests)
        files.update(result.files)
    test_files = sorted({t.split("::", 1)[0] for t in tests})
    return {
        "features": sorted(features),
        "files": sorted(files),
        "tests": sorted(tests),
        "test_files": test_files,
    }


# ── alignment ───────────────────────────────────────────────────────────────

def build_alignment(
    mem: CodebaseMemory,
    root: Path,
    intent_pack: dict,
    base: str = "HEAD",
    scan: bool = False,
) -> dict:
    """Verdict a diff against a captured intent. Returns a JSON-able record."""
    graph = mem.graph
    changed = git_changed_files(root, base=base)

    # expected side: the paths the intent points at (top memory matches + impact)
    expected: set[str] = set()
    for t in intent_pack.get("relevant_code", []):
        if t.get("path"):
            expected.add(t["path"])
    imp = intent_pack.get("impact") or {}
    for f in imp.get("files", []):
        expected.add(f)

    changed_set = set(changed)
    is_test = lambda p: "tests/" in p or p.rsplit("/", 1)[-1].startswith("test_")

    touched_expected = sorted(expected & changed_set)
    untouched_expected = sorted(expected - changed_set)
    unstated = sorted(p for p in changed_set - expected if not is_test(p))

    radius = _blast_radius(graph, changed)
    touched_features = _touched_features(graph, changed)

    # feature review verdicts (expected-vs-built) for touched features
    feature_reviews = []
    feature_gaps: list[str] = []
    has_drift_feature = False
    for name, feat in sorted(touched_features.items()):
        review = feat.get("review") or {}
        verdict = review.get("verdict")
        if verdict == "drift":
            has_drift_feature = True
        feature_reviews.append({
            "feature": name,
            "verdict": verdict or "unverified",
            "headline": review.get("headline", ""),
            "exercised_by": len(feat.get("exercised_by", [])),
        })
        for g in (review.get("gaps") or [])[:4]:
            feature_gaps.append(f"{name}: {g}")

    # sentinel findings landing on changed files
    findings = _findings_on_change(root, changed_set, scan=scan)
    critical_on_change = any(f.get("severity") == CRITICAL for f in findings)

    has_tests = bool(radius["tests"])

    # ── verdict synthesis (same vocabulary as cms review) ────────────────
    if not changed:
        verdict = "unverified"
        headline = f"No source changes versus {base} — nothing to align."
    elif has_drift_feature or critical_on_change:
        verdict = "drift"
        why = "a touched feature is in drift" if has_drift_feature else \
              "a critical Sentinel finding lands on a changed file"
        headline = f"Change conflicts with intent — {why}."
    elif not touched_expected:
        verdict = "unverified"
        headline = "Changed files don't map to the declared intent — can't confirm it was done here."
    elif not has_tests:
        verdict = "unverified"
        headline = "Declared targets were touched, but no mapped test covers the change — can't prove it landed."
    elif findings or untouched_expected or unstated:
        verdict = "partial"
        headline = "Declared targets were touched and covered, but gaps remain (see below)."
    else:
        verdict = "aligned"
        headline = f"Change hits the declared targets and is covered by {len(radius['tests'])} test(s)."

    assert verdict in VERDICTS

    gaps: list[str] = []
    for p in untouched_expected:
        gaps.append(f"intent-target-untouched: {p}")
    for p in unstated:
        gaps.append(f"unstated-change: {p}")
    if changed and not has_tests:
        gaps.append("no-verifying-tests: the changed code isn't covered by any mapped test")
    gaps.extend(feature_gaps)

    return {
        "intent": intent_pack.get("task", ""),
        "intent_source": intent_pack.get("intent_source", "explicit"),
        "base": base,
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "verdict": verdict,
        "headline": headline,
        "changed": changed,
        "touched_expected": touched_expected,
        "touched_features": [r["feature"] for r in feature_reviews],
        "feature_reviews": feature_reviews,
        "impact": radius,
        "tests_to_run": radius["test_files"],
        "findings": findings,
        "gaps": gaps,
    }


def _findings_on_change(root: Path, changed_set: set[str], scan: bool = False) -> list[dict]:
    """Active Sentinel findings whose file is in the changed set."""
    from .sentinel import ACTIVE_STATUSES
    from .sentinel.store import SentinelStore

    memory_dir = root / config.MEMORY_DIR_NAME
    if scan:
        from .sentinel.runner import run_scan
        run_scan(root)
    store = SentinelStore(memory_dir)
    out = []
    for f in store.load_findings().values():
        if f.get("status") not in ACTIVE_STATUSES:
            continue
        fp = (f.get("file") or "").replace("\\", "/")
        if fp and fp in changed_set:
            out.append({
                "id": f.get("bug_id") or f.get("id"),
                "severity": f.get("severity"),
                "file": fp,
                "summary": f.get("summary", "")[:200],
            })
    order = {s: i for i, s in enumerate(("critical", "high", "medium", "low", "info"))}
    return sorted(out, key=lambda f: order.get(f["severity"], 9))[:40]


# ── persistence (mirrors SentinelStore; seeds trajectory memory) ────────────

MAX_HISTORY = 100


class AlignStore:
    def __init__(self, memory_dir: Path) -> None:
        self.dir = memory_dir / ALIGN_DIR
        self.intent_path = self.dir / "intent.json"
        self.latest_path = self.dir / "latest.json"
        self.sessions_path = self.dir / "sessions.json"

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

    def save_intent(self, intent: dict) -> None:
        self._write(self.intent_path, intent)

    def load_intent(self) -> dict | None:
        return self._read(self.intent_path, None)

    def save_alignment(self, record: dict) -> None:
        self._write(self.latest_path, record)
        history = self._read(self.sessions_path, [])
        history.append({
            "generated_at": record.get("generated_at"),
            "intent": record.get("intent"),
            "base": record.get("base"),
            "verdict": record.get("verdict"),
            "headline": record.get("headline"),
            "changed": len(record.get("changed", [])),
            "gaps": len(record.get("gaps", [])),
        })
        self._write(self.sessions_path, history[-MAX_HISTORY:])

    def latest(self) -> dict | None:
        return self._read(self.latest_path, None)

    def history(self) -> list[dict]:
        return self._read(self.sessions_path, [])
