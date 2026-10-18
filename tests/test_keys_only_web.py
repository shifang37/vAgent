"""K2 keys-only track: settings saves, the zero-cost probe and hot effect.

The offline chain drives the real Web application against a controlled Wan
upstream: saving a video key probes without creating a task, and saved settings
reach new registrations with no restart. No credentials and no provider submit
happen here (the paid sample is K3's separate approval).
"""

import asyncio
import json
from contextlib import asynccontextmanager
from types import SimpleNamespace

import httpx
import pytest
from conftest import ScriptedModel
from langchain_core.messages import AIMessage
from shot_support import generation_args, mock_tools, save_storyboard, shot
from wan_support import VIDEO_URL, provider_response

from vagent.config import load_config
from vagent.video.contracts import JobV3, JobV5, parse_job
from vagent.video.jobs import JobService
from vagent.video.media_http import MediaHttpClient
from vagent.video.providers.wan import WanAdapter
from vagent.video.worker import JobWorker
from vagent.web import create_app

SHARED_HOST = "dashscope.aliyuncs.com"
DEDICATED_HOST = "test-space.cn-beijing.maas.aliyuncs.com"
VIDEO_KEY = "video-keys-only-secret"


@pytest.fixture
def keys_runtime(monkeypatch, video_clock):
    """A controlled Wan upstream: probes answer UNKNOWN, tasks answer SUCCEEDED."""
    import vagent.application as application

    runtime = SimpleNamespace(calls=[], providers=[], auth="ok")

    def handler(request):
        runtime.calls.append(request)
        if runtime.auth == "fail":
            return httpx.Response(401, json={"code": "InvalidApiKey", "message": "invalid"})
        if runtime.auth == "network":
            raise httpx.ConnectError("offline")
        task_id = request.url.path.rsplit("/", 1)[-1] if request.method == "GET" else "keys-live-1"
        if request.method == "GET" and not task_id.startswith("keys-live-"):
            # K2 probe: a task ID that cannot exist answers UNKNOWN.
            return httpx.Response(
                200,
                json={"request_id": "probe-trace", "output": {"task_id": task_id, "task_status": "UNKNOWN"}},
            )
        if request.method == "POST":
            return httpx.Response(200, json=provider_response("PENDING", task_id=task_id))
        return httpx.Response(
            200,
            json={
                **provider_response("SUCCEEDED", task_id=task_id, video_url=VIDEO_URL),
                "usage": {"duration": 5},
            },
        )

    def provider(**kwargs):
        instance = WanAdapter(**kwargs, transport=httpx.MockTransport(handler), clock=video_clock)
        runtime.providers.append(instance)
        return instance

    monkeypatch.setattr(application, "WanAdapter", provider)

    async def hold_media(_request):
        await asyncio.Event().wait()

    monkeypatch.setattr(
        application, "MediaHttpClient", lambda: MediaHttpClient(transport=httpx.MockTransport(hold_media))
    )
    monkeypatch.setattr(
        application, "JobService", lambda *args, **kwargs: JobService(*args, **kwargs, clock=video_clock)
    )
    monkeypatch.setattr(
        application, "JobWorker", lambda service: JobWorker(service, idle_interval_seconds=0.005)
    )
    return runtime


@asynccontextmanager
async def keys_app(tmp_path, *, env=None):
    """A fresh keys-only Web process; keys_runtime must be active around it."""
    config = load_config({"VAGENT_HOME": str(tmp_path / "state"), **(env or {})})
    model = ScriptedModel(lambda *_: AIMessage(content="unused"))
    app = create_app(config, model=model)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:3210"
        ) as client:
            client.headers["X-CSRF-Token"] = (await client.get("/api/session-token")).json()["token"]
            yield client, app.state.service


async def test_saving_a_video_key_probes_without_creating_a_task(tmp_path, keys_runtime):
    async with keys_app(tmp_path) as (client, service):
        saved = await client.patch("/api/config", json={"videoApiKey": VIDEO_KEY})
        assert saved.status_code == 200 and VIDEO_KEY not in saved.text
        settings = saved.json()
        # Decision 2: the first saved key switches the mode off to live once.
        assert settings["autoEnabledLive"] is True
        assert settings["videoMode"] == "live" and settings["activeVideoMode"] == "live"
        assert not settings["restartRequired"] and settings["restartFields"] == []
        probe = settings["videoProbe"]
        assert probe["status"] == "authenticated" and probe["host"] == SHARED_HOST
        assert probe["code"] == "PROVIDER_TASK_UNAVAILABLE" and probe["requestId"] == "probe-trace"
        assert probe["taskId"] and probe["checkedAt"]
        # Exactly one read-only GET: no submit, no Job, no task.
        assert len(keys_runtime.calls) == 1
        call = keys_runtime.calls[0]
        assert call.method == "GET" and call.url.host == SHARED_HOST
        assert call.url.path == f"/api/v1/tasks/{probe['taskId']}"
        assert call.headers["authorization"] == f"Bearer {VIDEO_KEY}"
        assert not service.store.snapshot()["jobs"]
        assert (tmp_path / "state" / "config.yml").exists()


