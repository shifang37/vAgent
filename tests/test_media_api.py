import asyncio
import json

import httpx
import pytest
from job_support import eventually
from media_support import SAMPLE
from wan_support import live_arguments, live_config, live_run

from vagent.errors import AppError
from vagent.video.media_response import media_response
from vagent.web import create_app


@pytest.fixture
async def media_app(tmp_path, media_runtime):
    app = create_app(live_config(tmp_path))
    async with app.router.lifespan_context(app):
        service = app.state.service
        context = live_run(service.store)
        registered = service.video_jobs.generate(live_arguments(), context=context)
        job_id = registered["data"]["jobId"]
        await eventually(lambda: service.video_jobs.get(job_id).status == "succeeded")
        job = service.video_jobs.get(job_id)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:3210"
        ) as client:
            client.headers["X-CSRF-Token"] = (await client.get("/api/session-token")).json()["token"]
            yield service, client, job
    assert all(client.is_closed for client in media_runtime.clients)


async def test_full_get_head_download_and_private_projection(media_app):
    service, client, job = media_app
    media_id = job.download.media_id
    detail = await client.get(f"/api/media/{media_id}")
    assert detail.status_code == 200
    public = detail.json()
    assert public["mediaId"] == media_id and public["mediaAvailable"]
    assert public["metadata"]["width"] == 1280 and public["metadata"]["durationSeconds"] == 5
    for secret in ["relativePath", "workspaceId", "Signature", str(service.store.home)]:
        assert secret not in detail.text
    url = public["contentUrl"]
    full = await client.get(url)
    assert full.status_code == 200 and full.content == SAMPLE.read_bytes()
    assert full.headers["content-type"] == "video/mp4" and full.headers["accept-ranges"] == "bytes"
    assert full.headers["content-length"] == str(SAMPLE.stat().st_size)
    assert full.headers["x-content-type-options"] == "nosniff"
    assert "media-src 'self'" in full.headers["content-security-policy"]
    head = await client.head(url, headers={"Range": "bytes=999999-"})
    assert head.status_code == 200 and head.content == b""
    for name in ["content-length", "content-type", "etag", "accept-ranges"]:
        assert head.headers[name] == full.headers[name]
    attachment = await client.get(url + "?download=1")
    assert attachment.content == full.content
    assert attachment.headers["content-disposition"] == f'attachment; filename="{media_id}.mp4"'
    assert full.headers["content-disposition"].startswith("inline;")


@pytest.mark.parametrize(
    "header,expected",
    [
        ("bytes=0-15", (0, 15)),
        ("bytes=32-", (32, None)),
        ("bytes=-12", (-12, None)),
        ("bytes=209900-9999999", (209900, None)),
        ("bytes=-9999999999999999999999999", (0, None)),
        ("bytes=00001-00003", (1, 3)),
        ("bytes=9999999-", 416),
        ("bytes=-0", 416),
        ("bytes=9-1", 416),
        ("bytes=999999999999999999999999999999-", 416),
        ("items=0-15", None),
        ("bytes=0-1,3-4", None),
        ("bytes=-", None),
        ("bytes=x-y", None),
    ],
)
async def test_single_ranges_use_actual_bytes_and_lengths(media_app, header, expected):
    _, client, job = media_app
    data = SAMPLE.read_bytes()
    response = await client.get(f"/api/media/{job.download.media_id}/content", headers={"Range": header})
    if expected == 416:
        assert response.status_code == 416 and response.headers["content-range"] == f"bytes */{len(data)}"
        assert response.headers["content-length"] == "0" and not response.content
    elif expected is None:
        assert response.status_code == 200 and response.content == data
    else:
        start, end = expected
        start = start if start >= 0 else len(data) + start
        end = len(data) - 1 if end is None else end
        assert response.status_code == 206 and response.content == data[start : end + 1]
        assert response.headers["content-range"] == f"bytes {start}-{end}/{len(data)}"
        assert response.headers["content-length"] == str(end - start + 1)


async def test_if_range_only_accepts_current_strong_etag(media_app):
    _, client, job = media_app
    url = f"/api/media/{job.download.media_id}/content"
    tag = (await client.head(url)).headers["etag"]
    for value in [tag, '"outdated"', "W/" + tag, "Wed, 01 Jan 2020 00:00:00 GMT"]:
        response = await client.get(url, headers={"Range": "bytes=0-9", "If-Range": value})
        assert response.status_code == (206 if value == tag else 200)
        assert response.content == (SAMPLE.read_bytes()[:10] if value == tag else SAMPLE.read_bytes())


