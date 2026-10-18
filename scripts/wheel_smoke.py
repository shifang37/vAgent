"""Run with the independently installed wheel's Python, without provider requests."""

import argparse
import asyncio
import hashlib
import json
import os
import struct
import subprocess
import sys
import sysconfig
import tempfile
import zlib
from contextlib import chdir
from datetime import datetime
from pathlib import Path

import httpx
from langchain_core.messages import AIMessage, ToolMessage

import vagent
from vagent.application import ApplicationService
from vagent.config import load_config
from vagent.models import DemoModel
from vagent.storage import FileStore
from vagent.tools import create_project_tools
from vagent.video.contracts import VideoCapabilities, VideoRequest, VideoSpec, validate_video_request
from vagent.video.jobs import JobService
from vagent.video.media_http import MediaHttpClient
from vagent.video.media_worker import MediaWorker
from vagent.video.providers.mock import MockVideoAdapter
from vagent.video.providers.wan import WanAdapter
from vagent.video.tools import register_video_tools
from vagent.video.worker import JobWorker
from vagent.waiting import WAIT_EXECUTION_VERSION, DeferredToolResult, ExternalResourceRef, WaitBinding
from vagent.web import create_app


class StreamingDemo(DemoModel):
    deltas = 0

    async def generate_stream(self, messages, tools, on_delta):
        reply = await self.generate(messages, tools)
        for offset in range(0, len(reply.content), 8):
            self.deltas += 1
            on_delta(reply.content[offset : offset + 8])
        return reply


class VideoDemo:
    name = "wheel-video-fixture"

    def __init__(self):
        self.calls = 0
        self.job_id = None

    async def generate(self, messages, tools):
        self.calls += 1
        assert {"video_capabilities", "video_generate", "job_get", "await_job"} <= {
            item["function"]["name"] for item in tools
        }
        if self.calls == 1:
            name, args = "video_capabilities", {}
        elif self.calls == 2:
            capability = json.loads(messages[-1].content)["data"]["models"][0]
            name = "video_generate"
            args = {
                **{key: capability[key] for key in ("provider", "model", "capabilitiesVersion")},
                "prompt": "wheel offline simulation",
                "spec": capability["specs"][0],
            }
        else:
            assert self.calls == 3
            data = json.loads(messages[-1].content)["data"]
            assert data["status"] == "pending_submit" and not data["mediaAvailable"]
            self.job_id = data["jobId"]
            return AIMessage(content=f"Simulated Job registered: {self.job_id}; no media.")
        return AIMessage(content="", tool_calls=[{"id": name, "name": name, "args": args}])


class WaitDemo:
    name = "wheel-wait-fixture"

    def __init__(self, job_id, *, resuming=False):
        self.job_id, self.calls = job_id, 0
        self.resuming = resuming

    async def generate(self, messages, tools):
        self.calls += 1
        assert "video_generate" not in {item["function"]["name"] for item in tools}
        if self.calls == 1 and not self.resuming:
            return AIMessage(
                content="",
                tool_calls=[
                    {
                        "id": "await",
                        "name": "await_job",
                        "args": {"jobIds": [self.job_id]},
                    }
                ],
            )
        data = json.loads(messages[-1].content)["data"]
        assert self.calls == (1 if self.resuming else 2) and data["status"] == "succeeded"
        assert data["result"]["simulated"] and not data["mediaAvailable"]
        return AIMessage(content="Simulation complete; no playable media.")


