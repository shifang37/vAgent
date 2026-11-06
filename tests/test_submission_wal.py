"""The submission WAL survives a locked state.json and recovers the paid task ID."""

import errno

import httpx
import pytest
from conftest import ScriptedModel, video_run
from langchain_core.messages import AIMessage
from wan_support import live_arguments, live_run, live_service, provider_response

from vagent import storage
from vagent.application import ApplicationService
from vagent.config import Config
from vagent.submission_log import SubmissionLog
from vagent.video.worker import JobWorker


def generate(service, context, **changes):
    result = service.generate(live_arguments(**changes), context=context)
    assert result["ok"], result
    return service.get(result["data"]["jobId"])


async def test_handle_wal_survives_a_failed_state_write(store, video_clock, monkeypatch):
    context = live_run(store)
    posts = []

    def handler(request):
        posts.append(request)
        return httpx.Response(200, json=provider_response())

    async with live_service(store, video_clock, handler) as (service, _):
        job = generate(service, context)
        original = storage.atomic_write_json
        failed = []

        def flaky(path, value):
            if not failed and value.get("jobs", {}).get(job.id, {}).get("status") == "queued":
                failed.append(1)
                raise OSError(errno.ENOSPC, "simulated locked state.json")
            return original(path, value)

        monkeypatch.setattr(storage, "atomic_write_json", flaky)
        with pytest.raises(OSError):
            await JobWorker(service).run_once()
        assert failed and service.get(job.id).status == "submitting"
        assert SubmissionLog(store.home).handles()[job.id]["taskId"] == "wan-original"

    home = store.home
    store.close()
    with storage.FileStore.open(home) as reopened:
        recovered = reopened.snapshot()["jobs"][job.id]
        assert recovered["status"] == "queued"
        assert recovered["providerTaskId"] == "wan-original"
        assert recovered["providerSubmissionRequestId"] == "trace-original"
        assert recovered["queryState"] == "polling"
    assert len(posts) == 1


async def test_unknown_job_adopts_a_confirmed_handle_on_open(store, video_clock):
    context = live_run(store)

    def handler(_):
        raise httpx.ConnectError("the handle was lost before it could be committed")

    async with live_service(store, video_clock, handler) as (service, _):
        job = generate(service, context)
        result = await JobWorker(service).run_once()
        assert result.status == "unknown" and result.provider_task_id is None
        # The provider had accepted the job, but only the redundant handle record
        # survived; the queued state commit never happened.
        assert SubmissionLog(store.home).append_handle(job.id, "wan", "trace-original", "wan-original")

    home = store.home
    store.close()
    with storage.FileStore.open(home) as reopened:
        recovered = reopened.snapshot()["jobs"][job.id]
        assert recovered["status"] == "queued"
        assert recovered["providerTaskId"] == "wan-original"
        assert recovered["error"] is None


async def test_submission_without_a_handle_stays_unknown(store, video_clock):
    context = live_run(store)

    def handler(_):
        raise httpx.ConnectError("network down before the provider answered")

    async with live_service(store, video_clock, handler) as (service, _):
        job = generate(service, context)
        result = await JobWorker(service).run_once()
        assert result.status == "unknown" and result.provider_task_id is None

    entries = SubmissionLog(store.home).entries()[job.id]
    assert "intent" in entries and "handle" not in entries


async def test_a_missing_intent_blocks_the_paid_submission(store, video_clock, monkeypatch):
    context = live_run(store)
    posts = []

    def handler(request):
        posts.append(request)
        return httpx.Response(200, json=provider_response())

    async with live_service(store, video_clock, handler) as (service, _):
        generate(service, context)
        worker = JobWorker(service)
        monkeypatch.setattr(worker.submission_log, "append_intent", lambda *args, **kwargs: False)
        failed = await worker.run_once()
        assert failed.status == "failed"
        assert failed.error.code == "SUBMISSION_INTENT_UNRECORDED"
    assert posts == []


def test_compaction_keeps_only_recoverable_jobs(tmp_path):
    log = SubmissionLog(tmp_path)
    assert log.append_handle("queued-job", "wan", "r1", "t1")
    assert log.append_handle("done-job", "wan", "r2", "t2")
    log.compact({"jobs": {"queued-job": {"status": "queued"}, "done-job": {"status": "succeeded"}}})
    assert set(log.entries()) == {"queued-job"}


async def test_application_stops_a_stale_running_run_without_a_local_task(tmp_path):
    config = Config(home=tmp_path, api_key=None, video_mode="mock")
    model = ScriptedModel(lambda *_: AIMessage(content="unused"))
    async with ApplicationService.open(config, model=model) as service:
        await service.job_worker.stop()
        video_run(service.store, "stale-run")
        service.store.transaction(
            lambda draft: draft["runs"]["stale-run"].update(status="running", executionVersion=1)
        )
        result = service.stop("stale-run")
        assert result["stopRequested"] is True
        record = service.store.snapshot()["runs"]["stale-run"]
        assert record["status"] == "cancelled" and record["resumable"] is True
