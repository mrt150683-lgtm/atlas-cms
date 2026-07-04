"""Test↔feature verification — turn verification checklists into executable proof.

``map_tests_to_features`` runs the test suite under coverage with dynamic
contexts (one context per test function), then intersects each feature's
member line ranges with the lines each test executed. The resulting test ids
land on feature nodes as ``verified_by``. ``verify_feature`` then runs exactly
those tests and reports pass/fail — proving the feature behaves as intended.

Requires ``coverage`` and ``pytest`` (``pip install cms[dev]``).
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
from collections import defaultdict
from pathlib import Path

import networkx as nx

from .features import get_features

COVERAGE_RC = """\
[run]
dynamic_context = test_function
branch = False

[json]
show_contexts = True
"""


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


def run_coverage(root: Path, pytest_args: list[str] | None = None) -> dict | None:
    """Run pytest under coverage with per-test contexts; return coverage JSON."""
    with tempfile.TemporaryDirectory() as tmp:
        rc = Path(tmp) / ".coveragerc"
        rc.write_text(COVERAGE_RC, encoding="utf-8")
        data_file = Path(tmp) / ".coverage"
        json_file = Path(tmp) / "coverage.json"
        env_args = ["--rcfile", str(rc), "--data-file", str(data_file)]
        run = subprocess.run(
            [_python(), "-m", "coverage", "run", *env_args,
             "-m", "pytest", "-q", *(pytest_args or [])],
            cwd=root, capture_output=True, text=True, timeout=600,
        )
        if run.returncode not in (0, 1):  # 1 = tests failed but ran; still useful
            print(run.stdout[-2000:] + run.stderr[-2000:], file=sys.stderr)
            return None
        export = subprocess.run(
            [_python(), "-m", "coverage", "json", *env_args,
             "-o", str(json_file), "--show-contexts"],
            cwd=root, capture_output=True, text=True, timeout=120,
        )
        if export.returncode != 0:
            print(export.stderr[-2000:], file=sys.stderr)
            return None
        return json.loads(json_file.read_text(encoding="utf-8"))


# @memory:feature:FeatureVerification
# @memory:connects:FeatureTracing, ImpactAnalysis
# @memory:summary:Executable proof — per-test coverage contexts intersected with feature member line ranges give verified_by test lists; cms verify <Feature> runs exactly those tests.
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
        graph.nodes[feat["id"]]["verified_by"] = sorted(tests)
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
