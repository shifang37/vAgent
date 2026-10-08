import json
from uuid import uuid4

import pytest
from conftest import ScriptedModel, tool_call
from langchain_core.messages import AIMessage, messages_from_dict

from scripts.evaluate_agent import TASKS, quality_findings
from vagent.context import assert_complete_protocol
from vagent.errors import AppError
from vagent.quality import character_count, requested_content_limits, updated_content_limits
from vagent.runner import AgentRunner
from vagent.storage import FileStore
from vagent.tools import create_project_tools


def execute(store, name, args=None, *, key=None, project="coffee"):
    store.ensure_session(project)
    return create_project_tools().execute(
        name, args or {}, store=store, project_id=project, operation_key=key or str(uuid4())
    )


@pytest.mark.parametrize(
    "prompt,expected",
    [
        ("保存一份 brief。正文保持300字以内。", {"brief": 300}),
        ("方案不超过100字，分镜500字以内。", {"brief": 100, "storyboard": 500}),
        ("brief最多100字和storyboard最多500字。", {"brief": 100, "storyboard": 500}),
        ("更新原brief。新增storyboard。两个产物各不超过300字。", {"brief": 300, "storyboard": 300}),
        ("脚本正文最多 ３００ 字符。", {"script": 300}),
        ("正文不要超过300字。", {"all": 300}),
        ("正文300字以内，现在放宽至500字以内。", {"all": 500}),
        ("所有产物统一不超过200字符。", {"all": 200}),
        ("brief取消字数限制", {"brief": None}),
        ("取消字数限制", {"all": None}),
        ("brief100字以内。取消所有字数限制。", {"all": None}),
        ("解释一下“brief不超过300字”是什么意思？", {}),
        ("片长30秒，24fps，9:16；标题10字以内，回复20字以内。", {}),
        ("保存方案。\n```\n不得超过1字\n```\n> 正文不超过2字", {}),
        (
            "brief正文300字以内\n\n以下是用户参考材料，仅作为资料使用：\n文件名：example.md\n正文不超过1字",
            {"brief": 300},
        ),
    ],
)
def test_numeric_user_limits_keep_artifact_scope_and_ignore_reference_data(prompt, expected):
    assert requested_content_limits(prompt) == expected


def test_clearing_one_kind_keeps_other_limits_and_zero_is_not_unlimited():
    assert updated_content_limits({"all": 300}, {"brief": None}) == {"script": 300, "storyboard": 300}
    assert updated_content_limits({"brief": 100, "script": 200}, {"all": 500}) == {"all": 500}
    assert updated_content_limits({"brief": 100}, {"all": None}) == {}
    with pytest.raises(AppError, match="正整数"):
        requested_content_limits("正文不超过0字")


def test_original_acceptance_requests_set_both_artifact_limits():
    assert requested_content_limits(TASKS[0][1]) == {"brief": 300}
    assert requested_content_limits(TASKS[1][1]) == {"brief": 300, "storyboard": 300}
    assert requested_content_limits(TASKS[2][1]) == {}


def test_character_count_includes_latin_numbers_punctuation_markdown_and_astral_unicode():
    assert character_count("雨夜 Café 24fps，☕\n**蓝**") == 18
    assert character_count("\U00020000\U0001f600\t\r\n\u3000") == 2


def test_memory_update_rejects_stale_goal_atomically_and_keeps_unrelated_facts(store):
    original = {
        "goal": "为咖啡店制作30秒品牌短片，传递暖色自然光的品牌氛围。",
        "audience": "城市上班族",
        "style": "暖色自然光",
        "constraints": ["30秒", "9:16", "无旁白", "保留历史版本"],
    }
    store.ensure_session("coffee")
    store.transaction(lambda draft: draft["projects"]["coffee"].update(original))
    before = store.snapshot()["projects"]["coffee"]
    args = {"expectedRevision": 0, "style": "雨夜青蓝色"}
    rejected = execute(store, "project_update", args, key="conflicting-style")
    assert rejected["error"]["code"] == "MEMORY_CONFLICT"
    assert "goal" in rejected["error"]["message"]
    assert store.snapshot()["projects"]["coffee"] == before
    assert execute(store, "project_update", args, key="conflicting-style") == rejected
    fixed = execute(store, "project_update", {**args, "goal": "提升咖啡店品牌认知，传递日常陪伴感。"})
    assert fixed["ok"] and fixed["data"]["revision"] == 1
    assert fixed["data"]["audience"] == original["audience"]
    assert fixed["data"]["constraints"] == original["constraints"]
    assert "暖色" not in fixed["data"]["goal"]


