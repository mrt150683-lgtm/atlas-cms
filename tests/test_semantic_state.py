"""Semantic-state evidence: legacy recovery, positive completion, failure
handling, judgment validity, and concurrency (the Ferro_Viz defect class).

The provider stubs here are test doubles for isolated unit tests only —
they never satisfy live acceptance.
"""

import json
import threading
import time
from pathlib import Path

import cms.semantic_state as ss
from cms.memory import CodebaseMemory
from cms.providers import MockProvider
from cms.update import ensure_judgment, incremental_update

GOOD_DISCOVERY = '[{"name": "Widgets", "description": "widget engine", "files": ["one.py"]}]'
GOOD_REVIEW = ('{"verdict": "aligned", "headline": "matches intent", "expected": "e",'
               ' "built": "b", "gaps": [], "education": "ed"}')
GOOD_SUGGEST = ('[{"title": "Add export", "description": "d", "kind": "feature",'
                ' "value": 4, "effort": 2, "rationale": "r", "builds_on": []}]')


class SemanticProvider:
    """Real-provider test double with scriptable discovery behaviour."""

    name = "semantic-test"
    model = "test-model-1"

    def __init__(self, discovery=GOOD_DISCOVERY, delay=0.0):
        self.discovery = discovery
        self.delay = delay
        self.discovery_calls = 0
        self.review_calls = 0
        self.suggest_calls = 0

    def summarize(self, prompt: str, context: dict) -> str:
        if "named FEATURES" in prompt:
            self.discovery_calls += 1
            if self.delay:
                time.sleep(self.delay)
            if isinstance(self.discovery, Exception):
                raise self.discovery
            return self.discovery
        if "reviewing one feature" in prompt or "top-level review" in prompt:
            self.review_calls += 1
            return GOOD_REVIEW
        if "return on investment" in prompt:
            self.suggest_calls += 1
            return GOOD_SUGGEST
        if "FEATURE TRACE" in prompt:
            return "## Purpose\nx\n## Flow\nx\n## Verification Checklist\n- x"
        return "Summary: real-looking file summary."


def _project(tmp_path: Path) -> Path:
    (tmp_path / "one.py").write_text("def f1():\n    pass\n", encoding="utf-8")
    return tmp_path


def _graph(root: Path):
    return CodebaseMemory.load(root / ".memory" / "graph.json").graph


def _make_legacy_stranded(root: Path) -> None:
    """Reproduce Ferro_Viz's exact stranded condition: real-looking
    summaries, zero features, no mock labels, NO semantic-state record,
    plus pre-existing empty judgment nodes."""
    incremental_update(root, SemanticProvider(discovery="[]"), echo=lambda *a: None)
    ss.state_path(root / ".memory").unlink()          # legacy: no evidence file
    mem = CodebaseMemory.load(root / ".memory" / "graph.json")
    mem.graph.add_node("review:app", type="review", name="App Review",
                       verdict="unverified",
                       headline="Structural pass only — run `cms review` with an API key.",
                       summary="0 features assembled with evidence.", counts={})
    mem.graph.add_node("suggestions:app", type="suggestions", name="Suggested Features",
                       items=[], provider="anthropic", summary="")
    mem.save(root / ".memory" / "graph.json")
    # preconditions of the stranded state
    g = mem.graph
    assert not [n for n, a in g.nodes(data=True) if a.get("type") == "feature"]
    assert not [a for _, a in g.nodes(data=True)
                if (a.get("summary_meta") or {}).get("provider") == "mock"]
    assert ss.load_state(root / ".memory") == {}


