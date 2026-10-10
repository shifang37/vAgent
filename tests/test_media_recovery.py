import asyncio
import errno
import json
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path

import httpx
import pytest
from conftest import video_request, video_run
from media_support import SAMPLE, download_service

from vagent.storage import FileStore
from vagent.video.jobs import JobService
from vagent.video.media import DownloadRetry, MediaError
from vagent.video.media_http import MediaHttpClient
from vagent.video.media_worker import MediaWorker
from vagent.video.worker import JobWorker


@pytest.mark.parametrize(
    "stage,extra_downloads",
    [
        ("intent", 1),
        ("writing", 1),
        ("validated", 1),
        ("prepared", 0),
        ("renamed", 0),
        ("committed", 0),
    ],
)
async def test_real_process_exit_reuses_media_id_and_prepared_bytes(
    store, video_clock, stage, extra_downloads
):
    async with download_service(store, video_clock) as (_, job, submissions):
        identity = job.download.media_id
        assert len(submissions) == 1
    home = store.home
    store.close()
    worker_script = Path(__file__).with_name("media_crash_worker.py")
    process = subprocess.Popen(
        [sys.executable, str(worker_script), str(home), stage], stdout=subprocess.PIPE, stderr=subprocess.PIPE
    )
    stdout, stderr = process.communicate(timeout=20)
    assert process.returncode == 73, (stdout, stderr)
    lock = home / "instance.lock"
    # Windows venv launchers may proxy a child Python process with a different PID.
    assert json.loads(lock.read_text())["pid"] == int(stdout.decode().splitlines()[0])
    lock.unlink()  # The known fixture process has exited; simulate documented lock recovery.
    restored_downloads = []

    def handler(request):
        restored_downloads.append(request)
        return httpx.Response(200, content=SAMPLE.read_bytes())

    with FileStore.open(home) as restored:
        jobs = JobService(restored, [], clock=video_clock)
        pending = jobs.get(job.id)
        if pending.download.next_attempt_at:
            video_clock.value = max(video_clock(), datetime.fromisoformat(pending.download.next_attempt_at))
        async with MediaHttpClient(transport=httpx.MockTransport(handler)) as client:
            worker = MediaWorker(jobs.media, client)
            await worker.run_once()
            complete = jobs.get(job.id)
            assert complete.status == "succeeded" and complete.download.media_id == identity
            assert complete.submit_attempts == 1 and complete.provider_task_id == job.provider_task_id
            assert len(restored_downloads) == extra_downloads
            assert complete.download.attempts == (2 if stage in {"writing", "validated"} else 1)
            assert len(restored.snapshot()["media"]) == 1
            assert (home / complete.download.relative_path).read_bytes() == SAMPLE.read_bytes()
            assert await worker.run_once() is None
    with FileStore.open(home) as again:
        assert again.snapshot()["jobs"][job.id]["download"]["attempts"] == complete.download.attempts


async def test_index_write_failure_keeps_prepared_final_for_recovery(store, video_clock, monkeypatch):
    import vagent.storage as storage

    original = storage.atomic_write_json
    calls = []

    def fail_index(path, value):
        if value.get("media"):
            raise OSError(errno.EIO, "injected state write failure")
        return original(path, value)

    def handler(request):
        calls.append(request)
        return httpx.Response(200, content=SAMPLE.read_bytes())

    async with download_service(store, video_clock) as (jobs, job, _):
        async with MediaHttpClient(transport=httpx.MockTransport(handler)) as client:
            worker = MediaWorker(jobs.media, client)
            monkeypatch.setattr(storage, "atomic_write_json", fail_index)
            with pytest.raises(OSError):
                await worker.run_once()
            pending = jobs.get(job.id)
            assert pending.status == "downloading" and pending.download.phase == "prepared"
            assert not store.snapshot()["media"] and (store.home / pending.download.relative_path).exists()
            monkeypatch.setattr(storage, "atomic_write_json", original)
            assert (await worker.run_once()).status == "succeeded" and len(calls) == 1


@pytest.mark.parametrize(
    "error_number,code",
    [
        (errno.ENOSPC, "MEDIA_DISK_FULL"),
        (errno.EACCES, "MEDIA_PERMISSION_DENIED"),
        (errno.EIO, "MEDIA_IO_ERROR"),
    ],
)
async def test_disk_failures_stop_automatic_downloads(store, video_clock, monkeypatch, error_number, code):
    def fail(*_):
        raise OSError(error_number, "private local path")

    async with download_service(store, video_clock) as (jobs, job, submits):
        monkeypatch.setattr(jobs.media.files, "prepare", fail)
        async with MediaHttpClient(
            transport=httpx.MockTransport(lambda _: httpx.Response(200, content=SAMPLE.read_bytes()))
        ) as client:
            failed = await MediaWorker(jobs.media, client).run_once()
            assert failed.error.code == code and failed.status == "download_failed"
            assert failed.download.next_attempt_at is None and "private" not in str(failed.error)
            assert len(submits) == 1