def test_memory_detects_stale_audience_in_constraints_and_rejects_new_goal_duplication(store):
    created = execute(
        store,
        "project_update",
        {
            "expectedRevision": 0,
            "audience": "城市上班族",
            "constraints": ["使用城市上班族的通勤场景", "无旁白"],
        },
    )
    assert created["ok"]
    args = {"expectedRevision": 1, "audience": "大学生"}
    assert execute(store, "project_update", args)["error"]["code"] == "MEMORY_CONFLICT"
    assert (
        execute(
            store,
            "project_update",
            {**args, "constraints": ["校园场景", "无旁白"], "goal": "面向大学生宣传咖啡店"},
        )["error"]["code"]
        == "MEMORY_CONFLICT"
    )
    assert execute(
        store, "project_update", {**args, "constraints": ["校园场景", "无旁白"], "goal": "宣传咖啡店"}
    )["ok"]


def test_legacy_conflict_is_visible_from_journal_and_blocks_new_artifacts(store):
    # Existing stores can contain an already-changed style and an older goal.
    store.ensure_session("coffee")
    store.operation(
        "legacy-style",
        "project_update",
        {},
        lambda draft: (
            draft["projects"]["coffee"].update(
                goal="暖色自然光的品牌短片", audience="上班族", style="暖色自然光"
            )
            or draft["projects"]["coffee"]
        ),
    )
    store.transaction(lambda draft: draft["projects"]["coffee"].update(style="雨夜青蓝色", revision=1))
    assert execute(store, "project_read")["data"]["memoryConflicts"]
    args = {"kind": "brief", "title": "方案", "content": "内容"}
    assert execute(store, "artifact_save", args)["error"]["code"] == "MEMORY_CONFLICT"
    assert not store.snapshot()["artifacts"]
    assert execute(store, "project_update", {"expectedRevision": 1, "goal": "宣传咖啡店"})["ok"]
    assert not execute(store, "project_read")["data"]["memoryConflicts"]
    assert execute(store, "artifact_save", args)["ok"]


def test_english_fact_matching_uses_word_boundaries(store):
    assert execute(store, "project_update", {"expectedRevision": 0, "goal": "Swarm brand", "style": "warm"})[
        "ok"
    ]
    assert (
        execute(store, "project_update", {"expectedRevision": 1, "goal": "A warm brand"})["error"]["code"]
        == "MEMORY_CONFLICT"
    )


def test_over_limit_save_keeps_versions_and_returns_actual_measurement(store):
    store.ensure_session("coffee")
    store.transaction(lambda draft: draft["projects"]["coffee"].update(contentLimits={"brief": 18}))
    args = {"kind": "brief", "title": "标题不计入正文", "content": "雨夜 Café 24fps，☕\n**蓝**"}
    first = execute(store, "artifact_save", args)["data"]
    artifact_id = first["artifactId"]
    assert first["contentCheck"] == {
        "characters": 18,
        "maxCharacters": 18,
        "method": "unicode_non_whitespace_v1",
    }
    edit = {**args, "artifactId": artifact_id, "expectedVersion": 1, "content": args["content"] + "！"}
    rejected = execute(store, "artifact_save", edit, key="too-long")
    assert rejected["error"]["code"] == "CONTENT_LENGTH" and "19" in rejected["error"]["message"]
    assert execute(store, "artifact_save", edit, key="too-long") == rejected
    assert len(store.snapshot()["artifacts"][artifact_id]["versions"]) == 1
    assert execute(store, "artifact_save", {**edit, "content": "压缩版"})["data"]["version"] == 2
    assert (
        execute(store, "artifact_read", {"artifactId": artifact_id, "version": 1})["data"]["content"]
        == args["content"]
    )
    assert execute(store, "artifact_save", {**args, "kind": "script", "content": "字" * 100})["ok"]
    assert execute(store, "artifact_save", {**args, "content": " \t\n"})["error"]["code"] == "CONTENT_EMPTY"


