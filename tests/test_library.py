"""Atlas Library: store lifecycle, precedence, composition, import/export."""

from types import SimpleNamespace

import pytest

from cms import config
from cms.library import (
    LibraryError,
    LibraryStore,
    LibraryView,
    canonical_text,
    compose_context,
    edit_published_guard,
    export_asset,
    import_asset,
    import_skill_directory,
    new_asset_template,
    parse_asset_text,
    parse_ref,
    render_assets,
    serialize_asset,
    validate_meta,
)


def _asset(asset_id, type="skill", name=None, description="What it does.",
           tags=None, requires=None, conflicts=None, assets=None, body="Body text."):
    lines = ["---", f"id: {asset_id}", f"name: {name or asset_id.title()}",
             f"type: {type}", f"description: {description}"]
    if tags:
        lines.append(f"tags: [{', '.join(tags)}]")
    if requires:
        lines.append(f"requires: [{', '.join(requires)}]")
    if conflicts:
        lines.append(f"conflicts_with: [{', '.join(conflicts)}]")
    if assets:
        lines.append(f"assets: [{', '.join(assets)}]")
    lines += ["---", "", body]
    return "\n".join(lines)


@pytest.fixture
def env(tmp_path, monkeypatch):
    builtin = tmp_path / "builtin"
    builtin.mkdir()
    user = tmp_path / "userlib"
    project = tmp_path / "proj"
    project.mkdir()
    monkeypatch.setenv("CMS_LIBRARY_BUILTIN", str(builtin))
    monkeypatch.setattr(config, "LIBRARY_USER_DIR", user)
    return SimpleNamespace(builtin=builtin, user=user, project=project)


def _publish(env, asset_id, text, scope="project", by="tester"):
    store = LibraryView(env.project).store(scope)
    store.save_draft(text)
    return store.publish(asset_id, by)


# --- frontmatter --------------------------------------------------------------

def test_frontmatter_roundtrip_and_list_syntaxes():
    text = ("---\n"
            "id:  my-skill \n"
            "name: 'My Skill'\n"
            "type: skill\n"
            "description: \"Does things.\"\n"
            "tags: a, b , c\n"
            "requires: [dep-one, dep-two@3]\n"
            "---\n\nThe body.\nSecond line.")
    meta, body = parse_asset_text(text)
    clean = validate_meta(meta)
    assert clean["id"] == "my-skill"
    assert clean["tags"] == ["a", "b", "c"]
    assert clean["requires"] == ["dep-one", "dep-two@3"]
    assert body == "The body.\nSecond line."
    canon = serialize_asset(clean, body)
    meta2, body2, canon2 = canonical_text(canon)
    assert (meta2, body2, canon2) == (clean, body, canon)  # stable fixed point


def test_invalid_assets_rejected():
    with pytest.raises(LibraryError):
        parse_asset_text("no frontmatter here")
    with pytest.raises(LibraryError):
        parse_asset_text("---\nid: x\n")  # fence never closed
    with pytest.raises(LibraryError):
        canonical_text(_asset("ok-id", type="sorcery"))
    with pytest.raises(LibraryError):
        canonical_text(_asset("Bad_ID"))
    with pytest.raises(LibraryError):
        canonical_text(_asset("no-desc", description=" "))
    # profiles must pin members; non-profiles cannot carry members
    with pytest.raises(LibraryError):
        canonical_text(_asset("prof-x", type="profile", assets=["unpinned-member"]))
    with pytest.raises(LibraryError):
        canonical_text(_asset("skill-x", assets=["other@1"]))


def test_parse_ref_and_traversal_guard(env):
    assert parse_ref("abc-def@12") == ("abc-def", 12)
    assert parse_ref("abc-def") == ("abc-def", None)
    with pytest.raises(LibraryError):
        parse_ref("../evil")
    store = LibraryStore(env.project / "skills", "project")
    with pytest.raises(LibraryError):
        store.asset_path("../../etc/passwd")


# --- lifecycle -----------------------------------------------------------------

