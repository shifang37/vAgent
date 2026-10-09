"""Run with the independently installed wheel's Python, without provider requests."""

import asyncio
import json
import sys
import tempfile
from contextlib import chdir
from datetime import datetime
from pathlib import Path

import httpx
from langchain_core.messages import AIMessage

import vagent
from vagent.application import ApplicationService
from vagent.config import load_config
from vagent.models import DemoModel
from vagent.video.contracts import VideoCapabilities, VideoRequest, VideoSpec, validate_video_request
from vagent.video.jobs import JobService
from vagent.video.providers.mock import MockVideoAdapter
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
                        "args": {"jobId": self.job_id},
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
        registered = await service.runner().run("wheel", "register a simulated job")
        assert registered["status"] == "completed" and registered["videoMode"] == "mock"
        assert registered["modelSteps"] == 3 and registered["toolCalls"] == 2
        assert len(service.store.snapshot()["jobs"]) == 1
        waiting_model = WaitDemo(model.job_id)
        pending = await service.runner(model=waiting_model, read_only=True).run("wheel", "await pending job")
        assert pending["status"] == "waiting_external" and pending["resumable"]
        assert pending["executionVersion"] == 2
        assert waiting_model.calls == 1 and pending["toolCalls"] == 1
        binding = WaitBinding.model_validate(next(iter(service.store.snapshot()["waits"].values())))
        assert binding.status == "armed" and binding.context.run_id == pending["id"]
        assert binding.context.operation_key not in service.store.snapshot()["operations"]
    # B4 will manage the Worker lifecycle. This smoke test explicitly advances
    # B1's Worker with a virtual clock, without sleeping or invoking a model.
    resumed_model = WaitDemo(model.job_id, resuming=True)
    async with ApplicationService.open(config, model=resumed_model) as service:
        current = datetime.fromisoformat(binding.started_at)
        provider = MockVideoAdapter(service.store, clock=lambda: current)
        jobs = JobService(service.store, [provider], clock=lambda: current)
        worker = JobWorker(jobs)
        for expected in ("queued", "running", "succeeded"):
            job = await worker.run_once()
            assert job.id == model.job_id and job.status == expected
            if job.next_poll_at:
                current = datetime.fromisoformat(job.next_poll_at)
        marker = service.tools.execute(
            "await_job",
            {"jobId": model.job_id},
            store=service.store,
            project_id="wheel",
            operation_key=binding.context.operation_key,
            context=binding.context,
        )
        assert marker == binding.deferred()  # Original interrupt position is preserved.
        service.wait_coordinator.notify()
        async with asyncio.timeout(5):
            while service.run_record(pending["id"])["status"] == "waiting_external":
                await asyncio.sleep(0.01)
            while service.run_record(pending["id"])["status"] == "running":
                await asyncio.sleep(0.01)
        finished = service.run_record(pending["id"])
        assert finished["status"] == "completed" and finished["modelSteps"] == 2
        assert finished["id"] == pending["id"] and finished["toolCalls"] == 1
        assert finished["policy"] == pending["policy"] and resumed_model.calls == 1
        assert finished["activeWaitId"] is None
        delivered = WaitBinding.model_validate(service.store.snapshot()["waits"][binding.id])
        assert delivered.status == "delivered"
        assert service.store.snapshot()["operations"][binding.context.operation_key]["result"]["ok"]
        assert await service.wait_coordinator.run_once() is None and resumed_model.calls == 1
        assert provider.ledger_snapshot()["submitCalls"] == 1
        assert provider.ledger_snapshot()["queryCalls"] == 2
        assert not service.store.snapshot()["artifacts"]
    assert not (home / "instance.lock").exists()


async def main():
    assert Path(vagent.__file__).is_relative_to(Path(sys.prefix)), "Use an installed-wheel environment"
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
                    assert deferred.kind == "deferred" and WAIT_EXECUTION_VERSION == 2
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
            await smoke_video_tools(directory)
    print(
        "PASS: installed wheel, Web assets, config, streaming, text/quality, MCP, mock video tools, unique Job, durable wait, original Run restart/automatic continuation, manual Worker, readonly delivery, unchanged budgets, cleanup."
    )


if __name__ == "__main__":
    asyncio.run(main())
