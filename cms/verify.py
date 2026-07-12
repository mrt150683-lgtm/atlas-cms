"""Test↔feature verification — turn verification checklists into executable proof.

``map_tests_to_features`` runs the test suite under coverage with dynamic
contexts (one context per test function), then intersects each feature's
member line ranges with the lines each test executed. The resulting test ids
land on feature nodes as ``exercised_by`` — the tests that *execute* the
feature's code. (Deliberately not called "verified": coverage proves
execution, not behavioural correctness.) ``verify_feature`` then runs exactly
those tests and reports pass/fail.

Requires ``coverage`` and ``pytest`` (``pip install cms[dev]``).
"""

from __future__ import annotations

import json
import hashlib
import shutil
import subprocess
import sys
import tempfile
import time
from collections import defaultdict
from pathlib import Path
from typing import Callable

import networkx as nx

from .features import get_features
from .scanner import scan

COVERAGE_RC = """\
[run]
dynamic_context = test_function
branch = False

[json]
show_contexts = True
"""
CACHE_SCHEMA = 1


def _python() -> str:
    """Interpreter for subprocess runs — sys.executable is the exe when frozen."""
    if getattr(sys, "frozen", False):
        return shutil.which("python") or shutil.which("python3") or "python"
    return sys.executable


def _context_to_pytest_id(context: str, known_files: list[str]) -> str | None:
    """coverage context 'test_x.test_fn' or 'pkg.test_x.TestC.test_fn' ->
    pytest id 'tests/test_x.py::test_fn'. Module names are resolved against the
    scanned file list because pytest's rootdir handling strips package prefixes."""
    context = context.split("|", 1)[0].strip()
    if not context:
        return None
    parts = context.split(".")
    if len(parts) < 2:
        return None
    # longest module prefix that matches a known file's path suffix
    for cut in range(len(parts) - 1, 0, -1):
        suffix = "/".join(parts[:cut]) + ".py"
        match = next(
            (f for f in known_files if f == suffix or f.endswith("/" + suffix)), None
        )
        if match:
            tail = "::".join(parts[cut:])
            return f"{match}::{tail}"
    return None


def _coverage_input_hash(root: Path, pytest_args: list[str]) -> str:
    """Cheap invalidation key for code, tests, config, and command arguments."""
    rows = [f"args:{json.dumps(pytest_args, sort_keys=True)}"]
    seen = set()
    for rec in scan(root):
        rows.append(f"{rec.rel_path}|{rec.size_bytes}|{rec.mtime}")
        seen.add(rec.rel_path)
    # Tests may sit outside an active semantic scope; they still affect coverage.
    for path in sorted(root.rglob("test*.py")):
        rel = path.relative_to(root).as_posix()
        if rel in seen or any(part in {".git", ".venv", "__pycache__"} for part in path.parts):
            continue
        stat = path.stat()
        rows.append(f"{rel}|{stat.st_size}|{stat.st_mtime_ns}")
    return hashlib.sha256("\n".join(sorted(rows)).encode("utf-8")).hexdigest()


