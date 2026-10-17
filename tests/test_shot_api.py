"""M2 D3 shot HTTP surface: estimate preview, batch registration, redo and delivery."""

import asyncio
import json
from contextlib import asynccontextmanager

import httpx
from job_support import eventually
from shot_support import shot
from wan_support import live_config

from vagent.video import clients
from vagent.web import create_app

ABSENT = "44444444-4444-4444-4444-444444444444"


@asynccontextmanager
async def shot_app(config):
    app = create_app(config)
    async with app.router.lifespan_context(app):
        service = app.state.service
        service.store.ensure_session("coffee")
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:3210"
        ) as client:
            client.headers["X-CSRF-Token"] = (await client.get("/api/session-token")).json()["token"]
            yield service, client


def save_storyboard(service, descriptions):
    result = service.tools.execute(
        "storyboard_save",
        {"title": "雨夜咖啡店", "shots": [shot(text) for text in descriptions]},
        store=service.store,
        project_id="coffee",
        operation_key="web-seed-save",
    )
    assert result["ok"], result
    return result["data"]


def mock_config(tmp_path):
    from vagent.config import Config

    return Config(home=tmp_path / "state", api_key=None, video_mode="mock")


async def test_mock_storyboard_can_be_previewed_confirmed_and_redone(tmp_path):
    async with shot_app(mock_config(tmp_path)) as (service, client):
        saved = save_storyboard(service, ["镜头一", "镜头二", "镜头三"])
        storyboard_id, version = saved["storyboardId"], saved["version"]
        view = (await client.get("/api/sessions/coffee")).json()["storyboard"]
        assert view["storyboardId"] == storyboard_id and view["revision"] == 0
        assert [item["id"] for item in view["shots"]] == saved["shotIds"]
        assert all(item["versions"] == [] and item["latest"] is None for item in view["shots"])
        listing = (await client.get("/api/storyboards?projectId=coffee")).json()["storyboards"]
        assert [item["storyboardId"] for item in listing] == [storyboard_id]
        detail = (await client.get(f"/api/storyboards/{storyboard_id}?projectId=coffee")).json()
        assert detail["shotCount"] == 3 and detail["versionCount"] == 1
        estimate = (
            await client.get(
                f"/api/storyboards/{storyboard_id}/estimate",
                params={
                    "projectId": "coffee",
                    "storyboardVersion": version,
                    "shotIds": ",".join(saved["shotIds"]),
                },
            )
        ).json()
        assert estimate["mode"] == "mock" and estimate["simulated"] is True
        assert [row["reused"] for row in estimate["shots"]] == [False, False, False]
        assert estimate["totalEstimate"] is None and estimate["limits"] is None
        assert not service.store.snapshot()["jobs"]
        batch = {
            "clientRequestId": "web-batch-1",
            "storyboardId": storyboard_id,
            "storyboardVersion": version,
            "shotIds": saved["shotIds"][:2],
            "confirm": True,
        }
        created = await client.post("/api/sessions/coffee/shots/generate", json=batch)
        assert created.status_code == 202, created.text
        body = created.json()
        assert [row["shotId"] for row in body["shots"]] == saved["shotIds"][:2]
        assert body["mode"] == "mock" and body["storyboardRevision"] == 1
        assert body["runId"] == clients.client_run_id("coffee", "web-batch-1")
        jobs = (await client.get("/api/jobs?sessionId=coffee")).json()["jobs"]
        assert len(jobs) == 2
        assert {job["jobId"] for job in jobs} == {row["jobId"] for row in body["shots"]}
        # A mock shot Job keeps the frozen v1 contract: the storyboard owns the link.
        assert all("shotRef" not in job for job in jobs)
        replayed = await client.post("/api/sessions/coffee/shots/generate", json=batch)
        assert replayed.status_code == 202 and replayed.json() == body
        assert len((await client.get("/api/jobs?sessionId=coffee")).json()["jobs"]) == 2
        unconfirmed = await client.post(
            "/api/sessions/coffee/shots/generate",
            json={
                **batch,
                "clientRequestId": "web-batch-unconfirmed",
                "shotIds": [saved["shotIds"][2]],
                "confirm": False,
            },
        )
        assert unconfirmed.status_code == 400
        assert unconfirmed.json()["error"]["code"] == "CONFIRMATION_REQUIRED"
        assert len((await client.get("/api/jobs?sessionId=coffee")).json()["jobs"]) == 2
        target = saved["shotIds"][1]
        redo = {
            "clientRequestId": "web-redo-1",
            "storyboardId": storyboard_id,
            "storyboardVersion": version,
            "expectedRevision": 1,
        }
        redone = await client.post(f"/api/sessions/coffee/shots/{target}/redo", json=redo)
        assert redone.status_code == 202, redone.text
        assert redone.json()["shots"][0]["shotId"] == target
        assert redone.json()["storyboardRevision"] == 2
        stale = await client.post(
            f"/api/sessions/coffee/shots/{target}/redo",
            json={**redo, "clientRequestId": "web-redo-2", "expectedRevision": 1},
        )
        assert stale.status_code == 409 and stale.json()["error"]["code"] == "VERSION_CONFLICT"
        view = (await client.get(f"/api/storyboards/{storyboard_id}?projectId=coffee")).json()
        assert len(view["shots"][0]["versions"]) == 1 and len(view["shots"][1]["versions"]) == 2
        assert view["shots"][1]["latest"]["jobId"] == redone.json()["shots"][0]["jobId"]
        assert (await client.get(f"/api/storyboards/{ABSENT}?projectId=coffee")).status_code == 404
        assert (await client.get(f"/api/storyboards/{storyboard_id}?projectId=other")).status_code == 404
        assert (await client.get("/api/storyboards?projectId=other")).json()["storyboards"] == []
        assert (
            await client.post(
                f"/api/sessions/coffee/shots/{ABSENT}/redo",
                json={**redo, "clientRequestId": "web-redo-absent"},
            )
        ).status_code == 400
        # One request ID owns one shot: reusing it for another shot is refused.
        conflicted = await client.post(f"/api/sessions/coffee/shots/{ABSENT}/redo", json=redo)
        assert conflicted.status_code == 409
        assert conflicted.json()["error"]["code"] == "OPERATION_CONFLICT"
        assert (await client.post("/api/sessions/coffee/shots/generate", json={})).status_code == 422


