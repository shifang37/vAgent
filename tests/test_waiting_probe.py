import json
import subprocess
import sys
from pathlib import Path

import pytest
from langchain_core.messages import ToolMessage
from pydantic import ValidationError

from scripts.probe_m1b_wait import WaitProbe, run_probe
from vagent.context import assert_complete_protocol
from vagent.errors import AppError
from vagent.storage import FileStore
from vagent.waiting import (
    ResumeToken,
    ToolExecutionContext,
    ToolFailure,
    ToolResultError,
    ToolSuccess,
    WaitBinding,
)


def test_wait_contract_refuses_uncheckpointed_or_stopped_delivery():
    context = ToolExecutionContext(
        project_id="coffee", session_id="coffee", run_id="run", model_step=2, tool_call_id="original"
    )
    raw = {
        "id": "wait",
        "context": context,
        "resource": {"kind": "job", "id": "job"},
        "startedAt": "2026-10-09T02:00:00Z",
        "deadlineAt": "2026-10-09T02:10:00Z",
    }
    preparing = WaitBinding.model_validate(raw)
    assert preparing.context.operation_key == "run:2:original"
    with pytest.raises(AppError, match="尚无"):
        preparing.confirmed_result(preparing.resume_token())
    for change in ({"status": "armed"}, {"status": "stopped"}, {"deadlineAt": raw["startedAt"]}):
        with pytest.raises(ValidationError):
            WaitBinding.model_validate({**raw, **change})
    ready = WaitBinding.model_validate(
        {
            **raw,
            "status": "ready",
            "checkpointId": "checkpoint",
            "interruptId": "interrupt",
            "result": {"ok": True, "data": {"simulated": True}},
        }
    )
    assert ready.confirmed_result(ready.resume_token()).data == {"simulated": True}
    with pytest.raises(AppError, match="代次"):
        ready.confirmed_result(ResumeToken(wait_id="wait", generation=2))
    with pytest.raises(ValidationError):
        ResumeToken.model_validate({"waitId": "wait", "generation": 1, "result": {"ok": True}})
    with pytest.raises(ValidationError):
        ToolSuccess(ok=1, data={})
    assert WaitBinding.model_validate(ready.model_dump(mode="json", by_alias=True)) == ready
    stopped = WaitBinding.model_validate(
        {**ready.model_dump(by_alias=True), "status": "stopped", "autoResume": False}
    )
    with pytest.raises(AppError, match="已停止"):
        stopped.confirmed_result(stopped.resume_token())


@pytest.mark.parametrize("payload", [{"value": float("nan")}, {"value": float("inf")}, {"value": b"bytes"}])
def test_wait_result_requires_portable_json(payload):
    with pytest.raises(ValidationError):
        ToolSuccess(data=payload)


async def test_real_sqlite_wait_reopens_without_repeating_tools_or_model():
    report = await run_probe()
    assert report["status"] == "passed" and report["providerRequests"] == 0
    assert report["toolCallIds"] == ["save-0", "wait-0", "save-1"]
    assert report["waits"] == ["delivered"]


async def test_same_batch_waits_replay_interrupts_in_original_order(store):
    probe = WaitProbe(store, wait_count=2)
    first = await probe.advance()
    assert len(probe.interrupts(first)) == 1
    assert (await probe.advance()).values == first.values
    assert probe.data["modelCalls"] == 1
    first_key = "probe-run:1:wait-0"
    token = probe.binding(first_key).resume_token()
    with pytest.raises(AppError, match="尚无"):
        await probe.advance(token)
    probe.complete("external-0")
    second = await probe.advance(token)
    assert probe.data["modelCalls"] == 1 and len(store.snapshot()["artifacts"]) == 2
    assert probe.interrupts(second)[0].value["resource"]["id"] == "external-1"
    with pytest.raises(AppError, match="matching"):
        await probe.advance(token)
    probe.complete("external-1")
    final = await probe.advance(probe.binding("probe-run:1:wait-1").resume_token())
    assert not final.next and not probe.interrupts(final) and probe.data["modelCalls"] == 2
    assert len(probe.data["toolKeys"]) == 5 and len(store.snapshot()["artifacts"]) == 3
    messages = final.values["messages"]
    assert_complete_protocol(messages)
    observed = {m.tool_call_id: json.loads(m.content) for m in messages if isinstance(m, ToolMessage)}
    assert observed["wait-0"]["data"]["resourceId"] == "external-0"
    assert observed["wait-1"]["data"]["resourceId"] == "external-1"


