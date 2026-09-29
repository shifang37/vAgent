import json

import pytest
from conftest import ScriptedModel, tool_call
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from vagent.context import ContextBuilder, assert_complete_protocol
from vagent.errors import AppError
from vagent.runner import AgentRunner
from vagent.skills import SkillCatalog, register_skill_tool
from vagent.tools import create_project_tools

PROJECT = {
    "id": "coffee",
    "revision": 1,
    "goal": "咖啡店",
    "audience": "上班族",
    "style": "暖色",
    "constraints": ["保留服装"],
    "plan": [],
}


def test_trimming_keeps_recent_whole_turns_tool_pairs_and_project_facts():
    history = [
        HumanMessage(content="旧需求" * 3000),
        AIMessage(content="旧答复"),
        HumanMessage(content="读取"),
        tool_call("project_read"),
        ToolMessage(content="已读取", tool_call_id="call-1"),
        AIMessage(content="完成"),
        HumanMessage(content="改成雨夜"),
    ]
    report = ContextBuilder(4096).build(system_prompt="规则", history=history, project=PROJECT, tools=[])
    assert report.dropped_messages == 2 and report.input_bytes <= 4096
    assert report.messages[-1].content == "改成雨夜" and "保留服装" in report.messages[0].content
    assert any(isinstance(m, ToolMessage) for m in report.messages)
    assert_complete_protocol(report.messages)
    assert len(history) == 7


def test_current_turn_is_never_silently_truncated():
    with pytest.raises(AppError, match="当前任务"):
        ContextBuilder(2048).build(
            system_prompt="规则", history=[HumanMessage(content="新需求" * 3000)], project=PROJECT, tools=[]
        )


def test_tool_definitions_count_towards_context_budget():
    builder = ContextBuilder(100000)
    args = {"system_prompt": "规则", "history": [HumanMessage(content="你好")], "project": PROJECT}
    assert (
        builder.build(**args, tools=create_project_tools().specs()).input_bytes
        > builder.build(**args, tools=[]).input_bytes + 1000
    )


@pytest.mark.parametrize(
    "messages",
    [
        [tool_call("project_read")],
        [ToolMessage(content="orphan", tool_call_id="unknown")],
        [tool_call("project_read"), HumanMessage(content="next")],
    ],
)
def test_invalid_protocol_rejected(messages):
    with pytest.raises(AppError):
        assert_complete_protocol(messages)


async def test_skill_body_only_loaded_after_recorded_tool_call(store):
    catalog = SkillCatalog.discover()
    body = "本 skill 提供创作方法"
    assert body not in json.dumps(catalog.list(), ensure_ascii=False)
    assert body in catalog.read("video-brief")["instructions"]

    def respond(messages, step):
        if step == 0:
            assert "video-brief" in messages[0].content and body not in messages[0].content
            return tool_call("skill_read", {"name": "video-brief"})
        assert body in messages[-1].content
        return AIMessage(content="已读取")

    result = await AgentRunner(
        store=store,
        model=ScriptedModel(respond),
        tools=register_skill_tool(create_project_tools(), catalog),
        skills=catalog.list(),
    ).run("coffee", "方案")
    assert result["status"] == "completed"
    operation = next(iter(store.snapshot()["operations"].values()))
    assert operation["result"]["data"]["version"] == catalog.read("video-brief")["version"]


def test_skill_path_and_unknown_name_rejected(store):
    registry = register_skill_tool(create_project_tools(), SkillCatalog.discover())
    args = {"store": store, "project_id": "coffee", "operation_key": "skill"}
    assert (
        registry.execute("skill_read", {"name": "../../.env"}, **args)["error"]["code"] == "INVALID_ARGUMENTS"
    )
    assert registry.execute("skill_read", {"name": "unknown"}, **args)["error"]["code"] == "SKILL_NOT_FOUND"


@pytest.mark.parametrize(
    "source",
    [
        "---\nname: wrong\ndescription: test\n---\nbody",
        "---\nname: custom\nname: custom\ndescription: test\n---\nbody",
        "---\nname: custom\ndescription: &desc test\nother: *desc\n---\nbody",
        "---\nname: custom\ndescription: test\n---\n",
        "x" * 17000,
    ],
)
def test_invalid_or_oversize_skills_rejected(tmp_path, source):
    folder = tmp_path / "custom"
    folder.mkdir()
    (folder / "SKILL.md").write_text(source, encoding="utf-8")
    with pytest.raises(AppError):
        SkillCatalog.discover(tmp_path)


def test_skill_snapshot_and_version_change(tmp_path):
    folder = tmp_path / "custom"
    folder.mkdir()
    file = folder / "SKILL.md"
    header = "---\nname: custom\ndescription: test\n---\n"
    file.write_text(header + "one", encoding="utf-8")
    first = SkillCatalog.discover(tmp_path)
    file.write_text(header + "two", encoding="utf-8")
    second = SkillCatalog.discover(tmp_path)
    assert first.read("custom")["instructions"] == "one"
    assert second.read("custom")["instructions"] == "two"
    assert first.read("custom")["version"] != second.read("custom")["version"]


def test_symlink_skill_root_rejected(tmp_path):
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "linked"
    try:
        link.symlink_to(real, target_is_directory=True)
    except OSError:
        pytest.skip("Windows account lacks symlink permission")
    with pytest.raises(AppError, match="普通目录"):
        SkillCatalog.discover(link)
