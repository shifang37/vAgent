"""Local protocol fault fixtures. Never installed or exposed by vagent."""

import asyncio

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

server = FastMCP("test-fixtures", log_level="ERROR")
read = ToolAnnotations(readOnlyHint=True)


@server.tool(annotations=read)
async def slow() -> str:
    await asyncio.sleep(2)
    return "done"


@server.tool(annotations=read)
def fail() -> str:
    raise ValueError("private-server-exception-must-not-leak")


@server.tool(annotations=read)
def large() -> str:
    return "x" * 20000


@server.tool(annotations=ToolAnnotations(readOnlyHint=False))
def write() -> str:
    return "not-admitted"


if __name__ == "__main__":
    server.run(transport="stdio")
