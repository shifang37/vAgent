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
