"""Pre-M2 signatures captured at 92a4a87 restore byte-identically under M2 code."""

import hashlib

import httpx
import pytest
from conftest import ScriptedModel, tool_call
from langchain_core.messages import AIMessage

from vagent.context import ContextBuilder
from vagent.errors import AppError
from vagent.runner import AgentRunner
from vagent.storage import FileStore
from vagent.tools import create_project_tools
from vagent.video.jobs import JobService
from vagent.video.providers.mock import MockVideoAdapter
from vagent.video.providers.wan import WanAdapter
from vagent.video.tools import register_video_tools

# Captured by executing the 92a4a87 sources (tmp/m2-d1-capture/capture_signatures.py),
# not derived from the current rules. These hashes are the byte-identity evidence
# for restoring pre-M2 mock and live Runs while M2 adds storyboard tools.
PRE_M2_SIGNATURES = {
    "mock-v2-ctx1-ro0": "ffdeba060ab99a681d4ba2b020145c47fab00da268b86775c2810a85e3c738cd",
    "mock-v2-ctx1-ro1": "aa22e0faccbb80e5c659468b1499205c848ab3a48ad0dd55c850475889af9ce5",
    "mock-v2-ctx2-ro0": "205031c4ce0f775057cd25b77d34a6f4c9816d4ccb2ff6f95be9321c8fab84e6",
    "mock-v2-ctx2-ro1": "70250b2280852270cee1e4d199bd9816860607abe2711f1fcb7091150949ab6f",
    "live-v2-ctx1-ro0": "3ff7fb97dd89af5272aa53c7d73dc16bd6e872bdda70105e9098488a4501cdf8",
    "live-v2-ctx1-ro1": "998cd114c20385313ecf5ff1fa22713c360753b5f3f9d2435fe63b7dae9d9bc0",
    "live-v2-ctx2-ro0": "3ff330193321d1daa99389edce78453453bac5802c5da8c06022523c05d14c5c",
    "live-v2-ctx2-ro1": "a546c701424c5dece50899445bac3403b5529d0a5c2bf9c2809811477eddbb21",
}
PRE_M2_TOOL_NAMES = [
    "artifact_read",
    "artifact_save",
    "await_job",
    "job_get",
    "plan_update",
    "project_read",
    "project_update",
    "video_capabilities",
    "video_generate",
]
PRE_M2_READ_ONLY_TOOL_NAMES = [
    "artifact_read",
    "await_job",
    "job_get",
    "project_read",
    "video_capabilities",
]
PRE_M2_MOCK_FEATURES = {
    "video": {
        "mode": "mock",
        "toolsVersion": 2,
        "rulesVersion": 2,
        "capabilities": [
            {
                "provider": "mock",
                "model": "mock-t2v",
                "capabilitiesVersion": "v1",
                "mode": "mock",
                "inputTypes": ["text"],
                "specs": [
                    {"durationSeconds": 5.0, "resolution": "720p", "aspectRatio": "16:9"},
                    {"durationSeconds": 10.0, "resolution": "1080p", "aspectRatio": "9:16"},
                ],
                "supportsCancel": False,
                "supportsIdempotentSubmit": False,
            }
        ],
    }
}
PRE_M2_LIVE_FEATURES = {
    "video": {
        "mode": "live",
        "toolsVersion": 3,
        "rulesVersion": 3,
        "capabilities": [
            {
                "provider": "wan",
                "model": "wan2.7-t2v-2026-06-12",
                "capabilitiesVersion": "wan27-t2v-beijing-v1",
                "mode": "live",
                "inputTypes": ["text"],
                "specs": [{"durationSeconds": 5, "resolution": "720p", "aspectRatio": "16:9"}],
                "supportsCancel": False,
                "supportsIdempotentSubmit": False,
                "region": "cn-beijing",
                "maxPromptCharacters": 4991,
                "shotMode": "single",
                "audioMode": "auto",
                "promptExtend": False,
                "watermark": True,
                "seed": 0,
                "supportsResultUrlRefresh": False,
                "price": {
                    "currency": "CNY",
                    "unit": "output_second",
                    "unitPrice": "0.60",
                    "quantity": "5",
                    "amount": "3.00",
                    "priceVersion": "wan27-beijing-720p-20261010",
                    "checkedAt": "2026-10-10",
                    "sourceUrl": "https://help.aliyun.com/zh/model-studio/model-pricing#ba6f7744d5e0o",
                },
            }
        ],
    }
}


