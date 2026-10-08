import asyncio
import json

import httpx
import pytest
from langchain_core.messages import AIMessage, HumanMessage

from vagent.application import ApplicationService
from vagent.cli import EventDisplay
from vagent.config import Config
from vagent.models import DeepSeekModel
from vagent.runner import AgentRunner, RunPolicy
from vagent.tools import create_project_tools
from vagent.usage import extract_usage, summarize_usage
from vagent.web import create_app

USAGE = {
    "prompt_tokens": 20,
    "completion_tokens": 4,
    "total_tokens": 24,
    "prompt_cache_hit_tokens": 12,
    "prompt_cache_miss_tokens": 8,
}


def packet(delta=None, *, reason=None, usage=None):
    data = {
        "id": "stream-test",
        "object": "chat.completion.chunk",
        "created": 1,
        "model": "deepseek-flash",
        "choices": [] if delta is None else [{"index": 0, "delta": delta, "finish_reason": reason}],
        "usage": usage,
    }
    return f"data: {json.dumps(data, ensure_ascii=False)}\n\n".encode()


def text_frames(text="完成", *, usage=USAGE, reason="stop"):
    return [
        packet({"role": "assistant", "content": text}),
        packet({}, reason=reason),
        packet(usage=usage),
        b"data: [DONE]\n\n",
    ]


def tool_frames(arguments, *, reason="tool_calls"):
    middle = len(arguments) // 2
    return [
        packet({"role": "assistant", "content": ""}),
        packet(
            {
                "tool_calls": [
                    {
                        "index": 0,
                        "id": "save-1",
                        "type": "function",
                        "function": {
                            "name": "artifact_save",
                            "arguments": arguments[:middle],
                        },
                    }
                ]
            }
        ),
        packet({"tool_calls": [{"index": 0, "function": {"arguments": arguments[middle:]}}]}),
        packet({}, reason=reason),
        packet(usage=USAGE),
        b"data: [DONE]\n\n",
    ]


def response(frames):
    return httpx.Response(200, content=b"".join(frames), headers={"Content-Type": "text/event-stream"})


class HeldStream(httpx.AsyncByteStream):
    def __init__(self, before, after):
        self.before, self.after = before, after
        self.entered, self.release = asyncio.Event(), asyncio.Event()
        self.closed = False

    async def __aiter__(self):
        for frame in self.before:
            yield frame
        self.entered.set()
        await self.release.wait()
        for frame in self.after:
            yield frame

    async def aclose(self):
        self.closed = True


