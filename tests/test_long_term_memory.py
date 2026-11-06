"""H2 long-term memory: offline retrieval, dedupe/supersede, privacy, and opt-in wiring."""

import json
from pathlib import Path

import pytest
from conftest import ScriptedModel, tool_call
from langchain_core.messages import AIMessage

import vagent.memory as memory_module
from vagent.config import load_config
from vagent.context import MEMORY_HEADER, PROJECT_HEADER
from vagent.errors import AppError
from vagent.memory import (
    HashingEmbedder,
    MemoryIndex,
    cosine,
    fingerprint,
    looks_secret,
    project_facts,
)
from vagent.runner import AgentRunner
from vagent.tools import create_project_tools

GOAL = "面向年轻人的咖啡店，提升品牌认知和到店意愿"


@pytest.fixture
def index(tmp_path):
    return MemoryIndex(tmp_path / "data")


def seed(store, **fields):
    store.ensure_session("coffee")
    store.transaction(lambda draft: draft["projects"]["coffee"].update(fields))


def runner_for(store, **kwargs):
    return AgentRunner(
        store=store,
        model=ScriptedModel(lambda *_: AIMessage(content="好。")),
        tools=create_project_tools(),
        **kwargs,
    )


# --- embedder --------------------------------------------------------------------


def test_embedding_is_deterministic_and_normalized():
    embedder = HashingEmbedder()
    first = embedder.embed("咖啡店的品牌认知")
    assert first == embedder.embed("咖啡店的品牌认知")
    assert abs(sum(value * value for value in first) - 1.0) < 1e-9
    assert embedder.embed("") == [0.0] * embedder.dim


def test_cosine_ranks_related_text_above_unrelated():
    embedder = HashingEmbedder()
    query = embedder.embed("咖啡店 品牌")
    related = embedder.embed("面向年轻人的咖啡店品牌认知")
    unrelated = embedder.embed("量子计算 编译器 内核")
    assert cosine(query, related) > cosine(query, unrelated)


def test_memory_module_is_offline_and_free_of_io():
    text = (Path(__file__).resolve().parents[1] / "src" / "vagent" / "memory.py").read_text(encoding="utf-8")
    for forbidden in ("httpx", "requests", "socket", "random", "urlopen"):
        assert forbidden not in text


# --- records: dedupe, supersede, privacy -----------------------------------------


def test_project_facts_come_from_confirmed_fields_only():
    facts = project_facts(
        {"id": "p", "goal": GOAL, "audience": "年轻人", "style": "", "constraints": ["4:5"]}
    )
    assert facts == [("goal", GOAL), ("audience", "年轻人"), ("constraint", "4:5")]
    # Free text is never a source: an unknown key is ignored.
    assert project_facts({"id": "p", "notes": "模型自由发挥的内容"}) == []


def test_sync_is_idempotent_and_content_addressed(index):
    project = {"id": "coffee", "goal": GOAL}
    assert len(index.sync(project)) == 1
    assert index.sync(project) == []
    assert len(index.records("coffee")) == 1
    assert fingerprint("coffee", "goal", GOAL) in {record["id"] for record in index.records("coffee")}


def test_changing_a_fact_supersedes_the_previous_record(index):
    index.sync({"id": "coffee", "goal": GOAL})
    index.sync({"id": "coffee", "goal": "面向家庭的咖啡店"})
    current = index.records("coffee")
    assert [record["text"] for record in current] == ["面向家庭的咖啡店"]
    # The superseded record is retained, not deleted, and excluded from retrieval.
    assert len(index.records("coffee", current_only=False)) == 2
    assert [record["text"] for record in index.recall("coffee", "咖啡店")] == ["面向家庭的咖啡店"]


def test_adding_a_constraint_keeps_sibling_constraints_active(index):
    index.sync({"id": "coffee", "constraints": ["4:5 竖屏", "暖色调"]})
    assert len(index.sync({"id": "coffee", "constraints": ["4:5 竖屏", "暖色调", "夜景"]})) == 1

    assert sorted(record["text"] for record in index.records("coffee")) == sorted(
        ["4:5 竖屏", "暖色调", "夜景"]
    )
    assert len(index.records("coffee", current_only=False)) == 3