def test_publish_freezes_and_versions(env):
    store = LibraryView(env.project).store("project")
    store.save_draft(_asset("my-skill"))
    rec = store.publish("my-skill", "alex")
    assert rec["current_version"] == 1 and rec["status"] == "published"
    snap1 = store.snapshot_path("my-skill", 1)
    assert snap1.is_file()
    v1_text = snap1.read_text(encoding="utf-8")

    # unchanged content refuses to publish
    with pytest.raises(LibraryError, match="frozen"):
        store.publish("my-skill", "alex")

    # editing the draft derives dirty, never mutates the snapshot
    store.save_draft(_asset("my-skill", body="New body."))
    assert store.get("my-skill")["dirty"] is True
    rec2 = store.publish("my-skill", "alex")
    assert rec2["current_version"] == 2
    assert snap1.read_text(encoding="utf-8") == v1_text  # v1 untouched

    # pinned old version still resolves the old content
    view = LibraryView(env.project)
    result = view.compose(["my-skill@1"])
    assert result["assets"][0]["version"] == 1
    assert "Body text." in result["assets"][0]["content"]
    latest = view.compose(["my-skill"])
    assert latest["assets"][0]["version"] == 2

    with pytest.raises(LibraryError, match="human identity"):
        store.publish("my-skill", "  ")
    with pytest.raises(LibraryError):
        edit_published_guard("my-skill")


def test_deprecated_pinned_vs_unpinned(env):
    store = LibraryView(env.project).store("project")
    store.save_draft(_asset("old-way"))
    store.publish("old-way", "alex")
    store.deprecate("old-way")
    unpinned = LibraryView(env.project).compose(["old-way"])
    assert not unpinned["assets"]
    assert any(w["kind"] == "deprecated-dependency" for w in unpinned["warnings"])
    pinned = LibraryView(env.project).compose(["old-way@1"])
    assert pinned["assets"][0]["id"] == "old-way"


def test_builtin_readonly_and_synthesized_v1(env):
    (env.builtin / "base-rule.md").write_text(
        _asset("base-rule", type="constraint"), encoding="utf-8")
    view = LibraryView(env.project)
    rec = view.effective("base-rule")[0]
    assert rec["status"] == "published" and rec["trust"] == "built-in"
    assert rec["current_version"] == 1
    result = view.compose(["base-rule"])
    assert result["assets"][0]["scope"] == "built-in"
    with pytest.raises(LibraryError, match="read-only"):
        view.store("built-in").save_draft(_asset("base-rule"))
    with pytest.raises(LibraryError, match="read-only"):
        view.store("built-in").publish("base-rule", "alex")


def test_unregistered_files_visible_and_registrable(env):
    lib = env.project / "skills"
    lib.mkdir()
    (lib / "hand-made.md").write_text(_asset("hand-made"), encoding="utf-8")
    rows = LibraryView(env.project).list()
    row = next(r for r in rows if r["id"] == "hand-made")
    assert row.get("registered") is False and row["status"] == "draft"
    store = LibraryView(env.project).store("project")
    store.register_file("hand-made")
    assert store.get("hand-made").get("registered") is not False


# --- dropping a plain skill file into the folder --------------------------------

CLAUDE_SKILL = ("---\n"
                "name: skill-creator\n"
                "description: Create new skills and improve existing ones.\n"
                "license: Complete terms in LICENSE.txt\n"
                "---\n\nHow to write a skill.")


def test_plain_skill_file_is_picked_up_without_atlas_frontmatter(env):
    """A Claude-style skill file (name + description, no id/type) dropped in the
    folder is a first-class asset — the filename is the id, type defaults to skill."""
    lib = env.project / "skills"
    lib.mkdir()
    (lib / "skill-creator.md").write_text(CLAUDE_SKILL, encoding="utf-8")

    row = next(r for r in LibraryView(env.project).list() if r["id"] == "skill-creator")
    assert row["type"] == "skill" and row["status"] == "draft"
    assert row["registered"] is False
    assert row["description"].startswith("Create new skills")

    # it can be adopted, and adoption preserves frontmatter Atlas does not model
    store = LibraryView(env.project).store("project")
    store.register_file("skill-creator")
    text = (lib / "skill-creator.md").read_text(encoding="utf-8")
    assert "id: skill-creator" in text and "type: skill" in text
    assert "license: Complete terms in LICENSE.txt" in text  # never destroyed
    assert (lib / "skill-creator.md").read_text(encoding="utf-8").endswith(
        "How to write a skill.\n")

    # and published straight from the folder
    rec = store.publish("skill-creator", "alex")
    assert rec["current_version"] == 1
    composed = LibraryView(env.project).compose(["skill-creator"])
    assert composed["assets"][0]["content"] == "How to write a skill."


