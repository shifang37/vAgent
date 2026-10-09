import asyncio
import json
from dataclasses import replace

import httpx
import pytest
from conftest import ScriptedModel
from job_support import JobModel, eventually, finish_job, register_job
from langchain_core.messages import AIMessage, ToolMessage, messages_from_dict

from vagent.application import ApplicationService
from vagent.config import Config
from vagent.errors import AppError
from vagent.video.providers.mock import MockScenario, MockVideoAdapter
from vagent.wait_runtime import execution_control
from vagent.web import create_app


async def test_worker_events_keep_job_owner_after_creator_completes_and_another_run_starts(
    tmp_path, job_runtime
):
    model = JobModel(wait=False)
    async with ApplicationService.open(
        Config(home=tmp_path, api_key=None, video_mode="mock"), model=model
    ) as service:
        model.configure(job_runtime)
        owner_queue, other_queue = service.subscribe("coffee"), service.subscribe("other")
        first = await service.start("coffee", "登记任务", "create")
        creator = await service.wait_for_run(first["id"])
        assert creator["status"] == "completed" and model.calls == 2
        original_events = creator["events"]
        entered = asyncio.Event()

        async def blocked(*_):
            entered.set()
            await asyncio.Event().wait()

        service.model = ScriptedModel(blocked)
        other = await service.start("other", "独立需求", "other-run")
        await asyncio.wait_for(entered.wait(), 3)
        job = await finish_job(service, job_runtime.clock, model.job_id)
        assert job.status == "succeeded"
        assert job.submit_attempts == 1 and job.query_attempts == 2
        assert service.active_run_id == other["id"]
        assert service.run_record(first["id"])["events"] == original_events
        events = [owner_queue.get_nowait() for _ in range(owner_queue.qsize())]
        updates = [event for event in events if event["type"] == "job.updated"]
        assert updates and updates[-1]["job"]["status"] == "succeeded"
        assert {event["runId"] for event in updates} == {creator["id"]}
        revisions = [event["job"]["revision"] for event in updates]
        assert revisions == sorted(set(revisions))
        assert not any(other_queue.get_nowait()["type"] == "job.updated" for _ in range(other_queue.qsize()))
        assert service.session("other")["jobs"] == []
        assert service.session("coffee")["jobs"][0] == service.job(job.id)
        service.stop(other["id"])
        await service.task


@pytest.mark.parametrize("mode", ["off", "mock"])
async def test_reopen_automatically_queries_original_job_in_saved_mode_without_key(
    tmp_path, job_runtime, mode
):
    config = Config(home=tmp_path, api_key=None, video_mode="mock")
    async with ApplicationService.open(config) as service:
        job_id = register_job(service)
        await eventually(lambda: service.job(job_id)["status"] == "queued")
        before = service.job(job_id)
        worker, coordinator = service.job_worker._task, service.wait_coordinator._task
    assert worker.done() and coordinator.done() and not (tmp_path / "instance.lock").exists()
    async with ApplicationService.open(replace(config, video_mode=mode)) as service:
        result = await finish_job(service, job_runtime.clock, job_id)
        assert result.status == "succeeded" and result.submit_attempts == 1
        assert result.provider_task_id == before["providerTaskId"] and result.query_attempts == 2
        assert service.capabilities()["videoGeneration"] == (mode == "mock")
        assert service.capabilities()["jobWorkerRunning"]
        assert not service.store.snapshot()["waits"]
        assert all(run["modelSteps"] == 0 for run in service.store.snapshot()["runs"].values())
        assert job_runtime.adapters[-1].ledger_snapshot()["submitCalls"] == 1


async def test_shared_service_waits_and_delivers_original_tool_result_once(tmp_path, job_runtime):
    model, events = JobModel(), []
    async with ApplicationService.open(
        Config(home=tmp_path, api_key=None, video_mode="mock"), model=model, on_event=events.append
    ) as service:
        model.configure(job_runtime)
        first = await service.start("coffee", "等待模拟任务", "wait")
        await service.task
        waiting = service.run_record(first["id"])
        assert waiting["status"] == "waiting_external" and model.calls == 2
        assert not execution_control(service.store).lock.locked()
        assert service.session("coffee")["wait"]["resource"] == {"kind": "job", "id": model.job_id}
        waiter = asyncio.create_task(service.wait_for_run(first["id"]))
        await asyncio.sleep(0)
        assert not waiter.done()
        assert service.run_record(first["id"])["modelCalls"] == waiting["modelCalls"]
        await finish_job(service, job_runtime.clock, model.job_id)
        result = await asyncio.wait_for(waiter, 5)
        assert result["status"] == "completed" and result["modelSteps"] == 3
        assert result["toolCalls"] == 2 and result["policy"] == waiting["policy"]
        replies = [m for m in messages_from_dict(result["messages"]) if isinstance(m, ToolMessage)]
        assert [m.tool_call_id for m in replies] == ["generate", "original-wait"]
        assert json.loads(replies[-1].content)["data"]["result"]["mediaAvailable"] is False
        assert {event["type"] for event in events} >= {
            "run.waiting",
            "run.resumed",
            "run.completed",
            "job.updated",
        }
        assert not service.subscribers
        assert await service.wait_for_run(result["id"]) == result


