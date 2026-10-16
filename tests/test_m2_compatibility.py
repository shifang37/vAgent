"""Pre-M2 signatures captured at 92a4a87 restore byte-identically under M2 code."""

import httpx
import pytest
from conftest import ScriptedModel, tool_call
from langchain_core.messages import AIMessage

from vagent.context import ContextBuilder
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


def test_current_mock_toolset_adds_storyboard_tools_at_version_four(store):
    tools = mock_tools(store)
    names = sorted(spec["function"]["name"] for spec in tools.specs())
    assert {"storyboard_read", "storyboard_save"}.issubset(set(names))
    features = tools.features["video"]
    assert features["mode"] == "mock" and features["toolsVersion"] == 4 and features["rulesVersion"] == 4
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
