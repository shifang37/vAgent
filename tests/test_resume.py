import asyncio
import json
import os
import subprocess
import sys
from types import SimpleNamespace

import pytest
from conftest import ScriptedModel, tool_call
from langchain_core.messages import AIMessage, ToolMessage, messages_from_dict

from vagent.checkpoints import open_checkpointer
from vagent.context import assert_complete_protocol
from vagent.errors import AppError
from vagent.journal import RunJournal
from vagent.models import DemoModel
from vagent.runner import AgentRunner, RunPolicy
from vagent.skills import SkillCatalog, register_skill_tool
from vagent.storage import FileStore
from vagent.tools import create_project_tools


def runner(store, callback, **kwargs):
    return AgentRunner(store=store, model=ScriptedModel(callback), tools=create_project_tools(), **kwargs)


async def fail(*_):
    raise RuntimeError("Authorization: secret-must-not-leak")


async def test_resume_provider_failure_uses_checkpoint_results_after_restart(store):
    def respond(messages, step):
        if step == 0:
            return tool_call("artifact_save", {"kind": "brief", "title": "original", "content": "saved"})
        return fail()

    first = await runner(store, respond).run("coffee", "save", request_id="same-request")
    assert first["status"] == "failed" and first["resumable"]
    assert first["modelSteps"] == 2 and first["toolCalls"] == 1
    assert "secret-must-not-leak" not in json.dumps(first)
    home = store.home
    store.close()
    with FileStore.open(home) as reopened:

        def finish(messages, _):
            assert isinstance(messages[-1], ToolMessage)
            assert json.loads(messages[-1].content)["data"]["persisted"]
            return AIMessage(content="done")

        resumed = runner(reopened, finish)
        result = await resumed.resume(first["id"])
        assert result["id"] == first["id"] and result["requestId"] == "same-request"
        assert result["status"] == "completed" and not result["resumable"]
        assert result["modelSteps"] == 3 and result["toolCalls"] == 1
        assert result["activeSeconds"] >= first["activeSeconds"]
        assert len(reopened.snapshot()["artifacts"]) == 1
        assert reopened.snapshot()["sessions"]["coffee"]["messages"] == result["messages"]
        assert await resumed.resume(first["id"]) == result
        assert await resumed.run("coffee", "save", request_id="same-request") == result
        assert resumed.model.calls == 1


async def test_cancelled_tool_can_be_explicitly_resumed(store):
    cancelled = asyncio.Event()

    def stop(event):
        if event["type"] == "tool.started":
            cancelled.set()

    first = await runner(
        store,
        lambda *_: tool_call("artifact_save", {"kind": "brief", "title": "test", "content": "body"}),
        on_event=stop,
    ).run("coffee", "save", cancelled=cancelled)
    assert first["status"] == "cancelled" and first["resumable"]
    assert first["toolCalls"] == 0
    assert_complete_protocol(messages_from_dict(first["messages"]))
    result = await runner(store, lambda *_: AIMessage(content="saved")).resume(first["id"])
    assert result["status"] == "completed" and result["toolCalls"] == 1
    assert result["modelSteps"] == 2
    assert len(store.snapshot()["artifacts"]) == 1


async def test_external_cancellation_resumes_pending_model_and_retains_usage(store):
    started = asyncio.Event()

    async def respond(messages, step):
        if step == 0:
            reply = tool_call("project_read")
            reply.usage_metadata = {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15}
            return reply
        started.set()
        await asyncio.Event().wait()

    pending = asyncio.create_task(runner(store, respond).run("coffee", "test"))
    await asyncio.wait_for(started.wait(), 2)
    pending.cancel()
    first = await pending
    assert first["status"] == "cancelled" and first["resumable"]
    assert first["inputTokens"] == 10
    reply = AIMessage(
        content="done", usage_metadata={"input_tokens": 20, "output_tokens": 7, "total_tokens": 27}
    )
    resumed = runner(store, lambda *_: reply)
    final = await resumed.resume(first["id"])
    assert final["status"] == "completed" and final["modelSteps"] == 3
    assert final["inputTokens"] == 30 and final["outputTokens"] == 12


async def test_call_ids_are_scoped_to_model_step(store):
    def respond(messages, step):
        if step < 2:
            return tool_call("project_update", {"expectedRevision": step, "style": str(step)})
        return AIMessage(content="done")

    result = await runner(store, respond).run("coffee", "test")
    assert result["status"] == "completed" and result["toolCalls"] == 2
    assert store.snapshot()["projects"]["coffee"]["style"] == "1"


