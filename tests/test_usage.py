import asyncio
import json
import os
import subprocess
import sys

import httpx
import pytest
from conftest import ScriptedModel, tool_call
from langchain_core.messages import AIMessage

from vagent.models import DeepSeekModel, DemoModel
from vagent.runner import AgentRunner
from vagent.storage import FileStore
from vagent.tools import create_project_tools
from vagent.usage import extract_usage, summarize_usage


def completion(usage, *, tool=False):
    message = {"role": "assistant", "content": "done"}
    if tool:
        message = {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {"id": "read", "type": "function", "function": {"name": "project_read", "arguments": "{}"}}
            ],
        }
    return {
        "id": "test",
        "object": "chat.completion",
        "created": 1,
        "model": "deepseek-flash",
        "choices": [{"index": 0, "finish_reason": "tool_calls" if tool else "stop", "message": message}],
        "usage": usage,
    }


def reply(input_tokens=100, output_tokens=5, hit=60):
    return AIMessage(
        content="done",
        usage_metadata={
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "total_tokens": input_tokens + output_tokens,
            "input_token_details": {"cache_read": hit},
        },
    )


def runner(store, callback, **kwargs):
    return AgentRunner(store=store, model=ScriptedModel(callback), tools=create_project_tools(), **kwargs)


async def test_deepseek_cache_fields_persist_and_weighted_hit_rate_survives_reopen(store):
    requests = []
    usages = [
        {
            "prompt_tokens": 100,
            "completion_tokens": 5,
            "total_tokens": 105,
            "prompt_cache_hit_tokens": 90,
            "prompt_cache_miss_tokens": 10,
        },
        {
            "prompt_tokens": 10,
            "completion_tokens": 7,
            "total_tokens": 17,
            "prompt_cache_hit_tokens": 0,
            "prompt_cache_miss_tokens": 10,
        },
    ]

    def handler(request):
        requests.append(request)
        return httpx.Response(200, json=completion(usages[len(requests) - 1], tool=len(requests) == 1))

    events = []
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await AgentRunner(
            store=store,
            model=DeepSeekModel("test-placeholder", http_client=client),
            stream_output=False,
            tools=create_project_tools(),
            on_event=events.append,
        ).run("coffee", "test")
    assert result["status"] == "completed" and len(requests) == 2
    summary = summarize_usage(result)
    assert summary["modelCallCount"] == 2
    assert summary["observedInputTokens"] == 110 and summary["observedOutputTokens"] == 12
    assert summary["cacheHitTokens"] == 90 and summary["cacheMissTokens"] == 20
    assert summary["cacheHitRate"] == pytest.approx(90 / 110)
    assert summary["cacheUsageComplete"] and summary["tokenUsageComplete"]
    assert [call["cacheUsageSource"] for call in result["modelCalls"]] == ["reported", "reported"]
    assert all(call["durationSeconds"] >= 0 for call in result["modelCalls"])
    assert len([event for event in events if event["type"] == "model.usage"]) == 2
    home = store.home
    store.close()
    with FileStore.open(home) as reopened:
        assert summarize_usage(reopened.snapshot()["runs"][result["id"]]) == summary


@pytest.mark.parametrize(
    "usage,has_tokens",
    [
        ({"prompt_tokens": 20, "completion_tokens": 3, "total_tokens": 23}, True),
        ({}, False),
        (None, False),
    ],
)
async def test_absent_http_usage_is_unknown_not_a_zero_cache_hit(store, usage, has_tokens):
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json=completion(usage)))
    ) as client:
        result = await AgentRunner(
            store=store,
            model=DeepSeekModel("test-placeholder", http_client=client),
            stream_output=False,
            tools=create_project_tools(),
        ).run("coffee", "test")
    call = result["modelCalls"][0]
    assert call["cacheHitTokens"] is None and call["cacheMissTokens"] is None
    assert (call["inputTokens"] is not None) == has_tokens
    summary = summarize_usage(result)
    assert summary["cacheHitRate"] is None and not summary["cacheUsageComplete"]
    assert summary["tokenUsageComplete"] == has_tokens


def test_normalized_cache_read_derives_misses_and_ignores_cache_creation():
    message = reply()
    message.usage_metadata["input_token_details"]["cache_creation"] = 999
    usage = extract_usage(message)
    assert usage["cacheHitTokens"] == 60 and usage["cacheMissTokens"] == 40
    assert usage["cacheUsageSource"] == "derived"
    assert extract_usage(reply(hit=0))["cacheMissTokens"] == 100


@pytest.mark.parametrize("hit,miss", [(101, 0), (20, 20), (-1, 101), (True, 99), ("50", 50)])
def test_invalid_cache_counts_are_not_reported_as_valid_savings(hit, miss):
    message = AIMessage(
        content="done",
        response_metadata={
            "token_usage": {
                "prompt_tokens": 100,
                "completion_tokens": 2,
                "prompt_cache_hit_tokens": hit,
                "prompt_cache_miss_tokens": miss,
            }
        },
    )
    usage = extract_usage(message)
    assert usage["inputTokens"] == 100
    assert usage["cacheUsageSource"] == "invalid"
    assert usage["cacheHitTokens"] is None and usage["cacheMissTokens"] is None


def test_partial_cache_data_and_explicit_zero_are_preserved():
    message = AIMessage(content="done", response_metadata={"token_usage": {"prompt_cache_hit_tokens": 80}})
    usage = extract_usage(message)
    assert usage["cacheHitTokens"] == 80 and usage["cacheMissTokens"] is None
    run = {"modelSteps": 1, "usageStartStep": 1, "inputTokens": 0, "outputTokens": 0, "modelCalls": [usage]}
    summary = summarize_usage(run)
    assert summary["cacheHitTokens"] == 80 and summary["cacheHitRate"] is None
    run["modelCalls"] = [extract_usage(reply(input_tokens=0, output_tokens=0, hit=0))]
    summary = summarize_usage(run)
    assert summary["cacheHitTokens"] == summary["cacheMissTokens"] == 0
    assert summary["cacheHitRate"] is None and summary["cacheUsageComplete"]


