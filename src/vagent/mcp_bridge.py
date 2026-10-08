"""Explicit read-only allowlists over the official MCP stdio client."""

import asyncio
import hashlib
import json
import os
import re
import sys
from contextlib import AsyncExitStack, asynccontextmanager
from datetime import timedelta
from pathlib import Path

from jsonschema import Draft202012Validator
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from vagent.errors import AppError
from vagent.tools import ToolDefinition, ToolRegistry


class ServerConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    name: str = Field(pattern=r"^[a-z][a-z0-9_]{0,15}$")
    command: str = Field(min_length=1)
    args: list[str] = Field(default_factory=list, max_length=32)
    read_only_tools: list[str] = Field(min_length=1, max_length=16)
    timeout_seconds: float = Field(default=10.0, gt=0, le=60)


def server_configs(path: Path | None, local: bool) -> list[ServerConfig]:
    configs = []
    if local:
        configs.append(
            ServerConfig(
                name="video",
                command=sys.executable,
                args=["-m", "vagent.mcp_server"],
                read_only_tools=["shot_timing", "frame_budget"],
            )
        )
    if path:
        try:
            if path.stat().st_size > 32768:
                raise ValueError
            data = json.loads(path.read_text(encoding="utf-8"))
            if (
                not isinstance(data, dict)
                or set(data) != {"servers"}
                or not isinstance(data["servers"], list)
            ):
                raise ValueError
            configs.extend(ServerConfig.model_validate(entry) for entry in data["servers"])
        except (OSError, ValueError, ValidationError):
            raise AppError(
                "MCP_CONFIG", "MCP 配置必须是有效的 servers 数组，含明确的只读工具白名单。"
            ) from None
    if len(configs) > 4 or len({c.name for c in configs}) != len(configs):
        raise AppError("MCP_CONFIG", "最多配置 4 个名称不同的 MCP 服务。")
    return configs


def check_schema(value):
    if isinstance(value, dict):
        for key, child in value.items():
            if key in {"$ref", "$dynamicRef"} and (not isinstance(child, str) or not child.startswith("#")):
                raise AppError("MCP_SCHEMA", "MCP Schema 不能引用外部地址。")
            check_schema(child)
    elif isinstance(value, list):
        for child in value:
            check_schema(child)


def make_call(session: ClientSession, name: str, timeout: float):
    async def call(args: dict):
        try:
            async with asyncio.timeout(timeout):
                result = await session.call_tool(name, args, read_timeout_seconds=timedelta(seconds=timeout))
        except TimeoutError:
            raise AppError("MCP_TIMEOUT", "MCP 只读工具超时，未自动重试。") from None
        except Exception:
            raise AppError("MCP_CONNECTION", "MCP 工具连接失败，未自动重试。") from None
        if result.isError:
            raise AppError("MCP_TOOL_ERROR", "MCP 服务报告工具执行失败。")
        # Tool-returned text remains untrusted data. No URLs are fetched and no
        # resource, prompt, sampling, elicitation or executable content is followed.
        payload = {
            "content": [item.model_dump(mode="json") for item in result.content if item.type == "text"]
        }
        if result.structuredContent is not None:
            payload["structuredContent"] = result.structuredContent
        if len(json.dumps(payload, ensure_ascii=False).encode()) > 16384:
            raise AppError("MCP_RESULT_LIMIT", "MCP 结果超过 16 KiB，未塞入上下文。")
        return payload

    return call


@asynccontextmanager
async def connect_mcp(registry: ToolRegistry, configs: list[ServerConfig]):
    statuses = []
    async with AsyncExitStack() as stack:
        # Never stream a subprocess's stderr into model-visible or Web logs.
        errlog = stack.enter_context(open(os.devnull, "w"))
        for config in configs:
            try:
                streams = await stack.enter_async_context(
                    stdio_client(
                        StdioServerParameters(
                            command=config.command, args=config.args, env={"PYTHONIOENCODING": "utf-8"}
                        ),
                        errlog=errlog,
                    )
                )
                session = await stack.enter_async_context(
                    ClientSession(*streams, read_timeout_seconds=timedelta(seconds=config.timeout_seconds))
                )
                async with asyncio.timeout(config.timeout_seconds):
                    initialized = await session.initialize()
                    found = {}
                    cursor = None
                    for _ in range(8):
                        page = await session.list_tools(cursor=cursor)
                        for tool in page.tools:
                            if tool.name in found:
                                raise AppError("MCP_SCHEMA", "MCP 服务返回了重复的工具名称。")
                            found[tool.name] = tool
                        cursor = page.nextCursor
                        if not cursor:
                            break
                    if cursor or len(found) > 128:
                        raise AppError("MCP_LIMIT", "MCP 工具目录过大。")
                names = []
                identity = hashlib.sha256(
                    json.dumps(config.model_dump(), sort_keys=True).encode()
                ).hexdigest()
                for name in sorted(set(config.read_only_tools)):
                    tool = found.get(name)
                    if tool is None or not tool.annotations or tool.annotations.readOnlyHint is not True:
                        raise AppError("MCP_ALLOWLIST", "MCP 白名单工具缺失或未声明只读。")
                    registered_name = f"mcp_{config.name}_{name}"
                    if not re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", registered_name):
                        raise AppError("MCP_SCHEMA", "MCP 工具名称不符合模型协议。")
                    check_schema(tool.inputSchema)
                    Draft202012Validator.check_schema(tool.inputSchema)
                    if tool.inputSchema.get("type") != "object":
                        raise AppError("MCP_SCHEMA", "MCP 工具 Schema 必须是 object。")
                    registry.register(
                        ToolDefinition(
                            name=registered_name,
                            description=f"[MCP 只读] {(tool.description or name)[:1000]}",
                            schema=tool.inputSchema,
                            effect="read",
                            async_execute=make_call(session, name, config.timeout_seconds),
                            identity=identity + ":" + initialized.serverInfo.version,
                        )
                    )
                    names.append(registered_name)
                statuses.append(
                    {
                        "name": config.name,
                        "status": "connected",
                        "transport": "stdio",
                        "protocolVersion": initialized.protocolVersion,
                        "tools": names,
                    }
                )
            except AppError:
                raise
            except Exception:
                raise AppError("MCP_CONNECT", "MCP 启动或工具发现失败，请检查本地服务配置。") from None
        yield statuses