async def smoke_video_tools(directory):
    home = Path(directory) / "video-state"
    config = load_config({"VAGENT_HOME": str(home), "VAGENT_VIDEO_MODE": "mock"})
    model = VideoDemo()
    async with ApplicationService.open(config, model=model) as service:
        assert service.configuration()["sources"]["videoMode"] == "environment"
        assert not service.configuration()["editable"]["videoMode"]
        # Freeze the Job clock: the submit still happens immediately, but the queued
        # queries wait for the explicit restart instead of racing the wait run on a
        # slow runner.
        frozen = datetime.fromisoformat(service.video_jobs.timestamp())
        service.video_jobs.clock = lambda: frozen
        first = await service.start("wheel", "register a simulated job", "register")
        registered = await service.wait_for_run(first["id"])
        assert registered["status"] == "completed" and registered["videoMode"] == "mock"
        assert registered["modelSteps"] == 3 and registered["toolCalls"] == 2
        assert len(service.store.snapshot()["jobs"]) == 1
        waiting_model = WaitDemo(model.job_id)
        service.model = waiting_model
        first_wait = await service.start("wheel", "await pending job", "wait", read_only=True)
        await service.task
        pending = service.run_record(first_wait["id"])
        assert pending["status"] == "waiting_external" and pending["resumable"], (
            pending["status"],
            pending["resumable"],
            pending["errorCode"],
        )
        assert pending["executionVersion"] == WAIT_EXECUTION_VERSION
        assert waiting_model.calls == 1 and pending["toolCalls"] == 1
        binding = WaitBinding.model_validate(next(iter(service.store.snapshot()["waits"].values())))
        assert binding.status == "armed" and binding.context.run_id == pending["id"]
        assert binding.context.operation_key not in service.store.snapshot()["operations"]
        # Let the application confirm its first submit before testing a normal exit.
        async with asyncio.timeout(3):
            while not service.job(model.job_id)["providerTaskId"]:
                await asyncio.sleep(0.01)
        original_handle = service.job(model.job_id)["providerTaskId"]
    # The installed application owns Worker + coordinator. Only move the test
    # clock to durable deadlines; never manually call Worker.run_once().
    resumed_model = WaitDemo(model.job_id, resuming=True)
    async with ApplicationService.open(config, model=resumed_model) as service:
        current = datetime.fromisoformat(binding.started_at)
        service.video_jobs.clock = lambda: current
        async with asyncio.timeout(5):
            while (job := service.video_jobs.get(model.job_id)).status != "succeeded":
                if job.next_poll_at and not job.query_started_at:
                    current = max(current, datetime.fromisoformat(job.next_poll_at))
                await asyncio.sleep(0.01)
        assert job.provider_task_id == original_handle
        marker = service.tools.execute(
            "await_job",
            {"jobIds": [model.job_id]},
            store=service.store,
            project_id="wheel",
            operation_key=binding.context.operation_key,
            context=binding.context,
        )
        assert marker == binding.deferred()  # Original interrupt position is preserved.
        finished = await asyncio.wait_for(service.wait_for_run(pending["id"]), 5)
        assert finished["status"] == "completed" and finished["modelSteps"] == 2
        assert finished["id"] == pending["id"] and finished["toolCalls"] == 1
        assert finished["policy"] == pending["policy"] and resumed_model.calls == 1
        assert finished["activeWaitId"] is None
        delivered = WaitBinding.model_validate(service.store.snapshot()["waits"][binding.id])
        assert delivered.status == "delivered"
        assert service.store.snapshot()["operations"][binding.context.operation_key]["result"]["ok"]
        assert await service.wait_coordinator.run_once() is None and resumed_model.calls == 1
        provider = MockVideoAdapter(service.store)
        assert provider.ledger_snapshot()["submitCalls"] == 1
        assert provider.ledger_snapshot()["queryCalls"] == 2
        assert not service.store.snapshot()["artifacts"]
        app = create_app(config)
        app.state.service = service
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:3210"
        ) as client:
            snapshot = (await client.get("/api/sessions/wheel")).json()
            assert snapshot["jobs"][0] == (await client.get(f"/api/jobs/{model.job_id}")).json()
            assert snapshot["jobs"][0]["status"] == "succeeded" and snapshot["run"]["status"] == "completed"
            assert (await client.get("/job-state.js")).status_code == 200
    assert not (home / "instance.lock").exists()
    cli = Path(sysconfig.get_path("scripts")) / ("vagent.exe" if os.name == "nt" else "vagent")
    env = isolated_environment(home)
    listing = json.loads(run_command([str(cli), "jobs", "list", "--session", "wheel"], env=env))
    detail = json.loads(run_command([str(cli), "jobs", "get", model.job_id], env=env))
    assert listing["jobs"] == [detail] and detail["status"] == "succeeded"
    usage = json.loads(run_command([str(cli), "usage", "--run", pending["id"]], env=env))
    assert usage["runs"][0]["usage"]["recordedCallCount"] == 2
    assert not usage["runs"][0]["usage"]["tokenUsageComplete"]
    assert not (home / "instance.lock").exists()
    return {
        "jobId": model.job_id,
        "providerTaskId": original_handle,
        "runId": pending["id"],
        "toolCallId": binding.context.tool_call_id,
        "submitCalls": 1,
        "queryCalls": 2,
        "modelCallsBeforeRestart": 1,
        "modelCallsAfterRestart": finished["modelSteps"],
        "toolCalls": finished["toolCalls"],
        "policyPreserved": finished["policy"] == pending["policy"],
        "jobApiAndSnapshot": True,
        "cliListGetAndUsage": True,
        "lockReleased": True,
    }


class LiveMediaDemo:
    name = "wheel-live-fixture"

    def __init__(self):
        self.calls = 0
        self.job_id = None

    async def generate(self, messages, tools):
        self.calls += 1
        assert {"video_capabilities", "video_generate", "job_get", "await_job"} <= {
            item["function"]["name"] for item in tools
        }
        seen = [message.name for message in messages if isinstance(message, ToolMessage)]
        if "video_capabilities" not in seen:
            return AIMessage(
                content="",
                tool_calls=[{"id": "capabilities", "name": "video_capabilities", "args": {}}],
            )
        if "video_generate" not in seen:
            # The M2 live view publishes the shot entries (wan27-shot-beijing-v2);
            # the frozen single-shot version stays registered for this path.
            args = {
                "provider": "wan",
                "model": "wan2.7-t2v-2026-06-12",
                "capabilitiesVersion": "wan27-t2v-beijing-v1",
                "prompt": "wheel live fixture",
                "spec": {"durationSeconds": 5, "resolution": "720p", "aspectRatio": "16:9"},
            }
            return AIMessage(
                content="", tool_calls=[{"id": "generate", "name": "video_generate", "args": args}]
            )
        if "await_job" not in seen:
            data = json.loads(messages[-1].content)["data"]
            assert data["status"] == "pending_submit" and not data["mediaAvailable"]
            assert data["simulated"] is False
            self.job_id = data["jobId"]
            return AIMessage(
                content="",
                tool_calls=[{"id": "await", "name": "await_job", "args": {"jobIds": [self.job_id]}}],
            )
        data = json.loads(messages[-1].content)["data"]
        assert data["status"] == "succeeded" and data["mediaAvailable"] and len(data["mediaRefs"]) == 1
        return AIMessage(content="Live fixture media delivered locally.")