def test_publish_direct_from_a_dropped_file(env):
    lib = env.project / "skills"
    lib.mkdir()
    (lib / "dropped-in.md").write_text(
        "---\nname: dropped-in\ndescription: Dropped straight in.\n---\n\nBody.",
        encoding="utf-8")
    store = LibraryView(env.project).store("project")
    rec = store.publish("dropped-in", "alex")   # auto-registers, no separate step
    assert rec["status"] == "published" and rec["type"] == "skill"


def test_unreadable_files_are_surfaced_never_silently_skipped(env):
    """A file we cannot use must say why. A silently skipped file is
    indistinguishable from a bug — which is exactly how this was found."""
    lib = env.project / "skills"
    lib.mkdir()
    (lib / "no-frontmatter.md").write_text("Just prose, no fence.", encoding="utf-8")
    (lib / "wrong-name.md").write_text(_asset("other-id"), encoding="utf-8")
    (lib / "bad-type.md").write_text(_asset("bad-type", type="sorcery"), encoding="utf-8")

    rows = {r["id"]: r for r in LibraryView(env.project).list()}
    assert set(rows) == {"no-frontmatter", "wrong-name", "bad-type"}
    for row in rows.values():
        assert row["status"] == "unreadable" and row["problem"]
        assert row["enabled"] is False        # can never reach an agent
    assert "frontmatter" in rows["no-frontmatter"]["problem"]
    assert "the filename is the id" in rows["wrong-name"]["problem"]
    assert "unknown asset type" in rows["bad-type"]["problem"]

    # a description is never invented from the name — it is what a reader uses
    # to decide whether to load the asset, so its absence is stated plainly
    (lib / "no-desc.md").write_text("---\nname: No Desc\n---\n\nBody.", encoding="utf-8")
    row = next(r for r in LibraryView(env.project).list() if r["id"] == "no-desc")
    assert row["status"] == "unreadable" and "description" in row["problem"]

    # unusable files stay out of composed context, with a warning
    result = LibraryView(env.project).compose(["no-frontmatter"])
    assert result["assets"] == []
    assert result["warnings"]


def test_unknown_frontmatter_keys_survive_a_round_trip(env):
    meta, body, canon = canonical_text(CLAUDE_SKILL, fallback_id="skill-creator")
    assert meta["license"] == "Complete terms in LICENSE.txt"
    again, body2, canon2 = canonical_text(canon)
    assert again == meta and body2 == body and canon2 == canon


# --- precedence / shadowing / disable -------------------------------------------

def test_project_shadows_user_shadows_builtin(env):
    (env.builtin / "style.md").write_text(
        _asset("style", type="preference", body="builtin style"), encoding="utf-8")
    _publish(env, "style", _asset("style", type="preference", body="user style"),
             scope="user")
    _publish(env, "style", _asset("style", type="preference", body="project style"))
    view = LibraryView(env.project)
    result = view.compose(["style"])
    assert len(result["assets"]) == 1
    assert "project style" in result["assets"][0]["content"]
    assert result["shadowed"] == [{"id": "style", "winning_scope": "project",
                                   "shadowed_scopes": ["built-in", "user"]}]
    listed = view.list(q="style")
    assert {r["scope"]: r["effective"] for r in listed} == {
        "built-in": False, "user": False, "project": True}


def test_disable_without_delete_via_override(env):
    (env.builtin / "noisy.md").write_text(_asset("noisy"), encoding="utf-8")
    view = LibraryView(env.project)
    view.store("project").set_enabled("noisy", False)  # override, not a record
    result = LibraryView(env.project).compose(["noisy"])
    assert not result["assets"]
    assert any(w["kind"] == "disabled-dependency" for w in result["warnings"])
    # still listed, marked disabled; re-enable clears the override
    row = next(r for r in LibraryView(env.project).list() if r["id"] == "noisy")
    assert row["enabled_effective"] is False
    view.store("project").set_enabled("noisy", True)
    assert LibraryView(env.project).compose(["noisy"])["assets"]


# --- dependencies / conflicts / profiles ----------------------------------------