def test_removing_a_constraint_supersedes_only_that_record(index):
    index.sync({"id": "coffee", "constraints": ["A", "B"]})
    index.sync({"id": "coffee", "constraints": ["A"]})

    assert [record["text"] for record in index.records("coffee")] == ["A"]
    retired = [r for r in index.records("coffee", current_only=False) if r["supersededBy"]]
    assert [record["text"] for record in retired] == ["B"]
    assert retired[0]["supersededBy"] == "removed"


def test_a_removal_is_persisted_immediately_and_a_no_op_sync_does_not_write(index):
    index.sync({"id": "coffee", "constraints": ["A", "B"]})
    index.sync({"id": "coffee", "constraints": ["A"]})

    # The mark must land with the removal itself, not ride along with a later addition.
    stored = json.loads(index.path.read_text(encoding="utf-8"))
    assert stored["version"] == 2
    assert [r for r in stored["records"].values() if r["text"] == "B"][0]["supersededBy"] == "removed"

    before = index.path.read_text(encoding="utf-8")
    assert index.sync({"id": "coffee", "constraints": ["A"]}) == []
    assert index.path.read_text(encoding="utf-8") == before


def test_scalar_change_supersedes_only_the_old_value(index):
    index.sync({"id": "coffee", "goal": GOAL, "audience": "年轻人", "constraints": ["4:5"]})
    replacement = "面向家庭的咖啡店"
    index.sync({"id": "coffee", "goal": replacement, "audience": "年轻人", "constraints": ["4:5"]})

    records = {record["id"]: record for record in index.records("coffee", current_only=False)}
    old_goal = records[fingerprint("coffee", "goal", GOAL)]
    assert old_goal["supersededBy"] == fingerprint("coffee", "goal", replacement)
    # Unchanged facts of other kinds stay active.
    assert records[fingerprint("coffee", "audience", "年轻人")]["supersededBy"] is None
    assert records[fingerprint("coffee", "constraint", "4:5")]["supersededBy"] is None
    assert len(index.records("coffee")) == 3


def test_a_returning_fact_is_reactivated(index):
    index.sync({"id": "coffee", "constraints": ["A", "B"]})
    index.sync({"id": "coffee", "constraints": ["A"]})
    assert [record["text"] for record in index.records("coffee")] == ["A"]

    assert len(index.sync({"id": "coffee", "constraints": ["A", "B"]})) == 0  # B comes back
    assert sorted(record["text"] for record in index.records("coffee")) == ["A", "B"]


def test_eviction_keeps_the_newest_and_prefers_superseded(tmp_path, monkeypatch):
    monkeypatch.setattr(memory_module, "MAX_RECORDS", 3)
    ticks = iter(range(1, 10))
    index = MemoryIndex(tmp_path / "data", clock=lambda: f"2026-01-01T00:00:0{next(ticks)}+00:00")
    for goal in ("第一版", "第二版", "第三版", "第四版"):
        index.sync({"id": "coffee", "goal": goal})

    # Four records under a three-record cap: the oldest retired record goes first,
    # not the lexicographically smallest id.
    assert [record["text"] for record in index.records("coffee")] == ["第四版"]
    assert {record["text"] for record in index.records("coffee", current_only=False)} == {
        "第二版",
        "第三版",
        "第四版",
    }


def test_eviction_stays_local_while_the_project_cap_is_the_only_pressure(tmp_path, monkeypatch):
    monkeypatch.setattr(memory_module, "MAX_RECORDS_PER_PROJECT", 2)
    ticks = iter(range(1, 20))
    index = MemoryIndex(tmp_path / "data", clock=lambda: f"2026-01-01T00:00:{next(ticks)}+00:00")
    index.sync({"id": "coffee", "goal": "A"})
    index.sync({"id": "tea", "goal": "T"})
    index.sync({"id": "coffee", "goal": "B"})
    index.sync({"id": "coffee", "goal": "C"})  # coffee is now over its local cap

    # The overflow is paid by coffee's oldest retired record, never the other project.
    assert {record["text"] for record in index.records("tea")} == {"T"}
    assert [record["text"] for record in index.records("coffee")] == ["C"]
    assert {record["text"] for record in index.records("coffee", current_only=False)} == {"B", "C"}


