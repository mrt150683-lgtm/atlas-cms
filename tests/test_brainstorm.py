"""Brainstorm engine: novelty avoid-list, preference learning, goals,
temperature passthrough, and provider honesty."""

import json
from pathlib import Path

import pytest

import cms.brainstorm as bmod
import cms.fuse as fuse
import cms.scout as scout_mod
from cms.brainstorm import (BrainstormError, add_goal, generate_ideas,
                            load_goals, load_ideas, rate_idea, remove_goal)
from cms.providers import MockProvider

TEN = json.dumps([f"Idea number {i}: a fresh concept about topic {i}." for i in range(10)])


class IdeaProvider:
    name = "idea-test"
    model = "m1"

    def __init__(self, reply=TEN):
        self.reply = reply
        self.prompts: list[str] = []
        self.contexts: list[dict] = []

    def summarize(self, prompt, context):
        self.prompts.append(prompt)
        self.contexts.append(context)
        if isinstance(self.reply, Exception):
            raise self.reply
        return self.reply


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(bmod, "BRAINSTORM_DIR", tmp_path / "bs")
    monkeypatch.setattr(fuse, "REGISTRY_PATH", tmp_path / "reg" / "projects.json")
    monkeypatch.setattr(fuse, "FUSION_DIR", tmp_path / "reg" / "fusion")
    monkeypatch.setattr(scout_mod, "SCOUT_DIR", tmp_path / "scoutdir")


def test_generate_persists_ten_and_passes_temperature(tmp_path):
    p = IdeaProvider()
    new = generate_ideas(p, temperature=0.7)
    assert len(new) == 10 and all(i["status"] == "new" for i in new)
    assert p.contexts[0]["temperature"] == 0.7
    stored = load_ideas()
    assert len(stored) == 10
    assert all(i["provenance"] == "llm" and i["temperature"] == 0.7
               for i in stored.values())
    # unconstrained mode states the novelty contract in the prompt
    assert "unrelated to the" in p.prompts[0]


def test_preference_learning_feeds_next_batch(tmp_path):
    p = IdeaProvider()
    new = generate_ideas(p)
    liked, disliked = new[0], new[1]
    rate_idea(liked["id"], "liked")
    rate_idea(disliked["id"], "disliked")

    p2 = IdeaProvider(json.dumps([f"Second wave concept {i}." for i in range(10)]))
    generate_ideas(p2)
    prompt = p2.prompts[0]
    liked_sec = prompt.split("LIKED these earlier ideas")[1].split("DISLIKED")[0]
    disliked_sec = prompt.split("DISLIKED these")[1].split("DO NOT produce")[0]
    assert liked["text"] in liked_sec
    assert disliked["text"] in disliked_sec
    # past ideas are in the avoid list so batches never repeat
    assert liked["text"] in prompt.split("DO NOT produce")[1]

    with pytest.raises(BrainstormError, match="verdict must be"):
        rate_idea(liked["id"], "meh")
    with pytest.raises(BrainstormError, match="unknown idea"):
        rate_idea("nope", "liked")


def test_avoid_list_covers_existing_work(tmp_path):
    # a scout card + suggestion exist -> unconstrained generation must avoid them
    scout_mod._save("plans.json", {"k": {
        "name": "plan.md", "project_dir": "proj", "status": "ok",
        "one_liner": "A very distinctive existing plan sentence.", "content_hash": "x"}})
    scout_mod._save("suggestions.json", {"s1": {
        "id": "s1", "kind": "concepts", "title": "Distinctive Existing Suggestion",
        "description": "", "builds_on": [], "status": "proposed"}})
    p = IdeaProvider()
    generate_ideas(p)
    avoid = p.prompts[0].split("DO NOT produce")[1]
    assert "A very distinctive existing plan sentence." in avoid
    assert "Distinctive Existing Suggestion" in avoid


def test_goals_inject_into_every_batch(tmp_path):
    add_goal("ideas that could help cure diseases")
    goals = add_goal("make me £500/week")
    assert len(goals) == 2
    p = IdeaProvider()
    generate_ideas(p)
    assert "STANDING GOALS" in p.prompts[0]
    assert "cure diseases" in p.prompts[0] and "£500/week" in p.prompts[0]

    remaining = remove_goal(goals[0]["id"])
    assert len(remaining) == 1 and load_goals()[0]["text"] == "make me £500/week"
    with pytest.raises(BrainstormError, match="empty"):
        add_goal("   ")


def test_project_grounded_mode_uses_card(tmp_path):
    proj = tmp_path / "alpha"
    proj.mkdir()
    (proj / "app.py").write_text("def go():\n    pass\n", encoding="utf-8")

    class DiscProv(IdeaProvider):
        def summarize(self, prompt, context):
            if "named FEATURES" in prompt:
                return '[{"name": "Transcription", "description": "d", "files": ["app.py"]}]'
            if "FEATURE TRACE" in prompt:
                return "## Purpose\nx\n## Flow\nx\n## Verification Checklist\n- x"
            if "concept generator" in prompt:
                return super().summarize(prompt, context)
            return "Summary."

    from cms.update import incremental_update
    incremental_update(proj, DiscProv(), echo=lambda *a: None)

    p = IdeaProvider()
    generate_ideas(p, project_root=str(proj))
    assert 'grounded in the project "alpha"' in p.prompts[0]
    assert "Transcription" in p.prompts[0]

    # an unmapped project is refused with the reason
    bare = tmp_path / "bare"
    bare.mkdir()
    with pytest.raises(BrainstormError, match="isn't fused-ready"):
        generate_ideas(IdeaProvider(), project_root=str(bare))


def test_honesty_guards(tmp_path):
    with pytest.raises(BrainstormError, match="real provider"):
        generate_ideas(MockProvider())
    with pytest.raises(BrainstormError, match="no JSON array"):
        generate_ideas(IdeaProvider("prose only"))
    with pytest.raises(BrainstormError, match="provider call failed"):
        generate_ideas(IdeaProvider(RuntimeError("down")))
    assert load_ideas() == {}  # failures never wrote state
