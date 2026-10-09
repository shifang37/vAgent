import hashlib
import json
from copy import deepcopy
from os.path import commonprefix

import httpx
import pytest
from conftest import ScriptedModel, tool_call
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage, messages_to_dict

from vagent.context import (
    PROJECT_HEADER,
    RUN_BUDGET_HEADER,
    SKILLS_HEADER,
    ContextBuilder,
    assert_complete_protocol,
)
from vagent.errors import AppError
from vagent.models import DeepSeekModel
from vagent.runner import SYSTEM_PROMPT, AgentRunner
from vagent.skills import SkillCatalog, register_skill_tool
from vagent.storage import FileStore
from vagent.tools import create_project_tools

PROJECT = {
    "id": "coffee",
    "revision": 0,
    "goal": "咖啡店宣传片",
    "audience": "上班族",
    "style": "暖色",
    "constraints": ["保留原版", "字幕使用中文"],
    "plan": [],
}


def context_input():
    catalog = SkillCatalog.discover()
    return {
        "system_prompt": SYSTEM_PROMPT,
        "project": deepcopy(PROJECT),
        "history": [HumanMessage(content="准备创作方案")],
        "tools": register_skill_tool(create_project_tools(), catalog).specs(),
        "skills": catalog.list(),
    }


def test_stable_prefix_survives_project_changes_and_discovery_order():
    args = context_input()
    original = deepcopy(args)
    builder = ContextBuilder()
    first = builder.build(**args)
    reordered_tools = [dict(reversed(tool.items())) for tool in reversed(args["tools"])]
    second = builder.build(
        **{
            **args,
            "project": {**args["project"], "audience": "夜班护士", "revision": 1},
            "tools": reordered_tools,
            "skills": list(reversed(args["skills"])),
        }
    )
    fixed = first.messages[0].content.split(PROJECT_HEADER)[0]
    assert fixed == second.messages[0].content.split(PROJECT_HEADER)[0]
    assert "video-brief" in fixed and "shot-description" in fixed
    assert "夜班护士" not in fixed and "夜班护士" in second.messages[0].content
    assert "本 skill 提供创作方法" not in fixed
    assert first.tools == second.tools
    assert [t["function"]["name"] for t in first.tools] == sorted(
        t["function"]["name"] for t in args["tools"]
    )
    assert json.dumps(first.tools, ensure_ascii=False) == json.dumps(second.tools, ensure_ascii=False)
    assert args == original


def test_json_mapping_order_does_not_change_prompt_or_tool_definitions():
    args = context_input()
    first = ContextBuilder().build(**args)
    second = ContextBuilder().build(
        **{
            **args,
            "project": dict(reversed(args["project"].items())),
            "skills": [dict(reversed(skill.items())) for skill in reversed(args["skills"])],
        }
    )
    assert messages_to_dict(first.messages) == messages_to_dict(second.messages)
    assert first.input_bytes == second.input_bytes


def test_run_budget_is_measured_after_stable_context_without_mutating_history():
    args = context_input()
    original = deepcopy(args)
    builder = ContextBuilder()
    plain = builder.build(**args)
    first = builder.build(**args, run_budget={"modelCallsRemaining": 8, "toolCallsRemaining": 12})
    last = builder.build(**args, run_budget={"modelCallsRemaining": 1, "toolCallsRemaining": 4})
    assert first.messages[0].content.split(RUN_BUDGET_HEADER)[0] == plain.messages[0].content
    assert last.messages[0].content.split(RUN_BUDGET_HEADER)[0] == plain.messages[0].content
    assert first.input_bytes > plain.input_bytes
    assert first.messages[1:] == last.messages[1:] == plain.messages[1:]
    assert first.tools == last.tools and args == original
    with pytest.raises(AppError, match="当前任务"):
        ContextBuilder(plain.input_bytes).build(
            **args, run_budget={"modelCallsRemaining": 8, "toolCallsRemaining": 12}
        )
    legacy = ContextBuilder(format_version=1)
    assert (
        legacy.build(**args, run_budget={"modelCallsRemaining": 8}).messages == legacy.build(**args).messages
    )


