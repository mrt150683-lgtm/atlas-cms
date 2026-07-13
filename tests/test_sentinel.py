"""Hermes Sentinel core logic — scanners, ledger audit, rules, store, gate."""

import json
from pathlib import Path

import pytest

from cms.sentinel import make_finding
from cms.sentinel.contracts import check_contracts
from cms.sentinel.domain_rules import check_domain_rules
from cms.sentinel.inventory import build_inventory
from cms.sentinel.ledger import audit_ledger, init_ledger, load_ledger
from cms.sentinel.providers_check import check_providers
from cms.sentinel.reports import as_bug_report, export_json, export_markdown
from cms.sentinel.runner import evaluate_gate, load_config, run_scan
from cms.sentinel.static_risk import scan_static_risks
from cms.sentinel.store import SentinelStore
from cms.sentinel.workflows import run_workflow_checks

REPO_ROOT = Path(__file__).resolve().parent.parent


def _patterns(findings):
    return {f["pattern"] for f in findings}


# ── project scanner ─────────────────────────────────────────────────────────

def test_inventory_detects_real_surfaces():
    inv = build_inventory(REPO_ROOT)
    assert inv["file_count"] > 20
    assert "cms/sentinel/runner.py" in inv["files"]["source"]
    assert any("sentinel" in c for c in inv["cli_commands"])
    assert "/api/graph" in inv["http_routes"]
    assert "/api/sentinel/latest" in inv["http_routes"]
    assert "query_codebase" in inv["mcp_tools"] and "get_sentinel_report" in inv["mcp_tools"]
    assert "sentinel.html" in inv["ui_pages"]
    assert any(f["name"] == "CleanDirectoryScanner" for f in inv["features"])


# ── static risk scanner ──────────────────────────────────────────────────────

def test_static_risk_detects_and_classifies(tmp_path):
    (tmp_path / "cms").mkdir()
    (tmp_path / "cms" / "core.py").write_text(
        "# TODO handle errors\n"
        "def validate_user(token):\n"
        "    return True\n",
        encoding="utf-8",
    )
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_core.py").write_text("# TODO later\n", encoding="utf-8")
    findings = scan_static_risks(tmp_path)

    trivial = [f for f in findings if f["pattern"] == "trivial-validator"]
    assert trivial and trivial[0]["severity"] == "critical"
    assert trivial[0]["file"] == "cms/core.py" and trivial[0]["line"] == 2

    todos = {f["file"]: f["severity"] for f in findings if f["pattern"] == r"\bTODO\b"}
    assert todos == {"cms/core.py": "low"}  # fixtures do not become active findings


def test_static_risk_ignores_own_pattern_registry():
    findings = scan_static_risks(REPO_ROOT)
    own = [f for f in findings if f["file"].startswith("cms/sentinel/")]
    assert all(f["pattern"] in ("trivial-validator", "substring-traversal-guard")
               for f in own)


def test_static_risk_excludes_documentation_and_fixture_markers(tmp_path):
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "risks.md").write_text(
        "Examples: FIXME, fake success, bypass validation, placeholder.\n",
        encoding="utf-8",
    )
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_fixture.py").write_text(
        "# TODO fixture\ndef validate_fixture():\n    return True\n",
        encoding="utf-8",
    )
    assert scan_static_risks(tmp_path) == []


def test_static_risk_flags_substring_traversal_guard(tmp_path):
    (tmp_path / "cms").mkdir()
    (tmp_path / "cms" / "srv.py").write_text(
        "def get_source(path):\n"
        "    if '..' in path:\n"          # bypassable — absolute paths have no '..'
        "        return {'error': 'nope'}\n"
        "    return open(path).read()\n"
        "def ok(version):\n"
        "    return '..' in version\n",   # not a path -> must NOT flag
        encoding="utf-8",
    )
    findings = scan_static_risks(tmp_path)
    weak = [f for f in findings if f["pattern"] == "substring-traversal-guard"]
    assert len(weak) == 1 and weak[0]["severity"] == "high"
    assert weak[0]["line"] == 2 and weak[0]["file"] == "cms/srv.py"


def test_static_risk_clean_on_this_repo_has_no_weak_guards():
    # production code uses resolved-parent containment everywhere
    findings = scan_static_risks(REPO_ROOT)
    weak = [f for f in findings
            if f["pattern"] == "substring-traversal-guard" and f["file"].startswith("cms/")]
    assert not weak, [f["file"] for f in weak]


# ── feature ledger ───────────────────────────────────────────────────────────