async def test_rejected_tool_response_still_records_reported_usage(store):
    message = reply()
    message.tool_calls = [{"id": None, "name": "project_read", "args": {}}]
    result = await runner(store, lambda *_: message).run("coffee", "test")
    assert result["errorCode"] == "INVALID_TOOL_CALL" and result["toolCalls"] == 0
    assert result["inputTokens"] == 100
    assert result["modelCalls"][0]["status"] == "responded"
    assert summarize_usage(result)["cacheHitTokens"] == 60


async def test_failure_and_resume_add_one_call_without_recounting_cached_tokens(store):
    def respond(messages, step):
        if step == 0:
            message = tool_call("project_read")
            message.usage_metadata = reply().usage_metadata
            return message
        raise RuntimeError("Authorization: secret-token")

    first = await runner(store, respond).run("coffee", "test", request_id="stable")
    assert first["modelCalls"][1]["status"] == "failed"
    assert first["modelCalls"][1]["inputTokens"] is None
    assert "secret-token" not in json.dumps(first)
    resumed = runner(store, lambda *_: reply(input_tokens=50, hit=10))
    final = await resumed.resume(first["id"])
    summary = summarize_usage(final)
    assert summary["modelCallCount"] == 3 and summary["callsWithCacheUsage"] == 2
    assert summary["cacheHitTokens"] == 70 and summary["cacheMissTokens"] == 80
    assert summary["observedInputTokens"] == 150
    assert summary["cacheHitRate"] == pytest.approx(70 / 150)
    assert not summary["cacheUsageComplete"] and not summary["tokenUsageComplete"]
    assert await resumed.resume(first["id"]) == final
    assert await resumed.run("coffee", "test", request_id="stable") == final
    assert resumed.model.calls == 1


async def test_stop_before_dispatch_does_not_count_a_model_call(store):
    cancelled = asyncio.Event()

    def stop(event):
        if event["type"] == "model.started":
            cancelled.set()

    result = await runner(store, lambda *_: reply(), on_event=stop).run("coffee", "test", cancelled=cancelled)
    assert result["modelSteps"] == 1 and result["status"] == "cancelled"
    assert summarize_usage(result)["modelCallCount"] == 0


async def test_cancelled_request_has_unknown_usage_and_survives_resume(store):
    started = asyncio.Event()

    async def wait(*_):
        started.set()
        await asyncio.Event().wait()

    pending = asyncio.create_task(runner(store, wait).run("coffee", "test"))
    await asyncio.wait_for(started.wait(), 2)
    pending.cancel()
    first = await pending
    assert first["modelCalls"][0]["status"] == "cancelled"
    assert first["modelCalls"][0]["cacheHitTokens"] is None
    final = await runner(store, lambda *_: reply()).resume(first["id"])
    summary = summarize_usage(final)
    assert summary["modelCallCount"] == 2 and summary["callsWithTokenUsage"] == 1
    assert summary["cacheHitTokens"] == 60 and not summary["cacheUsageComplete"]


async def test_legacy_run_can_resume_without_inventing_old_cache_statistics(store):
    async def fail(*_):
        raise RuntimeError("failed")

    first = await runner(store, fail).run("coffee", "test")

    def remove_new_fields(draft):
        old = draft["runs"][first["id"]]
        old.pop("modelCalls")
        old.pop("usageStartStep")
        old.update(inputTokens=25, outputTokens=5)

    store.transaction(remove_new_fields)
    old = summarize_usage(store.snapshot()["runs"][first["id"]])
    assert old["modelCallCount"] is None and old["cacheHitTokens"] is None
    final = await runner(store, lambda *_: reply()).resume(first["id"])
    summary = summarize_usage(final)
    assert summary["untrackedModelSteps"] == 1 and summary["recordedCallCount"] == 1
    assert summary["modelCallCount"] is None and not summary["tokenUsageComplete"]
    assert summary["observedInputTokens"] == 125 and summary["cacheHitTokens"] == 60


async def test_usage_cli_needs_no_key_and_excludes_conversation_content(store, tmp_path):
    result = await runner(store, lambda *_: reply()).run("coffee", "private-user-prompt")
    home = store.home
    store.close()
    env = {k: v for k, v in os.environ.items() if k not in {"DEEPSEEK_API_KEY", "VAGENT_DEEPSEEK_KEY"}}
    env.update(VAGENT_HOME=str(home), PYTHONIOENCODING="utf-8")
    for args in (["--run", result["id"]], ["--session", "coffee"]):
        process = subprocess.run(
            [sys.executable, "-m", "vagent", "usage", *args],
            cwd=tmp_path,
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=20,
        )
        assert process.returncode == 0, process.stdout + process.stderr
        assert "private-user-prompt" not in process.stdout
        report = json.loads(process.stdout)["runs"][0]
        assert report["usage"]["cacheHitTokens"] == 60 and len(report["modelCalls"]) == 1


async def test_demo_does_not_invent_token_usage(store):
    result = await AgentRunner(store=store, model=DemoModel(), tools=create_project_tools()).run(
        "demo", "test"
    )
    summary = summarize_usage(result)
    assert summary["modelCallCount"] == 3 and summary["callsWithTokenUsage"] == 0
    assert summary["cacheHitTokens"] is None and not summary["tokenUsageComplete"]
