"""Decision lock: propose/approve lifecycle, intent immutability, versioned
supersession with audit history, human-only approval, and alignment/chat
consumption of the approved word."""

from pathlib import Path

import pytest

from cms.decisions import ACTIVE_STATUSES, STATUSES, DecisionStore


@pytest.fixture()
def store(tmp_path):
    memory = tmp_path / ".memory"
    memory.mkdir()
    return DecisionStore(memory, root=tmp_path)


INTENT = {"behaviour": "get_source must reject paths outside the project root",
          "prohibited": ["serving absolute paths"], "constraints": ["no symlink escape"]}


def test_propose_approve_lifecycle(store) -> None:
    d = store.propose("AgentMemoryAccess", "Guard get_source", INTENT,
                      created_by={"kind": "model", "identity": "claude"})
    assert d["status"] == "proposed" and d["version"] == 1
    approved = store.approve(d["id"], "alex")
    assert approved["status"] == "approved"
    assert approved["approved_by"] == "alex" and approved["approved_at"]
    assert store.approved_for("AgentMemoryAccess")["id"] == d["id"]


def test_approval_requires_identity_and_proposed_state(store) -> None:
    d = store.propose("F", "t", INTENT)
    with pytest.raises(ValueError):
        store.approve(d["id"], "")
    store.approve(d["id"], "alex")
    with pytest.raises(ValueError):
        store.approve(d["id"], "alex")  # already approved
    with pytest.raises(ValueError):
        store.approve("dec-nope", "alex")


def test_intent_is_immutable_after_approval(store) -> None:
    d = store.propose("F", "t", INTENT)
    store.approve(d["id"], "alex")
    with pytest.raises(ValueError):
        store.update_intent_guard(d["id"], {"behaviour": "rewritten"})
    # and the stored payload is untouched
    assert store.get(d["id"])["intent"]["behaviour"] == INTENT["behaviour"]


def test_supersession_versions_and_preserves_history(store) -> None:
    v1 = store.propose("F", "first word", INTENT)
    store.approve(v1["id"], "alex")
    v2 = store.propose("F", "better word",
                       {"behaviour": "also validate symlinks"}, supersedes=v1["id"])
    assert v2["version"] == 2 and v2["supersedes"] == v1["id"]
    # supersession lands at APPROVAL of the successor, not proposal
    assert store.get(v1["id"])["status"] == "approved"
    store.approve(v2["id"], "alex")
    assert store.get(v1["id"])["status"] == "superseded"   # auditable, not deleted
    assert store.approved_for("F")["id"] == v2["id"]
    with pytest.raises(ValueError):
        store.propose("F", "x", INTENT, supersedes="dec-ghost")


def test_no_intent_shadowing_without_supersession(store) -> None:
    """A feature has ONE operative approved intent: approving a second,
    unlinked decision must be refused, not silently shadow the first."""
    v1 = store.propose("F", "first word", INTENT)
    store.approve(v1["id"], "alex")
    rogue = store.propose("F", "competing word", {"behaviour": "something else"})
    with pytest.raises(ValueError, match="supersedes"):
        store.approve(rogue["id"], "alex")
    assert store.approved_for("F")["id"] == v1["id"]  # unchanged
    # the honest path still works: supersede explicitly
    v2 = store.propose("F", "better word", {"behaviour": "improved"},
                       supersedes=v1["id"])
    store.approve(v2["id"], "alex")
    assert store.approved_for("F")["id"] == v2["id"]
    assert store.get(v1["id"])["status"] == "superseded"


def test_closure_outcomes(store) -> None:
    d = store.propose("F", "t", INTENT)
    with pytest.raises(ValueError):
        store.close(d["id"], "implemented")     # not approved yet
    store.approve(d["id"], "alex")
    closed = store.close(d["id"], "partially_implemented", reason="error path missing")
    assert closed["status"] == "partially_implemented"
    assert closed["closure_reason"] == "error path missing"

    d2 = store.propose("F", "t2", INTENT)
    assert store.close(d2["id"], "rejected")["status"] == "rejected"
    with pytest.raises(ValueError):
        store.close(d2["id"], "vanished")
    assert set(ACTIVE_STATUSES) == {"proposed", "approved"}
    assert "superseded" in STATUSES


def test_alignment_reports_approved_intent(tmp_path, monkeypatch) -> None:
    """build_alignment surfaces the locked word for touched features."""
    from cms import align as align_mod
    from cms.align import build_alignment
    from cms.features import build_features
    from cms.graph_builder import build_graph
    from cms.memory import CodebaseMemory
    from cms.providers import MockProvider
    from cms.scanner import scan

    (tmp_path / "app.py").write_text(
        "# @memory:feature:Greeting\ndef greet(n):\n    return n\n", encoding="utf-8")
    graph = build_graph(scan(tmp_path))
    build_features(graph, MockProvider())
    memory = tmp_path / ".memory"
    memory.mkdir()
    mem = CodebaseMemory(graph)

    store = DecisionStore(memory, root=tmp_path)
    d = store.propose("Greeting", "Greet politely",
                      {"behaviour": "greet returns the name unchanged"})
    store.approve(d["id"], "alex")

    monkeypatch.setattr(align_mod, "git_changed_files", lambda root, base="HEAD": ["app.py"])
    rec = build_alignment(mem, tmp_path, {"task": "polish greeting",
                                          "relevant_code": [{"path": "app.py"}]})
    assert rec["approved_intent"] == [{
        "feature": "Greeting", "decision_id": d["id"], "title": "Greet politely",
        "behaviour": "greet returns the name unchanged", "prohibited": [],
        "approved_at": store.get(d["id"])["approved_at"],
    }]
