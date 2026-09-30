"""Optional, bounded Redis cache for exact read-only model answers."""

import asyncio
import hashlib
import json
from contextlib import suppress

from langchain_core.messages import AIMessage, messages_to_dict
from redis.asyncio import Redis


class AnswerCache:
    def __init__(self, client, *, ttl: int = 3600, timeout: float = 0.5):
        self.client, self.ttl, self.timeout = client, ttl, timeout

    @classmethod
    def connect(cls, url: str, *, ttl: int = 3600):
        return cls(Redis.from_url(url, socket_connect_timeout=0.5, socket_timeout=0.5), ttl=ttl)

    @staticmethod
    def key(*, scope, signature, model_config, messages, tools, project, artifacts):
        payload = {
            "version": 1,
            "scope": scope,
            "signature": signature,
            "model": model_config,
            "messages": messages_to_dict(messages),
            "tools": tools,
            "project": project,
            "artifacts": artifacts,
        }
        raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return "vagent:answer:v1:" + hashlib.sha256(raw.encode()).hexdigest()

    async def get(self, key: str) -> tuple[AIMessage | None, str]:
        try:
            async with asyncio.timeout(self.timeout):
                raw = await self.client.get(key)
            if raw is None:
                return None, "miss"
            if len(raw) > 262144:
                return None, "invalid"
            value = json.loads(raw)
            if (
                not isinstance(value, dict)
                or set(value) != {"version", "answer"}
                or type(value["version"]) is not int
                or value["version"] != 1
                or not isinstance(value["answer"], str)
                or not value["answer"].strip()
            ):
                return None, "invalid"
            # No tool calls, provider usage, IDs or response metadata are replayed.
            return AIMessage(content=value["answer"]), "hit"
        except (ValueError, TypeError, UnicodeError):
            return None, "invalid"
        except Exception:
            return None, "error"

    async def put(self, key: str, reply: AIMessage) -> str:
        if (
            not isinstance(reply, AIMessage)
            or reply.tool_calls
            or reply.invalid_tool_calls
            or reply.additional_kwargs.get("tool_calls")
            or not isinstance(reply.content, str)
            or not reply.content.strip()
            or reply.response_metadata.get("finish_reason", "stop") != "stop"
        ):
            return "skipped"
        raw = json.dumps({"version": 1, "answer": reply.content}, ensure_ascii=False)
        if len(raw.encode()) > 262144:
            return "skipped"
        try:
            async with asyncio.timeout(self.timeout):
                await self.client.set(key, raw, ex=self.ttl)
            return "stored"
        except Exception:
            return "error"

    async def aclose(self):
        with suppress(Exception):
            async with asyncio.timeout(self.timeout):
                await self.client.aclose()
