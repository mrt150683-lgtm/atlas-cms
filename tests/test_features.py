from pathlib import Path

from cms.features import Feature, build_features, collect_declared_features, get_features, prepare_known, trace_flows
from cms.graph_builder import build_graph
from cms.memory import CodebaseMemory
from cms.providers import MockProvider
from cms.scanner import scan

SOURCE = '''\
"""Pipeline module."""


# @memory:feature:DataPipeline
# @memory:connects:Reporting
# @memory:summary:Loads, cleans and stores records.
def run_pipeline(path):
    data = load(path)
    cleaned = clean(data)
    store(cleaned)


def load(path):
    return path


def clean(data):
    return normalize(data)


def normalize(data):
    return data


def store(data):
    pass


# @memory:feature:Reporting
def make_report():
    return load("x")
'''


def _graph(tmp_path: Path):
    (tmp_path / "pipe.py").write_text(SOURCE, encoding="utf-8")
    return build_graph(scan(tmp_path))


def test_declared_features_collected(tmp_path: Path) -> None:
    graph = _graph(tmp_path)
    feats = collect_declared_features(graph)
    assert set(feats) == {"DataPipeline", "Reporting"}
    dp = feats["DataPipeline"]
    assert dp.members == ["func:pipe.py::run_pipeline"]
    assert dp.connects == ["Reporting"]
    assert dp.description == "Loads, cleans and stores records."


def test_trace_flows_follows_calls(tmp_path: Path) -> None:
    graph = _graph(tmp_path)
    entries, flows = trace_flows(graph, ["func:pipe.py::run_pipeline"])
    assert entries == ["func:pipe.py::run_pipeline"]
    assert flows, "expected at least one flow"
    names = [s["name"] for s in flows[0]]
    assert names[0] == "run_pipeline"
    assert "load" in names or "clean" in names  # walked into callees
    steps = {s["name"]: s for s in flows[0]}
    assert steps["run_pipeline"]["path"] == "pipe.py"
    assert steps["run_pipeline"]["line"] == 7


def test_build_features_writes_graph_nodes(tmp_path: Path) -> None:
    graph = _graph(tmp_path)
    feats = build_features(graph, MockProvider())
    assert {f.name for f in feats} == {"DataPipeline", "Reporting"}

    node = graph.nodes["feature:DataPipeline"]
    assert node["type"] == "feature"
    assert node["summary"]  # narrative present
    assert "Verification Checklist" in node["summary"]
    assert graph.edges["func:pipe.py::run_pipeline", "feature:DataPipeline"]["type"] == "PART_OF"
    assert graph.edges["feature:DataPipeline", "feature:Reporting"]["type"] == "CONNECTS"

    listing = get_features(graph)
    assert [f["name"] for f in listing] == ["DataPipeline", "Reporting"]


def test_derived_relations_between_features(tmp_path: Path) -> None:
    (tmp_path / "store.py").write_text(
        "# @memory:feature:Storage\ndef save(x):\n    return x\n", encoding="utf-8"
    )
    (tmp_path / "api.py").write_text(
        "from store import save\n\n\n# @memory:feature:Api\ndef handle(x):\n    return save(x)\n",
        encoding="utf-8",
    )
    graph = build_graph(scan(tmp_path))
    build_features(graph, MockProvider())
    # no declared connects between Api and Storage, but handle() calls save()
    edge = graph.edges["feature:Api", "feature:Storage"]
    assert edge["type"] == "RELATES"


def test_features_queryable(tmp_path: Path) -> None:
    graph = _graph(tmp_path)
    build_features(graph, MockProvider())
    mem = CodebaseMemory(graph)
    results = mem.query_intent("DataPipeline feature trace", top_k=3)
    assert any(r.node_id == "feature:DataPipeline" for r in results)


def test_discovered_synonyms_collapse_to_canonical_feature_with_aliases(tmp_path: Path) -> None:
    (tmp_path / "tree_export.py").write_text("def export():\n    pass\n", encoding="utf-8")
    (tmp_path / "other.py").write_text("def separate():\n    pass\n", encoding="utf-8")
    graph = build_graph(scan(tmp_path))
    extras = [
        Feature(name="NestedTreeStructureExport", source="discovered",
                members=["file:tree_export.py"]),
        Feature(name="CodebaseDocumentationExport", source="discovered",
                members=["file:tree_export.py"]),
        Feature(name="CodebaseTreeExport", source="discovered",
                members=["file:tree_export.py"]),
        Feature(name="SeparateCapability", source="discovered",
                members=["file:other.py"]),
    ]

    features, _, _ = prepare_known(graph, extras)

    assert set(features) == {"CodebaseDocumentationExport", "SeparateCapability"}
    canonical = features["CodebaseDocumentationExport"]
    assert canonical.aliases == ["CodebaseTreeExport", "NestedTreeStructureExport"]

    build_features(graph, MockProvider(), extra_features=extras, discover=False)
    node = graph.nodes["feature:CodebaseDocumentationExport"]
    assert node["aliases"] == ["CodebaseTreeExport", "NestedTreeStructureExport"]
    assert any(
        result.node_id == "feature:CodebaseDocumentationExport"
        for result in CodebaseMemory(graph).query_intent("NestedTreeStructureExport")
    )
