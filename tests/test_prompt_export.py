import json
from pathlib import Path

import pytest

from cms import config
from cms.exporter import export_graph
from cms.features import build_features
from cms.graph_builder import build_graph
from cms.library import LibraryView
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
    content, out = export_prompt(
        root, "change `auth.py` and docs/security.md for token verification", as_json=True)
    pack = json.loads(content)
    assert out.suffix == ".json"
    assert pack["task"] == "change `auth.py` and docs/security.md for token verification"
    assert pack["declared_paths"] == ["auth.py", "docs/security.md"]
    assert any(t["name"] == "verify_token" for t in pack["relevant_code"])
    assert pack["impact"]["target"].startswith(("func:", "file:"))
    assert pack["conventions"] and pack["verification"]
    assert pack["library"] is None  # no assets selected -> no library section


@pytest.fixture
def _isolated_library(tmp_path, monkeypatch):
    """Point the built-in + user scopes somewhere empty, so only the project's
    own assets take part."""
    monkeypatch.setenv("CMS_LIBRARY_BUILTIN", str(tmp_path / "no-builtins"))
    monkeypatch.setattr(config, "LIBRARY_USER_DIR", tmp_path / "no-userlib")


def _publish(root: Path, asset_id: str, type: str, body: str, **front) -> None:
    lines = ["---", f"id: {asset_id}", f"name: {asset_id.title()}", f"type: {type}",
             "description: A test asset."]
    lines += [f"{k}: [{', '.join(v)}]" for k, v in front.items()]
    lines += ["---", "", body]
    store = LibraryView(root).store("project")
    store.save_draft("\n".join(lines))
    store.publish(asset_id, "tester")


def test_selected_assets_compose_into_the_pack(tmp_path: Path, _isolated_library) -> None:
    root = _project(tmp_path)
    _publish(root, "house-rules", "constraint", "Never touch generated files.")
    _publish(root, "tdd-flow", "strategy", "Write the failing test first.")
    _publish(root, "backend-base", "profile", "The backend baseline.",
             assets=["house-rules@1", "tdd-flow@1"])

    content, _ = export_prompt(root, "harden token verification",
                               as_json=True, assets=["backend-base"])
    pack = json.loads(content)
    lib = pack["library"]
    assert lib["selection"] == ["backend-base"]
    used = {a["id"]: a for a in lib["assets"]}
    assert set(used) == {"backend-base", "house-rules", "tdd-flow"}  # profile expanded
    # exact provenance is recorded so the run can be reproduced
    assert used["house-rules"]["version"] == 1
    assert len(used["house-rules"]["content_hash"]) == 24
    assert used["house-rules"]["scope"] == "project"
    assert lib["est_tokens"] > 0 and lib["oversized"] is False

    markdown, _ = export_prompt(root, "harden token verification",
                                assets=["backend-base"])
    assert "## Library context" in markdown
    assert "Never touch generated files." in markdown       # canonical body verbatim
    assert "house-rules@v1" in markdown                     # version named
    # rules land before the code targets
    assert markdown.index("## Library context") < markdown.index("## Where to work")


def test_pack_surfaces_library_conflicts_and_gaps(tmp_path: Path, _isolated_library) -> None:
    root = _project(tmp_path)
    _publish(root, "dark-mode", "preference", "Dark, high contrast.",
             conflicts_with=["light-mode"])
    _publish(root, "light-mode", "preference", "Light and airy.")
    _publish(root, "needs-ghost", "skill", "Depends on something absent.",
             requires=["ghost-asset"])

    content, _ = export_prompt(root, "restyle the viewer", as_json=True,
                               assets=["dark-mode", "light-mode", "needs-ghost"])
    lib = json.loads(content)["library"]
    assert lib["conflicts"] == [{"a": "dark-mode", "b": "light-mode",
                                 "declared_by": ["dark-mode"]}]
    assert any(w["kind"] == "missing-dependency" and w["id"] == "ghost-asset"
               for w in lib["warnings"])
    assert {a["id"] for a in lib["assets"]} >= {"dark-mode", "light-mode"}  # both kept

    markdown, _ = export_prompt(root, "restyle the viewer",
                                assets=["dark-mode", "light-mode", "needs-ghost"])
    assert "CONFLICT" in markdown and "do not silently pick one" in markdown
    assert "missing-dependency" in markdown


def test_library_failure_never_breaks_the_brief(tmp_path: Path, _isolated_library) -> None:
    root = _project(tmp_path)
    content, _ = export_prompt(root, "harden token verification", as_json=True,
                               assets=["../../etc/passwd"])
    pack = json.loads(content)
    assert pack["relevant_code"]  # the brief still assembled
    assert any(w["kind"] == "invalid-ref" for w in pack["library"]["warnings"])
