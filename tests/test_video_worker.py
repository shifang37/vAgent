import asyncio
import json
import os
import subprocess
import sys

import pytest
from conftest import video_request, video_run

from vagent.errors import AppError
from vagent.storage import FileStore
from vagent.video.contracts import (
    JobResult,
    PollingPolicy,
    ProviderTaskHandle,
    ProviderTaskSnapshot,
    VideoCapabilities,
    VideoRequest,
)
from vagent.video.jobs import JobService
from vagent.video.providers.mock import MockScenario, MockVideoAdapter
from vagent.video.worker import JobWorker


def setup_job(store, clock, *, scenario=None, provider_type=MockVideoAdapter, policy=None):
    context = video_run(store)
    adapter = provider_type(store, clock=clock, scenario=scenario)
    service = JobService(store, [adapter], clock=clock, policy=policy)
    outcome = service.generate(video_request(adapter), context=context)
    assert outcome["ok"]
    return service, adapter, JobWorker(service), outcome["data"]["jobId"]


async def test_worker_completes_across_restart_without_model_or_resubmission(store, video_clock):
    service, adapter, worker, job_id = setup_job(store, video_clock)
    original_run = store.snapshot()["runs"]["video-run"]
    registration = store.snapshot()["operations"].copy()
    queued = await worker.run_once()
    assert queued.status == "queued" and queued.submit_attempts == 1
    assert queued.query_attempts == 0
    assert await worker.run_once() is None
    video_clock.due(queued)
    running = await worker.run_once()
    assert running.status == "running" and running.query_attempts == 1
    original_ledger = adapter.ledger_snapshot()
    home = store.home
    store.close()
    with FileStore.open(home) as reopened:
        # Changed fixture/default policy affects future submissions only.
        adapter = MockVideoAdapter(reopened, scenario=MockScenario(submission="rejected"), clock=video_clock)
        resumed = JobService(
            reopened, [adapter], clock=video_clock, policy=PollingPolicy(interval_seconds=99)
        )
        worker = JobWorker(resumed)
        assert adapter.ledger_snapshot() == original_ledger
        assert await worker.run_once() is None
        video_clock.due(running)
        final = await worker.run_once()
        assert final.id == job_id and final.status == "succeeded"
        assert final.provider_task_id == queued.provider_task_id
        assert final.query_attempts == 2 and final.policy.interval_seconds == 2
        assert final.result.simulated and not final.result.media_available
        assert final.result.artifact_refs == ()
        assert final.result.request_fingerprint == final.request_fingerprint
        assert final.result.spec == final.request.spec
        assert final.result.source_refs == final.request.source_refs
        assert reopened.snapshot()["operations"] == registration
        assert reopened.snapshot()["runs"]["video-run"] == original_run
        assert reopened.snapshot()["artifacts"] == {}
        ledger = adapter.ledger_snapshot()
        assert ledger["submitCalls"] == 1 and ledger["queryCalls"] == 2
        assert await worker.run_once() is None


@pytest.mark.parametrize(
    "scenario,status,stage,accepted,queries",
    [
        (MockScenario(states=["succeeded"]), "succeeded", None, True, 0),
        (MockScenario(states=["failed"]), "failed", "generate", True, 0),
        (MockScenario(states=["queued", "failed"]), "failed", "generate", True, 1),
        (MockScenario(submission="rejected"), "failed", "submit", False, 0),
        (MockScenario(submission="response_lost"), "unknown", "submit", True, 0),
    ],
)
async def test_confirmed_failure_and_submission_uncertainty_are_distinct(
    store, video_clock, scenario, status, stage, accepted, queries
):
    service, adapter, worker, job_id = setup_job(store, video_clock, scenario=scenario)
    final = await worker.run_once()
    if queries:
        video_clock.due(final)
        final = await worker.run_once()
    assert final.status == status
    assert (final.error.stage if final.error else None) == stage
    assert bool(final.provider_task_id) == (accepted and status != "unknown")
    assert final.submit_attempts == 1 and final.query_attempts == queries
    assert len(adapter.ledger_snapshot()["tasks"]) == int(accepted)
    home = store.home
    store.close()
    with FileStore.open(home) as reopened:
        provider = MockVideoAdapter(reopened, clock=video_clock)
        service = JobService(reopened, [provider], clock=video_clock)
        video_clock.advance(100)
        assert await JobWorker(service).run_once() is None
        assert service.get(job_id, project_id="coffee") == final
        assert provider.ledger_snapshot()["submitCalls"] == 1
        with pytest.raises(AppError) as error:
            service.retry_query(job_id, project_id="coffee")
        assert error.value.code == "JOB_QUERY_NOT_PAUSED"


