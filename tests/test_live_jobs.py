import asyncio
import copy
from dataclasses import replace
from datetime import datetime

import httpx
import pytest
from conftest import video_request, video_run
from wan_support import VIDEO_URL, live_arguments, live_config, live_run, live_service, provider_response

from vagent.errors import AppError
from vagent.storage import Database, FileStore
from vagent.tools import create_project_tools
from vagent.video.contracts import JobV2, ProviderTaskSnapshotV2, VideoPrice, parse_job
from vagent.video.jobs import JobService
from vagent.video.providers.mock import MockVideoAdapter
from vagent.video.tools import completed_job_data, register_video_tools, resolve_job_wait
from vagent.video.views import job_snapshot, job_view
from vagent.video.worker import JobWorker
from vagent.waiting import DeferredToolResult, WaitBinding


def generate(service, context, **changes):
    result = service.generate(live_arguments(**changes), context=context)
    assert result["ok"], result
    return service.get(result["data"]["jobId"])


def responded(status="PENDING", **output):
    return lambda _: httpx.Response(200, json=provider_response(status, **output))


async def test_live_job_freezes_quote_and_enters_download_only_once(store, video_clock):
    context = live_run(store)
    calls, events = [], []

    def handler(request):
        calls.append(request)
        job = service.list()[0]
        if request.method == "POST":
            assert job.status == "submitting" and job.submit_attempts == 1
            assert job.submission_started_at == video_clock().isoformat()
            assert job.query_deadline_at == "2030-01-02T00:00:00+00:00"
            return httpx.Response(200, json=provider_response())
        assert job.provider_task_id == "wan-original" and job.query_started_at is not None
        return httpx.Response(
            200,
            json={
                **provider_response("SUCCEEDED", request_id="query-trace", video_url=VIDEO_URL),
                "usage": {"duration": 5},
            },
        )

    async with live_service(store, video_clock, handler) as (service, _):
        service.on_change = events.append
        job = generate(service, context)
        assert job.contract_version == 2 and job.cost.estimate.amount == "3.00"
        assert job.cost.actual.status == "unknown" and job.cost.actual.amount is None
        assert job.policy.interval_seconds == 15 and job.request.parameters.watermark
        worker = JobWorker(service)
        queued = await worker.run_once()
        assert queued.provider_task_id == "wan-original" and queued.status == "queued"
        assert queued.provider_submission_request_id == "trace-original"
        video_clock.due(queued)
        downloaded = await worker.run_once()
        assert downloaded.status == "downloading" and downloaded.query_state == "idle"
        assert downloaded.last_provider_status == "SUCCEEDED"
        assert downloaded.last_query_request_id == "query-trace"
        assert downloaded.provider_submission_request_id == "trace-original"
        assert downloaded.result is None and not store.snapshot()["media"]
        assert downloaded.cost.provider_usage.duration == 5 and downloaded.cost.actual.amount is None
        assert downloaded.download.phase == "pending" and downloaded.download.attempts == 0
        assert downloaded.download.relative_path == f"media/coffee/{downloaded.download.media_id}.mp4"
        for _ in range(3):
            video_clock.advance(30)
            assert await worker.run_once() is None
        snapshot = ProviderTaskSnapshotV2(
            task_id=downloaded.provider_task_id, status="succeeded", output=downloaded.provider_output
        )
        assert worker._snapshot(downloaded, snapshot) == downloaded
        assert len([call for call in calls if call.method == "POST"]) == 1 and len(calls) == 2
        for view in (job_snapshot(downloaded), job_view(downloaded)):
            public = str(view)
            assert not view["simulated"] and not view["mediaAvailable"]
            for private in (
                "test-workspace",
                "video-test-secret",
                "private-signature",
                "videoUrl",
                "providerOutput",
                "workspaceId",
                "relativePath",
            ):
                assert private not in public
        stored = store.snapshot()
        assert isinstance(Database.model_validate(stored).jobs[job.id], JobV2)
        home = store.home
        store.close()
        with FileStore.open(home) as reopened:
            assert reopened.snapshot() == stored
            assert (
                parse_job(reopened.snapshot()["jobs"][job.id]).download.media_id
                == downloaded.download.media_id
            )


