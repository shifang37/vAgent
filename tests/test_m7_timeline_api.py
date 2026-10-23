"""M7 E3 HTTP surface: timeline read/save, audio import and timeline renders."""

import io
import wave
from contextlib import asynccontextmanager

import httpx
from timeline_support import PROJECT, clips_for, renderable_episode

from vagent.config import Config
from vagent.storage import FileStore
from vagent.web import create_app


@asynccontextmanager
async def timeline_app(config):
    app = create_app(config)
    async with app.router.lifespan_context(app):
        service = app.state.service
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:3210"
        ) as client:
            client.headers["X-CSRF-Token"] = (await client.get("/api/session-token")).json()["token"]
            yield service, client


def wav_bytes(seconds: float = 1.0) -> bytes:
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(48000)
        handle.writeframes(b"\x00\x00" * int(48000 * seconds))
    return buffer.getvalue()


async def seed(home):
    with FileStore.open(home) as store:
        store.ensure_session(PROJECT)
        fixture = await renderable_episode(store)
        return {
            "episodeId": fixture["episode"]["episodeId"],
            "trackId": fixture["trackId"],
            "clips": clips_for(store.snapshot(), fixture["boards"]),
        }


def off_config(home):
    return Config(home=home, api_key=None, video_mode="off")


def save_body(seed_value, **changes):
    return {
        "clientRequestId": "timeline-save-1",
        "episodeId": seed_value["episodeId"],
        "clips": seed_value["clips"],
        "subtitle": {"trackId": seed_value["trackId"], "trackVersion": 1},
        **changes,
    }


async def test_timeline_endpoints_are_local_and_render_a_film(tmp_path):
    home = tmp_path / "state"
    seed_value = await seed(home)
    async with timeline_app(off_config(home)) as (service, client):
        editor = (
            await client.get(f"/api/episodes/{seed_value['episodeId']}/timeline?projectId=coffee")
        ).json()
        assert editor["episodeId"] == seed_value["episodeId"]
        assert editor["timeline"] is None
        assert len(editor["shots"]) == len(seed_value["clips"])
        assert editor["subtitleTracks"][0]["trackId"] == seed_value["trackId"]

        created = await client.post(f"/api/sessions/{PROJECT}/timelines/save", json=save_body(seed_value))
        assert created.status_code == 201, created.text
        saved = created.json()
        assert saved["currentVersion"] == 1 and saved["replayed"] is False
        timeline_id = saved["timelineId"]
        state = service.store.snapshot()
        # Saving a timeline never registers or submits a Job; the seeded deliveries stay.
        assert len(state["jobs"]) == len(seed_value["clips"])
        timeline_run = next(
            run for run in state["runs"].values() if run["prompt"].startswith("本地界面发起的时间线保存")
        )
        assert timeline_run["videoMode"] == "off" and timeline_run["toolFeatures"] == {}

        detail = (await client.get(f"/api/timelines/{timeline_id}?projectId=coffee")).json()
        assert detail["timelineId"] == timeline_id
        assert detail["latest"]["clipCount"] == len(seed_value["clips"])
        assert detail["latest"]["stale"] is False

        replay = await client.post(
            f"/api/sessions/{PROJECT}/timelines/save",
            json=save_body(seed_value, clientRequestId="timeline-save-2"),
        )
        assert replay.status_code == 201 and replay.json()["replayed"] is True
        stale = await client.post(
            f"/api/sessions/{PROJECT}/timelines/save",
            json=save_body(
                seed_value,
                clientRequestId="timeline-save-3",
                timelineId=timeline_id,
                clips=[dict(item, inMs=1000) for item in seed_value["clips"]],
                expectedRevision=9,
            ),
        )
        assert stale.status_code == 409
        assert stale.json()["error"]["code"] == "TIMELINE_VERSION_CONFLICT"

        imported = await client.post(
            "/api/audio/import",
            data={
                "clientRequestId": "audio-import-1",
                "projectId": PROJECT,
                "kind": "bgm",
                "title": "雨声",
                "sourceUrl": "https://example.test/rain.wav",
                "license": "CC0",
            },
            files={"file": ("rain.wav", wav_bytes(), "audio/wav")},
        )
        assert imported.status_code == 201, imported.text
        audio = imported.json()
        assert audio["replayed"] is False and audio["kind"] == "bgm"
        listing = (await client.get("/api/audio?projectId=coffee")).json()
        assert [item["audioId"] for item in listing["audio"]] == [audio["audioId"]]
        again = await client.post(
            "/api/audio/import",
            data={
                "clientRequestId": "audio-import-2",
                "projectId": PROJECT,
                "kind": "bgm",
                "title": "雨声 2",
                "sourceUrl": "https://example.test/rain2.wav",
                "license": "CC0",
            },
            files={"file": ("rain.wav", wav_bytes(), "audio/wav")},
        )
        assert again.status_code == 201 and again.json()["replayed"] is True
        assert again.json()["audioId"] == audio["audioId"]

        rendered = await client.post(
            f"/api/sessions/{PROJECT}/films/render",
            json={"clientRequestId": "timeline-render-1", "scope": "timeline", "timelineId": timeline_id},
        )
        assert rendered.status_code == 202, rendered.text
        result = rendered.json()
        assert result["scope"] == "timeline" and result["timelineId"] == timeline_id
        read = (await client.get(f"/api/renders?projectId=coffee&timelineId={timeline_id}")).json()
        assert read["render"]["renderId"] == result["renderId"]
        assert read["render"]["scope"] == "timeline"
        episode = (await client.get(f"/api/episodes/{seed_value['episodeId']}?projectId=coffee")).json()
        assert episode["timeline"]["timelineId"] == timeline_id
        assert episode["render"]["scope"] == "timeline"
        script = await client.get("/timeline-state.js")
        assert script.status_code == 200 and b"timelineValidation" in script.content
        page = await client.get("/")
        assert b'id="timeline-panel"' in page.content and b'id="audio-input"' in page.content
        app_script = await client.get("/app.js")
        assert b"renderTimelinePanel" in app_script.content
        assert b"timelineRenderBody" in app_script.content


