"""M6 E1 HTTP surface: subtitle read, save, export and controlled SRT/ASS sidecars."""

import hashlib
from contextlib import asynccontextmanager

import httpx
import pytest
from subtitle_support import film_with_dialogue
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
async def subtitle_app(config):
    app = create_app(config)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:3210"
        ) as client:
            client.headers["X-CSRF-Token"] = (await client.get("/api/session-token")).json()["token"]
            yield app.state.service, client


async def seed(home):
    with FileStore.open(home) as store:
        fixture = await film_with_dialogue(store)
    return fixture


async def test_subtitle_endpoints_reject_csrf_and_unknown_fields(tmp_path):
    home = tmp_path / "state"
    fixture = await seed(home)
    body = {"clientRequestId": "save-1", "scope": "storyboard", "storyboardId": fixture["storyboardId"]}
    async with subtitle_app(live_config(home)) as (_service, client):
        missing = await client.post(
            "/api/sessions/coffee/subtitles/save", json=body, headers={"X-CSRF-Token": ""}
        )
        assert missing.status_code == 403 and missing.json()["error"]["code"] == "CSRF_DENIED"
        extra = await client.post("/api/sessions/coffee/subtitles/save", json={**body, "unexpected": 1})
        assert extra.status_code == 422
        unknown = await client.post(
            "/api/sessions/coffee/subtitles/save",
            json={**body, "scope": "season"},
        )
        assert unknown.status_code == 422


async def test_subtitle_save_read_export_and_sidecar_delivery(tmp_path):
    home = tmp_path / "state"
    fixture = await seed(home)
    async with subtitle_app(live_config(home)) as (_service, client):
        storyboard_id = fixture["storyboardId"]
        empty = (await client.get(f"/api/subtitles?projectId=coffee&storyboardId={storyboard_id}")).json()
        assert empty == {"storyboardId": storyboard_id, "episodeId": None, "track": None}
        saved = await client.post(
            "/api/sessions/coffee/subtitles/save",
            json={
                "clientRequestId": "save-1",
                "scope": "storyboard",
                "storyboardId": storyboard_id,
            },
        )
        assert saved.status_code == 201, saved.text
        track = saved.json()
        assert track["currentVersion"] == 1 and track["latest"]["cueCount"] > 0
        assert track["latest"]["timelineSource"] == "film"
        track_id = track["trackId"]
        # The storyboard panel reads the same track without rendering SRT digests.
        detail = (await client.get(f"/api/storyboards/{storyboard_id}?projectId=coffee")).json()
        assert detail["subtitle"]["trackId"] == track_id
        assert "srtSha256" not in detail["subtitle"]["latest"]
        # Soft export appends a rendered film version and keeps the earlier one.
        exported = await client.post(
            "/api/sessions/coffee/subtitles/export",
            json={
                "clientRequestId": "export-1",
                "trackId": track_id,
                "mode": "soft",
                "profile": "720p-h264-aac-v1",
            },
        )
        assert exported.status_code == 202, exported.text
        film = exported.json()
        assert film["currentVersion"] == 2
        render = film["latest"]["render"]
        assert render["subtitle"]["mode"] == "soft"
        # The sidecar URL serves exactly the bytes whose digest the render recorded.
        srt = await client.get(track["latest"]["srtUrl"])
        assert srt.status_code == 200
        assert srt.headers["content-type"].startswith("application/x-subrip")
        assert f"subtitles-{track_id}-v1.srt" in srt.headers["content-disposition"]
        assert hashlib.sha256(srt.content).hexdigest() == render["subtitle"]["sidecarSha256"]
        ass = await client.get(track["latest"]["assUrl"])
        assert ass.status_code == 200 and b"[V4+ Styles]" in ass.content
        # A different request under the same request ID is refused, not re-rendered.
        conflict = await client.post(
            "/api/sessions/coffee/subtitles/export",
            json={
                "clientRequestId": "export-1",
                "trackId": track_id,
                "mode": "burn",
                "profile": "720p-h264-aac-v1",
            },
        )
        assert conflict.status_code == 409
        assert conflict.json()["error"]["code"] == "OPERATION_CONFLICT"
        # 1080P is not an E1 export profile.
        unsupported = await client.post(
            "/api/sessions/coffee/subtitles/export",
            json={
                "clientRequestId": "export-2",
                "trackId": track_id,
                "mode": "soft",
                "profile": "1080p-h264-aac-v1",
            },
        )
        assert unsupported.status_code == 400
        assert unsupported.json()["error"]["code"] == "RENDER_PROFILE_UNSUPPORTED"
        by_track = (await client.get(f"/api/subtitles?projectId=coffee&trackId={track_id}")).json()
        assert by_track["track"]["trackId"] == track_id
        unknown = await client.get(f"/api/subtitles/{track_id}/99/srt")
        assert (
            unknown.status_code in {400, 404} and unknown.json()["error"]["code"] == "SUBTITLE_TRACK_MISSING"
        )


async def test_burn_export_over_http(tmp_path):
    home = tmp_path / "state"
    fixture = await seed(home)
    async with subtitle_app(live_config(home)) as (_service, client):
        saved = await client.post(
            "/api/sessions/coffee/subtitles/save",
            json={
                "clientRequestId": "save-burn",
                "scope": "storyboard",
                "storyboardId": fixture["storyboardId"],
            },
        )
        track_id = saved.json()["trackId"]
        burned = await client.post(
            "/api/sessions/coffee/subtitles/export",
            json={"clientRequestId": "burn-1", "trackId": track_id, "mode": "burn"},
        )
        assert burned.status_code == 202, burned.text
        film = burned.json()
        assert film["latest"]["render"]["subtitle"]["mode"] == "burn"
        assert film["latest"]["render"]["subtitle"]["sidecarSha256"] is None
        media_id = film["latest"]["outputMediaId"]
        content = await client.get(f"/api/media/{media_id}/content")
        assert content.status_code == 200 and content.headers["content-type"] == "video/mp4"
