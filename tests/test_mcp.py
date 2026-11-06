import asyncio
import json
import sys
import time
from pathlib import Path

import pytest
from conftest import ScriptedModel, tool_call
from langchain_core.messages import AIMessage, messages_from_dict

from vagent.application import ApplicationService
from vagent.config import Config
from vagent.context import assert_complete_protocol
from vagent.errors import AppError
from vagent.mcp_bridge import (
    ServerConfig,
    check_schema,
    connect_mcp,
    result_payload,
    server_configs,
)
from vagent.runner import AgentRunner, RunPolicy
from vagent.tools import ToolDefinition, ToolRegistry


async def test_real_mcp_discovery_validation_execution_and_operation_replay(store):
    registry = ToolRegistry()
    async with connect_mcp(registry, server_configs(None, True)) as statuses:
        assert statuses[0]["status"] == "connected" and statuses[0]["transport"] == "stdio"
        assert {tool["function"]["name"] for tool in registry.specs()} == {
            "mcp_video_shot_timing",
            "mcp_video_frame_budget",
        }
        args = {"durations": [5, 7, 11, 7], "target_seconds": 30}
        store.ensure_session("test")
        first = await registry.aexecute(
            "mcp_video_shot_timing", args, store=store, project_id="test", operation_key="real-mcp"
        )
        assert first["ok"], first
        data = first["data"]["structuredContent"]
        assert data["totalSeconds"] == 30 and data["matches"]
        assert data["timeline"][-1]["end"] == 30
        repeated = await registry.aexecute(
            "mcp_video_shot_timing", args, store=store, project_id="test", operation_key="real-mcp"
        )
        assert repeated == first and len(store.snapshot()["operations"]) == 1
        conflicting = await registry.aexecute(
            "mcp_video_shot_timing",
            {**args, "target_seconds": 40},
            store=store,
            project_id="test",
            operation_key="real-mcp",
        )
        assert conflicting["error"]["code"] == "OPERATION_CONFLICT"
        invalid = await registry.aexecute(
            "mcp_video_shot_timing",
            {**args, "durations": [-5]},
            store=store,
            project_id="test",
            operation_key="invalid",
        )
        assert invalid["error"]["code"] == "INVALID_ARGUMENTS"
        missing = await registry.aexecute(
            "mcp_video_shell", {}, store=store, project_id="test", operation_key="missing"
        )
        assert missing["error"]["code"] == "UNKNOWN_TOOL"


async def test_agent_graph_uses_real_mcp_result_then_saves_artifact(tmp_path):
    def respond(messages, step):
        if step == 0:
            return tool_call("mcp_video_frame_budget", {"seconds": 30, "fps": 24, "aspect_ratio": "9:16"})
        if step == 1:
            data = json.loads(messages[-1].content)["data"]["structuredContent"]
            assert data["frames"] == 720 and data["suggestedSize"] == [1080, 1920]
            return tool_call(
                "artifact_save",
                {"kind": "storyboard", "title": "MCP 帧数", "content": f"30 秒，{data['frames']} 帧"},
                call_id="save",
            )
        return AIMessage(content="已保存 720 帧规划")

    config = Config(home=tmp_path / "graph", api_key=None, mcp_local=True)
    async with ApplicationService.open(config, model=ScriptedModel(respond)) as service:
        first = await service.start("mcp", "计算并保存", "mcp-chain")
        await service.task
        result = service.run_record(first["id"])
        assert result["status"] == "completed" and result["toolCalls"] == 2
        assert len(service.session("mcp")["artifacts"]) == 1
        assert_complete_protocol(messages_from_dict(result["messages"]))


async def test_mcp_faults_and_cancellation_are_bounded_and_sanitized(store):
    registry = ToolRegistry()
    fixture = str(Path(__file__).with_name("mcp_fixture_server.py"))
    config = ServerConfig(
        name="fixture", command=sys.executable, args=[fixture], read_only_tools=["fail", "large", "slow"]
    )
    async with connect_mcp(registry, [config]):
        store.ensure_session("test")
        for name, code in [("fail", "MCP_TOOL_ERROR"), ("large", "MCP_RESULT_LIMIT")]:
            result = await registry.aexecute(
                f"mcp_fixture_{name}", {}, store=store, project_id="test", operation_key=name
            )
            assert result["error"]["code"] == code
            assert "private-server-exception" not in json.dumps(result)
        cancel = asyncio.Event()

        def events(event):
            if event["type"] == "tool.started":
                asyncio.get_running_loop().call_later(0.08, cancel.set)

        runner = AgentRunner(
            store=store,
            model=ScriptedModel(lambda *_: tool_call("mcp_fixture_slow")),
            tools=registry,
            on_event=events,
        )
        result = await runner.run("test", "slow", cancelled=cancel)
        assert result["status"] == "cancelled" and result["resumable"]
        assert len(store.snapshot()["operations"]) == 2  # Cancelled read was never committed.
        bounded = AgentRunner(
            store=store,
            model=ScriptedModel(lambda *_: tool_call("mcp_fixture_slow")),
            tools=registry,
            policy=RunPolicy(timeout_seconds=0.15),
        )
        result = await bounded.run("timeout", "slow")
        assert result["errorCode"] == "TIMEOUT"


