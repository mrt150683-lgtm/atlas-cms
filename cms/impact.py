"""Impact analysis — "if I change X, what is affected downstream?"

Walks the graph upstream: reverse CALLS (transitive callers), reverse IMPORTS
(files importing the containing file, transitively), then groups the blast
radius into functions/classes, files, features (via PART_OF), and tests.
Pure graph traversal — no LLM.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import networkx as nx


@dataclass
class Impact:
    target: str                       # resolved node id
    functions: list[str] = field(default_factory=list)
    files: list[str] = field(default_factory=list)
    features: list[str] = field(default_factory=list)
    tests: list[str] = field(default_factory=list)

    @property
    def total(self) -> int:
        return len(self.functions) + len(self.files) + len(self.features) + len(self.tests)


def resolve_target(graph: nx.DiGraph, target: str) -> str | None:
    """Accepts a node id, 'path::qualname', a rel path, or a bare name."""
    if graph.has_node(target):
        return target
    if "::" in target:
        path, qual = target.split("::", 1)
        for kind in ("func", "class"):
            nid = f"{kind}:{path}::{qual}"
            if graph.has_node(nid):
                return nid
    if graph.has_node(f"file:{target}"):
        return f"file:{target}"
    matches = [
        n for n, a in graph.nodes(data=True)
        if a.get("type") in ("func", "class", "file") and a.get("name") == target
    ]
    return sorted(matches)[0] if matches else None


def _is_test(path: str) -> bool:
    name = path.rsplit("/", 1)[-1]
    return "tests/" in path or path.startswith("test") or name.startswith("test_")


# @memory:feature:ImpactAnalysis
# @memory:connects:KnowledgeGraphConstruction, FeatureTracing, AgentMemoryAccess
# @memory:summary:Blast radius of changing a target — reverse CALLS/IMPORTS traversal grouped into affected functions, files, features and tests.
def analyze_impact(graph: nx.DiGraph, target: str) -> Impact | None:
    node_id = resolve_target(graph, target)
    if node_id is None:
        return None

    affected: set[str] = set()
    frontier = [node_id]
    while frontier:
        current = frontier.pop()
        for source, _, data in graph.in_edges(current, data=True):
            if data.get("type") in ("CALLS", "IMPORTS") and source not in affected and source != node_id:
                affected.add(source)
                frontier.append(source)
        # a change to a component ripples to its file, whose importers ripple on
        attrs = graph.nodes[current]
        if attrs.get("type") in ("func", "class"):
            file_id = f"file:{attrs['path']}"
            if graph.has_node(file_id) and file_id not in affected and file_id != node_id:
                affected.add(file_id)
                frontier.append(file_id)

    impact = Impact(target=node_id)
    feature_names: set[str] = set()
    for nid in sorted(affected):
        attrs = graph.nodes[nid]
        kind = attrs.get("type")
        path = attrs.get("path", "")
        for _, feat, d in graph.out_edges(nid, data=True):
            if d.get("type") == "PART_OF":
                feature_names.add(graph.nodes[feat]["name"])
        if _is_test(path):
            label = f"{path}::{attrs['qualname']}" if kind == "func" else path
            if label not in impact.tests:
                impact.tests.append(label)
        elif kind in ("func", "class"):
            impact.functions.append(f"{path}::{attrs.get('qualname', attrs.get('name'))}")
        elif kind == "file":
            impact.files.append(path)
    # features owning the target itself count too
    for _, feat, d in graph.out_edges(node_id, data=True):
        if d.get("type") == "PART_OF":
            feature_names.add(graph.nodes[feat]["name"])
    impact.features = sorted(feature_names)
    return impact