async def test_one_memory_parse_serves_sync_and_recall_across_steps(store, monkeypatch):
    seed(store, goal=GOAL)
    MemoryIndex(store.home).sync({"id": "coffee", "goal": GOAL})  # create the sidecar first
    index = MemoryIndex(store.home)
    parses = []
    original = MemoryIndex._load

    def counting(self):
        parses.append(1)
        return original(self)

    monkeypatch.setattr(MemoryIndex, "_load", counting)

    def respond(messages, step):
        if step == 0:
            return tool_call("project_read", {}, call_id="read-1")
        return AIMessage(content="好。")

    runner = AgentRunner(
        store=store,
        model=ScriptedModel(respond),
        tools=create_project_tools(),
        memory=index,
        memory_limit=4,
    )
    result = await runner.run("coffee", "回顾咖啡店的定位", request_id="h2-parse-once")

    assert result["status"] == "completed"
    # Two model steps, each syncing and recalling: the sidecar is parsed once in total.
    assert len(parses) == 1


def test_v1_sidecar_is_readable_and_rewritten_as_v2(index):
    legacy = {
        "version": 1,
        "embedding": "hashing-bigram-v1",
        "records": {
            "old-goal": {
                "id": "old-goal",
                "projectId": "coffee",
                "kind": "goal",
                "text": "旧目标",
                "embedding": "hashing-bigram-v1",
                "vector": index.embedder.embed("旧目标"),
                "createdAt": "2026-01-01T00:00:00+00:00",
                "supersededBy": "older-goal",
            }
        },
    }
    index.path.parent.mkdir(parents=True, exist_ok=True)
    index.path.write_text(json.dumps(legacy, ensure_ascii=False), encoding="utf-8")

    # A v1 record with a truthy supersededBy is inactive, exactly like v2.
    assert index.records("coffee") == []
    assert len(index.sync({"id": "coffee", "goal": GOAL})) == 1
    stored = json.loads(index.path.read_text(encoding="utf-8"))
    assert stored["version"] == 2
    assert [record["text"] for record in index.records("coffee")] == [GOAL]


@pytest.mark.parametrize(
    "text",
    [
        "sk-abcdef0123456789",
        "后台sk-abcdef0123456789",  # a CJK prefix defeats a `\b` boundary
        "Authorization: Bearer eyJhbGciOiJIUzI1NiJ9",
        "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.abcdefghijklmnop",
        "后台密码：hunter2",
        "密钥：9f8e7d6c5b4a",
        "API_KEY: 123456",
        "token=0123456789abcdef0123456789abcdef",
    ],
)
def test_secret_detection_catches_credentials(text):
    assert looks_secret(text)


@pytest.mark.parametrize(
    "text",
    [
        "不要出现 API key 字样",
        "不要写密码到项目里",
        "提交 0123456789abcdef0123456789abcdef",  # a commit hash, not a credential
        "编号 12345678901234567890123456789012",
        "面向年轻人的咖啡店",
    ],
)
def test_secret_detection_ignores_benign_text(text):
    assert not looks_secret(text)


def test_secrets_are_never_indexed(index):
    assert looks_secret("sk-abcdef0123456789")
    assert looks_secret("API_KEY = 值")
    assert not looks_secret("面向年轻人的咖啡店")
    index.sync({"id": "coffee", "goal": GOAL, "constraints": ["sk-secret0000000000"]})
    stored = json.dumps(index.records("coffee"), ensure_ascii=False)
    assert "secret0000000000" not in stored


def test_sidecar_is_corruption_tolerant_and_outside_state(index):
    index.sync({"id": "coffee", "goal": GOAL})
    assert index.path.name == "memory.json"
    index.path.write_text("{ not json", encoding="utf-8")
    assert index.records("coffee") == []
    index.sync({"id": "coffee", "goal": GOAL})
    assert len(index.records("coffee")) == 1


