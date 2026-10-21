"""E3 user skills: merge after the builtin root, isolate bad files, keep versions."""

import hashlib

import pytest
from conftest import ScriptedModel, tool_call

from vagent.config import Config
from vagent.errors import AppError
from vagent.runner import AgentRunner
from vagent.skills import SkillCatalog, discover_for
from vagent.tools import create_project_tools

HEADER = "---\nname: {name}\ndescription: test skill\n---\n"


def config_for(home):
    return Config(home=home, api_key=None)


def write_skill(root, name, body="本 skill 的说明", description="test skill"):
    folder = root / "skills" / name
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {description}\n---\n{body}", encoding="utf-8"
    )
    return folder / "SKILL.md"


def test_default_discovery_merges_the_user_root_after_the_builtin_root(tmp_path):
    file = write_skill(tmp_path, "custom")
    catalog = discover_for(config_for(tmp_path))
    names = [skill["name"] for skill in catalog.list()]
    builtin = [skill["name"] for skill in SkillCatalog.discover().list()]
    assert names[: len(builtin)] == builtin  # builtin first, each root sorted
    assert names == [*builtin, "custom"]
    assert catalog.diagnostics == []
    # Version is still the raw-byte hash prefix; the body is not in the metadata.
    assert catalog.read("custom")["version"] == hashlib.sha256(file.read_bytes()).hexdigest()[:12]
    assert "本 skill 的说明" not in str(catalog.list())


def test_a_missing_user_root_is_an_empty_extension_point(tmp_path):
    catalog = discover_for(config_for(tmp_path))
    assert [skill["name"] for skill in catalog.list()] == [
        skill["name"] for skill in SkillCatalog.discover().list()
    ]
    assert catalog.diagnostics == []


def test_a_builtin_name_wins_and_the_conflict_is_diagnosed(tmp_path):
    write_skill(tmp_path, "video-brief", body="用户同名说明")
    catalog = discover_for(config_for(tmp_path))
    builtin = next(skill for skill in SkillCatalog.discover().list() if skill["name"] == "video-brief")
    kept = catalog.read("video-brief")
    assert kept["version"] == builtin["version"] and "用户同名" not in kept["instructions"]
    assert catalog.diagnostics == [
        {
            "code": "SKILL_NAME_CONFLICT",
            "name": "video-brief",
            "message": "用户 skill 与已加载的 skill 重名，已忽略；内置名称优先。",
        }
    ]


def test_bad_user_files_are_isolated_without_stopping_the_catalog(tmp_path):
    write_skill(tmp_path, "good")
    broken = tmp_path / "skills" / "broken"
    broken.mkdir()
    (broken / "SKILL.md").write_text("not frontmatter", encoding="utf-8")
    (tmp_path / "skills" / "empty-dir").mkdir()
    oversized = tmp_path / "skills" / "oversized"
    oversized.mkdir()
    (oversized / "SKILL.md").write_text("x" * 17000, encoding="utf-8")
    catalog = discover_for(config_for(tmp_path))
    names = [skill["name"] for skill in catalog.list()]
    assert "good" in names and "broken" not in names and "oversized" not in names
    assert {item["code"] for item in catalog.diagnostics} == {"INVALID_SKILL", "INVALID_SKILL_FILE"}
    assert {item["name"] for item in catalog.diagnostics} == {"broken", "oversized"}
    assert str(tmp_path) not in str(catalog.diagnostics)


def test_the_combined_limit_keeps_builtins_and_the_first_user_names(tmp_path):
    builtin = SkillCatalog.discover()
    assert len(builtin.list()) == 2
    for index in range(32):
        write_skill(tmp_path, f"user-{index:02d}")
    catalog = discover_for(config_for(tmp_path))
    names = [skill["name"] for skill in catalog.list()]
    assert len(names) == 32
    assert names[:2] == [skill["name"] for skill in builtin.list()]
    assert names[2:] == [f"user-{index:02d}" for index in range(30)]
    assert [(item["code"], item["name"]) for item in catalog.diagnostics] == [
        ("SKILL_LIMIT", "user-30"),
        ("SKILL_LIMIT", "user-31"),
    ]


def test_an_explicit_root_still_replaces_everything_and_fails_fast(tmp_path):
    write_skill(tmp_path, "only-here")
    root = tmp_path / "skills"
    catalog = SkillCatalog.discover(root)
    assert [skill["name"] for skill in catalog.list()] == ["only-here"]
    broken = root / "broken"
    broken.mkdir()
    (broken / "SKILL.md").write_text("no frontmatter", encoding="utf-8")
    with pytest.raises(AppError) as refused:
        SkillCatalog.discover(root)
    assert refused.value.code == "INVALID_SKILL"


def test_symlinked_user_file_is_diagnosed_and_skipped(tmp_path):
    write_skill(tmp_path, "good")
    target = write_skill(tmp_path, "outside-source", "外部")
    link = tmp_path / "skills" / "linked"
    try:
        link.symlink_to(target.parent, target_is_directory=True)
    except OSError:
        pytest.skip("Windows account lacks symlink permission")
    catalog = discover_for(config_for(tmp_path))
    assert "linked" not in [skill["name"] for skill in catalog.list()]
    assert [item["code"] for item in catalog.diagnostics] == ["INVALID_SKILL_FILE"]


async def test_a_new_user_skill_changes_the_signature_and_refuses_the_old_run(store, tmp_path):
    def interrupt(messages, step):
        if step == 0:
            return tool_call("project_read")
        raise RuntimeError("offline interruption")

    before = discover_for(config_for(tmp_path))
    first = await AgentRunner(
        store=store,
        model=ScriptedModel(interrupt),
        tools=create_project_tools(),
        skills=before.list(),
    ).run("coffee", "读取项目")
    assert first["status"] == "failed" and first["resumable"]

    write_skill(tmp_path, "late-skill")
    after = discover_for(config_for(tmp_path))
    assert after.list() != before.list()
    resumed_model = ScriptedModel(lambda *_: tool_call("project_read"))
    current = AgentRunner(store=store, model=resumed_model, tools=create_project_tools(), skills=after.list())
    with pytest.raises(AppError) as changed:
        await current.resume(first["id"])
    assert changed.value.code == "RESUME_CONFIG_CHANGED"


def test_skill_read_still_uses_the_merged_catalog(store, tmp_path):
    write_skill(tmp_path, "late-skill")
    catalog = discover_for(config_for(tmp_path))
    assert catalog.read("late-skill")["instructions"] == "本 skill 的说明"
    with pytest.raises(AppError) as missing:
        catalog.read("not-there")
    assert missing.value.code == "SKILL_NOT_FOUND"
