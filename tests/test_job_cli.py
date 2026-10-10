import asyncio
import json
import os
import subprocess
import sys
from collections import deque
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from conftest import ScriptedModel, tool_call, video_request, video_run
from job_support import JobModel, eventually, finish_job, register_job

from vagent import cli
from vagent.application import ApplicationService
from vagent.config import Config
from vagent.console import _windows_line
from vagent.storage import FileStore
from vagent.video.jobs import JobService
from vagent.video.providers.mock import MockScenario, MockVideoAdapter
from vagent.video.worker import JobWorker


def environment(home):
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith("VAGENT_") and key != "DEEPSEEK_API_KEY"
    }
    return {**env, "VAGENT_HOME": str(home), "PYTHONIOENCODING": "utf-8", "VAGENT_VIDEO_MODE": "mock"}


def seed_job(home):
    with FileStore.open(home) as store:
        adapter = MockVideoAdapter(store)
        service = JobService(store, [adapter])
        return service.generate(video_request(adapter), context=video_run(store))["data"]["jobId"]


def command(tmp_path, env, *args):
    return subprocess.run(
        [sys.executable, "-m", "vagent", *args],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=15,
    )


def test_cli_job_list_get_are_local_without_key_worker_mcp_or_model(tmp_path):
    home = tmp_path / "state"
    job_id = seed_job(home)
    state_path = home / "state.json"
    before = json.loads(state_path.read_text(encoding="utf-8"))
    env = environment(home)
    # If local inspection accidentally starts application composition, this fails.
    env["VAGENT_MCP_CONFIG"] = str(tmp_path / "missing-mcp.json")
    listing = command(tmp_path, env, "jobs", "list", "--session", "coffee")
    assert listing.returncode == 0, listing.stderr
    jobs = json.loads(listing.stdout)["jobs"]
    assert len(jobs) == 1 and jobs[0]["jobId"] == job_id and jobs[0]["submitAttempts"] == 0
    result = command(tmp_path, env, "jobs", "get", job_id)
    assert result.returncode == 0 and json.loads(result.stdout) == jobs[0]
    assert json.loads(command(tmp_path, env, "jobs", "list", "--session", "other").stdout)["jobs"] == []
    missing = command(tmp_path, env, "jobs", "get", "missing")
    assert missing.returncode == 1 and "JOB_NOT_FOUND" in missing.stderr
    denied = command(tmp_path, env, "jobs", "retry-query", job_id)
    assert denied.returncode == 1 and "JOB_QUERY_NOT_PAUSED" in denied.stderr
    assert json.loads(state_path.read_text(encoding="utf-8")) == before
    assert not (home / "mock-video.json").exists() and not (home / "instance.lock").exists()


def test_cli_jobs_obey_existing_instance_lock(tmp_path):
    home = tmp_path / "locked"
    with FileStore.open(home):
        result = command(tmp_path, environment(home), "jobs", "list")
        assert result.returncode == 1 and "STORE_LOCKED" in result.stderr


async def test_cli_retry_query_without_key_only_reopens_original_query_window(tmp_path, video_clock):
    home = tmp_path / "state"
    video_clock.value = datetime.now(UTC)
    with FileStore.open(home) as store:
        adapter = MockVideoAdapter(
            store, scenario=MockScenario(query_error_calls=[1, 2, 3, 4]), clock=video_clock
        )
        jobs = JobService(store, [adapter], clock=video_clock)
        job_id = jobs.generate(video_request(adapter), context=video_run(store))["data"]["jobId"]
        worker = JobWorker(jobs)
        for _ in range(5):
            job = await worker.run_once()
            if job.next_poll_at:
                video_clock.due(job)
        assert job.query_state == "paused"
        before = adapter.ledger_snapshot()
    result = command(tmp_path, environment(home), "jobs", "retry-query", job_id)
    assert result.returncode == 0, result.stderr
    updated = json.loads(result.stdout)
    assert updated["queryState"] == "polling" and updated["providerTaskId"] == job.provider_task_id
    assert updated["submitAttempts"] == 1 and updated["queryAttempts"] == 4
    with FileStore.open(home) as store:
        assert MockVideoAdapter(store).ledger_snapshot() == before
        assert jobs.store.home == store.home