async def test_timeline_endpoints_reject_bad_requests(tmp_path):
    home = tmp_path / "state"
    seed_value = await seed(home)
    async with timeline_app(off_config(home)) as (_service, client):
        body = save_body(seed_value)
        missing = await client.post(
            f"/api/sessions/{PROJECT}/timelines/save", json=body, headers={"X-CSRF-Token": ""}
        )
        assert missing.status_code == 403
        extra = await client.post(f"/api/sessions/{PROJECT}/timelines/save", json={**body, "unexpected": 1})
        assert extra.status_code == 422
        unknown = await client.get("/api/timelines/ffffffff-ffff-4fff-8fff-ffffffffffff?projectId=coffee")
        assert unknown.status_code == 404
        assert unknown.json()["error"]["code"] == "TIMELINE_NOT_FOUND"
        bad_audio = await client.post(
            "/api/audio/import",
            data={
                "clientRequestId": "audio-bad",
                "projectId": PROJECT,
                "kind": "bgm",
                "title": "坏文件",
                "sourceUrl": "https://example.test/x.wav",
                "license": "CC0",
            },
            files={"file": ("x.wav", b"not audio", "audio/wav")},
        )
        assert bad_audio.status_code == 400
        assert bad_audio.json()["error"]["code"] == "AUDIO_FORMAT_UNSUPPORTED"
        no_license = await client.post(
            "/api/audio/import",
            data={
                "clientRequestId": "audio-no-license",
                "projectId": PROJECT,
                "kind": "bgm",
                "title": "无许可",
                "sourceUrl": "https://example.test/x.wav",
                "license": "",
            },
            files={"file": ("x.wav", wav_bytes(), "audio/wav")},
        )
        assert no_license.status_code == 400
        assert no_license.json()["error"]["code"] == "INVALID_REQUEST"