async def test_write_annotations_cannot_expand_local_allowlist():
    fixture = str(Path(__file__).with_name("mcp_fixture_server.py"))
    config = ServerConfig(name="fixture", command=sys.executable, args=[fixture], read_only_tools=["write"])
    with pytest.raises((AppError, ExceptionGroup)):
        async with connect_mcp(ToolRegistry(), [config]):
            pytest.fail("write tool admitted")


class _Text:
    type = "text"

    def __init__(self, text):
        self.text = text

    def model_dump(self, mode="json"):
        return {"type": "text", "text": self.text}


class _Result:
    isError = False

    def __init__(self, *, content=(), structured=None):
        self.content = list(content)
        self.structuredContent = structured


def test_nan_and_non_json_results_are_sanitized_and_bounded():
    payload = result_payload(_Result(structured={"score": float("nan"), "flags": [True, None]}))
    assert payload["source"] == "mcp"
    assert "外部 MCP 数据" in payload["notice"]  # the model reads data, not instructions
    assert payload["structuredContent"] == {"score": None, "flags": [True, None]}
    assert "NaN" not in json.dumps(payload)  # valid JSON, safe to put in a context

    with pytest.raises(AppError) as error:
        result_payload(_Result(content=[_Text("x" * 20000)]))
    assert error.value.code == "MCP_RESULT_LIMIT"


def test_pattern_schema_is_rejected_unless_explicitly_allowed():
    schema = {"type": "object", "properties": {"q": {"type": "string", "pattern": "^(a+)+$"}}}
    with pytest.raises(AppError) as error:
        check_schema(schema)
    assert error.value.code == "MCP_SCHEMA_UNSUPPORTED"
    check_schema(schema, allow_patterns=True)  # trusted self-run servers can opt in

    with pytest.raises(AppError):
        check_schema({"type": "object", "patternProperties": {"^x": {"type": "string"}}})
    config = ServerConfig(name="trusted", command="python", read_only_tools=["lookup"], allow_patterns=True)
    assert config.allow_patterns is True


def test_schema_size_and_depth_are_bounded():
    nested = {"type": "object"}
    current = nested
    for _ in range(20):
        current["properties"] = {"x": {"type": "object"}}
        current = current["properties"]["x"]
    with pytest.raises(AppError) as error:
        check_schema(nested)
    assert error.value.code == "MCP_SCHEMA"


async def test_a_pattern_schema_never_reaches_the_matcher(store):
    registry = ToolRegistry()
    executed = []
    registry.register(
        ToolDefinition(
            name="mcp_local_pattern",
            description="catastrophic regex fixture",
            schema={"type": "object", "properties": {"q": {"type": "string", "pattern": "^(a+)+$"}}},
            effect="read",
            async_execute=lambda args: executed.append(args) or {},
        )
    )
    store.ensure_session("test")
    started = time.monotonic()
    result = await registry.aexecute(
        "mcp_local_pattern",
        {"q": "a" * 26 + "!"},
        store=store,
        project_id="test",
        operation_key="pattern",
    )
    assert time.monotonic() - started < 1.0
    assert result["error"]["code"] == "MCP_SCHEMA_UNSUPPORTED"
    assert executed == []  # the validator never ran, so the event loop never blocked


async def test_unresolvable_ref_and_oversized_arguments_are_tool_errors(store):
    registry = ToolRegistry()
    registry.register(
        ToolDefinition(
            name="mcp_local_broken",
            description="broken ref fixture",
            schema={"type": "object", "$ref": "#/$defs/missing"},
            effect="read",
            async_execute=lambda args: pytest.fail("the tool must not run"),
        )
    )
    registry.register(
        ToolDefinition(
            name="mcp_local_echo",
            description="echo fixture",
            schema={"type": "object", "properties": {"blob": {"type": "string"}}},
            effect="read",
            async_execute=lambda args: {"ok": True},
        )
    )
    store.ensure_session("test")
    broken = await registry.aexecute(
        "mcp_local_broken", {}, store=store, project_id="test", operation_key="broken"
    )
    assert broken["error"]["code"] == "MCP_SCHEMA"
    oversized = await registry.aexecute(
        "mcp_local_echo",
        {"blob": "x" * 40000},
        store=store,
        project_id="test",
        operation_key="oversized",
    )
    assert oversized["error"]["code"] == "INVALID_ARGUMENTS"


def test_remote_schema_refs_and_bad_configs_are_rejected(tmp_path):
    with pytest.raises(AppError, match="外部地址"):
        check_schema({"properties": {"x": {"$ref": "https://untrusted.invalid/schema"}}})
    path = tmp_path / "mcp.json"
    path.write_text('{"servers": [{"name": "x", "command": "x", "read_only_tools": []}]}')
    with pytest.raises(AppError):
        server_configs(path, False)