@pytest.mark.parametrize("status", ["SUCCEEDED", "FAILED", "CANCELED"])
async def test_immediate_terminal_response_commits_original_handle_first(store, video_clock, status):
    context = live_run(store)
    events = []
    async with live_service(store, video_clock, responded(status, video_url=VIDEO_URL)) as (service, _):
        service.on_change = events.append
        job = generate(service, context)
        result = await JobWorker(service).run_once()
        assert result.status == ("downloading" if status == "SUCCEEDED" else "failed")
        accepted = [event for event in events if event.provider_task_id]
        assert accepted[0].status == "queued" and accepted[0].last_provider_status is None
        assert result.submit_attempts == 1 and result.query_attempts == 0
        assert service.get(job.id).provider_task_id == "wan-original"


async def test_intent_and_operation_replay_precede_current_configuration_and_price(store, video_clock):
    context = live_run(store)
    async with live_service(store, video_clock, responded()) as (service, _):
        first = service.generate(live_arguments(prompt="  原提示\n  保留内部空白  "), context=context)
        original = service.get(first["data"]["jobId"])
        service.config = replace(
            service.config, video_workspace_id="another-workspace", video_api_key=None, video_max_job_cost="0"
        )
        service.price = None
        replay = service.generate(live_arguments(prompt="  原提示\n  保留内部空白  "), context=context)
        assert replay == first
        other_call = context.model_copy(update={"tool_call_id": "same-intent"})
        same = service.generate(
            live_arguments(prompt="原提示\n  保留内部空白", sourceRefs=[]), context=other_call
        )
        assert same["data"]["jobId"] == original.id
        assert service.get(original.id) == original
        changed = service.generate(
            live_arguments(prompt="新需求"),
            context=context.model_copy(update={"tool_call_id": "different-intent"}),
        )
        assert changed["error"]["code"] == "JOB_ALREADY_EXISTS" and original.id in changed["error"]["message"]
        conflicting = service.generate({"apiKey": "must-not-be-recorded"}, context=context)
        assert conflicting["error"]["code"] == "OPERATION_CONFLICT"
        assert "must-not-be-recorded" not in str(store.snapshot())


@pytest.mark.parametrize(
    "changes,code",
    [
        ({"video_api_key": None}, "VIDEO_KEY_MISSING"),
        ({"video_workspace_id": None}, "VIDEO_WORKSPACE_REQUIRED"),
        ({"video_max_job_cost": "2.99"}, "VIDEO_COST_LIMIT"),
        ({"video_max_job_cost": "0"}, "VIDEO_COST_LIMIT"),
    ],
)
async def test_missing_configuration_or_budget_creates_no_job(store, video_clock, changes, code):
    context = live_run(store)
    calls = []
    async with live_service(store, video_clock, lambda request: calls.append(request), **changes) as (
        service,
        _,
    ):
        result = service.generate(live_arguments(), context=context)
        assert result["error"]["code"] == code
        assert not store.snapshot()["jobs"] and not calls


@pytest.mark.parametrize(
    "changes,code",
    [
        ({"capabilitiesVersion": "outdated"}, "VIDEO_CAPABILITIES_CHANGED"),
        (
            {"spec": {"durationSeconds": 4, "resolution": "720p", "aspectRatio": "16:9"}},
            "VIDEO_UNSUPPORTED_SPEC",
        ),
        (
            {"spec": {"durationSeconds": True, "resolution": "720p", "aspectRatio": "16:9"}},
            "INVALID_ARGUMENTS",
        ),
        (
            {"spec": {"durationSeconds": "5", "resolution": "720p", "aspectRatio": "16:9"}},
            "INVALID_ARGUMENTS",
        ),
        (
            {"spec": {"durationSeconds": 5.0, "resolution": "720p", "aspectRatio": "16:9"}},
            "INVALID_ARGUMENTS",
        ),
        ({"spec": {"duration_seconds": 5, "resolution": "720p", "aspectRatio": "16:9"}}, "INVALID_ARGUMENTS"),
        ({"sourceRefs": [{"artifact_id": "missing", "version": 1}]}, "INVALID_ARGUMENTS"),
        ({"sourceRefs": [{"artifactId": "missing", "version": 1}]}, "SOURCE_NOT_FOUND"),
        ({"seed": 0}, "INVALID_ARGUMENTS"),
        ({"workspaceId": "override"}, "INVALID_ARGUMENTS"),
        ({"baseUrl": "https://untrusted.example"}, "INVALID_ARGUMENTS"),
        ({"prompt": "字" * 4992}, "VIDEO_PROMPT_TOO_LONG"),
    ],
    ids=[
        "capabilities",
        "unsupported-spec",
        "bool",
        "string",
        "float",
        "spec-alias",
        "source-alias",
        "source-missing",
        "seed",
        "workspace",
        "base-url",
        "prompt-length",
    ],
)
async def test_live_arguments_reject_unsupported_or_hidden_values_before_submit(
    store, video_clock, changes, code
):
    context = live_run(store)
    async with live_service(store, video_clock, responded()) as (service, _):
        result = service.generate(live_arguments(**changes), context=context)
        assert result["error"]["code"] == code
        assert not service.list()


