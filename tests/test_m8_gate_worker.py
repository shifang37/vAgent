"""M8 E2: the Worker honours the submit gate without ever resubmitting.

Every case runs against a controlled Kling transport with an injected clock, so the
2.0-second interval and the 429 cooldown are observed exactly. A blocked submit must
keep ``pending_submit`` and ``submitAttempts=0``, must send nothing, and must leave the
Job's frozen fields untouched; only a real submit consumes the one submission.
"""

import json
from datetime import UTC, timedelta

import httpx
from m3_support import kling_service
from wan_support import live_run

from vagent.video.contracts import KLING_MODEL
from vagent.video.providers.gate import gate_for, metrics_for
from vagent.video.usage import summarize_records
from vagent.video.worker import JobWorker


def kling_request():
    return {
        "provider": "kling",
        "model": KLING_MODEL,
        "capabilitiesVersion": "multi-provider-v3",
        "prompt": "雨夜咖啡店",
        "spec": {"durationSeconds": 5, "resolution": "720p", "aspectRatio": "16:9"},
    }


def register(service, store, run_id):
    result = service.generate(kling_request(), context=live_run(store, run_id))
    assert result["ok"], result
    return result["data"]["jobId"]


def stored(store, job_id):
    return json.loads((store.home / "state.json").read_bytes())["jobs"][job_id]


async def test_a_proven_429_cools_down_the_provider_and_never_resubmits(store, video_clock):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(
            429, headers={"Retry-After": "120"}, json={"code": 1302, "message": "rate limited"}
        )

    async with kling_service(store, video_clock, handler) as (service, _):
        first = register(service, store, "run-1")
        worker = JobWorker(service)
        failed = await worker.run_once()
        assert failed.id == first
        assert failed.status == "failed" and failed.error.code == "PROVIDER_RATE_LIMITED"
        # A proven rejection is terminal: one submission, no upstream ID, no retry.
        assert failed.submit_attempts == 1 and failed.provider_task_id is None
        assert failed.error.http_status == 429
        assert len(calls) == 1

        # Retry-After (120 s) extends the frozen 60 s default, never shortens it.
        gate = gate_for(store)
        blocked_decision = gate.check("kling")
        assert blocked_decision.allowed is False and blocked_decision.reason == "cooldown"
        expected = video_clock.value + timedelta(seconds=120)
        assert blocked_decision.retry_at == expected.astimezone(UTC).isoformat()

        second = register(service, store, "run-2")
        video_clock.advance(2)
        backoff = await worker.run_once()
        assert backoff.id == second
        assert backoff.status == "pending_submit" and backoff.submit_attempts == 0
        assert backoff.runtime_block is not None
        assert backoff.runtime_block.stage == "submit"
        assert backoff.runtime_block.code == "PROVIDER_BACKOFF"
        assert len(calls) == 1  # Nothing was sent while the cooldown held.

        # The blocked Job is durably diagnosable and still has no submission.
        raw = stored(store, second)
        assert raw["status"] == "pending_submit" and raw["submitAttempts"] == 0
        assert raw["runtimeBlock"]["code"] == "PROVIDER_BACKOFF"
        assert raw["providerTaskId"] is None and raw["submissionStartedAt"] is None

        video_clock.advance(200)
        submitted = await worker.run_once()
        assert submitted.id == second and submitted.submit_attempts == 1
        assert submitted.status == "failed" and submitted.error.code == "PROVIDER_RATE_LIMITED"
        # The diagnostic is cleared by the next real submission.
        assert submitted.runtime_block is None
        assert len(calls) == 2


