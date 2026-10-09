import asyncio
import json
from datetime import timedelta
from uuid import uuid4

import pytest
from conftest import ScriptedModel, tool_call, video_request, video_run
from langchain_core.messages import AIMessage, ToolMessage, messages_from_dict

from vagent.application import ApplicationService
from vagent.config import Config
from vagent.context import assert_complete_protocol
from vagent.errors import AppError
from vagent.runner import AgentRunner, RunPolicy
from vagent.storage import FileStore
from vagent.tools import Arguments, ToolDefinition, ToolRegistry, create_project_tools
from vagent.video.jobs import JobService
from vagent.video.providers.mock import MockScenario, MockVideoAdapter
from vagent.video.tools import register_video_tools, resolve_job_wait
from vagent.video.worker import JobWorker
from vagent.wait_runtime import WaitCoordinator, WaitService
from vagent.waiting import DeferredToolResult, ExternalResourceRef, WaitBinding


def save_call(name):
    return {"id": name, "name": "artifact_save", "args": {"kind": "brief", "title": name, "content": name}}


class Harness:
    def __init__(self, store, clock, *, scenario=None, policy=None, on_event=None, read_only=False):
        self.store, self.clock = store, clock
        self.adapter = MockVideoAdapter(
            store, clock=clock, scenario=scenario or MockScenario(states=["succeeded"])
        )
        self.jobs = JobService(store, [self.adapter], clock=clock)
        self.tools = register_video_tools(create_project_tools(), self.jobs)
        self.worker = JobWorker(self.jobs)
        self.policy, self.on_event, self.read_only = policy, on_event, read_only
        self.model = None
        self.coordinator = WaitCoordinator(
            store,
            lambda _: self.runner(),
            clock=clock,
            resolvers={"job": lambda binding: resolve_job_wait(store, binding)},
            on_event=on_event,
        )

    def runner(self, model=None, **kwargs):
        return AgentRunner(
            store=self.store,
            model=model or self.model,
            tools=self.tools,
            clock=self.clock,
            policy=self.policy,
            on_event=self.on_event,
            read_only=self.read_only,
            **kwargs,
        )

    def job(self, name="owner"):
        context = video_run(self.store, name)
        return self.jobs.generate(video_request(self.adapter), context=context)["data"]["jobId"]

    async def begin(self, job_ids=None, *, after=None, writes=True):
        job_ids = job_ids or [self.job()]
        calls = []
        for index, job_id in enumerate(job_ids):
            if writes:
                calls.append(save_call(f"save-{index}"))
            calls.append({"id": f"await-{index}", "name": "await_job", "args": {"jobId": job_id}})
        if writes:
            calls.append(save_call(f"save-{len(job_ids)}"))

        def respond(messages, step):
            if step == 0:
                return AIMessage(content="", tool_calls=calls)
            assert_complete_protocol(messages)
            return after(messages) if after else AIMessage(content="模拟任务结果已确认，没有真实媒体。")

        self.model = ScriptedModel(respond)
        return await self.runner().run("coffee", "等待模拟任务", request_id="waiting")

    async def complete_jobs(self):
        for _ in range(20):
            job = await self.worker.run_once()
            if job and job.next_poll_at:
                self.clock.due(job)
            elif job is None:
                return
        raise AssertionError("Worker did not drain this bounded fixture")


def outcomes(record):
    messages = messages_from_dict(record["messages"])
    assert_complete_protocol(messages)
    return {
        message.tool_call_id: json.loads(message.content)
        for message in messages
        if isinstance(message, ToolMessage)
    }