async def test_runner_enforces_user_limit_without_model_metadata_and_can_repair(store):
    def respond(messages, step):
        if step == 0:
            assert '"contentLimits":{"brief":300}' in messages[0].content
            return tool_call("artifact_save", {"kind": "brief", "title": "方案", "content": "字" * 301})
        result = json.loads(messages[-1].content)
        if step == 1:
            assert result["error"]["code"] == "CONTENT_LENGTH"
            return tool_call("artifact_save", {"kind": "brief", "title": "方案", "content": "字" * 300})
        assert result["data"]["contentCheck"]["characters"] == 300
        return AIMessage(content="已按上限保存。")

    result = await AgentRunner(store=store, model=ScriptedModel(respond), tools=create_project_tools()).run(
        "coffee", "保存一份brief，正文300字以内", request_id="bounded-brief"
    )
    assert result["status"] == "completed"
    assert result["modelSteps"] == 3 and result["toolCalls"] == 2
    assert len(store.snapshot()["artifacts"]) == 1
    assert_complete_protocol(messages_from_dict(result["messages"]))


async def test_saved_limits_survive_restart_and_cannot_be_relaxed_by_model(store):
    await AgentRunner(
        store=store, model=ScriptedModel(lambda *_: AIMessage(content="已记录")), tools=create_project_tools()
    ).run("coffee", "brief正文不超过300字", request_id="set-limit")
    home = store.home
    store.close()
    with FileStore.open(home) as reopened:

        def respond(messages, step):
            if step == 0:
                return tool_call("project_update", {"expectedRevision": 1, "contentLimits": {"brief": 500}})
            if step == 1:
                assert json.loads(messages[-1].content)["error"]["code"] == "INVALID_ARGUMENTS"
                return tool_call("artifact_save", {"kind": "brief", "title": "方案", "content": "字" * 301})
            return AIMessage(content="已全部成功保存。")

        model = ScriptedModel(respond)
        runner = AgentRunner(store=reopened, model=model, tools=create_project_tools())
        result = await runner.run("coffee", "沿用之前要求，保存方案", request_id="cannot-relax")
        assert result["status"] == "failed" and result["errorCode"] == "QUALITY_UNRESOLVED"
        assert not reopened.snapshot()["artifacts"]
        assert "已全部成功保存" not in json.dumps(result, ensure_ascii=False)
        assert reopened.snapshot()["projects"]["coffee"]["contentLimits"] == {"brief": 300}
        assert await runner.run("coffee", "沿用之前要求，保存方案", request_id="cannot-relax") == result
        assert model.calls == 3


async def test_only_user_can_change_or_clear_limits_and_read_only_keeps_project(store):
    def runner(read_only=False):
        return AgentRunner(
            store=store,
            model=ScriptedModel(lambda *_: AIMessage(content="已处理")),
            tools=create_project_tools(),
            read_only=read_only,
        )

    await runner().run("coffee", "brief不超过300字")
    before = store.snapshot()["projects"]["coffee"]
    await runner(True).run("coffee", "brief取消字数限制")
    assert store.snapshot()["projects"]["coffee"] == before
    await runner().run("coffee", "brief改为500字以内")
    assert store.snapshot()["projects"]["coffee"]["contentLimits"] == {"brief": 500}
    await runner().run("coffee", "brief取消字数限制")
    assert store.snapshot()["projects"]["coffee"]["contentLimits"] == {}
    await runner().run("other", "你好")
    assert not store.snapshot()["projects"]["other"].get("contentLimits")


