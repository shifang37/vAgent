"""H1: context v3 replaces dropped turns with one deduplicated history summary."""

import json

import pytest
from conftest import ScriptedModel
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage, messages_to_dict

from vagent.config import load_config
from vagent.context import (
    SUMMARY_CONTEXT_VERSION,
    SUMMARY_HEADER,
    ContextBuilder,
    assert_complete_protocol,
)
from vagent.errors import AppError
from vagent.runner import AgentRunner
from vagent.storage import FileStore
from vagent.summary import (
    ExtractiveSummarizer,
    SummaryStore,
    span_key,
    summary_target_bytes,
)
from vagent.tools import create_project_tools
from vagent.tracing import InMemoryExporter, Tracer

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


def history(turns, *, tool=True):
    messages = []
    for index in range(turns):
        messages.append(HumanMessage(content=f"第{index}轮需求：保留事实 F{index}，一句话占用上下文预算。"))
        body = f"第{index}轮回复：已保存，记录了事实 F{index}，并补充一段占用预算的说明。"
        if tool:
            call_id = f"call-{index}"
            messages.append(
                AIMessage(content=body, tool_calls=[{"name": "project_read", "args": {}, "id": call_id}])
            )
            messages.append(
                ToolMessage(content='{"ok":true,"data":{}}', tool_call_id=call_id, name="project_read")
            )
        else:
            messages.append(AIMessage(content=body))
    return messages


def build_args(messages, budget=BUDGET):
    return {
        "system_prompt": "系统规则",
        "history": messages,
        "project": dict(PROJECT),
        "tools": TOOLS,
        "skills": [],
        "run_budget": {"modelCallsRemaining": 5, "toolCallsRemaining": 5},
    }


def digest(messages, budget=BUDGET):
    return ExtractiveSummarizer().summarize(messages, target_bytes=summary_target_bytes(budget))


class CountingSummarizer(ExtractiveSummarizer):
    def __init__(self):
        self.calls = 0

    def summarize(self, messages, *, target_bytes):
        self.calls += 1
        return super().summarize(messages, target_bytes=target_bytes)


class FailingSummarizer:
    name = "failing"
    fingerprint = "failing-v1"

    def __init__(self):
        self.calls = 0

    def summarize(self, messages, *, target_bytes):
        self.calls += 1
        raise RuntimeError("摘要服务不可用")


class OversizedSummarizer:
    name = "oversized"
    fingerprint = "oversized-v1"

    def summarize(self, messages, *, target_bytes):
        return "x" * (BUDGET * 2)


def seed(store, messages, session="coffee"):
    store.ensure_session(session)
    store.transaction(lambda draft: draft["sessions"][session].update(messages=messages_to_dict(messages)))


def test_v3_without_summarize_keeps_the_exact_v2_layout():
    args = build_args(history(40))
    v2 = ContextBuilder(BUDGET, format_version=2).build(**args)
    v3 = ContextBuilder(BUDGET, format_version=SUMMARY_CONTEXT_VERSION).build(**args)
    assert messages_to_dict(v3.messages) == messages_to_dict(v2.messages)
    assert v3.input_bytes == v2.input_bytes and v3.dropped_messages == v2.dropped_messages


def test_v3_summarizes_dropped_turns_within_budget_and_recalls_facts():
    messages = history(40)
    builder = ContextBuilder(BUDGET, format_version=SUMMARY_CONTEXT_VERSION)

    probe = builder.build(**build_args(messages), summarize=True)
    assert probe.dropped and probe.summary is None
    assert "F0" in digest(probe.dropped)

    report = builder.build(**build_args(messages), history_summary=digest(probe.dropped), summarize=True)
    assert report.summary == digest(probe.dropped)
    assert report.input_bytes <= BUDGET
    assert SUMMARY_HEADER in report.messages[0].content
    assert "F0" in report.messages[0].content  # an old fact the truncation view dropped
    assert_complete_protocol(report.messages)

    truncated = ContextBuilder(BUDGET, format_version=2).build(**build_args(messages))
    assert "F0" not in truncated.messages[0].content and all(
        "F0" not in (message.content if isinstance(message.content, str) else "")
        for message in truncated.messages[1:]
    )