async def smoke_live_media(directory):
    """Installed live-protocol and media flow: one submit, restart keeps the original Job."""
    import vagent.application as application

    home = Path(directory) / "live-state"
    sample = Path(__file__).resolve().parent.parent / "tests" / "fixtures" / "m1c" / "sample-720p.mp4"
    data = sample.read_bytes()
    video_url = (
        "https://dashscope-result-sh.oss-accelerate.aliyuncs.com/wheel-fixture.mp4?Signature=wheel-fixture"
    )
    fixture = {"submissions": 0, "queries": [], "downloads": 0, "gate": asyncio.Event()}
    clock = [datetime.fromisoformat("2030-01-01T00:00:00+00:00")]

    def upstream(request):
        if request.method == "POST":
            fixture["submissions"] += 1
            return httpx.Response(
                200,
                json={
                    "request_id": "wheel-trace",
                    "output": {"task_id": "wheel-task", "task_status": "PENDING"},
                },
            )
        task = request.url.path.rsplit("/", 1)[-1]
        fixture["queries"].append(task)
        return httpx.Response(
            200,
            json={
                "request_id": "wheel-query",
                "output": {"task_id": task, "task_status": "SUCCEEDED", "video_url": video_url},
            },
        )

    async def media(request):
        fixture["downloads"] += 1
        await fixture["gate"].wait()
        return httpx.Response(
            200, content=data, headers={"Content-Type": "video/mp4", "Content-Length": str(len(data))}
        )

    def advance(service):
        deadlines = []
        for job in service.video_jobs.list():
            if job.next_poll_at and not job.query_started_at:
                deadlines.append(job.next_poll_at)
            download = getattr(job, "download", None)
            if download is not None and download.next_attempt_at:
                deadlines.append(download.next_attempt_at)
        if deadlines:
            clock[0] = max(clock[0], min(datetime.fromisoformat(value) for value in deadlines))

    originals = (
        application.WanAdapter,
        application.MediaHttpClient,
        application.JobService,
        application.JobWorker,
        application.MediaWorker,
    )
    application.WanAdapter = lambda **kwargs: WanAdapter(**kwargs, transport=httpx.MockTransport(upstream))
    application.MediaHttpClient = lambda: MediaHttpClient(transport=httpx.MockTransport(media))
    application.JobService = lambda *args, **kwargs: JobService(*args, **kwargs, clock=lambda: clock[0])
    application.JobWorker = lambda service: JobWorker(service, idle_interval_seconds=0.05)
    application.MediaWorker = lambda media_service, client: MediaWorker(
        media_service, client, idle_interval_seconds=0.05
    )
    try:
        config = load_config(
            {
                "VAGENT_HOME": str(home),
                "VAGENT_VIDEO_MODE": "live",
                "VAGENT_DASHSCOPE_KEY": "wheel-video-fixture-key",
                "VAGENT_DASHSCOPE_WORKSPACE_ID": "wheel-fixture",
            }
        )
        model = LiveMediaDemo()
        async with ApplicationService.open(config, model=model) as service:
            first = await service.start("wheel", "deliver one live fixture video", "live")
            await service.task
            pending = service.run_record(first["id"])
            assert pending["status"] == "waiting_external" and pending["resumable"], (
                pending["status"],
                pending["resumable"],
                pending["errorCode"],
            )
            binding = WaitBinding.model_validate(next(iter(service.store.snapshot()["waits"].values())))
            assert binding.status == "armed" and binding.context.tool_call_id == "await"
            # Liveness guards, not speed assertions: slow CI runners need runway for the
            # download, restart, delivery and resume chain.
            async with asyncio.timeout(30):
                while True:
                    advance(service)
                    job = service.video_jobs.get(model.job_id)
                    if (
                        job.status == "downloading"
                        and job.download.attempts >= 1
                        and fixture["downloads"] >= 1
                    ):
                        break
                    await asyncio.sleep(0.01)
            original = service.video_jobs.get(model.job_id)
            assert original.provider_task_id == "wheel-task" and original.submit_attempts == 1
            assert original.download.phase == "writing" and original.download.window_attempts == 1
            media_id = original.download.media_id
            assert fixture["submissions"] == 1 and fixture["queries"] == ["wheel-task"]
            assert fixture["downloads"] == 1
            assert not (home / "media" / "wheel" / f"{media_id}.mp4").exists()
        assert not (home / "instance.lock").exists()

        resumed_model = LiveMediaDemo()
        async with ApplicationService.open(config, model=resumed_model) as service:
            async with asyncio.timeout(30):
                while True:
                    advance(service)
                    job = service.video_jobs.get(model.job_id)
                    if (
                        job.download.attempts >= 2
                        and job.download.phase == "writing"
                        and fixture["downloads"] >= 2
                    ):
                        break
                    await asyncio.sleep(0.01)
            fixture["gate"].set()
            async with asyncio.timeout(30):
                while service.video_jobs.get(model.job_id).status != "succeeded":
                    await asyncio.sleep(0.01)
            job = service.video_jobs.get(model.job_id)
            assert job.provider_task_id == "wheel-task" and job.submit_attempts == 1
            assert job.download.media_id == media_id and job.download.attempts == 2
            assert job.download.phase == "committed"
            assert fixture["submissions"] == 1 and fixture["queries"] == ["wheel-task"]
            finished = await asyncio.wait_for(service.wait_for_run(pending["id"]), 30)
            assert finished["status"] == "completed" and finished["id"] == pending["id"]
            assert finished["policy"] == pending["policy"] and resumed_model.calls == 1
            delivered = WaitBinding.model_validate(service.store.snapshot()["waits"][binding.id])
            assert delivered.status == "delivered"
            asset = service.store.snapshot()["media"][media_id]
            assert asset["sha256"] == hashlib.sha256(data).hexdigest()
            file = home / asset["relativePath"]
            assert file.read_bytes() == data and not file.with_suffix(".mp4.part").exists()
            app = create_app(config)
            app.state.service = service
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:3210"
            ) as client:
                detail = (await client.get(f"/api/media/{media_id}")).json()
                assert detail["mediaAvailable"] and detail["sizeBytes"] == len(data)
                full = await client.get(detail["contentUrl"])
                assert full.status_code == 200 and full.content == data
                ranged = await client.get(detail["contentUrl"], headers={"Range": "bytes=0-99"})
                assert ranged.status_code == 206 and len(ranged.content) == 100
        assert not (home / "instance.lock").exists()
        cli = Path(sysconfig.get_path("scripts")) / ("vagent.exe" if os.name == "nt" else "vagent")
        env = isolated_environment(home)
        detail = json.loads(run_command([str(cli), "jobs", "get", model.job_id], env=env))
        assert detail["status"] == "succeeded" and detail["mediaAvailable"] is True
        assert detail["localMediaPath"] == f"media/wheel/{media_id}.mp4"
        assert detail["providerTaskId"] == "wheel-task" and detail["submitAttempts"] == 1
        assert "Signature" not in json.dumps(detail)
    finally:
        (
            application.WanAdapter,
            application.MediaHttpClient,
            application.JobService,
            application.JobWorker,
            application.MediaWorker,
        ) = originals
    return {
        "jobId": model.job_id,
        "providerTaskId": "wheel-task",
        "mediaId": media_id,
        "sha256": hashlib.sha256(data).hexdigest(),
        "sizeBytes": len(data),
        "submitCalls": fixture["submissions"],
        "queryCalls": len(fixture["queries"]),
        "downloadAttempts": 2,
        "runId": pending["id"],
        "toolCallId": binding.context.tool_call_id,
        "restartedDuringDownload": True,
        "policyPreserved": finished["policy"] == pending["policy"],
        "mediaApiVerified": True,
        "cliMediaVerified": True,
        "lockReleased": True,
    }


