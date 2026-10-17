"""M2 batch generation: registration guardrails, dedup, redo and set waits.

The offline half drives the mock provider; the money-critical half drives a
controlled HTTP upstream through the frozen Wan protocol, so nothing is spent.
"""

import json
from decimal import Decimal

import httpx
from conftest import ScriptedModel, tool_call
from langchain_core.messages import AIMessage
from shot_support import (
    generation_args,
    insert_image,
    jobs_of,
    m2_run,
    mock_tools,
    redo_args,
    revise_storyboard,
    save_storyboard,
    shot,
    shot_versions,
    storyboard_record,
)
from wan_support import VIDEO_URL, live_service, provider_response

from vagent.runner import AgentRunner
from vagent.storage import FileStore
from vagent.video.contracts import (
    JobV3,
    parse_job,
)
from vagent.video.tools import job_settled, resolve_jobset_wait
from vagent.video.worker import JobWorker
from vagent.wait_runtime import WaitCoordinator, WaitService
from vagent.waiting import ToolExecutionContext, WaitBinding, WaitBindingV3, parse_wait


class Upstream:
    """A controlled Wan upstream: one task per submission, scripted per task."""

    def __init__(self):
        self.calls = []
        self.status = {}
        self.submits = 0

    def handler(self, request):
        self.calls.append(request)
        if request.method == "POST":
            self.submits += 1
            task_id = f"wan-{self.submits}"
            self.status[task_id] = "PENDING"
            return httpx.Response(200, json=provider_response("PENDING", task_id=task_id))
        task_id = request.url.path.rsplit("/", 1)[-1]
        status = self.status.get(task_id, "PENDING")
        if status == "SUCCEEDED":
            return httpx.Response(
                200,
                json={
                    **provider_response("SUCCEEDED", task_id=task_id, video_url=VIDEO_URL),
                    "usage": {"duration": 5},
                },
            )
        return httpx.Response(200, json=provider_response(status, task_id=task_id))

    @property
    def submitted(self):
        return [call for call in self.calls if call.method == "POST"]


def shot_context(run_id="shot-run", *, model_step=1, tool_call_id="batch"):
    """A tool context for an existing M2 Run; it never rewrites the run record."""
    return ToolExecutionContext(
        project_id="coffee",
        session_id="coffee",
        run_id=run_id,
        model_step=model_step,
        tool_call_id=tool_call_id,
    )


async def drive(service, clock, rounds=12, step=30):
    """Advance the test clock and drain the serial Worker until the queue is idle."""
    worker = JobWorker(service)
    for _ in range(rounds):
        clock.advance(step)
        for _ in range(4):
            if await worker.run_once() is None:
                break
    return [parse_job(raw) for raw in jobs_of(service.store, "shot-run")]


# --------------------------------------------------------------- mock batches


def test_mock_batch_registers_one_job_per_shot_in_order(store, video_clock):
    registry, service, adapter, _ = mock_tools(store, clock=video_clock)
    saved = save_storyboard(registry, store, [shot("镜头一"), shot("镜头二"), shot("镜头三")])
    m2_run(store, "shot-run", mode="mock")
    result = service.shot_generate(generation_args(saved), context=shot_context())
    assert result["ok"], result
    data = result["data"]
    assert data["simulated"] and not data["mediaAvailable"]
    assert data["totalEstimate"] is None and data["limits"] is None
    assert [item["shotId"] for item in data["shots"]] == saved["shotIds"]
    assert all(item["estimate"] is None and not item["reused"] for item in data["shots"])
    jobs = jobs_of(store, "shot-run")
    assert [job["id"] for job in jobs] == [item["jobId"] for item in data["shots"]]
    assert all(job["mode"] == "mock" for job in jobs)
    assert adapter.ledger_snapshot()["submitCalls"] == 0
    for shot_id in saved["shotIds"]:
        versions = shot_versions(store, saved["storyboardId"], shot_id)
        assert len(versions) == 1 and versions[0]["request"]["model"] == "mock-t2v"