def _write_graph(root, nodes):
    memory = root / ".memory"
    memory.mkdir(parents=True, exist_ok=True)
    (memory / "graph.json").write_text(
        json.dumps({"directed": True, "nodes": nodes, "links": []}), encoding="utf-8"
    )


def test_ledger_missing_is_flagged(tmp_path):
    findings = audit_ledger(tmp_path)
    assert "missing-ledger" in _patterns(findings)


def test_ledger_parses_and_flags_unbacked_completion(tmp_path):
    _write_graph(tmp_path, [
        {"id": "feature:Real", "type": "feature", "name": "Real",
         "members": [], "verified_by": ["tests/test_x.py::test_a"]},
    ])
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "feature_ledger.json").write_text(json.dumps({"features": [
        {"feature": "Ghost", "status": "complete",
         "evidence": {"files": ["cms/nope.py"], "tests": []}},
        {"feature": "Real", "status": "complete", "evidence": {}},
    ]}), encoding="utf-8")

    entries, errors = load_ledger(tmp_path)
    assert len(entries) == 2 and not errors

    patterns = _patterns(audit_ledger(tmp_path))
    assert "missing-evidence-file" in patterns       # Ghost's file doesn't exist
    assert "complete-without-tests" in patterns      # Ghost claims complete, no tests
    assert "complete-without-evidence" in patterns   # Ghost isn't in the graph either
    # Real has graph verified_by, so no completion complaint about it
    assert not any(f["feature"] == "Real" and f["area"] == "ledger_completion"
                   for f in audit_ledger(tmp_path))


def test_ledger_flags_unledgered_graph_features(tmp_path):
    _write_graph(tmp_path, [
        {"id": "feature:Orphan", "type": "feature", "name": "Orphan", "members": []},
    ])
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "feature_ledger.json").write_text(
        json.dumps({"features": []}), encoding="utf-8")
    findings = audit_ledger(tmp_path)
    assert any(f["pattern"] == "unledgered-feature" and f["feature"] == "Orphan"
               for f in findings)


def test_ledger_init_generates_conservative_statuses(tmp_path):
    _write_graph(tmp_path, [
        {"id": "feature:Tested", "type": "feature", "name": "Tested",
         "members": ["file:a.py"], "exercised_by": ["tests/test_a.py::test_it"]},
        {"id": "feature:Untested", "type": "feature", "name": "Untested",
         "members": ["file:b.py"], "exercised_by": []},
    ])
    out = init_ledger(tmp_path)
    data = json.loads(out.read_text(encoding="utf-8"))
    by_name = {e["feature"]: e for e in data["features"]}
    assert by_name["Tested"]["status"] == "complete"
    assert by_name["Untested"]["status"] == "in_progress"
    with pytest.raises(FileExistsError):
        init_ledger(tmp_path)


# ── contracts ────────────────────────────────────────────────────────────────

def test_contract_checker_detects_unhandled_ui_fetch(tmp_path):
    (tmp_path / "cms" / "ui_assets").mkdir(parents=True)
    (tmp_path / "cms" / "ui.py").write_text(
        'if url.path == "/api/graph":\n    pass\n', encoding="utf-8")
    (tmp_path / "cms" / "ui_assets" / "index.html").write_text(
        'fetch("/api/graph"); fetch("/api/missing_endpoint");', encoding="utf-8")
    findings = check_contracts(tmp_path)
    unhandled = [f for f in findings if f["pattern"] == "unhandled-endpoint"]
    assert len(unhandled) == 1 and "/api/missing_endpoint" in unhandled[0]["summary"]
    assert unhandled[0]["severity"] == "high"


def test_mcp_docs_contract_flags_stale_tool_list(tmp_path):
    from cms.mcp import TOOLS

    all_names = " ".join(f"`{t['name']}`" for t in TOOLS)
    (tmp_path / "README.md").write_text("Tools: `query_codebase`.", encoding="utf-8")
    (tmp_path / "SKILL.md").write_text(f"## 3. MCP tools (2)\n{all_names}", encoding="utf-8")
    findings = check_contracts(tmp_path)
    stale = [f for f in findings if f["pattern"] == "undocumented-mcp-tool"]
    assert len(stale) == 1 and stale[0]["file"] == "README.md"
    counts = [f for f in findings if f["pattern"] == "stale-mcp-tool-count"]
    assert len(counts) == 1 and counts[0]["file"] == "SKILL.md"


def test_mcp_docs_contract_clean_when_docs_current(tmp_path):
    from cms.mcp import TOOLS

    body = f"## 3. MCP tools ({len(TOOLS)})\n" + " ".join(f"`{t['name']}`" for t in TOOLS)
    (tmp_path / "README.md").write_text(body, encoding="utf-8")
    (tmp_path / "SKILL.md").write_text(body, encoding="utf-8")
    assert [f for f in check_contracts(tmp_path) if f["area"] == "mcp_docs_contract"] == []


