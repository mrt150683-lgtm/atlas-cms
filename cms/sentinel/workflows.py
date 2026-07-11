"""Sentinel Workflow Test Runner — end-to-end checks against the real pipeline.

Each check builds a throwaway fixture project in a temp directory and drives
the actual CMS code (scanner -> graph -> mock summaries -> features -> exports
-> query/impact), then asserts observable outcomes. Negative checks prove the
guards hold: no memory -> blocked, path traversal -> blocked, unknown MCP tool
-> rejected. The carry-over check encodes this repo's known regression trap:
data written onto feature nodes must survive an incremental update.

All checks run with the mock provider (deterministic, no network) and say so:
``mode: mock``. A check the codebase cannot support reports ``missing`` rather
than pretending to pass.
"""

from __future__ import annotations

import json
import tempfile
import traceback
from pathlib import Path

from . import make_finding

FIXTURE = '''\
# @memory:feature:Greeting
# @memory:summary:Greets people by name.
def greet(name):
    return helper(name)


def helper(name):
    return "hi " + name
'''


def _fixture_project(tmp: Path) -> Path:
    root = tmp / "proj"
    root.mkdir()
    (root / "app.py").write_text(FIXTURE, encoding="utf-8")
    return root


def _check(name: str, description: str, expected: str, fn) -> dict:
    """Run one workflow check; fn returns actual-description or raises."""
    try:
        actual = fn()
        return {"name": name, "description": description, "expected": expected,
                "actual": actual, "passed": True, "mode": "mock"}
    except _Missing as exc:
        return {"name": name, "description": description, "expected": expected,
                "actual": str(exc), "passed": None, "mode": "mock"}
    except Exception as exc:
        return {"name": name, "description": description, "expected": expected,
                "actual": f"{type(exc).__name__}: {exc}",
                "detail": traceback.format_exc(limit=4),
                "passed": False, "mode": "mock"}


class _Missing(Exception):
    """The codebase does not support this workflow yet — report, don't fake."""


def _wf_pipeline(tmp: Path) -> str:
    from ..exporter import export_features, export_graph, export_index
    from ..features import build_features
    from ..graph_builder import build_graph
    from ..impact import analyze_impact
    from ..memory import CodebaseMemory
    from ..providers import MockProvider
    from ..scanner import scan
    from ..summarizer import generate_summaries
    from ..tree_export import export_tree

    root = _fixture_project(tmp)
    records = scan(root)
    assert len(records) == 1, f"scanner found {len(records)} files, expected 1"
    memory_dir = root / ".memory"
    export_tree(root, records, memory_dir)
    graph = build_graph(records)
    generate_summaries(graph, root, MockProvider())
    feats = build_features(graph, MockProvider())
    export_graph(graph, memory_dir)
    export_features(graph, memory_dir)
    export_index(graph, memory_dir, file_count=len(records))
    assert any(f.name == "Greeting" for f in feats), "declared feature not traced"
    mem = CodebaseMemory.load(memory_dir / "graph.json")
    hits = mem.query_intent("greet people", top_k=3)
    assert hits and any("greet" in h.name for h in hits), "query engine found nothing"
    impact = analyze_impact(mem.graph, "app.py::helper")
    assert impact and impact.total >= 1, "impact analysis resolved nothing"
    assert (memory_dir / "features" / "Greeting.md").is_file(), "feature doc not exported"
    return "scan -> graph -> summaries -> features -> exports -> query -> impact all produced real artifacts"


def _wf_query_without_memory(tmp: Path) -> str:
    from ..memory import CodebaseMemory

    try:
        CodebaseMemory.load(tmp / "nowhere" / "graph.json")
    except (OSError, FileNotFoundError):
        return "loading memory without graph.json raises — CLI surfaces 'run cms run-all first'"
    raise AssertionError("loading a missing graph.json did not raise")


def _wf_mcp_traversal_blocked(tmp: Path) -> str:
    """A guard must refuse EVERY way out of the project root, not just `../`.
    The single-payload version of this check let a `'..' in path` bypass through
    (absolute paths have no '..'); this exercises the whole escape family."""
    from ..mcp import MCPServer

    root = _fixture_project(tmp)
    # a readable, scannable-extension file OUTSIDE the project root
    outside = tmp / "outside_secret.json"
    outside.write_text('{"secret": "never-serve-this"}', encoding="utf-8")
    server = MCPServer(root)
    payloads = {
        "relative": "../outside_secret.json",
        "relative-backslash": "..\\outside_secret.json",
        "absolute": str(outside),
        "absolute-resolved": str(outside.resolve()),
        "double-dot-encoded": "..%2Foutside_secret.json",
    }
    leaked = [name for name, p in payloads.items() if "source" in server.get_source(path=p)]
    assert not leaked, (
        f"get_source served files OUTSIDE the project root for payload(s) {leaked} — "
        "guard is bypassable (arbitrary local file read)"
    )
    return f"get_source refused all {len(payloads)} traversal payloads (relative, absolute, resolved, backslash, encoded)"


def _wf_mcp_unknown_tool(tmp: Path) -> str:
    from ..mcp import MCPServer

    root = _fixture_project(tmp)
    response = MCPServer(root).handle({
        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": "drop_all_tables", "arguments": {}},
    })
    assert response and "error" in response, f"unknown tool accepted: {response}"
    return "unknown tool call rejected with a JSON-RPC error"