# --- retrieval -------------------------------------------------------------------


def test_recall_is_relevance_ordered_and_budget_clipped(index):
    index.sync({"id": "coffee", "goal": GOAL, "audience": "年轻人"})
    assert index.recall("coffee", "咖啡店")[0]["kind"] == "goal"
    assert index.recall("coffee", "完全不相关的问题内容") == []
    block = index.render("coffee", "咖啡店品牌", limit=4, budget_bytes=24)
    assert block == "" or len(block.encode("utf-8")) <= 24


def test_cross_project_recall_is_explicit(index):
    index.sync({"id": "coffee", "goal": "咖啡店的品牌认知"})
    index.sync({"id": "tea", "goal": "茶饮店的品牌认知"})
    own = index.recall("coffee", "品牌认知")
    assert {record["projectId"] for record in own} == {"coffee"}
    every = index.recall("coffee", "品牌认知", cross_project=True)
    assert {record["projectId"] for record in every} == {"coffee", "tea"}


# --- config knob -----------------------------------------------------------------


def test_config_memory_defaults_off_and_parses_the_switch():
    assert load_config({"VAGENT_HOME": "tmp/h2-knob"}).memory_enabled is False
    assert load_config({"VAGENT_HOME": "tmp/h2-knob", "VAGENT_MEMORY": "on"}).memory_enabled is True
    assert load_config({"VAGENT_HOME": "tmp/h2-knob", "VAGENT_MEMORY_LIMIT": "8"}).memory_limit == 8


def test_config_rejects_an_invalid_memory_limit():
    with pytest.raises(AppError) as error:
        load_config({"VAGENT_HOME": "tmp/h2-knob", "VAGENT_MEMORY_LIMIT": "99"})
    assert error.value.code == "INVALID_MEMORY_LIMIT"


# --- runner wiring ---------------------------------------------------------------


async def test_memory_off_keeps_the_context_and_signature_untouched(store):
    seed(store, goal=GOAL)
    model = ScriptedModel(lambda *_: AIMessage(content="好。"))
    runner = AgentRunner(store=store, model=model, tools=create_project_tools())
    await runner.run("coffee", "回顾咖啡店的定位", request_id="h2-off")
    assert MEMORY_HEADER not in model.inputs[0][0].content
    assert runner.context_signature() == runner_for(store).context_signature()


async def test_memory_on_recalls_the_fact_after_the_frozen_prefix(store):
    seed(store, goal=GOAL)
    index = MemoryIndex(store.home)
    model = ScriptedModel(lambda *_: AIMessage(content="好。"))
    runner = AgentRunner(store=store, model=model, tools=create_project_tools(), memory=index, memory_limit=4)
    await runner.run("coffee", "回顾咖啡店的定位", request_id="h2-on")

    content = model.inputs[0][0].content
    assert MEMORY_HEADER in content and GOAL in content
    assert content.index(MEMORY_HEADER) > content.index(PROJECT_HEADER)
    # The stable prefix is byte-identical to a memory-off build: only a suffix is added.
    assert content.split(MEMORY_HEADER)[0] == _off_content(store)
    # Enabling memory moves the signature, so a Run signed without it cannot resume.
    assert runner.context_signature() != runner_for(store).context_signature()
    # The sidecar holds the record; state.json gains no memory collection.
    assert index.records("coffee") and "memory" not in store.snapshot()


def _off_content(store):
    from langchain_core.messages import HumanMessage

    from vagent.context import ContextBuilder
    from vagent.runner import RunPolicy

    policy = RunPolicy()
    builder = ContextBuilder(98304, format_version=2)
    report = builder.build(
        system_prompt=runner_for(store).system_prompt,
        history=[HumanMessage(content="回顾咖啡店的定位")],
        project=store.snapshot()["projects"]["coffee"],
        tools=create_project_tools().specs(),
        skills=[],
        run_budget={"modelCallsRemaining": policy.max_steps, "toolCallsRemaining": policy.max_tool_calls},
    )
    return report.messages[0].content