async def test_concurrent_resumes_do_not_start_two_models(store):
    first = await runner(store, fail).run("coffee", "test")
    started, release = asyncio.Event(), asyncio.Event()

    async def waiting(*_):
        started.set()
        await release.wait()
        return AIMessage(content="done")

    resumed = runner(store, waiting)
    pending = asyncio.create_task(resumed.resume(first["id"]))
    await asyncio.wait_for(started.wait(), 2)
    try:
        with pytest.raises(AppError, match="已有任务"):
            await resumed.resume(first["id"])
    finally:
        release.set()
        await pending
    assert resumed.model.calls == 1


async def test_legacy_run_is_not_restarted_as_a_new_graph(store):
    first = await runner(store, fail).run("coffee", "test")
    store.transaction(lambda draft: draft["runs"][first["id"]].pop("executionVersion"))
    resumed = runner(store, fail)
    with pytest.raises(AppError, match="旧版 Run"):
        await resumed.resume(first["id"])
    assert resumed.model.calls == 0


async def test_resume_keeps_original_step_limit_even_with_larger_new_policy(store):
    first = await runner(store, fail, policy=RunPolicy(max_steps=2)).run("coffee", "test")
    resumed = runner(store, fail, policy=RunPolicy(max_steps=100))
    second = await resumed.resume(first["id"])
    assert second["modelSteps"] == 2
    third = await resumed.resume(first["id"])
    assert third["errorCode"] == "STEP_LIMIT" and not third["resumable"]
    assert third["policy"]["maxSteps"] == 2 and resumed.model.calls == 1
    with pytest.raises(AppError, match="限额"):
        await resumed.resume(first["id"])


@pytest.mark.parametrize("frozen_tick", [None, 100.002])
@pytest.mark.parametrize("execution_version", [1, 2])
async def test_resume_keeps_spent_time_and_does_not_call_model(
    store, monkeypatch, frozen_tick, execution_version
):
    first = await runner(store, fail, execution_version=execution_version).run("coffee", "test")
    store.transaction(lambda draft: draft["runs"][first["id"]].update(activeSeconds=180.0))
    if frozen_tick is not None:
        # Model the same coarse monotonic tick across resume. Do not freeze the
        # event loop's real clock, which still schedules checkpoint I/O normally.
        clock = SimpleNamespace(monotonic=lambda: frozen_tick)
        monkeypatch.setattr("vagent.runner.time", clock)
        monkeypatch.setattr("vagent.journal.time", clock)
    resumed = runner(
        store,
        lambda *_: AIMessage(content="must not happen"),
        policy=RunPolicy(timeout_seconds=900),
        execution_version=execution_version,
    )
    result = await resumed.resume(first["id"])
    assert result["errorCode"] == "TIMEOUT" and not result["resumable"]
    assert result["modelSteps"] == 1 and resumed.model.calls == 0


async def test_old_run_cannot_overwrite_new_conversation(store):
    first = await runner(store, fail).run("coffee", "old")
    latest = await runner(store, lambda *_: AIMessage(content="new answer")).run("coffee", "new")
    with pytest.raises(AppError, match="更新的请求"):
        await runner(store, fail).resume(first["id"])
    assert store.snapshot()["sessions"]["coffee"]["messages"] == latest["messages"]


async def test_resume_rejects_changed_configuration_and_missing_checkpoint(store):
    first = await runner(store, fail).run("coffee", "test")
    changed = runner(store, fail, skills=[{"name": "new", "version": "different"}])
    with pytest.raises(AppError, match="配置已变化"):
        await changed.resume(first["id"])
    async with open_checkpointer(store.home) as saver:
        await saver.adelete_thread(first["id"])
    resumed = runner(store, fail)
    result = await resumed.resume(first["id"])
    assert result["errorCode"] == "NO_CHECKPOINT" and resumed.model.calls == 0


async def test_corrupt_checkpoint_is_preserved_without_model_call(store):
    first = await runner(store, fail).run("coffee", "test")
    path = store.home / "checkpoints.sqlite"
    path.write_bytes(b"corrupt checkpoint")
    resumed = runner(store, fail)
    result = await resumed.resume(first["id"])
    assert result["errorCode"] == "CHECKPOINT_ERROR" and resumed.model.calls == 0
    assert path.read_bytes() == b"corrupt checkpoint"


