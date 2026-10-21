"""E3 headless: plan first, authorize the exact set, then deliver without a browser.

Every provider response is a controlled httpx transport and the clips come from the
local fixture, so the suite makes no DNS/TLS call and submits no paid task.
"""

import asyncio
from contextlib import asynccontextmanager

import httpx
import pytest
from conftest import ScriptedModel, tool_call
from langchain_core.messages import AIMessage
from m3_support import KlingUpstream
from media_support import SAMPLE
from wan_support import VIDEO_URL, provider_response

import vagent.application as application
from vagent import headless
from vagent.application import ApplicationService
from vagent.config import Config
from vagent.errors import AppError
from vagent.video.jobs import JobService
from vagent.video.media_http import MediaHttpClient
from vagent.video.media_worker import MediaWorker
from vagent.video.providers.kling import KlingAdapter
from vagent.video.worker import JobWorker


def storyboard_model(count=2):
    def respond(messages, step):
        if step == 0:
            return tool_call(
                "storyboard_save",
                {
                    "title": "headless 分镜",
                    "shots": [
                        {
                            "description": f"镜头{index}：雨夜咖啡店",
                            "framing": "wide",
                            "durationSeconds": 5,
                            "mode": "t2v",
                        }
                        for index in range(1, count + 1)
                    ],
                },
                call_id="save",
            )
        return AIMessage(content=f"已把 {count} 个镜头保存为结构化分镜草案。")

    return ScriptedModel(respond)


@asynccontextmanager
async def headless_app(
    monkeypatch,
    tmp_path,
    *,
    model=None,
    upstream=None,
    mode="live",
    provider="kling",
    wan_handler=None,
    **config,
):
    from conftest import VideoClock

    clock = VideoClock()
    upstream = upstream or KlingUpstream()
    home = tmp_path / "state"
    settings = {
        "home": home,
        "api_key": "deepseek-test-placeholder",
        "video_mode": mode,
        "video_provider": provider,
        "video_api_key": "video-test-secret",
        "video_workspace_id": "test-workspace",
        "kling_api_key": "kling-test-secret",
        "fal_api_key": "fal-test-secret",
        **config,
    }
    config_obj = Config(**settings)
    download = httpx.Response(
        200,
        content=SAMPLE.read_bytes(),
        headers={"Content-Type": "video/mp4", "Content-Length": str(SAMPLE.stat().st_size)},
    )

    def kling(api_key):
        return KlingAdapter(api_key=api_key, transport=httpx.MockTransport(upstream.handler), clock=clock)

    def fal(api_key):
        from vagent.video.providers.fal import FalAdapter

        return FalAdapter(
            api_key=api_key,
            transport=httpx.MockTransport(lambda _: httpx.Response(500, json={})),
            clock=clock,
        )

    def wan(**kwargs):
        from vagent.video.providers.wan import WanAdapter

        return WanAdapter(
            **kwargs,
            transport=httpx.MockTransport(wan_handler or (lambda _: httpx.Response(500, json={}))),
            clock=clock,
        )

    monkeypatch.setattr(application, "KlingAdapter", kling)
    monkeypatch.setattr(application, "FalAdapter", fal)
    monkeypatch.setattr(application, "WanAdapter", wan)
    monkeypatch.setattr(
        application,
        "MediaHttpClient",
        lambda: MediaHttpClient(transport=httpx.MockTransport(lambda _: download)),
    )
    monkeypatch.setattr(
        application,
        "JobService",
        lambda store, adapters, **kwargs: JobService(store, adapters, clock=clock, **kwargs),
    )
    monkeypatch.setattr(
        application, "JobWorker", lambda service: JobWorker(service, idle_interval_seconds=0.005)
    )
    monkeypatch.setattr(
        application,
        "MediaWorker",
        lambda media, client: MediaWorker(media, client, idle_interval_seconds=0.005),
    )
    async with ApplicationService.open(config_obj, model=model) as service:
        yield service, upstream, clock


async def planned(service, model, count=2):
    return await headless.plan(service, session_id="coffee", prompt="做一个雨夜咖啡店短片", model=model)


async def settle(task, clock, *, pause=0.01):
    """The controlled clock is advanced while the authorized flow waits for Jobs."""
    while not task.done():
        clock.advance(20)
        await asyncio.sleep(pause)
    return await task