def run_coverage(
    root: Path,
    pytest_args: list[str] | None = None,
    *,
    echo: Callable[[str], None] | None = None,
    refresh: bool = False,
    stream: bool = False,
) -> dict | None:
    """Run pytest under per-test coverage, with progress and safe caching."""
    pytest_args = pytest_args or []
    echo = echo or (lambda _message: None)
    memory_dir = root / ".memory"
    cache_file = memory_dir / "coverage_contexts.json"
    state_file = memory_dir / "verify_state.json"
    input_hash = _coverage_input_hash(root, pytest_args)
    if not refresh and cache_file.is_file() and state_file.is_file():
        try:
            state = json.loads(state_file.read_text(encoding="utf-8"))
            if state.get("schema_version") == CACHE_SCHEMA and state.get("input_hash") == input_hash:
                echo("Coverage cache is current — reusing mapped per-test contexts.")
                return json.loads(cache_file.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            pass

    started = time.monotonic()
    with tempfile.TemporaryDirectory() as tmp:
        rc = Path(tmp) / ".coveragerc"
        rc.write_text(COVERAGE_RC, encoding="utf-8")
        data_file = Path(tmp) / ".coverage"
        json_file = Path(tmp) / "coverage.json"
        env_args = ["--rcfile", str(rc), "--data-file", str(data_file)]
        echo("Coverage stage 1/3 — running pytest with per-test contexts…")
        run = subprocess.run(
            [_python(), "-m", "coverage", "run", *env_args,
             "-m", "pytest", "-q", *pytest_args],
            cwd=root, capture_output=not stream, text=True, timeout=900,
        )
        if run.returncode not in (0, 1):  # 1 = tests failed but ran; still useful
            if not stream:
                print((run.stdout or "")[-2000:] + (run.stderr or "")[-2000:], file=sys.stderr)
            return None
        echo(f"Coverage stage 1/3 complete in {time.monotonic() - started:.1f}s.")
        echo("Coverage stage 2/3 — exporting execution contexts…")
        export = subprocess.run(
            [_python(), "-m", "coverage", "json", *env_args,
             "-o", str(json_file), "--show-contexts"],
            cwd=root, capture_output=True, text=True, timeout=120,
        )
        if export.returncode != 0:
            print(export.stderr[-2000:], file=sys.stderr)
            return None
        data = json.loads(json_file.read_text(encoding="utf-8"))
        echo("Coverage stage 3/3 — saving reusable evidence cache…")
        memory_dir.mkdir(parents=True, exist_ok=True)
        cache_file.write_text(json.dumps(data), encoding="utf-8")
        state_file.write_text(json.dumps({
            "schema_version": CACHE_SCHEMA,
            "input_hash": input_hash,
            "pytest_args": pytest_args,
            "duration_seconds": round(time.monotonic() - started, 3),
        }, indent=2), encoding="utf-8")
        echo(f"Coverage mapping evidence ready in {time.monotonic() - started:.1f}s.")
        return data


# @memory:feature:FeatureVerification
# @memory:connects:FeatureTracing, ImpactAnalysis
# @memory:summary:Executable evidence — per-test coverage contexts intersected with feature member line ranges give exercised_by test lists; cms verify <Feature> runs exactly those tests.
def map_tests_to_features(graph: nx.DiGraph, root: Path, coverage_data: dict) -> dict[str, list[str]]:
    """Intersect per-test executed lines with feature member line ranges."""
    known_files = [
        a["path"] for _, a in graph.nodes(data=True) if a.get("type") == "file"
    ]
    # rel_path -> line -> {pytest ids}
    executed: dict[str, dict[int, set[str]]] = defaultdict(lambda: defaultdict(set))
    id_cache: dict[str, str | None] = {}
    for file_path, file_data in coverage_data.get("files", {}).items():
        rel = Path(file_path).as_posix()
        for line_str, contexts in (file_data.get("contexts") or {}).items():
            for ctx in contexts:
                if ctx not in id_cache:
                    id_cache[ctx] = _context_to_pytest_id(ctx, known_files)
                tid = id_cache[ctx]
                if tid:
                    executed[rel][int(line_str)].add(tid)

    mapping: dict[str, list[str]] = {}
    for feat in get_features(graph):
        tests: set[str] = set()
        for member_id in feat.get("members", []):
            if not graph.has_node(member_id):
                continue
            attrs = graph.nodes[member_id]
            path = attrs.get("path", "")
            if attrs.get("type") == "file":
                for line_tests in executed.get(path, {}).values():
                    tests |= line_tests
            else:
                start, end = attrs.get("start_line") or 0, attrs.get("end_line") or 0
                for line, line_tests in executed.get(path, {}).items():
                    if start <= line <= end:
                        tests |= line_tests
        mapping[feat["name"]] = sorted(tests)
        graph.nodes[feat["id"]]["exercised_by"] = sorted(tests)
    return mapping


def verify_feature(root: Path, test_ids: list[str]) -> tuple[bool, str]:
    """Run exactly the tests that exercise a feature. Returns (passed, output)."""
    if not test_ids:
        return False, "no tests mapped to this feature"
    run = subprocess.run(
        [_python(), "-m", "pytest", "-q", *test_ids],
        cwd=root, capture_output=True, text=True, timeout=600,
    )
    return run.returncode == 0, (run.stdout + run.stderr).strip()
