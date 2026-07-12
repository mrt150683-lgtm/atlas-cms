"""Constellation (cms fuse): registry, evidence-gated cards, structural
overlaps, fusion synthesis honesty (real-provider-only, no fake success)."""

import json
from pathlib import Path

import pytest

import cms.fuse as fuse
import cms.semantic_state as ss
from cms.fuse import (FusionError, build_card, build_fusion, load_registry,
                      register_project, structural_overlaps)
from cms.providers import MockProvider
from cms.update import incremental_update

GOOD_FUSION = json.dumps({
    "integrations": [{"title": "Feed transcripts into notes",
                      "projects": ["alpha", "beta"],
                      "features": ["Transcription", "NoteVault"],
                      "description": "pipe STT output into the vault",
                      "first_step": "share the transcript schema"}],
    "emergent": [{"title": "Searchable voice diary", "projects": ["alpha", "beta"],
                  "description": "combined capability"}],
    "conflicts": [{"title": "Two storage layers", "projects": ["alpha", "beta"],
                   "features": ["NoteVault", "Transcription"],
                   "description": "both persist independently",
                   "resolution_hint": "one canonical store"}],
})


class FusionProvider:
    name = "fusion-test"
    model = "test-model"

    def __init__(self, fusion=GOOD_FUSION, discovery='[]', refined=None):
        self.fusion = fusion
        self.discovery = discovery
        self.refined = refined if refined is not None else fusion
        self.fusion_calls = 0

    def summarize(self, prompt, context):
        if "refining an existing cross-project fusion report" in prompt:
            self.fusion_calls += 1
            if isinstance(self.refined, Exception):
                raise self.refined
            return self.refined
        if "principal architect" in prompt:
            self.fusion_calls += 1
            if isinstance(self.fusion, Exception):
                raise self.fusion
            return self.fusion
        if "named FEATURES" in prompt:
            return self.discovery
        if "FEATURE TRACE" in prompt:
            return "## Purpose\nx\n## Flow\nx\n## Verification Checklist\n- x"
        return "Summary."


@pytest.fixture(autouse=True)
def _isolated_registry(tmp_path, monkeypatch):
    monkeypatch.setattr(fuse, "REGISTRY_PATH", tmp_path / "reg" / "projects.json")
    monkeypatch.setattr(fuse, "FUSION_DIR", tmp_path / "reg" / "fusion")


def _mapped_project(tmp_path: Path, name: str, feature: str) -> Path:
    root = tmp_path / name
    root.mkdir()
    (root / "app.py").write_text("def go():\n    pass\n", encoding="utf-8")
    disc = json.dumps([{"name": feature, "description": f"{feature} things",
                        "files": ["app.py"]}])
    incremental_update(root, FusionProvider(discovery=disc), echo=lambda *a: None)
    return root


def test_registry_records_built_projects(tmp_path):
    root = _mapped_project(tmp_path, "alpha", "Transcription")
    reg = load_registry()
    assert str(root) in reg["projects"]
    assert reg["projects"][str(root)]["name"] == "alpha"


def test_card_requires_positive_discovery_evidence(tmp_path):
    root = _mapped_project(tmp_path, "alpha", "Transcription")
    card = build_card(root)
    assert card["ready"] and [f["name"] for f in card["features"]] == ["Transcription"]
    assert card["feature_set_hash"]

    # strip the evidence file -> card must refuse, with the reason
    ss.state_path(root / ".memory").unlink()
    card = build_card(root)
    assert card["ready"] is False and "not positively recorded" in card["reason"]

    bare = tmp_path / "bare"
    bare.mkdir()
    assert build_card(bare)["ready"] is False


def test_structural_overlaps_detect_shared_domains(tmp_path):
    a = build_card(_mapped_project(tmp_path, "alpha", "VoiceTranscription"))
    b = build_card(_mapped_project(tmp_path, "beta", "TranscriptionPipeline"))
    c = build_card(_mapped_project(tmp_path, "gamma", "PhotoAlbum"))
    overlaps = structural_overlaps([a, b, c])
    kinds = {(o["kind"], tuple(o["projects"])) for o in overlaps}
    assert ("related-feature-domain", ("alpha", "beta")) in kinds
    assert not any("gamma" in o["projects"] and o["kind"] == "related-feature-domain"
                   for o in overlaps)
    assert all(o["provenance"] == "structural" for o in overlaps)


