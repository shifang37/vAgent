"""M3 E2: multi-provider registration, budget buckets and the N-shot delivery path.

No test here reaches a real upstream: Kling/fal answer from controlled transports,
the media download uses a local fixture, and every Job remains a local registration
until its own Worker submits it against the controlled response.
"""

import hashlib
import json
from dataclasses import replace

import httpx
import pytest
from m3_support import (
    FalUpstream,
    KlingUpstream,
    kling_service,
    providers_service,
    shot_context,
)
from media_support import SAMPLE
from shot_support import (
    generation_args,
    insert_image,
    jobs_of,
    m2_run,
    mock_tools,
    save_storyboard,
    shot,
)
from wan_support import live_run

from vagent.errors import AppError
from vagent.storage import FileStore
from vagent.video.contracts import (
    KLING_MODEL,
    JobV6,
    parse_job,
)
from vagent.video.media_http import MediaHttpClient
from vagent.video.media_worker import MediaWorker
from vagent.video.worker import JobWorker


async def test_a_kling_batch_registers_n_v6_jobs_with_its_own_budget_bucket(store, video_clock):
    upstream = KlingUpstream()
    registry, _, _, _ = mock_tools(store, clock=video_clock)
    saved = save_storyboard(
        registry, store, [shot("镜头一：雨夜咖啡店"), shot("镜头二：推近咖啡杯"), shot("镜头三：窗边光影")]
    )
    m2_run(store, "shot-run", mode="live")
    async with kling_service(store, video_clock, upstream.handler) as (service, _):
        result = service.shot_generate(generation_args(saved), context=shot_context())
        assert result["ok"], result
        assert result["data"]["totalEstimate"] == {"currency": "USD", "amount": "1.68"}
        jobs = [parse_job(raw) for raw in jobs_of(store, "shot-run")]
        assert len(jobs) == 3
        assert all(isinstance(job, JobV6) and job.contract_version == 6 for job in jobs)
        assert {job.request.provider for job in jobs} == {"kling"}
        assert {job.request.endpoint_profile for job in jobs} == {"kling-singapore-v1"}
        assert all(job.shot_ref is not None for job in jobs)
        assert all(job.cost.estimate.currency == "USD" for job in jobs)
        assert all(job.cost.estimate.amount == "0.56" for job in jobs)
        assert all(job.cost.resource_estimate.units == 4 for job in jobs)
        limits = jobs[-1].cost.limits
        assert limits.max_new_jobs_per_run == 4 and limits.used_new_jobs == 3
        bucket = next(item for item in limits.buckets if item.provider == "kling")
        assert bucket.currency == "USD"
        assert (bucket.used_amount, bucket.remaining_amount) == ("1.68", "0.72")
        assert (bucket.used_units, bucket.remaining_units) == (12, 4)
        assert not upstream.submitted  # Registration never submits.
        assert "workspaceId" not in json.dumps(store.snapshot()["jobs"], ensure_ascii=False)


async def test_the_shared_job_ceiling_and_provider_buckets_do_not_pool(store, video_clock):
    kling = KlingUpstream()
    fal = FalUpstream()
    registry, _, _, _ = mock_tools(store, clock=video_clock)
    saved = save_storyboard(registry, store, [shot(f"镜头{i}") for i in range(1, 6)])
    m2_run(store, "cap-run", mode="live")
    async with providers_service(
        store, video_clock, kling_handler=kling.handler, fal_handler=fal.handler
    ) as (service, _, _):
        first = service.shot_generate(
            generation_args(saved, saved["shotIds"][:3]), context=shot_context("cap-run", "batch-1")
        )
        assert first["ok"], first
        service.config = replace(service.config, video_provider="fal")
        over = service.shot_generate(
            generation_args(saved, saved["shotIds"][3:]), context=shot_context("cap-run", "batch-2")
        )
        assert over["ok"] is False and over["error"]["code"] == "VIDEO_RUN_JOB_LIMIT"
        assert len(jobs_of(store, "cap-run")) == 3
        # A fresh Run may mix providers, but each bucket keeps its own usage.
        saved_two = save_storyboard(
            registry, store, [shot(f"新镜头{i}") for i in range(1, 4)], key="second-storyboard"
        )
        m2_run(store, "mix-run", mode="live")
        service.config = replace(service.config, video_provider="kling")
        assert service.shot_generate(
            generation_args(saved_two, saved_two["shotIds"][:2]), context=shot_context("mix-run", "k")
        )["ok"]
        service.config = replace(service.config, video_provider="fal")
        mixed = service.shot_generate(
            generation_args(saved_two, saved_two["shotIds"][2:]), context=shot_context("mix-run", "f")
        )
        assert mixed["ok"], mixed
        jobs = [parse_job(raw) for raw in jobs_of(store, "mix-run")]
        assert [job.request.provider for job in jobs].count("kling") == 2
        assert [job.request.provider for job in jobs].count("fal") == 1
        limits = jobs[-1].cost.limits
        assert limits.used_new_jobs == 3
        by_provider = {item.provider: item for item in limits.buckets}
        assert (by_provider["kling"].used_amount, by_provider["kling"].remaining_amount) == (
            "1.12",
            "1.28",
        )
        assert (by_provider["fal"].used_amount, by_provider["fal"].remaining_amount) == (
            "0.42",
            "1.58",
        )
        assert by_provider["wan"].used_amount == "0.00"


