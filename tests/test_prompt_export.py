import json
from pathlib import Path

from cms.exporter import export_graph
from cms.features import build_features
from cms.graph_builder import build_graph
from cms.prompt_export import export_prompt
from cms.providers import MockProvider
from cms.scanner import scan

SOURCE = '''\
# @memory:feature:TokenChecking
# @memory:summary:Validates JWT tokens.
def verify_token(token):
    """Check a JWT and return claims."""
    return decode(token)


def decode(token):
    return token
'''

CALLER = '''\
from auth import verify_token


def login(t):
    return verify_token(t)
'''


def _project(tmp_path: Path) -> Path:
    (tmp_path / "auth.py").write_text(SOURCE, encoding="utf-8")
    (tmp_path / "views.py").write_text(CALLER, encoding="utf-8")
    graph = build_graph(scan(tmp_path))
    build_features(graph, MockProvider())
    export_graph(graph, tmp_path / ".memory")
    return tmp_path


def test_markdown_prompt_grounded_in_memory(tmp_path: Path) -> None:
    root = _project(tmp_path)
    content, out = export_prompt(root, "harden the token verification logic")
    assert out.name == "harden-the-token-verification-logic.md"
    assert "# Task: harden the token verification logic" in content
    assert "auth.py" in content                      # where to work
    assert "verify_token" in content
    assert "TokenChecking" in content                # owning feature
    assert "## Blast radius" in content
    assert "views.py" in content                     # impact includes the caller's file
    assert "## Verify when done" in content


def test_json_pack_structure(tmp_path: Path) -> None:
    root = _project(tmp_path)
    content, out = export_prompt(root, "token verification", as_json=True)
    pack = json.loads(content)
    assert out.suffix == ".json"
    assert pack["task"] == "token verification"
    assert any(t["name"] == "verify_token" for t in pack["relevant_code"])
    assert pack["impact"]["target"].startswith(("func:", "file:"))
    assert pack["conventions"] and pack["verification"]