async def test_unknown_final_file_is_preserved_without_http(store, video_clock):
    def handler(_):
        pytest.fail("A conflicting local final file must not be overwritten")

    async with download_service(store, video_clock) as (jobs, job, _):
        path = store.home / job.download.relative_path
        path.parent.mkdir(parents=True)
        path.write_bytes(b"evidence")
        async with MediaHttpClient(transport=httpx.MockTransport(handler)) as client:
            failed = await MediaWorker(jobs.media, client).run_once()
            assert failed.error.code == "MEDIA_FILE_CONFLICT" and path.read_bytes() == b"evidence"


async def test_repair_rejects_changed_bytes_and_retains_historical_result(store, video_clock):
    data = SAMPLE.read_bytes()

    async with download_service(store, video_clock) as (jobs, job, submits):
        async with MediaHttpClient(
            transport=httpx.MockTransport(lambda _: httpx.Response(200, content=data))
        ) as client:
            worker = MediaWorker(jobs.media, client)
            original = await worker.run_once()
            asset = jobs.media.asset(job.download.media_id)
            (store.home / asset.relative_path).unlink()
            missing = jobs.media.refresh(original)
            jobs.media.retry_download(
                job.id,
                DownloadRetry(client_request_id="repair", expected_revision=missing.revision),
                project_id="coffee",
            )
            data = data[:-1] + bytes([data[-1] ^ 1])
            failed = await worker.run_once()
            assert failed.status == "succeeded" and failed.result == original.result
            assert failed.download.error.code == "MEDIA_HASH_MISMATCH" and failed.download.phase == "failed"
            assert failed.media_availability.status == "unavailable" and jobs.media.asset(asset.id) == asset
            assert len(submits) == 1


async def test_download_does_not_hold_store_or_job_worker_and_cancel_closes_files(store, video_clock):
    entered, closed = asyncio.Event(), asyncio.Event()

    class Stream(httpx.AsyncByteStream):
        async def __aiter__(self):
            entered.set()
            await asyncio.Event().wait()
            yield b""

        async def aclose(self):
            closed.set()

    async with download_service(store, video_clock) as (jobs, job, _):
        async with MediaHttpClient(
            transport=httpx.MockTransport(lambda _: httpx.Response(200, stream=Stream()))
        ) as client:
            worker = MediaWorker(jobs.media, client, idle_interval_seconds=0.005)
            worker.start()
            await asyncio.wait_for(entered.wait(), timeout=2)
            context = video_run(store, "parallel-job", project_id="second")
            adapter = jobs._adapters[("mock", "mock-t2v")]
            second = jobs.generate(video_request(adapter), context=context)
            assert second["ok"]
            progressed = await asyncio.wait_for(JobWorker(jobs).run_once(), timeout=1)
            assert progressed.status == "queued" and progressed.context.project_id == "second"
            await worker.stop()
            assert closed.is_set() and jobs.get(job.id).download.attempts == 1
            assert jobs.get(job.id).download.phase == "writing"
            part = store.home / (job.download.relative_path + ".part")
            part.unlink()  # Also proves no leaked Windows file handle after cancellation.


def test_media_path_rejects_escaping_paths(store):
    files = JobService(store, []).media.files
    for relative in [
        "../escape.mp4",
        "media/../../escape.mp4",
        "media/coffee/no-uuid.mp4",
        "media\\coffee\\file.mp4",
        "/absolute.mp4",
    ]:
        with pytest.raises(MediaError):
            files.open(relative, writing=True)


def test_media_path_rejects_directory_symlink(store, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    root = store.home / "media"
    try:
        root.symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("Current Windows account cannot create symbolic links")
    files = JobService(store, []).media.files
    with pytest.raises((MediaError, OSError)):
        files.open("media/coffee/00000000-0000-0000-0000-000000000001.mp4.part", writing=True)
    assert not list(outside.iterdir())


@pytest.mark.skipif(os.name != "nt", reason="Windows reparse point guard")
def test_windows_junction_cannot_escape_media_root(store, tmp_path):
    outside = tmp_path / "junction-outside"
    outside.mkdir()
    junction = store.home / "media"
    # Test-only junction creation, with literal paths; no recursive shell deletion.
    result = subprocess.run(["cmd", "/c", "mklink", "/J", str(junction), str(outside)], capture_output=True)
    if result.returncode:
        pytest.skip("Junction creation is unavailable")
    try:
        with pytest.raises(MediaError):
            JobService(store, []).media.files.open(
                "media/coffee/00000000-0000-0000-0000-000000000001.mp4.part", writing=True
            )
        assert not list(outside.iterdir())
    finally:
        junction.rmdir()
