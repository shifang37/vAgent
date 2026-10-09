"""Run with the independently installed wheel's Python, without provider requests."""

import asyncio
import sys
import tempfile
from contextlib import chdir
from pathlib import Path

import httpx

import vagent
from vagent.config import load_config
from vagent.models import DemoModel
from vagent.video.contracts import VideoCapabilities, VideoRequest, VideoSpec, validate_video_request
from vagent.waiting import WAIT_EXECUTION_VERSION, DeferredToolResult, ExternalResourceRef
from vagent.web import create_app


class StreamingDemo(DemoModel):
    deltas = 0

    async def generate_stream(self, messages, tools, on_delta):
        reply = await self.generate(messages, tools)
        for offset in range(0, len(reply.content), 8):
            self.deltas += 1
            on_delta(reply.content[offset : offset + 8])
        return reply


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
    print(
        "PASS: installed wheel, Web assets, local config, streaming Runner, artifacts, B0 contracts (video disabled), quality rejection, real MCP discovery, cleanup."
    )


if __name__ == "__main__":
    asyncio.run(main())