def test_legacy_stranded_project_recovers(tmp_path: Path) -> None:
    root = _project(tmp_path)
    _make_legacy_stranded(root)

    p = SemanticProvider()
    incremental_update(root, p, echo=lambda *a: None)
    assert p.discovery_calls == 1, "legacy (never_run) must trigger real discovery"
    g = _graph(root)
    assert g.nodes["feature:Widgets"]["source"] == "discovered"

    rec = ss.stage(ss.load_state(root / ".memory"), "features")
    assert rec["status"] == "complete" and rec["real_provider"] is True
    assert rec["provider"] == "semantic-test" and rec["model"] == "test-model-1"
    assert rec["input_hash"] and rec["feature_set_hash"]
    assert rec["discovered_feature_count"] == 1

    # invalid legacy judgment (no evidence, pre-discovery) is replaced
    ran = ensure_judgment(root, p, echo=lambda *a: None)
    assert ran == {"review": True, "suggestions": True}
    g = _graph(root)
    assert "Structural pass only" not in g.nodes["review:app"]["headline"]
    assert len(g.nodes["suggestions:app"]["items"]) == 1
    state = ss.load_state(root / ".memory")
    fsh = ss.feature_set_hash(g)
    assert ss.stage(state, "review")["feature_set_hash"] == fsh
    assert ss.stage(state, "suggestions")["feature_set_hash"] == fsh

    # unchanged second pass: no rerun, no recharge
    p2 = SemanticProvider()
    incremental_update(root, p2, echo=lambda *a: None)
    assert p2.discovery_calls == 0
    assert ensure_judgment(root, p2, echo=lambda *a: None) == \
        {"review": False, "suggestions": False}
    assert p2.review_calls == 0 and p2.suggest_calls == 0


def test_legitimate_zero_result_is_positive_completion(tmp_path: Path) -> None:
    root = _project(tmp_path)
    p = SemanticProvider(discovery="[]")
    incremental_update(root, p, echo=lambda *a: None)
    rec = ss.stage(ss.load_state(root / ".memory"), "features")
    assert rec["status"] == "complete"          # zero found is still SUCCESS
    assert rec["feature_count"] == 0
    assert rec["status"] != "never_run"         # distinguishable from never-ran

    p2 = SemanticProvider(discovery="[]")
    incremental_update(root, p2, echo=lambda *a: None)
    assert p2.discovery_calls == 0              # does not rerun indefinitely


def test_provider_failure_never_becomes_success(tmp_path: Path) -> None:
    root = _project(tmp_path)
    p = SemanticProvider(discovery=RuntimeError("api down"))
    incremental_update(root, p, echo=lambda *a: None)

    state = ss.load_state(root / ".memory")
    rec = ss.stage(state, "features")
    assert rec["status"] == "failed" and "api down" in rec["error"]
    g = _graph(root)
    assert not [n for n, a in g.nodes(data=True) if a.get("type") == "feature"]
    # no judgment may be built on top of a failed discovery
    assert ensure_judgment(root, p, echo=lambda *a: None) == \
        {"review": False, "suggestions": False}
    assert not g.has_node("review:app") and not g.has_node("suggestions:app")

    # recovery: input changes (new file) -> retry succeeds
    (root / "two.py").write_text("def f2():\n    pass\n", encoding="utf-8")
    p_ok = SemanticProvider()
    incremental_update(root, p_ok, echo=lambda *a: None)
    assert p_ok.discovery_calls == 1
    assert ss.stage(ss.load_state(root / ".memory"), "features")["status"] == "complete"
    assert _graph(root).has_node("feature:Widgets")


def test_malformed_output_keeps_last_known_good(tmp_path: Path) -> None:
    root = _project(tmp_path)
    incremental_update(root, SemanticProvider(), echo=lambda *a: None)
    good_rec = ss.stage(ss.load_state(root / ".memory"), "features")
    assert good_rec["status"] == "complete"

    (root / "two.py").write_text("def f2():\n    pass\n", encoding="utf-8")
    p_bad = SemanticProvider(discovery="sorry, here is prose with no JSON")
    incremental_update(root, p_bad, echo=lambda *a: None)

    rec = ss.stage(ss.load_state(root / ".memory"), "features")
    assert rec["status"] == "failed"            # malformed is never success
    assert "malformed" in rec["error"] or "JSON" in rec["error"]
    assert rec["last_success"]["status"] == "complete"   # evidence preserved
    assert _graph(root).has_node("feature:Widgets")      # artifacts intact

    (root / "three.py").write_text("def f3():\n    pass\n", encoding="utf-8")
    p_ok = SemanticProvider()
    incremental_update(root, p_ok, echo=lambda *a: None)
    assert ss.stage(ss.load_state(root / ".memory"), "features")["status"] == "complete"


