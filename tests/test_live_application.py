import asyncio
import json
import os
import subprocess
import sys
from dataclasses import replace
from types import SimpleNamespace

import httpx
import pytest
from conftest import ScriptedModel, tool_call
from job_support import eventually
from langchain_core.messages import AIMessage, ToolMessage, messages_from_dict
from wan_support import VIDEO_URL, live_arguments, live_config

from vagent.application import ApplicationService
from vagent.config import VIDEO_SETTINGS, ConfigUpdate, load_config, update_local_settings
from vagent.errors import AppError
from vagent.video.jobs import JobService
from vagent.video.media_http import MediaHttpClient
from vagent.video.providers.wan import WanAdapter
from vagent.video.worker import JobWorker
from vagent.web import create_app


@pytest.fixture
def live_runtime(monkeypatch, video_clock):
    import vagent.application as application

    runtime = SimpleNamespace(
        calls=[], providers=[], status="SUCCEEDED", query_status=None, clock=video_clock
    )

    def handler(request):
        runtime.calls.append(request)
        status = runtime.query_status if request.method == "GET" and runtime.query_status else runtime.status
        return httpx.Response(
            200,
            json={
                "request_id": "offline-trace",
                "output": {"task_id": "wan-original", "task_status": status, "video_url": VIDEO_URL},
            },
        )

    def provider(**kwargs):
        instance = WanAdapter(**kwargs, transport=httpx.MockTransport(handler), clock=video_clock)
        runtime.providers.append(instance)
        return instance

    monkeypatch.setattr(application, "WanAdapter", provider)

    async def hold_media(_request):
        # C1 scenarios intentionally stop before local delivery. Never let the C2
        # Worker access public hosts while those cloud-only assertions execute.
        await asyncio.Event().wait()

    monkeypatch.setattr(
        application,
        "MediaHttpClient",
        lambda: MediaHttpClient(
            transport=httpx.MockTransport(hold_media),
        ),
    )
    monkeypatch.setattr(
        application, "JobService", lambda *args, **kwargs: JobService(*args, **kwargs, clock=video_clock)
    )
    monkeypatch.setattr(
        application, "JobWorker", lambda service: JobWorker(service, idle_interval_seconds=0.005)
    )
    return runtime


def test_video_settings_persist_with_separate_keys_and_environment_precedence(tmp_path):
    env = {"VAGENT_HOME": str(tmp_path)}
    original = load_config(env)
    updated = update_local_settings(
        original,
        ConfigUpdate(
            apiKey="text-secret",
            videoApiKey="video-secret",
            videoMode="live",
            videoWorkspaceId="test-space",
            videoModel="wan2.7-t2v-2026-06-12",
            videoRegion="cn-beijing",
            videoMaxJobCost="3",
        ),
    )
    assert load_config(env) == updated
    assert updated.api_key == "text-secret" and updated.video_api_key == "video-secret"
    assert updated.video_max_job_cost == "3.00" and "secret" not in repr(updated)
    assert updated.sources["videoApiKey"] == "local" and updated.sources["videoWorkspaceId"] == "local"
    overridden = load_config(
        {
            **env,
            "VAGENT_DASHSCOPE_KEY": "env-video",
            "VAGENT_DASHSCOPE_WORKSPACE_ID": "env-space",
            "VAGENT_VIDEO_MODE": "off",
            "VAGENT_VIDEO_MAX_JOB_COST": "0",
        }
    )
    assert overridden.api_key == "text-secret" and overridden.video_api_key == "env-video"
    assert overridden.video_mode == "off" and overridden.video_max_job_cost == "0.00"
    before = (tmp_path / "config.yml").read_bytes()
    for request in (
        {"videoApiKey": "new"},
        {"clearVideoApiKey": True},
        {"videoWorkspaceId": "other"},
        {"videoMode": "live"},
        {"videoMaxJobCost": "3.00"},
    ):
        with pytest.raises(AppError) as caught:
            update_local_settings(overridden, ConfigUpdate(**request))
        assert caught.value.code == "CONFIG_OVERRIDE"
        assert (tmp_path / "config.yml").read_bytes() == before
    cleared = update_local_settings(updated, ConfigUpdate(clearVideoApiKey=True))
    assert cleared.video_api_key is None and cleared.api_key == "text-secret"
    assert load_config(env) == cleared


