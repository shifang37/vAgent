import json

import pytest
from conftest import ScriptedModel, tool_call, video_request, video_run
from langchain_core.messages import AIMessage

from vagent.context import ContextBuilder
from vagent.runner import AgentRunner
from vagent.storage import FileStore
from vagent.tools import create_project_tools
from vagent.video.jobs import JobService
from vagent.video.providers.mock import MockScenario, MockVideoAdapter
from vagent.video.tools import register_video_tools, resolve_job_wait
from vagent.video.worker import JobWorker
from vagent.wait_runtime import WaitCoordinator

# Captured by executing the original 41be04a sources, not derived from B3 rules.
B2_SIGNATURES = {
    (1, False): "c2619b2ad62f233f38859dde49e72b8a952207b0647c51571f07d2494ce0b19b",
    (1, True): "cc2e986cb746977000078324fe76ab7e5d8d3aa8658625c5d0a92712ebd58570",
    (2, False): "14cc6667baafce8930a4c16aad877080048542c4f912f37f9e7c3a8a5ebc6e10",
    (2, True): "95d14e349b801f097b05d87e35734f4ecd2060aa113617b9a13b4d87568e7701",
}


@pytest.mark.parametrize("context_version", [1, 2])
@pytest.mark.parametrize("read_only", [False, True])
async def test_original_b2_video_fingerprints_and_checkpoints_resume_unchanged(
    store, context_version, read_only
):
    def response(messages, step):
        if step == 0:
            return tool_call("video_capabilities")
        raise RuntimeError("offline failure")

    model = ScriptedModel(response)
    model.name = "b3-legacy-video-fixture"
    tools = register_video_tools(create_project_tools(), JobService(store, [MockVideoAdapter(store)]))
    runner = AgentRunner(
        store=store,
        model=model,
        tools=tools,
        read_only=read_only,
        execution_version=1,
        context=ContextBuilder(format_version=context_version),
    )
    assert runner.context_signature() == B2_SIGNATURES[context_version, read_only]
    first = await runner.run("coffee", "original B2 request")
    assert first["executionVersion"] == 1 and first["resumable"]
    assert first["toolFeatures"]["video"]["rulesVersion"] == 1
    operations = store.snapshot()["operations"]
    home = store.home
    store.close()
    with FileStore.open(home) as reopened:
        model = ScriptedModel(lambda *_: AIMessage(content="original B2 continued"))
        model.name = "b3-legacy-video-fixture"
        tools = register_video_tools(
            create_project_tools(), JobService(reopened, [MockVideoAdapter(reopened)])
        )
        current = AgentRunner(store=reopened, model=model, tools=tools, read_only=read_only)
        assert current.execution_version == 2 and current.tools.features["video"]["rulesVersion"] == 2
        final = await current.resume(first["id"])
        assert final["status"] == "completed" and final["executionVersion"] == 1
        assert final["contextSignature"] == B2_SIGNATURES[context_version, read_only]
        assert final["contextVersion"] == context_version and final["policy"] == first["policy"]
        assert final["modelSteps"] == 3 and final["toolCalls"] == 1 and model.calls == 1
        assert reopened.snapshot()["operations"] == operations
        assert "当前入口不会自动推进 Job" in model.inputs[0][0].content


async def test_b2_failed_preparing_wait_is_never_automatically_revived(store):
    adapter = MockVideoAdapter(store, scenario=MockScenario(states=["succeeded"]))
    jobs = JobService(store, [adapter])
    job_id = jobs.generate(video_request(adapter), context=video_run(store))["data"]["jobId"]
    tools = register_video_tools(create_project_tools(), jobs)
    model = ScriptedModel(lambda *_: tool_call("await_job", {"jobId": job_id}))
    old = await AgentRunner(store=store, model=model, tools=tools, execution_version=1).run(
        "coffee", "old wait"
    )
    assert old["errorCode"] == "EXTERNAL_WAIT_UNAVAILABLE" and not old["resumable"]
    await JobWorker(jobs).run_once()
    before = store.snapshot()
    coordinator = WaitCoordinator(
        store,
        lambda _: AgentRunner(store=store, model=model, tools=tools),
        resolvers={"job": lambda binding: resolve_job_wait(store, binding)},
    )
    assert await coordinator.run_once() is None and model.calls == 1
    assert store.snapshot() == before
    assert list(before["waits"].values())[0]["status"] == "preparing"
    assert "EXTERNAL_WAIT_UNAVAILABLE" in json.dumps(old["messages"])