async def test_query_retry_window_survives_restart_and_manual_retry_keeps_original_id(store, video_clock):
    scenario = MockScenario(query_error_calls=[2, 3, 4, 5])
    service, adapter, worker, job_id = setup_job(store, video_clock, scenario=scenario)
    queued = await worker.run_once()
    video_clock.due(queued)
    running = await worker.run_once()
    original_id = running.provider_task_id
    home = store.home
    current_store = store
    try:
        for count, delay in enumerate([1, 2, 4, None], start=1):
            video_clock.due(running)
            before_time = video_clock().timestamp()
            running = await worker.run_once()
            assert running.status == "running" and running.provider_task_id == original_id
            assert running.error.stage == "query"
            assert running.consecutive_query_errors == count
            assert running.query_attempts == 1 + count
            if delay is not None:
                assert running.query_state == "retrying"
                video_clock.due(running)
                assert video_clock().timestamp() - before_time == delay
            else:
                assert running.query_state == "paused" and running.next_poll_at is None
            current_store.close()
            current_store = FileStore.open(home)
            adapter = MockVideoAdapter(current_store, clock=video_clock)
            service = JobService(current_store, [adapter], clock=video_clock)
            worker = JobWorker(service)
            assert service.get(job_id, project_id="coffee") == running
        assert await worker.run_once() is None
        retried = service.retry_query(job_id, project_id="coffee")
        assert retried.query_attempts == 5 and retried.consecutive_query_errors == 0
        assert retried.provider_task_id == original_id and retried.submit_attempts == 1
        final = await worker.run_once()
        assert final.status == "succeeded" and final.query_attempts == 6
        assert final.provider_task_id == original_id
        assert adapter.ledger_snapshot()["submitCalls"] == 1
        assert adapter.ledger_snapshot()["queryCalls"] == 6
    finally:
        current_store.close()


async def test_successful_query_resets_only_the_error_window(store, video_clock):
    service, adapter, worker, _ = setup_job(
        store, video_clock, scenario=MockScenario(query_error_calls=[1, 3])
    )
    job = await worker.run_once()
    for attempt, errors in [(1, 1), (2, 0), (3, 1), (4, 0)]:
        video_clock.due(job)
        job = await worker.run_once()
        assert job.query_attempts == attempt and job.consecutive_query_errors == errors
    assert job.status == "succeeded" and adapter.ledger_snapshot()["submitCalls"] == 1


@pytest.mark.parametrize("stage", ["submit", "query"])
async def test_timeouts_are_bounded_and_never_reclassify_query_failure_as_generation_failure(
    store, video_clock, stage
):
    class Stalled(MockVideoAdapter):
        async def submit(self, request, operation_key):
            if stage == "submit":
                await asyncio.Event().wait()
            return await super().submit(request, operation_key)

        async def query(self, task_id):
            await asyncio.Event().wait()

    policy = PollingPolicy(submit_timeout_seconds=0.01, query_timeout_seconds=0.01, retry_delays_seconds=[])
    service, adapter, worker, _ = setup_job(store, video_clock, provider_type=Stalled, policy=policy)
    job = await worker.run_once()
    if stage == "submit":
        assert job.status == "unknown" and job.error.stage == "submit"
    else:
        video_clock.due(job)
        job = await worker.run_once()
        assert job.status == "queued" and job.query_state == "paused"
        assert job.query_attempts == 1 and job.error.code == "QUERY_TIMEOUT"
        assert job.provider_task_id is not None
    assert await worker.run_once() is None


