"""M7 E2 HTTP surface: season preview, batch generation and explicit resume."""

from contextlib import asynccontextmanager

import httpx
from shot_support import mock_tools

from vagent.config import Config
from vagent.storage import FileStore
from vagent.web import create_app

PROJECT = "coffee"
SCENES = ((4, 20), (4, 20), (4, 20))


@asynccontextmanager
async def batch_app(config):
    app = create_app(config)
    async with app.router.lifespan_context(app):
        service = app.state.service
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:3210"
        ) as client:
            client.headers["X-CSRF-Token"] = (await client.get("/api/session-token")).json()["token"]
            yield service, client


def mock_config(home):
    return Config(home=home, api_key=None, video_mode="mock")


def seed(home, count=2):
    with FileStore.open(home) as store:
        store.ensure_session(PROJECT)
        registry, _service, _adapter, _context = mock_tools(store)
        episodes = []
        for index in range(count):
            saved = registry.execute(
                "episode_save",
                {
                    "title": f"第{index + 1}集",
                    "scenes": [
                        {
                            "heading": f"第{number + 1}场：雨夜咖啡店",
                            "synopsis": f"第{number + 1}场的概要。",
                            "timeOfDay": "night",
                            "shotCount": shots,
                            "durationSeconds": seconds,
                        }
                        for number, (shots, seconds) in enumerate(SCENES)
                    ],
                },
                store=store,
                project_id=PROJECT,
                operation_key=f"m7b-api-episode-{index}",
            )
            assert saved["ok"], saved
            read = registry.execute(
                "episode_read",
                {"episodeId": saved["data"]["episodeId"]},
                store=store,
                project_id=PROJECT,
                operation_key=f"m7b-api-read-{index}",
            )
            assert read["ok"], read
            for number, (shots, seconds) in enumerate(SCENES):
                base, extra = divmod(seconds, shots)
                board = registry.execute(
                    "storyboard_save",
                    {
                        "title": f"第{number + 1}场分镜",
                        "sceneId": read["data"]["scenes"][number]["sceneId"],
                        "shots": [
                            {
                                "description": f"镜头{shot + 1}",
                                "framing": "wide",
                                "durationSeconds": base + (1 if shot < extra else 0),
                                "mode": "t2v",
                            }
                            for shot in range(shots)
                        ],
                    },
                    store=store,
                    project_id=PROJECT,
                    operation_key=f"m7b-api-board-{index}-{number}",
                )
                assert board["ok"], board
            episodes.append(saved["data"]["episodeId"])
        season = registry.execute(
            "season_save",
            {"title": "第一季", "episodeIds": episodes},
            store=store,
            project_id=PROJECT,
            operation_key="m7b-api-season",
        )
        assert season["ok"], season
        return episodes, season["data"]["seasonId"]


async def test_season_estimate_and_generate_are_local_and_isolated(tmp_path):
    home = tmp_path / "state"
    episodes, season_id = seed(home)
    async with batch_app(mock_config(home)) as (service, client):
        estimate = await client.get(
            f"/api/seasons/{season_id}/estimate?projectId={PROJECT}&episodeIds={','.join(episodes)}"
        )
        assert estimate.status_code == 200, estimate.text
        preview = estimate.json()
        assert preview["totalNewJobs"] == 24
        assert preview["simulated"] is True and preview["overLimit"] is False
        assert [item["shotCount"] for item in preview["episodes"]] == [12, 12]
        assert service.store.snapshot()["batches"] == {}

        created = await client.post(
            f"/api/sessions/{PROJECT}/seasons/generate",
            json={
                "clientRequestId": "season-generate-1",
                "seasonId": season_id,
                "episodeIds": episodes,
                "confirm": True,
            },
        )
        assert created.status_code == 202, created.text
        data = created.json()
        assert data["status"] == "succeeded"
        assert data["newJobCount"] == 24
        assert data["replayed"] is False

        detail = (await client.get(f"/api/seasons/{season_id}?projectId={PROJECT}")).json()
        assert detail["batch"]["batchId"] == data["batchId"]
        assert [item["status"] for item in detail["batch"]["episodes"]] == ["succeeded", "succeeded"]
        assert [item["batchId"] for item in detail["batches"]] == [data["batchId"]]
        snapshot = (await client.get(f"/api/sessions/{PROJECT}")).json()
        assert snapshot["season"]["batch"]["batchId"] == data["batchId"]

        replay = await client.post(
            f"/api/sessions/{PROJECT}/seasons/generate",
            json={
                "clientRequestId": "season-generate-1",
                "seasonId": season_id,
                "episodeIds": episodes,
                "confirm": True,
            },
        )
        assert replay.status_code == 202
        assert replay.json() == {**data, "replayed": True}
        state = service.store.snapshot()
        assert len(state["jobs"]) == 24
        assert len(state["batches"]) == 1

        # A second, distinct request with identical content replays the batch too.
        identical = await client.post(
            f"/api/sessions/{PROJECT}/seasons/generate",
            json={
                "clientRequestId": "season-generate-2",
                "seasonId": season_id,
                "episodeIds": episodes,
                "confirm": True,
            },
        )
        assert identical.status_code == 202
        assert identical.json()["batchId"] == data["batchId"]
        assert identical.json()["replayed"] is True
        assert len(service.store.snapshot()["jobs"]) == 24


async def test_season_generate_rejects_bad_targets_and_conflicts(tmp_path):
    home = tmp_path / "state"
    episodes, season_id = seed(home)
    async with batch_app(mock_config(home)) as (_service, client):
        body = {
            "clientRequestId": "season-bad",
            "seasonId": season_id,
            "episodeIds": episodes,
            "confirm": True,
        }
        missing_csrf = await client.post(
            f"/api/sessions/{PROJECT}/seasons/generate", json=body, headers={"X-CSRF-Token": ""}
        )
        assert missing_csrf.status_code == 403

        both = await client.post(
            f"/api/sessions/{PROJECT}/seasons/generate",
            json={**body, "clientRequestId": "season-both", "resumeBatchId": "x" * 8},
        )
        assert both.status_code == 422
        extra = await client.post(
            f"/api/sessions/{PROJECT}/seasons/generate",
            json={**body, "clientRequestId": "season-extra", "unexpected": 1},
        )
        assert extra.status_code == 422

        unknown = await client.post(
            f"/api/sessions/{PROJECT}/seasons/generate",
            json={
                "clientRequestId": "season-unknown",
                "seasonId": "ffffffff-ffff-4fff-8fff-ffffffffffff",
                "confirm": True,
            },
        )
        assert unknown.status_code == 404
        assert unknown.json()["error"]["code"] == "SEASON_NOT_FOUND"

        created = await client.post(f"/api/sessions/{PROJECT}/seasons/generate", json=body)
        assert created.status_code == 202, created.text
        conflict = await client.post(
            f"/api/sessions/{PROJECT}/seasons/generate",
            json={
                "clientRequestId": "season-resume-done",
                "seasonId": season_id,
                "resumeBatchId": created.json()["batchId"],
                "confirm": True,
            },
        )
        assert conflict.status_code == 409
        assert conflict.json()["error"]["code"] == "BATCH_CONFLICT"