async def test_wait_releases_graph_freezes_budget_and_delivers_original_batch_once(store, video_clock):
    harness = Harness(store, video_clock, policy=RunPolicy(max_steps=2, max_tool_calls=3))
    first = await harness.begin()
    assert first["status"] == "waiting_external" and first["executionVersion"] == 2
    assert first["modelSteps"] == 1 and first["toolCalls"] == 2 and harness.model.calls == 1
    assert first["activeWaitId"] and first["inFlightSeconds"] == 0
    assert len(store.snapshot()["artifacts"]) == 1
    unchanged = store.snapshot()
    for _ in range(3):
        video_clock.advance(30)
        waiting = await harness.coordinator.run_once()
        assert waiting["activeSeconds"] == first["activeSeconds"]
        assert waiting["modelCalls"] == first["modelCalls"]
        assert waiting["modelSteps"] == 1 and waiting["toolCalls"] == 2
        assert store.snapshot() == unchanged
    with pytest.raises(AppError) as busy:
        await harness.runner().run("other", "another request")
    assert busy.value.code == "RUN_BUSY"
    assert await harness.runner().run("coffee", "等待模拟任务", request_id="waiting") == waiting
    await harness.complete_jobs()
    final = await harness.coordinator.run_once()
    assert final["status"] == "completed", final
    assert final["modelSteps"] == 2 and final["toolCalls"] == 3 and harness.model.calls == 2
    assert final["policy"] == first["policy"] and final["activeWaitId"] is None
    assert final["externalWaitSeconds"] == 90 and final["activeSeconds"] < 10
    assert final["externalWaitStartedAt"] is None
    assert list(outcomes(final)) == ["save-0", "await-0", "save-1"]
    assert outcomes(final)["await-0"]["data"]["status"] == "succeeded"
    assert all(len(artifact["versions"]) == 1 for artifact in store.snapshot()["artifacts"].values())
    assert len(store.snapshot()["artifacts"]) == 2
    assert store.snapshot()["waits"][first["activeWaitId"]]["status"] == "delivered"
    before = store.snapshot()
    assert await harness.coordinator.run_once() is None
    assert await harness.runner().resume(final["id"]) == final
    assert store.snapshot() == before and harness.model.calls == 2
    assert harness.adapter.ledger_snapshot()["submitCalls"] == 1


async def test_two_interrupts_in_one_batch_keep_positions_and_individual_results(store, video_clock):
    harness = Harness(store, video_clock)
    first_job = harness.job("owner-0")
    video_clock.advance(0.1)
    second_job = harness.job("owner-1")
    first = await harness.begin([first_job, second_job])
    assert (await harness.worker.run_once()).id == first_job
    second = await harness.coordinator.run_once()
    assert second["status"] == "waiting_external" and second["activeWaitId"] != first["activeWaitId"]
    assert second["modelSteps"] == 1 and second["toolCalls"] == 4 and harness.model.calls == 1
    assert len(store.snapshot()["artifacts"]) == 2
    assert store.snapshot()["waits"][first["activeWaitId"]]["status"] == "claimed"
    assert (await harness.worker.run_once()).id == second_job
    final = await harness.coordinator.run_once()
    assert final["status"] == "completed", final
    results = outcomes(final)
    assert list(results) == ["save-0", "await-0", "save-1", "await-1", "save-2"]
    assert results["await-0"]["data"]["jobId"] == first_job
    assert results["await-1"]["data"]["jobId"] == second_job
    assert final["toolCalls"] == 5 and harness.model.calls == 2
    assert all(wait["status"] == "delivered" for wait in store.snapshot()["waits"].values())
    assert harness.adapter.ledger_snapshot()["submitCalls"] == 2


async def test_completed_before_registration_has_no_wait_or_extra_model(store, video_clock):
    harness = Harness(store, video_clock)
    job_id = harness.job()
    await harness.complete_jobs()
    final = await harness.begin([job_id])
    assert final["status"] == "completed" and not store.snapshot()["waits"]
    assert harness.model.calls == 2 and final["externalWaitSeconds"] == 0