async def test_the_submit_interval_delays_the_second_job_of_a_provider(store, video_clock):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(401, json={"code": 1001, "message": "invalid key"})

    async with kling_service(store, video_clock, handler) as (service, _):
        first = register(service, store, "run-1")
        video_clock.advance(1)
        second = register(service, store, "run-2")
        video_clock.advance(1)
        worker = JobWorker(service)

        done = await worker.run_once()
        assert done.id == first and done.status == "failed"
        assert done.error.code == "PROVIDER_AUTH_FAILED"
        assert len(calls) == 1

        # 1.5 s later the global 1 s interval is satisfied but the 2 s one is not.
        video_clock.advance(1.5)
        backoff = await worker.run_once()
        assert backoff.id == second and backoff.status == "pending_submit"
        assert backoff.submit_attempts == 0
        assert backoff.runtime_block.code == "PROVIDER_BACKOFF"
        assert "间隔" in backoff.runtime_block.message
        assert len(calls) == 1

        video_clock.advance(0.5)
        submitted = await worker.run_once()
        assert submitted.id == second and submitted.submit_attempts == 1
        assert len(calls) == 2


async def test_a_query_side_429_never_opens_a_submit_cooldown(store, video_clock):
    calls = []

    def handler(request):
        calls.append(request)
        if request.method == "POST":
            return httpx.Response(200, json={"code": 0, "data": {"id": "kling-task-1"}})
        return httpx.Response(429, headers={"Retry-After": "30"}, json={"code": 1302})

    async with kling_service(store, video_clock, handler) as (service, _):
        job_id = register(service, store, "run-1")
        worker = JobWorker(service)
        accepted = await worker.run_once()
        assert accepted.status == "queued" and accepted.provider_task_id == "kling-task-1"

        video_clock.advance(20)
        queried = await worker.run_once()
        assert queried.id == job_id and queried.query_state == "retrying"
        # §5.1 only: a 429 while querying backs off the query, not the submit gate.
        assert gate_for(store).check("kling").allowed is True
        assert metrics_for(store).snapshot()["rateLimitHits"] == 0
        assert len(calls) == 2


async def test_usage_reports_runtime_metrics_as_non_durable(store, video_clock):
    def handler(request):
        return httpx.Response(429, json={"code": 1302})

    async with kling_service(store, video_clock, handler) as (service, _):
        register(service, store, "run-1")
        worker = JobWorker(service)
        await worker.run_once()
        video_clock.advance(2)
        register(service, store, "run-2")
        await worker.run_once()

        snapshot = metrics_for(store).snapshot()
        assert snapshot["durable"] is False
        assert snapshot["rateLimitHits"] == 1
        assert snapshot["backoffCount"] == 1
        assert snapshot["backoffByProvider"]["kling"] == 1
        assert snapshot["submitLatencyMs"]["samples"] == 1
        assert snapshot["submitLatencyMs"]["p50"] >= 0.0

        report = summarize_records(store.snapshot(), home=store.home, runtime=snapshot)
        assert report["runtime"]["durable"] is False
        assert "runtime" not in summarize_records(store.snapshot(), home=store.home)
        # No bill was entered: the fee stays unknown (never zero) in its own currency.
        assert report["billing"]["ledgerStatus"] == "missing"
        assert report["billing"]["currencies"] == [
            {
                "currency": "USD",
                "estimate": "1.12",
                "reported": None,
                "reportedJobs": 0,
                "unknownJobs": 2,
                "delta": None,
            }
        ]
        assert report["billing"]["unknownJobs"] == 2
        assert any("不换汇" in note for note in report["billing"]["notes"])


async def test_a_failed_job_still_consumes_the_single_recorded_submission(store, video_clock):
    """M1-C invariant: the gate never turns a terminal submit into a second attempt."""

    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(403, json={"code": 1004, "message": "no permission"})

    async with kling_service(store, video_clock, handler) as (service, _):
        job_id = register(service, store, "run-1")
        worker = JobWorker(service)
        failed = await worker.run_once()
        assert failed.status == "failed" and failed.submit_attempts == 1
        video_clock.advance(300)
        assert await worker.run_once() is None
        assert len(calls) == 1
        assert stored(store, job_id)["status"] == "failed"
        assert stored(store, job_id)["submitAttempts"] == 1