def test_v3_digest_that_overshoots_degrades_to_plain_truncation():
    messages = history(40)
    builder = ContextBuilder(BUDGET, format_version=SUMMARY_CONTEXT_VERSION)
    oversized = "x" * (BUDGET * 2)
    report = builder.build(**build_args(messages), history_summary=oversized, summarize=True)
    assert report.summary is None
    assert SUMMARY_HEADER not in report.messages[0].content
    plain = ContextBuilder(BUDGET, format_version=2).build(**build_args(messages))
    assert messages_to_dict(report.messages) == messages_to_dict(plain.messages)


def test_extractive_digest_is_deterministic_bounded_and_keeps_outcomes():
    messages = history(2)
    target = summary_target_bytes(BUDGET)
    first = ExtractiveSummarizer().summarize(messages, target_bytes=target)
    assert first == ExtractiveSummarizer().summarize(messages, target_bytes=target)
    assert len(first.encode("utf-8")) <= target
    assert "ok" in first and "工具调用: project_read" in first


def test_span_key_is_content_addressed_and_config_sensitive():
    span = history(2)
    key = span_key(span, config_fingerprint="extractive-v1")
    assert key == span_key(history(2), config_fingerprint="extractive-v1")
    assert key != span_key(history(3), config_fingerprint="extractive-v1")
    assert key != span_key(span, config_fingerprint="other")


def test_summary_store_round_trips_and_tolerates_corruption(tmp_path):
    store = SummaryStore(tmp_path)
    assert store.get("missing") is None
    store.put("k", "- 用户: 保留了事实 F0", model="test")
    assert store.get("k") == "- 用户: 保留了事实 F0"
    assert json.loads((tmp_path / "summaries.json").read_text(encoding="utf-8"))["version"] == 1

    (tmp_path / "summaries.json").write_text("{not json", encoding="utf-8")
    assert store.get("k") is None
    store.put("k", "- 用户: 重建", model="test")  # a corrupt cache is simply rebuilt
    assert store.get("k") == "- 用户: 重建"


async def test_v3_run_applies_the_digest_once_and_reuses_it_after_reopen(store):
    messages = history(40)
    seed(store, messages)
    events = []
    summarizer = CountingSummarizer()
    model = ScriptedModel(lambda *_: AIMessage(content="已根据摘要继续。"))
    runner = AgentRunner(
        store=store,
        model=model,
        tools=create_project_tools(),
        context=ContextBuilder(BUDGET, format_version=SUMMARY_CONTEXT_VERSION),
        summarizer=summarizer,
        on_event=events.append,
    )
    result = await runner.run("coffee", "继续这个项目", request_id="summary-one")

    assert result["status"] == "completed"
    assert result["contextVersion"] == SUMMARY_CONTEXT_VERSION
    system = model.inputs[0][0].content
    assert SUMMARY_HEADER in system and "F0" in system
    assert summarizer.calls == 1
    assert [event["type"] for event in events].count("context.summarized") == 1

    home = store.home
    store.close()
    with FileStore.open(home) as reopened:
        seed(reopened, messages)
        resumed = CountingSummarizer()
        again = ScriptedModel(lambda *_: AIMessage(content="已复用摘要。"))
        second = await AgentRunner(
            store=reopened,
            model=again,
            tools=create_project_tools(),
            context=ContextBuilder(BUDGET, format_version=SUMMARY_CONTEXT_VERSION),
            summarizer=resumed,
            on_event=events.append,
        ).run("coffee", "继续这个项目", request_id="summary-two")

        assert second["status"] == "completed"
        assert resumed.calls == 0  # the persisted digest is reused, never re-summarized
        assert SUMMARY_HEADER in again.inputs[0][0].content
        assert [event["type"] for event in events].count("context.summary_reused") == 1