@pytest.mark.parametrize(
    "scenario,code",
    [
        (MockScenario(states=["failed"]), "JOB_FAILED"),
        (MockScenario(submission="rejected"), "JOB_FAILED"),
        (MockScenario(submission="response_lost"), "JOB_SUBMISSION_UNKNOWN"),
        (MockScenario(query_error_calls=[1, 2, 3, 4]), "JOB_QUERY_PAUSED"),
        (None, "JOB_WAIT_TIMEOUT"),
    ],
)
async def test_external_errors_and_timeout_are_tool_results_not_new_submissions(
    store, video_clock, scenario, code
):
    harness = Harness(store, video_clock, scenario=scenario)
    first = await harness.begin()
    if scenario:
        await harness.complete_jobs()
    else:
        video_clock.advance(601)
    final = await harness.coordinator.run_once()
    assert final["status"] == "completed", final
    assert outcomes(final)["await-0"]["error"]["code"] == code
    assert final["modelSteps"] == 2 and harness.model.calls == 2 and final["toolCalls"] == 3
    assert final["policy"] == first["policy"]
    assert harness.adapter.ledger_snapshot()["submitCalls"] == (1 if scenario else 0)
    if code == "JOB_WAIT_TIMEOUT":
        assert final["externalWaitSeconds"] == 601 and final["activeSeconds"] < 10
        await harness.complete_jobs()
        assert next(iter(store.snapshot()["jobs"].values()))["status"] == "succeeded"
        assert await harness.coordinator.run_once() is None and harness.model.calls == 2


async def test_stop_closes_visible_calls_preserves_job_and_rejects_old_run_after_new_request(
    store, video_clock
):
    harness = Harness(store, video_clock)
    first = await harness.begin()
    video_clock.advance(40)
    assert harness.coordinator.stop_run(first["id"])
    stopped = store.snapshot()["runs"][first["id"]]
    assert stopped["status"] == "cancelled" and stopped["externalWaitSeconds"] == 40
    assert outcomes(stopped)["await-0"]["error"]["code"] == "CANCELLED"
    binding = harness.coordinator.waits.get(first["activeWaitId"])
    assert binding.status == "stopped" and not binding.auto_resume
    assert binding.context.operation_key not in store.snapshot()["operations"]
    await harness.complete_jobs()
    assert await harness.coordinator.run_once() is None and harness.model.calls == 1
    model = ScriptedModel(lambda *_: AIMessage(content="new conversation"))
    newer = await harness.runner(model).run("coffee", "new request")
    with pytest.raises(AppError) as error:
        await harness.runner().resume(first["id"])
    assert error.value.code == "STALE_RUN"
    assert store.snapshot()["sessions"]["coffee"]["messages"] == newer["messages"]


async def test_explicit_resume_rearms_stopped_wait_without_resetting_tool_budget(store, video_clock):
    harness = Harness(store, video_clock)
    first = await harness.begin()
    original = harness.coordinator.waits.get(first["activeWaitId"])
    video_clock.advance(20)
    harness.coordinator.stop_run(first["id"])
    video_clock.advance(100)
    again = await harness.runner().resume(first["id"])
    assert again["status"] == "waiting_external" and again["toolCalls"] == first["toolCalls"]
    binding = harness.coordinator.waits.get(first["activeWaitId"])
    assert binding.generation == original.generation + 1 and binding.auto_resume
    assert harness.model.calls == 1
    video_clock.advance(30)
    await harness.complete_jobs()
    final = await harness.coordinator.run_once()
    assert final["status"] == "completed", final
    assert final["externalWaitSeconds"] == 50 and final["modelSteps"] == 2 and final["toolCalls"] == 3
    with pytest.raises(AppError) as stale:
        harness.coordinator.waits.get(binding.id).confirmed_result(original.resume_token())
    assert stale.value.code == "STALE_WAIT"


async def test_result_delivery_then_model_failure_needs_explicit_resume(store, video_clock):
    def failure(_):
        raise RuntimeError("private failed response")

    harness = Harness(store, video_clock)
    first = await harness.begin(after=failure)
    await harness.complete_jobs()
    failed = await harness.coordinator.run_once()
    assert failed["status"] == "failed" and failed["resumable"] and failed["modelSteps"] == 2
    assert harness.coordinator.waits.get(first["activeWaitId"]).status == "delivered"
    before = store.snapshot()["operations"]
    assert await harness.coordinator.run_once() is None and harness.model.calls == 2
    model = ScriptedModel(lambda *_: AIMessage(content="explicit continuation"))
    final = await harness.runner(model).resume(first["id"])
    assert final["status"] == "completed" and final["modelSteps"] == 3 and model.calls == 1
    assert store.snapshot()["operations"] == before and final["toolCalls"] == 3


