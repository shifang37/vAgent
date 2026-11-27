"""P2 acceptance: the project-memory tools, the context block and its budget fallback.

One project's ledger is written by the tools, rendered as a labelled data block directly
after the project facts, and read by a *second* session bound to the same project. The
ledger is bounded (D8), optimistic-locked (D5), project-scoped and never available to a
read-only Run. When the ledger is what pushes a request over the budget, it is dropped
first and reported as an event instead of failing the Run.
"""

import json

import pytest
from conftest import ScriptedModel, tool_call
from langchain_core.messages import AIMessage, HumanMessage

from vagent.application import ApplicationService
from vagent.config import Config
from vagent.context import (
    PROJECT_MEMORY_HEADER,
    RUN_BUDGET_HEADER,
    ContextBuilder,
)
from vagent.errors import AppError
from vagent.project_memory import (
    MEMORY_MAX_BYTES,
    MEMORY_MAX_ENTRIES,
    MEMORY_MAX_ENTRY_CHARS,
)
from vagent.runner import AgentRunner
from vagent.tools import create_project_tools

PROJECT = "coffee"

SECOND_SESSION = "ep02"


def call(registry, store, name, args, key, project_id=PROJECT):
    return registry.execute(name, args, store=store, project_id=project_id, operation_key=key)


def seed_progress(store, project_id=PROJECT, *, key="ep-03", text="第 3 集已保存 v2：4 场 11 镜"):
    """Progress is code-owned (D5): only a domain write may record it."""
    seed_progress_lines(store, {key: text}, project_id=project_id)


def seed_progress_lines(store, lines, project_id=PROJECT):
    """Write several code-owned progress lines at once, inside the storage limits."""

    def upsert(draft):
        memory = draft["memories"].setdefault(
            project_id,
            {"id": project_id, "revision": 0, "entries": [], "updatedAt": "2026-11-27T00:00:00+00:00"},
        )
        memory["entries"] = [
            {"key": entry["key"], "kind": entry["kind"], "text": entry["text"]}
            for entry in memory["entries"]
            if entry["key"] not in lines
        ]
        memory["entries"].extend(
            {"key": key, "kind": "progress", "text": text} for key, text in lines.items()
        )
        memory["entries"].sort(key=lambda entry: (entry["kind"], entry["key"]))
        memory["revision"] += 1
        memory["updatedAt"] = "2026-11-27T00:00:00+00:00"

    store.transaction(upsert)


def memory_tools():
    return create_project_tools()


