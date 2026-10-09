"""Child-process fixture for abrupt exits of the production B3 graph and Worker."""

import asyncio
import json
import os
import sys

from langchain_core.messages import AIMessage
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

from vagent.runner import AgentRunner
from vagent.storage import FileStore
from vagent.tools import create_project_tools
from vagent.video.jobs import JobService
from vagent.video.providers.mock import MockScenario, MockVideoAdapter
from vagent.video.tools import register_video_tools, resolve_job_wait
from vagent.video.worker import JobWorker
from vagent.wait_runtime import WaitCoordinator, WaitService


async def main():
    phase = sys.argv[2]
    if phase in {"command", "task_result"}:
        original = AsyncSqliteSaver.aput_writes

        async def crash_after_resume(self, config, writes, task_id, task_path=""):
            await original(self, config, writes, task_id, task_path)
            if any(
                channel == ("__resume__" if phase == "command" else "wait_deliveries") and value
                for channel, value in writes
            ):
                os._exit(73)

        AsyncSqliteSaver.aput_writes = crash_after_resume
    with FileStore.open(sys.argv[1]) as store:
        adapter = MockVideoAdapter(store, scenario=MockScenario(states=["succeeded"]))
        jobs = JobService(store, [adapter])
        tools = register_video_tools(create_project_tools(), jobs)

        class Model:
            name = "scripted-test"

            def __init__(self):
                self.calls = 0

            async def generate(self, messages, tools):
                self.calls += 1
                if self.calls == 1:
                    capabilities = adapter.capabilities()
                    return AIMessage(
                        content="",
                        tool_calls=[
                            {
                                "id": "generate",
                                "name": "video_generate",
                                "args": {
                                    "provider": capabilities.provider,
                                    "model": capabilities.model,
                                    "capabilitiesVersion": capabilities.capabilities_version,
                                    "prompt": "B3 persistent mock fixture",
                                    "spec": capabilities.specs[0].model_dump(mode="json", by_alias=True),
                                },
                            }
                        ],
                    )
                if self.calls == 2:
                    job_id = json.loads(messages[-1].content)["data"]["jobId"]

                    def save(name):
                        return {
                            "id": name,
                            "name": "artifact_save",
                            "args": {"kind": "brief", "title": name, "content": name},
                        }

                    return AIMessage(
                        content="",
                        tool_calls=[
                            save("before"),
                            {"id": "await", "name": "await_job", "args": {"jobId": job_id}},
                            save("after"),
                        ],
                    )
                if phase == "model_started":
                    os._exit(73)
                return AIMessage(content="Persisted simulation complete; no media.")

        model = Model()

        def event(event):
            event_type = event["type"]
            matching = {
                "prepared": "wait.prepared",
                "armed": "wait.armed",
                "ready": "wait.ready",
                "claimed": "wait.claimed",
                "result": "wait.result_committed",
                "tools_checkpoint": "wait.delivered",
            }
            if event_type == matching.get(phase):
                os._exit(73)
            if event_type == "wait.checkpointed":
                record = store.snapshot()["runs"][event["runId"]]
                if (phase == "before_arm" and record["modelSteps"] == 2) or (
                    phase == "terminal" and record["modelSteps"] == 3
                ):
                    os._exit(73)

        if phase == "binding":

            def crash_before_preparing(*_):
                os._exit(73)

            WaitService.prepare = crash_before_preparing

        def runner(_=None):
            return AgentRunner(store=store, model=model, tools=tools, on_event=event)

        pending = await runner().run("coffee", "B3 crash fixture")
        assert pending["status"] == "waiting_external"
        job = await JobWorker(jobs).run_once()
        assert job.status == "succeeded"
        if phase == "job_done":
            os._exit(73)
        coordinator = WaitCoordinator(
            store,
            runner,
            resolvers={"job": lambda binding: resolve_job_wait(store, binding)},
            on_event=event,
        )
        await coordinator.run_once()
        raise AssertionError(f"Crash point not reached: {phase}")


if __name__ == "__main__":
    asyncio.run(main())
