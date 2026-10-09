import asyncio
import json

import pytest
from conftest import ScriptedModel, tool_call
from langchain_core.messages import AIMessage, HumanMessage, messages_from_dict

from vagent.context import RUN_BUDGET_HEADER, ContextBuilder, assert_complete_protocol
from vagent.errors import AppError
from vagent.models import DemoModel
from vagent.runner import AgentRunner, RunPolicy
from vagent.skills import SkillCatalog, register_skill_tool
from vagent.storage import FileStore
from vagent.tools import create_project_tools


async def test_demo_saves_real_artifact_and_reuses_request(store):
    catalog = SkillCatalog.discover()
    runner = AgentRunner(
        store=store,
        model=DemoModel(),
        tools=register_skill_tool(create_project_tools(), catalog),
        skills=catalog.list(),
    )
    first = await runner.run("coffee", "准备方案", request_id="stable")
    second = await runner.run("coffee", "准备方案", request_id="stable")
    assert first == second
    assert first["status"] == "completed"
    assert first["modelSteps"] == 4 and first["toolCalls"] == 3
    artifact_id = next(iter(store.snapshot()["artifacts"]))
    assert artifact_id in first["answer"]
    assert_complete_protocol(messages_from_dict(first["messages"]))
    with pytest.raises(AppError, match="不同需求"):
        await runner.run("coffee", "另一需求", request_id="stable")


async def test_committed_write_survives_provider_failure_and_is_not_replayed(store):
    def respond(messages, step):
        if step == 0:
            return tool_call("artifact_save", {"kind": "brief", "title": "saved", "content": "content"})
        raise RuntimeError("Authorization: Bearer secret-key-must-not-leak")

    model = ScriptedModel(respond)
    runner = AgentRunner(store=store, model=model, tools=create_project_tools())
    result = await runner.run("coffee", "保存", request_id="failed-request")
    assert result["status"] == "failed"
    assert "secret-key" not in json.dumps(result)
    assert len(store.snapshot()["artifacts"]) == 1
    assert store.snapshot()["sessions"]["coffee"]["messages"] == []
    assert await runner.run("coffee", "保存", request_id="failed-request") == result
    assert model.calls == 2


async def test_successful_conversation_continues_after_reopen(store):
    first = await AgentRunner(
        store=store, model=ScriptedModel(lambda *_: AIMessage(content="记住了")), tools=create_project_tools()
    ).run("coffee", "暖色调")
    home = store.home
    store.close()
    with FileStore.open(home) as reopened:
        model = ScriptedModel(lambda *_: AIMessage(content="继续"))
        second = await AgentRunner(store=reopened, model=model, tools=create_project_tools()).run(
            "coffee", "改成雨夜"
        )
        assert second["status"] == "completed"
        assert [m.content for m in model.inputs[0] if isinstance(m, HumanMessage)] == ["暖色调", "改成雨夜"]
        assert second["messages"][:2] == first["messages"]


@pytest.mark.parametrize(
    "name,args,code",
    [
        ("shell", {}, "UNKNOWN_TOOL"),
        ("project_update", {"expectedRevision": "0"}, "INVALID_ARGUMENTS"),
        ("project_read", {"path": "../../.env"}, "INVALID_ARGUMENTS"),
    ],
)
async def test_invalid_tools_return_results_to_model(store, name, args, code):
    def respond(messages, step):
        if step == 0:
            return tool_call(name, args)
        assert json.loads(messages[-1].content)["error"]["code"] == code
        return AIMessage(content="已看到错误")

    result = await AgentRunner(store=store, model=ScriptedModel(respond), tools=create_project_tools()).run(
        "coffee", "测试"
    )
    assert result["status"] == "completed"
    assert_complete_protocol(messages_from_dict(result["messages"]))


async def test_step_limit_prevents_unbounded_loop(store):
    model = ScriptedModel(lambda _, step: tool_call("project_read", call_id=f"read-{step}"))
    result = await AgentRunner(
        store=store, model=model, tools=create_project_tools(), policy=RunPolicy(max_steps=2)
    ).run("coffee", "循环")
    assert result["errorCode"] == "STEP_LIMIT"
    assert model.calls == 2
    assert_complete_protocol(messages_from_dict(result["messages"]))


async def test_model_sees_original_remaining_budget_after_failure_and_restart(store):
    observed = []

    def respond(messages, step):
        observed.append(json.loads(messages[0].content.split(RUN_BUDGET_HEADER)[1]))
        if step == 0:
            return tool_call("artifact_save", {"kind": "brief", "title": "saved", "content": "content"})
        raise RuntimeError("connection interrupted")

    runner = AgentRunner(
        store=store,
        model=ScriptedModel(respond),
        tools=create_project_tools(),
        policy=RunPolicy(max_steps=3, max_tool_calls=2),
    )
    first = await runner.run("coffee", "保存方案")
    assert first["resumable"] and first["modelSteps"] == 2
    home = store.home
    store.close()
    with FileStore.open(home) as reopened:

        def finish(messages, _):
            observed.append(json.loads(messages[0].content.split(RUN_BUDGET_HEADER)[1]))
            return AIMessage(content="已保存")

        final = await AgentRunner(
            store=reopened,
            model=ScriptedModel(finish),
            tools=create_project_tools(),
            policy=RunPolicy(max_steps=20, max_tool_calls=20),
        ).resume(first["id"])
        assert final["status"] == "completed" and final["modelSteps"] == 3
        assert final["toolCalls"] == 1 and len(reopened.snapshot()["artifacts"]) == 1
        assert all(
            RUN_BUDGET_HEADER not in message.content for message in messages_from_dict(final["messages"])
        )
    assert observed == [
        {"modelCallsRemaining": 3, "toolCallsRemaining": 2},
        {"modelCallsRemaining": 2, "toolCallsRemaining": 1},
        {"modelCallsRemaining": 1, "toolCallsRemaining": 1},
    ]