def test_requires_closure_transitive_and_missing(env):
    _publish(env, "level-two", _asset("level-two"))
    _publish(env, "level-one", _asset("level-one", requires=["level-two", "ghost-dep"]))
    _publish(env, "top-skill", _asset("top-skill", requires=["level-one"]))
    result = LibraryView(env.project).compose(["top-skill"])
    ids = [a["id"] for a in result["assets"]]
    assert set(ids) == {"top-skill", "level-one", "level-two"}
    assert any(w["kind"] == "missing-dependency" and w["id"] == "ghost-dep"
               for w in result["warnings"])
    missing_sel = LibraryView(env.project).compose(["nowhere-man"])
    assert any(w["kind"] == "missing-selection" for w in missing_sel["warnings"])


def test_conflicts_reported_both_kept(env):
    _publish(env, "dark-ui", _asset("dark-ui", type="preference",
                                    conflicts=["light-ui"]))
    _publish(env, "light-ui", _asset("light-ui", type="preference"))
    result = LibraryView(env.project).compose(["dark-ui", "light-ui"])
    assert len(result["assets"]) == 2  # warn, never auto-resolve
    assert result["conflicts"] == [{"a": "dark-ui", "b": "light-ui",
                                    "declared_by": ["dark-ui"]}]
    # declared on the other side is detected too
    _publish(env, "tabs-style", _asset("tabs-style", type="preference"))
    _publish(env, "spaces-style", _asset("spaces-style", type="preference",
                                         conflicts=["tabs-style"]))
    r2 = LibraryView(env.project).compose(["tabs-style", "spaces-style"])
    assert r2["conflicts"][0]["declared_by"] == ["spaces-style"]


def test_profile_expansion_nested_and_cycle(env):
    _publish(env, "inner-skill", _asset("inner-skill"))
    _publish(env, "inner-prof", _asset("inner-prof", type="profile",
                                       assets=["inner-skill@1"]))
    _publish(env, "outer-prof", _asset("outer-prof", type="profile",
                                       assets=["inner-prof@1"]))
    result = LibraryView(env.project).compose(["outer-prof"])
    ids = [a["id"] for a in result["assets"]]
    assert ids[0] in ("outer-prof", "inner-prof")  # profiles render first
    assert "inner-skill" in ids and len(ids) == 3

    # a cycle warns instead of hanging
    _publish(env, "loop-a", _asset("loop-a", requires=["loop-b"]))
    _publish(env, "loop-b", _asset("loop-b", requires=["loop-a"]))
    looped = LibraryView(env.project).compose(["loop-a"])
    assert any(w["kind"] == "circular-reference" for w in looped["warnings"])
    assert {a["id"] for a in looped["assets"]} == {"loop-a", "loop-b"}


def test_version_pin_clash_nearest_wins(env):
    store = LibraryView(env.project).store("project")
    store.save_draft(_asset("shared-dep"))
    store.publish("shared-dep", "alex")
    store.save_draft(_asset("shared-dep", body="v2 body"))
    store.publish("shared-dep", "alex")
    _publish(env, "wants-old", _asset("wants-old", requires=["shared-dep@1"]))
    result = LibraryView(env.project).compose(["shared-dep@2", "wants-old"])
    clash = next(w for w in result["warnings"] if w["kind"] == "version-pin-clash")
    assert clash["kept"] == 2 and clash["dropped"] == 1  # selection is nearest
    shared = next(a for a in result["assets"] if a["id"] == "shared-dep")
    assert shared["version"] == 2


def test_drafts_excluded_unless_included(env):
    LibraryView(env.project).store("project").save_draft(_asset("wip-skill"))
    result = LibraryView(env.project).compose(["wip-skill"])
    assert not result["assets"]
    assert any(w["kind"] == "unpublished-asset" for w in result["warnings"])
    included = LibraryView(env.project).compose(["wip-skill"], include_drafts=True)
    assert included["assets"][0]["draft"] is True
    assert included["assets"][0]["version"] is None


def test_ordering_and_size_estimate(env, monkeypatch):
    _publish(env, "some-skill", _asset("some-skill", type="skill"))
    _publish(env, "some-rule", _asset("some-rule", type="constraint"))
    _publish(env, "some-pref", _asset("some-pref", type="preference"))
    result = LibraryView(env.project).compose(["some-skill", "some-pref", "some-rule"])
    assert [a["type"] for a in result["assets"]] == ["constraint", "preference", "skill"]
    assert result["est_chars"] == sum(len(a["content"]) for a in result["assets"])
    assert result["est_tokens"] == result["est_chars"] // 4
    assert result["oversized"] is False
    monkeypatch.setattr(config, "LIBRARY_WARN_CHARS", 5)
    assert LibraryView(env.project).compose(["some-skill"])["oversized"] is True
    rendered = render_assets(result)
    assert "### [constraint]" in rendered and "@v1, project, project" in rendered