@pytest.mark.parametrize("problem", ["missing_key", "mode", "checkpoint", "steps", "time"])
async def test_auto_resume_keeps_result_when_configuration_or_budget_is_unavailable(
    store, video_clock, problem
):
    harness = Harness(store, video_clock, policy=RunPolicy(max_steps=1 if problem == "steps" else 8))
    first = await harness.begin()
    await harness.complete_jobs()
    expected = {
        "missing_key": "KEY_MISSING",
        "mode": "RESUME_CONFIG_CHANGED",
        "checkpoint": "NO_CHECKPOINT",
        "steps": "STEP_LIMIT",
        "time": "TIMEOUT",
    }[problem]
    if problem == "missing_key":

        def missing(_):
            raise AppError("KEY_MISSING", "原模型配置不可用。")

        harness.coordinator.runner_factory = missing
    elif problem == "mode":
        harness.coordinator.runner_factory = lambda _: AgentRunner(
            store=store, model=harness.model, tools=create_project_tools()
        )
    elif problem == "checkpoint":
        (store.home / "checkpoints.sqlite").unlink()
    elif problem == "time":
        store.transaction(lambda draft: draft["runs"][first["id"]].update(activeSeconds=180.0))
    result = await harness.coordinator.run_once()
    assert result["status"] == "waiting_external" and result["waitResumeError"]["code"] == expected
    binding = harness.coordinator.waits.get(first["activeWaitId"])
    assert binding.status == "ready" and binding.result.ok
    assert binding.context.operation_key not in store.snapshot()["operations"]
    assert result["modelSteps"] == 1 and harness.model.calls == 1
    assert harness.adapter.ledger_snapshot()["submitCalls"] == 1


async def test_read_only_run_can_wait_and_finish_without_creating_or_mutating_job(store, video_clock):
    harness = Harness(store, video_clock, read_only=True)
    first = await harness.begin(writes=False)
    jobs = store.snapshot()["jobs"]
    assert first["status"] == "waiting_external" and first["readOnly"] and first["toolCalls"] == 1
    assert len(jobs) == 1 and not store.snapshot()["artifacts"]
    await harness.complete_jobs()
    final = await harness.coordinator.run_once()
    assert final["status"] == "completed" and final["toolCalls"] == 1
    assert len(store.snapshot()["jobs"]) == 1 and not store.snapshot()["artifacts"]
    assert outcomes(final)["await-0"]["data"]["request"] == next(iter(jobs.values()))["request"]


async def test_competing_coordinators_and_manual_resume_share_one_graph_owner(store, video_clock):
    started, release = asyncio.Event(), asyncio.Event()

    async def finish(_):
        started.set()
        await release.wait()
        return AIMessage(content="done")

    harness = Harness(store, video_clock)
    first = await harness.begin(after=finish)
    await harness.complete_jobs()
    second = WaitCoordinator(
        store, lambda _: harness.runner(), resolvers=harness.coordinator.resolvers, clock=video_clock
    )
    task = asyncio.create_task(harness.coordinator.run_once())
    await asyncio.wait_for(started.wait(), 3)
    try:
        assert await second.run_once() is None
        with pytest.raises(AppError) as error:
            await harness.runner().resume(first["id"])
        assert error.value.code == "RUN_BUSY"
    finally:
        release.set()
    final = await task
    assert final["status"] == "completed" and harness.model.calls == 2


async def test_user_stop_wins_against_an_active_continuation(store, video_clock):
    started = asyncio.Event()

    async def finish(_):
        started.set()
        await asyncio.Event().wait()

    harness = Harness(store, video_clock)
    first = await harness.begin(after=finish)
    await harness.complete_jobs()
    task = asyncio.create_task(harness.coordinator.run_once())
    await asyncio.wait_for(started.wait(), 3)
    assert harness.coordinator.stop_run(first["id"])
    final = await asyncio.wait_for(task, 3)
    assert final["status"] == "cancelled" and final["modelSteps"] == 2
    assert final["modelCalls"][-1]["status"] == "cancelled"
    assert await harness.coordinator.run_once() is None and harness.model.calls == 2
    assert outcomes(final)["await-0"]["ok"]


