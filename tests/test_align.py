"""Change-alignment tests — verdict logic, persistence, and the MCP loop.

Mirrors tests/test_impact.py's tiny graph fixture (core<-service<-app + a test
importing service) so the blast radius includes a real covering test. Git is
sidestepped by monkeypatching ``cms.align.git_changed_files`` — the unit under
test is the verdict synthesis, not git plumbing.
"""

import json
from pathlib import Path

import cms.align as align
from cms.align import AlignStore, build_alignment
from cms.features import build_features
from cms.graph_builder import build_graph
from cms.memory import CodebaseMemory
from cms.providers import MockProvider
from cms.scanner import scan

CORE = "# @memory:feature:Storage\ndef save(data):\n    return data\n"
SERVICE = "from core import save\n\n\ndef process(x):\n    return save(x)\n"
APP = "from service import process\n\n\ndef main():\n    process(1)\n"
TEST = "from service import process\n\n\ndef test_process():\n    assert process(1) == 1\n"


def _project(tmp_path: Path) -> Path:
    (tmp_path / "core.py").write_text(CORE, encoding="utf-8")
    (tmp_path / "service.py").write_text(SERVICE, encoding="utf-8")
    (tmp_path / "app.py").write_text(APP, encoding="utf-8")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_service.py").write_text(TEST, encoding="utf-8")
    graph = build_graph(scan(tmp_path))
    build_features(graph, MockProvider())
    memory_dir = tmp_path / ".memory"
    memory_dir.mkdir()
    CodebaseMemory(graph).save(memory_dir / "graph.json")
    return tmp_path


def _mem(root: Path) -> CodebaseMemory:
    return CodebaseMemory.load(root / ".memory" / "graph.json")


def _intent(paths: list[str], task: str = "improve storage") -> dict:
    return {
        "task": task,
        "intent_source": "explicit",
        "declared_paths": paths,
        "relevant_code": [{"kind": "file", "name": p, "path": p} for p in paths],
        "impact": {"files": []},
    }


def _write_finding(root: Path, severity: str, file: str) -> None:
    sdir = root / ".memory" / "sentinel"
    sdir.mkdir(parents=True, exist_ok=True)
    (sdir / "findings.json").write_text(json.dumps({
        "fp1": {"status": "open", "severity": severity, "file": file,
                "summary": "boom", "bug_id": "BUG-1", "id": "SEN-1"},
    }), encoding="utf-8")


def test_aligned_when_target_touched_and_covered(tmp_path, monkeypatch):
    root = _project(tmp_path)
    monkeypatch.setattr(align, "git_changed_files", lambda r, base="HEAD": ["core.py"])
    rec = build_alignment(_mem(root), root, _intent(["core.py"]))
    assert rec["verdict"] == "aligned", rec["headline"]
    assert any("test_service" in t for t in rec["impact"]["tests"])
    assert rec["gaps"] == []


def test_unverified_when_no_covering_test(tmp_path, monkeypatch):
    root = _project(tmp_path)
    # app.py has no importers -> blast radius reaches no test
    monkeypatch.setattr(align, "git_changed_files", lambda r, base="HEAD": ["app.py"])
    rec = build_alignment(_mem(root), root, _intent(["app.py"]))
    assert rec["verdict"] == "unverified"
    assert any(g.startswith("no-verifying-tests") for g in rec["gaps"])


def test_drift_when_critical_finding_on_changed_file(tmp_path, monkeypatch):
    root = _project(tmp_path)
    _write_finding(root, "critical", "core.py")
    monkeypatch.setattr(align, "git_changed_files", lambda r, base="HEAD": ["core.py"])
    rec = build_alignment(_mem(root), root, _intent(["core.py"]))
    assert rec["verdict"] == "drift"
    assert rec["findings"] and rec["findings"][0]["severity"] == "critical"


def test_partial_flags_unstated_scope_creep(tmp_path, monkeypatch):
    root = _project(tmp_path)
    monkeypatch.setattr(align, "git_changed_files", lambda r, base="HEAD": ["core.py", "app.py"])
    rec = build_alignment(_mem(root), root, _intent(["core.py"]))
    assert rec["verdict"] == "partial"
    assert any(g == "unstated-change: app.py" for g in rec["gaps"])


def test_semantic_candidates_are_advisory_not_mandatory(tmp_path, monkeypatch):
    root = _project(tmp_path)
    monkeypatch.setattr(align, "git_changed_files", lambda r, base="HEAD": ["core.py"])
    intent = _intent(["core.py", "service.py"])
    intent["declared_paths"] = []

    rec = build_alignment(_mem(root), root, intent)

    assert rec["verdict"] == "aligned"
    assert rec["related_not_touched"] == ["service.py"]
    assert not any(g.startswith("intent-target-untouched") for g in rec["gaps"])