async def test_workspace_save_probes_the_dedicated_host(tmp_path, keys_runtime):
    async with keys_app(tmp_path) as (client, _):
        saved = await client.patch(
            "/api/config",
            json={"videoApiKey": VIDEO_KEY, "videoWorkspaceId": "test-space", "videoMode": "live"},
        )
        probe = saved.json()["videoProbe"]
        assert probe["status"] == "authenticated" and probe["host"] == DEDICATED_HOST
        assert keys_runtime.calls[-1].url.host == DEDICATED_HOST


async def test_probe_auth_failure_never_blocks_the_save(tmp_path, keys_runtime):
    keys_runtime.auth = "fail"
    async with keys_app(tmp_path) as (client, service):
        saved = await client.patch("/api/config", json={"videoApiKey": VIDEO_KEY})
        assert saved.status_code == 200
        settings = saved.json()
        assert settings["videoApiKeyConfigured"] and settings["videoMode"] == "live"
        probe = settings["videoProbe"]
        assert probe["status"] == "rejected" and probe["code"] == "PROVIDER_AUTH_FAILED"
        assert not service.store.snapshot()["jobs"]
        assert len(keys_runtime.calls) == 1 and keys_runtime.calls[0].method == "GET"
    # The rejected key stays saved; the probe state resets with the process.
    async with keys_app(tmp_path) as (client, _):
        settings = (await client.get("/api/config")).json()
        assert settings["videoApiKeyConfigured"] and settings["videoProbe"] is None
        assert len(keys_runtime.calls) == 1


async def test_probe_network_failure_is_inconclusive(tmp_path, keys_runtime):
    keys_runtime.auth = "network"
    async with keys_app(tmp_path) as (client, _):
        saved = await client.patch("/api/config", json={"videoApiKey": VIDEO_KEY})
        assert saved.status_code == 200
        probe = saved.json()["videoProbe"]
        assert probe["status"] == "inconclusive" and probe["code"] == "QUERY_NETWORK"
        assert saved.json()["videoApiKeyConfigured"]


async def test_saves_without_credential_fields_never_probe(tmp_path, keys_runtime):
    async with keys_app(tmp_path) as (client, service):
        await client.patch("/api/config", json={"videoApiKey": VIDEO_KEY})
        assert len(keys_runtime.calls) == 1
        again = (await client.patch("/api/config", json={"videoMaxJobCost": "4"})).json()
        assert len(keys_runtime.calls) == 1
        assert again["videoMaxJobCost"] == "4.00"
        assert again["videoProbe"]["status"] == "authenticated"
        cleared = (await client.patch("/api/config", json={"clearVideoApiKey": True})).json()
        assert len(keys_runtime.calls) == 1
        assert not cleared["videoApiKeyConfigured"] and cleared["videoProbe"] is None
        assert not service.store.snapshot()["jobs"]


async def test_first_key_switches_to_live_exactly_once(tmp_path, keys_runtime):
    async with keys_app(tmp_path) as (client, _):
        first = (await client.patch("/api/config", json={"videoApiKey": VIDEO_KEY})).json()
        assert first["autoEnabledLive"] is True and first["videoMode"] == "live"
        choice = (await client.patch("/api/config", json={"videoMode": "mock"})).json()
        assert choice["videoMode"] == "mock" and choice["autoEnabledLive"] is False
        rotated = (await client.patch("/api/config", json={"videoApiKey": "rotated-secret"})).json()
        assert rotated["videoMode"] == "mock" and rotated["autoEnabledLive"] is False
        explicit = (await client.patch("/api/config", json={"videoMode": "off"})).json()
        assert explicit["videoMode"] == "off"
        normal = (await client.patch("/api/config", json={"videoApiKey": "third-secret"})).json()
        assert normal["videoMode"] == "off" and normal["autoEnabledLive"] is False


async def test_explicit_off_in_the_same_save_is_respected(tmp_path, keys_runtime):
    async with keys_app(tmp_path) as (client, _):
        saved = (
            await client.patch("/api/config", json={"videoApiKey": VIDEO_KEY, "videoMode": "off"})
        ).json()
        assert saved["videoMode"] == "off" and saved["autoEnabledLive"] is False
        assert saved["videoProbe"]["status"] == "authenticated"