async def test_deepseek_stream_text_and_usage_are_preserved_once():
    requests, deltas = [], []

    def handler(request):
        requests.append(json.loads(request.content))
        return response(
            [
                packet({"role": "assistant", "reasoning_content": "private-reasoning", "content": ""}),
                packet({"content": "你好"}),
                packet({"content": "，世界"}),
                packet({}, reason="stop"),
                packet(usage=USAGE),
                b"data: [DONE]\n\n",
            ]
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        reply = await DeepSeekModel("test", http_client=client).generate_stream(
            [HumanMessage(content="hello")], create_project_tools().specs(), deltas.append
        )
    assert isinstance(reply, AIMessage) and reply.content == "你好，世界"
    assert deltas == ["你好", "，世界"]
    assert len(requests) == 1
    assert requests[0]["stream"] is True and requests[0]["stream_options"] == {"include_usage": True}
    assert requests[0]["thinking"] == {"type": "disabled"}
    assert extract_usage(reply) == {
        "inputTokens": 20,
        "outputTokens": 4,
        "cacheHitTokens": 12,
        "cacheMissTokens": 8,
        "cacheUsageSource": "reported",
    }


async def test_stream_tools_wait_for_finish_then_use_complete_arguments_and_results(store):
    arguments = json.dumps({"kind": "brief", "title": "test", "content": "已保存内容"}, ensure_ascii=False)
    frames = tool_frames(arguments)
    held = HeldStream(frames[:3], frames[3:])
    requests = []

    def handler(request):
        requests.append(json.loads(request.content))
        if len(requests) == 1:
            return httpx.Response(200, stream=held, headers={"Content-Type": "text/event-stream"})
        result = requests[-1]["messages"][-1]
        assert result["tool_call_id"] == "save-1"
        assert json.loads(result["content"])["data"]["persisted"]
        return response(text_frames())

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        runner = AgentRunner(
            store=store, model=DeepSeekModel("test", http_client=client), tools=create_project_tools()
        )
        pending = asyncio.create_task(runner.run("test", "save"))
        await asyncio.wait_for(held.entered.wait(), 3)
        assert not store.snapshot()["artifacts"]  # Even complete JSON needs the finish marker.
        held.release.set()
        result = await pending
    assert result["status"] == "completed" and result["toolCalls"] == 1
    assert len(store.snapshot()["artifacts"]) == 1 and held.closed
    usage = summarize_usage(result)
    assert usage["observedInputTokens"] == 40 and usage["observedOutputTokens"] == 8
    assert usage["cacheHitTokens"] == 24 and usage["cacheUsageComplete"]


@pytest.mark.parametrize(
    "reason,arguments,code",
    [
        ("tool_calls", '{"kind":"brief","title":"test","content":"partial"', "INVALID_TOOL_CALL"),
        ("tool_calls", "[]", "INVALID_TOOL_CALL"),
        ("length", '{"kind":"brief","title":"test","content":"partial"}', "RESPONSE_TRUNCATED"),
    ],
)
async def test_stream_rejects_incomplete_or_truncated_tool_response(store, reason, arguments, code):
    requests = []

    def handler(request):
        requests.append(request)
        return response(tool_frames(arguments, reason=reason))

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await AgentRunner(
            store=store, model=DeepSeekModel("test", http_client=client), tools=create_project_tools()
        ).run("test", "save")
    assert result["errorCode"] == code and result["status"] == "failed"
    assert not store.snapshot()["artifacts"] and result["toolCalls"] == 0
    assert result["modelCalls"][0]["inputTokens"] == 20  # Rejected response is still billed.
    assert len(requests) == 1 and not result["resumable"]


async def test_interrupted_stream_discards_draft_and_only_explicit_resume_calls_model(store):
    requests, events = [], []

    def handler(request):
        requests.append(json.loads(request.content))
        if len(requests) == 1:
            return response([packet({"role": "assistant", "content": "draft-not-a-final-answer"})])
        assert "draft-not-a-final-answer" not in json.dumps(requests[-1])
        return response(text_frames("恢复完成"))

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        runner = AgentRunner(
            store=store,
            model=DeepSeekModel("test", http_client=client),
            tools=create_project_tools(),
            on_event=events.append,
        )
        first = await runner.run("test", "hello", request_id="same")
        assert first["errorCode"] == "STREAM_INTERRUPTED" and first["resumable"]
        assert "draft-not-a-final-answer" not in json.dumps(store.snapshot())
        assert len(requests) == 1 and first["modelCalls"][0]["inputTokens"] is None
        assert any(event["type"] == "assistant.discarded" for event in events)
        assert await runner.run("test", "hello", request_id="same") == first
        final = await runner.resume(first["id"])
    assert final["status"] == "completed" and final["answer"] == "恢复完成"
    assert final["modelSteps"] == 2 and len(requests) == 2
    assert not summarize_usage(final)["tokenUsageComplete"]


async def test_stream_failure_diagnostics_never_echo_provider_errors(store):
    events, requests = [], []

    def handler(request):
        requests.append(request)
        return httpx.Response(429, json={"error": {"message": "test-secret-provider-body"}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await AgentRunner(
            store=store,
            model=DeepSeekModel("test", http_client=client),
            tools=create_project_tools(),
            on_event=events.append,
        ).run("test", "hello")
    failed = next(event for event in events if event["type"] == "model.failed")
    assert failed["httpStatus"] == 429 and failed["errorType"].endswith("RateLimitError")
    assert result["errorCode"] == "RATE_LIMIT" and len(requests) == 1
    assert "test-secret-provider-body" not in json.dumps([result, events])


async def test_cancelled_http_stream_closes_connection_and_preserves_saved_artifact(store):
    held = HeldStream([packet({"role": "assistant", "content": "unsaved-stream-draft"})], text_frames())
    requests = []

    def handler(request):
        requests.append(json.loads(request.content))
        if len(requests) == 1:
            return response(tool_frames('{"kind":"brief","title":"test","content":"keep"}'))
        if len(requests) == 2:
            return httpx.Response(200, stream=held, headers={"Content-Type": "text/event-stream"})
        assert "unsaved-stream-draft" not in json.dumps(requests[-1])
        return response(text_frames("完成"))

    cancelled = asyncio.Event()
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        runner = AgentRunner(
            store=store,
            model=DeepSeekModel("test", http_client=client),
            tools=create_project_tools(),
            policy=RunPolicy(max_steps=3, timeout_seconds=20),
        )
        task = asyncio.create_task(runner.run("test", "save", cancelled=cancelled))
        await asyncio.wait_for(held.entered.wait(), 3)
        cancelled.set()
        first = await task
        assert first["status"] == "cancelled" and held.closed
        assert "unsaved-stream-draft" not in json.dumps(store.snapshot())
        assert first["toolCalls"] == 1 and len(store.snapshot()["artifacts"]) == 1
        final = await runner.resume(first["id"])
    assert final["status"] == "completed" and final["modelSteps"] == 3
    assert len(store.snapshot()["artifacts"]) == 1 and len(requests) == 3


@pytest.mark.parametrize(
    "usage", [None, {}, {"prompt_tokens": 20, "completion_tokens": 4, "total_tokens": 24}]
)
async def test_stream_missing_usage_remains_unknown(usage):
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: response(text_frames(usage=usage)))
    ) as client:
        reply = await DeepSeekModel("test", http_client=client).generate_stream(
            [HumanMessage(content="hello")], create_project_tools().specs(), lambda _: None
        )
    observed = extract_usage(reply)
    assert observed["inputTokens"] == (20 if usage else None)
    assert observed["cacheHitTokens"] is None and observed["cacheUsageSource"] == "missing"


class BurstModel:
    name = "stream-fixture"

    def __init__(self, count=1):
        self.count = count
        self.entered, self.release = asyncio.Event(), asyncio.Event()

    async def generate_stream(self, messages, tools, delta):
        for _ in range(self.count):
            delta("transient-draft ")
        self.entered.set()
        await self.release.wait()
        return AIMessage(content="transient-draft " * self.count)


async def test_web_draft_overflow_reconnect_and_no_per_token_disk_writes(tmp_path, monkeypatch):
    model = BurstModel(150)
    config = Config(home=tmp_path / "stream", api_key=None)
    async with ApplicationService.open(config, model=model) as service:
        calls = []
        original = service.store.transaction

        def tracked(callback):
            calls.append(1)
            return original(callback)

        monkeypatch.setattr(service.store, "transaction", tracked)
        queue = service.subscribe("test")
        other = service.subscribe("other")
        await service.start("test", "hello", "id")
        await asyncio.wait_for(model.entered.wait(), 3)
        view = service.session("test")
        assert view["draft"]["sequence"] == 150 and view["draft"]["text"] == "transient-draft " * 150
        assert len(calls) < 20 and other.empty()
        assert "transient-draft" not in json.dumps(service.store.snapshot())
        queued = [queue.get_nowait() for _ in range(queue.qsize())]
        assert any(event["type"] == "snapshot" for event in queued)
        app = create_app(config)
        app.state.service = service

        class Request:
            async def is_disconnected(self):
                return False

        endpoint = next(route.endpoint for route in app.routes if route.path == "/api/events")
        sse = (await endpoint(sessionId="test", request=Request())).body_iterator
        first = await anext(sse)
        assert first.startswith("event: snapshot\n")
        assert json.loads(first.split("data: ")[1])["draft"] == view["draft"]
        model.release.set()
        await service.task
        assert service.session("test")["draft"] is None
        assert len(service.session("test")["messages"]) == 2
        assert all(event["type"] != "assistant.delta" for event in service.session("test")["run"]["events"])
        final = await anext(sse)
        assert json.loads(final.split("data: ")[1])["draft"] is None
        await sse.aclose()
        assert len(service.subscribers) == 2  # SSE subscription was removed on disconnect.


async def test_sse_emits_deltas_separately_from_snapshots(tmp_path):
    config = Config(home=tmp_path / "sse", api_key=None)
    async with ApplicationService.open(config, model=BurstModel()) as service:
        session = service.create_session()["id"]
        app = create_app(config)
        app.state.service = service

        class Request:
            async def is_disconnected(self):
                return False

        endpoint = next(route.endpoint for route in app.routes if route.path == "/api/events")
        iterator = (await endpoint(sessionId=session, request=Request())).body_iterator
        await anext(iterator)
        await service.start(session, "hello", "test")
        await asyncio.wait_for(service.model.entered.wait(), 3)
        frames = []
        while not any(frame.startswith("event: assistant.delta") for frame in frames):
            frames.append(await asyncio.wait_for(anext(iterator), 3))
        delta = json.loads(frames[-1].split("data: ")[1])
        assert delta["sequence"] == 1 and delta["text"] == "transient-draft "
        service.stop(service.active_run_id)
        await service.task
        assert service.session(session)["draft"] is None
        assert "transient-draft" not in json.dumps(service.store.snapshot())
        await iterator.aclose()
        assert not service.subscribers


def test_cli_streams_once_and_labels_discarded_drafts(capsys):
    display = EventDisplay()
    display({"type": "assistant.delta", "text": "流式"})
    assert "流式" in capsys.readouterr().out  # Text is visible before completion.
    display({"type": "assistant.delta", "text": "回复"})
    display({"type": "model.completed", "final": True})
    display.finish({"answer": "流式回复", "id": "x", "status": "completed", "modelSteps": 1, "toolCalls": 0})
    output = capsys.readouterr().out
    assert "流式回复" not in output and "[completed]" in output
    interrupted = EventDisplay()
    interrupted({"type": "assistant.delta", "text": "未完草稿"})
    interrupted({"type": "assistant.discarded"})
    interrupted.finish(
        {"answer": "已停止", "id": "y", "status": "cancelled", "modelSteps": 1, "toolCalls": 0}
    )
    output = capsys.readouterr().out
    assert "草稿未完成，已丢弃" in output and "已停止" in output