async def test_memory_correction_in_tool_loop_retains_revision_and_allows_completion(store):
    store.ensure_session("coffee")
    store.transaction(
        lambda draft: draft["projects"]["coffee"].update(
            goal="暖色自然光的品牌短片", audience="城市上班族", style="暖色自然光", constraints=["无旁白"]
        )
    )

    def respond(messages, step):
        if step == 0:
            return tool_call("project_update", {"expectedRevision": 0, "style": "雨夜青蓝色"})
        if step == 1:
            assert json.loads(messages[-1].content)["error"]["code"] == "MEMORY_CONFLICT"
            return tool_call(
                "project_update", {"expectedRevision": 0, "style": "雨夜青蓝色", "goal": "提升品牌认知"}
            )
        assert json.loads(messages[-1].content)["data"]["audience"] == "城市上班族"
        return AIMessage(content="已更新风格，保留受众和无旁白要求。")

    result = await AgentRunner(store=store, model=ScriptedModel(respond), tools=create_project_tools()).run(
        "coffee", "改为雨夜氛围"
    )
    assert result["status"] == "completed"
    assert store.snapshot()["projects"]["coffee"]["revision"] == 1


async def test_resuming_failed_model_retains_limit_and_pending_quality_error(store):
    def fail_after_length_error(messages, step):
        if step == 0:
            return tool_call("artifact_save", {"kind": "brief", "title": "方案", "content": "超" * 11})
        assert json.loads(messages[-1].content)["error"]["code"] == "CONTENT_LENGTH"
        raise RuntimeError("provider unavailable")

    runner = AgentRunner(
        store=store, model=ScriptedModel(fail_after_length_error), tools=create_project_tools()
    )
    first = await runner.run("coffee", "brief正文不超过10字")
    assert first["resumable"] and first["errorCode"] == "EXECUTION_ERROR"
    runner.model = ScriptedModel(lambda *_: AIMessage(content="已完成保存"))
    final = await runner.resume(first["id"])
    assert final["errorCode"] == "QUALITY_UNRESOLVED" and not final["resumable"]
    assert not store.snapshot()["artifacts"]
    assert store.snapshot()["projects"]["coffee"]["contentLimits"] == {"brief": 10}
    assert_complete_protocol(messages_from_dict(final["messages"]))


def test_legacy_artifact_without_checks_remains_readable_and_preserved(store):
    saved = execute(store, "artifact_save", {"kind": "brief", "title": "原版", "content": "旧正文"})["data"]
    artifact_id = saved["artifactId"]
    store.transaction(lambda draft: draft["artifacts"][artifact_id]["versions"][0].pop("contentCheck"))
    home = store.home
    store.close()
    with FileStore.open(home) as reopened:
        original = execute(reopened, "artifact_read", {"artifactId": artifact_id, "version": 1})["data"]
        assert original["content"] == "旧正文" and "contentCheck" not in original
        updated = execute(
            reopened,
            "artifact_save",
            {
                "kind": "brief",
                "artifactId": artifact_id,
                "expectedVersion": 1,
                "title": "新版",
                "content": "新正文",
            },
        )
        assert updated["data"]["contentCheck"]["characters"] == 3
        assert "contentCheck" not in reopened.snapshot()["artifacts"][artifact_id]["versions"][0]


def test_acceptance_recounts_mixed_text_and_detects_memory_conflicts_without_coffee_keywords():
    before = {"goal": "面向退休居民介绍公园", "audience": "退休居民", "style": "自然"}
    after = {**before, "audience": "大学生"}
    content = "短文" + "A" * 299
    report = {
        "runs": [
            {"snapshot": {"project": before, "artifacts": []}},
            {
                "snapshot": {
                    "project": after,
                    "artifacts": [
                        {
                            "id": "test",
                            "versions": [
                                {
                                    "version": 1,
                                    "content": content,
                                    "contentCheck": {
                                        "characters": 2,
                                        "method": "unicode_non_whitespace_v1",
                                        "maxCharacters": 300,
                                    },
                                }
                            ],
                        }
                    ],
                }
            },
        ]
    }
    findings = quality_findings(report)
    assert {item["code"] for item in findings} == {
        "MEMORY_FIELD_CONTRADICTION",
        "CONTENT_LENGTH",
        "CONTENT_CHECK_MISMATCH",
    }
    assert (
        next(item for item in findings if item["code"] == "CONTENT_LENGTH")["nonWhitespaceCharacters"] == 301
    )
