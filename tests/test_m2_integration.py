"""Offline multi-shot storyboard trajectories through the real Agent loop."""

import json

from conftest import ScriptedModel, tool_call
from langchain_core.messages import AIMessage

from vagent.application import ApplicationService
from vagent.config import load_config
from vagent.runner import AgentRunner
from vagent.tools import create_project_tools
from vagent.video.jobs import JobService
from vagent.video.providers.mock import MockVideoAdapter
from vagent.video.tools import register_video_tools


def shot(description, **changes):
    return {"description": description, "framing": "wide", "durationSeconds": 5, "mode": "t2v", **changes}


async def test_mock_multi_shot_storyboard_trajectory(store):
    registry = register_video_tools(create_project_tools(), JobService(store, [MockVideoAdapter(store)]))
    saved = {}

    def respond(messages, step):
        if step == 0:
            return tool_call(
                "storyboard_save",
                {
                    "title": "雨夜咖啡店三镜头分镜",
                    "shots": [
                        shot("远景：雨夜街道，霓虹倒影", framing="wide"),
                        shot("中景：推门入店，店员抬头", framing="medium", cameraMovement="跟拍推进"),
                        shot("特写：杯口热气与拉花", framing="close", durationSeconds=3),
                    ],
                },
                call_id="save-draft",
            )
        if step == 1:
            result = json.loads(messages[-1].content)
            assert result["ok"], result
            saved.update(result["data"])
            return tool_call("storyboard_read", {}, call_id="read-draft")
        if step == 2:
            result = json.loads(messages[-1].content)
            assert result["ok"] and result["data"]["currentVersion"] == 1
            assert [item["id"] for item in result["data"]["shots"]] == saved["shotIds"]
            return tool_call(
                "storyboard_save",
                {
                    "storyboardId": saved["storyboardId"],
                    "expectedVersion": 1,
                    "title": "雨夜咖啡店三镜头分镜",
                    "shots": [
                        {"id": saved["shotIds"][0], **shot("远景：雨夜街道，霓虹与伞影", framing="wide")},
                        {
                            "id": saved["shotIds"][1],
                            **shot("中景：推门入店，店员抬头", framing="medium", cameraMovement="跟拍推进"),
                        },
                        {
                            "id": saved["shotIds"][2],
                            **shot("特写：杯口热气与拉花", framing="close", durationSeconds=3),
                        },
                    ],
                },
                call_id="save-revision",
            )
        result = json.loads(messages[-1].content)
        assert result["ok"] and result["data"]["version"] == 2, result
        return AIMessage(content=f"分镜已保存：{saved['storyboardId']}，版本 2，共 3 个镜头。")

    model = ScriptedModel(respond)
    result = await AgentRunner(store=store, model=model, tools=registry).run(
        "coffee", "把这段需求做成三镜头分镜，保存后给我镜头清单"
    )
    assert result["status"] == "completed", result["answer"]
    assert result["executionVersion"] == 3 and result["videoMode"] == "mock"
    assert result["toolFeatures"]["video"]["toolsVersion"] == 10
    assert result["toolCalls"] == 3 and model.calls == 4
    state = store.snapshot()
    assert len(state["storyboards"]) == 1 and not state["jobs"]
    record = next(iter(state["storyboards"].values()))
    assert record["currentVersion"] == 2 and len(record["versions"]) == 2 and record["revision"] == 1
    assert record["versions"][0]["shots"][0]["description"] == "远景：雨夜街道，霓虹倒影"
    assert [item["id"] for item in record["versions"][1]["shots"]] == saved["shotIds"]
    assert saved["storyboardId"] in result["answer"]


async def test_mock_application_exposes_storyboard_tools_end_to_end(tmp_path):
    config = load_config({"VAGENT_HOME": str(tmp_path), "VAGENT_VIDEO_MODE": "mock"})
    model = ScriptedModel(
        lambda messages, step: (
            tool_call(
                "storyboard_save",
                {"title": "应用级分镜", "shots": [shot("单镜头：雨夜咖啡店")]},
                call_id="app-save",
            )
            if step == 0
            else AIMessage(content="分镜已保存，未生成视频。")
        )
    )
    async with ApplicationService.open(config, model=model) as service:
        await service.start("coffee", "保存一个单镜头分镜", "storyboard-run")
        await service.task
        record = service.run_record(service.active_run_id)
        assert record["status"] == "completed", record["answer"]
        assert record["toolFeatures"]["video"]["toolsVersion"] == 10
        names = {tool["name"] for tool in service.capabilities()["tools"]}
        assert {"storyboard_read", "storyboard_save"}.issubset(names)
        assert len(service.store.snapshot()["storyboards"]) == 1


async def test_off_mode_hides_storyboard_tools(tmp_path):
    config = load_config({"VAGENT_HOME": str(tmp_path)})
    model = ScriptedModel(lambda *_: AIMessage(content="ok"))
    async with ApplicationService.open(config, model=model) as service:
        names = {tool["name"] for tool in service.capabilities()["tools"]}
        assert not {"storyboard_read", "storyboard_save"}.intersection(names)