async def test_shutdown_preserves_wait_then_missing_key_keeps_job_result_for_explicit_resume(
    tmp_path, job_runtime
):
    config = Config(home=tmp_path, api_key=None, video_mode="mock")
    model = JobModel()
    async with ApplicationService.open(config, model=model) as service:
        model.configure(job_runtime)
        first = await service.start("coffee", "等待并重启", "restart")
        await service.task
        waiting = service.run_record(first["id"])
    saved = json.loads((tmp_path / "state.json").read_text(encoding="utf-8"))
    assert saved["waits"][waiting["activeWaitId"]]["autoResume"]
    assert saved["runs"][first["id"]]["status"] == "waiting_external"
    events = []
    async with ApplicationService.open(config, on_event=events.append) as service:
        await finish_job(service, job_runtime.clock, model.job_id)
        await eventually(lambda: service.run_record(first["id"]).get("waitResumeError"))
        blocked = await service.wait_for_run(first["id"])
        assert blocked["waitResumeError"]["code"] == "MISSING_KEY"
        await eventually(
            lambda: service.store.snapshot()["waits"][waiting["activeWaitId"]]["status"] == "ready"
        )
        assert blocked["modelCalls"] == waiting["modelCalls"]
        assert any(event["type"] == "run.resume_blocked" for event in events)
        service.model = model
        await service.resume(first["id"])
        result = await asyncio.wait_for(service.wait_for_run(first["id"]), 5)
        assert result["status"] == "completed" and result["waitResumeError"] is None
        assert result["policy"] == waiting["policy"] and model.calls == 3
        assert service.job(model.job_id)["submitAttempts"] == 1


async def test_stop_waiting_agent_does_not_cancel_job_or_wake_old_run(tmp_path, job_runtime):
    model = JobModel()
    async with ApplicationService.open(
        Config(home=tmp_path, api_key=None, video_mode="mock"), model=model
    ) as service:
        model.configure(job_runtime)
        first = await service.start("coffee", "停止等待", "stop")
        await service.task
        assert service.stop(first["id"])["stopRequested"]
        stopped = service.run_record(first["id"])
        assert stopped["status"] == "cancelled"
        assert not service.store.snapshot()["waits"][stopped["activeWaitId"]]["autoResume"]
        service.model = ScriptedModel(lambda *_: AIMessage(content="新的会话回复"))
        new = await service.start("coffee", "新的需求", "new")
        await service.wait_for_run(new["id"])
        await finish_job(service, job_runtime.clock, model.job_id)
        assert service.session("coffee")["run"]["id"] == new["id"]
        assert service.run_record(first["id"])["status"] == "cancelled" and model.calls == 2
        assert service.model.calls == 1 and service.job(model.job_id)["status"] == "succeeded"
        with pytest.raises(AppError, match="更新的请求"):
            await service.resume(first["id"])


async def test_job_api_reads_without_key_and_retry_query_keeps_upstream_id_and_boundary(
    tmp_path, job_runtime
):
    job_runtime.scenario = MockScenario(query_error_calls=[1, 2, 3, 4])
    app = create_app(Config(home=tmp_path, api_key=None))
    async with app.router.lifespan_context(app):
        service = app.state.service
        job_id = register_job(service)
        paused = await finish_job(service, job_runtime.clock, job_id)
        assert paused.query_state == "paused" and paused.query_attempts == 4
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:3210"
        ) as client:
            path = f"/api/jobs/{job_id}"
            view = (await client.get(path)).json()
            assert view["canRetryQuery"] and view["status"] == "queued" and view["simulated"]
            assert (await client.get("/api/jobs?sessionId=coffee")).json()["jobs"] == [view]
            assert (await client.get("/api/jobs?sessionId=other")).json()["jobs"] == []
            assert (await client.get("/api/jobs/missing")).status_code == 404
            assert (await client.get("/api/sessions/coffee")).json()["jobs"] == [view]
            assert (await client.get(path)).json() == view
            assert service.video_jobs.get(job_id) == paused
            retry = path + "/retry-query"
            assert (await client.post(retry, json={})).status_code == 403
            token = (await client.get("/api/session-token")).json()["token"]
            client.headers["X-CSRF-Token"] = token
            for headers in [
                {"Origin": "https://invalid.example"},
                {"Host": "invalid.example"},
                {"Sec-Fetch-Site": "cross-site"},
            ]:
                assert (await client.post(retry, json={}, headers=headers)).status_code == 403
            assert service.video_jobs.get(job_id) == paused
            response = await client.post(retry, json={})
            assert response.status_code == 200
            assert response.json()["providerTaskId"] == view["providerTaskId"]
            assert response.json()["submitAttempts"] == 1 and not response.json()["canRetryQuery"]
            assert (await client.post(retry, json={})).status_code == 409
            final = await finish_job(service, job_runtime.clock, job_id)
            assert final.status == "succeeded" and final.query_attempts == 6
            assert final.submit_attempts == 1 and final.provider_task_id == paused.provider_task_id


