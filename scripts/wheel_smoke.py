"""Run with the independently installed wheel's Python, without provider requests."""

import asyncio
import sys
import tempfile
from contextlib import chdir
from pathlib import Path

import httpx

import vagent
from vagent.config import Config
from vagent.models import DemoModel
from vagent.web import create_app


async def main():
    assert Path(vagent.__file__).is_relative_to(Path(sys.prefix)), "Use an installed-wheel environment"
    with tempfile.TemporaryDirectory(prefix="vagent-wheel-web-") as directory:
        assert Path(directory).resolve().parent == Path(tempfile.gettempdir()).resolve()
        with chdir(directory):
            app = create_app(
                Config(home=Path(directory) / "state", api_key=None, mcp_local=True), model=DemoModel()
            )
            async with app.router.lifespan_context(app):
                async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:3210"
                ) as client:
                    for path in ["/", "/app.js", "/styles.css", "/mark.svg"]:
                        assert (await client.get(path)).status_code == 200
                    health = (await client.get("/api/health")).json()
                    assert len(health["mcp"][0]["tools"]) == 2
                    client.headers["X-CSRF-Token"] = (await client.get("/api/session-token")).json()["token"]
                    response = await client.post(
                        "/api/sessions/wheel/messages",
                        json={"prompt": "offline check", "clientRequestId": "wheel"},
                    )
                    assert response.status_code == 202, response.text
                    await app.state.service.task
                    result = (await client.get("/api/sessions/wheel")).json()
                    assert result["run"]["status"] == "completed" and len(result["artifacts"]) == 1
                    assert len(result["run"]["toolTrace"]) == 3
                    assert result["artifacts"][0]["versions"][0]["contentCheck"]["characters"] > 0
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
        "PASS: installed wheel, Web assets, API, Runner, artifacts, quality rejection, real MCP discovery, cleanup."
    )


if __name__ == "__main__":
    asyncio.run(main())