@pytest.mark.parametrize(
    "field,value",
    [
        ("VAGENT_DASHSCOPE_WORKSPACE_ID", "UpperCase"),
        ("VAGENT_DASHSCOPE_WORKSPACE_ID", "host.example"),
        ("VAGENT_DASHSCOPE_WORKSPACE_ID", "x:443"),
        ("VAGENT_DASHSCOPE_WORKSPACE_ID", "x/path"),
        ("VAGENT_DASHSCOPE_WORKSPACE_ID", "-prefix"),
        ("VAGENT_DASHSCOPE_WORKSPACE_ID", "尾部"),
        ("VAGENT_VIDEO_PROVIDER", "other"),
        ("VAGENT_VIDEO_MODEL", "wan2.7-t2v"),
        ("VAGENT_VIDEO_REGION", "cn-shanghai"),
        ("VAGENT_VIDEO_MAX_JOB_COST", "NaN"),
        ("VAGENT_VIDEO_MAX_JOB_COST", "Infinity"),
        ("VAGENT_VIDEO_MAX_JOB_COST", "3e0"),
        ("VAGENT_VIDEO_MAX_JOB_COST", "-1"),
        ("VAGENT_VIDEO_MAX_JOB_COST", "3.001"),
    ],
)
def test_invalid_video_environment_has_safe_errors(tmp_path, field, value):
    with pytest.raises(AppError) as caught:
        load_config({"VAGENT_HOME": str(tmp_path), field: value, "VAGENT_DASHSCOPE_KEY": "private-secret"})
    assert caught.value.code == "VIDEO_CONFIG_INVALID" and "private-secret" not in str(caught.value)


async def test_config_api_saves_without_http_and_video_changes_apply_after_restart(tmp_path, live_runtime):
    config = load_config({"VAGENT_HOME": str(tmp_path)})
    model = ScriptedModel(lambda *_: AIMessage(content="unused"))
    app = create_app(config, model=model)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:3210"
        ) as client:
            client.headers["X-CSRF-Token"] = (await client.get("/api/session-token")).json()["token"]
            saved = await client.patch(
                "/api/config",
                json={
                    "videoMode": "live",
                    "videoApiKey": "video-api-secret",
                    "videoWorkspaceId": "test-space",
                    "videoMaxJobCost": "3",
                },
            )
            assert saved.status_code == 200 and "video-api-secret" not in saved.text
            settings = saved.json()
            assert settings["videoMode"] == "live" and settings["activeVideoMode"] == "off"
            assert settings["restartRequired"] and set(settings["restartFields"]) == {
                "videoMode",
                "videoApiKey",
                "videoWorkspaceId",
            }
            assert settings["videoApiKeyConfigured"] and settings["videoPermissionStatus"] == "unverified"
            assert settings["videoMaxJobCost"] == "3.00"
            assert app.state.service.runner().video_mode == "off"
            assert not app.state.service.video_jobs.config.video_api_key
            assert not live_runtime.calls and model.calls == 0
            assert not app.state.service.store.snapshot()["jobs"]
    restarted = create_app(load_config({"VAGENT_HOME": str(tmp_path)}), model=model)
    async with restarted.router.lifespan_context(restarted):
        service = restarted.state.service
        settings = service.configuration()
        assert not settings["restartRequired"] and settings["activeVideoMode"] == "live"
        assert (
            service.runner().video_mode == "live"
            and service.video_jobs.config.video_api_key == "video-api-secret"
        )
        assert not service.configuration()["apiKeyConfigured"]
        cleared = service.save_configuration(ConfigUpdate(clearVideoApiKey=True))
        assert not cleared["videoApiKeyConfigured"] and cleared["restartRequired"]
        assert service.video_jobs.config.video_api_key == "video-api-secret"
        assert "video-api-secret" not in str(service.store.snapshot())
        assert not live_runtime.calls
    assert all(provider.client.is_closed for provider in live_runtime.providers)