async def test_read_only_mode_scope_sources_and_unknown_quote_are_enforced(store, video_clock):
    readonly = live_run(store, "readonly", read_only=True)
    off = video_run(store, "off")
    store.transaction(lambda draft: draft["runs"][off.run_id].update(videoMode="off"))
    context = live_run(store)
    async with live_service(store, video_clock, responded()) as (service, _):
        assert service.generate(live_arguments(), context=readonly)["error"]["code"] == "READ_ONLY"
        assert service.generate(live_arguments(), context=off)["error"]["code"] == "VIDEO_MODEL_UNAVAILABLE"
        mock = next(adapter for key, adapter in service._adapters.items() if key[0] == "mock")
        assert (
            service.generate(video_request(mock), context=context)["error"]["code"]
            == "VIDEO_MODEL_UNAVAILABLE"
        )
        service.price = None
        assert (
            service.generate(
                live_arguments(), context=context.model_copy(update={"tool_call_id": "no-price"})
            )["error"]["code"]
            == "VIDEO_PRICE_UNKNOWN"
        )
        service.price = service.capabilities("live")[0].price
        tools = create_project_tools()
        args = {"kind": "script", "title": "来源", "content": "第一版正文"}
        source = tools.execute(
            "artifact_save", args, store=store, project_id="coffee", operation_key="source"
        )["data"]
        refs = [{"artifactId": source["artifactId"], "version": 1}]
        job = generate(service, context.model_copy(update={"tool_call_id": "with-source"}), sourceRefs=refs)
        tools.execute(
            "artifact_save",
            {**args, "artifactId": source["artifactId"], "expectedVersion": 1, "content": "第二版正文"},
            store=store,
            project_id="coffee",
            operation_key="source-update",
        )
        assert service.get(job.id).request.source_refs[0].version == 1


@pytest.mark.parametrize(
    "cause,code",
    [
        ("key", "VIDEO_KEY_MISSING"),
        ("scope", "VIDEO_PROVIDER_UNAVAILABLE"),
        ("cost", "VIDEO_COST_LIMIT"),
        ("price", "VIDEO_PRICE_CHANGED"),
        ("price-missing", "VIDEO_PRICE_UNKNOWN"),
    ],
)
async def test_pending_submit_blocks_once_and_continues_only_the_original_job(
    store, video_clock, cause, code
):
    context = live_run(store)
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200, json=provider_response())

    async with live_service(store, video_clock, handler) as (service, _):
        job = generate(service, context)
        config, price = service.config, service.price
        if cause == "key":
            service.config = replace(config, video_api_key=None)
        elif cause == "scope":
            service.config = replace(config, video_workspace_id="other-space")
        elif cause == "cost":
            service.config = replace(config, video_max_job_cost="0")
        elif cause == "price":
            service.price = VideoPrice.model_validate({**price.model_dump(), "price_version": "new-price"})
        else:
            service.price = None
        worker = JobWorker(service)
        blocked = await worker.run_once()
        assert blocked.status == "pending_submit" and blocked.submit_attempts == 0
        assert blocked.runtime_block.code == code and blocked.cost == job.cost
        assert await worker.run_once() is None and not calls
        assert service.get(job.id).revision == blocked.revision
        service.config, service.price = config, price
        accepted = await worker.run_once()
        assert accepted.provider_task_id == "wan-original" and accepted.runtime_block is None
        assert accepted.id == job.id and len(calls) == 1


