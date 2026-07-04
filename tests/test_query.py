from pathlib import Path

from cms.graph_builder import build_graph
from cms.memory import CodebaseMemory
from cms.providers import MockProvider
from cms.scanner import scan
from cms.summarizer import generate_summaries

SOURCE = '''\
"""Token verification middleware."""


def verify_token(token):
    """Check a JWT and return its claims."""
    return token


def unrelated_math(x):
    """Add numbers."""
    return x + 1
'''


def _memory(tmp_path: Path) -> CodebaseMemory:
    (tmp_path / "auth.py").write_text(SOURCE, encoding="utf-8")
    records = scan(tmp_path)
    graph = build_graph(records)
    generate_summaries(graph, tmp_path, MockProvider())
    return CodebaseMemory(graph)


def test_query_ranks_relevant_function_first(tmp_path: Path) -> None:
    mem = _memory(tmp_path)
    results = mem.query_intent("where is the token verification?", top_k=3)
    assert results
    assert results[0].name in ("verify_token", "auth.py")
    assert any(r.node_id == "func:auth.py::verify_token" for r in results)
    top_func = next(r for r in results if r.node_id == "func:auth.py::verify_token")
    assert top_func.lines == "4-6"


def test_query_roundtrip_through_json(tmp_path: Path) -> None:
    mem = _memory(tmp_path)
    graph_path = tmp_path / "graph.json"
    mem.save(graph_path)
    reloaded = CodebaseMemory.load(graph_path)
    results = reloaded.query_intent("verify token", top_k=2)
    assert results and results[0].score > 0


def test_who_calls_and_who_imports(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text("from b import target\n\ndef caller():\n    target()\n", encoding="utf-8")
    (tmp_path / "b.py").write_text("def target():\n    pass\n", encoding="utf-8")
    mem = CodebaseMemory(build_graph(scan(tmp_path)))
    assert mem.who_calls("target") == ["func:a.py::caller"]
    assert mem.who_imports("b.py") == ["file:a.py"]