async def test_tool_limit_blocks_entire_batch_and_pairs_all_results(store):
    reply = AIMessage(
        content="",
        tool_calls=[
            {
                "name": "project_update",
                "args": {"expectedRevision": 0, "goal": "should not save"},
                "id": f"call-{i}",
            }
            for i in range(2)
        ],
    )
    result = await AgentRunner(
        store=store,
        model=ScriptedModel(lambda *_: reply),
        tools=create_project_tools(),
        policy=RunPolicy(max_tool_calls=1),
    ).run("coffee", "批量")
    assert result["errorCode"] == "TOOL_LIMIT"
    assert result["toolCalls"] == 0
    assert store.snapshot()["projects"]["coffee"]["goal"] == ""
    assert_complete_protocol(messages_from_dict(result["messages"]))


@pytest.mark.parametrize(
    "reply",
    [
        AIMessage(content="", tool_calls=[{"name": "project_read", "args": {}, "id": None}]),
        AIMessage(content="", tool_calls=[{"name": "project_read", "args": {}, "id": "same"}] * 2),
        AIMessage(
            content="",
            invalid_tool_calls=[{"name": "project_read", "args": "bad json", "id": "bad", "error": "parse"}],
        ),
    ],
)
async def test_malformed_calls_never_execute(store, reply):
    result = await AgentRunner(
        store=store, model=ScriptedModel(lambda *_: reply), tools=create_project_tools()
    ).run("coffee", "测试")
    assert result["errorCode"] == "INVALID_TOOL_CALL"
    assert result["toolCalls"] == 0
    assert store.snapshot()["operations"] == {}


async def test_pre_cancelled_run_never_starts_model(store):
    cancelled = asyncio.Event()
    cancelled.set()
    model = ScriptedModel(lambda *_: AIMessage(content="不应发生"))
    result = await AgentRunner(store=store, model=model, tools=create_project_tools()).run(
        "coffee", "停止", cancelled=cancelled
    )
    assert result["status"] == "cancelled"
    assert model.calls == 0


async def test_cancelled_before_tool_does_not_write_and_keeps_pairs(store):
    cancelled = asyncio.Event()

    def event_callback(event):
        if event["type"] == "tool.started":
            cancelled.set()

    model = ScriptedModel(
        lambda *_: tool_call("project_update", {"expectedRevision": 0, "goal": "must not write"})
    )
    result = await AgentRunner(
        store=store, model=model, tools=create_project_tools(), on_event=event_callback
    ).run("coffee", "停止", cancelled=cancelled)
    assert result["status"] == "cancelled" and result["toolCalls"] == 0
    assert store.snapshot()["projects"]["coffee"]["revision"] == 0
    assert_complete_protocol(messages_from_dict(result["messages"]))


async def test_timeout_cancels_hanging_model(store):
    stopped = asyncio.Event()

    async def hanging(*_):
        try:
            await asyncio.Event().wait()
        finally:
            stopped.set()

    result = await AgentRunner(
        store=store,
        model=ScriptedModel(hanging),
        tools=create_project_tools(),
        policy=RunPolicy(timeout_seconds=0.3),
    ).run("coffee", "超时")
    assert result["errorCode"] == "TIMEOUT"
    assert stopped.is_set()


async def test_external_task_cancellation_records_cancelled_status(store):
    started = asyncio.Event()

    async def hanging(*_):
        started.set()
        await asyncio.Event().wait()

    runner = AgentRunner(store=store, model=ScriptedModel(hanging), tools=create_project_tools())
    task = asyncio.create_task(runner.run("coffee", "中断"))
    await asyncio.wait_for(started.wait(), timeout=2)
    task.cancel()
    result = await task
    assert result["status"] == "cancelled"
    assert not store.snapshot()["sessions"]["coffee"]["messages"]


async def test_current_context_rejected_before_provider(store):
    model = ScriptedModel(lambda *_: AIMessage(content="不应发生"))
    result = await AgentRunner(
        store=store, model=model, tools=create_project_tools(), context=ContextBuilder(2048)
    ).run("coffee", "任务" * 3000)
    assert result["errorCode"] == "CONTEXT_LIMIT"
    assert result["modelSteps"] == model.calls == 0


async def test_project_facts_refreshed_after_write(store):
    def respond(messages, step):
        if step == 0:
            return tool_call("project_update", {"expectedRevision": 0, "audience": "夜班护士"})
        assert "夜班护士" in messages[0].content
        return AIMessage(content="已保存")

    result = await AgentRunner(store=store, model=ScriptedModel(respond), tools=create_project_tools()).run(
        "coffee", "更新受众"
    )
    assert result["status"] == "completed" and result["contextBytes"] > 0


async def test_concurrent_runs_are_rejected(store):
    started, release = asyncio.Event(), asyncio.Event()

    async def waiting(*_):
        started.set()
        await release.wait()
        return AIMessage(content="完成")

    runner = AgentRunner(store=store, model=ScriptedModel(waiting), tools=create_project_tools())
    pending = asyncio.create_task(runner.run("coffee", "第一个"))
    await asyncio.wait_for(started.wait(), timeout=2)
    try:
        with pytest.raises(AppError, match="已有任务"):
            await runner.run("other", "第二个")
    finally:
        release.set()
        await pending
