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
import re
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

_GENERIC_PATH_TERMS = {
    "add", "app", "change", "code", "file", "files", "fix", "required",
    "source", "support", "test", "tests", "update",
}
_IMPLEMENTATION_EXTENSIONS = {
    ".py", ".js", ".jsx", ".ts", ".tsx", ".java", ".go", ".rs", ".c", ".cc",
    ".cpp", ".h", ".hpp", ".cs", ".rb", ".php", ".swift", ".kt", ".kts",
}


def _terms(text: str) -> set[str]:
    return {w[:-1] if w.endswith("s") and len(w) > 4 else w
            for w in re.findall(r"[a-z0-9]+", str(text or "").lower())}


def _intent_allows_support(path: str, task: str, related: set[str],
                           changed: set[str]) -> bool:
    """Whether a non-test change is an intent-justified support artifact.

    These rules cover conventional companion outputs without making arbitrary
    source files disappear from scope checking. Each allowance is tied either
    to words in the declared goal or to a related/changed canonical companion.
    """
    low = path.lower()
    task_terms = _terms(task)
    path_terms = _terms(path.replace("/", " ").replace("_", " ").replace(".", " "))

    meaningful = (path_terms - _GENERIC_PATH_TERMS) & (task_terms - _GENERIC_PATH_TERMS)
    if meaningful:
        return True

    docs_requested = bool(task_terms & {"doc", "docs", "documentation", "readme", "skill"})
    if docs_requested and (
            low.endswith((".md", ".rst")) or low.startswith("docs/")
            or low in {"readme.md", "skill.md", "updates.md"}):
        return True
    if task_terms & {"ci", "workflow", "github"} and low.startswith(".github/workflows/"):
        return True
    if task_terms & {"dependency", "dependabot"} and low == ".github/dependabot.yml":
        return True
    if "security" in task_terms and (low == "security.md" or "security" in path_terms):
        return True
    if task_terms & {"ui", "interface", "viewer"} and (
            low.startswith("cms/ui_assets/") or low == "cms/ui.py"):
        return True
    if low.startswith("cms/ui_assets/") and (
            "cms/ui.py" in related or "cms/ui.py" in changed):
        return True
    if low == "docs/feature_ledger.json" and task_terms & {
            "alignment", "coverage", "ledger", "proof", "sentinel", "verify",
    }:
        return True
    return False


def _intent_allows_source(graph: nx.DiGraph, path: str, task: str) -> bool:
    """Whether a changed source file is independently grounded in the goal.

    Semantic retrieval is bounded and can omit a legitimate implementation
    file in a multi-part goal. That omission must not become scope drift when
    the graph's own file/member names, summaries, docstrings, or anchors carry
    multiple concrete goal terms. Requiring two non-generic terms keeps a lone
    broad word from laundering unrelated source changes.
    """
    if Path(path).suffix.lower() not in _IMPLEMENTATION_EXTENSIONS:
        return False
    fid = f"file:{path}"
    if not graph.has_node(fid):
        return False
    task_terms = _terms(task) - _GENERIC_PATH_TERMS
    evidence: list[str] = [path]
    nodes = [fid]
    nodes.extend(
        target for _, target, data in graph.out_edges(fid, data=True)
        if data.get("type") == "CONTAINS"
    )
    for node_id in nodes:
        attrs = graph.nodes[node_id]
        evidence.extend(str(attrs.get(key) or "")
                        for key in ("name", "qualname", "summary", "docstring"))
        anchors = attrs.get("anchors") or {}
        for values in anchors.values():
            evidence.extend(str(value) for value in (values if isinstance(values, list) else [values]))
    source_terms = _terms(" ".join(evidence)) - _GENERIC_PATH_TERMS
    return len(task_terms & source_terms) >= 2


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

    # Semantic hits and blast-radius paths are advisory candidates: a search
    # result being relevant does not mean the implementation must edit it.
    related: set[str] = set()
    for t in intent_pack.get("relevant_code", []):
        if t.get("path"):
            related.add(t["path"])
    imp = intent_pack.get("impact") or {}
    for f in imp.get("files", []):
        related.add(f)
    # Only paths literally named in the goal are mandatory.
    required = {str(p).replace("\\", "/")
                for p in (intent_pack.get("declared_paths") or []) if p}

    changed_set = set(changed)
    is_test = lambda p: "tests/" in p or p.rsplit("/", 1)[-1].startswith("test_")

    expected = related | required
    touched_expected = sorted(expected & changed_set)
    untouched_required = sorted(required - changed_set)
    related_not_touched = sorted(related - changed_set)
    task = str(intent_pack.get("task") or "")
    justified_sources = sorted(
        p for p in changed_set - expected
        if _intent_allows_source(graph, p, task)
    )
    unstated = sorted(
        p for p in changed_set - expected
        if not is_test(p)
        and p not in justified_sources
        and not _intent_allows_support(p, task, related, changed_set)
    )

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

    # approved intent (decision lock) for touched features — the durable word
    # the change is supposed to serve; surfaced so the verdict reader can
    # compare behaviour against what was actually agreed
    approved_intent = []
    try:
        from .decisions import DecisionStore

        store = DecisionStore(root / config.MEMORY_DIR_NAME, root=root)
        for name in sorted(touched_features):
            dec = store.approved_for(name)
            if dec:
                approved_intent.append({
                    "feature": name, "decision_id": dec["id"],
                    "title": dec["title"], "behaviour": dec["intent"]["behaviour"],
                    "prohibited": dec["intent"].get("prohibited", []),
                    "approved_at": dec.get("approved_at"),
                })
    except Exception:
        pass

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
    elif findings or untouched_required or unstated or feature_gaps:
        verdict = "partial"
        headline = "Declared targets were touched and covered, but gaps remain (see below)."
    else:
        verdict = "aligned"
        headline = f"Change hits the declared targets and is covered by {len(radius['tests'])} test(s)."

    assert verdict in VERDICTS

    gaps: list[str] = []
    for p in untouched_required:
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
        "related_not_touched": related_not_touched,
        "intent_justified_sources": justified_sources,
        "touched_features": [r["feature"] for r in feature_reviews],
        "feature_reviews": feature_reviews,
        "impact": radius,
        "tests_to_run": radius["test_files"],
        "findings": findings,
        "approved_intent": approved_intent,
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
