import hashlib
import json

import httpx
import pytest
from media_support import SAMPLE, download_service

from vagent.errors import AppError
from vagent.storage import FileStore
from vagent.video.media import DownloadRetry
from vagent.video.media_http import MediaHttpClient
from vagent.video.media_worker import MediaWorker


async def test_complete_local_delivery_and_missing_file_repair(store, video_clock):
    data = SAMPLE.read_bytes()
    downloads = []

    def handler(request):
        downloads.append(request)
        return httpx.Response(200, content=data, headers={"Content-Type": "video/mp4"})

    async with download_service(store, video_clock) as (jobs, job, submissions):
        original = job.download.media_id
        async with MediaHttpClient(transport=httpx.MockTransport(handler)) as client:
            worker = MediaWorker(jobs.media, client)
            completed = await worker.run_once()
            assert completed.status == "succeeded" and completed.media_availability.status == "available"
            assert completed.download.attempts == 1 and completed.download.phase == "committed"
            assert completed.result.media_refs[0].media_id == original
            asset = jobs.media.asset(original)
            assert asset.sha256 == hashlib.sha256(data).hexdigest()
            assert asset.size_bytes == len(data) and asset.metadata.width == 1280
            assert asset.metadata.has_audio and asset.metadata.duration_seconds == 5
            file = store.home / asset.relative_path
            assert file.read_bytes() == data and not file.with_suffix(".mp4.part").exists()
            assert await worker.run_once() is None
            revision = completed.revision
            assert jobs.media.refresh(completed).revision == revision
            history = completed.result
            saved_asset = asset
            file.unlink()
            missing = jobs.media.refresh(completed)
            assert missing.revision == revision + 1 and missing.status == "succeeded"
            assert missing.result == history and missing.media_availability.reason == "missing"
            request = DownloadRetry(client_request_id="repair-1", expected_revision=missing.revision)
            accepted = jobs.media.retry_download(job.id, request, project_id="coffee")
            assert accepted["download"]["repair"] and accepted["download"]["generation"] == 2
            assert jobs.media.retry_download(job.id, request, project_id="coffee") == accepted
            repaired = await worker.run_once()
            assert repaired.status == "succeeded" and repaired.media_availability.status == "available"
            assert repaired.result == history and jobs.media.asset(original) == saved_asset
            assert repaired.download.attempts == 2 and repaired.download.window_attempts == 1
            assert len(downloads) == 2 and len(submissions) == 1 and len(store.snapshot()["media"]) == 1
            assert all("authorization" not in r.headers and "cookie" not in r.headers for r in downloads)
            assert "Signature" not in json.dumps(jobs.media.view(original))


async def test_network_window_exhaustion_is_durable_and_retry_is_idempotent(store, video_clock):
    def handler(_):
        raise httpx.ReadError("private remote details")

    async with download_service(store, video_clock) as (jobs, job, submissions):
        async with MediaHttpClient(transport=httpx.MockTransport(handler)) as client:
            worker = MediaWorker(jobs.media, client)
            for attempt, delay in ((1, 5), (2, 30), (3, None)):
                current = await worker.run_once()
                assert current.download.attempts == attempt
                assert "private" not in current.download.error.message
                if delay:
                    assert current.download.phase == "pending"
                    assert await worker.run_once() is None
                    video_clock.advance(delay)
            assert current.status == "download_failed" and current.download.next_attempt_at is None
            assert await worker.run_once() is None
            request = DownloadRetry(client_request_id="retry", expected_revision=current.revision)
            first = jobs.media.retry_download(job.id, request, project_id="coffee")
            assert jobs.media.retry_download(job.id, request, project_id="coffee") == first
            active = jobs.get(job.id)
            other = jobs.media.retry_download(
                job.id,
                DownloadRetry(
                    client_request_id="another",
                    expected_revision=active.revision,
                ),
                project_id="coffee",
            )
            assert other["download"]["generation"] == 2 and other["download"]["attempts"] == 3
            with pytest.raises(AppError, match="同一调用") as caught:
                jobs.media.retry_download(
                    job.id,
                    DownloadRetry(
                        client_request_id="retry",
                        expected_revision=active.revision,
                    ),
                    project_id="coffee",
                )
            assert caught.value.code == "OPERATION_CONFLICT"
            assert len(submissions) == 1
    home = store.home
    store.close()
    with FileStore.open(home) as reopened:
        record = reopened.snapshot()["jobs"][job.id]
        assert record["download"]["attempts"] == 3 and record["download"]["generation"] == 2
