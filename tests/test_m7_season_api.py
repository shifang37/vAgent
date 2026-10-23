"""M7 E1 HTTP surface: season list, detail and local saves without a video mode."""

from contextlib import asynccontextmanager

import httpx
from shot_support import mock_tools
from subtitle_support import episode_args, plan

from vagent.config import Config
from vagent.storage import FileStore
from vagent.web import create_app

PROJECT = "coffee"


@asynccontextmanager
async def season_app(config):
    app = create_app(config)
    async with app.router.lifespan_context(app):
        service = app.state.service
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:3210"
        ) as client:
            client.headers["X-CSRF-Token"] = (await client.get("/api/session-token")).json()["token"]
            yield service, client


def seed(home):
    with FileStore.open(home) as store:
        store.ensure_session(PROJECT)
        registry, _service, _adapter, _context = mock_tools(store)
        episodes = []
        for index in range(3):
            args = episode_args(plan())
            args["title"] = f"第{index + 1}集"
            result = registry.execute(
                "episode_save",
                args,
                store=store,
                project_id=PROJECT,
                operation_key=f"m7-api-episode-{index}",
            )
            assert result["ok"], result
            episodes.append(result["data"]["episodeId"])
    return episodes


def off_config(home):
    return Config(home=home, api_key=None, video_mode="off")


async def test_season_endpoints_are_local_and_work_with_video_mode_off(tmp_path):
    home = tmp_path / "state"
    episodes = seed(home)
    async with season_app(off_config(home)) as (service, client):
        empty = (await client.get(f"/api/seasons?projectId={PROJECT}")).json()
        assert empty == {"seasons": []}

        body = {"clientRequestId": "season-save-1", "title": "雨夜第一季", "episodeIds": episodes[:2]}
        created = await client.post(f"/api/sessions/{PROJECT}/seasons/save", json=body)
        assert created.status_code == 201, created.text
        saved = created.json()
        assert saved["version"] == 1 and saved["replayed"] is False
        season_id = saved["seasonId"]

        listing = (await client.get(f"/api/seasons?projectId={PROJECT}")).json()
        assert [item["seasonId"] for item in listing["seasons"]] == [season_id]
        detail = (await client.get(f"/api/seasons/{season_id}?projectId={PROJECT}")).json()
        assert detail["season"]["title"] == "雨夜第一季"
        assert [item["episodeId"] for item in detail["episodes"]] == episodes[:2]
        assert detail["batch"] is None

        snapshot = (await client.get(f"/api/sessions/{PROJECT}")).json()
        assert snapshot["season"]["seasonId"] == season_id

        # Identical content replays; a stale revision is a 409 with the frozen code.
        replay = await client.post(
            f"/api/sessions/{PROJECT}/seasons/save",
            json={**body, "clientRequestId": "season-save-2"},
        )
        assert replay.status_code == 201 and replay.json()["replayed"] is True
        stale = await client.post(
            f"/api/sessions/{PROJECT}/seasons/save",
            json={
                "clientRequestId": "season-save-3",
                "seasonId": season_id,
                "title": "雨夜第一季",
                "episodeIds": episodes,
                "expectedRevision": 9,
            },
        )
        assert stale.status_code == 409
        assert stale.json()["error"]["code"] == "SEASON_VERSION_CONFLICT"

        state = service.store.snapshot()
        assert state["jobs"] == {}
        assert len(state["seasons"][season_id]["versions"]) == 1
        season_run = next(
            run for run in state["runs"].values() if run["prompt"].startswith("本地界面发起的季成员保存")
        )
        assert season_run["videoMode"] == "off" and season_run["toolFeatures"] == {}


async def test_season_endpoints_reject_csrf_unknown_fields_and_unknown_seasons(tmp_path):
    home = tmp_path / "state"
    episodes = seed(home)
    async with season_app(off_config(home)) as (_service, client):
        body = {"clientRequestId": "season-csrf", "title": "第一季", "episodeIds": episodes[:1]}
        missing = await client.post(
            f"/api/sessions/{PROJECT}/seasons/save", json=body, headers={"X-CSRF-Token": ""}
        )
        assert missing.status_code == 403 and missing.json()["error"]["code"] == "CSRF_DENIED"
        extra = await client.post(f"/api/sessions/{PROJECT}/seasons/save", json={**body, "unexpected": 1})
        assert extra.status_code == 422
        too_many = await client.post(
            f"/api/sessions/{PROJECT}/seasons/save",
            json={
                "clientRequestId": "season-many",
                "title": "第一季",
                "episodeIds": [episodes[0], episodes[1], episodes[2], episodes[0], episodes[1]],
            },
        )
        assert too_many.status_code == 422
        unknown = await client.get("/api/seasons/ffffffff-ffff-4fff-8fff-ffffffffffff?projectId=coffee")
        assert unknown.status_code == 404
        assert unknown.json()["error"]["code"] == "SEASON_NOT_FOUND"
