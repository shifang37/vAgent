import asyncio

import httpx
import pytest

from vagent.http import ModelHttpClient
from vagent.models import DeepSeekModel
from vagent.runner import AgentRunner, RunPolicy
from vagent.tools import create_project_tools

URL = "https://api.deepseek.com/chat/completions"


@pytest.mark.parametrize("error_type", [httpx.ConnectError, httpx.ConnectTimeout])
async def test_connection_retries_send_one_model_request_and_keep_runner_budget(store, error_type):
    attempts = 0

    def handler(request):
        nonlocal attempts
        attempts += 1
        if attempts <= 2:
            raise error_type("connection not established", request=request)
        return httpx.Response(
            200,
            json={
                "id": "response",
                "object": "chat.completion",
                "created": 1,
                "model": "deepseek-flash",
                "choices": [
                    {"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": "完成"}}
                ],
                "usage": {"prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 12},
            },
        )

    async with ModelHttpClient(transport=httpx.MockTransport(handler)) as client:
        result = await AgentRunner(
            store=store,
            model=DeepSeekModel("test-placeholder", http_client=client),
            tools=create_project_tools(),
            policy=RunPolicy(max_steps=1),
            stream_output=False,
        ).run("coffee", "直接回答")
        assert client.connection_retries == 2 and attempts == 3
    assert result["status"] == "completed" and result["modelSteps"] == 1
    assert len(result["modelCalls"]) == 1 and result["inputTokens"] == 10


async def test_connection_retry_limit_and_explicit_validation_opt_out():
    attempts = 0

    def handler(request):
        nonlocal attempts
        attempts += 1
        raise httpx.ConnectError("connection failed", request=request)

    async with ModelHttpClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(httpx.ConnectError):
            await client.post(URL, json={})
        assert attempts == 3 and client.connection_retries == 2
        with pytest.raises(httpx.ConnectError):
            await client.post(URL, json={}, extensions={"vagent_connect_retries": False})
        assert attempts == 4 and client.connection_retries == 2


@pytest.mark.parametrize(
    "error_type",
    [httpx.ReadError, httpx.ReadTimeout, httpx.WriteError, httpx.WriteTimeout, httpx.RemoteProtocolError],
)
async def test_no_retry_after_request_may_have_been_sent(error_type):
    attempts = 0

    def handler(request):
        nonlocal attempts
        attempts += 1
        raise error_type("request outcome unknown", request=request)

    async with ModelHttpClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(error_type):
            await client.post(URL, json={})
        assert attempts == 1 and client.connection_retries == 0


@pytest.mark.parametrize("status", [401, 429, 503])
async def test_error_responses_are_not_retried(status):
    attempts = 0

    def handler(request):
        nonlocal attempts
        attempts += 1
        return httpx.Response(status)

    async with ModelHttpClient(transport=httpx.MockTransport(handler)) as client:
        response = await client.post(URL, json={})
        assert response.status_code == status and attempts == 1 and client.connection_retries == 0


async def test_cancellation_during_backoff_stops_further_connections():
    attempted = asyncio.Event()
    attempts = 0

    def handler(request):
        nonlocal attempts
        attempts += 1
        attempted.set()
        raise httpx.ConnectError("connection failed", request=request)

    async with ModelHttpClient(transport=httpx.MockTransport(handler)) as client:
        task = asyncio.create_task(client.post(URL, json={}))
        await attempted.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert attempts == 1 and client.connection_retries == 0


async def test_redirected_requests_are_not_replayed_after_connect_failure():
    attempts = 0

    def handler(request):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return httpx.Response(307, headers={"Location": URL + "?redirected=1"})
        raise httpx.ConnectError("later connection failed", request=request)

    async with ModelHttpClient(transport=httpx.MockTransport(handler), follow_redirects=True) as client:
        with pytest.raises(httpx.ConnectError):
            await client.post(URL, json={})
        assert attempts == 2 and client.connection_retries == 0