def test_batch_requires_confirmation_and_registers_nothing_on_failure(store, video_clock):
    registry, service, adapter, _ = mock_tools(store, clock=video_clock)
    saved = save_storyboard(registry, store, [shot("镜头一"), shot("镜头二")])
    m2_run(store, "shot-run", mode="mock")
    cases = [
        (generation_args(saved, confirm=False), "CONFIRMATION_REQUIRED"),
        (generation_args(saved, shot_ids=[saved["shotIds"][0], "not-a-shot"]), "SHOT_NOT_FOUND"),
        (generation_args(saved, version=saved["version"] + 1), "VERSION_CONFLICT"),
        ({**generation_args(saved), "shotIds": []}, "INVALID_ARGUMENTS"),
    ]
    for args, code in cases:
        result = service.shot_generate(args, context=shot_context(tool_call_id=code))
        assert result["error"]["code"] == code, (code, result)
    assert not store.snapshot()["jobs"]
    assert not storyboard_record(store, saved["storyboardId"])["shotVersions"]
    assert adapter.ledger_snapshot()["submitCalls"] == 0


def test_batch_replay_and_repeated_requests_reuse_the_same_jobs(store, video_clock):
    registry, service, _, _ = mock_tools(store, clock=video_clock)
    saved = save_storyboard(registry, store, [shot("镜头一"), shot("镜头二")])
    m2_run(store, "shot-run", mode="mock")
    args = generation_args(saved)
    first = service.shot_generate(args, context=shot_context())
    assert first["ok"], first
    assert service.shot_generate(args, context=shot_context()) == first
    conflict = service.shot_generate({**args, "shotIds": [saved["shotIds"][0]]}, context=shot_context())
    assert conflict["error"]["code"] == "OPERATION_CONFLICT"
    m2_run(store, "second-run", mode="mock")
    again = service.shot_generate(args, context=shot_context("second-run"))
    assert again["ok"], again
    assert [item["jobId"] for item in again["data"]["shots"]] == [
        item["jobId"] for item in first["data"]["shots"]
    ]
    assert all(item["reused"] for item in again["data"]["shots"])
    assert len(store.snapshot()["jobs"]) == 2
    assert len(storyboard_record(store, saved["storyboardId"])["shotVersions"]) == 2


def test_new_storyboard_version_registers_new_jobs_without_touching_history(store, video_clock):
    registry, service, _, _ = mock_tools(store, clock=video_clock)
    saved = save_storyboard(registry, store, [shot("镜头一"), shot("镜头二")])
    m2_run(store, "shot-run", mode="mock")
    first = service.shot_generate(generation_args(saved), context=shot_context())
    revised = revise_storyboard(
        registry,
        store,
        saved,
        [
            {"id": saved["shotIds"][0], **shot("镜头一改")},
            {"id": saved["shotIds"][1], **shot("镜头二")},
        ],
    )
    before = json.dumps(storyboard_record(store, saved["storyboardId"])["shotVersions"], sort_keys=True)
    m2_run(store, "second-run", mode="mock")
    second = service.shot_generate(
        generation_args(revised, shot_ids=[saved["shotIds"][0]]), context=shot_context("second-run")
    )
    assert second["ok"] and not second["data"]["shots"][0]["reused"]
    assert second["data"]["shots"][0]["jobId"] != first["data"]["shots"][0]["jobId"]
    record = storyboard_record(store, saved["storyboardId"])
    assert len(record["shotVersions"]) == 3
    assert json.dumps(record["shotVersions"][:2], sort_keys=True) == before
    assert len(shot_versions(store, saved["storyboardId"], saved["shotIds"][0])) == 2
    assert len(shot_versions(store, saved["storyboardId"], saved["shotIds"][1])) == 1


def test_storyboard_scope_and_read_only_runs_are_enforced(store, video_clock):
    registry, service, _, _ = mock_tools(store, clock=video_clock)
    store.ensure_session("other")
    foreign = save_storyboard(registry, store, [shot("别人的镜头")], project_id="other", key="other")
    m2_run(store, "shot-run", mode="mock")
    assert (
        service.shot_generate(generation_args(foreign), context=shot_context())["error"]["code"]
        == "NOT_FOUND"
    )
    m2_run(store, "reader-run", mode="mock", read_only=True)
    refused = service.shot_generate(generation_args(foreign), context=shot_context("reader-run"))
    assert refused["error"]["code"] in {"NOT_FOUND", "READ_ONLY"}
    assert not store.snapshot()["jobs"]


# ------------------------------------------------------------------ redo


