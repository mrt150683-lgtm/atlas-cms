from __future__ import annotations

import json
from pathlib import Path

import pytest

from cms.ideas import IdeaError, IdeaJournal, migrate_legacy_brainstorm


class IdeaProvider:
    name = "test"
    model = "idea-test-model"

    def __init__(self, rows=None, error: Exception | None = None):
        self.rows = rows or [{
            "title": "Memory paths as executable sketches",
            "kind": "tool",
            "overview": "Turn a drawn path through project features into a reproducible tool proposal.",
            "rationale": "It makes cross-project discovery tangible.",
            "contributions": ["Journal supplies history", "Atlas supplies capabilities"],
            "missing_capability": "A path interpreter",
            "risks": ["Visual overload"],
            "first_experiment": "Connect two idea nodes",
        }]
        self.error = error
        self.calls = []

    def summarize(self, prompt, context):
        self.calls.append((prompt, context))
        if self.error:
            raise self.error
        return json.dumps(self.rows)


def test_canonical_ideas_are_human_owned_and_hierarchical(tmp_path: Path) -> None:
    journal = IdeaJournal(tmp_path / "ideas")
    root = journal.create_idea("Atlas Idea Journal", overview="A durable thought map.")
    child = journal.create_idea("Join the Dots", kind="feature", parent_id=root["id"])

    assert journal.get_idea(root["id"])["children"][0]["id"] == child["id"]
    with pytest.raises(IdeaError, match="models create candidates"):
        journal.create_idea("Silent model edit", actor_kind="model")
    with pytest.raises(IdeaError, match="cycle"):
        journal.update_idea(root["id"], parent_id=child["id"])

    updated = journal.update_idea(child["id"], status="promising", overview="Draw through nodes.")
    assert updated["status"] == "promising"
    assert journal.search("Draw nodes")[0]["id"] == child["id"]
    assert journal.update_idea(child["id"], parent_id=None)["parent_id"] is None


def test_sources_and_relationships_are_deduplicated(tmp_path: Path) -> None:
    journal = IdeaJournal(tmp_path / "ideas")
    a = journal.create_idea("First idea")
    b = journal.create_idea("Second idea")
    first = journal.add_source("raw brainstorming transcript", idea_id=a["id"])
    again = journal.add_source("raw brainstorming transcript", idea_id=a["id"])
    assert first["id"] == again["id"]

    rel = journal.add_relationship(a["id"], "idea", b["id"], "builds_on")
    duplicate = journal.add_relationship(a["id"], "idea", b["id"], "builds_on")
    assert rel["id"] == duplicate["id"]
    assert journal.get_idea(a["id"])["relationships"][0]["target_ref"] == b["id"]

    missing = journal.add_relationship(a["id"], "project", str(tmp_path / "gone"))
    refreshed = next(r for r in journal.get_idea(a["id"])["relationships"]
                     if r["id"] == missing["id"])
    assert refreshed["target_present"] is False
    assert refreshed["stale"] is True


def test_generation_is_structured_and_failure_is_transactional(tmp_path: Path) -> None:
    journal = IdeaJournal(tmp_path / "ideas")
    journal.create_idea("Existing memory graph", overview="Do not repeat this.")
    provider = IdeaProvider()
    result = journal.generate(provider, mode="journal", direction="new agent tools", seed=42)

    assert result["seed"] == 42
    assert result["candidates"][0]["status"] == "new"
    assert result["candidates"][0]["generation_id"] == result["generation_id"]
    assert "Existing memory graph" in provider.calls[0][0]
    assert journal.search("executable sketches") == []  # candidates are not canonical ideas

    before = len(journal.list_candidates())
    with pytest.raises(IdeaError, match="provider call failed"):
        journal.generate(IdeaProvider(error=RuntimeError("offline")), direction="fail")
    assert len(journal.list_candidates()) == before


def test_candidate_acceptance_is_an_explicit_human_transition(tmp_path: Path) -> None:
    journal = IdeaJournal(tmp_path / "ideas")
    candidate = journal.propose_candidate("Candidate", "Model proposal", actor_kind="model")
    assert journal.search("Candidate") == []

    accepted = journal.decide_candidate(candidate["id"], "accepted")
    idea = journal.get_idea(accepted["accepted_idea_id"])
    assert accepted["status"] == "accepted"
    assert idea["title"] == "Candidate"
    assert idea["origin"] == "agent"


def test_join_the_dots_preserves_order_seed_and_path(tmp_path: Path) -> None:
    journal = IdeaJournal(tmp_path / "ideas")
    a = journal.create_idea("Transcription pipeline")
    b = journal.create_idea("Profile memory")
    nodes = [f"idea:{a['id']}", f"idea:{b['id']}"]

    result = journal.join_dots(IdeaProvider(), nodes, surprise=0.2, seed=77,
                               points=[[1, 2], [3, 4]])
    assert result["seed"] == 77
    assert result["path"] == "Transcription pipeline -> Profile memory"
    assert [n["id"] for n in result["selected_nodes"]] == nodes
    assert "exact order" in result["context"]["direction"]


def test_legacy_brainstorm_imports_as_candidates_only_once(tmp_path: Path) -> None:
    legacy = tmp_path / "brainstorm"
    legacy.mkdir()
    (legacy / "ideas.json").write_text(json.dumps({
        "old": {"id": "old", "text": "Old generated concept", "status": "disliked"}
    }), encoding="utf-8")
    journal = IdeaJournal(tmp_path / "ideas")

    first = migrate_legacy_brainstorm(journal, legacy)
    second = migrate_legacy_brainstorm(journal, legacy)
    assert first == {"imported": 1, "skipped": False}
    assert second == {"imported": 0, "skipped": True}
    assert journal.list_candidates()[0]["status"] == "rejected"
    assert journal.search("Old generated") == []