async def test_jobs_work_sigint_preserves_existing_agent_wait_intent(tmp_path):
    home = tmp_path / "state"
    job_id = None
    model = ScriptedModel(lambda *_: tool_call("await_job", {"jobId": job_id}))
    async with ApplicationService.open(
        Config(home=home, api_key=None, video_mode="mock"), model=model
    ) as service:
        await service.job_worker.stop()
        job_id = register_job(service)
        first = await service.start("coffee", "持久等待", "work-wait")
        await service.task
        waiting = service.run_record(first["id"])
        assert waiting["status"] == "waiting_external"
    result = subprocess.run(
        [sys.executable, str(Path(__file__).with_name("job_cli_process.py")), "work", job_id],
        cwd=tmp_path,
        env=environment(home),
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=20,
    )
    assert result.returncode == 130, (result.stdout, result.stderr)
    with FileStore.open(home) as store:
        state = store.snapshot()
        assert state["waits"][waiting["activeWaitId"]]["autoResume"]
        assert state["runs"][first["id"]]["status"] == "waiting_external"
        assert state["runs"][first["id"]]["modelCalls"] == waiting["modelCalls"]
        assert state["jobs"][job_id]["status"] == "queued" and len(state["runs"]) == 2


async def test_cli_run_keeps_waiting_and_resume_uses_same_job_and_budget(
    tmp_path, monkeypatch, job_runtime, capsys
):
    config = Config(home=tmp_path, api_key=None, video_mode="mock")
    model = JobModel()
    monkeypatch.setattr(cli, "load_config", lambda: config)
    monkeypatch.setattr(
        "vagent.application.DeepSeekModel", lambda *_args, **_kwargs: model.configure(job_runtime)
    )
    task = asyncio.create_task(
        cli.run_agent(cli.parser().parse_args(["run", "等待模拟任务", "-s", "coffee"]))
    )
    await eventually(lambda: model.job_id)
    provider = job_runtime.adapters[-1]
    jobs = SimpleNamespace(video_jobs=JobService(provider.store, [], clock=job_runtime.clock))
    await eventually(
        lambda: any(run["status"] == "waiting_external" for run in provider.store.snapshot()["runs"].values())
    )
    assert not task.done() and model.calls == 2
    task.cancel()  # Same cancellation delivered by asyncio.Runner's Ctrl+C handler.
    assert await asyncio.wait_for(task, 5) == 130
    stopped = json.loads((tmp_path / "state.json").read_text(encoding="utf-8"))
    run = next(iter(stopped["runs"].values()))
    assert run["status"] == "cancelled" and not stopped["waits"][run["activeWaitId"]]["autoResume"]
    assert not (tmp_path / "instance.lock").exists()
    assert "CLI 退出后停止推进" in capsys.readouterr().out
    resumed = asyncio.create_task(cli.run_agent(cli.parser().parse_args(["resume", run["id"]])))
    await eventually(lambda: len(job_runtime.adapters) == 2)
    jobs.video_jobs = JobService(job_runtime.adapters[-1].store, [], clock=job_runtime.clock)
    await finish_job(jobs, job_runtime.clock, model.job_id)
    assert await asyncio.wait_for(resumed, 5) == 0
    state = json.loads((tmp_path / "state.json").read_text(encoding="utf-8"))
    final = state["runs"][run["id"]]
    assert final["status"] == "completed" and final["modelSteps"] == 3 and final["toolCalls"] == 2
    assert final["policy"] == run["policy"] and len(state["jobs"]) == 1
    assert state["jobs"][model.job_id]["submitAttempts"] == 1
    assert "模拟任务" in capsys.readouterr().out and not (tmp_path / "instance.lock").exists()


async def test_cli_run_waits_to_success_without_returning_at_graph_interrupt(
    tmp_path, monkeypatch, job_runtime
):
    config = Config(home=tmp_path, api_key=None, video_mode="mock")
    model = JobModel()
    monkeypatch.setattr(cli, "load_config", lambda: config)
    monkeypatch.setattr(
        "vagent.application.DeepSeekModel", lambda *_args, **_kwargs: model.configure(job_runtime)
    )
    task = asyncio.create_task(cli.run_agent(cli.parser().parse_args(["run", "等待", "-s", "coffee"])))
    await eventually(lambda: model.job_id)
    store = job_runtime.adapters[-1].store
    await eventually(
        lambda: any(run["status"] == "waiting_external" for run in store.snapshot()["runs"].values())
    )
    assert not task.done()
    await finish_job(
        SimpleNamespace(video_jobs=JobService(store, [], clock=job_runtime.clock)),
        job_runtime.clock,
        model.job_id,
    )
    assert await asyncio.wait_for(task, 5) == 0 and model.calls == 3
    assert not (tmp_path / "instance.lock").exists()