def test_memory_read_and_update_round_trip(store):
    store.ensure_session(PROJECT)
    registry = memory_tools()

    empty = call(registry, store, "memory_read", {}, "read-empty")
    assert empty["ok"], empty
    assert empty["data"] == {
        "revision": 0,
        "entries": [],
        "bytes": 2,
        "limits": {
            "maxEntries": MEMORY_MAX_ENTRIES,
            "maxEntryChars": MEMORY_MAX_ENTRY_CHARS,
            "maxBytes": MEMORY_MAX_BYTES,
        },
    }

    saved = call(
        registry,
        store,
        "memory_update",
        {
            "expectedRevision": 0,
            "upsert": [
                {"key": "style-rain", "kind": "decision", "text": "全片保持雨夜冷调"},
                {"key": "rain-shop", "kind": "fact", "text": "店招是蓝色霓虹"},
                {"key": "next-shot", "kind": "todo", "text": "补拍第 4 集开场"},
            ],
        },
        "update-1",
    )
    assert saved["ok"], saved
    data = saved["data"]
    assert data["revision"] == 1
    # The rendered order is progress -> decision -> todo -> fact, then key.
    assert [entry["key"] for entry in data["entries"]] == ["style-rain", "next-shot", "rain-shop"]
    assert data["bytes"] == len(
        json.dumps(data["entries"], ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    )

    filtered = call(registry, store, "memory_read", {"kind": "todo"}, "read-todo")
    assert [entry["key"] for entry in filtered["data"]["entries"]] == ["next-shot"]
    assert filtered["data"]["revision"] == 1 and filtered["data"]["bytes"] == data["bytes"]

    removed = call(
        registry,
        store,
        "memory_update",
        {"expectedRevision": 1, "remove": ["next-shot", "missing-key"]},
        "update-2",
    )
    assert removed["ok"] and removed["data"]["revision"] == 2
    assert {entry["key"] for entry in removed["data"]["entries"]} == {"style-rain", "rain-shop"}

    # A write that changes nothing is not a new ledger revision.
    again = call(
        registry,
        store,
        "memory_update",
        {"expectedRevision": 2, "upsert": [{"key": "rain-shop", "kind": "fact", "text": "店招是蓝色霓虹"}]},
        "update-3",
    )
    assert again["ok"] and again["data"]["revision"] == 2


def test_memory_update_replays_the_same_operation(store):
    store.ensure_session(PROJECT)
    registry = memory_tools()
    args = {"expectedRevision": 0, "upsert": [{"key": "a", "kind": "fact", "text": "事实"}]}
    first = call(registry, store, "memory_update", args, "same-key")
    second = call(registry, store, "memory_update", args, "same-key")
    assert first == second and store.snapshot()["memories"][PROJECT]["revision"] == 1


def test_memory_limits_are_enforced(store):
    """P-05: 65 entries, 401 characters and over 8 KiB are refused without writing."""
    registry = memory_tools()
    store.ensure_session("many")
    store.ensure_session("long")
    store.ensure_session("big")

    def upsert(project_id, entries, revision, key):
        return call(
            registry,
            store,
            "memory_update",
            {"expectedRevision": revision, "upsert": entries},
            key,
            project_id=project_id,
        )

    short = [{"key": f"k{i}", "kind": "fact", "text": "x"} for i in range(32)]
    assert upsert("many", short, 0, "many-1")["ok"]
    assert upsert("many", [{**entry, "key": f"k{i + 32}"} for i, entry in enumerate(short)], 1, "many-2")[
        "ok"
    ]
    before = store.snapshot()["memories"]
    too_many = upsert("many", [{"key": "overflow", "kind": "fact", "text": "x"}], 2, "many-3")
    assert not too_many["ok"] and too_many["error"]["code"] == "MEMORY_LIMIT"
    assert store.snapshot()["memories"] == before

    too_long = upsert("long", [{"key": "long", "kind": "fact", "text": "字" * 401}], 0, "long-1")
    assert not too_long["ok"] and too_long["error"]["code"] == "MEMORY_LIMIT"
    assert "long" not in store.snapshot()["memories"]

    too_big = upsert(
        "big",
        [{"key": f"b{i}", "kind": "fact", "text": "x" * 400} for i in range(30)],
        0,
        "big-1",
    )
    assert not too_big["ok"] and too_big["error"]["code"] == "MEMORY_LIMIT"
    assert too_big["error"]["message"]
    assert "big" not in store.snapshot()["memories"]


def test_memory_revision_conflict(store):
    """P-06: a stale expectedRevision never overwrites the newer ledger."""
    store.ensure_session(PROJECT)
    registry = memory_tools()
    first = call(
        registry,
        store,
        "memory_update",
        {"expectedRevision": 0, "upsert": [{"key": "a", "kind": "fact", "text": "甲"}]},
        "conflict-1",
    )
    assert first["ok"] and first["data"]["revision"] == 1
    stale = call(
        registry,
        store,
        "memory_update",
        {"expectedRevision": 0, "upsert": [{"key": "b", "kind": "fact", "text": "乙"}]},
        "conflict-2",
    )
    assert not stale["ok"] and stale["error"]["code"] == "MEMORY_REVISION_CONFLICT"
    entries = store.snapshot()["memories"][PROJECT]["entries"]
    assert [entry["key"] for entry in entries] == ["a"]

    retried = call(
        registry,
        store,
        "memory_update",
        {"expectedRevision": 1, "upsert": [{"key": "b", "kind": "fact", "text": "乙"}]},
        "conflict-3",
    )
    assert retried["ok"] and retried["data"]["revision"] == 2


def test_progress_is_code_owned(store):
    store.ensure_session(PROJECT)
    registry = memory_tools()
    written = call(
        registry,
        store,
        "memory_update",
        {"expectedRevision": 0, "upsert": [{"key": "ep-03", "kind": "progress", "text": "已完成"}]},
        "progress-write",
    )
    assert not written["ok"] and written["error"]["code"] == "MEMORY_KIND_FORBIDDEN"
    seed_progress(store)
    removed = call(
        registry, store, "memory_update", {"expectedRevision": 1, "remove": ["ep-03"]}, "progress-remove"
    )
    assert not removed["ok"] and removed["error"]["code"] == "MEMORY_KIND_FORBIDDEN"
    assert store.snapshot()["memories"][PROJECT]["entries"][0]["key"] == "ep-03"


def test_read_only_cannot_update_memory(store):
    """P-07: a read-only surface publishes memory_read and never memory_update."""
    store.ensure_session(PROJECT)
    read_only = memory_tools().read_only()
    names = {spec["function"]["name"] for spec in read_only.specs()}
    assert "memory_read" in names and "memory_update" not in names
    refused = read_only.execute(
        "memory_update",
        {"expectedRevision": 0, "upsert": [{"key": "a", "kind": "fact", "text": "甲"}]},
        store=store,
        project_id=PROJECT,
        operation_key="read-only-write",
    )
    assert not refused["ok"] and refused["error"]["code"] == "UNKNOWN_TOOL"
    assert store.snapshot()["memories"] == {}


async def test_read_only_run_cannot_update_memory(store):
    store.ensure_session(PROJECT)
    model = ScriptedModel(
        lambda messages, step: (
            tool_call(
                "memory_update",
                {"expectedRevision": 0, "upsert": [{"key": "a", "kind": "fact", "text": "甲"}]},
            )
            if step == 0
            else AIMessage(content="只读任务不能写项目记忆。")
        )
    )
    result = await AgentRunner(store=store, model=model, tools=create_project_tools(), read_only=True).run(
        PROJECT, "记住这个决定"
    )
    assert result["status"] == "completed"
    assert "UNKNOWN_TOOL" in json.dumps(result["messages"], ensure_ascii=False)
    assert store.snapshot()["memories"] == {}


def test_memory_is_project_scoped(store):
    """P-08: a session bound to project B never sees project A's ledger."""
    store.ensure_session(PROJECT)
    store.ensure_session("other")
    registry = memory_tools()
    call(
        registry,
        store,
        "memory_update",
        {"expectedRevision": 0, "upsert": [{"key": "a", "kind": "decision", "text": "A 项目的决定"}]},
        "scope-1",
    )
    other = call(registry, store, "memory_read", {}, "scope-2", project_id="other")
    assert other["ok"] and other["data"] == {
        "revision": 0,
        "entries": [],
        "bytes": 2,
        "limits": other["data"]["limits"],
    }
    assert json.dumps(other["data"], ensure_ascii=False).find("A 项目的决定") == -1
    missing = call(registry, store, "memory_read", {}, "scope-3", project_id="missing")
    assert not missing["ok"] and missing["error"]["code"] == "PROJECT_NOT_FOUND"


async def test_new_session_sees_project_memory(store):
    """P-04: the first turn of a session bound to an existing project carries its ledger."""
    store.ensure_session(PROJECT)

    def seed(messages, step):
        if step == 0:
            return tool_call(
                "memory_update",
                {
                    "expectedRevision": 0,
                    "upsert": [
                        {"key": "style-rain", "kind": "decision", "text": "全片保持雨夜冷调"},
                        {"key": "ep-03", "kind": "todo", "text": "补拍第 3 集结尾"},
                    ],
                },
                call_id="seed-memory",
            )
        if step == 1:
            return tool_call(
                "artifact_save",
                {"kind": "brief", "title": "第 3 集大纲", "content": "雨夜追踪。"},
                call_id="seed-artifact",
            )
        return AIMessage(content="已记录第 3 集进度。")

    seed_model = ScriptedModel(seed)
    seed_model.name = "memory-seed-fixture"
    config = Config(home=store.home, api_key=None)
    async with ApplicationService.open(config, model=seed_model, existing_store=store) as service:
        await service.start(PROJECT, "开始第 3 集", "memory-seed-run")
        await service.task
    artifact_id = next(iter(store.snapshot()["artifacts"]))

    store.ensure_session(SECOND_SESSION, project_id=PROJECT)
    seen = {}

    def follow_up(messages, step):
        if step == 0:
            seen["system"] = messages[0].content
            return tool_call("project_read", {}, call_id="p04-project")
        seen["project"] = json.loads(messages[-1].content)["data"]
        return AIMessage(content="我记得第 3 集的进度与待办。")

    followup = ScriptedModel(follow_up)
    followup.name = "memory-followup-fixture"
    async with ApplicationService.open(config, model=followup, existing_store=store) as service:
        await service.start(SECOND_SESSION, "我们做到哪了？", "memory-followup-run")
        await service.task
        read_back = service.store.snapshot()["memories"][PROJECT]
        view = service.session(SECOND_SESSION)
        tool_names = {spec["function"]["name"] for spec in service.tools.specs()}

    system = seen["system"]
    assert PROJECT_MEMORY_HEADER in system
    assert "全片保持雨夜冷调" in system and "补拍第 3 集结尾" in system
    assert f'"revision":{read_back["revision"]}' in system
    assert system.index(PROJECT_MEMORY_HEADER) > system.index("当前项目的持久事实")
    assert {"memory_read", "memory_update"}.issubset(tool_names)
    assert {entry["key"] for entry in read_back["entries"]} == {"style-rain", "ep-03"}
    # The same session reads the project (and the old artifact) it is bound to.
    assert seen["project"]["id"] == PROJECT
    assert {item["id"] for item in seen["project"]["artifacts"]} == {artifact_id}
    assert {artifact["id"] for artifact in view["artifacts"]} == {artifact_id}


def test_memory_keeps_prefix_stable(store):
    """P-10: with the ledger unchanged the prefix through the memory block is identical."""
    store.ensure_session(PROJECT)
    seed_progress(store)
    state = store.snapshot()
    project, memory = state["projects"][PROJECT], state["memories"][PROJECT]
    for version in (2, 3, 4):
        builder = ContextBuilder(format_version=version)
        history = [HumanMessage(content="继续第 4 集"), AIMessage(content="好的")]
        first = builder.build(
            system_prompt="S",
            history=history,
            project=project,
            tools=create_project_tools().specs(),
            run_budget={"modelCallsRemaining": 5, "toolCallsRemaining": 8},
            project_memory=memory,
        )
        second = builder.build(
            system_prompt="S",
            history=history,
            project=project,
            tools=create_project_tools().specs(),
            run_budget={"modelCallsRemaining": 4, "toolCallsRemaining": 8},
            project_memory=memory,
        )
        content = first.messages[0].content
        assert PROJECT_MEMORY_HEADER in content
        if version == 4:
            # The per-step budget moved to the tail, so the whole system message is stable.
            assert second.messages[0].content == content
        else:
            assert (
                second.messages[0].content.split(RUN_BUDGET_HEADER)[0] == content.split(RUN_BUDGET_HEADER)[0]
            )
            assert second.messages[0].content != content


def test_v1_layout_never_renders_project_memory(store):
    """Existing checkpoints keep their original layout even with a ledger present."""
    store.ensure_session(PROJECT)
    seed_progress(store)
    state = store.snapshot()
    legacy = ContextBuilder(format_version=1)
    with_memory = legacy.build(
        system_prompt="S",
        history=[HumanMessage(content="继续")],
        project=state["projects"][PROJECT],
        tools=create_project_tools(memory=False).specs(),
        project_memory=state["memories"][PROJECT],
    )
    without = legacy.build(
        system_prompt="S",
        history=[HumanMessage(content="继续")],
        project=state["projects"][PROJECT],
        tools=create_project_tools(memory=False).specs(),
    )
    assert PROJECT_MEMORY_HEADER not in with_memory.messages[0].content
    assert with_memory.messages[0].content == without.messages[0].content
    assert with_memory.input_bytes == without.input_bytes
    assert with_memory.project_memory_dropped is False


def request_bytes(builder, project, tools, memory=None, budget=None, *, system_prompt="S"):
    report = builder.build(
        system_prompt=system_prompt,
        history=[HumanMessage(content="继续")],
        project=project,
        tools=tools,
        run_budget=budget,
        project_memory=memory,
    )
    return report.input_bytes


def test_memory_dropped_before_context_limit(store):
    """P-09: a ledger that does not fit is dropped and reported before failing the Run."""
    store.ensure_session(PROJECT)
    seed_progress_lines(store, {f"ep-{index}": "雨夜冷调" * 80 for index in range(1, 6)})
    state = store.snapshot()
    project, memory = state["projects"][PROJECT], state["memories"][PROJECT]
    tools = create_project_tools().specs()
    budget = {"modelCallsRemaining": 5, "toolCallsRemaining": 8}
    plain = request_bytes(ContextBuilder(65536), project, tools, budget=budget)
    # 32 spare bytes: the ledger (several hundred bytes) cannot fit, everything else does.
    builder = ContextBuilder(plain + 32, format_version=2)
    dropped = builder.build(
        system_prompt="S",
        history=[HumanMessage(content="继续")],
        project=project,
        tools=tools,
        run_budget=budget,
        project_memory=memory,
    )
    assert dropped.project_memory_dropped is True and dropped.memory_dropped is False
    assert PROJECT_MEMORY_HEADER not in dropped.messages[0].content
    assert dropped.input_bytes <= plain + 32

    # Without the fallback the request genuinely does not fit: memory never truncates.
    too_small = ContextBuilder(plain - 1, format_version=2)
    with pytest.raises(AppError) as error:
        too_small.build(
            system_prompt="S",
            history=[HumanMessage(content="继续")],
            project=project,
            tools=tools,
            run_budget=budget,
            project_memory=None,
        )
    assert error.value.code == "CONTEXT_LIMIT"


async def test_run_reports_the_project_memory_drop(store):
    store.ensure_session(PROJECT)
    seed_progress_lines(store, {f"ep-{index}": "雨夜冷调" * 80 for index in range(1, 6)})
    events = []
    model = ScriptedModel(lambda *_: AIMessage(content="继续"))
    runner = AgentRunner(store=store, model=model, tools=create_project_tools(), on_event=events.append)
    plain = request_bytes(
        runner.context,
        store.snapshot()["projects"][PROJECT],
        runner.tools.specs(),
        budget={"modelCallsRemaining": 8, "toolCallsRemaining": 12},
        system_prompt=runner.system_prompt,
    )
    runner.context = ContextBuilder(plain + 32, format_version=2)
    result = await runner.run(PROJECT, "继续")
    assert result["status"] == "completed"
    assert "context.project_memory_dropped" in {event["type"] for event in events}
    assert PROJECT_MEMORY_HEADER not in model.inputs[0][0].content


async def test_old_run_resumes_unchanged(store):
    """P-02: a Run recorded without the capability restores its original layout."""
    store.ensure_session(PROJECT)
    seed_progress(store)

    def interrupt(messages, step):
        if step == 0:
            return tool_call("project_read", {}, call_id="old-read")
        raise RuntimeError("offline interruption")

    old_model = ScriptedModel(interrupt)
    old_model.name = "old-run-fixture"
    old = AgentRunner(store=store, model=old_model, tools=create_project_tools(memory=False))
    before = await old.run(PROJECT, "读取旧项目", request_id="old-run")
    assert before["resumable"] and before["toolFeatures"] == {}
    assert before["contextSignature"] == old.context_signature()

    restored_model = ScriptedModel(lambda *_: AIMessage(content="已恢复"))
    restored_model.name = "old-run-fixture"
    restored = AgentRunner(store=store, model=restored_model, tools=create_project_tools()).for_record(before)
    names = {spec["function"]["name"] for spec in restored.tools.specs()}
    assert "memory_read" not in names and "memory_update" not in names
    assert restored.tools.features == {}
    assert restored.context_signature(before["contextVersion"]) == before["contextSignature"]

    resumed_model = ScriptedModel(lambda *_: AIMessage(content="按原上下文继续完成"))
    resumed_model.name = "old-run-fixture"
    resumed = AgentRunner(store=store, model=resumed_model, tools=create_project_tools())
    final = await resumed.resume(before["id"])
    assert final["status"] == "completed" and final["modelSteps"] == 3
    assert final["contextSignature"] == before["contextSignature"]
    system = resumed_model.inputs[0][0].content
    assert PROJECT_MEMORY_HEADER not in system and "第 3 集已保存" not in system