def test_redo_adds_one_version_and_one_job_for_the_target_shot_only(store, video_clock):
    registry, service, _, _ = mock_tools(store, clock=video_clock)
    saved = save_storyboard(registry, store, [shot("镜头一"), shot("镜头二"), shot("镜头三")])
    m2_run(store, "shot-run", mode="mock")
    first = service.shot_generate(generation_args(saved), context=shot_context())
    before = storyboard_record(store, saved["storyboardId"])
    target = saved["shotIds"][1]
    others = [item for item in first["data"]["shots"] if item["shotId"] != target]
    m2_run(store, "redo-run", mode="mock")
    result = service.redo_shot(
        redo_args(saved, target, revision=before["revision"]), context=shot_context("redo-run")
    )
    assert result["ok"], result
    entry = result["data"]["shots"][0]
    assert entry["shotId"] == target and not entry["reused"]
    assert entry["jobId"] not in [item["jobId"] for item in others]
    after = storyboard_record(store, saved["storyboardId"])
    assert after["versions"] == before["versions"] and after["currentVersion"] == before["currentVersion"]
    assert after["shotVersions"][: len(before["shotVersions"])] == before["shotVersions"]
    assert len(shot_versions(store, saved["storyboardId"], target)) == 2
    for item in others:
        versions = shot_versions(store, saved["storyboardId"], item["shotId"])
        assert [version["id"] for version in versions] == [item["shotVersionId"]]
        assert versions[0]["jobId"] == item["jobId"]
    assert len(store.snapshot()["jobs"]) == 4


def test_repeated_and_stale_redo_calls_are_idempotent(store, video_clock):
    registry, service, _, _ = mock_tools(store, clock=video_clock)
    saved = save_storyboard(registry, store, [shot("镜头一")])
    m2_run(store, "shot-run", mode="mock")
    service.shot_generate(generation_args(saved), context=shot_context())
    revision = storyboard_record(store, saved["storyboardId"])["revision"]
    args = redo_args(saved, saved["shotIds"][0], revision=revision, client_request_id="redo-1")
    m2_run(store, "redo-run", mode="mock")
    first = service.redo_shot(args, context=shot_context("redo-run"))
    assert first["ok"], first
    assert service.redo_shot(args, context=shot_context("redo-run")) == first
    stale = service.redo_shot(
        {**args, "clientRequestId": "redo-2"}, context=shot_context("redo-run", tool_call_id="redo-2")
    )
    assert stale["error"]["code"] == "VERSION_CONFLICT"
    assert len(store.snapshot()["jobs"]) == 2


# ------------------------------------------------------- runner end to end


async def test_mock_runner_generates_three_shots_and_waits_as_one_set(store, video_clock):
    registry, service, adapter, _ = mock_tools(store, clock=video_clock)
    saved = {}

    def respond(messages, step):
        if step == 0:
            return tool_call(
                "storyboard_save",
                {"title": "雨夜咖啡店三镜头", "shots": [shot("镜头一"), shot("镜头二"), shot("镜头三")]},
                call_id="save",
            )
        result = json.loads(messages[-1].content)
        if step == 1:
            assert result["ok"] and result["data"]["version"] == 1
            saved.update(result["data"])
            return tool_call("shot_generate", generation_args(result["data"]), call_id="generate")
        if step == 2:
            assert result["ok"] and len(result["data"]["shots"]) == 3
            saved["jobIds"] = [item["jobId"] for item in result["data"]["shots"]]
            return tool_call("await_job", {"jobIds": saved["jobIds"]}, call_id="wait")
        assert result["ok"] and result["data"]["succeeded"] == 3 and result["data"]["failed"] == 0
        # Set members keep one stable sorted order, not the order of the call.
        assert sorted(item["jobId"] for item in result["data"]["jobs"]) == sorted(saved["jobIds"])
        return AIMessage(content=f"{len(result['data']['jobs'])} 个镜头任务已完成。")

    model = ScriptedModel(respond)
    runner = AgentRunner(store=store, model=model, tools=registry)
    suspended = await runner.run("coffee", "三镜头分镜并生成")
    assert suspended["status"] == "waiting_external", suspended["answer"]
    assert suspended["toolFeatures"]["video"]["toolsVersion"] == 4
    binding = parse_wait(next(iter(store.snapshot()["waits"].values())))
    assert isinstance(binding, WaitBindingV3) and binding.resource.kind == "jobset"
    assert list(binding.job_ids) == sorted(saved["jobIds"])
    assert not [key for key in store.snapshot()["operations"] if key.endswith(":wait")]
    await drive(service, video_clock)
    assert all(parse_job(raw).status == "succeeded" for raw in jobs_of(store, suspended["id"]))
    coordinator = WaitCoordinator(
        store, lambda _: runner, resolvers=registry.wait_resolvers, clock=video_clock
    )
    final = await coordinator.run_once()
    assert final["status"] == "completed", final.get("waitResumeError") or final["answer"]
    assert "3 个镜头任务已完成" in final["answer"]
    assert final["toolCalls"] == 3 and model.calls == 4
    assert adapter.ledger_snapshot()["submitCalls"] == 3
    assert parse_wait(next(iter(store.snapshot()["waits"].values()))).status == "delivered"


