"""M6 E3 HTTP surface: local render, render progress and derived sidecars."""

import hashlib
from contextlib import asynccontextmanager

import httpx
import pytest
from render_support import renderable_storyboard
from wan_support import live_config

from vagent.errors import AppError
from vagent.storage import FileStore
from vagent.video.assembler import bundled_engine
from vagent.web import create_app


def engine_available():
    try:
        bundled_engine()
        return True
    except AppError:
        return False


pytestmark = pytest.mark.skipif(not engine_available(), reason="bundled FFmpeg is unavailable")


@asynccontextmanager
async def render_app(config):
    app = create_app(config)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:3210"
        ) as client:
            client.headers["X-CSRF-Token"] = (await client.get("/api/session-token")).json()["token"]
            yield app.state.service, client


async def seed(home):
    with FileStore.open(home) as store:
        fixture = await renderable_storyboard(store)
    return fixture


async def test_film_render_endpoint_writes_a_playable_film_and_sidecar(tmp_path):
    home = tmp_path / "state"
    fixture = await seed(home)
    async with render_app(live_config(home)) as (_service, client):
        body = {
            "clientRequestId": "render-1",
            "scope": "storyboard",
            "storyboardId": fixture["storyboardId"],
            "trackId": fixture["trackId"],
            "profile": "720p-h264-aac-v1",
            "subtitleMode": "both",
            "transitions": [
                {
                    "afterShotId": fixture["shotIds"][0],
                    "type": "crossfade",
                    "durationMs": 500,
                }
            ],
            "titleCards": [{"text": "片头", "startMs": 100, "durationMs": 1000}],
        }
        rendered = await client.post("/api/sessions/coffee/films/render", json=body)
        assert rendered.status_code == 202, rendered.text
        data = rendered.json()
        assert data["filmVersion"] == 1 and data["status"] == "succeeded"
        assert data["film"]["latest"]["durationSeconds"] == 19.5
        render = data["film"]["latest"]["render"]
        assert render["subtitle"]["retimed"] is True
        assert render["transitions"][0]["type"] == "crossfade"
        # Progress is readable by task, target and film.
        by_task = await client.get(f"/api/renders/{data['renderId']}?projectId=coffee")
        assert by_task.status_code == 200
        assert by_task.json()["render"]["status"] == "succeeded"
        by_target = await client.get(f"/api/renders?projectId=coffee&storyboardId={fixture['storyboardId']}")
        assert by_target.json()["render"]["renderId"] == data["renderId"]
        by_film = await client.get(f"/api/renders?projectId=coffee&filmId={data['filmId']}")
        assert by_film.json()["render"]["filmVersion"] == 1
        # The derived sidecar is served with the digest the render recorded.
        sidecar = await client.get(render["subtitle"]["sidecarUrl"])
        assert sidecar.status_code == 200
        assert sidecar.headers["content-type"].startswith("application/x-subrip")
        assert hashlib.sha256(sidecar.content).hexdigest() == render["subtitle"]["sidecarSha256"]
        ass = await client.get(f"/api/films/{data['filmId']}/versions/1/subtitles.ass")
        assert ass.status_code == 200 and b"[V4+ Styles]" in ass.content
        media = await client.get(f"/api/media/{data['film']['latest']['outputMediaId']}/content")
        assert media.status_code == 200 and media.headers["content-type"] == "video/mp4"
        # Request-ID replay returns the same version; a changed body conflicts.
        again = await client.post("/api/sessions/coffee/films/render", json=body)
        assert again.status_code == 202, again.text
        assert again.json()["filmVersion"] == 1
        conflict = await client.post(
            "/api/sessions/coffee/films/render", json={**body, "subtitleMode": "burn"}
        )
        assert conflict.status_code == 409
        assert conflict.json()["error"]["code"] == "OPERATION_CONFLICT"


async def test_render_endpoint_rejects_csrf_unknown_fields_and_invalid_target(tmp_path):
    home = tmp_path / "state"
    fixture = await seed(home)
    async with render_app(live_config(home)) as (_service, client):
        body = {
            "clientRequestId": "render-invalid",
            "scope": "storyboard",
            "storyboardId": fixture["storyboardId"],
            "trackId": fixture["trackId"],
        }
        denied = await client.post(
            "/api/sessions/coffee/films/render", json=body, headers={"X-CSRF-Token": ""}
        )
        assert denied.status_code == 403 and denied.json()["error"]["code"] == "CSRF_DENIED"
        extra = await client.post("/api/sessions/coffee/films/render", json={**body, "unexpected": 1})
        assert extra.status_code == 422
        unknown = await client.post(
            "/api/sessions/coffee/films/render",
            json={**body, "profile": "2160p-h264-aac-v1"},
        )
        assert unknown.status_code == 422
        missing = await client.post(
            "/api/sessions/coffee/films/render",
            json={**body, "storyboardId": "does-not-exist"},
        )
        assert missing.status_code in {400, 404}
        assert missing.json()["error"]["code"] in {"NOT_FOUND", "FILM_NOT_FOUND"}
        empty = await client.get("/api/renders?projectId=coffee&storyboardId=does-not-exist")
        assert empty.status_code == 200 and empty.json()["render"] is None
        script = await client.get("/render-state.js")
        assert script.status_code == 200 and b"renderRequestBody" in script.content