async def test_chat_input_yields_to_worker_then_exit_releases_background_tasks(
    tmp_path, monkeypatch, job_runtime
):
    config = Config(home=tmp_path, api_key=None, video_mode="mock")
    model = JobModel(wait=False)
    waiting_for_input, leave = asyncio.Event(), asyncio.Event()
    prompts = 0

    async def prompt(_):
        nonlocal prompts
        prompts += 1
        if prompts == 1:
            return "登记任务后回复"
        waiting_for_input.set()
        await leave.wait()
        return "/exit"

    monkeypatch.setattr(cli, "load_config", lambda: config)
    monkeypatch.setattr(cli, "read_prompt", prompt)
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(
        "vagent.application.DeepSeekModel", lambda *_args, **_kwargs: model.configure(job_runtime)
    )
    task = asyncio.create_task(cli.run_agent(cli.parser().parse_args(["chat", "-s", "coffee"])))
    await asyncio.wait_for(waiting_for_input.wait(), 5)
    assert model.calls == 2 and not task.done()
    store = job_runtime.adapters[-1].store
    job = await finish_job(
        SimpleNamespace(video_jobs=JobService(store, [], clock=job_runtime.clock)),
        job_runtime.clock,
        model.job_id,
    )
    assert job.status == "succeeded" and not task.done() and model.calls == 2
    leave.set()
    assert await asyncio.wait_for(task, 5) == 0
    assert not (tmp_path / "instance.lock").exists()


@pytest.mark.parametrize(
    "mode,queries_before_run",
    [
        pytest.param("work", 0, id="work"),
        pytest.param("run", 0, id="run"),
        pytest.param("run", 2, id="run-after-polls"),
        pytest.param(
            "chat",
            0,
            id="chat",
            marks=pytest.mark.skipif(sys.platform != "win32", reason="Windows console fixture"),
        ),
    ],
)
def test_cli_sigint_subprocess_saves_jobs_and_releases_lock(tmp_path, mode, queries_before_run):
    home = tmp_path / "state"
    job_id = seed_job(home)
    script = Path(__file__).with_name("job_cli_process.py")
    result = subprocess.run(
        [sys.executable, str(script), mode, job_id, str(queries_before_run)],
        cwd=tmp_path,
        env=environment(home),
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=20,
    )
    assert result.returncode == 130, (result.stdout, result.stderr)
    assert "Task was destroyed" not in result.stderr and "Traceback" not in result.stderr
    assert not (home / "instance.lock").exists()
    with FileStore.open(home) as store:
        state = store.snapshot()
        assert len(state["jobs"]) == 1 and state["jobs"][job_id]["submitAttempts"] <= 1
        if mode == "run":
            run = next(run for run in state["runs"].values() if run["id"] != "video-run")
            assert run["status"] == "cancelled"
            assert run["modelSteps"] == run["toolCalls"] == 1
            assert len(run["modelCalls"]) == 1
            assert not state["waits"][run["activeWaitId"]]["autoResume"]
            assert state["jobs"][job_id]["status"] == "queued"
            assert state["jobs"][job_id]["queryAttempts"] >= queries_before_run
        else:
            assert len(state["runs"]) == 1 and not state["waits"]
            assert state["jobs"][job_id]["status"] == "queued"


async def test_windows_console_input_is_cancellable_without_threads_and_preserves_unicode():
    keys = deque(["你", "好", "\b", "间", "\ud83d", "\ude42", "\r"])
    console = SimpleNamespace(kbhit=lambda: bool(keys), getwch=lambda: keys.popleft())
    assert await _windows_line(console) == "你间🙂"
    pending = asyncio.create_task(_windows_line(console))
    await asyncio.sleep(0.04)
    assert not pending.done()
    pending.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(pending, 1)


@pytest.mark.parametrize(
    "key,error", [("\x03", asyncio.CancelledError), ("\x1a", EOFError), ("\x04", EOFError)]
)
async def test_windows_console_control_keys_end_input(key, error):
    console = SimpleNamespace(kbhit=lambda: True, getwch=lambda: key)
    with pytest.raises(error):
        await _windows_line(console)