# --- import / export --------------------------------------------------------------

def test_import_defaults_trust_and_roundtrip(env):
    claude_style = "---\nname: Getting Rich\ndescription: Money manual.\n---\n\nSpend less than you earn."
    rec = import_asset(env.project, claude_style, filename="getting_rich.md")
    assert rec["id"] == "getting-rich" and rec["trust"] == "imported"
    assert rec["status"] == "draft"  # imported content never auto-publishes
    with pytest.raises(LibraryError, match="already exists"):
        import_asset(env.project, claude_style)

    exported = export_asset(env.project, "getting-rich")
    meta, body = parse_asset_text(exported)
    assert body == "Spend less than you earn."  # body byte-stable
    assert meta["type"] == "skill"
    # export -> import round-trip is a fixed point
    LibraryView(env.project).store("project").publish("getting-rich", "alex")
    again = export_asset(env.project, "getting-rich")
    assert again == exported


def test_verify_integrity_catches_tampering(env):
    _publish(env, "sealed-skill", _asset("sealed-skill"))
    view = LibraryView(env.project)
    assert view.verify_integrity() == []
    snap = view.store("project").snapshot_path("sealed-skill", 1)
    snap.write_text(snap.read_text(encoding="utf-8") + "\ntampered", encoding="utf-8")
    problems = LibraryView(env.project).verify_integrity()
    assert problems == [{"id": "sealed-skill", "version": 1,
                         "problem": "hash-mismatch", "scope": "project"}]


def test_agent_draft_gets_agent_trust(env):
    store = LibraryView(env.project).store("project")
    rec = store.save_draft(_asset("bot-idea"),
                           created_by={"kind": "model", "identity": "claude"})
    assert rec["trust"] == "agent" and rec["created_by"]["kind"] == "model"


def test_new_asset_template_is_valid(env):
    text = new_asset_template("fresh-skill", "skill", "Fresh Skill")
    meta, _, _ = canonical_text(text)
    assert meta["id"] == "fresh-skill"
    with pytest.raises(LibraryError):
        new_asset_template("fresh-prof", "profile")


def test_compose_context_module_helper(env):
    _publish(env, "helper-skill", _asset("helper-skill"))
    result = compose_context(env.project, ["helper-skill"])
    a = result["assets"][0]
    assert {"id", "version", "content_hash", "scope", "trust"} <= set(a)


def test_mode_is_first_class_and_renders_before_guidance(env):
    mode = _publish(env, "focus-mode", _asset("focus-mode", type="mode"))
    skill = _publish(env, "build-skill", _asset("build-skill"))
    result = compose_context(env.project, [skill["id"], mode["id"]])
    assert [row["type"] for row in result["assets"]] == ["mode", "skill"]
    assert new_asset_template("routing-mode", "mode")


def test_frontmatter_block_description_is_supported():
    meta, body = parse_asset_text("""---
name: claude-api
description: |-
  First trigger sentence.
  Second trigger sentence.
---
Body
""")
    assert meta["description"] == "First trigger sentence.\nSecond trigger sentence."
    assert body == "Body"


def test_import_skill_directory_keeps_package_resources_out_of_context(env):
    package = env.project / "vendor" / "skills" / "specialist"
    (package / "scripts").mkdir(parents=True)
    (package / "scripts" / "check.py").write_text("print('ok')", encoding="utf-8")
    (package / "LICENSE.txt").write_text("licence prose", encoding="utf-8")
    (package / "SKILL.md").write_text("""---
name: Specialist
description: Uses its attached checker.
---
# Specialist
Run `scripts/check.py`.
""", encoding="utf-8")

    result = import_skill_directory(env.project, "vendor", source_name="World class pack")
    assert [row["id"] for row in result["imported"]] == ["specialist"]
    assert result["problems"] == []
    composed = compose_context(env.project, ["specialist"], include_drafts=True)
    asset = composed["assets"][0]
    assert asset["resource_root"] == "vendor/skills/specialist"
    rendered = render_assets(composed)
    assert "Package resources" in rendered
    assert "licence prose" not in rendered