async def test_a_missing_key_disables_only_its_own_provider(store, video_clock):
    upstream = KlingUpstream()
    registry, _, _, _ = mock_tools(store, clock=video_clock)
    saved = save_storyboard(registry, store, [shot("镜头一")])
    m2_run(store, "no-key-run", mode="live")
    async with kling_service(store, video_clock, upstream.handler, kling_api_key=None) as (service, _):
        status = {item["provider"]: item for item in service.provider_statuses()}
        assert status["kling"]["configured"] is False
        assert status["kling"]["error"]["code"] == "VIDEO_KEY_MISSING"
        result = service.shot_generate(generation_args(saved), context=shot_context("no-key-run"))
        assert result["ok"] is False and result["error"]["code"] == "VIDEO_KEY_MISSING"
        assert not jobs_of(store, "no-key-run")
        assert not upstream.submitted


async def test_out_of_bounds_provider_requests_are_rejected_before_registration(store, video_clock):
    upstream = KlingUpstream()
    registry, _, _, _ = mock_tools(store, clock=video_clock)
    save_storyboard(registry, store, [shot("镜头一")])
    m2_run(store, "bounds-run", mode="live")
    async with kling_service(store, video_clock, upstream.handler) as (service, _):
        long_prompt = "长" * 2501
        bounded = service.generate(
            {
                "provider": "kling",
                "model": KLING_MODEL,
                "capabilitiesVersion": "multi-provider-v3",
                "prompt": long_prompt,
                "spec": {"durationSeconds": 5, "resolution": "720p", "aspectRatio": "16:9"},
            },
            context=live_run(store, "single-run"),
        )
        assert bounded["ok"] is False and bounded["error"]["code"] == "VIDEO_PROMPT_TOO_LONG"
        overlong = service.generate(
            {
                "provider": "kling",
                "model": KLING_MODEL,
                "capabilitiesVersion": "multi-provider-v3",
                "prompt": "合法提示词",
                "spec": {"durationSeconds": 10, "resolution": "720p", "aspectRatio": "16:9"},
            },
            context=live_run(store, "long-run"),
        )
        assert overlong["ok"] is False and overlong["error"]["code"] == "VIDEO_UNSUPPORTED_SPEC"
        assert not store.snapshot()["jobs"]
        assert not upstream.submitted
    # Image modes are outside the published provider table.
    asset_id = insert_image(store)
    image_saved = save_storyboard(
        registry,
        store,
        [
            {
                "description": "参考图镜头",
                "framing": "wide",
                "durationSeconds": 5,
                "mode": "i2v",
                "referenceAssetIds": [asset_id],
            }
        ],
        key="image-storyboard",
    )
    m2_run(store, "image-run", mode="live")
    async with kling_service(store, video_clock, upstream.handler) as (service, _):
        image = service.shot_generate(generation_args(image_saved), context=shot_context("image-run"))
        assert image["ok"] is False and image["error"]["code"] == "VIDEO_UNSUPPORTED_MODE"