@pytest.mark.parametrize("phase", ["wait.ready", "wait.claimed", "wait.result_committed"])
async def test_stop_during_delivery_reuses_stable_result_on_explicit_resume(store, video_clock, phase):
    harness = Harness(store, video_clock)
    first = await harness.begin()
    stopped = []

    def stop_once(event):
        if event["type"] == phase and not stopped:
            stopped.append(event)
            harness.coordinator.stop_run(first["id"])

    harness.on_event = stop_once
    harness.coordinator.waits.on_event = stop_once
    await harness.complete_jobs()
    await harness.coordinator.run_once()
    current = store.snapshot()["runs"][first["id"]]
    assert current["status"] == "cancelled" and harness.model.calls == 1
    binding = harness.coordinator.waits.get(first["activeWaitId"])
    assert binding.result.ok and not binding.auto_resume
    result_before = binding.result
    final = await harness.runner().resume(first["id"])
    assert final["status"] == "completed", final
    assert harness.coordinator.waits.get(binding.id).result == result_before
    assert final["toolCalls"] == 3 and final["modelSteps"] == 2 and harness.model.calls == 2
    assert len(store.snapshot()["artifacts"]) == 2
    assert outcomes(final)["await-0"]["data"]["jobId"] == binding.resource.id


async def test_rewaiting_after_timeout_is_a_new_call_with_cumulative_budget(store, video_clock):
    harness = Harness(store, video_clock, policy=RunPolicy(max_steps=3, max_tool_calls=2))
    job_id = harness.job()

    def respond(messages, step):
        if step < 2:
            if step == 1:
                assert json.loads(messages[-1].content)["error"]["code"] == "JOB_WAIT_TIMEOUT"
            return tool_call("await_job", {"jobId": job_id}, call_id="same-id")
        assert json.loads(messages[-1].content)["data"]["jobId"] == job_id
        return AIMessage(content="done")

    harness.model = ScriptedModel(respond)
    first = await harness.runner().run("coffee", "wait twice")
    video_clock.advance(601)
    second = await harness.coordinator.run_once()
    assert second["status"] == "waiting_external" and second["modelSteps"] == 2 and second["toolCalls"] == 2
    assert second["activeWaitId"] != first["activeWaitId"]
    video_clock.advance(10)
    await harness.complete_jobs()
    final = await harness.coordinator.run_once()
    assert final["status"] == "completed" and final["externalWaitSeconds"] == 611
    assert final["modelSteps"] == 3 and final["toolCalls"] == 2
    messages = messages_from_dict(final["messages"])
    assert_complete_protocol(messages)
    results = [json.loads(message.content) for message in messages if isinstance(message, ToolMessage)]
    assert len(results) == 2 and not results[0]["ok"] and results[1]["ok"]


async def test_resource_finishes_after_checkpoint_but_before_arm_without_losing_wakeup(
    store, video_clock, monkeypatch
):
    from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

    harness = Harness(store, video_clock)
    job_id = harness.job()
    original = AsyncSqliteSaver.aput_writes
    completed = []

    async def commit_and_complete(self, config, writes, task_id, task_path=""):
        await original(self, config, writes, task_id, task_path)
        if any(channel == "__interrupt__" for channel, _ in writes) and not completed:
            completed.append(await harness.worker.run_once())

    monkeypatch.setattr(AsyncSqliteSaver, "aput_writes", commit_and_complete)
    first = await harness.begin([job_id])
    assert completed and completed[0].status == "succeeded"
    binding = harness.coordinator.waits.get(first["activeWaitId"])
    assert binding.status == "ready" and binding.result.ok
    final = await harness.coordinator.run_once()
    assert final["status"] == "completed" and harness.model.calls == 2
    assert harness.adapter.ledger_snapshot()["submitCalls"] == 1


