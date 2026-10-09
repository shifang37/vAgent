import asyncio
import json
import os
import subprocess
import sys
from dataclasses import replace

import httpx
import pytest
from conftest import ScriptedModel, tool_call, video_request, video_run
from langchain_core.messages import AIMessage, ToolMessage, messages_from_dict

from vagent.application import ApplicationService
from vagent.config import Config, ConfigUpdate, load_config, update_local_settings
from vagent.context import assert_complete_protocol
from vagent.errors import AppError
from vagent.runner import SYSTEM_PROMPT, AgentRunner
from vagent.tools import Arguments, ToolDefinition, ToolRegistry, create_project_tools
from vagent.usage import summarize_usage
from vagent.video.contracts import VideoCapabilities
from vagent.video.jobs import JobService
from vagent.video.providers.mock import MockVideoAdapter
from vagent.video.tools import register_video_tools
from vagent.waiting import DeferredToolResult, ExternalResourceRef
from vagent.web import create_app


class VideoRegistrationModel:
    name = "video-registration-fixture"

    def __init__(self):
        self.calls = 0
        self.job_id = None
        self.request = None

    async def generate(self, messages, tools):
        self.calls += 1
        names = {item["function"]["name"] for item in tools}
        assert {"video_capabilities", "video_generate", "job_get", "await_job"} <= names
        if self.calls == 1:
            return tool_call("video_capabilities", call_id="capabilities")
        last = json.loads(messages[-1].content)
        if self.calls == 2:
            capability = last["data"]["models"][0]
            self.request = {
                **{key: capability[key] for key in ("provider", "model", "capabilitiesVersion")},
                "prompt": "雨夜咖啡店",
                "spec": capability["specs"][0],
            }
            return tool_call("video_generate", self.request, call_id="generate")
        if self.calls == 3:
            assert last["ok"] and last["data"]["status"] == "pending_submit"
            self.job_id = last["data"]["jobId"]
            return AIMessage(
                content="",
                tool_calls=[
                    {"id": "duplicate", "name": "video_generate", "args": self.request},
                    {"id": "different", "name": "video_generate", "args": {**self.request, "prompt": "晴天"}},
                ],
            )
        if self.calls == 4:
            assert last["error"]["code"] == "JOB_ALREADY_EXISTS" and self.job_id in last["error"]["message"]
            assert json.loads(messages[-2].content)["data"]["jobId"] == self.job_id
            return tool_call("job_get", {"jobId": self.job_id}, call_id="lookup")
        assert self.calls == 5 and last["data"]["status"] == "pending_submit"
        assert last["data"]["simulated"] and not last["data"]["mediaAvailable"]
        return AIMessage(content=f"已登记模拟任务 {self.job_id}，尚无真实媒体。")


async def test_application_model_discovers_capability_and_creates_one_job(tmp_path):
    config = load_config({"VAGENT_HOME": str(tmp_path), "VAGENT_VIDEO_MODE": "mock"})
    model = VideoRegistrationModel()
    async with ApplicationService.open(config, model=model) as service:
        await service.start("coffee", "登记模拟视频", "registration")
        await service.task
        record = service.run_record(service.active_run_id)
        assert record["status"] == "completed", record["answer"]
        assert record["videoMode"] == "mock" and record["executionVersion"] == 1
        assert record["toolFeatures"]["video"]["toolsVersion"] == 1
        assert record["toolFeatures"]["video"]["capabilities"] == [
            item.model_dump(mode="json", by_alias=True) for item in service.video_jobs.capabilities()
        ]
        assert record["modelSteps"] == 5 and record["toolCalls"] == 5
        state = service.store.snapshot()
        assert len(state["jobs"]) == 1 and not state["artifacts"] and not state["waits"]
        job = service.video_jobs.get(model.job_id, project_id="coffee")
        assert job.context.run_id == record["id"] and job.context.model_step == 2
        assert job.context.tool_call_id == "generate" and job.submit_attempts == 0
        assert job.status == "pending_submit"  # B4 owns automatic Worker lifecycle.
        assert model.job_id in record["answer"]
        duplicate = await service.start("coffee", "登记模拟视频", "registration")
        assert duplicate["id"] == record["id"] and model.calls == 5
        assert service.capabilities()["videoSimulated"] and not service.capabilities()["videoMediaAvailable"]


