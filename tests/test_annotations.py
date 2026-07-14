"""Structured annotations: canonical targets, lifecycle, supersession audit,
model-author immutability, legacy note merging, and context-pack scoping."""

import pytest

from cms.annotations import (
    ACTIVE_STATUSES,
    STATUSES,
    TYPES,
    AnnotationStore,
    normalize_target,
)
from cms.notes import NotesStore


@pytest.fixture()
def store(tmp_path):
    memory = tmp_path / ".memory"
    memory.mkdir()
    return AnnotationStore(memory, root=tmp_path)


def test_normalize_target_forms() -> None:
    assert normalize_target("feature:Search") == ("feature:Search", "node")
    assert normalize_target("func:cms/ui.py::serve") == ("func:cms/ui.py::serve", "node")
    assert normalize_target({"edge": ["feature:A", "feature:B"]}) == (
        "edge:feature:A|feature:B", "edge")
    assert normalize_target({"path": "cms/ui.py", "start": 10, "end": 20}) == (
        "range:cms/ui.py#10-20", "source_range")
    with pytest.raises(ValueError):
        normalize_target({"nope": 1})
    with pytest.raises(ValueError):
        normalize_target("   ")


def test_add_and_list_roundtrip(store) -> None:
    a = store.add("feature:Search", "bug_suspicion", "ranking ignores aliases",
                  feature="Search", confidence=0.7,
                  author={"kind": "model", "identity": "claude", "model": "haiku"})
    assert a["status"] == "open" and a["type"] == "bug_suspicion"
    assert a["confidence"] == 0.7 and a["author"]["kind"] == "model"
    rows = store.list(target="feature:Search")
    assert [r["id"] for r in rows] == [a["id"]]
    assert store.counts() == {"feature:Search": 1}


def test_unknown_type_and_author_kind_degrade_to_safe_defaults(store) -> None:
    a = store.add("file:cms/ui.py", "wild-type", "text", author={"kind": "alien"})
    assert a["type"] == "note" and a["author"]["kind"] == "user"


def test_lifecycle_transitions_stamp_timestamps(store) -> None:
    a = store.add("file:cms/ui.py", "question", "why threaded?")
    r = store.set_status(a["id"], "resolved", reason="answered in docs")
    assert r["resolved_at"] and r["payload"]["status_reason"] == "answered in docs"
    r = store.set_status(a["id"], "archived")
    assert r["archived_at"]
    assert store.list() == []  # archived out of default listing
    assert store.list(include_archived=True)[0]["id"] == a["id"]
    with pytest.raises(ValueError):
        store.set_status(a["id"], "vanished")


def test_supersession_keeps_audit_history(store) -> None:
    old = store.add("feature:Search", "observation", "v1 claim",
                    author={"kind": "model", "identity": "claude"})
    new = store.add("feature:Search", "observation", "v2 corrected claim",
                    author={"kind": "model", "identity": "claude"},
                    supersedes=old["id"])
    assert new["supersedes"] == old["id"]
    assert store.get(old["id"])["status"] == "superseded"  # still on disk
    listed = store.list(target="feature:Search")
    assert [r["id"] for r in listed] == [new["id"]]  # superseded hidden by default
    with pytest.raises(ValueError):
        store.add("feature:Search", "observation", "x", supersedes="ann-nope")


def test_model_bodies_are_immutable_user_bodies_editable(store) -> None:
    model = store.add("file:a.py", "observation", "model text",
                      author={"kind": "model", "identity": "claude"})
    user = store.add("file:a.py", "note", "user text")
    with pytest.raises(ValueError):
        store.edit_body(model["id"], "rewritten")
    assert store.edit_body(user["id"], "edited")["body"] == "edited"


def test_legacy_viewer_notes_merge_read_only(store, tmp_path) -> None:
    NotesStore(tmp_path / ".memory").add("cms/ui.py", "quoted text", "my note")
    rows = store.list(target="file:cms/ui.py")
    assert len(rows) == 1 and rows[0]["legacy"] is True
    assert rows[0]["payload"]["quote"] == "quoted text"
    # legacy notes never enter model context packs
    assert store.active_for_context(targets=["file:cms/ui.py"]) == []


def test_active_for_context_scopes_and_trims(store) -> None:
    store.add("feature:Search", "contradiction", "docs say X, code does Y",
              feature="Search", priority="high")
    a2 = store.add("feature:Search", "question", "z" * 1000, feature="Search")
    store.set_status(a2["id"], "resolved")
    store.add("feature:Other", "note", "unrelated", feature="Other")
    ctx = store.active_for_context(feature="Search")
    assert len(ctx) == 1  # resolved + unrelated excluded
    assert ctx[0]["type"] == "contradiction" and len(ctx[0]["body"]) <= 400
    assert set(ACTIVE_STATUSES) == {"open", "under_review", "accepted"}
    assert "superseded" in STATUSES and "verification_result" in TYPES