# ------------------------------------------------------- live guardrails


async def test_live_batch_enforces_the_per_job_and_per_run_ceilings(store, video_clock):
    upstream = Upstream()
    async with live_service(store, video_clock, upstream.handler) as (service, _):
        registry, _, _, _ = mock_tools(store, clock=video_clock)
        over_job = save_storyboard(registry, store, [shot("六秒镜头", durationSeconds=6)])
        over_count = save_storyboard(
            registry, store, [shot(f"镜头{index}") for index in range(5)], key="five", title="五镜头"
        )
        m2_run(store, "shot-run", mode="live")
        over_job_result = service.shot_generate(
            generation_args(over_job), context=shot_context(tool_call_id="over-job")
        )
        assert over_job_result["error"]["code"] == "VIDEO_COST_LIMIT"
        over_count_result = service.shot_generate(
            generation_args(over_count), context=shot_context(tool_call_id="over-count")
        )
        assert over_count_result["error"]["code"] == "VIDEO_RUN_JOB_LIMIT"
        assert not store.snapshot()["jobs"] and upstream.submitted == []
        assert not storyboard_record(store, over_count["storyboardId"])["shotVersions"]


async def test_live_quota_snapshot_is_recomputed_from_persisted_jobs(store, video_clock):
    upstream = Upstream()
    budget = {"video_max_run_cost": "8.00"}
    async with live_service(store, video_clock, upstream.handler, **budget) as (service, _):
        registry, _, _, _ = mock_tools(store, clock=video_clock)
        saved = save_storyboard(registry, store, [shot("镜头一"), shot("镜头二")])
        m2_run(store, "shot-run", mode="live")
        first = service.shot_generate(generation_args(saved), context=shot_context())
        assert first["ok"], first
        revised = revise_storyboard(
            registry,
            store,
            saved,
            [
                {"id": saved["shotIds"][0], **shot("镜头一改")},
                {"id": saved["shotIds"][1], **shot("镜头二")},
            ],
        )
        assert first["data"]["limits"] == {
            "maxJobCost": "3.00",
            "maxNewJobsPerRun": 4,
            "maxRunCost": "8.00",
            "usedNewJobs": 2,
            "usedAmount": "6.00",
            "remainingAmount": "2.00",
        }
        assert first["data"]["totalEstimate"] == {"currency": "CNY", "amount": "6.00"}
        jobs = [parse_job(raw) for raw in jobs_of(store, "shot-run")]
        assert all(isinstance(job, JobV3) for job in jobs)
        assert [job.cost.estimate.amount for job in jobs] == ["3.00", "3.00"]
        assert [job.shot_ref.shot_id for job in jobs] == saved["shotIds"]
        assert upstream.submitted == []
        home = store.home
        store.close()
    # A restart recomputes usage from the persisted Jobs: the budget does not reset.
    with FileStore.open(home) as reopened:
        async with live_service(reopened, video_clock, upstream.handler, **budget) as (service, _):
            second = service.shot_generate(
                generation_args(revised), context=shot_context(tool_call_id="second-batch")
            )
            assert second["error"]["code"] == "VIDEO_RUN_COST_LIMIT"
            assert len(reopened.snapshot()["jobs"]) == 2
            assert sum(
                Decimal(parse_job(raw).cost.estimate.amount) for raw in reopened.snapshot()["jobs"].values()
            ) == Decimal("6.00")