@pytest.mark.parametrize("mode", ["off", "mock"])
async def test_configuration_exposes_startup_mode_and_rejects_hot_switch(tmp_path, mode, monkeypatch):
    env = {"VAGENT_HOME": str(tmp_path), "VAGENT_VIDEO_MODE": mode}
    config = load_config(env)
    assert config.video_mode == mode and config.sources["videoMode"] == "environment"
    app = create_app(config, model=ScriptedModel(lambda *_: AIMessage(content="ok")))
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:3210"
        ) as client:
            settings = (await client.get("/api/config")).json()
            assert settings["videoMode"] == mode and settings["sources"]["videoMode"] == "environment"
            assert settings["editable"]["videoMode"] is False
            health = (await client.get("/api/health")).json()
            assert health["videoGeneration"] == (mode == "mock")
            assert health["videoMode"] == mode and not health["videoMediaAvailable"]
            client.headers["X-CSRF-Token"] = (await client.get("/api/session-token")).json()["token"]
            rejected = await client.patch(
                "/api/config", json={"videoMode": "mock" if mode == "off" else "off"}
            )
            assert rejected.status_code == 422
            saved = await client.patch("/api/config", json={"model": "offline-model"})
            assert saved.status_code == 200 and saved.json()["videoMode"] == mode
            assert "videoMode" not in (tmp_path / "config.yml").read_text(encoding="utf-8")
            monkeypatch.setenv("VAGENT_VIDEO_MODE", "mock" if mode == "off" else "off")
            assert (await client.get("/api/config")).json()["videoMode"] == mode
            assert app.state.service.runner().video_mode == mode
    assert load_config(env).video_mode == mode


@pytest.mark.parametrize("mode", ["real", "MOCK", "mock ", "secret-invalid-mode"])
def test_invalid_modes_fail_before_opening_store_without_echoing_input(tmp_path, mode):
    with pytest.raises(AppError) as caught:
        load_config({"VAGENT_HOME": str(tmp_path), "VAGENT_VIDEO_MODE": mode})
    assert caught.value.code == "INVALID_VIDEO_MODE" and "secret" not in str(caught.value)
    assert not (tmp_path / "state.json").exists()


def test_default_mode_and_configuration_source_survive_other_settings_changes(tmp_path):
    config = load_config({"VAGENT_HOME": str(tmp_path)})
    assert config.video_mode == "off" and config.sources["videoMode"] == "default"
    configured = replace(config, video_mode="mock", sources={**config.sources, "videoMode": "provided"})
    changed = update_local_settings(configured, ConfigUpdate(model="offline-model"))
    assert changed.video_mode == "mock" and changed.sources["videoMode"] == "provided"


def test_cli_configuration_reports_video_mode_and_source(tmp_path):
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith("VAGENT_") and key != "DEEPSEEK_API_KEY"
    }
    env.update(VAGENT_HOME=str(tmp_path), VAGENT_VIDEO_MODE="mock", PYTHONIOENCODING="utf-8")
    result = subprocess.run(
        [sys.executable, "-m", "vagent", "config", "show"],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=20,
        check=True,
    )
    config = json.loads(result.stdout)
    assert config["video_mode"] == "mock" and config["sources"]["videoMode"] == "environment"
    assert not config["api_key_configured"] and not (tmp_path / "state.json").exists()


class UntouchedCache:
    def __init__(self):
        self.accesses = []

    def key(self, **kwargs):
        self.accesses.append("key")
        return "stale-key"

    async def get(self, key):
        self.accesses.append("get")
        return AIMessage(content="stale job status"), "hit"

    async def put(self, key, answer):
        self.accesses.append("put")
        return "stored"


@pytest.mark.parametrize("read_only", [False, True])
async def test_video_visibility_bypasses_cache_before_first_model_and_preserves_token_usage(
    tmp_path, read_only
):
    cache = UntouchedCache()

    def respond(messages, step):
        result = tool_call("video_capabilities") if step == 0 else AIMessage(content="模拟能力已确认")
        result.response_metadata = {
            "token_usage": {
                "prompt_tokens": 20,
                "completion_tokens": 3,
                "prompt_cache_hit_tokens": 12,
                "prompt_cache_miss_tokens": 8,
            }
        }
        return result

    model = ScriptedModel(respond)
    model.cache_config = {"adapter": "offline-cache-fixture"}
    async with ApplicationService.open(
        Config(home=tmp_path, api_key=None, video_mode="mock"), model=model
    ) as service:
        service.cache = cache
        result = await service.runner(read_only=read_only).run("coffee", "查看视频能力")
        assert result["status"] == "completed" and result["modelSteps"] == 2
        assert not cache.accesses and not result["answerCache"]
        assert not service.capabilities()["answerCacheEnabled"]
        usage = summarize_usage(result)
        assert usage["cacheHitTokens"] == 24 and usage["cacheMissTokens"] == 16
        assert usage["cacheUsageComplete"] and usage["observedInputTokens"] == 40


