import json
import subprocess
import sys
from pathlib import Path

import pytest
from conftest import ScriptedModel, VideoClock
from langchain_core.messages import AIMessage, ToolMessage, messages_from_dict
from test_wait_runtime import Harness, outcomes

from vagent.context import assert_complete_protocol
from vagent.runner import AgentRunner
from vagent.storage import FileStore
from vagent.tools import create_project_tools
from vagent.video.jobs import JobService
from vagent.video.providers.mock import MockScenario, MockVideoAdapter
from vagent.video.tools import register_video_tools, resolve_job_wait
from vagent.video.worker import JobWorker
from vagent.wait_runtime import WaitCoordinator


@pytest.mark.parametrize(
    "phase",
    [
        "binding",
        "prepared",
        "before_arm",
        "armed",
        "job_done",
        "ready",
        "claimed",
        "command",
        "task_result",
        "result",
        "tools_checkpoint",
        "model_started",
        "terminal",
    ],
)
async def test_hard_exit_recovers_production_wait_without_repeating_model_or_side_effects(tmp_path, phase):
    home = tmp_path / "state"
    child = subprocess.run(
        [sys.executable, str(Path(__file__).with_name("wait_crash_worker.py")), str(home), phase],
        capture_output=True,
        timeout=25,
    )
    assert child.returncode == 73, child.stderr.decode(errors="replace")
    before = json.loads((home / "state.json").read_text(encoding="utf-8"))
    (run_id,) = before["runs"]
    (job_id,) = before["jobs"]
    (wait_id,) = before["waits"]
    assert before["runs"][run_id]["modelSteps"] == (3 if phase in {"model_started", "terminal"} else 2)
    assert home.resolve().is_relative_to(tmp_path.resolve())
    # Only this fixture's confirmed-exited process owned the stale instance lock.
    (home / "instance.lock").unlink()
    with FileStore.open(home) as store:
        adapter = MockVideoAdapter(store, scenario=MockScenario(states=["succeeded"]))
        jobs = JobService(store, [adapter])
        tools = register_video_tools(create_project_tools(), jobs)
        model = ScriptedModel(lambda messages, _: AIMessage(content="Recovered persisted simulation."))
        runner = AgentRunner(store=store, model=model, tools=tools)
        coordinator = WaitCoordinator(
            store,
            lambda _: runner,
            resolvers={"job": lambda binding: resolve_job_wait(store, binding)},
        )
        if phase not in {"model_started", "terminal"}:
            assert store.snapshot()["runs"][run_id]["status"] == "waiting_external"
        if phase == "model_started":
            interrupted = store.snapshot()["runs"][run_id]
            assert interrupted["status"] == "interrupted" and interrupted["activeSeconds"] >= 60
            assert interrupted["modelCalls"][-1]["status"] == "interrupted"
            paused = await coordinator.run_once()
            assert paused["waitResumeError"]["code"] == "EXPLICIT_RESUME_REQUIRED"
            assert await coordinator.run_once() is not None and model.calls == 0
            final = await runner.resume(run_id)
        else:
            worker = JobWorker(jobs)
            for _ in range(4):
                await worker.run_once()
                await coordinator.run_once()
                if store.snapshot()["runs"][run_id]["status"] == "completed":
                    break
            final = store.snapshot()["runs"][run_id]
        assert final["status"] == "completed", final
        assert final["id"] == run_id and final["policy"] == before["runs"][run_id]["policy"]
        assert final["modelSteps"] == (4 if phase == "model_started" else 3)
        assert final["toolCalls"] == 4 and model.calls == (0 if phase == "terminal" else 1)
        messages = messages_from_dict(final["messages"])
        assert_complete_protocol(messages)
        tool_messages = [message for message in messages if isinstance(message, ToolMessage)]
        assert [message.tool_call_id for message in tool_messages] == ["generate", "before", "await", "after"]
        assert json.loads(tool_messages[2].content)["data"]["jobId"] == job_id
        state = store.snapshot()
        assert state["waits"][wait_id]["status"] == "delivered" and final["activeWaitId"] is None
        assert len(state["jobs"]) == 1 and state["jobs"][job_id]["submitAttempts"] == 1
        if before["jobs"][job_id]["providerTaskId"] is not None:
            assert state["jobs"][job_id]["providerTaskId"] == before["jobs"][job_id]["providerTaskId"]
        assert len(state["artifacts"]) == 2 and set(before["artifacts"]) <= set(state["artifacts"])
        assert all(len(artifact["versions"]) == 1 for artifact in state["artifacts"].values())
        assert adapter.ledger_snapshot()["submitCalls"] == 1
        for _ in range(3):
            assert await coordinator.run_once() is None
        assert await runner.resume(run_id) == final and store.snapshot() == state


@pytest.mark.parametrize("phase", ["wait.prepared", "wait.armed", "wait.result_committed"])
async def test_second_wait_survives_hard_exit_without_skipping_first_interrupt(tmp_path, phase):
    home = tmp_path / "state"
    child = subprocess.run(
        [sys.executable, str(Path(__file__).with_name("wait_batch_crash_worker.py")), str(home), phase],
        capture_output=True,
        timeout=25,
    )
    assert child.returncode == 73, child.stderr.decode(errors="replace")
    before = json.loads((home / "state.json").read_text(encoding="utf-8"))
    original = next(record for record in before["runs"].values() if record["requestId"] == "waiting")
    assert home.resolve().is_relative_to(tmp_path.resolve())
    (home / "instance.lock").unlink()
    clock = VideoClock()
    clock.advance(30)
    with FileStore.open(home) as store:
        harness = Harness(store, clock)
        harness.model = ScriptedModel(lambda *_: AIMessage(content="both original results received"))
        await harness.complete_jobs()
        for _ in range(3):
            await harness.coordinator.run_once()
            if store.snapshot()["runs"][original["id"]]["status"] == "completed":
                break
        final = store.snapshot()["runs"][original["id"]]
        assert final["status"] == "completed", final
        results = outcomes(final)
        assert list(results) == ["save-0", "await-0", "save-1", "await-1", "save-2"]
        assert results["await-0"]["data"]["jobId"] != results["await-1"]["data"]["jobId"]
        assert final["modelSteps"] == 2 and final["toolCalls"] == 5 and harness.model.calls == 1
        assert len(store.snapshot()["artifacts"]) == 3
        assert all(len(artifact["versions"]) == 1 for artifact in store.snapshot()["artifacts"].values())
        assert harness.adapter.ledger_snapshot()["submitCalls"] == 2
        assert all(binding["status"] == "delivered" for binding in store.snapshot()["waits"].values())
