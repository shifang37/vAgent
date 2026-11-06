"""H0: close the design-doc / code gaps. Only the schema-feedback cap changes behavior."""

from conftest import ScriptedModel, tool_call
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage, messages_from_dict

from vagent.context import assert_complete_protocol
from vagent.runner import AgentRunner, schema_error_signature, schema_feedback_exhausted
from vagent.tools import create_project_tools

# project_update rejects a string revision and project_read rejects a path;
# both are schema-level rejections that reach the model as tool results.
BAD_UPDATE = {"expectedRevision": "0"}
BAD_READ = {"path": "../../.env"}


def payload(name, body, call_id="call-1"):
    return ToolMessage(content=body, tool_call_id=call_id, name=name)


async def test_identical_schema_errors_stop_after_one_feedback(store):
    model = ScriptedModel(
        lambda messages, step: tool_call("project_update", BAD_UPDATE, call_id=f"call-{step}")
    )
    result = await AgentRunner(store=store, model=model, tools=create_project_tools()).run("coffee", "改项目")

    assert result["status"] == "failed"
    assert result["errorCode"] == "SCHEMA_FEEDBACK_LIMIT"
    # The first rejection is fed back once; the second identical one stops the Run.
    assert model.calls == 2
    assert_complete_protocol(messages_from_dict(result["messages"]))
    assert "参数 Schema" in result["answer"]


async def test_distinct_schema_errors_are_not_capped(store):
    def respond(messages, step):
        if step == 3:
            return AIMessage(content="已看到两类错误")
        name, args = ("project_update", BAD_UPDATE) if step % 2 == 0 else ("project_read", BAD_READ)
        return tool_call(name, args, call_id=f"call-{step}")

    model = ScriptedModel(respond)
    result = await AgentRunner(store=store, model=model, tools=create_project_tools()).run("coffee", "测试")

    assert result["status"] == "completed"
    assert model.calls == 4


async def test_recovery_after_one_schema_error_completes(store):
    def respond(messages, step):
        if step == 0:
            return tool_call("project_update", BAD_UPDATE, call_id="bad")
        if step == 1:
            return tool_call("project_read", {}, call_id="good")
        return AIMessage(content="已修正")

    model = ScriptedModel(respond)
    result = await AgentRunner(store=store, model=model, tools=create_project_tools()).run("coffee", "测试")

    assert result["status"] == "completed"
    assert result["errorCode"] is None


async def test_schema_feedback_limit_resets_between_runs_in_one_session(store):
    """A session keeps earlier Runs in its history; the cap is per Run, not per session."""

    def script(messages, step):
        if step == 0:
            return tool_call("project_update", BAD_UPDATE, call_id="bad")
        return AIMessage(content="已修正")

    def runner_for(model):
        return AgentRunner(store=store, model=model, tools=create_project_tools())

    first = ScriptedModel(script)
    one = await runner_for(first).run("coffee", "第一次", request_id="schema-run-1")
    assert one["status"] == "completed" and first.calls == 2

    # The next Run repeats the very same mistake: it still gets one feedback and
    # completes instead of being capped by the previous Run's last batch.
    second = ScriptedModel(script)
    two = await runner_for(second).run("coffee", "第二次", request_id="schema-run-2")
    assert two["status"] == "completed"
    assert two["errorCode"] is None
    assert second.calls == 2

    # Repeating the same rejection twice inside THIS Run is still capped.
    third = ScriptedModel(
        lambda messages, step: tool_call("project_update", BAD_UPDATE, call_id=f"bad-{step}")
    )
    three = await runner_for(third).run("coffee", "第三次", request_id="schema-run-3")
    assert three["status"] == "failed"
    assert three["errorCode"] == "SCHEMA_FEEDBACK_LIMIT"
    assert third.calls == 2


def test_schema_feedback_verdict_comes_from_history():
    ok = '{"ok": true, "data": {}}'
    bad = '{"ok": false, "error": {"code": "INVALID_ARGUMENTS", "message": "x"}}'
    other = '{"ok": false, "error": {"code": "MEMORY_CONFLICT", "message": "x"}}'

    def turn(name, body, call_id):
        return [AIMessage(content="", tool_calls=[{"name": name, "args": {}, "id": call_id}])] + [
            payload(name, body, call_id)
        ]

    same = [HumanMessage(content="go"), *turn("project_update", bad, "a"), *turn("project_update", bad, "b")]
    assert schema_feedback_exhausted(same) is True

    # A HumanMessage starts a new Run: the previous Run's batch is not its "first"
    # identical rejection, so the same error in a new Run is fed back again.
    two_runs = [
        HumanMessage(content="first"),
        *turn("project_update", bad, "a"),
        AIMessage(content="已修正"),
        HumanMessage(content="second"),
        *turn("project_update", bad, "b"),
    ]
    assert schema_feedback_exhausted(two_runs) is False

    broken = [
        HumanMessage(content="go"),
        *turn("project_update", bad, "a"),
        *turn("project_read", ok, "b"),
        *turn("project_update", bad, "c"),
    ]
    assert schema_feedback_exhausted(broken) is False

    # A non-schema failure such as MEMORY_CONFLICT is a quality gate, not a schema error.
    quality = [
        HumanMessage(content="go"),
        *turn("artifact_save", other, "a"),
        *turn("artifact_save", other, "b"),
    ]
    assert schema_feedback_exhausted(quality) is False
    assert schema_feedback_exhausted([HumanMessage(content="go"), *turn("project_update", bad, "a")]) is False

    assert schema_error_signature([payload("project_update", bad)]) == frozenset(
        {("project_update", "INVALID_ARGUMENTS")}
    )
    assert schema_error_signature([payload("project_update", ok)]) is None