async def test_read_only_runner_rejects_generate_even_when_model_requests_hidden_tool(tmp_path):
    model = ScriptedModel(
        lambda messages, step: (
            tool_call("video_generate", {"prompt": "forbidden"})
            if step == 0
            else AIMessage(content="只读模式不能登记")
        )
    )
    async with ApplicationService.open(
        Config(home=tmp_path, api_key=None, video_mode="mock"), model=model
    ) as service:
        runner = service.runner(read_only=True)
        result = await runner.run("coffee", "登记任务")
        assert result["status"] == "completed" and not service.store.snapshot()["jobs"]
        assert json.loads(model.inputs[1][-1].content)["error"]["code"] == "UNKNOWN_TOOL"
        assert "不能创建视频 Job" in runner.system_prompt


async def test_old_off_application_run_resumes_under_new_mock_startup(tmp_path):
    def fail_after_read(messages, step):
        if step == 0:
            return tool_call("project_read")
        raise RuntimeError("offline interrupted request")

    off = Config(home=tmp_path, api_key=None)
    async with ApplicationService.open(off, model=ScriptedModel(fail_after_read)) as service:
        await service.start("coffee", "读取项目", "original")
        await service.task
        record = service.run_record(service.active_run_id)
        operations = service.store.snapshot()["operations"]
        assert record["resumable"] and record["videoMode"] == "off"
        assert service.runner().system_prompt == SYSTEM_PROMPT

        # Pre-B2 execution records omit both fields.
        def legacy(draft):
            draft["runs"][record["id"]].pop("videoMode")
            draft["runs"][record["id"]].pop("toolFeatures")

        service.store.transaction(legacy)
    model = ScriptedModel(lambda *_: AIMessage(content="已按原配置继续"))
    async with ApplicationService.open(replace(off, video_mode="mock"), model=model) as service:
        assert service.runner().video_mode == "mock"
        await service.resume(record["id"])
        await service.task
        final = service.run_record(record["id"])
        assert final["status"] == "completed" and final["modelSteps"] == 3 and final["toolCalls"] == 1
        assert final["contextSignature"] == record["contextSignature"] and final["policy"] == record["policy"]
        assert service.store.snapshot()["operations"] == operations and not service.store.snapshot()["jobs"]
        assert model.calls == 1 and "视频生成权限" in model.inputs[0][0].content


@pytest.mark.parametrize("change", ["off", "capabilities_version", "specs", "rules", "schema"])
async def test_mock_resume_requires_original_mode_capabilities_rules_and_tools(store, change, monkeypatch):
    adapter = MockVideoAdapter(store)
    registry = register_video_tools(create_project_tools(), JobService(store, [adapter]))

    def fail(*_):
        raise RuntimeError("offline model failure")

    original = await AgentRunner(store=store, model=ScriptedModel(fail), tools=registry).run(
        "coffee", "查询能力"
    )
    assert original["resumable"]
    if change == "off":
        changed = create_project_tools()
    else:
        capability = adapter.capabilities().model_dump(mode="json", by_alias=True)
        if change == "capabilities_version":
            capability["capabilitiesVersion"] = "v2"
        if change == "specs":
            capability["specs"] = capability["specs"][:1]
        if change == "rules":
            monkeypatch.setattr("vagent.video.tools.VIDEO_RULES", "Changed simulation rules")
        changed = register_video_tools(
            create_project_tools(),
            JobService(
                store, [MockVideoAdapter(store, capabilities=VideoCapabilities.model_validate(capability))]
            ),
        )
        if change == "schema":
            changed.register(ToolDefinition("extra", "changed tools", Arguments, "read", lambda *_: {}))
    model = ScriptedModel(lambda *_: AIMessage(content="must not run"))
    before = store.snapshot()
    with pytest.raises(AppError) as error:
        await AgentRunner(store=store, model=model, tools=changed).resume(original["id"])
    assert error.value.code == "RESUME_CONFIG_CHANGED"
    assert not model.calls and store.snapshot() == before


async def test_mock_batch_replay_keeps_original_context_job_and_tool_budget(store):
    adapter = MockVideoAdapter(store)
    registry = register_video_tools(create_project_tools(), JobService(store, [adapter]))
    cancelled = asyncio.Event()

    def respond(messages, step):
        if step == 0:
            return tool_call("video_capabilities", call_id="caps")
        return AIMessage(
            content="",
            tool_calls=[
                {"id": "generate", "name": "video_generate", "args": video_request(adapter)},
                {"id": "after-generation", "name": "video_capabilities", "args": {}},
            ],
        )

    def cancel(event):
        if event["type"] == "tool.started" and event["callId"] == "after-generation":
            cancelled.set()

    first = await AgentRunner(store=store, model=ScriptedModel(respond), tools=registry, on_event=cancel).run(
        "coffee", "登记视频", cancelled=cancelled
    )
    assert first["status"] == "cancelled" and first["resumable"]
    jobs = store.snapshot()["jobs"]
    assert len(jobs) == 1 and first["toolCalls"] == first["modelSteps"] == 2
    model = ScriptedModel(lambda *_: AIMessage(content="已登记模拟任务"))
    final = await AgentRunner(store=store, model=model, tools=registry).resume(first["id"])
    assert final["status"] == "completed" and final["modelSteps"] == final["toolCalls"] == 3
    assert final["policy"] == first["policy"] and store.snapshot()["jobs"] == jobs
    assert adapter.ledger_snapshot()["submitCalls"] == 0 and model.calls == 1