def isolated_environment(home):
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith("VAGENT_") and key not in {"DEEPSEEK_API_KEY", "PYTHONPATH", "PYTHONHOME"}
    }
    env.update(VAGENT_HOME=str(home), VAGENT_VIDEO_MODE="off", PYTHONIOENCODING="utf-8")
    return env


def run_command(command, *, env):
    completed = subprocess.run(
        command, env=env, capture_output=True, text=True, encoding="utf-8", timeout=120
    )
    if completed.returncode:
        raise AssertionError(
            f"Installed command failed ({completed.returncode}):\n{completed.stdout}\n{completed.stderr}"
        )
    return completed.stdout


def smoke_evaluation(directory):
    output = Path(directory) / "m1b-evaluation.json"
    run_command(
        [
            sys.executable,
            "-I",
            str(Path(__file__).with_name("evaluate_m1b.py").resolve()),
            "--home",
            str(Path(directory) / "m1b-state"),
            "--output",
            str(output),
        ],
        env=isolated_environment(Path(directory) / "unused-config"),
    )
    report = json.loads(output.read_text(encoding="utf-8"))
    assert report["status"] == "passed" and report["mode"] == "offline-fixture"
    assert report["totals"]["passedCases"] == report["totals"]["plannedCases"] == 5
    assert report["totals"]["submitCalls"] == 3 and report["totals"]["queryCalls"] == 6
    assert not (Path(report["dataDirectory"]) / "instance.lock").exists()
    return report


def smoke_m1c_evaluation(directory):
    output = Path(directory) / "m1c-evaluation.json"
    run_command(
        [
            sys.executable,
            "-I",
            str(Path(__file__).with_name("evaluate_m1c.py").resolve()),
            "--home",
            str(Path(directory) / "m1c-state"),
            "--output",
            str(output),
        ],
        env=isolated_environment(Path(directory) / "unused-config"),
    )
    report = json.loads(output.read_text(encoding="utf-8"))
    assert report["status"] == "passed" and report["mode"] == "offline-fixture"
    assert report["totals"]["passedCases"] == report["totals"]["plannedCases"] == 7
    assert report["totals"]["submitCalls"] == 6 and report["totals"]["mediaDelivered"] == 3
    assert not (Path(report["dataDirectory"]) / "instance.lock").exists()
    return report


def solid_png(width, height, rgb):
    """A real, decodable PNG so the installed asset surface carries genuine image bytes."""

    def chunk(kind, payload):
        return (
            struct.pack(">I", len(payload))
            + kind
            + payload
            + struct.pack(">I", zlib.crc32(kind + payload) & 0xFFFFFFFF)
        )

    rows = b"".join(b"\x00" + bytes(rgb) * width for _ in range(height))
    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(rows, 6))
        + chunk(b"IEND", b"")
    )