def test_only_literal_declared_paths_are_mandatory(tmp_path, monkeypatch):
    root = _project(tmp_path)
    monkeypatch.setattr(align, "git_changed_files", lambda r, base="HEAD": ["core.py"])
    intent = _intent(["core.py", "service.py"])
    intent["declared_paths"] = ["service.py"]

    rec = build_alignment(_mem(root), root, intent)

    assert rec["verdict"] == "partial"
    assert "intent-target-untouched: service.py" in rec["gaps"]


def test_intent_justified_support_files_are_not_scope_creep(tmp_path, monkeypatch):
    root = _project(tmp_path)
    support = ["README.md", ".github/workflows/ci.yml", ".github/dependabot.yml",
               "SECURITY.md"]
    monkeypatch.setattr(
        align, "git_changed_files", lambda r, base="HEAD": ["core.py", *support])
    intent = _intent(["core.py"], task=(
        "improve storage with docs, CI workflow, dependency updates and security policy"))

    rec = build_alignment(_mem(root), root, intent)

    assert rec["verdict"] == "aligned"
    assert not any(g.startswith("unstated-change") for g in rec["gaps"])


def test_unrequested_support_file_still_counts_as_scope_creep(tmp_path, monkeypatch):
    root = _project(tmp_path)
    monkeypatch.setattr(
        align, "git_changed_files", lambda r, base="HEAD": ["core.py", "SECURITY.md"])

    rec = build_alignment(_mem(root), root, _intent(["core.py"]))

    assert rec["verdict"] == "partial"
    assert "unstated-change: SECURITY.md" in rec["gaps"]


def test_graph_evidence_justifies_source_missed_by_bounded_search(tmp_path, monkeypatch):
    root = _project(tmp_path)
    monkeypatch.setattr(
        align, "git_changed_files", lambda r, base="HEAD": ["core.py", "service.py"])
    intent = _intent(["core.py"], task="improve service process behaviour")

    rec = build_alignment(_mem(root), root, intent)

    assert rec["verdict"] == "aligned"
    assert rec["intent_justified_sources"] == ["service.py"]
    assert not any(g == "unstated-change: service.py" for g in rec["gaps"])


def test_one_weak_source_term_does_not_hide_scope_creep(tmp_path, monkeypatch):
    root = _project(tmp_path)
    monkeypatch.setattr(
        align, "git_changed_files", lambda r, base="HEAD": ["core.py", "app.py"])
    intent = _intent(["core.py"], task="improve app storage")

    rec = build_alignment(_mem(root), root, intent)

    assert rec["verdict"] == "partial"
    assert "unstated-change: app.py" in rec["gaps"]


def test_unverified_when_no_changes(tmp_path, monkeypatch):
    root = _project(tmp_path)
    monkeypatch.setattr(align, "git_changed_files", lambda r, base="HEAD": [])
    rec = build_alignment(_mem(root), root, _intent(["core.py"]))
    assert rec["verdict"] == "unverified"
    assert "nothing to align" in rec["headline"].lower()


def test_store_persists_intent_and_history(tmp_path):
    root = _project(tmp_path)
    store = AlignStore(root / ".memory")
    store.save_intent({"task": "t"})
    assert store.load_intent()["task"] == "t"
    store.save_alignment({"generated_at": "now", "intent": "t", "base": "HEAD",
                          "verdict": "aligned", "headline": "ok", "changed": ["a"], "gaps": []})
    hist = store.history()
    assert len(hist) == 1 and hist[0]["verdict"] == "aligned" and hist[0]["changed"] == 1


def test_capture_intent_persists_active_intent(tmp_path):
    root = _project(tmp_path)
    from cms.intent import capture_intent

    pack = capture_intent(root, goal="storage save logic")
    assert pack["intent_source"] == "explicit"
    assert AlignStore(root / ".memory").load_intent()["task"] == "storage save logic"


# ── MCP loop: declare_intent -> check_alignment ─────────────────────────────

def _server(tmp_path):
    from cms.mcp import MCPServer

    return MCPServer(_project(tmp_path))


def test_mcp_declares_intent_and_checks_alignment(tmp_path, monkeypatch):
    server = _server(tmp_path)
    tools = server.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})["result"]["tools"]
    names = {t["name"] for t in tools}
    assert {"declare_intent", "check_alignment"} <= names

    declared = server.declare_intent(goal="storage save logic")
    assert declared["intent"] == "storage save logic"

    monkeypatch.setattr(align, "git_changed_files", lambda r, base="HEAD": ["core.py"])
    checked = server.check_alignment()
    assert checked["verdict"] in ("aligned", "partial", "unverified", "drift")
    # intent was persisted by declare_intent, so check_alignment finds it
    assert "error" not in checked


def test_check_alignment_without_intent_errors(tmp_path):
    server = _server(tmp_path)
    assert "error" in server.check_alignment()
