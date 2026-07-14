"""Semantic hierarchy: structural fallback honesty, LLM grouping, evidence
gating (never re-charge unchanged input), durable-state carry-over across
rebuilds, and failure recording."""

import json
from pathlib import Path

import pytest

from cms import semantic_state as ss
from cms.features import build_features
from cms.graph_builder import build_graph
from cms.hierarchy import (
    HierarchyError,
    _parse_spec,
    ensure_hierarchy,
    hierarchy_input_hash,
    structural_spec,
    top_dirs,
    write_hierarchy,
)
from cms.providers import MockProvider
from cms.scanner import scan
from cms.update import incremental_update

APP = '''\
# @memory:feature:Greeting
def greet(name):
    return helper(name)


def helper(name):
    return name
'''

UTIL = '''\
# @memory:feature:Shouting
def shout(name):
    return name.upper()
'''


def _project(tmp_path: Path):
    (tmp_path / "app").mkdir()
    (tmp_path / "util").mkdir()
    (tmp_path / "app" / "main.py").write_text(APP, encoding="utf-8")
    (tmp_path / "util" / "loud.py").write_text(UTIL, encoding="utf-8")
    graph = build_graph(scan(tmp_path))
    build_features(graph, MockProvider())
    memory = tmp_path / ".memory"
    memory.mkdir(exist_ok=True)
    return graph, memory


class GroupingProvider:
    """Real-provider stand-in returning a fixed two-component grouping."""

    name = "fake"
    model = "fake-1"

    def __init__(self, fail: bool = False) -> None:
        self.calls = 0
        self.fail = fail

    def summarize(self, prompt: str, context: dict) -> str:
        self.calls += 1
        if self.fail:
            raise RuntimeError("provider down")
        return json.dumps({"systems": [{
            "name": "TestApp", "description": "The whole app.",
            "components": [
                {"name": "Greetings", "description": "Says hello.",
                 "features": ["Greeting"], "dirs": ["app"]},
                {"name": "Volume", "description": "Loudness tools.",
                 "features": ["Shouting"], "dirs": ["util"]},
            ]}]})


def test_structural_spec_groups_by_top_dir(tmp_path: Path) -> None:
    graph, _ = _project(tmp_path)
    assert top_dirs(graph) == ["app", "util"]
    spec = structural_spec(graph, "proj")
    assert len(spec["systems"]) == 1
    comps = {c["name"]: c for c in spec["systems"][0]["components"]}
    assert comps["App"]["features"] == ["Greeting"]
    assert comps["Util"]["features"] == ["Shouting"]
    # honesty: structural output labels itself
    assert "run `cms update` with an API key" in spec["systems"][0]["description"]


def test_mock_writes_labelled_nodes_but_never_completion(tmp_path: Path) -> None:
    graph, memory = _project(tmp_path)
    ran = ensure_hierarchy(memory, graph, MockProvider(), echo=lambda *_: None)
    assert ran is False
    systems = [n for n, a in graph.nodes(data=True) if a.get("type") == "system"]
    assert len(systems) == 1
    assert graph.nodes[systems[0]]["provenance"] == "heuristic"
    rec = ss.stage(ss.load_state(memory), "hierarchy")
    assert rec["status"] == "skipped"  # never a completion marker under mock


def test_llm_grouping_builds_part_of_chain_and_records_evidence(tmp_path: Path) -> None:
    graph, memory = _project(tmp_path)
    provider = GroupingProvider()
    ran = ensure_hierarchy(memory, graph, provider, echo=lambda *_: None)
    assert ran is True and provider.calls == 1
    # feature -> component -> system PART_OF chain
    assert graph.has_edge("feature:Greeting", "component:Greetings")
    assert graph.has_edge("component:Greetings", "system:TestApp")
    assert graph.edges["feature:Greeting", "component:Greetings"]["type"] == "PART_OF"
    assert graph.edges["feature:Greeting", "component:Greetings"]["provenance"] == "llm"
    rec = ss.stage(ss.load_state(memory), "hierarchy")
    assert rec["status"] == "complete" and rec["real_provider"] is True
    assert rec["input_hash"] == hierarchy_input_hash(graph)
    assert rec["hierarchy_spec"]["systems"][0]["name"] == "TestApp"