async def test_worker_stop_cancels_submission_and_leaves_other_jobs_pending(store, video_clock):
    entered = asyncio.Event()

    class AcceptedButStalled(MockVideoAdapter):
        async def submit(self, request, operation_key):
            handle = await super().submit(request, operation_key)
            # A different thread can acquire the Store while the provider is active.
            await asyncio.wait_for(asyncio.to_thread(store.ensure_session, "parallel-read"), timeout=2)
            entered.set()
            await asyncio.Event().wait()
            return handle

    service, adapter, worker, job_id = setup_job(store, video_clock, provider_type=AcceptedButStalled)
    other = video_run(store, "later-run")
    # Register after a clock tick so the original submission is selected first.
    video_clock.advance(0.1)
    second = service.generate(video_request(adapter), context=other)["data"]["jobId"]
    first_task = worker.start()
    assert worker.start() is first_task
    await asyncio.wait_for(entered.wait(), timeout=3)
    await worker.stop()
    assert first_task.done()
    assert service.get(job_id, project_id="coffee").status == "unknown"
    assert service.get(second, project_id="coffee").status == "pending_submit"
    assert adapter.ledger_snapshot()["submitCalls"] == 1
    assert await worker.run_once() is None


async def test_worker_stop_records_interrupted_query_once(store, video_clock):
    entered = asyncio.Event()

    class StalledQuery(MockVideoAdapter):
        async def query(self, task_id):
            await super().query(task_id)
            entered.set()
            await asyncio.Event().wait()

    service, adapter, worker, job_id = setup_job(store, video_clock, provider_type=StalledQuery)
    queued = await worker.run_once()
    video_clock.due(queued)
    worker.start()
    await asyncio.wait_for(entered.wait(), timeout=2)
    await worker.stop()
    job = service.get(job_id, project_id="coffee")
    assert job.status == "queued" and job.query_state == "retrying"
    assert job.query_attempts == 1 and job.consecutive_query_errors == 1
    assert job.query_started_at is None and job.error.code == "QUERY_INTERRUPTED"
    home = store.home
    store.close()
    with FileStore.open(home) as reopened:
        adapter = MockVideoAdapter(reopened, clock=video_clock)
        service = JobService(reopened, [adapter], clock=video_clock)
        assert service.get(job_id, project_id="coffee") == job
        video_clock.due(job)
        final = await JobWorker(service).run_once()
        assert final.status == "succeeded" and final.query_attempts == 2
        assert adapter.ledger_snapshot()["submitCalls"] == 1


@pytest.mark.parametrize("stage", ["submit", "query"])
async def test_unclassified_provider_errors_never_store_raw_exception_text(store, video_clock, stage):
    secret = "Bearer secret-provider-body"

    class Broken(MockVideoAdapter):
        async def submit(self, request, operation_key):
            handle = await super().submit(request, operation_key)
            if stage == "submit":
                raise RuntimeError(secret)
            return handle

        async def query(self, task_id):
            raise RuntimeError(secret)

    service, _, worker, _ = setup_job(store, video_clock, provider_type=Broken)
    job = await worker.run_once()
    if stage == "query":
        video_clock.due(job)
        job = await worker.run_once()
        assert job.status == "queued" and job.error.stage == "query"
    else:
        assert job.status == "unknown"
    assert secret not in json.dumps(store.snapshot())


@pytest.mark.parametrize("response", ["wrong_id", "wrong_result", "malformed"])
async def test_invalid_query_response_preserves_original_task_and_confirmed_state(
    store, video_clock, response
):
    class InvalidQuery(MockVideoAdapter):
        async def query(self, task_id):
            if response == "malformed":
                return {"status": "succeeded"}
            if response == "wrong_id":
                return ProviderTaskSnapshot(task_id="unrelated", status="running")
            request = VideoRequest.model_validate(video_request(self, prompt="different"))
            return ProviderTaskSnapshot(
                task_id=task_id,
                status="succeeded",
                result=JobResult(
                    request_fingerprint=request.fingerprint(), spec=request.spec, summary="wrong"
                ),
            )

    service, _, worker, _ = setup_job(store, video_clock, provider_type=InvalidQuery)
    queued = await worker.run_once()
    video_clock.due(queued)
    failed_query = await worker.run_once()
    assert failed_query.status == queued.status
    assert failed_query.provider_task_id == queued.provider_task_id
    assert failed_query.error.code == "QUERY_INVALID_RESPONSE"
    assert failed_query.result is None and failed_query.submit_attempts == 1