async def test_generic_external_resource_uses_the_same_production_wait_path(store, video_clock):
    ready = {}

    def defer(args, current_store, context):
        for raw in current_store.snapshot()["waits"].values():
            binding = WaitBinding.model_validate(raw)
            if binding.context == context:
                return binding.deferred()
        binding = WaitBinding(
            id=str(uuid4()),
            context=context,
            resource=ExternalResourceRef(kind="report", id="report-1"),
            operation_fingerprint=current_store.operation_fingerprint("report", args),
            started_at=video_clock().isoformat(),
            deadline_at=(video_clock() + timedelta(minutes=10)).isoformat(),
        )
        current_store.transaction(
            lambda draft: draft["waits"].__setitem__(
                binding.id, binding.model_dump(mode="json", by_alias=True)
            )
        )
        return binding.deferred()

    registry = ToolRegistry().register(
        ToolDefinition("report", "wait for report", Arguments, "read", context_execute=defer)
    )
    registry.register_wait_resolver("report", lambda binding: ready.get(binding.resource.id))
    model = ScriptedModel(
        lambda messages, step: tool_call("report") if step == 0 else AIMessage(content="report delivered")
    )
    runner = AgentRunner(store=store, model=model, tools=registry, clock=video_clock)
    first = await runner.run("coffee", "report")
    assert (
        first["executionVersion"] == 2
        and first["videoMode"] == "off"
        and first["status"] == "waiting_external"
    )
    ready["report-1"] = {"ok": True, "data": {"report": "persisted external result"}}
    coordinator = WaitCoordinator(
        store, lambda _: runner, resolvers=registry.wait_resolvers, clock=video_clock
    )
    final = await coordinator.run_once()
    assert final["status"] == "completed" and not store.snapshot()["jobs"]
    assert outcomes(final)["call-1"]["data"]["report"] == "persisted external result"
    assert model.calls == 2 and final["toolCalls"] == 1


async def test_unregistered_deferred_marker_cannot_fabricate_a_wait_or_result(store):
    registry = ToolRegistry().register(
        ToolDefinition(
            "report",
            "report",
            Arguments,
            "read",
            context_execute=lambda *_: DeferredToolResult(
                wait_id="missing", generation=1, resource=ExternalResourceRef(kind="report", id="report")
            ),
        )
    )
    registry.register_wait_resolver("report", lambda _: {"ok": True, "data": "must not use"})
    model = ScriptedModel(lambda *_: tool_call("report"))
    result = await AgentRunner(store=store, model=model, tools=registry).run("coffee", "report")
    assert result["errorCode"] == "WAIT_NOT_FOUND" and model.calls == 1
    assert not store.snapshot()["waits"] and not store.snapshot()["operations"]
    assert not outcomes(result)["call-1"]["ok"]


async def test_offline_interval_is_external_time_and_does_not_refresh_budget(store, video_clock):
    harness = Harness(store, video_clock, policy=RunPolicy(max_steps=2, max_tool_calls=3, timeout_seconds=20))
    first = await harness.begin()
    original_job = next(iter(store.snapshot()["jobs"].values()))
    home = store.home
    store.close()
    video_clock.advance(400)
    with FileStore.open(home) as reopened:
        restored = Harness(reopened, video_clock, policy=RunPolicy(max_steps=100, timeout_seconds=900))
        restored.model = ScriptedModel(lambda *_: AIMessage(content="original wait resumed"))
        await restored.complete_jobs()
        final = await restored.coordinator.run_once()
        assert final["status"] == "completed" and final["externalWaitSeconds"] == 400
        assert final["policy"] == first["policy"] and final["activeSeconds"] < 20
        assert final["modelSteps"] == 2 and final["toolCalls"] == 3
        assert final["activeSeconds"] >= first["activeSeconds"]
        assert list(reopened.snapshot()["jobs"]) == [original_job["id"]]
        assert restored.model.calls == 1


async def wait_for_status(service, run_id, status):
    async with asyncio.timeout(4):
        while service.run_record(run_id)["status"] != status:
            await asyncio.sleep(0.01)
    return service.run_record(run_id)