async def test_missing_and_corrupt_media_are_410_and_emit_revision_updates(media_app, media_runtime):
    service, client, job = media_app
    queue = service.subscribe("coffee")
    asset = service.media.asset(job.download.media_id)
    path = service.store.home / asset.relative_path
    history = service.store.snapshot()["runs"]
    for operation, reason in [
        (lambda: path.unlink(), "missing"),
        (lambda: path.write_bytes(b"not the media"), "hash_mismatch"),
    ]:
        operation()
        response = await client.get(f"/api/media/{asset.id}/content")
        assert response.status_code == 410 and response.json()["error"]["code"] == "JOB_MEDIA_UNAVAILABLE"
        event = await asyncio.wait_for(queue.get(), timeout=1)
        assert event["type"] == "job.updated" and not event["job"]["mediaAvailable"]
        assert event["job"]["mediaAvailability"]["reason"] == reason
        view = (await client.get(f"/api/media/{asset.id}")).json()
        assert view["contentUrl"] is None and not view["mediaAvailable"]
        current = (await client.get(f"/api/jobs/{job.id}")).json()
        assert current["status"] == "succeeded" and current["canRetryDownload"]
        assert current["result"] == job.result.model_dump(mode="json", by_alias=True)
    assert service.store.snapshot()["runs"] == history and len(media_runtime.submissions) == 1
    assert (await client.get("/api/media/absent/content")).status_code == 404
    assert (await client.head(f"/api/media/{asset.id}/content")).content == b""
    with pytest.raises(AppError) as caught:
        service.media.view(asset.id, project_id="another-project")
    assert caught.value.code == "MEDIA_NOT_FOUND"


async def test_retry_api_csrf_revision_replay_and_completed_run_stays_completed(media_app, media_runtime):
    service, client, job = media_app
    path = service.store.home / job.download.relative_path
    path.unlink()
    current = (await client.get(f"/api/jobs/{job.id}")).json()
    url = f"/api/jobs/{job.id}/retry-download"
    body = {"clientRequestId": "original-repair", "expectedRevision": current["revision"]}
    denied = await client.post(url, json=body, headers={"X-CSRF-Token": "wrong"})
    assert denied.status_code == 403
    media_runtime.gate = asyncio.Event()
    accepted = await client.post(url, json=body)
    assert accepted.status_code == 202 and accepted.json()["download"]["generation"] == 2
    assert (await client.post(url, json=body)).json() == accepted.json()
    changed = await client.post(url, json={**body, "expectedRevision": current["revision"] + 1})
    assert changed.status_code == 409 and changed.json()["error"]["code"] == "OPERATION_CONFLICT"
    stale = await client.post(url, json={**body, "clientRequestId": "stale"})
    assert stale.status_code == 409 and stale.json()["error"]["code"] == "JOB_REVISION_CONFLICT"
    await eventually(lambda: len(media_runtime.downloads) == 2)
    latest = (await client.get(f"/api/jobs/{job.id}")).json()
    duplicate = await client.post(
        url, json={"clientRequestId": "second-click", "expectedRevision": latest["revision"]}
    )
    assert duplicate.status_code == 202 and duplicate.json()["download"]["generation"] == 2
    media_runtime.gate.set()

    def finished():
        if service.media_worker._task.done():
            service.media_worker._task.result()
        current = service.video_jobs.get(job.id)
        assert current.download.phase not in {"failed"}, current.download.error
        return current.download.phase == "committed"

    await eventually(finished)
    assert service.video_jobs.get(job.id).result == job.result
    assert service.store.snapshot()["runs"][job.context.run_id]["status"] == "completed"
    assert len(media_runtime.submissions) == 1 and len(media_runtime.downloads) == 2
    assert path.read_bytes() == SAMPLE.read_bytes()


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"clientRequestId": "a", "expectedRevision": True},
        {"clientRequestId": "a", "expectedRevision": -1},
        {"clientRequestId": "a", "expectedRevision": 0, "url": "private"},
        {"clientRequestId": "__proto__", "expectedRevision": 0},
        {"clientRequestId": "constructor", "expectedRevision": 0},
        {"clientRequestId": "prototype", "expectedRevision": 0},
    ],
)
async def test_retry_body_is_strict(media_app, body):
    _, client, job = media_app
    response = await client.post(f"/api/jobs/{job.id}/retry-download", json=body)
    assert response.status_code == 422 and "private" not in response.text


async def test_media_api_uses_same_local_origin_boundary(media_app):
    _, client, job = media_app
    url = f"/api/media/{job.download.media_id}/content"
    for headers in [{"Host": "evil.test"}, {"Origin": "https://evil.test"}, {"Sec-Fetch-Site": "cross-site"}]:
        assert (await client.get(url, headers=headers)).status_code == 403


async def test_disconnect_closes_verified_content_handle(media_app):
    from starlette.requests import ClientDisconnect

    service, _, job = media_app
    asset, lease = service.media.open_content(job.download.media_id)
    response = media_response(asset, lease, method="GET", request_headers={})

    async def send(message):
        if message["type"] == "http.response.body":
            raise OSError("client disconnected")

    async def receive():
        return {"type": "http.disconnect"}

    with pytest.raises(ClientDisconnect):
        await response({"type": "http", "asgi": {"spec_version": "2.4"}}, receive, send)
    assert lease.file.closed


async def test_unknown_media_path_and_unavailable_retry_state(media_app):
    _, client, job = media_app
    for suffix in ["not-found", "..%2Fstate.json", "%2Fabsolute"]:
        response = await client.get(f"/api/media/{suffix}/content")
        assert response.status_code == 404
    result = await client.post(
        f"/api/jobs/{job.id}/retry-download",
        json={"clientRequestId": "unneeded", "expectedRevision": job.revision},
    )
    assert result.status_code == 409 and result.json()["error"]["code"] == "JOB_DOWNLOAD_RETRY_UNAVAILABLE"
    assert "Signature" not in json.dumps((await client.get("/api/jobs")).json())
