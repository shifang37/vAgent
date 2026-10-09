"""B0-only offline LangGraph experiment; not an application Worker or Run coordinator.

Uses real FileStore operations and SQLite checkpoints in disposable directories.
The separate probe journal deliberately does not migrate production schema v1.
"""

import argparse
import asyncio
import copy
import json
import os
import tempfile
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TypedDict
from uuid import NAMESPACE_URL, uuid4, uuid5

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, ToolMessage
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt

from vagent.checkpoints import open_checkpointer
from vagent.context import assert_complete_protocol
from vagent.errors import AppError, failure
from vagent.runner import bounded_call
from vagent.storage import FileStore
from vagent.tools import create_project_tools
from vagent.waiting import (
    DeferredToolResult,
    ExternalResourceRef,
    ResumeToken,
    ToolExecutionContext,
    ToolResult,
    ToolSuccess,
    WaitBinding,
)


class ProbeState(TypedDict):
    messages: list[BaseMessage]


class WaitProbe:
    def __init__(self, store: FileStore, *, wait_count=1, crash_at=None):
        self.store = store
        self.path = store.home / "b0-probe.json"
        self.crash_at = crash_at
        self.tools = create_project_tools()
        store.ensure_session("probe")
        self.data = (
            json.loads(self.path.read_text(encoding="utf-8"))
            if self.path.exists()
            else {"modelCalls": 0, "toolKeys": [], "waits": {}, "readyResources": {}, "waitCount": wait_count}
        )
        self.save()

    def save(self):
        temporary = self.path.with_name(f"probe-{uuid4()}.tmp")
        try:
            with temporary.open("x", encoding="utf-8") as handle:
                json.dump(self.data, handle, ensure_ascii=False)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.path)
        finally:
            temporary.unlink(missing_ok=True)

    def binding(self, key):
        return WaitBinding.model_validate(self.data["waits"][key])

    def change_binding(self, key, **updates):
        raw = self.binding(key).model_dump(mode="json", by_alias=True)
        raw.update(revision=raw["revision"] + 1, **updates)
        value = WaitBinding.model_validate(raw)
        self.data["waits"][key] = value.model_dump(mode="json", by_alias=True)
        self.save()
        return value

    def publish_ready(self, key):
        binding = self.binding(key)
        if binding.status == "armed" and binding.resource.id in self.data["readyResources"]:
            self.change_binding(key, status="ready", result=self.data["readyResources"][binding.resource.id])

    def complete(self, resource_id, *, result: ToolResult | None = None):
        result = (
            result
            if result is not None
            else ToolSuccess(data={"resourceId": resource_id, "simulated": True, "mediaAvailable": False})
        )
        self.data["readyResources"][resource_id] = result.model_dump(mode="json", by_alias=True)
        self.save()
        for key in tuple(self.data["waits"]):
            binding = self.binding(key)
            if binding.resource.id == resource_id:
                self.publish_ready(key)

    def calls(self):
        calls = []
        for index in range(self.data["waitCount"] + 1):
            calls.append(
                {
                    "id": f"save-{index}",
                    "name": "artifact_save",
                    "args": {"kind": "brief", "title": f"part-{index}", "content": f"saved part {index}"},
                }
            )
            if index < self.data["waitCount"]:
                calls.append(
                    {
                        "id": f"wait-{index}",
                        "name": "fixture_wait",
                        "args": {"resourceId": f"external-{index}"},
                    }
                )
        return calls

    async def execute_call(self, call, context):
        key = context.operation_key
        if call["name"] != "fixture_wait":
            return await self.tools.aexecute(
                call["name"], call["args"], store=self.store, project_id="probe", operation_key=key
            )
        resource_id = call["args"]["resourceId"]
        # Replayed deferred calls MUST still consume their original interrupt index,
        # even after their Operation result exists or their resource has completed.
        if key in self.data["waits"]:
            return self.binding(key).deferred()
        if resource_id in self.data["readyResources"]:
            result = self.data["readyResources"][resource_id]
            return self.store.operation(key, call["name"], call["args"], lambda _: result["data"])
        started = datetime.now(UTC)
        binding = WaitBinding(
            id=str(uuid5(NAMESPACE_URL, key)),
            context=context,
            resource=ExternalResourceRef(kind="fixture", id=resource_id),
            started_at=started.isoformat(),
            deadline_at=(started + timedelta(minutes=10)).isoformat(),
        )
        self.data["waits"][key] = binding.model_dump(mode="json", by_alias=True)
        self.save()
        if self.crash_at == "prepared":
            os._exit(73)
        return binding.deferred()

    def graph(self):
        async def model_node(state):
            assert_complete_protocol(state["messages"])
            self.data["modelCalls"] += 1
            self.save()
            if isinstance(state["messages"][-1], HumanMessage):
                reply = AIMessage(content="", tool_calls=self.calls())
            else:
                reply = AIMessage(content="Observed all persisted tool results; simulation only.")
            return {"messages": [*state["messages"], reply]}

        async def tools_node(state):
            results = []
            for call in state["messages"][-1].tool_calls:
                context = ToolExecutionContext(
                    project_id="probe",
                    session_id="probe",
                    run_id="probe-run",
                    model_step=1,
                    tool_call_id=call["id"],
                )
                key = context.operation_key
                if key not in self.data["toolKeys"]:
                    self.data["toolKeys"].append(key)
                    self.save()
                try:
                    result = await bounded_call(
                        lambda: self.execute_call(call, context), asyncio.Event(), time.monotonic() + 10
                    )
                except Exception:
                    result = failure("PROBE_TOOL_ERROR", "Offline experiment tool failed")
                # Outside the tool-error boundary: GraphInterrupt must reach LangGraph.
                if isinstance(result, DeferredToolResult):
                    token = ResumeToken.model_validate(
                        interrupt(result.model_dump(mode="json", by_alias=True))
                    )
                    binding = self.binding(key)
                    confirmed = binding.confirmed_result(token)
                    if binding.status == "ready":
                        binding = self.change_binding(
                            key, status="claimed", claimedModelSteps=self.data["modelCalls"]
                        )

                    def finish(_):
                        if not confirmed.ok:
                            raise AppError(confirmed.error.code, confirmed.error.message)
                        return copy.deepcopy(confirmed.data)

                    result = self.store.operation(key, call["name"], call["args"], finish)
                    if self.crash_at == "result":
                        os._exit(73)
                results.append(
                    ToolMessage(content=json.dumps(result), tool_call_id=call["id"], name=call["name"])
                )
            return {"messages": [*state["messages"], *results]}

        graph = StateGraph(ProbeState)
        graph.add_node("model", model_node)
        graph.add_node("tools", tools_node)
        graph.add_edge(START, "model")
        graph.add_conditional_edges("model", lambda s: "tools" if s["messages"][-1].tool_calls else END)
        graph.add_edge("tools", "model")
        return graph

    @staticmethod
    def interrupts(snapshot):
        return [item for task in snapshot.tasks for item in task.interrupts]

    def synchronize(self, snapshot):
        """Reconcile the probe journal only against an actual persisted graph."""
        if not snapshot.values:
            return
        checkpoint_id = snapshot.config["configurable"]["checkpoint_id"]
        for pending in self.interrupts(snapshot):
            for key in tuple(self.data["waits"]):
                binding = self.binding(key)
                if binding.id == pending.value["waitId"] and binding.status == "preparing":
                    self.change_binding(
                        key, status="armed", checkpointId=checkpoint_id, interruptId=pending.id
                    )
                    self.publish_ready(key)
        completed = {m.tool_call_id for m in snapshot.values["messages"] if isinstance(m, ToolMessage)}
        for key in tuple(self.data["waits"]):
            binding = self.binding(key)
            if binding.context.tool_call_id in completed and binding.status == "claimed":
                self.change_binding(key, status="delivered", deliveredCheckpointId=checkpoint_id)

    async def advance(self, token: ResumeToken | None = None):
        config = {"configurable": {"thread_id": "b0-probe"}}
        async with open_checkpointer(self.store.home) as saver:
            graph = self.graph().compile(checkpointer=saver)
            previous = await graph.aget_state(config)
            pending = self.interrupts(previous)
            self.synchronize(previous)
            if previous.values and not previous.next and not pending:
                return previous  # Duplicate wake-ups never start another model node.
            if previous.values:
                if token is None:
                    if pending:
                        return previous
                    if previous.next != ("tools",):
                        raise AppError("PROBE_INTERRUPTED", "Only the pending tool node may be reconstructed")
                    command = None  # A crash may precede the first interrupt checkpoint.
                else:
                    match = next((i for i in pending if i.value["waitId"] == token.wait_id), None)
                    if match is None:
                        raise AppError("STALE_WAIT", "No matching pending interrupt")
                    binding = next(
                        self.binding(k) for k in self.data["waits"] if self.binding(k).id == token.wait_id
                    )
                    binding.confirmed_result(token)  # Validate BEFORE committing a Command to SQLite.
                    command = Command(resume={match.id: token.model_dump(mode="json", by_alias=True)})
            else:
                command = (
                    {"messages": [HumanMessage(content="Offline B0 wait proof")]} if token is None else None
                )
                if command is None:
                    raise AppError("NO_CHECKPOINT", "Cannot resume a missing graph")
            await graph.ainvoke(command, config, durability="sync")
            snapshot = await graph.aget_state(config)
            if self.crash_at == "before_arm" and self.interrupts(snapshot):
                os._exit(73)
            if self.crash_at == "graph_commit" and not snapshot.next and not self.interrupts(snapshot):
                os._exit(73)
            self.synchronize(snapshot)
            if self.crash_at == "checkpoint" and self.interrupts(snapshot):
                os._exit(73)
            return snapshot