async def test_plan_freezes_requests_and_budgets_without_submitting(monkeypatch, tmp_path):
    model = storyboard_model(2)
    async with headless_app(monkeypatch, tmp_path, model=model) as (service, upstream, _clock):
        result = await planned(service, model)
        assert result["formatVersion"] == 1 and result["status"] == "planned"
        assert result["runId"] and result["storyboardId"]
        assert result["storyboardVersion"] == 1 and result["storyboardRevision"] == 0
        assert result["planFingerprint"] and len(result["planFingerprint"]) == 64
        assert result["jobIds"] == [] and result["output"] is None
        assert result["estimates"] == [
            {
                "provider": "kling",
                "currency": "USD",
                "amount": "1.12",
                "units": 8,
                "jobCount": 2,
                "shots": result["estimates"][0]["shots"],
            }
        ]
        assert len(result["estimates"][0]["shots"]) == 2
        assert result["limits"]["maxJobs"] == 4
        assert result["limits"]["authorized"] is None
        state = service.store.snapshot()
        assert state["jobs"] == {} and state["media"] == {}
        assert not any(key.endswith("headless_generate") for key in state["operations"])
        assert not upstream.requests  # no submit during planning

        replay = await headless.plan(
            service,
            session_id="coffee",
            prompt="做一个雨夜咖啡店短片",
            model=storyboard_model(2),
            request_id=None,
        )
        assert replay["runId"] != result["runId"]
        assert replay["planFingerprint"] != result["planFingerprint"]  # a new plan is a new Run


async def test_plan_only_exposes_the_planning_tool_set(monkeypatch, tmp_path):
    model = storyboard_model(1)
    async with headless_app(monkeypatch, tmp_path, model=model) as (service, _upstream, _clock):
        runner = service.headless_runner(model=model)
        names = {spec["function"]["name"] for spec in runner.tools.specs()}
        assert {"storyboard_save", "storyboard_read", "video_capabilities"} <= names
        assert not {"shot_generate", "video_generate", "film_assemble"} & names
        assert {"shot_generate", "video_generate", "film_assemble"} <= set(
            spec["function"]["name"] for spec in service.tools.specs()
        )


async def test_plan_requires_a_storyboard_and_a_video_mode(monkeypatch, tmp_path):
    model = ScriptedModel(lambda messages, step: AIMessage(content="只给了一个文字方案。"))
    async with headless_app(monkeypatch, tmp_path, model=model) as (service, upstream, _clock):
        with pytest.raises(AppError) as incomplete:
            await headless.plan(service, session_id="coffee", prompt="只回复文字", model=model)
        assert incomplete.value.code == "HEADLESS_PLAN_INCOMPLETE"
        assert headless.exit_code(incomplete.value) == 2
        assert not upstream.requests and not service.store.snapshot()["jobs"]

    off_model = storyboard_model(1)
    async with headless_app(monkeypatch, tmp_path / "off", model=off_model, mode="off") as (
        service,
        _upstream,
        _clock,
    ):
        with pytest.raises(AppError) as off:
            await headless.plan(service, session_id="coffee", prompt="短片", model=off_model)
        assert off.value.code == "VIDEO_MODE_OFF" and headless.exit_code(off.value) == 2