async def test_deferred_boundary_stops_batch_without_false_success_or_next_model(store):
    context = video_run(store, "owner")
    adapter = MockVideoAdapter(store)
    service = JobService(store, [adapter])
    job_id = service.generate(video_request(adapter), context=context)["data"]["jobId"]
    registry = register_video_tools(create_project_tools(), service)
    model = ScriptedModel(
        lambda *_: AIMessage(
            content="",
            tool_calls=[
                {
                    "id": "before",
                    "name": "artifact_save",
                    "args": {"kind": "brief", "title": "before", "content": "saved"},
                },
                {"id": "await", "name": "await_job", "args": {"jobId": job_id}},
                {
                    "id": "after",
                    "name": "artifact_save",
                    "args": {"kind": "brief", "title": "after", "content": "must not save"},
                },
            ],
        )
    )
    events = []
    runner = AgentRunner(store=store, model=model, tools=registry, on_event=events.append)
    result = await runner.run("coffee", "保存然后等待")
    assert result["status"] == "failed" and result["errorCode"] == "EXTERNAL_WAIT_UNAVAILABLE"
    assert not result["resumable"] and result["toolCalls"] == 2 and model.calls == 1
    assert len(store.snapshot()["artifacts"]) == 1 and len(store.snapshot()["waits"]) == 1
    assert all(not key.endswith((":await", ":after")) for key in store.snapshot()["operations"])
    assert [event["callId"] for event in events if event["type"] == "tool.completed"] == ["before"]
    assert [event["callId"] for event in events if event["type"] == "tool.deferred"] == ["await"]
    messages = messages_from_dict(result["messages"])
    assert_complete_protocol(messages)
    tool_results = [json.loads(message.content) for message in messages if isinstance(message, ToolMessage)]
    assert [item["ok"] for item in tool_results] == [True, False, False]
    with pytest.raises(AppError) as error:
        await runner.resume(result["id"])
    assert error.value.code == "NOT_RESUMABLE"


async def test_runner_deferred_contract_does_not_depend_on_video_provider(store):
    observed = []

    def defer(args, current_store, context):
        observed.append(context)
        return DeferredToolResult(
            wait_id="report-wait", generation=1, resource=ExternalResourceRef(kind="report", id="report-id")
        )

    registry = ToolRegistry().register(
        ToolDefinition("report", "deferred report", Arguments, "read", context_execute=defer)
    )
    model = ScriptedModel(lambda *_: tool_call("report", call_id="original-call"))
    result = await AgentRunner(store=store, model=model, tools=registry).run("coffee", "report")
    assert result["errorCode"] == "EXTERNAL_WAIT_UNAVAILABLE" and model.calls == 1
    assert observed[0].run_id == result["id"] and observed[0].tool_call_id == "original-call"
    assert result["videoMode"] == "off" and not store.snapshot()["jobs"]


async def test_mock_video_tools_compose_with_real_local_read_only_mcp(tmp_path):
    def respond(messages, step):
        if step == 0:
            return AIMessage(
                content="",
                tool_calls=[
                    {"id": "caps", "name": "video_capabilities", "args": {}},
                    {
                        "id": "frames",
                        "name": "mcp_video_frame_budget",
                        "args": {"seconds": 5, "fps": 24, "aspect_ratio": "16:9"},
                    },
                ],
            )
        assert json.loads(messages[-2].content)["data"]["mode"] == "mock"
        assert json.loads(messages[-1].content)["ok"]
        return AIMessage(content="已读取模拟能力和帧数计算")

    config = Config(home=tmp_path, api_key=None, video_mode="mock", mcp_local=True)
    async with ApplicationService.open(config, model=ScriptedModel(respond)) as service:
        assert len(service.tools.identities) == 2
        assert all(name.startswith("mcp_") for name in service.tools.identities)
        result = await service.runner(read_only=True).run("coffee", "读取视频能力与帧数")
        assert result["status"] == "completed" and result["toolCalls"] == 2
        assert not service.store.snapshot()["jobs"]
