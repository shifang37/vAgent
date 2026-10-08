"""Bundled, deterministic read-only production-planning tools served over real MCP stdio."""

import math
from typing import Annotated, Any, Literal

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations
from pydantic import Field

server = FastMCP("vagent-video-planning", log_level="ERROR")
READ_ONLY = ToolAnnotations(
    readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False
)


@server.tool(annotations=READ_ONLY)
def shot_timing(
    durations: Annotated[list[Annotated[float, Field(gt=0, le=3600)]], Field(min_length=1, max_length=60)],
    target_seconds: Annotated[float, Field(gt=0, le=3600)],
) -> dict[str, Any]:
    """检查镜头时长是否满足目标，返回每个镜头起止秒数；这是精确计算，不生成视频。"""
    cursor = 0.0
    timeline = []
    for index, duration in enumerate(durations, 1):
        timeline.append({"shot": index, "start": round(cursor, 3), "end": round(cursor + duration, 3)})
        cursor += duration
    return {
        "timeline": timeline,
        "totalSeconds": round(cursor, 3),
        "targetSeconds": target_seconds,
        "differenceSeconds": round(cursor - target_seconds, 3),
        "matches": math.isclose(cursor, target_seconds, abs_tol=0.001),
    }


@server.tool(annotations=READ_ONLY)
def frame_budget(
    seconds: Annotated[float, Field(gt=0, le=3600)],
    fps: Literal[24, 25, 30, 50, 60] = 24,
    aspect_ratio: Literal["16:9", "9:16", "1:1"] = "16:9",
) -> dict[str, Any]:
    """计算时长对应的帧数和 1080 级画幅建议；不调用生成供应商。"""
    sizes = {"16:9": [1920, 1080], "9:16": [1080, 1920], "1:1": [1080, 1080]}
    return {
        "seconds": seconds,
        "fps": fps,
        "frames": round(seconds * fps),
        "aspectRatio": aspect_ratio,
        "suggestedSize": sizes[aspect_ratio],
    }


if __name__ == "__main__":
    server.run(transport="stdio")