async def test_valid_submit_handle_survives_an_invalid_optional_result(store, video_clock):
    class InvalidInitialResult(MockVideoAdapter):
        async def submit(self, request, operation_key):
            handle = await super().submit(request, operation_key)
            return ProviderTaskHandle(
                task_id=handle.task_id,
                snapshot=ProviderTaskSnapshot(
                    task_id=handle.task_id,
                    status="succeeded",
                    result=JobResult(request_fingerprint="0" * 64, spec=request.spec, summary="invalid"),
                ),
            )

    service, provider, worker, _ = setup_job(
        store, video_clock, provider_type=InvalidInitialResult, scenario=MockScenario(states=["succeeded"])
    )
    queued = await worker.run_once()
    assert queued.status == "queued" and queued.provider_task_id is not None
    video_clock.due(queued)
    final = await worker.run_once()
    assert final.status == "succeeded" and final.provider_task_id == queued.provider_task_id
    assert provider.ledger_snapshot()["submitCalls"] == 1


async def test_repeated_ticks_are_serial_and_other_jobs_are_not_starved(store, video_clock):
    scenario = MockScenario(query_error_calls=[1, 2, 3, 4])
    service, provider, worker, first_id = setup_job(store, video_clock, scenario=scenario)
    other = video_run(store, "second-run")
    video_clock.advance(0.1)
    second_id = service.generate(video_request(provider), context=other)["data"]["jobId"]
    results = await asyncio.gather(*(worker.run_once() for _ in range(6)))
    assert {j.id for j in results if j is not None} == {first_id, second_id}
    assert provider.ledger_snapshot()["submitCalls"] == 2
    video_clock.advance(2)
    updated = [await worker.run_once(), await worker.run_once()]
    assert {job.id for job in updated} == {first_id, second_id}
    assert all(job.query_attempts == 1 and job.query_state == "retrying" for job in updated)
    assert provider.ledger_snapshot()["queryCalls"] == 2


async def test_worker_instances_share_the_single_store_execution_boundary(store, video_clock):
    entered, release = asyncio.Event(), asyncio.Event()
    active, peak = 0, 0

    class Tracked(MockVideoAdapter):
        async def submit(self, request, operation_key):
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            try:
                entered.set()
                await release.wait()
                return await super().submit(request, operation_key)
            finally:
                active -= 1

    service, provider, first_worker, _ = setup_job(store, video_clock, provider_type=Tracked)
    second_context = video_run(store, "second-run")
    service.generate(video_request(provider), context=second_context)
    second_worker = JobWorker(JobService(store, [provider], clock=video_clock))
    first = asyncio.create_task(first_worker.run_once())
    await asyncio.wait_for(entered.wait(), timeout=2)
    second = asyncio.create_task(second_worker.run_once())
    await asyncio.sleep(0)
    release.set()
    results = await asyncio.gather(first, second)
    assert peak == 1 and {result.status for result in results} == {"queued"}
    assert provider.ledger_snapshot()["submitCalls"] == 2


async def test_missing_old_adapter_does_not_block_other_jobs(store, video_clock):
    old_context = video_run(store, "old-run")
    default = MockVideoAdapter(store, clock=video_clock)
    old_capabilities = default.capabilities().model_dump(mode="json", by_alias=True)
    old_capabilities["model"] = "older-model"
    old = MockVideoAdapter(
        store, clock=video_clock, capabilities=VideoCapabilities.model_validate(old_capabilities)
    )
    old_service = JobService(store, [old], clock=video_clock)
    old_id = old_service.generate(video_request(old), context=old_context)["data"]["jobId"]
    video_clock.advance(1)
    current_context = video_run(store, "current-run")
    current = JobService(store, [default], clock=video_clock)
    current_id = current.generate(video_request(default), context=current_context)["data"]["jobId"]
    queued = await JobWorker(current).run_once()
    assert queued.id == current_id
    assert current.get(old_id, project_id="coffee").status == "pending_submit"
    assert default.ledger_snapshot()["submitCalls"] == 1


