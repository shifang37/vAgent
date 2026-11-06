import asyncio
import json
from collections.abc import Callable
from contextlib import aclosing
from typing import Protocol

import httpx
from langchain_core.messages import (
    AIMessage,
    AIMessageChunk,
    BaseMessage,
    HumanMessage,
    ToolMessage,
    message_chunk_to_message,
)
from langchain_core.messages.ai import add_ai_message_chunks
from langchain_deepseek import ChatDeepSeek

from vagent.config import require_key
from vagent.errors import AppError
from vagent.usage import extract_usage


class _UsageChatDeepSeek(ChatDeepSeek):
    def _convert_chunk_to_generation_chunk(self, chunk, default_chunk_class, base_generation_info):
        result = super()._convert_chunk_to_generation_chunk(chunk, default_chunk_class, base_generation_info)
        # Keep raw cache hit AND miss counts: normalized metadata drops misses and
        # may synthesize zeros for missing fields. This hook is covered by HTTP tests.
        if result is not None and isinstance(chunk.get("usage"), dict):
            result.message.response_metadata["token_usage"] = chunk["usage"]
        return result


class StreamResponseError(AppError):
    def __init__(self, code, message, reply):
        super().__init__(code, message)
        self.usage = extract_usage(reply)


def is_transport_failure(error: BaseException) -> bool:
    """Whether a failed stream died on the wire rather than in the provider's answer.

    The OpenAI-compatible client wraps transport failures in its own
    ``APIConnectionError`` / ``APITimeoutError`` classes, so checking ``httpx`` alone
    would misclassify them as ``EXECUTION_ERROR`` and lose the recoverable/resumable
    classification. The original exception is still reachable through the cause chain.
    """
    seen: set[int] = set()
    current: BaseException | None = error
    for _ in range(8):
        if current is None or id(current) in seen:
            return False
        seen.add(id(current))
        # A provider answer with an HTTP status is an API error, not a broken wire.
        if getattr(current, "status_code", None) is not None:
            return False
        if isinstance(current, (httpx.TransportError, httpx.TimeoutException, TimeoutError)):
            return True
        if isinstance(current, OSError):
            return True
        if type(current).__name__ in {
            "APIConnectionError",
            "APITimeoutError",
            "ModelConnectionError",
            "ConnectError",
            "ReadError",
        }:
            return True
        current = current.__cause__ or current.__context__
    return False


def _last_finish_reason(chunks) -> str | None:
    """Gateways may repeat the final chunk; ``add_ai_message_chunks`` would join the
    strings into ``"stopstop"``. The last non-empty value is the actual reason."""
    reason = None
    for chunk in chunks:
        value = chunk.response_metadata.get("finish_reason")
        if isinstance(value, str) and value:
            reason = value
    return reason


def _merge_tool_call_chunks(chunks) -> list[dict]:
    """Merge per-index deltas, taking the last non-empty name/id instead of joining them."""
    slots: dict[object, dict] = {}
    for chunk in chunks:
        for part in chunk.tool_call_chunks:
            index = part.get("index")
            key = index if index is not None else part.get("id") or 0
            slot = slots.setdefault(key, {"name": None, "id": None, "args": ""})
            if part.get("name"):
                slot["name"] = part["name"]
            if part.get("id"):
                slot["id"] = part["id"]
            args = part.get("args")
            if isinstance(args, str):
                slot["args"] += args
    return list(slots.values())


def _decode_tool_calls(chunks) -> tuple[list[dict], list[dict]]:
    """Complete calls and malformed ones; a malformed call is fed back, never executed."""
    calls: list[dict] = []
    invalid: list[dict] = []
    for part in _merge_tool_call_chunks(chunks):
        name, call_id = part["name"], part["id"]
        try:
            args = json.loads(part["args"])
            if not isinstance(args, dict) or not name or not call_id:
                raise ValueError
        except (ValueError, TypeError):
            invalid.append(
                {
                    "name": name or "",
                    "args": part["args"][:200],
                    "id": call_id,
                    "error": "工具参数不是完整的 JSON 对象",
                }
            )
            continue
        calls.append({"name": name, "args": args, "id": call_id, "type": "tool_call"})
    return calls, invalid


class AgentModel(Protocol):
    name: str

    async def generate(self, messages: list[BaseMessage], tools: list[dict]) -> AIMessage: ...