async def test_application_exit_preserves_wait_and_startup_resumes_original_run(tmp_path):
    config = Config(home=tmp_path, api_key=None, video_mode="mock")
    job_id = None
    original_model = ScriptedModel(lambda *_: tool_call("await_job", {"jobId": job_id}, call_id="original"))
    async with ApplicationService.open(config, model=original_model) as service:
        await service.job_worker.stop()  # B3 controls the provider completion boundary explicitly.
        adapter = MockVideoAdapter(service.store, scenario=MockScenario(states=["succeeded"]))
        jobs = JobService(service.store, [adapter])
        job_id = jobs.generate(video_request(adapter), context=video_run(service.store))["data"]["jobId"]
        first = await service.start("coffee", "等待", "application-wait", read_only=True)
        await service.task
        first = service.run_record(first["id"])
        assert first["status"] == "waiting_external" and original_model.calls == 1
        with pytest.raises(AppError) as busy:
            await service.start("other", "new request", "other-request")
        assert busy.value.code == "RUN_BUSY"
    state = json.loads((tmp_path / "state.json").read_text(encoding="utf-8"))
    assert state["runs"][first["id"]]["status"] == "waiting_external"
    assert state["waits"][first["activeWaitId"]]["autoResume"]
    assert not (tmp_path / "instance.lock").exists()
    model = ScriptedModel(lambda *_: AIMessage(content="完成模拟等待"))
    async with ApplicationService.open(config, model=model) as service:
        await service.job_worker.stop()
        provider = MockVideoAdapter(service.store, scenario=MockScenario(states=["succeeded"]))
        await JobWorker(JobService(service.store, [provider])).run_once()
        service.wait_coordinator.notify()
        final = await wait_for_status(service, first["id"], "completed")
        assert final["modelSteps"] == 2 and final["toolCalls"] == 1 and model.calls == 1
        assert final["readOnly"] and final["policy"] == first["policy"]
        assert outcomes(final)["original"]["data"]["jobId"] == job_id
        assert len(service.store.snapshot()["jobs"]) == 1
        assert service.session("coffee")["run"]["activeWaitId"] is None
        assert any(event["type"] == "run.resumed" for event in final["events"])


async def test_application_stop_wait_persists_before_returning(tmp_path):
    config = Config(home=tmp_path, api_key=None, video_mode="mock")
    job_id = None
    model = ScriptedModel(lambda *_: tool_call("await_job", {"jobId": job_id}))
    async with ApplicationService.open(config, model=model) as service:
        job_id = service.video_jobs.generate(
            video_request(MockVideoAdapter(service.store)), context=video_run(service.store)
        )["data"]["jobId"]
        first = await service.start("coffee", "等待", "stop-me")
        await service.task
        assert service.stop(first["id"])["stopRequested"]
        record = service.run_record(first["id"])
        assert (
            record["status"] == "cancelled"
            and not service.store.snapshot()["waits"][record["activeWaitId"]]["autoResume"]
        )
        assert not service.stop(first["id"])["stopRequested"] and model.calls == 1


async def test_application_configuration_validation_defers_automatic_continuation(tmp_path):
    config = Config(home=tmp_path, api_key=None, video_mode="mock")
    job_id = None
    model = ScriptedModel(
        lambda messages, step: (
            tool_call("await_job", {"jobId": job_id}) if step == 0 else AIMessage(content="done")
        )
    )
    async with ApplicationService.open(config, model=model) as service:
        await service.job_worker.stop()
        adapter = MockVideoAdapter(service.store, scenario=MockScenario(states=["succeeded"]))
        jobs = JobService(service.store, [adapter])
        job_id = jobs.generate(video_request(adapter), context=video_run(service.store))["data"]["jobId"]
        first = await service.start("coffee", "wait while validating configuration", "config-wait")
        await service.task
        service.config_busy = True
        await JobWorker(jobs).run_once()
        await service.wait_coordinator.run_once()
        current = service.run_record(first["id"])
        assert current["status"] == "waiting_external" and current["waitResumeError"]["code"] == "CONFIG_BUSY"
        assert model.calls == 1
        service.config_busy = False
        service.wait_coordinator.notify()
        final = await wait_for_status(service, first["id"], "completed")
        assert final["waitResumeError"] is None and model.calls == 2


