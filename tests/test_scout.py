"""Scout: plan hunting, content-hash card caching, mass review with
never-re-propose semantics, and real-provider honesty."""

import json
from pathlib import Path

import pytest

import cms.fuse as fuse
import cms.scout as scout
from cms.providers import MockProvider
from cms.scout import (ScoutError, find_plans, load_cards, load_suggestions,
                       mass_review, scan_plans, set_suggestion_status)

GOOD_CARD = json.dumps({"one_liner": "A local-first STT pipeline plan.",
                        "tags": ["stt", "audio"], "goals": ["transcribe locally"],
                        "atlas_candidate": True, "reason": "real project"})
GOOD_REVIEW = json.dumps({
    "concepts": [{"title": "Voice-first memory capture", "description": "d",
                  "builds_on": ["plan.md"]}],
    "patterns": [{"title": "Everything is local-first", "description": "d",
                  "builds_on": ["plan.md"]}],
    "pairings": [], "atlas_candidates": [],
})


class ScoutProvider:
    name = "scout-test"
    model = "m1"

    def __init__(self, card=GOOD_CARD, review=GOOD_REVIEW):
        self.card, self.review = card, review
        self.card_calls = 0
        self.review_calls = 0

    def summarize(self, prompt, context):
        if "cataloguing one project-plan document" in prompt:
            self.card_calls += 1
            if isinstance(self.card, Exception):
                raise self.card
            return self.card
        if "scattered across their machine" in prompt:
            self.review_calls += 1
            if isinstance(self.review, Exception):
                raise self.review
            return self.review
        return "Summary."


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(scout, "SCOUT_DIR", tmp_path / "scoutdir")
    monkeypatch.setattr(fuse, "REGISTRY_PATH", tmp_path / "reg" / "projects.json")


def _tree(tmp_path: Path) -> Path:
    base = tmp_path / "desk"
    (base / "proj_a").mkdir(parents=True)
    (base / "proj_a" / "plan.md").write_text("# build STT", encoding="utf-8")
    (base / "proj_b").mkdir()
    (base / "proj_b" / "Master_Plan_v2.md").write_text("# vault", encoding="utf-8")
    (base / "proj_b" / "notes.md").write_text("not a plan", encoding="utf-8")
    junk = base / "proj_a" / "node_modules" / "pkg"
    junk.mkdir(parents=True)
    (junk / "plan.md").write_text("vendored", encoding="utf-8")
    return base


def test_find_plans_matches_and_prunes(tmp_path):
    base = _tree(tmp_path)
    names = [p.name for p in find_plans(base)]
    assert names == ["plan.md", "Master_Plan_v2.md"]  # path-sorted; node_modules pruned


def test_scan_cards_cache_by_content_hash(tmp_path):
    base = _tree(tmp_path)
    p = ScoutProvider()
    stats = scan_plans(base, p, echo=lambda *a: None)
    assert stats == {"found": 2, "new": 2, "unchanged": 0, "failed": 0}
    assert p.card_calls == 2
    card = next(c for c in load_cards().values() if c["name"] == "plan.md")
    assert card["one_liner"].startswith("A local-first") and card["atlas_candidate"]
    assert card["provider"] == "scout-test"

    # unchanged rescan: zero cost
    p2 = ScoutProvider()
    stats = scan_plans(base, p2, echo=lambda *a: None)
    assert stats["unchanged"] == 2 and p2.card_calls == 0

    # edit one file -> exactly one re-card
    (base / "proj_a" / "plan.md").write_text("# build STT v2", encoding="utf-8")
    p3 = ScoutProvider()
    stats = scan_plans(base, p3, echo=lambda *a: None)
    assert stats["new"] == 1 and p3.card_calls == 1


def test_scan_honesty(tmp_path):
    base = _tree(tmp_path)
    with pytest.raises(ScoutError, match="real provider"):
        scan_plans(base, MockProvider(), echo=lambda *a: None)

    # per-file failure recorded, not faked; retried on next scan
    p = ScoutProvider(card="no json here")
    stats = scan_plans(base, p, echo=lambda *a: None)
    assert stats["failed"] == 2
    assert all(c["status"] == "failed" for c in load_cards().values())
    p2 = ScoutProvider()
    stats = scan_plans(base, p2, echo=lambda *a: None)
    assert stats["new"] == 2  # failed cards re-attempted


def test_mass_review_and_never_repropose(tmp_path):
    base = _tree(tmp_path)
    scan_plans(base, ScoutProvider(), echo=lambda *a: None)

    result = mass_review(ScoutProvider())
    assert result["cards_reviewed"] == 2 and result["new_suggestions"] == 2
    ideas = load_suggestions()
    concept = next(s for s in ideas.values() if s["kind"] == "concepts")
    assert concept["status"] == "proposed" and concept["provenance"] == "llm"

    # reject it -> excluded from the next prompt AND status survives resurfacing
    set_suggestion_status(concept["id"], "rejected")
    p2 = ScoutProvider()  # model re-proposes the same ideas
    result2 = mass_review(p2)
    assert result2["dismissed_excluded"] == 1
    assert result2["new_suggestions"] == 0  # nothing new, nothing resurrected
    assert load_suggestions()[concept["id"]]["status"] == "rejected"
    # and the dismissed title was passed to the prompt as a do-not-repropose
    assert "Voice-first memory capture" not in json.dumps(result2["suggestions"])


def test_review_guards(tmp_path):
    with pytest.raises(ScoutError, match="no plan cards"):
        mass_review(ScoutProvider())
    base = _tree(tmp_path)
    scan_plans(base, ScoutProvider(), echo=lambda *a: None)
    with pytest.raises(ScoutError, match="real provider"):
        mass_review(MockProvider())
    before = load_suggestions()
    with pytest.raises(ScoutError, match="no JSON object"):
        mass_review(ScoutProvider(review="prose"))
    assert load_suggestions() == before  # malformed output changed nothing
    with pytest.raises(ScoutError, match="status must be"):
        set_suggestion_status("whatever", "meh")