@pytest.mark.parametrize(
    ("changes", "code"),
    [
        ({"max_cost": []}, "HEADLESS_BUDGET_NOT_COVERED"),
        ({"max_cost": ["kling:USD=1.00"], "max_units": ["kling=8"]}, "HEADLESS_BUDGET_NOT_COVERED"),
        ({"max_cost": ["kling:USD=2.41"], "max_units": ["kling=8"]}, "HEADLESS_BUDGET_TOO_HIGH"),
        ({"max_cost": ["kling:USD=1.12"], "max_units": ["kling=7"]}, "HEADLESS_BUDGET_NOT_COVERED"),
        ({"max_cost": ["kling:USD=1.12"], "max_units": ["kling=17"]}, "HEADLESS_BUDGET_TOO_HIGH"),
        (
            {"max_cost": ["kling:USD=1.12", "wan:CNY=1.00"], "max_units": ["kling=8"]},
            "HEADLESS_BUDGET_UNKNOWN",
        ),
        ({"max_cost": ["kling:USD=-1"], "max_units": ["kling=8"]}, "HEADLESS_BUDGET_INVALID"),
        (
            {"max_cost": ["kling:USD=1.12"], "max_units": ["kling=8"], "max_jobs": 1},
            "HEADLESS_BUDGET_NOT_COVERED",
        ),
        (
            {"max_cost": ["kling:USD=1.12"], "max_units": ["kling=8"], "max_jobs": 5},
            "HEADLESS_BUDGET_TOO_HIGH",
        ),
    ],
)
async def test_every_budget_bucket_is_checked_before_anything_is_registered(
    monkeypatch, tmp_path, changes, code
):
    model = storyboard_model(2)
    async with headless_app(monkeypatch, tmp_path, model=model) as (service, upstream, _clock):
        result = await planned(service, model)
        with pytest.raises(AppError) as rejected:
            headless.authorize(
                service,
                run_id=result["runId"],
                plan_fingerprint=result["planFingerprint"],
                confirm=True,
                **changes,
            )
        assert rejected.value.code == code
        assert headless.exit_code(rejected.value) == 3
        state = service.store.snapshot()
        assert state["jobs"] == {} and not upstream.requests
        assert state["runs"][result["runId"]]["headless"]["status"] == "planned"


async def test_fingerprint_and_confirmation_are_mandatory(monkeypatch, tmp_path):
    model = storyboard_model(2)
    async with headless_app(monkeypatch, tmp_path, model=model) as (service, upstream, _clock):
        result = await planned(service, model)
        with pytest.raises(AppError) as wrong:
            headless.authorize(
                service,
                run_id=result["runId"],
                plan_fingerprint="0" * 64,
                confirm=True,
                max_cost=["kling:USD=1.12"],
                max_units=["kling=8"],
            )
        assert (
            wrong.value.code == "HEADLESS_PLAN_FINGERPRINT_MISMATCH" and headless.exit_code(wrong.value) == 3
        )
        with pytest.raises(AppError) as unconfirmed:
            headless.authorize(
                service,
                run_id=result["runId"],
                plan_fingerprint=result["planFingerprint"],
                confirm=False,
                max_cost=["kling:USD=1.12"],
                max_units=["kling=8"],
            )
        assert unconfirmed.value.code == "HEADLESS_CONFIRMATION_REQUIRED"
        assert not upstream.requests and not service.store.snapshot()["jobs"]


async def test_authorized_plan_delivers_n_clips_and_resume_reuses_the_authorization(monkeypatch, tmp_path):
    model = storyboard_model(2)
    async with headless_app(monkeypatch, tmp_path, model=model) as (service, upstream, clock):
        result = await planned(service, model)
        authorized = headless.authorize(
            service,
            run_id=result["runId"],
            plan_fingerprint=result["planFingerprint"],
            confirm=True,
            max_cost=["kling:USD=1.12"],
            max_units=["kling=8"],
            max_jobs=2,
        )
        assert authorized["status"] == "authorized"
        assert authorized["limits"]["authorized"]["maxJobs"] == 2
        assert not upstream.requests  # authorization itself registers and submits nothing

        delivered = await settle(
            asyncio.create_task(headless.execute(service, run_id=result["runId"], wait_timeout=60)),
            clock,
        )
        assert delivered["status"] == "delivered" and delivered["filmVersion"] == 1
        assert len(delivered["jobIds"]) == 2
        assert delivered["output"]["sizeBytes"] > 0
        assert len(delivered["output"]["sha256"]) == 64
        from pathlib import Path

        path = Path(delivered["output"]["path"])
        assert path.is_file() and path.stat().st_size == delivered["output"]["sizeBytes"]
        assert len(upstream.submitted) == 2
        state = service.store.snapshot()
        assert len(state["films"]) == 1 and len(state["media"]) == 3

        # Repeating execute is a read-only replay: same film, no new Job or submit.
        replay = await headless.execute(service, run_id=result["runId"], wait_timeout=60)
        assert replay["output"] == delivered["output"] and replay["filmId"] == delivered["filmId"]
        assert len(upstream.submitted) == 2 and len(service.store.snapshot()["jobs"]) == 2
        with pytest.raises(AppError) as already:
            headless.authorize(
                service,
                run_id=result["runId"],
                plan_fingerprint=result["planFingerprint"],
                confirm=True,
                max_cost=["kling:USD=1.12"],
                max_units=["kling=8"],
            )
        assert already.value.code == "HEADLESS_ALREADY_AUTHORIZED" and headless.exit_code(already.value) == 3