async def test_regressive_query_snapshot_cannot_roll_back_running_job(store, video_clock):
    class Regressive(MockVideoAdapter):
        calls = 0

        async def query(self, task_id):
            self.calls += 1
            actual = await super().query(task_id)
            if self.calls == 2:
                return ProviderTaskSnapshot(task_id=task_id, status="queued")
            return actual

    _, provider, worker, _ = setup_job(store, video_clock, provider_type=Regressive)
    job = await worker.run_once()
    for expected in ["running", "running", "succeeded"]:
        video_clock.due(job)
        job = await worker.run_once()
        assert job.status == expected
    assert provider.ledger_snapshot()["submitCalls"] == 1


@pytest.mark.parametrize("stage", ["submit", "result"])
async def test_result_commit_failure_leaves_durable_intent_for_recovery(
    store, video_clock, monkeypatch, stage
):
    import vagent.storage

    service, provider, worker, job_id = setup_job(store, video_clock)
    if stage == "result":
        queued = await worker.run_once()
        video_clock.due(queued)
        running = await worker.run_once()
        video_clock.due(running)
    original_write = vagent.storage.atomic_write_json

    def fail(path, state):
        status = state["jobs"][job_id]["status"]
        if status == ("queued" if stage == "submit" else "succeeded"):
            raise OSError("injected response commit failure")
        return original_write(path, state)

    monkeypatch.setattr(vagent.storage, "atomic_write_json", fail)
    with pytest.raises(OSError):
        await worker.run_once()
    before = service.get(job_id, project_id="coffee")
    assert before.status == ("submitting" if stage == "submit" else "running")
    assert before.result is None
    if stage == "result":
        assert before.query_started_at is not None and before.query_attempts == 2
    assert provider.ledger_snapshot()["submitCalls"] == 1
    assert json.loads((store.home / "state.json").read_bytes())["jobs"][job_id] == before.model_dump(
        mode="json", by_alias=True
    )
    monkeypatch.undo()
    home = store.home
    store.close()
    with FileStore.open(home) as reopened:
        provider = MockVideoAdapter(reopened, clock=video_clock)
        service = JobService(reopened, [provider], clock=video_clock)
        job = service.get(job_id, project_id="coffee")
        if stage == "submit":
            assert job.status == "unknown"
            assert await JobWorker(service).run_once() is None
        else:
            assert job.query_attempts == 2 and job.consecutive_query_errors == 1
            video_clock.due(job)
            final = await JobWorker(service).run_once()
            assert final.status == "succeeded" and final.query_attempts == 3
            assert final.provider_task_id == before.provider_task_id
        assert provider.ledger_snapshot()["submitCalls"] == 1


@pytest.mark.parametrize(
    "raw",
    [b"not-json", b'{"schemaVersion":99}', b'{"schemaVersion":true}', b'{"schemaVersion":1,"submitCalls":1}'],
)
def test_corrupt_mock_ledger_is_never_reset(store, raw):
    path = store.home / "mock-video.json"
    path.write_bytes(raw)
    with pytest.raises(AppError) as error:
        MockVideoAdapter(store)
    assert error.value.code == "INVALID_MOCK_LEDGER"
    assert path.read_bytes() == raw


