from pathlib import Path

from cms.anchors import parse_anchors
from cms.graph_builder import build_graph
from cms.memory import CodebaseMemory
from cms.scanner import scan

SOURCE = '''\
"""Auth module."""

# === @memory:module:AuthLayer ===
# Purpose: Central authentication entry point
# Key flows: login -> verify -> session

# @memory:feature:UserAuthentication
# @memory:connects:LoginFlow, TokenService
# @memory:summary:Handles JWT issuance and refresh.
def login_user(name):
    return name


def plain_function():
    pass
'''


def test_parse_anchor_groups() -> None:
    groups = parse_anchors(SOURCE)
    assert len(groups) == 2

    block, line = groups
    assert block.tags["module"] == ["AuthLayer"]
    assert block.notes == [
        "Purpose: Central authentication entry point",
        "Key flows: login -> verify -> session",
    ]
    assert block.is_file_level

    assert line.tags["feature"] == ["UserAuthentication"]
    assert line.tags["connects"] == ["LoginFlow", "TokenService"]
    assert line.tags["summary"] == ["Handles JWT issuance and refresh."]
    assert not line.is_file_level


def test_anchors_attach_to_graph_nodes(tmp_path: Path) -> None:
    (tmp_path / "auth.py").write_text(SOURCE, encoding="utf-8")
    graph = build_graph(scan(tmp_path))

    func = graph.nodes["func:auth.py::login_user"]
    assert func["anchors"]["feature"] == ["UserAuthentication"]
    assert func["anchors"]["connects"] == ["LoginFlow", "TokenService"]

    file_node = graph.nodes["file:auth.py"]
    assert file_node["anchors"]["module"] == ["AuthLayer"]
    assert "notes" in file_node["anchors"]

    assert "anchors" not in graph.nodes["func:auth.py::plain_function"]


def test_anchors_boost_query_ranking(tmp_path: Path) -> None:
    (tmp_path / "auth.py").write_text(SOURCE, encoding="utf-8")
    mem = CodebaseMemory(build_graph(scan(tmp_path)))

    results = mem.query_intent("TokenService login flow", top_k=3)
    assert results
    assert results[0].node_id == "func:auth.py::login_user"
    assert results[0].anchors["feature"] == ["UserAuthentication"]