async def smoke_m2_workbench(directory):
    """Installed M2 surface: storyboard tools, client batch registration, HTTP and CLI."""
    home = Path(directory) / "m2-workbench"
    reference = Path(directory) / "reference.png"
    reference.write_bytes(solid_png(1280, 720, (32, 48, 72)))
    config = load_config({"VAGENT_HOME": str(home), "VAGENT_VIDEO_MODE": "mock"})
    async with ApplicationService.open(config) as service:
        service.store.ensure_session("wheel")
        asset = service.import_asset("wheel", reference.read_bytes(), "reference.png")
        saved = service.tools.execute(
            "storyboard_save",
            {
                "title": "wheel 分镜",
                "shots": [
                    {"description": "镜头一：雨夜街道", "framing": "wide", "durationSeconds": 5},
                    {"description": "镜头二：推门入店", "framing": "close", "durationSeconds": 5},
                ],
            },
            store=service.store,
            project_id="wheel",
            operation_key="wheel-storyboard",
        )
        assert saved["ok"], saved
        storyboard_id, version, shots = (
            saved["data"]["storyboardId"],
            saved["data"]["version"],
            saved["data"]["shotIds"],
        )
        registered = service.generate_shots(
            "wheel",
            {
                "clientRequestId": "wheel-batch",
                "storyboardId": storyboard_id,
                "storyboardVersion": version,
                "shotIds": shots,
                "confirm": True,
            },
        )
        assert registered["mode"] == "mock" and len(registered["shots"]) == 2
        assert all(entry["reused"] is False for entry in registered["shots"])
        app = create_app(config)
        app.state.service = service
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:3210"
        ) as client:
            listing = (await client.get("/api/storyboards", params={"projectId": "wheel"})).json()
            assert [item["storyboardId"] for item in listing["storyboards"]] == [storyboard_id]
            assert (await client.get("/api/storyboards", params={"projectId": "other"})).json()[
                "storyboards"
            ] == []
            view = (
                await client.get(f"/api/storyboards/{storyboard_id}", params={"projectId": "wheel"})
            ).json()
            assert [item["id"] for item in view["shots"]] == shots
            # A mock shot never delivers media, so the link is absent for every version.
            assert all(
                item["latest"]["mediaAvailable"] is False and item["latest"]["mediaUrl"] is None
                for item in view["shots"]
            )
            estimate = (
                await client.get(
                    f"/api/storyboards/{storyboard_id}/estimate",
                    params={
                        "projectId": "wheel",
                        "storyboardVersion": version,
                        "shotIds": ",".join(shots),
                    },
                )
            ).json()
            assert estimate["mode"] == "mock" and estimate["simulated"] is True
            assert [row["reused"] for row in estimate["shots"]] == [True, True]
            assert estimate["totalEstimate"] is None and estimate["limits"] is None
            library = (await client.get("/api/assets", params={"projectId": "wheel"})).json()
            assert [entry["assetId"] for entry in library["assets"]] == [asset["assetId"]]
            detail = (await client.get(f"/api/assets/{asset['assetId']}")).json()
            assert detail["available"] is True and "relativePath" not in json.dumps(detail)
            content = await client.get(detail["contentUrl"])
            assert content.status_code == 200 and content.headers["content-type"] == "image/png"
            ranged = await client.get(detail["contentUrl"], headers={"Range": "bytes=0-99"})
            assert ranged.status_code == 206 and len(ranged.content) == 100
            absent = await client.get("/api/assets/44444444-4444-4444-4444-444444444444")
            assert absent.status_code == 404 and absent.json()["error"]["code"] == "ASSET_NOT_FOUND"
            usage = (await client.get("/api/usage", params={"projectId": "wheel"})).json()
            assert usage["jobs"]["total"] == 2 and usage["assets"]["total"] == 1
            assert usage["storyboards"] == {"total": 1, "shotVersions": 2}
    assert not (home / "instance.lock").exists()
    cli = Path(sysconfig.get_path("scripts")) / ("vagent.exe" if os.name == "nt" else "vagent")
    env = {**isolated_environment(home), "VAGENT_VIDEO_MODE": "mock"}
    cli_view = json.loads(run_command([str(cli), "storyboard", "show", "-s", "wheel"], env=env))
    assert cli_view["storyboardId"] == storyboard_id and cli_view["shotCount"] == 2
    assert [item["latest"]["jobId"] for item in cli_view["shots"]] == [
        entry["jobId"] for entry in registered["shots"]
    ]
    listing = json.loads(run_command([str(cli), "assets", "list"], env=env))
    assert [entry["assetId"] for entry in listing["assets"]] == [asset["assetId"]]
    imported = json.loads(run_command([str(cli), "assets", "import", str(reference), "-s", "wheel"], env=env))
    assert imported["failed"] == [] and imported["imported"][0]["deduplicated"] is True
    assert "relativePath" not in json.dumps(imported)
    redone = json.loads(
        run_command([str(cli), "shots", "redo", shots[0], "--request-id", "wheel-redo"], env=env)
    )
    assert redone["shots"][0]["shotId"] == shots[0] and redone["mode"] == "mock"
    replay = json.loads(
        run_command([str(cli), "shots", "redo", shots[0], "--request-id", "wheel-redo"], env=env)
    )
    assert replay == redone
    usage = json.loads(run_command([str(cli), "usage", "--project", "wheel"], env=env))
    assert usage["jobs"]["total"] == 3 and usage["jobs"]["mock"] == 3
    assert usage["storyboards"]["shotVersions"] == 3
    assert usage["assets"] == {"total": 1, "libraryTotal": 1}
    assert usage["cost"]["estimateAmount"] == "0.00"
    assert not (home / "instance.lock").exists()
    return {
        "storyboardId": storyboard_id,
        "shotIds": shots,
        "assetId": asset["assetId"],
        "batchJobIds": [entry["jobId"] for entry in registered["shots"]],
        "redoJobId": redone["shots"][0]["jobId"],
        "storyboardViewAndEstimate": True,
        "assetHttpAndContent": True,
        "cliStoryboardRedoAssetsUsage": True,
        "lockReleased": True,
    }