CRASH_WORKER = r"""
import asyncio, os, sys
from datetime import UTC, datetime, timedelta
from vagent.storage import FileStore, RunRecordV1
from vagent.tools import create_project_tools
from vagent.video.jobs import JobService
from vagent.video.providers.mock import MockVideoAdapter
from vagent.video.worker import JobWorker
from vagent.waiting import ToolExecutionContext

phase = sys.argv[2]
clock_value = datetime(2030, 1, 1, tzinfo=UTC)
def clock():
    return clock_value

class CrashAdapter(MockVideoAdapter):
    async def submit(self, request, operation_key):
        if phase == "submitting":
            os._exit(73)
        handle = await super().submit(request, operation_key)
        if phase == "accepted":
            os._exit(73)
        return handle
    async def query(self, task_id):
        if phase == "query_intent":
            os._exit(73)
        snapshot = await super().query(task_id)
        if phase == "query_response":
            os._exit(73)
        return snapshot

async def main():
    global clock_value
    with FileStore.open(sys.argv[1]) as store:
        store.ensure_session("coffee")
        record = RunRecordV1(
            id="run", session_id="coffee", request_id="request", prompt="offline", model="no-model",
            status="completed", messages=[], model_steps=0, tool_calls=0, input_tokens=0, output_tokens=0,
            answer="", created_at=clock().isoformat(), updated_at=clock().isoformat(),
        ).model_dump(by_alias=True)
        store.transaction(lambda draft: draft["runs"].update({"run": record}))
        source = create_project_tools().execute("artifact_save", {
            "kind": "brief", "title": "original", "content": "original source body"
        }, store=store, project_id="coffee", operation_key="source")["data"]
        context = ToolExecutionContext(project_id="coffee", session_id="coffee", run_id="run",
            model_step=1, tool_call_id="original-generate")
        provider = CrashAdapter(store, clock=clock)
        caps = provider.capabilities()
        service = JobService(store, [provider], clock=clock)
        registered = service.generate({
            "provider": caps.provider, "model": caps.model, "capabilitiesVersion": caps.capabilities_version,
            "prompt": "original", "spec": caps.specs[0].model_dump(mode="json", by_alias=True),
            "sourceRefs": [{"artifactId": source["artifactId"], "version": 1}],
        }, context=context)
        assert registered["ok"]
        if phase == "registered":
            os._exit(73)
        worker = JobWorker(service)
        await worker.run_once()
        if phase == "handle":
            os._exit(73)
        clock_value += timedelta(seconds=2)
        await worker.run_once()
        clock_value += timedelta(seconds=2)
        final = await worker.run_once()
        assert final.status == "succeeded"
        os._exit(73)

asyncio.run(main())
"""


@pytest.mark.parametrize(
    "phase,submits_before,status_after_open",
    [
        ("registered", 0, "pending_submit"),
        ("submitting", 0, "unknown"),
        ("accepted", 1, "unknown"),
        ("handle", 1, "queued"),
        ("query_intent", 1, "queued"),
        ("query_response", 1, "queued"),
        ("success", 1, "succeeded"),
    ],
)
async def test_real_process_crash_recovers_without_repeating_submission(
    tmp_path, video_clock, phase, submits_before, status_after_open
):
    home = tmp_path / "state"
    process = subprocess.run(
        [sys.executable, "-c", CRASH_WORKER, str(home), phase],
        capture_output=True,
        timeout=20,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
    )
    assert process.returncode == 73, process.stderr.decode(errors="replace")
    original = json.loads((home / "state.json").read_bytes())
    job_id = next(iter(original["jobs"]))
    with pytest.raises(AppError) as locked:
        FileStore.open(home)
    assert locked.value.code == "STORE_LOCKED"
    # The child has exited; clear its exact stale lock as an operator would.
    (home / "instance.lock").unlink()
    with FileStore.open(home) as reopened:
        provider = MockVideoAdapter(reopened, clock=video_clock)
        service = JobService(reopened, [provider], clock=video_clock)
        job = service.get(job_id, project_id="coffee")
        assert job.status == status_after_open
        assert provider.ledger_snapshot()["submitCalls"] == submits_before
        assert job.query_started_at is None
        if phase.startswith("query_"):
            assert job.query_attempts == 1 and job.consecutive_query_errors == 1
            assert job.error.code == "QUERY_INTERRUPTED"
        worker = JobWorker(service)
        for _ in range(4):
            if job.next_poll_at is not None:
                video_clock.due(job)
            updated = await worker.run_once()
            if updated is None:
                break
            job = updated
        assert job.status == ("unknown" if status_after_open == "unknown" else "succeeded")
        assert provider.ledger_snapshot()["submitCalls"] == (0 if phase == "submitting" else 1)
        assert reopened.snapshot()["operations"] == original["operations"]
        assert reopened.snapshot()["artifacts"] == original["artifacts"]
        assert reopened.snapshot()["runs"] == original["runs"]
        assert job.request.model_dump(mode="json", by_alias=True) == original["jobs"][job_id]["request"]
        if job.result is not None:
            assert job.result.source_refs == job.request.source_refs
            assert job.result.source_refs[0].version == 1
        if original["jobs"][job_id]["providerTaskId"] is not None:
            assert job.provider_task_id == original["jobs"][job_id]["providerTaskId"]
        assert await worker.run_once() is None