def test_tool_compaction_preserves_strings_number_literals_and_protocol_metadata():
    source = ' { "ok" : true, "data" : { "text": " a  b\\n\\"quote\\" ", "n": 1.234567890123456789e-10, "x": 1, "x": 2 } } '
    expected = '{"ok":true,"data":{"text":" a  b\\n\\"quote\\" ","n":1.234567890123456789e-10,"x":1,"x":2}}'
    user = HumanMessage(content="用户  原文\n不要更改  空白")
    ai = tool_call("project_read")
    ai.additional_kwargs["reasoning_content"] = "protocol metadata"
    result = ToolMessage(
        content=source, tool_call_id="call-1", name="project_read", additional_kwargs={"marker": 1}
    )
    args = context_input()
    history = [user, ai, result]
    original = messages_to_dict(history)
    report = ContextBuilder().build(**{**args, "history": history})
    assert report.messages[1] is user and report.messages[2] is ai
    assert report.messages[-1].content == expected
    assert report.messages[-1].tool_call_id == "call-1"
    assert report.messages[-1].additional_kwargs == result.additional_kwargs
    assert messages_to_dict(history) == original
    assert_complete_protocol(report.messages)


@pytest.mark.parametrize(
    "content", ["普通  文本\n原样保留", '{"note": "不是工具结果"}', '{"ok": true, broken']
)
def test_non_envelope_tool_text_is_not_rewritten(content):
    args = context_input()
    message = ToolMessage(content=content, tool_call_id="call-1")
    report = ContextBuilder().build(
        **{**args, "history": [HumanMessage(content="读取"), tool_call("project_read"), message]}
    )
    assert report.messages[-1] is message


def test_smaller_input_keeps_complete_current_turn_at_exact_budget():
    args = context_input()
    payload = {
        "ok": True,
        "data": {
            **PROJECT,
            "artifacts": [
                {
                    "id": f"artifact-{i}",
                    "kind": "brief",
                    "title": f"方案 {i}",
                    "version": 1,
                    "createdAt": "2026-09-30T00:00:00Z",
                }
                for i in range(20)
            ],
        },
    }
    args["history"] = [
        HumanMessage(content="读取项目"),
        tool_call("project_read"),
        ToolMessage(
            content=json.dumps(payload, ensure_ascii=False), tool_call_id="call-1", name="project_read"
        ),
    ]
    old = ContextBuilder(format_version=1).build(**args)
    current = ContextBuilder().build(**args)
    assert current.input_bytes < old.input_bytes
    kept = ContextBuilder(current.input_bytes).build(**args)
    assert kept.dropped_messages == 0
    assert json.loads(kept.messages[-1].content) == payload
    assert_complete_protocol(kept.messages)
    with pytest.raises(AppError, match="当前任务"):
        ContextBuilder(current.input_bytes, format_version=1).build(**args)