async def test_accepted_task_configuration_pause_consumes_no_query_and_low_cost_does_not_block_results(
    store, video_clock
):
    context = live_run(store)
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(
            200,
            json=provider_response(
                "PENDING" if request.method == "POST" else "SUCCEEDED", video_url=VIDEO_URL
            ),
        )

    async with live_service(store, video_clock, handler) as (service, _):
        job = generate(service, context)
        worker = JobWorker(service)
        accepted = await worker.run_once()
        service.config = replace(service.config, video_api_key=None)
        video_clock.due(accepted)
        paused = await worker.run_once()
        assert paused.status == "queued" and paused.query_pause_reason == "configuration"
        assert paused.query_attempts == 0 and paused.consecutive_query_errors == 0 and len(calls) == 1
        assert await worker.run_once() is None
        with pytest.raises(AppError) as error:
            service.retry_query(job.id, project_id="coffee")
        assert error.value.code == "VIDEO_KEY_MISSING"
        service.config = replace(service.config, video_api_key="video-test-secret", video_max_job_cost="0")
        service.retry_query(job.id, project_id="coffee")
        result = await worker.run_once()
        assert result.status == "downloading" and result.cost.estimate.max_job_cost == "3.00"
        assert len(calls) == 2 and calls[1].url.path == "/api/v1/tasks/wan-original"


async def test_query_retry_window_and_retry_after_survive_restart(store, video_clock):
    context = live_run(store)
    calls = []

    def handler(request):
        calls.append(request)
        if request.method == "POST":
            return httpx.Response(200, json=provider_response("RUNNING"))
        return httpx.Response(429, json={"code": "Throttling"}, headers={"Retry-After": "90"})

    async with live_service(store, video_clock, handler) as (service, provider):
        original = generate(service, context)
        worker = JobWorker(service)
        job = await worker.run_once()
        for attempt in range(1, 5):
            video_clock.due(job)
            at = video_clock()
            job = await worker.run_once()
            assert job.status == "running" and job.query_attempts == attempt
            assert job.consecutive_query_errors == attempt
            if attempt < 4:
                assert (datetime.fromisoformat(job.next_poll_at) - at).total_seconds() == 90
                if attempt == 1:
                    home, saved = store.home, store.snapshot()
                    store.close()
                    store = FileStore.open(home)
                    assert store.snapshot() == saved
                    service = JobService(store, [provider], config=live_config(home), clock=video_clock)
                    worker = JobWorker(service)
                    assert await worker.run_once() is None
            else:
                assert job.query_state == "paused" and job.query_pause_reason == "retry_exhausted"
        assert job.id == original.id and len(calls) == 5
        assert all(call.url.path.endswith("wan-original") for call in calls[1:])
        resumed = service.retry_query(job.id, project_id="coffee")
        assert resumed.query_attempts == 4 and resumed.consecutive_query_errors == 0
        store.close()


async def test_deadline_preempts_retry_after_without_another_http_request(store, video_clock):
    context = live_run(store)
    calls = []

    def handler(request):
        calls.append(request)
        return (
            httpx.Response(200, json=provider_response())
            if request.method == "POST"
            else httpx.Response(429, json={"code": "Throttling"}, headers={"Retry-After": "172800"})
        )

    async with live_service(store, video_clock, handler) as (service, _):
        generate(service, context)
        worker = JobWorker(service)
        queued = await worker.run_once()
        video_clock.due(queued)
        retry = await worker.run_once()
        assert datetime.fromisoformat(retry.next_poll_at) > datetime.fromisoformat(retry.query_deadline_at)
        video_clock.value = datetime.fromisoformat(retry.query_deadline_at)
        paused = await worker.run_once()
        assert paused.query_pause_reason == "task_unavailable" and len(calls) == 2
        with pytest.raises(AppError) as caught:
            service.retry_query(paused.id, project_id="coffee")
        assert caught.value.code == "PROVIDER_TASK_UNAVAILABLE"
        assert await worker.run_once() is None