def test_contracts_clean_on_this_repo():
    findings = check_contracts(REPO_ROOT)
    assert not [f for f in findings if f["severity"] in ("critical", "high")], \
        [f["summary"] for f in findings if f["severity"] in ("critical", "high")]
    # the docs↔live-surface contract must hold on our own repo (dogfood)
    assert not [f for f in findings if f["area"] == "mcp_docs_contract"], \
        [f["summary"] for f in findings if f["area"] == "mcp_docs_contract"]


# ── domain rules ─────────────────────────────────────────────────────────────

def test_domain_rules_flag_missing_graph(tmp_path):
    findings = check_domain_rules(tmp_path)
    assert "no-graph" in _patterns(findings)


def test_domain_rules_flag_provenance_and_ghost_members(tmp_path):
    _write_graph(tmp_path, [
        {"id": "file:a.py", "type": "file", "path": "a.py",
         "summary": "does things"},                      # no summary_meta
        {"id": "feature:F", "type": "feature", "name": "F",
         "members": ["file:gone.py"], "summary": "trace",
         "narrative_provider": "mock"},                  # ghost member
        {"id": "suggestions:app", "type": "suggestions",
         "items": [{"title": "x", "value": 4, "effort": 2, "roi": 9.0}]},
    ])
    patterns = _patterns(check_domain_rules(tmp_path))
    assert "summary-without-provider" in patterns
    assert "ghost-members" in patterns
    assert "roi-mismatch" in patterns


def test_domain_rules_clean_on_this_repo():
    findings = [f for f in check_domain_rules(REPO_ROOT)
                if f["severity"] in ("critical", "high")]
    assert not findings, [f["summary"] for f in findings]


def test_security_rule_audits_both_source_surfaces(tmp_path):
    from cms.sentinel.domain_rules import _rule_security

    (tmp_path / "cms").mkdir()
    # HTTP surface fine; MCP get_source present but guard string removed
    (tmp_path / "cms" / "ui.py").write_text(
        'ThreadingHTTPServer(("127.0.0.1", port), h)\n'
        '# path outside project root\n', encoding="utf-8")
    (tmp_path / "cms" / "mcp.py").write_text(
        "def get_source(self, path):\n    return open(path).read()\n", encoding="utf-8")
    patterns = {(f["file"], f["pattern"]) for f in _rule_security(tmp_path)}
    assert ("cms/mcp.py", "missing-traversal-guard") in patterns
    assert ("cms/ui.py", "missing-traversal-guard") not in patterns


def test_workflow_traversal_check_rejects_absolute_paths(tmp_path):
    # the hardened check must fail when a guard blocks '../' but not absolute paths.
    # `DOTDOT` (not a literal '..' in this test's own AST) so the scanner doesn't
    # flag this deliberately-vulnerable mock as a weak guard in the test file.
    import cms.mcp as mcp_mod
    from cms.config import LANGUAGE_BY_EXTENSION
    from cms.sentinel.workflows import _wf_mcp_traversal_blocked

    orig = mcp_mod.MCPServer.get_source
    DOTDOT = "." + "."

    def leaky(self, path, start_line=1, end_line=None):
        if DOTDOT in path:               # bug-10: blocks '../' but not absolute paths
            return {"error": "path outside project root"}
        target = (self.root / path).resolve()
        if target.suffix.lower() not in LANGUAGE_BY_EXTENSION or not target.is_file():
            return {"error": "not a scanned source file"}
        return {"path": path, "source": target.read_text(encoding="utf-8", errors="replace")}

    mcp_mod.MCPServer.get_source = leaky
    try:
        with pytest.raises(AssertionError, match="OUTSIDE the project root"):
            _wf_mcp_traversal_blocked(tmp_path)
    finally:
        mcp_mod.MCPServer.get_source = orig
    # and it passes against the real, correct guard
    (tmp_path / "clean").mkdir()
    assert "refused all" in _wf_mcp_traversal_blocked(tmp_path / "clean")


# ── workflows ────────────────────────────────────────────────────────────────

def test_workflow_checks_all_pass_on_real_pipeline(tmp_path):
    results, findings = run_workflow_checks(tmp_path)
    assert len(results) >= 7
    failed = [r for r in results if r["passed"] is not True]
    assert not failed, failed
    assert findings == []
    assert all(r["mode"] == "mock" for r in results)


