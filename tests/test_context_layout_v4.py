"""FX-12: context v4 keeps one stable prefix and moves per-step blocks into a tail."""

import pytest
from conftest import ScriptedModel
from langchain_core.messages import (
    AIMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
    messages_from_dict,
    messages_to_dict,
)

from vagent.config import load_config
from vagent.context import (
    MEMORY_HEADER,
    RUN_BUDGET_HEADER,
    SUMMARY_CONTEXT_VERSION,
    SUMMARY_HEADER,
    TAIL_CONTEXT_VERSION,
    TAIL_HEADER,
    ContextBuilder,
)
from vagent.errors import AppError
from vagent.runner import AgentRunner
from vagent.summary import ExtractiveSummarizer
from vagent.tools import create_project_tools

PROJECT = {
    "id": "coffee",
    "revision": 0,
    "goal": "咖啡店宣传片",
    "audience": "上班族",
    "style": "暖色",
    "constraints": [],
    "plan": [],
}
BUDGET = 12000
TOOLS = [{"type": "function", "function": {"name": "project_read", "parameters": {}}}]


def history(turns):
    messages = []
    for index in range(turns):
        messages.append(HumanMessage(content=f"第{index}轮需求：保留事实 F{index}，一句话占用上下文预算。"))
        call_id = f"call-{index}"
        messages.append(
            AIMessage(
                content=f"第{index}轮回复：已保存事实 F{index}。",
                tool_calls=[{"name": "project_read", "args": {}, "id": call_id}],
            )
        )
        messages.append(
            ToolMessage(content='{"ok":true,"data":{}}', tool_call_id=call_id, name="project_read")
        )
    return messages


def build_args(messages, *, budget=BUDGET, **overrides):
    return {
        "system_prompt": "系统规则",
        "history": messages,
        "project": dict(PROJECT),
        "tools": TOOLS,
        "skills": [],
        "run_budget": {"modelCallsRemaining": 5, "toolCallsRemaining": 5},
        **overrides,
    }


def seed(store, messages, session="coffee"):
    store.ensure_session(session)
    store.transaction(lambda draft: draft["sessions"][session].update(messages=messages_to_dict(messages)))


def test_adjacent_steps_share_the_history_prefix_and_differ_only_in_the_tail():
    builder = ContextBuilder(BUDGET, format_version=TAIL_CONTEXT_VERSION)
    step_one = builder.build(
        **build_args(history(3), run_budget={"modelCallsRemaining": 5, "toolCallsRemaining": 5})
    )
    step_two = builder.build(
        **build_args(history(3), run_budget={"modelCallsRemaining": 4, "toolCallsRemaining": 4})
    )

    # The system message and every history message are byte-identical across the steps:
    # only the synthesized tail differs.
    assert messages_to_dict(step_one.messages[:-1]) == messages_to_dict(step_two.messages[:-1])
    assert step_one.messages[-1].content != step_two.messages[-1].content
    assert step_one.messages[-1].content.startswith(TAIL_HEADER)

    # v3 puts the changing budget in the system message, which is exactly the cache miss
    # v4 removes: the same two steps share no common first message.
    v3 = ContextBuilder(BUDGET, format_version=SUMMARY_CONTEXT_VERSION)
    v3_one = v3.build(
        **build_args(history(3), run_budget={"modelCallsRemaining": 5, "toolCallsRemaining": 5})
    )
    v3_two = v3.build(
        **build_args(history(3), run_budget={"modelCallsRemaining": 4, "toolCallsRemaining": 4})
    )
    assert v3_one.messages[0].content != v3_two.messages[0].content


def test_v4_tail_carries_memory_and_budget_out_of_the_system_prefix():
    report = ContextBuilder(BUDGET, format_version=TAIL_CONTEXT_VERSION).build(
        **build_args(history(2), memory_block="- (goal) 咖啡店")
    )
    system = report.messages[0].content
    tail = report.messages[-1].content
    assert RUN_BUDGET_HEADER not in system and MEMORY_HEADER not in system
    assert RUN_BUDGET_HEADER in tail and MEMORY_HEADER in tail and "- (goal) 咖啡店" in tail


