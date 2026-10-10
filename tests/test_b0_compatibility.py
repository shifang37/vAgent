import json

import pytest
from langchain_core.messages import AIMessage

from vagent.checkpoints import CHECKPOINT_VERSION
from vagent.context import ContextBuilder
from vagent.runner import AgentRunner
from vagent.storage import FileStore
from vagent.tools import create_project_tools
from vagent.video.jobs import JobService
from vagent.video.providers.mock import MockVideoAdapter
from vagent.video.tools import register_video_tools
from vagent.waiting import WAIT_EXECUTION_VERSION

# Captured from a03f249 before adding B0 contracts. Do not recompute from current
# prompts/specs: these hashes represent the configuration of persisted v1 Runs.
LEGACY_SIGNATURES = {
    1: "ca3d165c443da216c26394eb3d95d5400fc9633636e05ef90fd008388d41d3c7",
    2: "349273b9b922fca6637f83651e760dad4303343352ade27da2b30ca74ddcc7ae",
}


class LegacyModel:
    name = "b0-legacy-fixture"

    def __init__(self, *, finish=False):
        self.calls = 0
        self.finish = finish

    async def generate(self, messages, tools):
        self.calls += 1
        if self.finish:
            return AIMessage(content="The original artifact remains saved.")
        if self.calls == 1:
            return AIMessage(
                content="",
                tool_calls=[
                    {
                        "id": "original-save",
                        "name": "artifact_save",
                        "args": {"kind": "brief", "title": "original", "content": "original body"},
                    }
                ],
            )
        raise RuntimeError("offline interruption")


@pytest.mark.parametrize("version", [1, 2])
@pytest.mark.parametrize("enable_video", [False, True])
async def test_b0_preserves_v1_execution_with_both_context_layouts(store, version, enable_video):
    assert CHECKPOINT_VERSION == 1 and WAIT_EXECUTION_VERSION == 2
    runner = AgentRunner(
        store=store,
        model=LegacyModel(),
        tools=create_project_tools(),
        context=ContextBuilder(format_version=version),
    )
    assert runner.context_signature() == LEGACY_SIGNATURES[version]
    before = await runner.run("legacy", "Save an artifact", request_id="original-request")
    assert before["resumable"] and before["executionVersion"] == 1
    original = store.snapshot()
    assert original["schemaVersion"] == 3
    assert before["modelSteps"] == 2 and before["toolCalls"] == 1
    home = store.home
    store.close()
    # Recreate the v1 domain envelope around the unchanged execution-v1 checkpoint.
    # This now covers migration as well as both original configuration signatures.
    legacy = {key: value for key, value in original.items() if key not in {"jobs", "waits", "media"}}
    legacy["schemaVersion"] = 1
    for run in legacy["runs"].values():
        run.pop("videoMode", None)
        run.pop("toolFeatures", None)
    legacy_bytes = json.dumps(legacy, ensure_ascii=False).encode("utf-8")
    (home / "state.json").write_bytes(legacy_bytes)
    with FileStore.open(home) as reopened:
        assert reopened.snapshot()["schemaVersion"] == 3
        assert next(home.glob("state-v1-*.json")).read_bytes() == legacy_bytes
        model = LegacyModel(finish=True)
        tools = create_project_tools()
        if enable_video:
            register_video_tools(tools, JobService(reopened, [MockVideoAdapter(reopened)]))
        resumed = AgentRunner(store=reopened, model=model, tools=tools)
        final = await resumed.resume(before["id"])
        assert final["status"] == "completed" and final["modelSteps"] == 3
        assert final["contextVersion"] == version and final["toolCalls"] == 1
        assert final["policy"] == before["policy"]
        assert reopened.snapshot()["artifacts"] == original["artifacts"]
        assert reopened.snapshot()["operations"] == original["operations"]
        assert await resumed.resume(before["id"]) == final and model.calls == 1
