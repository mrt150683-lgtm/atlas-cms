import json
from pathlib import Path

from typer.testing import CliRunner

from cms.anchor_drift import detect_anchor_drift
from cms.cli import app
from cms.features import build_features
from cms.graph_builder import build_graph
from cms.memory import CodebaseMemory
from cms.providers import MockProvider
from cms.scanner import scan
from cms.sentinel.runner import run_scan


DRIFTED = '''\
# @memory:feature:Dispatch
# @memory:summary:Calls `old_handler` before returning.
def dispatch(value):
    return current_handler(value)


def current_handler(value):
    return value
'''

HONEST = '''\
# @memory:feature:Dispatch
# @memory:summary:Calls `current_handler` before returning.
def dispatch(value):
    return current_handler(value)


def current_handler(value):
    return value
'''


def _memory(root: Path, source: str) -> CodebaseMemory:
    (root / "app.py").write_text(source, encoding="utf-8")
    graph = build_graph(scan(root))
    build_features(graph, MockProvider())
    memory = CodebaseMemory(graph)
    memory_dir = root / ".memory"
    memory_dir.mkdir(exist_ok=True)
    memory.save(memory_dir / "graph.json")
    return memory


def test_summary_symbol_true_positive_and_true_negative(tmp_path: Path) -> None:
    drift_root = tmp_path / "drifted"
    drift_root.mkdir()
    drift = detect_anchor_drift(_memory(drift_root, DRIFTED).graph, drift_root)
    summary_findings = [f for f in drift.findings if f.kind == "summary-symbol-drift"]
    assert len(summary_findings) == 1
    assert summary_findings[0].symbol == "old_handler"
    assert summary_findings[0].node_id == "func:app.py::dispatch"

    honest_root = tmp_path / "honest"
    honest_root.mkdir()
    honest = detect_anchor_drift(_memory(honest_root, HONEST).graph, honest_root)
    assert not [f for f in honest.findings if f.kind == "summary-symbol-drift"]


def test_connect_without_evidence_and_supported_inverse_link(tmp_path: Path) -> None:
    unsupported = tmp_path / "unsupported"
    unsupported.mkdir()
    (unsupported / "alpha.py").write_text(
        "# @memory:feature:Alpha\n# @memory:connects:Beta\ndef alpha():\n    return 1\n",
        encoding="utf-8",
    )
    (unsupported / "beta.py").write_text(
        "# @memory:feature:Beta\ndef beta():\n    return 2\n", encoding="utf-8"
    )
    graph = build_graph(scan(unsupported))
    build_features(graph, MockProvider())
    findings = detect_anchor_drift(graph, unsupported).findings
    assert [(f.feature, f.related_feature) for f in findings if f.kind == "connect-without-evidence"] == [
        ("Alpha", "Beta")
    ]

    supported = tmp_path / "supported"
    supported.mkdir()
    (supported / "alpha.py").write_text(
        "# @memory:feature:Alpha\n# @memory:connects:Beta\ndef alpha():\n    return 1\n",
        encoding="utf-8",
    )
    (supported / "beta.py").write_text(
        "from alpha import alpha\n\n# @memory:feature:Beta\ndef beta():\n    return alpha()\n",
        encoding="utf-8",
    )
    graph = build_graph(scan(supported))
    build_features(graph, MockProvider())
    assert not [f for f in detect_anchor_drift(graph, supported).findings
                if f.kind == "connect-without-evidence"]


def test_cli_json_is_a_gate(tmp_path: Path) -> None:
    _memory(tmp_path, DRIFTED)
    result = CliRunner().invoke(app, ["drift", "--root", str(tmp_path), "--json"])
    assert result.exit_code == 1
    payload = json.loads(result.stdout)
    assert payload["high_confidence"] == 1
    assert payload["signals"] == {"summary-symbol-drift": 1}


def test_sentinel_finding_clears_when_source_matches_again(tmp_path: Path) -> None:
    _memory(tmp_path, DRIFTED)
    first_scan, first_findings = run_scan(tmp_path, modules=("anchor_drift",))
    active = [f for f in first_findings.values() if f["module"] == "anchor_drift"
              and f["status"] == "open"]
    assert len(active) == 1
    fingerprint = active[0]["fingerprint"]
    assert first_scan["modules_run"] == ["anchor_drift"]

    # Fix the current body without rebuilding graph.json: Sentinel must compare
    # the stored anchor with live source, not merely report whole-file staleness.
    (tmp_path / "app.py").write_text(
        DRIFTED.replace("return current_handler(value)", "return old_handler(value)"),
        encoding="utf-8",
    )
    _, second_findings = run_scan(tmp_path, modules=("anchor_drift",))
    assert second_findings[fingerprint]["status"] == "resolved"