@pytest.mark.parametrize(
    "payload",
    [
        {"videoApiKey": None},
        {"videoApiKey": ""},
        {"videoApiKey": "secret\nheader"},
        {"videoApiKey": "secret", "clearVideoApiKey": True},
        {"clearVideoApiKey": "true"},
        {"videoWorkspaceId": "space.example"},
        {"videoRegion": "cn-shanghai"},
        {"videoMaxJobCost": 3},
        {"videoMaxJobCost": True},
        {"videoMaxJobCost": "1e2"},
        {"videoUrl": "secret"},
    ],
)
async def test_config_write_rejects_bad_video_fields_without_echo(tmp_path, live_runtime, payload):
    app = create_app(load_config({"VAGENT_HOME": str(tmp_path)}))
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:3210"
        ) as client:
            client.headers["X-CSRF-Token"] = (await client.get("/api/session-token")).json()["token"]
            response = await client.patch("/api/config", json=payload)
            assert response.status_code == 422 and "secret" not in response.text
            assert not live_runtime.calls and not (tmp_path / "config.yml").exists()


async def test_live_application_model_tools_replay_http_views_and_client_closure(tmp_path, live_runtime):
    remembered = {}

    def respond(messages, step):
        if step == 0:
            return tool_call("video_capabilities", call_id="capabilities")
        previous = json.loads(messages[-1].content)
        if step == 1:
            assert previous["data"]["mode"] == "live" and not previous["data"]["simulated"]
            assert [model["provider"] for model in previous["data"]["models"]] == ["wan"]
            assert previous["data"]["configuration"]["configured"]
            return tool_call("video_generate", live_arguments(), call_id="generate")
        if step == 2:
            remembered["jobId"] = previous["data"]["jobId"]
            assert previous["data"]["status"] == "pending_submit" and not previous["data"]["mediaAvailable"]
            return tool_call("video_generate", live_arguments(), call_id="replay")
        if step == 3:
            assert previous["data"]["jobId"] == remembered["jobId"]
            return tool_call("job_get", {"jobId": remembered["jobId"]}, call_id="lookup")
        assert not previous["data"]["simulated"] and previous["data"]["result"] is None
        return AIMessage(content=f"原任务 {remembered['jobId']} 已登记，等待本地交付。")

    model = ScriptedModel(respond)
    config = live_config(tmp_path, api_key="text-api-secret")
    app = create_app(config, model=model)
    async with app.router.lifespan_context(app):
        service = app.state.service
        queue = service.subscribe("coffee")
        run = await service.start("coffee", "生成一个视频", "original-request")
        await service.task
        record = service.run_record(run["id"])
        assert record["status"] == "completed" and record["modelSteps"] == 5 and record["toolCalls"] == 4
        assert (
            record["videoMode"] == "live"
            and record["executionVersion"] == 2
            and record["contextVersion"] == 2
        )
        assert record["toolFeatures"]["video"]["toolsVersion"] == 3
        assert record["toolFeatures"]["video"]["rulesVersion"] == 3
        await eventually(lambda: service.job(remembered["jobId"])["status"] == "downloading")
        job = service.job(remembered["jobId"])
        assert job["providerTaskId"] == "wan-original" and job["submitAttempts"] == 1
        assert not job["simulated"] and not job["mediaAvailable"]
        assert len(live_runtime.calls) == 1 and len(service.store.snapshot()["jobs"]) == 1
        assert (await service.start("coffee", "生成一个视频", "original-request"))["id"] == run["id"]
        assert model.calls == 5
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:3210"
        ) as client:
            responses = [
                await client.get(route)
                for route in (
                    "/api/health",
                    "/api/config",
                    "/api/jobs",
                    f"/api/jobs/{job['jobId']}",
                    "/api/sessions/coffee",
                    f"/api/runs/{run['id']}",
                )
            ]
            assert all(response.status_code == 200 for response in responses)
            for response in responses:
                for secret in (
                    "text-api-secret",
                    "video-test-secret",
                    "private-signature",
                    "videoUrl",
                    "providerOutput",
                ):
                    assert secret not in response.text
        updates = [queue.get_nowait() for _ in range(queue.qsize())]
        assert any(event.get("job", {}).get("status") == "downloading" for event in updates)
        for secret in ("video-test-secret", "private-signature", "test-workspace", "videoUrl"):
            assert secret not in str(updates) + str(model.inputs) + str(
                service.store.snapshot()["operations"]
            )
            assert secret.encode() not in (tmp_path / "checkpoints.sqlite").read_bytes()
        assert not service.http_client.is_closed
        worker, coordinator, text_client = (
            service.job_worker._task,
            service.wait_coordinator._task,
            service.http_client,
        )
    assert worker.done() and coordinator.done() and text_client.is_closed
    assert all(provider.client.is_closed for provider in live_runtime.providers)
    assert not (tmp_path / "instance.lock").exists()


