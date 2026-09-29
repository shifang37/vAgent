import json
from typing import Protocol

import httpx
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, ToolMessage
from langchain_deepseek import ChatDeepSeek

from vagent.config import require_key


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
        self._client = ChatDeepSeek(
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