@pytest.mark.parametrize(
    "scenario,status",
    [
        (MockScenario(submission="response_lost"), "unknown"),
        (MockScenario(states=["failed"]), "failed"),
    ],
)
async def test_unconfirmed_or_failed_job_stays_visible_and_cannot_retry_submission(
    tmp_path, job_runtime, scenario, status
):
    job_runtime.scenario = scenario
    async with ApplicationService.open(Config(home=tmp_path, api_key=None)) as service:
        job_id = register_job(service)
        job = await finish_job(service, job_runtime.clock, job_id)
        view = service.job(job_id)
        assert view["status"] == status and not view["canRetryQuery"]
        assert view["error"]["stage"] in {"submit", "generate"} and not view["mediaAvailable"]
        with pytest.raises(AppError) as error:
            service.retry_query(job_id)
        assert error.value.code == "JOB_QUERY_NOT_PAUSED"
        assert service.video_jobs.get(job_id) == job and job.submit_attempts == 1


async def test_sse_routes_job_and_run_events_and_overflow_reconnect_restores_current_jobs(
    tmp_path, job_runtime
):
    config = Config(home=tmp_path, api_key=None)
    async with ApplicationService.open(config) as service:
        job_id = register_job(service)
        app = create_app(config)
        app.state.service = service

        class Request:
            async def is_disconnected(self):
                return False

        endpoint = next(route.endpoint for route in app.routes if route.path == "/api/events")
        iterator = (await endpoint(sessionId="coffee", request=Request())).body_iterator
        initial = await anext(iterator)
        assert initial.startswith("event: snapshot\n") and job_id in initial
        await finish_job(service, job_runtime.clock, job_id)
        update = await asyncio.wait_for(anext(iterator), 3)
        assert update.startswith("event: job.updated\n")
        assert json.loads(update.split("data: ")[1])["job"]["runId"] == "owner"
        for _ in range(70):
            service.notify("coffee", {"type": "job.updated", "job": {"jobId": job_id, "revision": 0}})
        recovered = await anext(iterator)
        assert recovered.startswith("event: snapshot\n")
        assert json.loads(recovered.split("data: ")[1])["jobs"][0] == service.job(job_id)
        queue = next(iter(service.subscribers))
        while not queue.empty():
            queue.get_nowait()
        for kind in ("run.waiting", "run.resumed", "run.resume_blocked", "run.completed"):
            service.notify("coffee", {"type": kind, "sessionId": "coffee", "runId": "owner"})
            frame = await anext(iterator)
            assert frame.startswith(f"event: {kind}\n")
            assert json.loads(frame.split("data: ")[1])["snapshot"]["jobs"][0]["status"] == "succeeded"
        service.notify("coffee", {"type": "future.event"})
        assert (await anext(iterator)).startswith("event: snapshot\n")
        await iterator.aclose()
        assert not service.subscribers
        iterator = (await endpoint(sessionId="coffee", request=Request())).body_iterator
        reconnect = json.loads((await anext(iterator)).split("data: ")[1])
        assert reconnect["jobs"][0] == service.job(job_id)
        await iterator.aclose()


async def test_application_shutdown_during_accepted_submit_keeps_unknown_and_releases_lock(
    tmp_path, monkeypatch
):
    entered = asyncio.Event()

    class SlowSubmit(MockVideoAdapter):
        async def submit(self, request, operation_key):
            result = await super().submit(request, operation_key)
            entered.set()
            await asyncio.Event().wait()
            return result

    monkeypatch.setattr("vagent.application.MockVideoAdapter", SlowSubmit)
    config = Config(home=tmp_path, api_key=None)
    async with ApplicationService.open(config) as service:
        job_id = register_job(service)
        await asyncio.wait_for(entered.wait(), 3)
        assert service.job(job_id)["status"] == "submitting"
    assert not (tmp_path / "instance.lock").exists()
    async with ApplicationService.open(config) as service:
        assert service.job(job_id)["status"] == "unknown"
        await asyncio.sleep(0.03)
        assert MockVideoAdapter(service.store).ledger_snapshot()["submitCalls"] == 1