async def test_a_kling_batch_delivers_n_local_clips_end_to_end(store, video_clock):
    upstream = KlingUpstream()
    registry, _, _, _ = mock_tools(store, clock=video_clock)
    saved = save_storyboard(registry, store, [shot(f"镜头{i}：雨夜咖啡店") for i in range(1, 4)])
    m2_run(store, "shot-run", mode="live")
    async with kling_service(store, video_clock, upstream.handler) as (service, _):
        assert service.shot_generate(generation_args(saved), context=shot_context())["ok"]
        jobs = [parse_job(raw) for raw in jobs_of(store, "shot-run")]
        worker = JobWorker(service)
        for _ in range(60):
            changed = await worker.run_once()
            if changed is None:
                video_clock.advance(20)
                continue
            current = [service.get(job.id) for job in jobs]
            if all(job.status in {"downloading", "succeeded", "failed"} for job in current):
                break
        else:
            raise AssertionError("the controlled Kling jobs did not settle")
        assert len(upstream.submitted) == 3
        assert upstream.hosts == {"api-singapore.klingai.com"}
        data = SAMPLE.read_bytes()

        def download(request):
            # The bound URL is exactly what the authenticated task returned.
            assert str(request.url).startswith("https://cdn.example-provider.test/")
            return httpx.Response(200, content=data, headers={"Content-Type": "video/mp4"})

        async with MediaHttpClient(transport=httpx.MockTransport(download)) as client:
            media_worker = MediaWorker(service.media, client)
            for _ in range(10):
                if await media_worker.run_once() is None:
                    break
        delivered = [service.get(job.id) for job in jobs]
        assert all(job.status == "succeeded" for job in delivered)
        assert all(job.media_availability.status == "available" for job in delivered)
        assert all(job.result_source.origin == "https://cdn.example-provider.test:443" for job in delivered)
        assert all(job.download.source_policy_version == "kling-auth-result-origin-v1" for job in delivered)
        media = store.snapshot()["media"]
        assert len(media) == 3
        assert {item["sha256"] for item in media.values()} == {hashlib.sha256(data).hexdigest()}
    # The delivered clips concatenate locally, with no further provider call.
    async with kling_service(store, video_clock, upstream.handler) as (service, _):
        record = store.snapshot()["storyboards"][saved["storyboardId"]]
        assembled = await service.films.assemble(
            project_id="coffee",
            storyboard_id=saved["storyboardId"],
            expected_revision=record["revision"],
            operation_key="film:1:assemble",
            args={"storyboardId": saved["storyboardId"], "expectedRevision": record["revision"]},
        )
        assert assembled["ok"], assembled
        assert assembled["data"]["currentVersion"] == 1
        assert len(assembled["data"]["versions"][0]["inputs"]) == 3
        assert len(upstream.submitted) == 3  # Assembly never submits.


async def test_a_kling_job_survives_restart_and_a_tampered_bucket_is_refused(store, video_clock):
    upstream = KlingUpstream()
    registry, _, _, _ = mock_tools(store, clock=video_clock)
    saved = save_storyboard(registry, store, [shot("镜头一")])
    m2_run(store, "shot-run", mode="live")
    async with kling_service(store, video_clock, upstream.handler) as (service, _):
        registered = service.shot_generate(generation_args(saved), context=shot_context())
        assert registered["ok"]
        job_id = registered["data"]["shots"][0]["jobId"]
    home = store.home
    store.close()
    reopened = FileStore.open(home)
    try:
        restored = parse_job(reopened.snapshot()["jobs"][job_id])
        assert isinstance(restored, JobV6) and restored.status == "pending_submit"
        assert (
            next(item for item in restored.cost.limits.buckets if item.provider == "kling").used_amount
            == "0.56"
        )
    finally:
        reopened.close()
    # A tampered bucket breaks the Job invariants, so the Store refuses to open.
    raw = json.loads((home / "state.json").read_text(encoding="utf-8"))
    job_id = next(iter(raw["jobs"]))
    raw["jobs"][job_id]["cost"]["limits"]["buckets"][1]["remainingAmount"] = "9.99"
    (home / "state.json").write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(AppError) as caught:
        FileStore.open(home)
    assert caught.value.code == "INVALID_STORE"