async def test_client_registrations_notify_the_workbench(tmp_path):
    async with shot_app(mock_config(tmp_path)) as (service, client):
        saved = save_storyboard(service, ["镜头一", "镜头二"])
        queue = service.subscribe("coffee")
        created = await client.post(
            "/api/sessions/coffee/shots/generate",
            json={
                "clientRequestId": "web-notify",
                "storyboardId": saved["storyboardId"],
                "storyboardVersion": saved["version"],
                "shotIds": saved["shotIds"],
                "confirm": True,
            },
        )
        assert created.status_code == 202, created.text
        updates = []
        async with asyncio.timeout(30):
            while len(updates) < 2:
                event = await queue.get()
                if event["type"] == "job.updated":
                    updates.append(event)
        assert all(
            item["job"]["jobId"] in {row["jobId"] for row in created.json()["shots"]} for item in updates
        )
        assert {item["sessionId"] for item in updates} == {"coffee"}
        assert {item["runId"] for item in updates} == {created.json()["runId"]}
    assert not (service.store.home / "instance.lock").exists()


async def test_delivered_shot_media_is_playable_from_the_panel(tmp_path, media_runtime):
    """One live shot through the real Workers, with the frozen Wan protocol mocked."""
    async with shot_app(live_config(tmp_path / "state")) as (service, client):
        saved = save_storyboard(service, ["镜头一"])
        created = await client.post(
            "/api/sessions/coffee/shots/generate",
            json={
                "clientRequestId": "web-live-1",
                "storyboardId": saved["storyboardId"],
                "storyboardVersion": saved["version"],
                "shotIds": saved["shotIds"],
                "confirm": True,
            },
        )
        assert created.status_code == 202, created.text
        body = created.json()
        job_id = body["shots"][0]["jobId"]
        assert body["totalEstimate"]["amount"] == "3.00"
        assert body["limits"]["usedNewJobs"] == 1 and body["limits"]["remainingAmount"] == "9.00"
        await eventually(lambda: service.video_jobs.get(job_id).status == "succeeded")
        job = service.video_jobs.get(job_id)
        assert len(media_runtime.submissions) == 1 and len(media_runtime.downloads) == 1
        detail = (await client.get(f"/api/storyboards/{saved['storyboardId']}?projectId=coffee")).json()
        latest = detail["shots"][0]["latest"]
        assert latest["status"] == "succeeded" and latest["mediaAvailable"] is True
        assert latest["mediaUrl"] == f"/api/media/{job.download.media_id}/content"
        content = await client.get(latest["mediaUrl"])
        assert content.status_code == 200 and content.headers["content-type"] == "video/mp4"
        assert content.content == media_runtime.data
        # A shot Job's result carries its own shot specification (JobResultV3).
        detail_job = (await client.get(f"/api/jobs/{job_id}")).json()
        assert detail_job["result"]["spec"] == {
            "mode": "t2v",
            "durationSeconds": 5,
            "resolution": "720P",
            "aspectRatio": "16:9",
        }
        assert detail_job["result"]["mediaRefs"][0]["mediaId"] == job.download.media_id
        # A shot Job's view has to be readable: public request fields, its shot link
        # and no workspace identity (the V3 request publishes its own public shape).
        assert detail_job["request"]["prompt"] == "镜头一"
        assert detail_job["request"]["spec"]["mode"] == "t2v"
        assert detail_job["shotRef"]["shotId"] == saved["shotIds"][0]
        assert "workspaceId" not in json.dumps(detail_job)
        snapshot = (await client.get("/api/sessions/coffee")).json()
        assert [item["jobId"] for item in snapshot["jobs"]] == [job_id]
        assert snapshot["jobs"][0]["shotRef"]["shotId"] == saved["shotIds"][0]
        assert json.dumps(detail) and "Signature" not in json.dumps(detail)