async def test_live_set_wait_reports_per_shot_results_and_partial_failures(store, video_clock):
    upstream = Upstream()
    async with live_service(store, video_clock, upstream.handler) as (service, _):
        registry, _, _, _ = mock_tools(store, clock=video_clock)
        saved = save_storyboard(registry, store, [shot("镜头一"), shot("镜头二")])
        m2_run(store, "shot-run", mode="live")
        registered = service.shot_generate(generation_args(saved), context=shot_context())
        assert registered["ok"], registered
        await drive(service, video_clock)
        assert len(upstream.submitted) == 2
        upstream.status[sorted(upstream.status)[0]] = "FAILED"
        jobs = await drive(service, video_clock)
        assert sorted(job.status for job in jobs) == ["failed", "queued"]
        assert any(job_settled(job) for job in jobs) and not all(job_settled(job) for job in jobs)
        binding = WaitBindingV3(
            id="set-wait",
            context=ToolExecutionContext(
                project_id="coffee", session_id="coffee", run_id="wait-run", model_step=1, tool_call_id="w"
            ),
            resource={"kind": "jobset", "id": "set-1"},
            jobIds=tuple(sorted(job.id for job in jobs)),
            startedAt="2030-01-01T00:00:00+00:00",
            deadlineAt="2030-12-31T00:00:00+00:00",
        )
        assert resolve_jobset_wait(store, binding, service=service) is None
        for task_id in upstream.status:
            upstream.status[task_id] = "FAILED"
        await drive(service, video_clock)
        resolved = resolve_jobset_wait(store, binding, service=service)
        summary = resolved["data"]
        assert summary["failed"] == 2 and summary["succeeded"] == 0
        assert all(item["error"]["code"] == "JOB_FAILED" for item in summary["jobs"])
        assert summary["jobIds"] == sorted(job.id for job in jobs)
        assert upstream.submitted.__len__() == 2


async def test_live_set_wait_timeout_returns_partial_results_and_can_be_repeated(store, video_clock):
    upstream = Upstream()
    async with live_service(store, video_clock, upstream.handler) as (service, _):
        registry, _, _, _ = mock_tools(store, clock=video_clock)
        saved = save_storyboard(registry, store, [shot("镜头一"), shot("镜头二")])
        m2_run(store, "shot-run", mode="live")
        registered = service.shot_generate(generation_args(saved), context=shot_context())
        job_ids = sorted(item["jobId"] for item in registered["data"]["shots"])
        m2_run(store, "wait-run", mode="live")
        waiter = shot_context("wait-run", model_step=2, tool_call_id="wait")
        deferred = registry.execute(
            "await_job",
            {"jobIds": job_ids},
            store=store,
            project_id="coffee",
            operation_key=waiter.operation_key,
            context=waiter,
        )
        assert deferred.kind == "deferred" and deferred.resource.kind == "jobset"
        binding = parse_wait(store.snapshot()["waits"][deferred.wait_id])
        assert isinstance(binding, WaitBindingV3) and list(binding.job_ids) == job_ids
        video_clock.advance(601)
        expired = resolve_jobset_wait(store, binding, service=service)
        assert expired["data"]["failed"] == 2 and "已到期" in expired["data"]["summary"]
        again = registry.execute(
            "await_job",
            {"jobIds": job_ids},
            store=store,
            project_id="coffee",
            operation_key=f"{waiter.operation_key}-again",
            context=waiter.model_copy(update={"tool_call_id": "wait-again"}),
        )
        assert again.kind == "deferred" and again.wait_id != deferred.wait_id
        assert len(store.snapshot()["waits"]) == 2
        assert upstream.submitted == []