async def test_a_single_kling_registration_freezes_the_generic_request(store, video_clock):
    upstream = KlingUpstream()
    async with kling_service(store, video_clock, upstream.handler) as (service, _):
        args = {
            "provider": "kling",
            "model": KLING_MODEL,
            "capabilitiesVersion": "multi-provider-v3",
            "prompt": "雨夜咖啡店，镜头缓慢推近。",
            "spec": {"durationSeconds": 5, "resolution": "720p", "aspectRatio": "16:9"},
        }
        context = live_run(store, "single-run")
        registered = service.generate(args, context=context)
        assert registered["ok"], registered
        job = service.get(registered["data"]["jobId"])
        assert isinstance(job, JobV6) and job.shot_ref is None
        assert job.request.model == KLING_MODEL and job.request.mode == "t2v"
        assert job.cost.limits.used_new_jobs == 1
        assert (
            next(item for item in job.cost.limits.buckets if item.provider == "kling").remaining_amount
            == "1.84"
        )
        replay = service.generate(args, context=context)
        assert replay["data"]["jobId"] == job.id
        assert len(store.snapshot()["jobs"]) == 1
        assert not upstream.submitted


async def test_preview_reports_the_provider_currency_without_registering(store, video_clock):
    upstream = KlingUpstream()
    registry, _, _, _ = mock_tools(store, clock=video_clock)
    saved = save_storyboard(registry, store, [shot("镜头一"), shot("镜头二")])
    m2_run(store, "preview-run", mode="live")
    async with kling_service(store, video_clock, upstream.handler) as (service, _):
        preview = service.preview_shots(
            "coffee",
            {
                "storyboardId": saved["storyboardId"],
                "storyboardVersion": saved["version"],
                "shotIds": list(saved["shotIds"]),
                "confirm": False,
            },
        )
        assert preview["totalEstimate"] == {"currency": "USD", "amount": "1.12"}
        assert preview["shots"][0]["estimate"]["currency"] == "USD"
        limits = preview["limits"]
        assert limits["usedNewJobs"] == 2 and limits["maxNewJobsPerRun"] == 4
        assert (
            next(item for item in limits["buckets"] if item["provider"] == "kling")["remainingAmount"]
            == "1.28"
        )
        assert not store.snapshot()["jobs"] and not upstream.submitted


async def test_the_capability_tool_reports_each_provider_and_its_key_status(store, video_clock):
    upstream = KlingUpstream()
    from vagent.tools import create_project_tools
    from vagent.video.tools import register_video_tools

    async with kling_service(store, video_clock, upstream.handler) as (service, _):
        service.store.ensure_session("coffee")
        registry = register_video_tools(create_project_tools(), service, mode="live")
        context = live_run(store, "caps-run")
        result = registry.execute(
            "video_capabilities",
            {},
            store=store,
            project_id="coffee",
            operation_key=context.operation_key,
            context=context,
        )
        assert result["ok"], result
        data = result["data"]
        assert [item["provider"] for item in data["models"]] == ["kling"]
        statuses = {item["provider"]: item for item in data["providers"]}
        assert statuses["kling"]["configured"] is True
        assert statuses["fal"]["configured"] is False
        assert statuses["wan"]["configured"] is False
        assert statuses["kling"]["permissionStatus"] == "unverified"