async def test_wait_timeout_keeps_jobs_and_resume_finishes_them(monkeypatch, tmp_path):
    model = storyboard_model(1)
    upstream = KlingUpstream(status="processing")
    async with headless_app(monkeypatch, tmp_path, model=model, upstream=upstream) as (
        service,
        upstream,
        clock,
    ):
        result = await planned(service, model, count=1)
        headless.authorize(
            service,
            run_id=result["runId"],
            plan_fingerprint=result["planFingerprint"],
            confirm=True,
            max_cost=["kling:USD=0.56"],
            max_units=["kling=4"],
            max_jobs=1,
        )
        with pytest.raises(AppError) as timeout:
            await settle(
                asyncio.create_task(headless.execute(service, run_id=result["runId"], wait_timeout=0.2)),
                clock,
            )
        assert timeout.value.code == "HEADLESS_WAIT_TIMEOUT" and headless.exit_code(timeout.value) == 5
        state = service.store.snapshot()
        registration = state["runs"][result["runId"]]["headless"]["registration"]
        assert len(registration["jobIds"]) == 1 and len(state["jobs"]) == 1
        assert len(upstream.submitted) == 1
        waiting = headless.result_for_run(service.store, result["runId"])
        assert waiting["jobIds"] == registration["jobIds"] and waiting["output"] is None

        upstream.status = "succeeded"
        resumed = await settle(
            asyncio.create_task(headless.execute(service, run_id=result["runId"], wait_timeout=60)),
            clock,
        )
        assert resumed["status"] == "delivered"
        assert len(upstream.submitted) == 1  # resume never re-submits


async def test_a_storyboard_change_after_authorization_is_refused(monkeypatch, tmp_path):
    model = storyboard_model(1)
    async with headless_app(monkeypatch, tmp_path, model=model) as (service, upstream, clock):
        result = await planned(service, model, count=1)
        headless.authorize(
            service,
            run_id=result["runId"],
            plan_fingerprint=result["planFingerprint"],
            confirm=True,
            max_cost=["kling:USD=0.56"],
            max_units=["kling=4"],
        )
        board = service.store.snapshot()["storyboards"][result["storyboardId"]]
        changed = service.tools.execute(
            "storyboard_save",
            {
                "storyboardId": result["storyboardId"],
                "expectedVersion": board["currentVersion"],
                "title": "改过的分镜",
                "shots": [
                    {"description": "另一个镜头", "framing": "wide", "durationSeconds": 5, "mode": "t2v"}
                ],
            },
            store=service.store,
            project_id="coffee",
            operation_key="change:1:storyboard",
        )
        assert changed["ok"], changed
        with pytest.raises(AppError) as stale:
            await headless.execute(service, run_id=result["runId"], wait_timeout=5)
        assert stale.value.code == "HEADLESS_PLAN_STALE" and headless.exit_code(stale.value) == 6
        assert not upstream.requests and not service.store.snapshot()["jobs"]


def test_exit_codes_are_classified():
    assert headless.exit_code(AppError("INVALID_ARGUMENTS", "")) == 2
    assert headless.exit_code(AppError("HEADLESS_BUDGET_NOT_COVERED", "")) == 3
    assert headless.exit_code(AppError("FILM_ASSEMBLY_FAILED", "")) == 4
    assert headless.exit_code(AppError("HEADLESS_WAIT_TIMEOUT", "")) == 5
    assert headless.exit_code(AppError("HEADLESS_PLAN_STALE", "")) == 6
    assert headless.exit_code(AppError("CANCELLED", "")) == 130
    assert headless.exit_code(AppError("UNMAPPED_RUNTIME_CODE", "")) == 4