def smoke_m2_evaluation(directory):
    output = Path(directory) / "m2-evaluation.json"
    run_command(
        [
            sys.executable,
            "-I",
            str(Path(__file__).with_name("evaluate_m2.py").resolve()),
            "--home",
            str(Path(directory) / "m2-state"),
            "--output",
            str(output),
        ],
        env=isolated_environment(Path(directory) / "unused-config"),
    )
    report = json.loads(output.read_text(encoding="utf-8"))
    assert report["status"] == "passed" and report["mode"] == "offline-fixture"
    assert report["totals"]["passedCases"] == report["totals"]["plannedCases"] == 7
    assert report["totals"]["submitCalls"] == 10 and report["totals"]["shotsDelivered"] == 9
    assert not (Path(report["dataDirectory"]) / "instance.lock").exists()
    return report


def seed_keys_only_storyboard(home):
    """One fresh project with a single 5 s t2v shot; nothing else exists yet."""
    home.mkdir(parents=True, exist_ok=True)
    with FileStore.open(home) as store:
        store.ensure_session("keys-only")
        registry = register_video_tools(create_project_tools(), JobService(store, [MockVideoAdapter(store)]))
        saved = registry.execute(
            "storyboard_save",
            {
                "title": "keys-only wheel",
                "shots": [
                    {"description": "雨夜咖啡店，镜头缓慢推近", "framing": "wide", "durationSeconds": 5}
                ],
            },
            store=store,
            project_id="keys-only",
            operation_key="keys-only-storyboard",
        )
        assert saved["ok"], saved
        return {
            "storyboardId": saved["data"]["storyboardId"],
            "version": saved["data"]["version"],
            "shotIds": list(saved["data"]["shotIds"]),
        }