def mock_tools(store):
    return register_video_tools(create_project_tools(), JobService(store, [MockVideoAdapter(store)]))


@pytest.mark.parametrize("context_version", [1, 2])
@pytest.mark.parametrize("read_only", [False, True])
def test_pre_m2_mock_signature_and_toolset_restore_unchanged(store, context_version, read_only):
    tools = mock_tools(store)
    model = ScriptedModel(lambda *_: AIMessage(content="unused"))
    model.name = "m2-restore-fixture"
    runner = AgentRunner(
        store=store,
        model=model,
        tools=tools,
        read_only=read_only,
        execution_version=2,
        context=ContextBuilder(format_version=context_version),
    )
    assert runner.context_signature() == PRE_M2_SIGNATURES[f"mock-v2-ctx{context_version}-ro{int(read_only)}"]
    assert runner.tools.features == PRE_M2_MOCK_FEATURES
    expected_names = PRE_M2_READ_ONLY_TOOL_NAMES if read_only else PRE_M2_TOOL_NAMES
    assert sorted(spec["function"]["name"] for spec in runner.tools.specs()) == expected_names


@pytest.mark.parametrize("context_version", [1, 2])
@pytest.mark.parametrize("read_only", [False, True])
async def test_pre_m2_live_signature_and_toolset_restore_unchanged(store, context_version, read_only):
    async with WanAdapter(
        api_key="capture-fixture",
        workspace_id="capture-workspace",
        transport=httpx.MockTransport(lambda _: httpx.Response(500)),
    ) as wan:
        tools = register_video_tools(
            create_project_tools(), JobService(store, [MockVideoAdapter(store), wan]), mode="live"
        )
        model = ScriptedModel(lambda *_: AIMessage(content="unused"))
        model.name = "m2-restore-fixture"
        runner = AgentRunner(
            store=store,
            model=model,
            tools=tools,
            read_only=read_only,
            execution_version=2,
            context=ContextBuilder(format_version=context_version),
        )
        assert (
            runner.context_signature()
            == PRE_M2_SIGNATURES[f"live-v2-ctx{context_version}-ro{int(read_only)}"]
        )
        assert runner.tools.features == PRE_M2_LIVE_FEATURES
        expected_names = PRE_M2_READ_ONLY_TOOL_NAMES if read_only else PRE_M2_TOOL_NAMES
    assert sorted(spec["function"]["name"] for spec in runner.tools.specs()) == expected_names


def test_current_mock_toolset_is_ten_ten_and_keeps_the_frozen_four_four_view(store):
    tools = mock_tools(store)
    names = sorted(spec["function"]["name"] for spec in tools.specs())
    assert {"storyboard_read", "storyboard_save", "shot_generate", "asset_read"}.issubset(set(names))
    assert {"film_assemble", "film_read"}.issubset(set(names))
    assert {"episode_save", "bible_save"}.issubset(set(names))
    features = tools.features["video"]
    assert features["mode"] == "mock" and (features["toolsVersion"], features["rulesVersion"]) == (10, 10)
    frozen = tools.for_tools_version(4)
    assert (frozen.features["video"]["toolsVersion"], frozen.features["video"]["rulesVersion"]) == (4, 4)
    assert frozen.features["video"]["capabilities"] == features["capabilities"]
    frozen_names = sorted(spec["function"]["name"] for spec in frozen.specs())
    assert {"storyboard_read", "storyboard_save", "shot_generate", "asset_read"}.issubset(set(frozen_names))
    assert not {"film_assemble", "film_read"}.intersection(frozen_names)
    restored = tools.for_execution_version(2)
    assert restored.features == PRE_M2_MOCK_FEATURES
    assert sorted(spec["function"]["name"] for spec in restored.specs()) == PRE_M2_TOOL_NAMES