async def test_real_adapter_receives_stable_tools_and_prefix_across_project_update(store):
    requests = []
    catalog = SkillCatalog.discover()

    def handler(request):
        requests.append(json.loads(request.content))
        message = {"role": "assistant", "content": "已更新"}
        if len(requests) == 1:
            message = {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "update",
                        "type": "function",
                        "function": {
                            "name": "project_update",
                            "arguments": json.dumps({"expectedRevision": 0, "audience": "夜班护士"}),
                        },
                    }
                ],
            }
        return httpx.Response(
            200,
            json={
                "id": "test",
                "object": "chat.completion",
                "created": 1,
                "model": "deepseek-flash",
                "choices": [
                    {
                        "index": 0,
                        "finish_reason": "tool_calls" if len(requests) == 1 else "stop",
                        "message": message,
                    }
                ],
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await AgentRunner(
            store=store,
            model=DeepSeekModel("test-placeholder", http_client=client),
            stream_output=False,
            tools=register_skill_tool(create_project_tools(), catalog),
            skills=catalog.list(),
        ).run("coffee", "更新受众")
    assert result["status"] == "completed" and result["contextVersion"] == 2
    first, second = [r["messages"][0]["content"] for r in requests]
    assert first.split(PROJECT_HEADER)[0] == second.split(PROJECT_HEADER)[0]
    assert first.index(SKILLS_HEADER) < first.index(PROJECT_HEADER)
    assert "夜班护士" in second and requests[0]["tools"] == requests[1]["tools"]
    assert requests[1]["messages"][-1]["tool_call_id"] == "update"
    assert '"ok":true' in requests[1]["messages"][-1]["content"]
    legacy_args = context_input()
    legacy_before = ContextBuilder(format_version=1).build(**legacy_args).messages[0].content
    legacy_after = (
        ContextBuilder(format_version=1)
        .build(**{**legacy_args, "project": {**PROJECT, "audience": "夜班护士"}})
        .messages[0]
        .content
    )
    assert len(commonprefix([first, second])) > len(commonprefix([legacy_before, legacy_after]))


async def test_existing_checkpoint_uses_legacy_layout_then_new_run_uses_v2(store):
    catalog = SkillCatalog.discover()
    registry = register_skill_tool(create_project_tools(), catalog)

    def respond(messages, step):
        if step == 0:
            return tool_call("project_read")
        raise RuntimeError("retry later")

    previous = AgentRunner(
        store=store,
        model=ScriptedModel(respond),
        tools=registry,
        skills=catalog.list(),
        context=ContextBuilder(format_version=1),
    )
    first = await previous.run("coffee", "读取项目")
    expected_signature = hashlib.sha256(
        json.dumps(
            {
                "version": 1,
                "model": "scripted-test",
                "system": SYSTEM_PROMPT,
                "tools": registry.specs(),
                "skills": catalog.list(),
                "contextBytes": 65536,
            },
            sort_keys=True,
            ensure_ascii=False,
        ).encode()
    ).hexdigest()
    assert first["contextSignature"] == expected_signature
    store.transaction(lambda draft: draft["runs"][first["id"]].pop("contextVersion"))
    original_tool_text = first["messages"][-1]["data"]["content"]
    home = store.home
    store.close()
    with FileStore.open(home) as reopened:

        def finish(messages, _):
            assert messages[0].content.index(PROJECT_HEADER) < messages[0].content.index(SKILLS_HEADER)
            assert messages[-1].content == original_tool_text
            return AIMessage(content="完成")

        resumed = AgentRunner(
            store=reopened, model=ScriptedModel(finish), tools=registry, skills=catalog.list()
        )
        final = await resumed.resume(first["id"])
        assert final["status"] == "completed" and final["modelSteps"] == 3
        assert final.get("contextVersion", 1) == 1
        model = ScriptedModel(lambda *_: AIMessage(content="新任务"))
        new = await AgentRunner(store=reopened, model=model, tools=registry, skills=catalog.list()).run(
            "coffee", "新的任务"
        )
        assert new["contextVersion"] == 2
        assert model.inputs[0][0].content.index(SKILLS_HEADER) < model.inputs[0][0].content.index(
            PROJECT_HEADER
        )


async def test_unknown_layout_cannot_silently_change_resume_behavior(store):
    def fail(*_):
        raise RuntimeError("retry")

    runner = AgentRunner(store=store, model=ScriptedModel(fail), tools=create_project_tools())
    first = await runner.run("coffee", "test")
    store.transaction(lambda draft: draft["runs"][first["id"]].update(contextVersion=99))
    with pytest.raises(AppError, match="上下文格式版本"):
        await runner.resume(first["id"])
    assert runner.model.calls == 1


async def test_long_artifact_loads_only_requested_slice_without_altering_content(store):
    store.ensure_session("coffee")
    registry = create_project_tools()
    content = "开头标签" + ('正文  "空白保留"\n' * 1200) + "末尾标签"
    saved = registry.execute(
        "artifact_save",
        {"kind": "script", "title": "长脚本", "content": content},
        store=store,
        project_id="coffee",
        operation_key="prepare",
    )
    artifact_id = saved["data"]["artifactId"]

    def respond(messages, step):
        if step == 0:
            return tool_call("artifact_read", {"artifactId": artifact_id, "offset": 4000, "limit": 1200})
        result = json.loads(messages[-1].content)["data"]
        assert result["content"] == content[4000:5200]
        assert result["totalCharacters"] == len(content) and result["nextOffset"] == 5200
        assert "开头标签" not in messages[-1].content and "末尾标签" not in messages[-1].content
        return AIMessage(content="已阅读相关区段")

    final = await AgentRunner(store=store, model=ScriptedModel(respond), tools=registry).run(
        "coffee", "阅读中间部分"
    )
    assert final["status"] == "completed" and final["toolCalls"] == 1
    assert store.snapshot()["artifacts"][artifact_id]["versions"][0]["content"] == content