# ── providers (driver validation) ───────────────────────────────────────────

def test_provider_validator_clean_on_real_providers():
    findings = check_providers(REPO_ROOT)
    assert not [f for f in findings if f["severity"] in ("critical", "high")], \
        [f["summary"] for f in findings]


# ── store, bug reports, gate ────────────────────────────────────────────────

def _scan_with(findings, modules=("static_risk",)):
    return {"scan_id": "scan-test", "created_at": "2026-07-05T00:00:00Z",
            "execution_mode": "mock", "modules_run": list(modules),
            "module_errors": {}, "findings": findings, "workflow_checks": [], "gate": {}}


def test_store_persists_statuses_and_tracks_regressions(tmp_path):
    store = SentinelStore(tmp_path / ".memory")
    finding = make_finding("static_risk", "critical", "bad thing", file="a.py", pattern="p")

    merged = store.merge_scan(_scan_with([finding]))
    (fp, stored) = next(iter(merged.items()))
    assert stored["status"] == "open" and stored["bug_id"] == "BUG-000001"

    # status updates persist; false positives need a reason
    with pytest.raises(ValueError):
        store.set_status(stored["bug_id"], "false_positive", "")
    updated = store.set_status(stored["bug_id"], "acknowledged")
    assert updated["status"] == "acknowledged"
    assert store.load_findings()[fp]["status"] == "acknowledged"  # survives reload

    # not re-detected while its module ran clean -> auto-resolved
    store.merge_scan(_scan_with([]))
    assert store.load_findings()[fp]["status"] == "resolved"

    # re-detected after resolution -> reopened with the same bug id
    reopened = store.merge_scan(_scan_with([finding]))[fp]
    assert reopened["status"] == "open" and reopened["bug_id"] == "BUG-000001"

    # module errored -> its missing findings are NOT auto-resolved
    scan = _scan_with([])
    scan["module_errors"] = {"static_risk": "boom"}
    assert store.merge_scan(scan)[fp]["status"] == "open"


def test_bug_report_generation_and_export(tmp_path):
    store = SentinelStore(tmp_path / ".memory")
    finding = make_finding(
        "workflows", "critical", "carry-over lost data", area="workflow",
        pattern="carry_over", risk="evidence wiped", recommendation="fix _carry_over",
    )
    merged = store.merge_scan(_scan_with([finding], modules=("workflows",)))
    stored = next(iter(merged.values()))

    report = as_bug_report(stored)
    for key in ("bug_id", "severity", "summary", "evidence", "risk",
                "recommended_fix", "required_regression_test", "created_at"):
        assert report[key] or key in ("evidence",), key
    assert report["bug_id"].startswith("BUG-")

    md = export_markdown(store.latest_scan(), merged)
    assert "Hermes Sentinel Report" in md and report["bug_id"] in md
    data = json.loads(export_json(store.latest_scan(), merged))
    assert data["bug_reports"][0]["bug_id"] == report["bug_id"]


def test_gate_fails_on_active_critical_only(tmp_path):
    critical = dict(make_finding("m", "critical", "boom"), status="open")
    cfg = {"fail_on": ["critical"], "warn_on": ["high"]}
    assert evaluate_gate({"a": critical}, cfg)["failed"] is True
    dismissed = dict(critical, status="false_positive")
    assert evaluate_gate({"a": dismissed}, cfg)["failed"] is False
    high = dict(make_finding("m", "high", "meh"), status="open")
    gate = evaluate_gate({"a": high}, cfg)
    assert gate["failed"] is False and gate["warnings"]


def test_run_scan_persists_results(tmp_path):
    (tmp_path / "app.py").write_text("def go():\n    return 1\n", encoding="utf-8")
    scan, findings = run_scan(tmp_path)
    assert set(scan["modules_run"]) >= {"inventory", "static_risk", "ledger",
                                        "contracts", "workflows", "domain_rules", "providers"}
    assert scan["execution_mode"] in ("mock", "live")
    store = SentinelStore(tmp_path / ".memory")
    assert store.latest_scan()["scan_id"] == scan["scan_id"]  # survives restart
    assert store.scan_history()
    # fixture has no memory layer -> sentinel must say so, not fake a pass
    assert any(f["pattern"] in ("no-graph", "inventory-warning") for f in findings.values())


def test_config_defaults_and_file_override(tmp_path):
    assert load_config(tmp_path)["fail_on"] == ["critical"]
    (tmp_path / "sentinel.config.json").write_text(
        json.dumps({"fail_on": ["critical", "high"]}), encoding="utf-8")
    assert load_config(tmp_path)["fail_on"] == ["critical", "high"]
