"""M2 D3 asset HTTP surface: upload boundary, listing, ranged delivery and 404/410."""

import hashlib
from contextlib import asynccontextmanager

import httpx
from shot_support import png_bytes

from vagent.config import Config
from vagent.web import MAX_UPLOAD_BYTES, create_app


@asynccontextmanager
async def asset_app(tmp_path):
    app = create_app(Config(home=tmp_path / "state", api_key=None, video_mode="mock"))
    async with app.router.lifespan_context(app):
        service = app.state.service
        service.store.ensure_session("coffee")
        service.store.ensure_session("other")
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:3210"
        ) as client:
            client.headers["X-CSRF-Token"] = (await client.get("/api/session-token")).json()["token"]
            yield service, client


async def upload(client, data, *, project_id="coffee", name="reference.png", mime="image/png"):
    return await client.post(
        "/api/assets", data={"projectId": project_id}, files={"file": (name, data, mime)}
    )


async def test_upload_lists_and_delivers_the_exact_bytes(tmp_path):
    async with asset_app(tmp_path) as (service, client):
        data = png_bytes(1280, 720)
        created = await upload(client, data)
        assert created.status_code == 201, created.text
        asset = created.json()
        asset_id = asset["assetId"]
        assert asset["projectId"] == "coffee" and asset["mimeType"] == "image/png"
        assert asset["deduplicated"] is False and asset["sizeBytes"] == len(data)
        assert asset["dimensions"] == {"width": 1280, "height": 720}
        assert asset["sha256"] == hashlib.sha256(data).hexdigest()
        for secret in ("relativePath", "assets/", str(service.store.home)):
            assert secret not in created.text
        listing = (await client.get("/api/assets?projectId=coffee")).json()["assets"]
        assert [item["assetId"] for item in listing] == [asset_id]
        detail = (await client.get(f"/api/assets/{asset_id}")).json()
        assert detail["available"] is True and detail["contentUrl"] == f"/api/assets/{asset_id}/content"
        assert detail["availability"] == {"status": "available", "reason": None}
        # The library is directory-wide; a project filter is an explicit choice.
        assert (await client.get("/api/assets")).json()["assets"] == listing
        assert (await client.get("/api/assets?projectId=other")).json()["assets"] == []
        content = await client.get(detail["contentUrl"])
        assert content.status_code == 200 and content.content == data
        assert content.headers["content-type"] == "image/png"
        assert content.headers["accept-ranges"] == "bytes"
        assert content.headers["content-disposition"] == f'inline; filename="{asset_id}.png"'
        ranged = await client.get(detail["contentUrl"], headers={"Range": "bytes=0-15"})
        assert ranged.status_code == 206 and ranged.content == data[:16]
        assert ranged.headers["content-range"] == f"bytes 0-15/{len(data)}"
        head = await client.head(detail["contentUrl"])
        assert head.status_code == 200 and head.content == b""
        attachment = await client.get(detail["contentUrl"] + "?download=1")
        assert attachment.content == data
        assert attachment.headers["content-disposition"] == f'attachment; filename="{asset_id}.png"'
        assert (await client.get(detail["contentUrl"], headers={"Range": "bytes=999999-"})).status_code == 416
        assert (await client.get("/api/assets/44444444-4444-4444-4444-444444444444")).status_code == 404
        assert (await client.get("/api/assets/not-a-uuid/content")).status_code == 404


async def test_upload_boundary_is_strict_and_never_writes_partial_state(tmp_path):
    async with asset_app(tmp_path) as (service, client):
        data = png_bytes(1280, 720)
        denied = await client.post(
            "/api/assets",
            data={"projectId": "coffee"},
            files={"file": ("reference.png", data, "image/png")},
            headers={"X-CSRF-Token": "wrong"},
        )
        assert denied.status_code == 403
        assert (await client.post("/api/assets", json={"projectId": "coffee"})).status_code == 415
        no_file = await client.post(
            "/api/assets",
            data={"projectId": "coffee"},
            files={"other": ("notes.txt", b"text", "text/plain")},
        )
        assert no_file.status_code == 400 and no_file.json()["error"]["code"] == "INVALID_REQUEST"
        no_project = await client.post(
            "/api/assets", data={}, files={"file": ("reference.png", data, "image/png")}
        )
        assert no_project.status_code == 400
        assert (await upload(client, data, project_id="absent")).status_code == 404
        oversized = await upload(client, b"x" * (MAX_UPLOAD_BYTES + 1024))
        assert oversized.status_code == 413
        bad_image = await upload(client, b"GIF89a" + b"\x00" * 32)
        assert bad_image.status_code == 400
        assert bad_image.json()["error"]["code"] == "IMAGE_FORMAT_UNSUPPORTED"
        alpha = await upload(client, png_bytes(1280, 720, color_type=6))
        assert alpha.status_code == 400 and alpha.json()["error"]["code"] == "IMAGE_ALPHA_UNSUPPORTED"
        assert (await client.get("/api/assets")).json()["assets"] == []
        assert not (service.store.home / "assets").exists()


async def test_assets_dedupe_stay_local_and_report_a_lost_file(tmp_path):
    async with asset_app(tmp_path) as (service, client):
        data = png_bytes(1280, 720)
        first = (await upload(client, data)).json()
        again = (await upload(client, data)).json()
        assert again["assetId"] == first["assetId"] and again["deduplicated"] is True
        assert len(service.store.snapshot()["assets"]) == 1
        url = f"/api/assets/{first['assetId']}/content"
        for headers in (
            {"Host": "evil.test"},
            {"Origin": "https://evil.test"},
            {"Sec-Fetch-Site": "cross-site"},
        ):
            assert (await client.get(url, headers=headers)).status_code == 403
        path = service.store.home / service.store.snapshot()["assets"][first["assetId"]]["relativePath"]
        path.unlink()
        gone = await client.get(url)
        assert gone.status_code == 410 and gone.json()["error"]["code"] == "ASSET_UNAVAILABLE"
        view = (await client.get(f"/api/assets/{first['assetId']}")).json()
        assert view["available"] is False and view["contentUrl"] is None
        assert view["availability"]["reason"] == "missing"
        # Re-importing the same bytes is the documented repair: same identity, new file.
        repaired = (await upload(client, data)).json()
        assert repaired["assetId"] == first["assetId"]
        assert repaired["deduplicated"] is True and repaired["repaired"] is True
        assert (await client.get(url)).content == data


async def test_usage_endpoint_aggregates_without_external_calls(tmp_path):
    async with asset_app(tmp_path) as (service, client):
        asset = (await upload(client, png_bytes(1280, 720))).json()
        usage = (await client.get("/api/usage?projectId=coffee")).json()
        assert usage["projectId"] == "coffee"
        assert usage["assets"] == {"total": 1, "libraryTotal": 1}
        assert usage["jobs"]["total"] == 0 and usage["cost"]["estimateAmount"] == "0.00"
        everything = (await client.get("/api/usage")).json()
        assert everything["projectId"] is None and everything["assets"]["libraryTotal"] == 1
        empty = (await client.get("/api/usage?projectId=other")).json()
        assert empty["projectId"] == "other" and empty["assets"]["total"] == 0
        assert (await client.get("/api/usage?projectId=__proto__")).status_code == 400
        assert asset["assetId"]