async def test_live_missing_key_remains_usable_for_text_and_returns_local_generation_error(
    tmp_path, live_runtime
):
    def respond(messages, step):
        if step == 0:
            return tool_call("video_generate", live_arguments())
        assert json.loads(messages[-1].content)["error"]["code"] == "VIDEO_KEY_MISSING"
        return AIMessage(content="视频 Key 尚未配置，可以继续整理文本。")

    model = ScriptedModel(respond)
    async with ApplicationService.open(live_config(tmp_path, video_api_key=None), model=model) as service:
        run = await service.start("coffee", "生成视频", "no-video-key")
        result = await service.wait_for_run(run["id"])
        assert result["status"] == "completed" and not service.store.snapshot()["jobs"]
        assert not live_runtime.calls and model.calls == 2


@pytest.mark.parametrize("original_mode", ["off", "mock"])
@pytest.mark.parametrize("mcp", [False, True])
async def test_legacy_run_resumes_under_live_startup_with_original_tools_and_budget(
    tmp_path, live_runtime, original_mode, mcp
):
    def interrupted(messages, step):
        if step == 0:
            return tool_call(
                "artifact_save",
                {"kind": "brief", "title": "旧产物", "content": "原文"},
                call_id="original-save",
            )
        raise RuntimeError("offline interruption")

    config = live_config(tmp_path, video_mode=original_mode, mcp_local=mcp)
    async with ApplicationService.open(config, model=ScriptedModel(interrupted)) as service:
        run = await service.start("coffee", "保存文本", "original")
        before = await service.wait_for_run(run["id"])
        assert before["resumable"] and before["modelSteps"] == 2
        artifacts, operations = service.store.snapshot()["artifacts"], service.store.snapshot()["operations"]
        signature = before["contextSignature"]
    model = ScriptedModel(lambda *_: AIMessage(content="原产物已保存。"))
    async with ApplicationService.open(replace(config, video_mode="live"), model=model) as service:
        assert service.runner().video_mode == "live"
        assert [cap["provider"] for cap in service.runner().tools.features["video"]["capabilities"]] == [
            "wan"
        ]
        await service.resume(run["id"])
        after = await service.wait_for_run(run["id"])
        assert after["status"] == "completed" and after["contextSignature"] == signature
        assert after["videoMode"] == original_mode and after["toolFeatures"] == before["toolFeatures"]
        assert after["modelSteps"] == 3 and after["toolCalls"] == 1 and after["policy"] == before["policy"]
        assert (
            service.store.snapshot()["artifacts"] == artifacts
            and service.store.snapshot()["operations"] == operations
        )
        assert not live_runtime.calls


async def test_live_wait_survives_restart_until_local_delivery_and_stop_never_resubmits(
    tmp_path, live_runtime
):
    remembered = {}

    def respond(messages, step):
        if step == 0:
            return tool_call("video_generate", live_arguments(), call_id="generate")
        if step == 1:
            remembered["jobId"] = json.loads(messages[-1].content)["data"]["jobId"]
            return tool_call("await_job", {"jobId": remembered["jobId"]}, call_id="original-await")
        raise AssertionError("Cloud success must not wake a model before local media delivery")

    model = ScriptedModel(respond)
    config = live_config(tmp_path)
    async with ApplicationService.open(config, model=model) as service:
        run = await service.start("coffee", "等待真实视频", "wait")
        await service.task
        await eventually(lambda: service.job(remembered["jobId"])["status"] == "downloading")
        record = service.run_record(run["id"])
        assert record["status"] == "waiting_external" and record["modelSteps"] == 2
        original_wait = service.store.snapshot()["waits"][record["activeWaitId"]]
        original_job = service.video_jobs.get(remembered["jobId"])
        assert original_wait["context"]["toolCallId"] == "original-await"
    # Startup mode can differ; the original live view remains isolated and stable.
    async with ApplicationService.open(replace(config, video_mode="off"), model=model) as service:
        await service.wait_coordinator.run_once()
        restored = service.run_record(run["id"])
        assert restored["status"] == "waiting_external" and restored["modelSteps"] == 2
        assert restored["policy"] == record["policy"] and restored["activeSeconds"] == record["activeSeconds"]
        recovered_job = service.video_jobs.get(original_job.id)
        assert recovered_job.request == original_job.request
        assert recovered_job.provider_task_id == original_job.provider_task_id
        assert recovered_job.provider_output == original_job.provider_output
        assert recovered_job.download.media_id == original_job.download.media_id
        assert recovered_job.download.attempts >= original_job.download.attempts
        assert recovered_job.submit_attempts == 1 and recovered_job.status == "downloading"
        assert service.store.snapshot()["waits"][original_wait["id"]]["context"] == original_wait["context"]
        assert len(live_runtime.calls) == 1 and model.calls == 2
        service.stop(run["id"])
        stopped = service.run_record(run["id"])
        assert stopped["status"] == "cancelled"
        messages = [
            message for message in messages_from_dict(stopped["messages"]) if isinstance(message, ToolMessage)
        ]
        assert [message.tool_call_id for message in messages] == ["generate", "original-await"]
        await service.wait_coordinator.run_once()
        assert model.calls == 2 and len(live_runtime.calls) == 1