def test_adding_v4_leaves_v2_and_v3_byte_identical():
    messages = history(5)
    v2 = ContextBuilder(BUDGET, format_version=2).build(**build_args(messages))
    v3 = ContextBuilder(BUDGET, format_version=SUMMARY_CONTEXT_VERSION).build(**build_args(messages))
    v4 = ContextBuilder(BUDGET, format_version=TAIL_CONTEXT_VERSION).build(**build_args(messages))

    assert messages_to_dict(v2.messages) == messages_to_dict(v3.messages)
    assert messages_to_dict(v4.messages) != messages_to_dict(v3.messages)
    assert len(v4.messages) == len(v3.messages) + 1  # one synthesized tail, nothing else
    assert v4.dropped_messages == v3.dropped_messages == 0


def test_tail_role_can_be_human_and_is_validated():
    report = ContextBuilder(BUDGET, format_version=TAIL_CONTEXT_VERSION, tail_role="human").build(
        **build_args(history(1))
    )
    assert isinstance(report.messages[-1], HumanMessage)

    with pytest.raises(AppError) as error:
        ContextBuilder(BUDGET, format_version=TAIL_CONTEXT_VERSION, tail_role="assistant")
    assert error.value.code == "INVALID_CONTEXT_TAIL"


async def test_v4_run_adopts_the_digest_in_the_tail_and_never_persists_it(store):
    messages = history(40)
    seed(store, messages)
    model = ScriptedModel(lambda *_: AIMessage(content="已根据摘要继续。"))
    runner = AgentRunner(
        store=store,
        model=model,
        tools=create_project_tools(),
        context=ContextBuilder(BUDGET, format_version=TAIL_CONTEXT_VERSION),
        summarizer=ExtractiveSummarizer(),
    )
    result = await runner.run("coffee", "继续这个项目", request_id="v4-run")

    assert result["status"] == "completed"
    assert result["contextVersion"] == TAIL_CONTEXT_VERSION
    system = model.inputs[0][0].content
    tail = model.inputs[0][-1].content
    assert isinstance(model.inputs[0][-1], SystemMessage)
    assert SUMMARY_HEADER not in system and "F0" in tail
    # The tail is synthesized for the model call only; the committed history has none.
    committed = messages_from_dict(result["messages"])
    assert all(TAIL_HEADER not in message.content for message in committed)
    assert result["messages"][-1]["type"] == "ai"


def test_v4_signature_covers_the_tail_role(store):
    def signature(tail_role):
        return AgentRunner(
            store=store,
            model=ScriptedModel(lambda *_: AIMessage(content="x")),
            tools=create_project_tools(),
            context=ContextBuilder(BUDGET, format_version=TAIL_CONTEXT_VERSION, tail_role=tail_role),
        ).context_signature()

    assert signature("system") != signature("human")
    assert signature("system") == signature("system")
    assert (
        signature("system")
        != AgentRunner(
            store=store,
            model=ScriptedModel(lambda *_: AIMessage(content="x")),
            tools=create_project_tools(),
            context=ContextBuilder(BUDGET, format_version=SUMMARY_CONTEXT_VERSION),
        ).context_signature()
    )


def test_context_tail_knob_defaults_to_system_and_rejects_unknown_roles():
    base = load_config({"VAGENT_HOME": "tmp/h1-tail"})
    assert (base.context_version, base.context_tail) == (2, "system")
    switched = load_config({"VAGENT_HOME": "tmp/h1-tail", "VAGENT_CONTEXT_TAIL": "human"})
    assert switched.context_tail == "human"
    v4 = load_config({"VAGENT_HOME": "tmp/h1-tail", "VAGENT_CONTEXT_VERSION": "4"})
    assert v4.context_version == TAIL_CONTEXT_VERSION
    with pytest.raises(AppError) as error:
        load_config({"VAGENT_HOME": "tmp/h1-tail", "VAGENT_CONTEXT_TAIL": "assistant"})
    assert error.value.code == "INVALID_CONTEXT_TAIL"