async def test_live_worker_serial_spacing_is_shared_by_owners(store, video_clock):
    one = live_run(store, "one")
    two = live_run(store, "two")
    calls = []

    def handler(request):
        calls.append(video_clock())
        return httpx.Response(200, json=provider_response(task_id=f"task-{len(calls)}"))

    async with live_service(store, video_clock, handler) as (service, _):
        generate(service, one)
        generate(service, two)
        workers = JobWorker(service), JobWorker(service)
        results = await asyncio.gather(*(worker.run_once() for worker in workers))
        assert sum(result is not None for result in results) == 1 and len(calls) == 1
        video_clock.advance(0.99)
        assert await workers[1].run_once() is None
        video_clock.advance(0.01)
        assert (await workers[1].run_once()).provider_task_id == "task-2"
        assert (calls[1] - calls[0]).total_seconds() >= 1


async def test_download_state_waits_without_delivering_a_cloud_url(store, video_clock):
    context = live_run(store)
    async with live_service(store, video_clock, responded("SUCCEEDED", video_url=VIDEO_URL)) as (service, _):
        job = generate(service, context)
        job = await JobWorker(service).run_once()
        tools = register_video_tools(create_project_tools(), service, mode="live")
        wait_context = context.model_copy(update={"model_step": 2, "tool_call_id": "wait"})
        deferred = tools.execute(
            "await_job",
            {"jobId": job.id},
            store=store,
            project_id="coffee",
            operation_key=wait_context.operation_key,
            context=wait_context,
        )
        assert isinstance(deferred, DeferredToolResult)
        binding = WaitBinding.model_validate(next(iter(store.snapshot()["waits"].values())))
        assert resolve_job_wait(store, binding) is None
        assert (
            tools.execute(
                "await_job",
                {"jobId": job.id},
                store=store,
                project_id="coffee",
                operation_key=wait_context.operation_key,
                context=wait_context,
            )
            == deferred
        )
        assert wait_context.operation_key not in store.snapshot()["operations"]
        with pytest.raises(AppError) as caught:
            completed_job_data(job)
        assert caught.value.code == "JOB_NOT_READY"


async def test_v3_holds_both_job_versions_without_reencoding_mock_data(store, video_clock):
    mock_context = video_run(store, "old-mock")
    mock = MockVideoAdapter(store, clock=video_clock)
    old_service = JobService(store, [mock], clock=video_clock)
    old = old_service.generate(video_request(mock), context=mock_context)
    old_raw = copy.deepcopy(store.snapshot()["jobs"][old["data"]["jobId"]])
    live_context = live_run(store)
    async with live_service(store, video_clock, responded()) as (service, _):
        live = generate(service, live_context)
        database = Database.model_validate(store.snapshot())
        assert database.jobs[live.id].contract_version == 2
        assert database.jobs[old_raw["id"]].contract_version == 1
        assert store.snapshot()["jobs"][old_raw["id"]] == old_raw


async def test_handle_commit_survives_failure_while_registering_cloud_success(
    store, video_clock, monkeypatch
):
    context = live_run(store)
    calls = []

    def handler(request):
        calls.append(request.method)
        return httpx.Response(200, json=provider_response("SUCCEEDED", video_url=VIDEO_URL))

    async with live_service(store, video_clock, handler) as (service, provider):
        original = generate(service, context)
        save = service.save

        def fail_download_intent(job, **kwargs):
            if job.status == "downloading":
                raise OSError("injected failure after acceptance commit")
            return save(job, **kwargs)

        monkeypatch.setattr(service, "save", fail_download_intent)
        with pytest.raises(OSError):
            await JobWorker(service).run_once()
        confirmed = service.get(original.id)
        assert confirmed.provider_task_id == "wan-original" and confirmed.status == "queued"
        home = store.home
        store.close()
        with FileStore.open(home) as reopened:
            resumed = JobService(reopened, [provider], config=live_config(home), clock=video_clock)
            video_clock.due(resumed.get(original.id))
            result = await JobWorker(resumed).run_once()
            assert result.status == "downloading" and result.id == original.id
            assert calls == ["POST", "GET"]


