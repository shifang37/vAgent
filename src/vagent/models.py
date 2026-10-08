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
        async with aclosing(self._client.bind_tools(tools).astream(messages, stream_usage=True)) as stream:
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
        if not chunks:
            raise AppError("STREAM_INTERRUPTED", "回复流未完成，草稿未保存，可在剩余预算内继续。")
        combined = add_ai_message_chunks(chunks[0], *chunks[1:])
        reply = message_chunk_to_message(combined)
        reply.usage_metadata = usage_metadata
        reply.response_metadata["token_usage"] = raw_usage
        reason = reply.response_metadata.get("finish_reason")
        if not reason:
            raise StreamResponseError(
                "STREAM_INTERRUPTED", "回复流中断，草稿未保存，可在剩余预算内继续。", reply
            )
        if reason not in {"stop", "tool_calls"}:
            raise StreamResponseError(
                "RESPONSE_TRUNCATED", "模型回复被截断，本步未保存回复或执行工具。", reply
            )
        # LangChain deliberately repairs partial JSON while aggregating chunks.
        # Tool execution needs strictly complete JSON, so decode the raw arguments again.
        calls = []
        try:
            for call in combined.tool_call_chunks:
                args = json.loads(call["args"])
                if not isinstance(args, dict) or not call.get("name") or not call.get("id"):
                    raise ValueError
                calls.append({"name": call["name"], "args": args, "id": call["id"], "type": "tool_call"})
            if (reason == "tool_calls") != bool(calls):
                raise ValueError
        except (ValueError, TypeError, KeyError):
            raise StreamResponseError(
                "INVALID_TOOL_CALL", "模型返回了不完整的工具参数，本步未执行工具。", reply
            ) from None
        reply.tool_calls = calls
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