class DeepSeekModel:
    def __init__(
        self,
        api_key: str | None,
        model: str = "deepseek-flash",
        *,
        http_client: httpx.AsyncClient | None = None,
    ):
        self.name = model
        # Recorded on the model.call span: this adapter is configured with zero
        # connection retries, so a retry count of 0 is a fact, not a missing value.
        self.max_retries = 0
        # Usage received before a cancelled stream, read by the runner's cancel path.
        self.last_stream_usage: dict | None = None
        self.cache_config = {
            "adapter": "deepseek-v1",
            "endpoint": "https://api.deepseek.com",
            "model": model,
            "max_tokens": 4096,
            "thinking": "disabled",
        }
        self._client = _UsageChatDeepSeek(
            api_key=require_key(api_key),
            model=model,
            api_base="https://api.deepseek.com",
            max_retries=0,
            timeout=60,
            max_tokens=4096,
            extra_body={"thinking": {"type": "disabled"}},
            http_async_client=http_client,
        )

    async def generate(self, messages: list[BaseMessage], tools: list[dict]) -> AIMessage:
        return await self._client.bind_tools(tools).ainvoke(messages)

    async def generate_stream(
        self, messages: list[BaseMessage], tools: list[dict], on_delta: Callable[[str], None]
    ) -> AIMessage:
        chunks = []
        raw_usage, usage_metadata = {}, None
        self.last_stream_usage = None

        def partial_reply() -> AIMessage:
            """The reply assembled from the deltas received so far (never persisted)."""
            if not chunks:
                reply = AIMessage(content="")
            else:
                combined = add_ai_message_chunks(chunks[0], *chunks[1:])
                reply = message_chunk_to_message(combined)
            reply.usage_metadata = usage_metadata
            reply.response_metadata["token_usage"] = raw_usage
            return reply

        try:
            async with aclosing(
                self._client.bind_tools(tools).astream(messages, stream_usage=True)
            ) as stream:
                async for chunk in stream:
                    if not isinstance(chunk, AIMessageChunk):
                        raise AppError("INVALID_RESPONSE", "模型返回了不支持的消息流。")
                    metadata = chunk.response_metadata.copy()
                    usage = metadata.pop("token_usage", None)
                    if usage is not None:
                        raw_usage, usage_metadata = usage, chunk.usage_metadata
                    # Usage is cumulative, not a per-chunk delta. Count it once.
                    chunks.append(
                        chunk.model_copy(update={"response_metadata": metadata, "usage_metadata": None})
                    )
                    if isinstance(chunk.content, str) and chunk.content:
                        on_delta(chunk.content)
        except (httpx.HTTPError, OSError) as error:
            # The transport died mid-stream: classify it as recoverable instead of the
            # generic EXECUTION_ERROR, and keep whatever usage the provider already sent
            # (it may bill the aborted call).
            raise StreamResponseError(
                "STREAM_INTERRUPTED", "回复流中断，草稿未保存，可在剩余预算内继续。", partial_reply()
            ) from error
        except AppError as error:
            # A local cancel/stop raised from ``on_delta``: still attach the received
            # usage so the journal bills the tokens the provider already generated.
            if getattr(error, "usage", None) is None:
                try:
                    error.usage = extract_usage(partial_reply())
                except Exception:  # pragma: no cover - extraction is total in practice
                    pass
            raise
        except Exception as error:
            # The OpenAI-compatible adapter re-wraps transport failures; recover the
            # classification instead of letting it become EXECUTION_ERROR.
            if not is_transport_failure(error):
                raise
            raise StreamResponseError(
                "STREAM_INTERRUPTED", "回复流中断，草稿未保存，可在剩余预算内继续。", partial_reply()
            ) from error
        except asyncio.CancelledError:
            # The task was cancelled while the stream was open. The caller sees no
            # exception object here, so publish the received usage on the adapter: the
            # Run journal still bills the tokens the provider already generated.
            self.last_stream_usage = extract_usage(partial_reply())
            raise
        if not chunks:
            raise StreamResponseError(
                "STREAM_INTERRUPTED", "回复流未完成，草稿未保存，可在剩余预算内继续。", partial_reply()
            )
        combined = add_ai_message_chunks(chunks[0], *chunks[1:])
        reply = message_chunk_to_message(combined)
        reply.usage_metadata = usage_metadata
        reply.response_metadata["token_usage"] = raw_usage
        reason = _last_finish_reason(chunks)
        reply.response_metadata["finish_reason"] = reason
        if not reason:
            raise StreamResponseError(
                "STREAM_INTERRUPTED", "回复流中断，草稿未保存，可在剩余预算内继续。", reply
            )
        if reason not in {"stop", "tool_calls"}:
            raise StreamResponseError(
                "RESPONSE_TRUNCATED", "模型回复被截断，本步未保存回复或执行工具。", reply
            )
        # LangChain deliberately repairs partial JSON while aggregating chunks.
        # Tool execution needs strictly complete JSON, so decode the merged raw
        # arguments again, with names/ids taken once from the repeated deltas.
        calls, invalid = _decode_tool_calls(chunks)
        if reason == "stop" and (calls or invalid):
            # A stop with tool calls is a provider contract violation; feed every call
            # back as malformed instead of executing it.
            invalid = [
                *invalid,
                *[
                    {
                        "name": call["name"],
                        "args": call["args"],
                        "id": call["id"],
                        "error": "stop 同时携带工具调用",
                    }
                    for call in calls
                ],
            ]
            calls = []
        reply.tool_calls = calls
        reply.invalid_tool_calls = invalid
        return reply


class DemoModel:
    """Deterministic local fixture, not a model or a measure of DeepSeek quality."""

    name = "offline-demo (模拟模型)"

    async def generate(self, messages: list[BaseMessage], tools: list[dict]) -> AIMessage:
        start = max(i for i, message in enumerate(messages) if isinstance(message, HumanMessage))
        results = [message for message in messages[start:] if isinstance(message, ToolMessage)]
        available = {tool["function"]["name"] for tool in tools}
        sequence = (["skill_read"] if "skill_read" in available else []) + ["project_read", "artifact_save"]
        if len(results) < len(sequence):
            name = sequence[len(results)]
            args = {"name": "video-brief"} if name == "skill_read" else {}
            if name == "artifact_save":
                args = {
                    "kind": "brief",
                    "title": "咖啡店创作方案（模拟）",
                    "content": "模拟方案：面向上班族，以暖色晨光呈现咖啡制作与门店氛围。此处仅演示工具与存储流程，没有生成视频。",
                }
            return AIMessage(
                content="", tool_calls=[{"id": f"demo-{len(results)}", "name": name, "args": args}]
            )
        result = json.loads(results[-1].content)
        if not result["ok"]:
            return AIMessage(content="模拟保存失败，未完成交付。")
        data = result["data"]
        return AIMessage(
            content=f"模拟创作方案已保存：{data['artifactId']}，版本 {data['version']}。未调用真实模型或生成视频。"
        )