async def test_interrupted_query_charges_original_timeout_and_preserves_retry_budget(store, video_clock):
    context = live_run(store)

    class Crash(BaseException):
        pass

    def handler(request):
        if request.method == "GET":
            raise Crash()
        return httpx.Response(200, json=provider_response())

    async with live_service(store, video_clock, handler) as (service, _):
        job = generate(service, context)
        worker = JobWorker(service)
        accepted = await worker.run_once()
        video_clock.due(accepted)
        with pytest.raises(Crash):
            await worker.run_once()
        interrupted = service.get(job.id)
        assert interrupted.query_attempts == 1 and interrupted.query_started_at is not None
        home = store.home
        store.close()
        with FileStore.open(home) as reopened:
            recovered = parse_job(reopened.snapshot()["jobs"][job.id])
            assert recovered.query_attempts == 1 and recovered.consecutive_query_errors == 1
            assert (
                datetime.fromisoformat(recovered.next_poll_at)
                - datetime.fromisoformat(interrupted.next_poll_at)
            ).total_seconds() == 15
            assert recovered.provider_task_id == "wan-original"
            saved = reopened.snapshot()
        with FileStore.open(home) as reopened:
            assert reopened.snapshot() == saved


@pytest.mark.parametrize(
    "status,body,expected",
    [
        (401, {"code": "InvalidApiKey", "message": "private-secret"}, "failed"),
        (401, {"code": "InvalidApiKey", "output": {"task_id": "ambiguous"}}, "unknown"),
        (200, {}, "unknown"),
        (500, {"code": "InternalError"}, "unknown"),
    ],
)
async def test_rejection_and_ambiguous_submissions_never_reenter_queue(
    store, video_clock, status, body, expected
):
    context = live_run(store)
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(status, json=body)

    async with live_service(store, video_clock, handler) as (service, _):
        generate(service, context)
        worker = JobWorker(service)
        result = await worker.run_once()
        assert result.status == expected and result.provider_task_id is None and result.submit_attempts == 1
        assert result.error.stage == "submit" and result.cost.actual.amount is None
        video_clock.advance(900)
        assert await worker.run_once() is None and len(calls) == 1
        assert "private-secret" not in str(store.snapshot())


@pytest.mark.parametrize(
    "response,reason,code",
    [
        (
            httpx.Response(200, json=provider_response("UNKNOWN")),
            "task_unavailable",
            "PROVIDER_TASK_UNAVAILABLE",
        ),
        (
            httpx.Response(401, json={"code": "InvalidApiKey", "request_id": "query-error-trace"}),
            "configuration",
            "PROVIDER_AUTH_FAILED",
        ),
    ],
)
async def test_query_unavailable_and_authentication_pause_immediately(
    store, video_clock, response, reason, code
):
    context = live_run(store)

    def handler(request):
        return (
            httpx.Response(200, json=provider_response("RUNNING")) if request.method == "POST" else response
        )

    async with live_service(store, video_clock, handler) as (service, _):
        generate(service, context)
        worker = JobWorker(service)
        queued = await worker.run_once()
        video_clock.due(queued)
        paused = await worker.run_once()
        assert paused.status == "running" and paused.provider_task_id == "wan-original"
        assert paused.query_attempts == 1 and paused.consecutive_query_errors == 1
        assert paused.query_pause_reason == reason and paused.error.code == code
        assert paused.next_poll_at is None and paused.result is None
        assert await worker.run_once() is None
        if reason == "configuration":
            assert paused.last_query_request_id == "query-error-trace"
            assert "query-error-trace" not in str(job_view(paused))


async def test_submission_cancellation_and_restart_keep_unknown_without_retry(store, video_clock):
    context = live_run(store)
    started, calls = asyncio.Event(), []

    async def handler(request):
        calls.append(request)
        started.set()
        await asyncio.Event().wait()

    async with live_service(store, video_clock, handler) as (service, provider):
        job = generate(service, context)
        task = asyncio.create_task(JobWorker(service).run_once())
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        unknown = service.get(job.id)
        assert unknown.status == "unknown" and unknown.error.code == "SUBMISSION_UNKNOWN"
        home = store.home
        store.close()
        with FileStore.open(home) as reopened:
            restored = JobService(reopened, [provider], config=live_config(home), clock=video_clock)
            assert await JobWorker(restored).run_once() is None and restored.get(job.id) == unknown
            assert len(calls) == 1