async def smoke_keys_only_live(directory):
    """Installed keys-only path: the settings save probes for free, registration goes live
    hot and the shared host delivers one batch shot with no restart anywhere."""
    import vagent.application as application

    home = Path(directory) / "keys-only-state"
    saved_storyboard = seed_keys_only_storyboard(home)
    sample = Path(__file__).resolve().parent.parent / "tests" / "fixtures" / "m1c" / "sample-720p.mp4"
    data = sample.read_bytes()
    shared_host = "dashscope.aliyuncs.com"
    video_key = "wheel-keys-only-video-key"
    video_url = (
        "https://dashscope-result-sh.oss-accelerate.aliyuncs.com/keys-only-fixture.mp4?Signature=keys-only"
    )
    fixture = {"calls": []}
    clock = [datetime.fromisoformat("2030-01-01T00:00:00+00:00")]

    def upstream(request):
        fixture["calls"].append(request)
        if request.method == "POST":
            return httpx.Response(
                200,
                json={
                    "request_id": "keys-only-submit",
                    "output": {"task_id": "keys-only-task", "task_status": "PENDING"},
                },
            )
        task = request.url.path.rsplit("/", 1)[-1]
        if task == "keys-only-task":
            return httpx.Response(
                200,
                json={
                    "request_id": "keys-only-query",
                    "output": {"task_id": task, "task_status": "SUCCEEDED", "video_url": video_url},
                    "usage": {"duration": 5},
                },
            )
        # The zero-cost save probe: a task ID that cannot exist answers UNKNOWN.
        return httpx.Response(
            200,
            json={
                "request_id": "keys-only-probe",
                "output": {"task_id": task, "task_status": "UNKNOWN"},
            },
        )

    def advance(service):
        """Jump the fixture clock to the earliest persisted deadline, never backwards."""
        deadlines = []
        for job in service.video_jobs.list():
            if job.next_poll_at and not job.query_started_at:
                deadlines.append(job.next_poll_at)
            download = getattr(job, "download", None)
            if download is not None and download.next_attempt_at:
                deadlines.append(download.next_attempt_at)
        if deadlines:
            clock[0] = max(clock[0], min(datetime.fromisoformat(value) for value in deadlines))

    def media(_request):
        return httpx.Response(
            200, content=data, headers={"Content-Type": "video/mp4", "Content-Length": str(len(data))}
        )

    originals = (
        application.WanAdapter,
        application.MediaHttpClient,
        application.JobService,
        application.JobWorker,
        application.MediaWorker,
    )
    application.WanAdapter = lambda **kwargs: WanAdapter(**kwargs, transport=httpx.MockTransport(upstream))
    application.MediaHttpClient = lambda: MediaHttpClient(transport=httpx.MockTransport(media))
    application.JobService = lambda *args, **kwargs: JobService(*args, **kwargs, clock=lambda: clock[0])
    application.JobWorker = lambda service: JobWorker(service, idle_interval_seconds=0.05)
    application.MediaWorker = lambda media_service, client: MediaWorker(
        media_service, client, idle_interval_seconds=0.05
    )
    try:
        # The workbench starts off with no video key at all: only the settings save enables live.
        config = load_config({"VAGENT_HOME": str(home)})
        app = create_app(config, model=DemoModel())
        async with app.router.lifespan_context(app):
            service = app.state.service
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:3210"
            ) as client:
                client.headers["X-CSRF-Token"] = (await client.get("/api/session-token")).json()["token"]
                saved = await client.patch(
                    "/api/config", json={"apiKey": "wheel-keys-only-text", "videoApiKey": video_key}
                )
                assert saved.status_code == 200, saved.text
                settings = saved.json()
                assert video_key not in saved.text and "wheel-keys-only-text" not in saved.text
                assert settings["apiKeyConfigured"] and settings["videoApiKeyConfigured"]
                assert settings["autoEnabledLive"] is True
                assert settings["videoMode"] == "live" and settings["activeVideoMode"] == "live"
                assert settings["videoWorkspaceId"] is None and settings["restartFields"] == []
                probe = settings["videoProbe"]
                assert probe["status"] == "authenticated" and probe["host"] == shared_host
                assert probe["code"] == "PROVIDER_TASK_UNAVAILABLE"
                assert probe["requestId"] == "keys-only-probe"
                # Exactly one read-only GET: no task created, nothing submitted.
                assert len(fixture["calls"]) == 1
                probe_call = fixture["calls"][0]
                assert probe_call.method == "GET" and probe_call.url.host == shared_host
                assert probe_call.url.path == f"/api/v1/tasks/{probe['taskId']}"
                assert probe_call.headers["authorization"] == f"Bearer {video_key}"
                assert not service.store.snapshot()["jobs"]
                # No restart: the saved key registers one live shot on the shared host.
                registered = service.generate_shots(
                    "keys-only",
                    {
                        "clientRequestId": "keys-only-wheel-1",
                        "storyboardId": saved_storyboard["storyboardId"],
                        "storyboardVersion": saved_storyboard["version"],
                        "shotIds": saved_storyboard["shotIds"],
                        "confirm": True,
                    },
                )
                assert registered["mode"] == "live"
                job_id = registered["shots"][0]["jobId"]
                raw = service.store.snapshot()["jobs"][job_id]
                assert raw["contractVersion"] == 5
                assert raw["request"]["endpointProfile"] == "beijing-shared-v1"
                assert "workspaceId" not in json.dumps(raw, ensure_ascii=False)
                async with asyncio.timeout(30):
                    while True:
                        advance(service)
                        if service.video_jobs.get(job_id).status == "succeeded":
                            break
                        await asyncio.sleep(0.01)
                job = service.video_jobs.get(job_id)
                assert job.provider_task_id == "keys-only-task" and job.submit_attempts == 1
                submits = [call for call in fixture["calls"] if call.method == "POST"]
                assert len(submits) == 1 and submits[0].url.host == shared_host
                assert submits[0].headers["authorization"] == f"Bearer {video_key}"
                assert submits[0].url.path == "/api/v1/services/aigc/video-generation/video-synthesis"
                assert {call.url.host for call in fixture["calls"]} == {shared_host}
                queries = [call for call in fixture["calls"] if call.method == "GET"]
                assert [call.url.path.rsplit("/", 1)[-1] for call in queries] == [
                    probe["taskId"],
                    "keys-only-task",
                ]
                assert len(fixture["calls"]) == 3
                body = json.loads(submits[0].content)
                assert body["model"] == "wan2.7-t2v-2026-06-12"
                assert body["input"]["prompt"].startswith("生成单镜头视频。\n")
                assert body["parameters"] == {
                    "resolution": "720P",
                    "ratio": "16:9",
                    "duration": 5,
                    "prompt_extend": False,
                    "watermark": True,
                    "seed": 0,
                }
                media_id = job.download.media_id
                asset = service.store.snapshot()["media"][media_id]
                assert asset["sha256"] == hashlib.sha256(data).hexdigest()
                assert (home / asset["relativePath"]).read_bytes() == data
                detail = (await client.get(f"/api/media/{media_id}")).json()
                assert detail["mediaAvailable"] and detail["sizeBytes"] == len(data)
                full = await client.get(detail["contentUrl"])
                assert full.status_code == 200 and full.content == data
        assert not (home / "instance.lock").exists()
        cli = Path(sysconfig.get_path("scripts")) / ("vagent.exe" if os.name == "nt" else "vagent")
        env = isolated_environment(home)
        detail = json.loads(run_command([str(cli), "jobs", "get", job_id], env=env))
        assert detail["status"] == "succeeded" and detail["mediaAvailable"] is True
        assert detail["providerTaskId"] == "keys-only-task" and detail["submitAttempts"] == 1
        assert "Signature" not in json.dumps(detail)
    finally:
        (
            application.WanAdapter,
            application.MediaHttpClient,
            application.JobService,
            application.JobWorker,
            application.MediaWorker,
        ) = originals
    return {
        "jobId": job_id,
        "providerTaskId": "keys-only-task",
        "mediaId": media_id,
        "sha256": hashlib.sha256(data).hexdigest(),
        "sizeBytes": len(data),
        "probeStatus": probe["status"],
        "probeHost": probe["host"],
        "contractVersion": raw["contractVersion"],
        "endpointProfile": raw["request"]["endpointProfile"],
        "autoEnabledLive": True,
        "restartRequired": False,
        "submitCalls": 1,
        "queryCalls": 1,
        "mediaApiVerified": True,
        "cliMediaVerified": True,
        "lockReleased": True,
    }


