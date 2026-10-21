"""E1 film HTTP surface: read, explicit assemble, media union and Range delivery."""

from contextlib import asynccontextmanager

import httpx
from film_support import assembly_args, delivered_pair
from wan_support import live_config

from vagent.storage import FileStore
from vagent.web import create_app


@asynccontextmanager
async def film_app(config):
    app = create_app(config)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:3210"
        ) as client:
            client.headers["X-CSRF-Token"] = (await client.get("/api/session-token")).json()["token"]
            yield app.state.service, client


async def test_film_endpoints_require_csrf_and_strict_bodies(tmp_path, monkeypatch):
    home = tmp_path / "state"
    with FileStore.open(home) as store:
        fixture = await delivered_pair(store)
    async with film_app(live_config(home)) as (_service, client):
        url = "/api/sessions/coffee/films/assemble"
        body = {
            "clientRequestId": "assemble-1",
            "storyboardId": fixture["storyboardId"],
            "expectedRevision": fixture["revision"],
        }
        missing = await client.post(url, json=body, headers={"X-CSRF-Token": ""})
        assert missing.status_code == 403 and missing.json()["error"]["code"] == "CSRF_DENIED"
        extra = await client.post(url, json={**body, "unexpected": 1})
        assert extra.status_code == 422
        negative = await client.post(url, json={**body, "expectedRevision": -1})
        assert negative.status_code == 422


async def test_film_read_assemble_and_media_union(tmp_path):
    home = tmp_path / "state"
    with FileStore.open(home) as store:
        fixture = await delivered_pair(store)
    async with film_app(live_config(home)) as (_service, client):
        storyboard_id = fixture["storyboardId"]
        empty = (await client.get(f"/api/films/{storyboard_id}?projectId=coffee")).json()
        assert empty == {"storyboardId": storyboard_id, "film": None}
        stale = await client.post(
            "/api/sessions/coffee/films/assemble",
            json={**assembly_args(fixture), "clientRequestId": "assemble-stale", "expectedRevision": 99},
        )
        assert stale.status_code == 409 and stale.json()["error"]["code"] == "VERSION_CONFLICT"
        assembled = await client.post(
            "/api/sessions/coffee/films/assemble",
            json={**assembly_args(fixture), "clientRequestId": "assemble-ok"},
        )
        assert assembled.status_code == 202, assembled.text
        film = assembled.json()
        assert film["currentVersion"] == 1 and film["latest"]["mediaAvailable"] is True
        media_id = film["latest"]["outputMediaId"]
        detail = (await client.get(f"/api/media/{media_id}")).json()
        assert detail["kind"] == "film" and detail["filmVersion"] == 1
        assert detail["mediaAvailable"] is True
        assert detail["metadata"]["durationSeconds"] > 10
        full = await client.get(f"/api/media/{media_id}/content")
        assert full.status_code == 200 and full.headers["content-type"] == "video/mp4"
        assert full.headers["etag"] == f'"{detail["sha256"]}"'
        assert len(full.content) == detail["sizeBytes"]
        partial = await client.get(f"/api/media/{media_id}/content", headers={"Range": "bytes=0-99"})
        assert partial.status_code == 206 and len(partial.content) == 100
        head = await client.head(f"/api/media/{media_id}/content")
        assert head.status_code == 200 and head.headers["content-length"] == str(detail["sizeBytes"])
        download = await client.get(f"/api/media/{media_id}/content?download=1")
        assert download.status_code == 200
        assert f'filename="{media_id}.mp4"' in download.headers["content-disposition"]
        replay = await client.post(
            "/api/sessions/coffee/films/assemble",
            json={**assembly_args(fixture), "clientRequestId": "assemble-ok"},
        )
        assert replay.status_code == 202 and replay.json()["latest"]["outputMediaId"] == media_id
        reread = (await client.get(f"/api/films/{storyboard_id}?projectId=coffee")).json()
        assert reread["film"]["currentVersion"] == 1
        assert reread["film"]["latest"]["mediaAvailable"] is True
        other = await client.get(f"/api/films/{storyboard_id}?projectId=other")
        assert other.status_code == 404