async def test_set_wait_survives_restart_and_stop_keeps_the_jobs(store, video_clock):
    upstream = Upstream()
    async with live_service(store, video_clock, upstream.handler) as (service, _):
        registry, _, _, _ = mock_tools(store, clock=video_clock)
        saved = save_storyboard(registry, store, [shot("镜头一"), shot("镜头二")])
        m2_run(store, "shot-run", mode="live")
        registered = service.shot_generate(generation_args(saved), context=shot_context())
        job_ids = sorted(item["jobId"] for item in registered["data"]["shots"])
        m2_run(store, "wait-run", mode="live")
        waiter = shot_context("wait-run", model_step=2, tool_call_id="wait")
        deferred = registry.execute(
            "await_job",
            {"jobIds": job_ids},
            store=store,
            project_id="coffee",
            operation_key=waiter.operation_key,
            context=waiter,
        )
        home = store.home
        persisted = json.dumps(store.snapshot()["waits"], sort_keys=True)
        store.close()
    with FileStore.open(home) as reopened:
        assert json.dumps(reopened.snapshot()["waits"], sort_keys=True) == persisted
        binding = parse_wait(reopened.snapshot()["waits"][deferred.wait_id])
        assert isinstance(binding, WaitBindingV3) and list(binding.job_ids) == job_ids
        # A wait only exists inside an unfinished Run, so model that state first.
        reopened.transaction(
            lambda draft: draft["runs"]["wait-run"].update(status="waiting_external", activeWaitId=None)
        )
        assert WaitService(reopened, clock=video_clock).stop_run("wait-run")
        stopped = parse_wait(reopened.snapshot()["waits"][deferred.wait_id])
        assert stopped.status == "stopped" and not stopped.auto_resume
        assert len(reopened.snapshot()["jobs"]) == 2
        assert all(
            raw["status"] in {"pending_submit", "submitting", "queued"}
            for raw in reopened.snapshot()["jobs"].values()
        )


def test_single_element_wait_uses_the_frozen_job_binding(store, video_clock):
    registry, service, _, _ = mock_tools(store, clock=video_clock)
    saved = save_storyboard(registry, store, [shot("镜头一")])
    m2_run(store, "shot-run", mode="mock")
    registered = service.shot_generate(generation_args(saved), context=shot_context())
    job_id = registered["data"]["shots"][0]["jobId"]
    waiter = shot_context("shot-run", model_step=2, tool_call_id="wait")
    deferred = registry.execute(
        "await_job",
        {"jobIds": [job_id]},
        store=store,
        project_id="coffee",
        operation_key=waiter.operation_key,
        context=waiter,
    )
    binding = parse_wait(store.snapshot()["waits"][deferred.wait_id])
    assert isinstance(binding, WaitBinding) and not isinstance(binding, WaitBindingV3)
    assert binding.resource.kind == "job" and binding.resource.id == job_id
    job = parse_job(store.snapshot()["jobs"][job_id])
    assert not job_settled(job)


async def test_mock_runner_regenerates_one_shot_without_touching_the_others(store, video_clock):
    """N shots through the real Agent loop, then one redo of a single shot."""
    registry, service, adapter, _ = mock_tools(store, clock=video_clock)
    saved = {}

    def first_run(messages, step):
        if step == 0:
            return tool_call(
                "storyboard_save",
                {"title": "两镜头", "shots": [shot("镜头一"), shot("镜头二")]},
                call_id="save",
            )
        result = json.loads(messages[-1].content)
        if step == 1:
            assert result["ok"] and result["data"]["version"] == 1
            saved.update(result["data"])
            return tool_call("shot_generate", generation_args(result["data"]), call_id="generate")
        assert result["ok"] and len(result["data"]["shots"]) == 2
        saved["jobIds"] = [item["jobId"] for item in result["data"]["shots"]]
        return AIMessage(content="两个镜头已登记。")

    runner = AgentRunner(store=store, model=ScriptedModel(first_run), tools=registry)
    first = await runner.run("coffee", "两镜头分镜并生成")
    assert first["status"] == "completed", first["answer"]
    assert len(saved["jobIds"]) == 2

    def redo_run(messages, step):
        if step == 0:
            return tool_call(
                "storyboard_save",
                {
                    "storyboardId": saved["storyboardId"],
                    "expectedVersion": saved["version"],
                    "title": "两镜头",
                    "shots": [
                        {"id": saved["shotIds"][0], **shot("镜头一")},
                        {"id": saved["shotIds"][1], **shot("镜头二改：推近后转向窗外")},
                    ],
                },
                call_id="revise",
            )
        result = json.loads(messages[-1].content)
        if step == 1:
            assert result["ok"] and result["data"]["version"] == 2
            return tool_call(
                "shot_generate",
                generation_args(result["data"], shot_ids=[saved["shotIds"][1]]),
                call_id="redo",
            )
        assert result["ok"] and len(result["data"]["shots"]) == 1
        assert not result["data"]["shots"][0]["reused"]
        return AIMessage(content="第二个镜头已重做。")

    redo = AgentRunner(store=store, model=ScriptedModel(redo_run), tools=registry)
    second = await redo.run("coffee", "只重做第二个镜头")
    assert second["status"] == "completed", second["answer"]
    record = storyboard_record(store, saved["storyboardId"])
    assert [version["version"] for version in record["versions"]] == [1, 2]
    assert record["currentVersion"] == 2
    untouched, redone = saved["shotIds"]
    assert len(shot_versions(store, saved["storyboardId"], untouched)) == 1
    assert shot_versions(store, saved["storyboardId"], untouched)[0]["jobId"] == saved["jobIds"][0]
    versions = shot_versions(store, saved["storyboardId"], redone)
    assert len(versions) == 2 and versions[0]["jobId"] == saved["jobIds"][1]
    assert versions[1]["jobId"] != saved["jobIds"][1]
    assert len(store.snapshot()["jobs"]) == 3
    assert adapter.ledger_snapshot()["submitCalls"] == 0