async def main(output=None):
    assert Path(vagent.__file__).resolve().is_relative_to(Path(sys.prefix).resolve()), (
        "Use an installed-wheel environment"
    )
    if output:
        output = output.resolve()
        if output.exists():
            raise FileExistsError("Use a new output path to preserve earlier wheel evidence")
    with tempfile.TemporaryDirectory(prefix="vagent-wheel-web-") as directory:
        assert Path(directory).resolve().parent == Path(tempfile.gettempdir()).resolve()
        with chdir(directory):
            env = {"VAGENT_HOME": str(Path(directory) / "state"), "VAGENT_MCP_LOCAL": "1"}
            model = StreamingDemo()
            app = create_app(load_config(env), model=model)
            async with app.router.lifespan_context(app):
                async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:3210"
                ) as client:
                    for path in ["/", "/app.js", "/styles.css", "/mark.svg"]:
                        assert (await client.get(path)).status_code == 200
                    health = (await client.get("/api/health")).json()
                    assert len(health["mcp"][0]["tools"]) == 2
                    assert not health["videoGeneration"]
                    client.headers["X-CSRF-Token"] = (await client.get("/api/session-token")).json()["token"]
                    settings = await client.patch("/api/config", json={"apiKey": "wheel-test-placeholder"})
                    assert settings.status_code == 200 and settings.json()["apiKeyConfigured"]
                    assert "wheel-test-placeholder" not in (await client.get("/api/config")).text
                    assert load_config(env).api_key == "wheel-test-placeholder"
                    response = await client.post(
                        "/api/sessions/wheel/messages",
                        json={"prompt": "offline check", "clientRequestId": "wheel"},
                    )
                    assert response.status_code == 202, response.text
                    await app.state.service.task
                    result = (await client.get("/api/sessions/wheel")).json()
                    assert result["run"]["status"] == "completed" and len(result["artifacts"]) == 1
                    assert model.deltas > 0 and result["draft"] is None
                    assert all(event["type"] != "assistant.delta" for event in result["run"]["events"])
                    assert len(result["run"]["toolTrace"]) == 3
                    assert result["artifacts"][0]["versions"][0]["contentCheck"]["characters"] > 0
                    spec = VideoSpec(duration_seconds=5, resolution="720p", aspect_ratio="16:9")
                    capabilities = VideoCapabilities(
                        provider="mock", model="wheel-fixture", capabilities_version="v1", specs=[spec]
                    )
                    request = VideoRequest(
                        provider="mock",
                        model="wheel-fixture",
                        capabilities_version="v1",
                        prompt="offline",
                        spec=spec,
                        source_refs=[{"artifactId": result["artifacts"][0]["id"], "version": 1}],
                    )
                    validate_video_request(
                        request,
                        capabilities,
                        project_id="wheel",
                        artifacts={a["id"]: a for a in result["artifacts"]},
                    )
                    assert VideoRequest.model_validate_json(request.model_dump_json()) == request
                    deferred = DeferredToolResult(
                        wait_id="wheel-wait",
                        generation=1,
                        resource=ExternalResourceRef(kind="job", id="wheel-job"),
                    )
                    assert deferred.kind == "deferred" and WAIT_EXECUTION_VERSION == 3
                    assert (await client.get("/.env")).status_code == 404
                    bounded = await client.post(
                        "/api/sessions/quality/messages",
                        json={"prompt": "保存brief，正文不超过1字", "clientRequestId": "quality-wheel"},
                    )
                    assert bounded.status_code == 202
                    await app.state.service.task
                    rejected = (await client.get("/api/sessions/quality")).json()
                    assert rejected["project"]["contentLimits"] == {"brief": 1}
                    assert rejected["run"]["errorCode"] == "QUALITY_UNRESOLVED"
                    assert not rejected["artifacts"]
                    assert any(e.get("errorCode") == "CONTENT_LENGTH" for e in rejected["run"]["events"])
            assert not (Path(directory) / "state" / "instance.lock").exists()
            video_report = await smoke_video_tools(directory)
            live_report = await smoke_live_media(directory)
            evaluation_report = smoke_evaluation(directory)
            m1c_report = smoke_m1c_evaluation(directory)
            m2_workbench = await smoke_m2_workbench(directory)
            m2_report = smoke_m2_evaluation(directory)
            keys_only_report = await smoke_keys_only_live(directory)
            report = {
                "status": "passed",
                "packagePath": str(Path(vagent.__file__).resolve()),
                "pythonPrefix": sys.prefix,
                "workingDirectory": str(Path.cwd()),
                "checks": {
                    "installedPackage": True,
                    "webAssets": True,
                    "configRedaction": True,
                    "streaming": model.deltas > 0,
                    "textAndQuality": True,
                    "localMcp": True,
                    "videoRestartAndCli": True,
                    "liveMediaRestartAndCli": True,
                    "fiveCaseM1bSuite": True,
                    "sevenCaseM1cSuite": True,
                    "m2WorkbenchCliAndHttp": True,
                    "sevenCaseM2Suite": True,
                    "keysOnlyLiveSharedHost": True,
                },
                "videoRestart": video_report,
                "liveMedia": live_report,
                "m1bEvaluation": evaluation_report,
                "m1cEvaluation": m1c_report,
                "m2Workbench": m2_workbench,
                "m2Evaluation": m2_report,
                "keysOnlyLive": keys_only_report,
            }
    report["temporaryDataCleaned"] = not Path(directory).exists()
    if output:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(
        "PASS: installed wheel, Web assets, config, streaming, text/quality, MCP, mock video tools, durable wait, original Run restart/automatic continuation, readonly delivery, unchanged budgets, CLI list/get/usage, five M1-B cases, live protocol with local media restart and CLI media evidence, seven M1-C cases, M2 storyboard/asset CLI and HTTP surface, seven M2 cases, keys-only live shared-host fixture with zero-cost save probe and hot registration, cleanup."
    )
    return report


if __name__ == "__main__":
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument("--output", type=Path, help="Save installed-wheel evidence without overwriting a report")
    asyncio.run(main(cli.parse_args().output))