async def test_new_run_during_stop_cleanup_does_not_reserve_an_orphan(store, video_clock):
    started, unwinding = asyncio.Event(), asyncio.Event()

    async def finish(_):
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            await unwinding.wait()

    harness = Harness(store, video_clock)
    first = await harness.begin(after=finish)
    await harness.complete_jobs()
    task = asyncio.create_task(harness.coordinator.run_once())
    await asyncio.wait_for(started.wait(), 3)
    harness.coordinator.stop_run(first["id"])
    before = store.snapshot()["runs"]
    try:
        with pytest.raises(AppError) as busy:
            await harness.runner().run("coffee", "new request during cancellation")
        assert busy.value.code == "RUN_BUSY"
        assert store.snapshot()["runs"] == before
    finally:
        unwinding.set()
        await task


async def test_coordinator_shutdown_after_claim_preserves_automatic_intent(store, video_clock, monkeypatch):
    harness = Harness(store, video_clock)
    first = await harness.begin()
    await harness.complete_jobs()
    claimed = asyncio.Event()
    original = WaitService.claim

    def claim_and_cancel(self, binding):
        result = original(self, binding)
        claimed.set()
        harness.coordinator.shutdown.set()
        asyncio.current_task().cancel()
        return result

    with monkeypatch.context() as patch:
        patch.setattr(WaitService, "claim", claim_and_cancel)
        task = asyncio.create_task(harness.coordinator.run_once())
        await asyncio.wait_for(claimed.wait(), 3)
        pending = await task
    assert pending["status"] == "waiting_external" and harness.model.calls == 1
    assert harness.coordinator.waits.get(first["activeWaitId"]).auto_resume
    harness.coordinator.shutdown.clear()
    final = await harness.coordinator.run_once()
    assert final["status"] == "completed" and harness.model.calls == 2


async def test_application_exit_during_preparation_reconstructs_interrupt(tmp_path, monkeypatch):
    from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

    config = Config(home=tmp_path, api_key=None, video_mode="mock")
    paused = asyncio.Event()
    original = AsyncSqliteSaver.aput_writes
    job_id = None
    model = ScriptedModel(lambda *_: tool_call("await_job", {"jobId": job_id}))

    async def suspend_interrupt_write(self, config, writes, task_id, task_path=""):
        if any(channel == "__interrupt__" for channel, _ in writes):
            paused.set()
            await asyncio.Event().wait()
        return await original(self, config, writes, task_id, task_path)

    with monkeypatch.context() as patch:
        patch.setattr(AsyncSqliteSaver, "aput_writes", suspend_interrupt_write)
        async with ApplicationService.open(config, model=model) as service:
            await service.job_worker.stop()
            adapter = MockVideoAdapter(service.store, scenario=MockScenario(states=["succeeded"]))
            jobs = JobService(service.store, [adapter])
            job_id = jobs.generate(video_request(adapter), context=video_run(service.store))["data"]["jobId"]
            first = await service.start("coffee", "exit while preparing", "exit-preparing")
            await asyncio.wait_for(paused.wait(), 3)
    saved = json.loads((tmp_path / "state.json").read_text(encoding="utf-8"))
    assert saved["runs"][first["id"]]["status"] == "waiting_external"
    assert model.calls == 1 and next(iter(saved["waits"].values()))["autoResume"]
    model = ScriptedModel(lambda *_: AIMessage(content="resumed after normal shutdown"))
    async with ApplicationService.open(config, model=model) as service:
        await service.job_worker.stop()
        adapter = MockVideoAdapter(service.store, scenario=MockScenario(states=["succeeded"]))
        await JobWorker(JobService(service.store, [adapter])).run_once()
        service.wait_coordinator.notify()
        final = await wait_for_status(service, first["id"], "completed")
        assert final["modelSteps"] == 2 and final["toolCalls"] == 1 and model.calls == 1