async def test_headless_plans_and_delivers_a_wan_cny_bucket(monkeypatch, tmp_path):
    def wan_succeeded(request):
        return httpx.Response(200, json=provider_response("SUCCEEDED", video_url=VIDEO_URL))

    model = storyboard_model(2)
    async with headless_app(
        monkeypatch, tmp_path, model=model, provider="wan", wan_handler=wan_succeeded
    ) as (service, _upstream, clock):
        result = await planned(service, model)
        assert result["estimates"][0] == {
            "provider": "wan",
            "currency": "CNY",
            "amount": "6.00",
            "units": None,
            "jobCount": 2,
            "shots": result["estimates"][0]["shots"],
        }
        assert result["limits"]["buckets"][0]["maxRunAmount"] == "12.00"
        authorized = headless.authorize(
            service,
            run_id=result["runId"],
            plan_fingerprint=result["planFingerprint"],
            confirm=True,
            max_cost=["wan:CNY=6.00"],
            max_jobs=2,
        )
        assert authorized["status"] == "authorized"
        delivered = await settle(
            asyncio.create_task(headless.execute(service, run_id=result["runId"], wait_timeout=60)),
            clock,
        )
        assert delivered["status"] == "delivered" and delivered["filmVersion"] == 1
        assert len(delivered["jobIds"]) == 2


async def test_a_cancelled_wait_preserves_the_registration(monkeypatch, tmp_path):
    model = storyboard_model(1)
    upstream = KlingUpstream(status="processing")
    async with headless_app(monkeypatch, tmp_path, model=model, upstream=upstream) as (
        service,
        upstream,
        _clock,
    ):
        result = await planned(service, model, count=1)
        headless.authorize(
            service,
            run_id=result["runId"],
            plan_fingerprint=result["planFingerprint"],
            confirm=True,
            max_cost=["kling:USD=0.56"],
            max_units=["kling=4"],
        )
        cancelled = asyncio.Event()
        cancelled.set()
        with pytest.raises(headless.HeadlessCancelled):
            await headless.execute(service, run_id=result["runId"], wait_timeout=30, cancelled=cancelled)
        state = service.store.snapshot()
        assert len(state["jobs"]) == 1 and state["films"] == {}
        assert not upstream.submitted  # cancellation never forces a submit


async def test_a_new_user_skill_refuses_the_planned_run(monkeypatch, tmp_path):
    model = storyboard_model(1)
    async with headless_app(monkeypatch, tmp_path, model=model) as (service, _upstream, _clock):
        result = await planned(service, model, count=1)
        home = service.store.home
    folder = home / "skills" / "late-skill"
    folder.mkdir(parents=True)
    (folder / "SKILL.md").write_text("---\nname: late-skill\ndescription: late\n---\nbody", encoding="utf-8")
    async with headless_app(monkeypatch, tmp_path, model=model) as (service, _upstream, _clock):
        with pytest.raises(AppError) as changed:
            headless.authorize(
                service,
                run_id=result["runId"],
                plan_fingerprint=result["planFingerprint"],
                confirm=True,
                max_cost=["kling:USD=0.56"],
                max_units=["kling=4"],
            )
        assert changed.value.code == "RESUME_CONFIG_CHANGED"
        assert headless.exit_code(changed.value) == 6
        assert not service.store.snapshot()["jobs"]


async def test_mock_headless_authorizes_without_a_paid_bucket(monkeypatch, tmp_path):
    model = storyboard_model(1)
    async with headless_app(monkeypatch, tmp_path, model=model, mode="mock") as (
        service,
        upstream,
        clock,
    ):
        result = await planned(service, model, count=1)
        assert result["estimates"] == [] and result["limits"]["authorized"] is None
        authorized = headless.authorize(
            service,
            run_id=result["runId"],
            plan_fingerprint=result["planFingerprint"],
            confirm=True,
            max_jobs=1,
        )
        assert authorized["status"] == "authorized"
        with pytest.raises(AppError) as unavailable:
            await settle(
                asyncio.create_task(headless.execute(service, run_id=result["runId"], wait_timeout=10)),
                clock,
            )
        # Mock registration is local; it delivers no media, so assembly refuses.
        assert unavailable.value.code in {"FILM_INPUT_NOT_READY", "HEADLESS_JOB_FAILED"}
        assert headless.exit_code(unavailable.value) == 4
        assert len(service.store.snapshot()["jobs"]) == 1 and not upstream.requests
