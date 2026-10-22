"""M3 E2: tools/rules 6/6 beside the frozen 5/5 and 4/4 tool signatures."""

import httpx
from conftest import ScriptedModel, tool_call
from langchain_core.messages import AIMessage
from shot_support import mock_tools

from vagent.runner import AgentRunner
from vagent.storage import FileStore
from vagent.tools import create_project_tools
from vagent.video.contracts import VideoIntentV2, VideoIntentV6
from vagent.video.jobs import JobService
from vagent.video.providers.mock import MockVideoAdapter
from vagent.video.providers.wan import WanAdapter
from vagent.video.tools import register_video_tools


def live_registry(store):
    import asyncio

    async def build():
        async with WanAdapter(
            api_key="capture-fixture",
            workspace_id="capture-workspace",
            transport=httpx.MockTransport(lambda _: httpx.Response(500)),
        ) as wan:
            return register_video_tools(
                create_project_tools(), JobService(store, [MockVideoAdapter(store), wan]), mode="live"
            )

    return asyncio.run(build())


def test_the_current_live_toolset_is_nine_nine_and_keeps_every_frozen_view(store):
    tools = live_registry(store)
    features = tools.features["video"]
    assert (features["toolsVersion"], features["rulesVersion"]) == (9, 9)
    assert {cap["provider"] for cap in features["capabilities"]} == {"wan"}
    for version, names in ((5, True), (4, False)):
        frozen = tools.for_tools_version(version)
        assert frozen.features["video"]["toolsVersion"] == version
        schema = next(
            spec["function"]["parameters"]
            for spec in frozen.specs()
            if spec["function"]["name"] == "video_generate"
        )
        assert set(schema["properties"]) == set(VideoIntentV2.model_json_schema(by_alias=True)["properties"])
        tool_names = {spec["function"]["name"] for spec in frozen.specs()}
        assert ("film_assemble" in tool_names) is names
        assert {"storyboard_save", "storyboard_read", "asset_read", "shot_generate"} <= tool_names
    current_schema = next(
        spec["function"]["parameters"]
        for spec in tools.specs()
        if spec["function"]["name"] == "video_generate"
    )
    assert set(current_schema["properties"]) == set(
        VideoIntentV6.model_json_schema(by_alias=True)["properties"]
    )
    assert VideoIntentV6.model_json_schema(by_alias=True)["properties"]["model"]["maxLength"] == 256


def mock_registry(store):
    """Register the current mock toolset without creating a Run or session."""
    return register_video_tools(create_project_tools(), JobService(store, [MockVideoAdapter(store)]))


async def test_a_five_five_run_restores_with_its_frozen_features_under_nine_nine(store):
    def begin(messages, step):
        if step == 0:
            return tool_call("video_capabilities")
        raise RuntimeError("offline interruption")

    model = ScriptedModel(begin)
    model.name = "e2-restore-fixture"
    frozen = mock_tools(store)[0].for_tools_version(5)
    runner = AgentRunner(store=store, model=model, tools=frozen, execution_version=3)
    first = await runner.run("coffee", "先确认模拟能力")
    assert first["status"] == "failed" and first["resumable"]
    assert first["toolFeatures"]["video"]["toolsVersion"] == 5
    assert first["toolFeatures"]["video"]["rulesVersion"] == 5
    home = store.home
    store.close()
    with FileStore.open(home) as reopened:
        resumed_model = ScriptedModel(lambda *_: AIMessage(content="按原 5/5 工具集继续完成"))
        resumed_model.name = "e2-restore-fixture"
        current = AgentRunner(store=reopened, model=resumed_model, tools=mock_registry(reopened))
        assert current.tools.features["video"]["toolsVersion"] == 9
        restored = current.for_record(reopened.snapshot()["runs"][first["id"]])
        assert restored.tools.features["video"]["toolsVersion"] == 5
        final = await current.resume(first["id"])
        assert final["status"] == "completed"
        assert final["toolFeatures"]["video"]["toolsVersion"] == 5
        assert final["answer"] == "按原 5/5 工具集继续完成"
        fresh_model = ScriptedModel(lambda *_: AIMessage(content="新 Run 使用 9/9"))
        fresh_model.name = "e2-restore-fixture"
        fresh = AgentRunner(store=reopened, model=fresh_model, tools=mock_registry(reopened))
        assert fresh.tools.features["video"]["toolsVersion"] == 9