async def test_worker_error_mapping_never_retries_an_ambiguous_submit(store, video_clock):
    calls = []

    def rejected(request):
        calls.append(request)
        return httpx.Response(429, json={"code": 1102, "message": "resource pack exhausted"})

    async with kling_service(store, video_clock, rejected) as (service, _):
        context = live_run(store, "rejected-run")
        registered = service.generate(
            {
                "provider": "kling",
                "model": KLING_MODEL,
                "capabilitiesVersion": "multi-provider-v3",
                "prompt": "雨夜咖啡店",
                "spec": {"durationSeconds": 5, "resolution": "720p", "aspectRatio": "16:9"},
            },
            context=context,
        )
        assert registered["ok"], registered
        assert service.get(registered["data"]["jobId"]).status == "pending_submit"
        worker = JobWorker(service)
        failed = await worker.run_once()
        assert failed.status == "failed" and failed.error.code == "PROVIDER_BILLING_BLOCKED"
        assert failed.submit_attempts == 1 and failed.provider_task_id is None
        video_clock.advance(60)
        assert await worker.run_once() is None
        assert len(calls) == 1

    ambiguous_calls = []

    def ambiguous(request):
        ambiguous_calls.append(request)
        return httpx.Response(500, json={"message": "gateway"})

    async with kling_service(store, video_clock, ambiguous) as (service, _):
        context = live_run(store, "ambiguous-run")
        registered = service.generate(
            {
                "provider": "kling",
                "model": KLING_MODEL,
                "capabilitiesVersion": "multi-provider-v3",
                "prompt": "雨夜咖啡店",
                "spec": {"durationSeconds": 5, "resolution": "720p", "aspectRatio": "16:9"},
            },
            context=context,
        )
        worker = JobWorker(service)
        unknown = await worker.run_once()
        assert unknown.status == "unknown" and unknown.error.code == "SUBMISSION_UNKNOWN"
        video_clock.advance(60)
        assert await worker.run_once() is None
        assert len(ambiguous_calls) == 1


async def test_a_rate_limited_query_backs_off_and_then_pauses(store, video_clock):
    def handler(request):
        if request.method == "POST":
            return httpx.Response(200, json={"code": 0, "data": {"id": "kling-task-1"}})
        return httpx.Response(429, json={"code": 1302}, headers={"Retry-After": "60"})

    async with kling_service(store, video_clock, handler) as (service, _):
        context = live_run(store, "throttled-run")
        registered = service.generate(
            {
                "provider": "kling",
                "model": KLING_MODEL,
                "capabilitiesVersion": "multi-provider-v3",
                "prompt": "雨夜咖啡店",
                "spec": {"durationSeconds": 5, "resolution": "720p", "aspectRatio": "16:9"},
            },
            context=context,
        )
        job_id = registered["data"]["jobId"]
        worker = JobWorker(service)
        queued = await worker.run_once()
        assert queued.status == "queued" and queued.provider_task_id == "kling-task-1"
        for expected in ("retrying", "retrying", "retrying", "paused"):
            video_clock.due(service.get(job_id))
            throttled = await worker.run_once()
            assert throttled.error.code == "PROVIDER_RATE_LIMITED"
        paused = service.get(job_id)
        assert paused.query_state == "paused"
        assert paused.query_pause_reason == "retry_exhausted"
        assert paused.status == "queued"
        assert paused.provider_task_id == "kling-task-1"


async def test_a_bound_result_origin_rejects_a_redirect_during_download(store, video_clock):
    upstream = KlingUpstream()
    registry, _, _, _ = mock_tools(store, clock=video_clock)
    saved = save_storyboard(registry, store, [shot("镜头一")])
    m2_run(store, "redirect-run", mode="live")
    async with kling_service(store, video_clock, upstream.handler) as (service, _):
        registered = service.shot_generate(generation_args(saved), context=shot_context("redirect-run"))
        assert registered["ok"], registered
        job_id = registered["data"]["shots"][0]["jobId"]
        worker = JobWorker(service)
        for _ in range(30):
            changed = await worker.run_once()
            if changed is None:
                video_clock.advance(20)
                continue
            if service.get(job_id).status == "downloading":
                break
        else:
            raise AssertionError("the controlled job never reached downloading")
        requests = []

        def redirect(request):
            requests.append(request)
            return httpx.Response(302, headers={"Location": "https://other.example.test/evil.mp4"})

        async with MediaHttpClient(transport=httpx.MockTransport(redirect)) as client:
            failed = await MediaWorker(service.media, client).run_once()
        assert failed.status == "download_failed"
        assert failed.error.code == "MEDIA_SOURCE_UNAPPROVED"
        assert failed.media_availability.status != "available"
        assert len(requests) == 1
        assert str(requests[0].url) == f"https://cdn.example-provider.test/{job_id}.mp4?sig=private".replace(
            job_id, "kling-task-1"
        )
        assert not (store.home / "media" / "coffee").exists() or not list(
            (store.home / "media" / "coffee").glob("*.mp4")
        )