def _wf_carry_over(tmp: Path) -> str:
    """Feature-node data (exercised_by, review) must survive incremental updates
    when members did not change — the repo's known silent-wipe regression."""
    from ..memory import CodebaseMemory
    from ..providers import MockProvider
    from ..update import incremental_update

    root = _fixture_project(tmp)
    incremental_update(root, MockProvider(), echo=lambda *_: None)
    graph_path = root / ".memory" / "graph.json"
    mem = CodebaseMemory.load(graph_path)
    node = mem.graph.nodes["feature:Greeting"]
    node["exercised_by"] = ["tests/test_app.py::test_greet"]
    node["review"] = {"verdict": "aligned", "headline": "sentinel-probe"}
    mem.save(graph_path)

    incremental_update(root, MockProvider(), echo=lambda *_: None)
    after = CodebaseMemory.load(graph_path).graph.nodes["feature:Greeting"]
    assert after.get("exercised_by") == ["tests/test_app.py::test_greet"], \
        f"exercised_by wiped by incremental update: {after.get('exercised_by')!r}"
    assert (after.get("review") or {}).get("headline") == "sentinel-probe", \
        f"review wiped by incremental update: {after.get('review')!r}"
    return "exercised_by and review survived an incremental update with unchanged members"


def _wf_mock_labelling(tmp: Path) -> str:
    from ..graph_builder import build_graph
    from ..providers import MockProvider
    from ..scanner import scan
    from ..summarizer import generate_summaries

    root = _fixture_project(tmp)
    graph = build_graph(scan(root))
    generate_summaries(graph, root, MockProvider())
    attrs = graph.nodes["file:app.py"]
    provider = (attrs.get("summary_meta") or {}).get("provider")
    assert provider == "mock", f"summary_meta.provider is {provider!r}, not 'mock'"
    assert "mock" in (attrs.get("summary") or "").lower(), \
        "mock summary text does not label itself as mock"
    return "mock summaries carry summary_meta.provider='mock' and say so in the text"


def _wf_activity_rotation(tmp: Path) -> str:
    from ..activity import MAX_BYTES, log_activity, read_activity

    memory_dir = tmp / ".memory"
    for i in range(600):
        log_activity(memory_dir, "query_codebase", [f"file:f{i}.py"], label="x" * 120)
    size = (memory_dir / "activity.jsonl").stat().st_size
    assert size <= MAX_BYTES * 2, f"activity log grew unbounded: {size} bytes"
    events = read_activity(memory_dir, since=0)
    assert events and all("ts" in e and "tool" in e for e in events), "events malformed"
    return f"600 events -> {size} bytes on disk (capped), reader returns well-formed events"


CHECKS = [
    ("pipeline_end_to_end",
     "Full memory pipeline on a fixture project produces queryable artifacts",
     "every stage runs and its output is observable", _wf_pipeline),
    ("query_without_memory_blocked",
     "Querying before a memory layer exists must be blocked, not fabricated",
     "loading a missing graph raises", _wf_query_without_memory),
    ("mcp_path_traversal_blocked",
     "MCP get_source must refuse paths outside the project root",
     "error result, no file content", _wf_mcp_traversal_blocked),
    ("mcp_unknown_tool_rejected",
     "Unknown MCP tool calls must be rejected",
     "JSON-RPC error response", _wf_mcp_unknown_tool),
    ("carry_over_preserves_verification",
     "exercised_by/review on feature nodes survive incremental updates (raw-evidence immutability)",
     "data intact after update with unchanged members", _wf_carry_over),
    ("mock_output_is_labelled",
     "Mock-provider output must be explicitly labelled, never mistakable for AI output",
     "summary_meta.provider == 'mock' and text says mock", _wf_mock_labelling),
    ("activity_log_bounded",
     "The activity feed must rotate, stay parseable, and never grow unbounded",
     "size capped, events well-formed", _wf_activity_rotation),
]


# @memory:feature:HermesSentinel
# @memory:connects:IncrementalUpdates, AgentMemoryAccess, ActivityPulse
# @memory:summary:Workflow test runner — drives the real pipeline in temp fixture projects (positive path plus blocked-path negatives and the carry-over regression trap); failures become high/critical findings.
def run_workflow_checks(root: Path) -> tuple[list[dict], list[dict]]:
    """Returns (check_results, findings-for-failures)."""
    results = []
    for name, description, expected, fn in CHECKS:
        with tempfile.TemporaryDirectory(prefix="cms-sentinel-") as tmp:
            results.append(_check(name, description, expected, lambda t=Path(tmp), f=fn: f(t)))

    findings = []
    for r in results:
        if r["passed"] is False:
            severity = "critical" if r["name"] in (
                "carry_over_preserves_verification",
                "mcp_path_traversal_blocked",
            ) else "high"
            findings.append(make_finding(
                "workflows", severity,
                f"workflow check {r['name']} failed: {r['actual']}",
                area="workflow", pattern=r["name"],
                evidence=[r.get("detail", "")[:400], f"expected: {r['expected']}"],
                risk=r["description"],
                recommendation="Reproduce with `cms sentinel run` and fix the failing stage; the check body in cms/sentinel/workflows.py is the repro script.",
                execution_mode="mock",
                fingerprint_of=r["name"],
            ))
        elif r["passed"] is None:
            findings.append(make_finding(
                "workflows", "medium",
                f"workflow {r['name']} is not supported yet: {r['actual']}",
                area="workflow_missing", pattern=r["name"],
                risk="The workflow the check describes does not exist — reported, not faked.",
                recommendation="Implement the workflow, then let the check verify it.",
                execution_mode="mock",
                fingerprint_of=r["name"],
            ))
    return results, findings


def check_results_as_json(results: list[dict]) -> str:
    return json.dumps(results, indent=1)