async def test_recorded_mock_v2_run_resumes_under_m2_with_original_tools(store):
    def begin(messages, step):
        if step == 0:
            return tool_call("video_capabilities")
        raise RuntimeError("offline interruption")

    model = ScriptedModel(begin)
    model.name = "m2-restore-fixture"
    runner = AgentRunner(store=store, model=model, tools=mock_tools(store), execution_version=2)
    first = await runner.run("coffee", "先确认模拟能力")
    assert first["executionVersion"] == 2 and first["resumable"]
    assert first["contextSignature"] == PRE_M2_SIGNATURES["mock-v2-ctx2-ro0"]
    assert first["toolFeatures"] == PRE_M2_MOCK_FEATURES
    home = store.home
    store.close()
    with FileStore.open(home) as reopened:
        resumed_model = ScriptedModel(lambda *_: AIMessage(content="按原工具集继续完成"))
        resumed_model.name = "m2-restore-fixture"
        current = AgentRunner(store=reopened, model=resumed_model, tools=mock_tools(reopened))
        assert current.execution_version == 3
        final = await current.resume(first["id"])
        assert final["status"] == "completed" and final["executionVersion"] == 2
        assert final["contextSignature"] == PRE_M2_SIGNATURES["mock-v2-ctx2-ro0"]
        assert final["toolFeatures"] == first["toolFeatures"]
        assert final["modelSteps"] == 3 and final["toolCalls"] == 1
        system_prompt = resumed_model.inputs[0][0].content
        assert "不能自动降低规格" in system_prompt
        assert "storyboard_save" not in system_prompt


# Captured from the D2 build (34cf1aa) with tmp/m2-d3-capture/capture_signatures.py,
# before D3 added the read-only asset tool and extended the M2 rules text inside the
# frozen 4/4 version. Pre-M2 (execution v2) stays byte-frozen above; an M2 Run
# recorded by the D2 build is refused with RESUME_CONFIG_CHANGED instead of silently
# resuming with a different tool set — the same pre-release boundary D1 recorded for
# D1-era Runs, and the reason these hashes are kept here as evidence.
D2_M2_SIGNATURES = {
    "mock-v3-ctx1-ro0": "0d93e06bb445b62295338d8f094f44c3ffc2db9a8118098f35bca89cc7d1920b",
    "mock-v3-ctx2-ro0": "e38edf7947f3e097271ecaea817e1733456a34c5a5e822079af090c9605da6b5",
    "live-v3-ctx1-ro0": "82e9891b5052d82b9a8f0d03d1f4fc0ac686a30cf25145d74c9eced256deb61b",
    "live-v3-ctx2-ro0": "68feeb058b6ea912d20e0e215c6ab70d9958eaa7ad24ab29565156180158e6b2",
}
D2_M2_TOOL_NAMES = [
    "artifact_read",
    "artifact_save",
    "await_job",
    "job_get",
    "plan_update",
    "project_read",
    "project_update",
    "shot_generate",
    "storyboard_read",
    "storyboard_save",
    "video_capabilities",
    "video_generate",
]
D2_M2_INSTRUCTIONS = {
    "mock": "b41fa1a68bf4d333ad721104229d91a49a7c830fdd8dd97fb90528a5704e3eb1",
    "live": "756a9346fe6827924932d67909d05a126929fd2e25cd8ae4fa5743a2791b1d2a",
}


def test_the_frozen_m2_view_still_adds_one_read_only_asset_tool(store):
    tools = mock_tools(store).for_tools_version(4)
    names = sorted(spec["function"]["name"] for spec in tools.specs())
    assert names == sorted([*D2_M2_TOOL_NAMES, "asset_read"])
    features = tools.features["video"]
    assert (features["mode"], features["toolsVersion"], features["rulesVersion"]) == ("mock", 4, 4)
    assert hashlib.sha256(tools.instructions.encode("utf-8")).hexdigest() != D2_M2_INSTRUCTIONS["mock"]
    assert "asset_read" in tools.instructions
    assert "film_assemble" not in tools.instructions