def race(call, count=4):
    """Run one call from several threads at once and collect every outcome."""
    import threading

    results = []
    threads = [threading.Thread(target=lambda: results.append(call())) for _ in range(count)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    return results


def test_racing_batch_calls_register_one_set(store, video_clock):
    registry, service, _, _ = mock_tools(store, clock=video_clock)
    saved = save_storyboard(registry, store, [shot("镜头一"), shot("镜头二")])
    m2_run(store, "shot-run", mode="mock")
    context = shot_context(tool_call_id="race")
    args = generation_args(saved)
    results = race(lambda: service.shot_generate(args, context=context))
    assert len(results) == 4 and all(result == results[0] for result in results), results[0]
    assert results[0]["ok"], results[0]
    assert len(store.snapshot()["jobs"]) == 2
    assert len([key for key in store.snapshot()["operations"] if "race" in key]) == 1
    record = storyboard_record(store, saved["storyboardId"])
    assert len(record["shotVersions"]) == 2 and record["revision"] == 1


def test_racing_redo_calls_register_one_version(store, video_clock):
    registry, service, _, _ = mock_tools(store, clock=video_clock)
    saved = save_storyboard(registry, store, [shot("镜头一")])
    m2_run(store, "shot-run", mode="mock")
    service.shot_generate(generation_args(saved), context=shot_context())
    revision = storyboard_record(store, saved["storyboardId"])["revision"]
    m2_run(store, "redo-run", mode="mock")
    args = redo_args(saved, saved["shotIds"][0], revision=revision, client_request_id="race")
    context = shot_context("redo-run", tool_call_id="race")
    results = race(lambda: service.redo_shot(args, context=context))
    assert len(results) == 4 and all(result == results[0] for result in results), results[0]
    assert results[0]["ok"], results[0]
    assert len(store.snapshot()["jobs"]) == 2
    assert len([key for key in store.snapshot()["operations"] if "race" in key]) == 1
    assert len(shot_versions(store, saved["storyboardId"], saved["shotIds"][0])) == 2


def test_legacy_runs_still_hold_at_most_one_job(store, video_clock):
    registry, service, _, _ = mock_tools(store, clock=video_clock)
    saved = save_storyboard(registry, store, [shot("镜头一"), shot("镜头二")])
    m2_run(store, "legacy-run", mode="mock")
    store.transaction(
        lambda draft: draft["runs"]["legacy-run"]["toolFeatures"].update(
            {"video": {"mode": "mock", "toolsVersion": 2, "rulesVersion": 2, "capabilities": []}}
        )
    )
    result = service.shot_generate(generation_args(saved), context=shot_context("legacy-run"))
    assert result["error"]["code"] == "JOB_CONFLICT"
    assert not store.snapshot()["jobs"]


def test_mock_rejects_image_modes_without_a_verified_capability(store, video_clock):
    registry, service, _, _ = mock_tools(store, clock=video_clock)
    m2_run(store, "shot-run", mode="mock")
    asset = insert_image(store)
    saved = save_storyboard(registry, store, [shot("首帧起手", mode="i2v", referenceAssetIds=[asset])])
    result = service.shot_generate(generation_args(saved), context=shot_context())
    assert result["error"]["code"] == "VIDEO_UNSUPPORTED_MODE"
    assert not store.snapshot()["jobs"]
