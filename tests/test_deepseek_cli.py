import json
import os
import subprocess
import sys

import httpx
import pytest
from langchain_core.messages import HumanMessage, ToolMessage

from vagent.config import load_config
from vagent.errors import AppError
from vagent.models import DeepSeekModel
from vagent.tools import create_project_tools


async def test_deepseek_http_endpoint_schema_thinking_and_tool_pairing():
    bodies = []

    def handler(request):
        assert str(request.url) == "https://api.deepseek.com/chat/completions"
        assert request.headers["authorization"] == "Bearer test-placeholder"
        bodies.append(json.loads(request.content))
        message = (
            {"role": "assistant", "content": "已读取项目"}
            if len(bodies) > 1
            else {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "read-1",
                        "type": "function",
                        "function": {"name": "project_read", "arguments": "{}"},
                    }
                ],
            }
        )
        return httpx.Response(
            200,
            json={
                "id": "test",
                "object": "chat.completion",
                "created": 1,
                "model": "deepseek-flash",
                "choices": [
                    {
                        "index": 0,
                        "finish_reason": "stop" if len(bodies) > 1 else "tool_calls",
                        "message": message,
                    }
                ],
                "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        model = DeepSeekModel("test-placeholder", http_client=client)
        messages = [HumanMessage(content="读取项目")]
        first = await model.generate(messages, create_project_tools().specs())
        assert first.tool_calls[0]["name"] == "project_read"
        last = await model.generate(
            [*messages, first, ToolMessage(content='{"ok":true}', tool_call_id="read-1")],
            create_project_tools().specs(),
        )
    assert last.content == "已读取项目"
    assert first.usage_metadata["input_tokens"] == 10
    assert bodies[0]["thinking"] == {"type": "disabled"}
    assert bodies[0]["tools"][0]["function"]["parameters"]["additionalProperties"] is False
    assert bodies[1]["messages"][-1]["tool_call_id"] == "read-1"


async def test_provider_errors_are_not_automatically_retried():
    attempts = 0

    def handler(request):
        nonlocal attempts
        attempts += 1
        return httpx.Response(429, json={"error": {"message": "limited", "type": "rate_limit"}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        model = DeepSeekModel("test-placeholder", http_client=client)
        with pytest.raises(Exception):
            await model.generate([HumanMessage(content="test")], create_project_tools().specs())
    assert attempts == 1


def test_key_required_and_config_precedence():
    with pytest.raises(AppError, match="DEEPSEEK_API_KEY"):
        DeepSeekModel("")
    config = load_config({"VAGENT_DEEPSEEK_KEY": "primary-secret", "DEEPSEEK_API_KEY": "fallback-secret"})
    assert config.api_key == "primary-secret" and "primary-secret" not in repr(config)


def test_cli_config_redacts_keys_and_missing_key_fails(tmp_path):
    env = {k: v for k, v in os.environ.items() if k not in {"DEEPSEEK_API_KEY", "VAGENT_DEEPSEEK_KEY"}}
    env.update(PYTHONIOENCODING="utf-8", VAGENT_HOME=str(tmp_path / "state"))
    result = subprocess.run(
        [sys.executable, "-m", "vagent", "config", "show"],
        cwd=tmp_path,
        env={**env, "DEEPSEEK_API_KEY": "test-secret"},
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    assert result.returncode == 0 and "test-secret" not in result.stdout + result.stderr
    assert json.loads(result.stdout)["api_key_configured"] is True
    missing = subprocess.run(
        [sys.executable, "-m", "vagent", "run", "test"],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    assert missing.returncode == 1 and "MISSING_KEY" in missing.stderr