async def test_the_frozen_live_m2_view_extends_its_rules_at_the_same_version(store):
    async with WanAdapter(
        api_key="capture-fixture",
        workspace_id="capture-workspace",
        transport=httpx.MockTransport(lambda _: httpx.Response(500)),
    ) as wan:
        tools = register_video_tools(
            create_project_tools(), JobService(store, [MockVideoAdapter(store), wan]), mode="live"
        )
        current = tools.features["video"]
        assert (current["toolsVersion"], current["rulesVersion"]) == (10, 10)
        frozen = tools.for_tools_version(4)
        features = frozen.features["video"]
        assert (features["toolsVersion"], features["rulesVersion"], features["mode"]) == (4, 4, "live")
        assert len(features["capabilities"]) == 2 == len(current["capabilities"])
        assert "asset_read" in {spec["function"]["name"] for spec in frozen.specs()}
        assert "film_assemble" not in {spec["function"]["name"] for spec in frozen.specs()}
        assert hashlib.sha256(frozen.instructions.encode("utf-8")).hexdigest() != D2_M2_INSTRUCTIONS["live"]
        # The frozen M1-C view still hides every M2 and M3 tool.
        assert sorted(spec["function"]["name"] for spec in tools.for_execution_version(2).specs()) == (
            PRE_M2_TOOL_NAMES
        )


async def test_a_d2_era_m2_run_is_refused_instead_of_resuming_differently(store):
    def interrupted(*_):
        raise RuntimeError("offline interruption")

    model = ScriptedModel(interrupted)
    model.name = "m2-restore-fixture"
    frozen = mock_tools(store).for_tools_version(4)
    runner = AgentRunner(store=store, model=model, tools=frozen, execution_version=3)
    recorded = await runner.run("coffee", "确认模拟能力")
    assert recorded["status"] == "failed" and recorded["resumable"]
    assert recorded["toolFeatures"]["video"]["toolsVersion"] == 4
    assert recorded["contextSignature"] != D2_M2_SIGNATURES["mock-v3-ctx2-ro0"]
    store.transaction(
        lambda draft: draft["runs"][recorded["id"]].update(
            contextSignature=D2_M2_SIGNATURES["mock-v3-ctx2-ro0"]
        )
    )
    current = AgentRunner(store=store, model=model, tools=mock_tools(store))
    with pytest.raises(AppError) as caught:
        await current.resume(recorded["id"])
    assert caught.value.code == "RESUME_CONFIG_CHANGED"


async def test_e1_restores_a_four_four_run_with_its_frozen_tools_and_starts_the_current_view(store):
    def begin(messages, step):
        if step == 0:
            return tool_call("video_capabilities")
        raise RuntimeError("offline interruption")

    model = ScriptedModel(begin)
    model.name = "e1-restore-fixture"
    frozen = mock_tools(store).for_tools_version(4)
    runner = AgentRunner(store=store, model=model, tools=frozen, execution_version=3)
    first = await runner.run("coffee", "先确认模拟能力")
    assert first["status"] == "failed" and first["resumable"]
    assert first["toolFeatures"]["video"]["toolsVersion"] == 4
    home = store.home
    store.close()
    with FileStore.open(home) as reopened:
        resumed_model = ScriptedModel(lambda *_: AIMessage(content="按原 4/4 工具集继续完成"))
        resumed_model.name = "e1-restore-fixture"
        current = AgentRunner(store=reopened, model=resumed_model, tools=mock_tools(reopened))
        assert current.tools.features["video"]["toolsVersion"] == 10
        restored = current.for_record(reopened.snapshot()["runs"][first["id"]])
        restored_names = {spec["function"]["name"] for spec in restored.tools.specs()}
        assert "film_assemble" not in restored_names and "storyboard_save" in restored_names
        final = await current.resume(first["id"])
        assert final["status"] == "completed"
        assert final["toolFeatures"]["video"]["toolsVersion"] == 4
        assert final["answer"] == "按原 4/4 工具集继续完成"
        fresh_model = ScriptedModel(lambda *_: AIMessage(content="新 Run 使用 5/5"))
        fresh_model.name = "e1-restore-fixture"
        fresh = AgentRunner(store=reopened, model=fresh_model, tools=mock_tools(reopened))
        assert fresh.tools.features["video"]["toolsVersion"] == 10
        assert {spec["function"]["name"] for spec in fresh.tools.specs()} >= {"film_assemble", "film_read"}