def test_judgment_validity_matrix(tmp_path: Path) -> None:
    root = _project(tmp_path)
    incremental_update(root, SemanticProvider(), echo=lambda *a: None)
    g = _graph(root)
    fsh = ss.feature_set_hash(g)
    mk = lambda **kw: {"stages": {"review": kw}} if kw else {}

    # nodes absent
    assert ss.judgment_validity(mk(), g, "review:missing", "review")[0] == "missing"
    g.add_node("review:app", type="review", headline="x")
    # present, no semantic-state evidence (legacy)
    assert ss.judgment_validity({}, g, "review:app", "review")[0] == "invalid"
    # present but not real provider (mock/structural)
    assert ss.judgment_validity(
        mk(status="complete", real_provider=False), g, "review:app", "review")[0] == "invalid"
    # present but failed state
    assert ss.judgment_validity(
        mk(status="failed", real_provider=True), g, "review:app", "review")[0] == "invalid"
    # generated against empty pre-discovery feature set, features now exist
    assert ss.judgment_validity(
        mk(status="complete", real_provider=True, feature_count=0,
           feature_set_hash="old"), g, "review:app", "review")[0] == "invalid"
    # valid but feature set changed -> stale (frozen, not rebuilt)
    assert ss.judgment_validity(
        mk(status="complete", real_provider=True, feature_count=1,
           feature_set_hash="different"), g, "review:app", "review")[0] == "stale"
    # valid and current
    assert ss.judgment_validity(
        mk(status="complete", real_provider=True, feature_count=1,
           feature_set_hash=fsh), g, "review:app", "review")[0] == "valid"


def test_stale_valid_judgment_is_frozen_not_rebuilt(tmp_path: Path) -> None:
    root = _project(tmp_path)
    p = SemanticProvider()
    incremental_update(root, p, echo=lambda *a: None)
    assert ensure_judgment(root, p, echo=lambda *a: None)["review"] is True

    # change the feature set: add a declared feature via anchor
    (root / "two.py").write_text(
        "# @memory:feature:Anchored\ndef f2():\n    pass\n", encoding="utf-8")
    p2 = SemanticProvider()
    incremental_update(root, p2, echo=lambda *a: None)
    g = _graph(root)
    state = ss.load_state(root / ".memory")
    assert ss.stage(state, "review")["feature_set_hash"] != ss.feature_set_hash(g)

    # frozen: valid-but-stale must NOT be silently regenerated
    assert ensure_judgment(root, p2, echo=lambda *a: None)["review"] is False
    assert p2.review_calls == 0
    assert ss.judgment_validity(state, g, "review:app", "review")[0] == "stale"


def test_concurrent_updates_charge_discovery_once(tmp_path: Path) -> None:
    root = _project(tmp_path)
    p = SemanticProvider(delay=0.08)
    errors: list = []

    def work():
        try:
            incremental_update(root, p, echo=lambda *a: None)
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=work) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)
    assert not errors
    assert p.discovery_calls == 1, "concurrent updaters must not double-charge discovery"
    # no torn graph/state pair: both parse, agree on the discovered feature
    g = _graph(root)
    assert g.has_node("feature:Widgets")
    rec = ss.stage(ss.load_state(root / ".memory"), "features")
    assert rec["status"] == "complete"
    assert [f["name"] for f in rec["discovered_features"]] == ["Widgets"]


def test_concurrent_judgment_charges_once(tmp_path: Path) -> None:
    root = _project(tmp_path)
    p = SemanticProvider(delay=0.05)
    incremental_update(root, p, echo=lambda *a: None)

    results: list[dict] = []
    threads = [threading.Thread(target=lambda: results.append(
        ensure_judgment(root, p, echo=lambda *a: None))) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)
    built = [r for r in results if r["review"]]
    assert len(built) == 1, results
    # winner pays once: 1 feature review + 1 app rollup; loser pays nothing
    assert p.review_calls <= 2 and p.suggest_calls <= 1


def test_mock_never_creates_completion_markers(tmp_path: Path) -> None:
    root = _project(tmp_path)
    incremental_update(root, MockProvider(), echo=lambda *a: None)
    state = ss.load_state(root / ".memory")
    rec = ss.stage(state, "features")
    assert rec["status"] == "skipped" and rec["real_provider"] is False
    assert "real provider" in rec["reason"]
    assert ss.stage(state, "review")["status"] == "never_run"
    g = _graph(root)
    assert not g.has_node("review:app") and not g.has_node("suggestions:app")

    # and a mock run after a real success must not downgrade the evidence
    p = SemanticProvider()
    incremental_update(root, p, echo=lambda *a: None)
    incremental_update(root, MockProvider(), echo=lambda *a: None)
    assert ss.stage(ss.load_state(root / ".memory"), "features")["status"] == "complete"