async def v2_baseline(tmp_path, messages, *, name):
    """One v2 Run over an identical seeded session: the byte-for-byte reference."""
    with FileStore.open(tmp_path / name) as plain:
        seed(plain, messages)
        model = ScriptedModel(lambda *_: AIMessage(content="继续。"))
        result = await AgentRunner(
            store=plain,
            model=model,
            tools=create_project_tools(),
            context=ContextBuilder(BUDGET, format_version=2),
        ).run("coffee", "继续这个项目", request_id=f"{name}-run")
    return result, model


async def test_summary_failure_matches_v2_byte_for_byte(store, tmp_path):
    messages = history(40)
    seed(store, messages)
    events = []
    summarizer = FailingSummarizer()
    v3_model = ScriptedModel(lambda *_: AIMessage(content="继续。"))
    v3 = await AgentRunner(
        store=store,
        model=v3_model,
        tools=create_project_tools(),
        context=ContextBuilder(BUDGET, format_version=SUMMARY_CONTEXT_VERSION),
        summarizer=summarizer,
        on_event=events.append,
    ).run("coffee", "继续这个项目", request_id="summary-failure")

    v2, v2_model = await v2_baseline(tmp_path, messages, name="state-v2")

    assert v3["status"] == v2["status"] == "completed"
    assert summarizer.calls == 1
    # A digest that was never generated must not shrink the history: the Run keeps
    # exactly the v2 truncation, message for message and byte for byte.
    assert messages_to_dict(v3_model.inputs[0]) == messages_to_dict(v2_model.inputs[0])
    assert v3["contextBytes"] == v2["contextBytes"]
    assert "context.summarized" not in [event["type"] for event in events]


async def test_rejected_summary_does_not_emit_applied_event(store, tmp_path):
    messages = history(40)
    seed(store, messages)
    events = []
    exporter = InMemoryExporter()
    model = ScriptedModel(lambda *_: AIMessage(content="继续。"))
    result = await AgentRunner(
        store=store,
        model=model,
        tools=create_project_tools(),
        context=ContextBuilder(BUDGET, format_version=SUMMARY_CONTEXT_VERSION),
        summarizer=OversizedSummarizer(),
        on_event=events.append,
        tracing=Tracer(exporter),
    ).run("coffee", "继续这个项目", request_id="summary-rejected")

    assert result["status"] == "completed"
    types = [event["type"] for event in events]
    # The digest exists but the budget refused it, so nothing was applied.
    assert "context.summarized" not in types
    rejected = [event for event in events if event["type"] == "context.summary_rejected"]
    assert len(rejected) == 1 and rejected[0]["reason"] == "budget"
    spans = exporter.by_name("context.summarize", Tracer.trace_id_for(result["id"]))
    assert len(spans) == 1
    assert spans[0]["attributes"]["applied"] is False
    assert spans[0]["attributes"]["summaryBytes"] > 0
    v2, v2_model = await v2_baseline(tmp_path, messages, name="state-v2-rejected")
    assert messages_to_dict(model.inputs[0]) == messages_to_dict(v2_model.inputs[0])
    assert result["contextBytes"] == v2["contextBytes"]


def test_context_version_knob_defaults_to_v2_and_accepts_three():
    assert load_config({"VAGENT_HOME": "tmp/h1-knob"}).context_version == 2
    assert load_config({"VAGENT_HOME": "tmp/h1-knob", "VAGENT_CONTEXT_VERSION": "3"}).context_version == 3
    with pytest.raises(AppError) as error:
        load_config({"VAGENT_HOME": "tmp/h1-knob", "VAGENT_CONTEXT_VERSION": "4"})
    assert error.value.code == "INVALID_CONTEXT_VERSION"


def test_v3_signature_covers_the_summary_guidance(store):
    def signature(version):
        return AgentRunner(
            store=store,
            model=ScriptedModel(lambda *_: AIMessage(content="x")),
            tools=create_project_tools(),
            context=ContextBuilder(BUDGET, format_version=version),
        ).context_signature()

    assert signature(2) != signature(SUMMARY_CONTEXT_VERSION)
    assert signature(SUMMARY_CONTEXT_VERSION) == signature(SUMMARY_CONTEXT_VERSION)
    assert signature(2) == signature(2)