def test_unchanged_input_reapplies_from_state_without_charging(tmp_path: Path) -> None:
    graph, memory = _project(tmp_path)
    provider = GroupingProvider()
    ensure_hierarchy(memory, graph, provider, echo=lambda *_: None)
    # simulate the full rebuild wipe: hierarchy nodes vanish from a new graph
    graph2 = build_graph(scan(tmp_path))
    build_features(graph2, MockProvider())
    dead = GroupingProvider(fail=True)
    ran = ensure_hierarchy(memory, graph2, dead, echo=lambda *_: None)
    assert ran is False and dead.calls == 0  # served from durable state
    assert graph2.has_node("system:TestApp")
    assert graph2.has_edge("component:Volume", "system:TestApp")


def test_failure_records_failed_and_keeps_structural_view(tmp_path: Path) -> None:
    graph, memory = _project(tmp_path)
    ran = ensure_hierarchy(memory, graph, GroupingProvider(fail=True), echo=lambda *_: None)
    assert ran is False
    rec = ss.stage(ss.load_state(memory), "hierarchy")
    assert rec["status"] == "failed" and "provider" in rec
    # view still usable, honestly labelled
    systems = [n for n, a in graph.nodes(data=True) if a.get("type") == "system"]
    assert systems and graph.nodes[systems[0]]["provenance"] == "heuristic"


def test_unassigned_features_land_in_other_component() -> None:
    spec = _parse_spec(json.dumps({"systems": [{
        "name": "S", "description": "", "components": [
            {"name": "C", "description": "", "features": ["Known"], "dirs": []}]}]}),
        {"Known", "Orphan"})
    comps = {c["name"]: c for c in spec["systems"][0]["components"]}
    assert comps["Other"]["features"] == ["Orphan"]
    with pytest.raises(HierarchyError):
        _parse_spec("no json here", {"A"})
    with pytest.raises(HierarchyError):
        _parse_spec('{"systems": []}', {"A"})


def test_hierarchy_survives_incremental_update(tmp_path: Path) -> None:
    """The critical carry-over: incremental_update rebuilds the graph from
    scratch; the recorded grouping must be re-applied, not wiped."""
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "main.py").write_text(APP, encoding="utf-8")
    incremental_update(tmp_path, MockProvider(), echo=lambda *_: None)
    memory = tmp_path / ".memory"
    # inject a real recorded grouping (as if a real provider had run)
    graph = build_graph(scan(tmp_path))
    build_features(graph, MockProvider())
    spec = {"systems": [{"name": "Solo", "description": "d", "components": [
        {"name": "Core", "description": "d", "features": ["Greeting"], "dirs": ["app"]}]}]}
    ss.record_stage(memory, "hierarchy", status="complete", provider="fake",
                    real_provider=True, input_hash=hierarchy_input_hash(graph),
                    hierarchy_spec=spec, systems=1, components=1)
    incremental_update(tmp_path, MockProvider(), echo=lambda *_: None)
    from cms.memory import CodebaseMemory

    saved = CodebaseMemory.load(memory / "graph.json").graph
    assert saved.has_node("system:Solo") and saved.has_node("component:Core")
    assert saved.has_edge("feature:Greeting", "component:Core")
    assert saved.graph.get("schema_version") == 2
    # mock update must not downgrade the real completion record
    assert ss.stage(ss.load_state(memory), "hierarchy")["status"] == "complete"


def test_write_hierarchy_is_idempotent(tmp_path: Path) -> None:
    graph, _ = _project(tmp_path)
    spec = structural_spec(graph, "p")
    write_hierarchy(graph, spec, "heuristic")
    before = sorted(n for n, a in graph.nodes(data=True)
                    if a.get("type") in ("system", "component"))
    write_hierarchy(graph, spec, "heuristic")
    after = sorted(n for n, a in graph.nodes(data=True)
                   if a.get("type") in ("system", "component"))
    assert before == after