async def test_clearing_then_reconfiguring_a_key_switches_again(tmp_path, keys_runtime):
    async with keys_app(tmp_path) as (client, _):
        await client.patch("/api/config", json={"videoApiKey": VIDEO_KEY})
        await client.patch("/api/config", json={"videoMode": "off"})
        await client.patch("/api/config", json={"clearVideoApiKey": True})
        again = (await client.patch("/api/config", json={"videoApiKey": VIDEO_KEY})).json()
        assert again["autoEnabledLive"] is True and again["videoMode"] == "live"


async def test_environment_provided_mode_never_auto_switches(tmp_path, keys_runtime):
    async with keys_app(tmp_path, env={"VAGENT_VIDEO_MODE": "off"}) as (client, _):
        saved = (await client.patch("/api/config", json={"videoApiKey": VIDEO_KEY})).json()
        assert saved["autoEnabledLive"] is False and saved["videoMode"] == "off"
        assert saved["sources"]["videoMode"] == "environment"
        assert saved["videoProbe"]["status"] == "authenticated"


async def test_saved_video_settings_serve_new_registrations_without_restart(tmp_path, keys_runtime):
    async with keys_app(tmp_path) as (client, service):
        registry, _, _, _ = mock_tools(service.store)
        saved_storyboard = save_storyboard(registry, service.store, [shot("镜头一：雨夜咖啡店")])
        assert not service.video_jobs.configuration_status()["configured"]
        await client.patch("/api/config", json={"videoApiKey": VIDEO_KEY, "videoMode": "live"})
        # No restart: the same process now registers and submits on the shared host.
        result = service.generate_shots(
            "coffee", {"clientRequestId": "k2-batch-1", **generation_args(saved_storyboard)}
        )
        jobs = [parse_job(raw) for raw in service.store.snapshot()["jobs"].values()]
        assert len(jobs) == 1 and isinstance(jobs[0], JobV5) and result["mode"] == "live"
        assert jobs[0].request.endpoint_profile == "beijing-shared-v1"
        assert "workspaceId" not in json.dumps(jobs[0].model_dump(mode="json", by_alias=True))
        worker = JobWorker(service.video_jobs)
        queued = await worker.run_once()
        assert queued.status == "queued" and queued.provider_task_id == "keys-live-1"
        submitted = [call for call in keys_runtime.calls if call.method == "POST"]
        assert len(submitted) == 1 and submitted[0].url.host == SHARED_HOST
        assert submitted[0].headers["authorization"] == f"Bearer {VIDEO_KEY}"
        assert len(keys_runtime.calls) == 2  # One probe GET plus this submit.


async def test_workspace_add_and_remove_switches_endpoints_hot(tmp_path, keys_runtime):
    async with keys_app(tmp_path) as (client, service):
        registry, _, _, _ = mock_tools(service.store)
        saved_storyboard = save_storyboard(registry, service.store, [shot("镜头一")])
        await client.patch("/api/config", json={"videoApiKey": VIDEO_KEY})
        added = (await client.patch("/api/config", json={"videoWorkspaceId": "test-space"})).json()
        assert added["videoWorkspaceId"] == "test-space"
        assert added["videoProbe"]["host"] == DEDICATED_HOST
        first = service.generate_shots(
            "coffee", {"clientRequestId": "k2-dedicated", **generation_args(saved_storyboard)}
        )
        first_job = parse_job(service.store.snapshot()["jobs"][first["shots"][0]["jobId"]])
        assert isinstance(first_job, JobV3) and first_job.request.workspace_id == "test-space"
        assert first_job.request.endpoint_profile == "beijing-workspace-v1"
        worker = JobWorker(service.video_jobs)
        queued = await worker.run_once()
        assert queued.provider_task_id == "keys-live-1"
        # Removing the workspace returns new registrations to the shared host.
        removed = (await client.patch("/api/config", json={"videoWorkspaceId": ""})).json()
        assert removed["videoWorkspaceId"] is None
        assert removed["videoProbe"]["host"] == SHARED_HOST
        second = service.generate_shots(
            "coffee", {"clientRequestId": "k2-shared", **generation_args(saved_storyboard)}
        )
        second_job = parse_job(service.store.snapshot()["jobs"][second["shots"][0]["jobId"]])
        assert isinstance(second_job, JobV5)
        assert second_job.request.endpoint_profile == "beijing-shared-v1"
        probes = [call for call in keys_runtime.calls if call.method == "GET"]
        assert [call.url.host for call in probes] == [SHARED_HOST, DEDICATED_HOST, SHARED_HOST]
        submits = [call for call in keys_runtime.calls if call.method == "POST"]
        assert [call.url.host for call in submits] == [DEDICATED_HOST]