async def run_probe():
    with tempfile.TemporaryDirectory(prefix="vagent-b0-wait-") as directory:
        home = Path(directory)
        with FileStore.open(home) as store:
            probe = WaitProbe(store)
            paused = await probe.advance()
            before = {"modelCalls": probe.data["modelCalls"], "artifacts": len(store.snapshot()["artifacts"])}
            assert probe.interrupts(paused) and before == {"modelCalls": 1, "artifacts": 1}
        with FileStore.open(home) as store:
            probe = WaitProbe(store)
            probe.complete("external-0")
            binding = next(probe.binding(k) for k in probe.data["waits"])
            final = await probe.advance(binding.resume_token())
            duplicate = await probe.advance(binding.resume_token())
            assert not final.next and not probe.interrupts(final) and duplicate.values == final.values
            assert probe.data["modelCalls"] == 2 and len(probe.data["toolKeys"]) == 3
            assert_complete_protocol(final.values["messages"])
            artifacts = store.snapshot()["artifacts"]
            assert len(artifacts) == 2 and all(len(a["versions"]) == 1 for a in artifacts.values())
            return {
                "scope": "B0 offline checkpoint reopen; not production Job/Run integration",
                "status": "passed",
                "beforeRestart": before,
                "afterRestart": {"modelCalls": 2, "toolCalls": 3, "artifacts": 2},
                "toolCallIds": [
                    m.tool_call_id for m in final.values["messages"] if isinstance(m, ToolMessage)
                ],
                "waits": [probe.binding(k).status for k in probe.data["waits"]],
                "duplicateWakeupAddedCalls": 0,
                "providerRequests": 0,
            }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.output and args.output.exists():
        parser.error("Output already exists; choose a new report path")
    report = asyncio.run(run_probe())
    text = json.dumps(report, ensure_ascii=False, indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        with args.output.open("x", encoding="utf-8") as handle:
            handle.write(text + "\n")
    print(text)


if __name__ == "__main__":
    main()