async def test_already_finished_resource_needs_no_interrupt(store):
    probe = WaitProbe(store)
    probe.complete("external-0")
    final = await probe.advance()
    assert not final.next and not probe.interrupts(final) and not probe.data["waits"]
    assert probe.data["modelCalls"] == 2 and len(store.snapshot()["artifacts"]) == 2


async def test_failure_arriving_before_wait_is_armed_keeps_original_error(store):
    probe = WaitProbe(store)
    synchronize = probe.synchronize
    failure = ToolFailure(error=ToolResultError(code="FIXTURE_FAILED", message="模拟外部任务失败"))

    def complete_before_arming(snapshot):
        if probe.interrupts(snapshot) and any(
            probe.binding(k).status == "preparing" for k in probe.data["waits"]
        ):
            probe.complete("external-0", result=failure)
        synchronize(snapshot)

    probe.synchronize = complete_before_arming
    await probe.advance()
    binding = probe.binding("probe-run:1:wait-0")
    assert binding.status == "ready" and binding.result == failure
    final = await probe.advance(binding.resume_token())
    outcomes = [
        m for m in final.values["messages"] if isinstance(m, ToolMessage) and m.tool_call_id == "wait-0"
    ]
    assert len(outcomes) == 1 and json.loads(outcomes[0].content) == failure.model_dump(
        mode="json", by_alias=True
    )
    assert probe.data["modelCalls"] == 2
    assert_complete_protocol(final.values["messages"])


CRASH_PROBE = """
import asyncio, sys
from scripts.probe_m1b_wait import WaitProbe
from vagent.storage import FileStore

async def main():
    with FileStore.open(sys.argv[1]) as store:
        probe = WaitProbe(store, crash_at=sys.argv[2] if sys.argv[2] in {'prepared', 'before_arm', 'checkpoint'} else None)
        await probe.advance()
        probe.complete('external-0')
        probe.crash_at = sys.argv[2]
        await probe.advance(probe.binding('probe-run:1:wait-0').resume_token())
asyncio.run(main())
"""


@pytest.mark.parametrize("phase", ["prepared", "before_arm", "checkpoint", "result", "graph_commit"])
async def test_hard_exit_recovers_original_wait_and_operation_results(tmp_path, phase):
    home = tmp_path / "probe-state"
    result = subprocess.run(
        [sys.executable, "-c", CRASH_PROBE, str(home), phase],
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True,
        timeout=25,
    )
    assert result.returncode == 73, result.stderr.decode(errors="replace")
    saved = json.loads((home / "state.json").read_text(encoding="utf-8"))
    assert len(saved["artifacts"]) == (2 if phase == "graph_commit" else 1)
    assert ("probe-run:1:wait-0" in saved["operations"]) == (phase in {"result", "graph_commit"})
    assert home.resolve().is_relative_to(tmp_path.resolve())
    # Only this test's confirmed-exited child owned this lock.
    (home / "instance.lock").unlink()
    with FileStore.open(home) as store:
        probe = WaitProbe(store)
        await probe.advance()  # Reconcile/reconstruct a prepared wait without a new model call.
        assert probe.data["modelCalls"] == (2 if phase == "graph_commit" else 1)
        probe.complete("external-0")
        final = await probe.advance(probe.binding("probe-run:1:wait-0").resume_token())
        assert not final.next and not probe.interrupts(final) and probe.data["modelCalls"] == 2
        assert len(probe.data["toolKeys"]) == 3
        assert len(store.snapshot()["artifacts"]) == 2
        assert set(saved["artifacts"]).issubset(store.snapshot()["artifacts"])
        assert all(len(a["versions"]) == 1 for a in store.snapshot()["artifacts"].values())
        assert probe.binding("probe-run:1:wait-0").status == "delivered"
        assert_complete_protocol(final.values["messages"])