@pytest.mark.parametrize("changes", [{"video_api_key": None}, {"video_workspace_id": "another-space"}])
async def test_live_resume_missing_original_scope_keeps_checkpoint_identity(tmp_path, live_runtime, changes):
    def fail(messages, step):
        if step == 0:
            return tool_call("video_generate", live_arguments())
        raise RuntimeError("offline model interruption")

    config = live_config(tmp_path)
    async with ApplicationService.open(config, model=ScriptedModel(fail)) as service:
        run = await service.start("coffee", "准备视频", "resume")
        record = await service.wait_for_run(run["id"])
        assert record["resumable"]
        await eventually(lambda: service.jobs() and service.jobs()[0]["status"] == "downloading")
    model = ScriptedModel(lambda *_: AIMessage(content="must not run"))
    async with ApplicationService.open(replace(config, **changes), model=model) as service:
        before = service.store.snapshot()
        with pytest.raises(AppError) as caught:
            await service.resume(run["id"])
        assert caught.value.code == "RESUME_CONFIG_CHANGED"
        assert service.store.snapshot() == before and model.calls == 0 and len(live_runtime.calls) == 1


async def test_dynamic_keys_budget_and_mode_do_not_change_live_tool_signature(tmp_path, live_runtime):
    model = ScriptedModel(lambda *_: AIMessage(content="ok"))
    config = live_config(tmp_path)
    async with ApplicationService.open(config, model=model) as service:
        signature = service.runner().context_signature()
        readonly = service.runner(read_only=True)
        names = {spec["function"]["name"] for spec in readonly.tools.specs()}
        assert "video_generate" not in names and "await_job" in names
        assert readonly.tools.bypass_answer_cache and service.tools.bypass_answer_cache
        features = service.tools.features
    async with ApplicationService.open(
        replace(config, video_api_key="rotated-secret", video_max_job_cost="0"), model=model
    ) as service:
        assert service.runner().context_signature() == signature and service.tools.features == features
        assert not service.video_jobs.configuration_status()["configured"]
        assert not live_runtime.calls


def test_cli_config_redacts_both_keys(tmp_path):
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith("VAGENT_") and key != "DEEPSEEK_API_KEY"
    }
    env.update(
        VAGENT_HOME=str(tmp_path),
        DEEPSEEK_API_KEY="cli-text-secret",
        VAGENT_DASHSCOPE_KEY="cli-video-secret",
        VAGENT_DASHSCOPE_WORKSPACE_ID="test-space",
        VAGENT_VIDEO_MODE="live",
        PYTHONIOENCODING="utf-8",
    )
    result = subprocess.run(
        [sys.executable, "-m", "vagent", "config", "show"],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=15,
    )
    assert result.returncode == 0, result.stderr
    assert (
        "cli-text-secret" not in result.stdout + result.stderr
        and "cli-video-secret" not in result.stdout + result.stderr
    )
    data = json.loads(result.stdout)
    assert data["api_key_configured"] and data["video_api_key_configured"]
    assert data["video_mode"] == "live" and data["video_permission_status"] == "unverified"
    assert all(name in data["sources"] for name in VIDEO_SETTINGS)