def test_fusion_report_built_and_persisted(tmp_path):
    roots = [_mapped_project(tmp_path, "alpha", "Transcription"),
             _mapped_project(tmp_path, "beta", "NoteVault")]
    p = FusionProvider()
    report = build_fusion(roots, p)
    assert p.fusion_calls == 1
    assert set(report["projects"]) == {"alpha", "beta"}
    assert report["integrations"][0]["provenance"] == "llm"
    assert report["conflicts"][0]["resolution_hint"]
    for name, info in report["projects"].items():
        assert info["feature_set_hash"]
    assert (fuse.FUSION_DIR / "latest.json").is_file()
    md = (fuse.FUSION_DIR / "latest.md").read_text(encoding="utf-8")
    assert "plan material, not ground truth" in md
    assert fuse.fusion_staleness(report) == []


def test_fusion_refuses_mock_and_underevidence(tmp_path):
    roots = [_mapped_project(tmp_path, "alpha", "Transcription"),
             _mapped_project(tmp_path, "beta", "NoteVault")]
    with pytest.raises(FusionError, match="real provider"):
        build_fusion(roots, MockProvider())

    # only one evidenced project -> refuse, naming the excluded one
    ss.state_path(roots[1] / ".memory").unlink()
    with pytest.raises(FusionError, match="beta"):
        build_fusion(roots, FusionProvider())


def test_fusion_failure_never_fakes_success(tmp_path):
    roots = [_mapped_project(tmp_path, "alpha", "Transcription"),
             _mapped_project(tmp_path, "beta", "NoteVault")]
    with pytest.raises(FusionError, match="no JSON object"):
        build_fusion(roots, FusionProvider(fusion="prose, no json"))
    with pytest.raises(FusionError, match="provider call failed"):
        build_fusion(roots, FusionProvider(fusion=RuntimeError("api down")))
    assert not (fuse.FUSION_DIR / "latest.json").exists()


def test_fusion_staleness_flags_drifted_member(tmp_path):
    roots = [_mapped_project(tmp_path, "alpha", "Transcription"),
             _mapped_project(tmp_path, "beta", "NoteVault")]
    report = build_fusion(roots, FusionProvider())
    # beta's feature set changes -> report must read as stale for beta
    (roots[1] / "extra.py").write_text(
        "# @memory:feature:Anchored\ndef f():\n    pass\n", encoding="utf-8")
    incremental_update(roots[1], FusionProvider(discovery="[]"), echo=lambda *a: None)
    assert fuse.fusion_staleness(report) == ["beta"]


REFINED_FUSION = json.dumps({
    "integrations": [{"title": "Deep transcript-memory pipeline",
                      "projects": ["alpha", "beta"],
                      "features": ["Transcription", "NoteVault"],
                      "description": "focused per direction",
                      "first_step": "define the ingest contract"}],
    "emergent": [], "conflicts": [],
})


def test_refine_updates_report_and_history(tmp_path):
    roots = [_mapped_project(tmp_path, "alpha", "Transcription"),
             _mapped_project(tmp_path, "beta", "NoteVault")]
    build_fusion(roots, FusionProvider())

    report = fuse.refine_fusion("focus on the transcript pipeline, drop conflicts",
                                FusionProvider(refined=REFINED_FUSION))
    assert report["integrations"][0]["title"] == "Deep transcript-memory pipeline"
    assert report["conflicts"] == []
    assert report["direction"].startswith("focus on the transcript")
    assert report["refined_from"]  # links back to the report it revised

    latest = fuse.load_fusion()
    assert latest["integrations"][0]["title"] == "Deep transcript-memory pipeline"
    hist = fuse.fusion_history()
    assert len(hist) == 1 and hist[0]["direction"].startswith("focus on")
    md = (fuse.FUSION_DIR / "latest.md").read_text(encoding="utf-8")
    assert "Deep transcript-memory pipeline" in md


def test_refine_guards_and_last_known_good(tmp_path):
    # no report yet
    with pytest.raises(FusionError, match="no fusion report"):
        fuse.refine_fusion("anything", FusionProvider())

    roots = [_mapped_project(tmp_path, "alpha", "Transcription"),
             _mapped_project(tmp_path, "beta", "NoteVault")]
    original = build_fusion(roots, FusionProvider())

    with pytest.raises(FusionError, match="real provider"):
        fuse.refine_fusion("x", MockProvider())
    with pytest.raises(FusionError, match="direction"):
        fuse.refine_fusion("   ", FusionProvider())
    # malformed refinement output must NOT clobber the last good report
    with pytest.raises(FusionError, match="no JSON object"):
        fuse.refine_fusion("go", FusionProvider(refined="prose only"))
    assert fuse.load_fusion()["generated_at"] == original["generated_at"]
    assert fuse.fusion_history() == []