async def test_terminal_graph_is_finalized_without_repeating_model(store, monkeypatch):
    class Crash(BaseException):
        pass

    publish = RunJournal.publish

    def crash_before_session_commit(self, state, **kwargs):
        if kwargs.get("final") and state["status"] == "completed":
            raise Crash()
        return publish(self, state, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(RunJournal, "publish", crash_before_session_commit)
        with pytest.raises(Crash):
            await runner(store, lambda *_: AIMessage(content="durable answer")).run("coffee", "test")
    home = store.home
    run_id = next(iter(store.snapshot()["runs"]))
    assert not store.snapshot()["sessions"]["coffee"]["messages"]
    store.close()
    with FileStore.open(home) as reopened:
        resumed = runner(reopened, fail)
        result = await resumed.resume(run_id)
        assert result["status"] == "completed" and result["answer"] == "durable answer"
        assert resumed.model.calls == 0 and result["modelSteps"] == 1
        assert reopened.snapshot()["sessions"]["coffee"]["messages"] == result["messages"]


# Run a real child process so SQLite, the JSON journal, and the instance lock
# experience abrupt process termination rather than Python's orderly cleanup.
CRASH_WORKER = """
import asyncio, os, sys
from langchain_core.messages import AIMessage, ToolMessage
from vagent.runner import AgentRunner, RunPolicy
from vagent.storage import FileStore
from vagent.tools import create_project_tools

class Model:
    name = "scripted-test"
    async def generate(self, messages, tools):
        if isinstance(messages[-1], ToolMessage):
            os._exit(73)
        return AIMessage(content="", tool_calls=[
            {"id": name, "name": "artifact_save", "args": {
                "kind": "brief", "title": name, "content": "saved " + name
            }} for name in ("first", "second")
        ])

def event(event):
    if sys.argv[2] == "tool" and event["type"] == "tool.completed":
        os._exit(73)

async def main():
    with FileStore.open(sys.argv[1]) as store:
        await AgentRunner(store=store, model=Model(), tools=create_project_tools(),
                          policy=RunPolicy(max_tool_calls=2), on_event=event).run("coffee", "save")
asyncio.run(main())
"""


@pytest.mark.parametrize("phase", ["tool", "model"])
async def test_hard_crash_replays_only_unfinished_work(tmp_path, phase):
    home = tmp_path / "state"
    worker = tmp_path / "crash_worker.py"
    worker.write_text(CRASH_WORKER, encoding="utf-8")
    result = subprocess.run([sys.executable, str(worker), str(home), phase], capture_output=True, timeout=20)
    assert result.returncode == 73, result.stderr.decode(errors="replace")
    before = json.loads((home / "state.json").read_text(encoding="utf-8"))
    assert len(before["artifacts"]) == (1 if phase == "tool" else 2)
    run_id = next(iter(before["runs"]))
    # The child has exited; manually clear only its stale lock, as users must.
    (home / "instance.lock").unlink()
    with FileStore.open(home) as store:
        interrupted = store.snapshot()["runs"][run_id]
        assert interrupted["status"] == "interrupted"
        assert interrupted["inFlightSeconds"] == 0
        if phase == "model":
            assert interrupted["activeSeconds"] >= 60
            assert interrupted["modelCalls"][-1]["status"] == "interrupted"
            assert interrupted["modelCalls"][-1]["inputTokens"] is None

        def finish(messages, _):
            results = [json.loads(m.content)["data"] for m in messages if isinstance(m, ToolMessage)]
            assert len(results) == 2 and all(result["version"] == 1 for result in results)
            return AIMessage(content="both saved")

        resumed = runner(store, finish)
        final = await resumed.resume(run_id)
        assert final["status"] == "completed"
        assert final["toolCalls"] == 2
        assert final["modelSteps"] == (2 if phase == "tool" else 3)
        assert len(final["modelCalls"]) == final["modelSteps"]
        assert resumed.model.calls == 1
        artifacts = store.snapshot()["artifacts"]
        assert len(artifacts) == 2 and all(len(a["versions"]) == 1 for a in artifacts.values())
        assert set(before["artifacts"]).issubset(artifacts)
        assert_complete_protocol(messages_from_dict(final["messages"]))


async def test_cli_resume_offline_run_without_key_or_source_cwd(store, tmp_path):
    cancelled = asyncio.Event()
    cancelled.set()
    catalog = SkillCatalog.discover()
    first = await AgentRunner(
        store=store,
        model=DemoModel(),
        tools=register_skill_tool(create_project_tools(), catalog),
        skills=catalog.list(),
    ).run("demo", "test", cancelled=cancelled)
    home = store.home
    store.close()
    env = {k: v for k, v in os.environ.items() if k not in {"DEEPSEEK_API_KEY", "VAGENT_DEEPSEEK_KEY"}}
    env.update(VAGENT_HOME=str(home), PYTHONIOENCODING="utf-8")
    result = subprocess.run(
        [sys.executable, "-m", "vagent", "resume", first["id"]],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=20,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "[completed]" in result.stdout
    with FileStore.open(home) as reopened:
        final = reopened.snapshot()["runs"][first["id"]]
        assert final["status"] == "completed" and len(reopened.snapshot()["artifacts"]) == 1
