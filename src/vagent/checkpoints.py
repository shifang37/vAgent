"""Local LangGraph persistence, opened under the existing FileStore instance lock."""

import sqlite3
from contextlib import asynccontextmanager
from pathlib import Path

from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

from vagent.errors import AppError

CHECKPOINT_VERSION = 1


def pending_interrupts(snapshot):
    # A second interrupt in one tools node may have next == ().
    pending = []
    for task in snapshot.tasks:
        delivered = task.result.get("wait_deliveries", {}) if isinstance(task.result, dict) else {}
        for item in task.interrupts:
            # A crash can leave both an old interrupt and the completed node's
            # durable pending writes. aget_state applies those writes to values;
            # their matching delivery evidence supersedes that old interrupt.
            marker = item.value if isinstance(item.value, dict) else {}
            if marker.get("waitId") in delivered and delivered[marker["waitId"]] == marker.get("generation"):
                continue
            pending.append(item)
    return pending


def completed_tool_task(snapshot) -> bool:
    return any(
        task.name == "tools" and isinstance(task.result, dict) and task.result.get("wait_deliveries")
        for task in snapshot.tasks
    )


def checkpoint_id(snapshot) -> str:
    return snapshot.config["configurable"]["checkpoint_id"]


def terminal_snapshot(snapshot) -> bool:
    return (
        bool(snapshot.values)
        and not pending_interrupts(snapshot)
        and not snapshot.next
        and snapshot.values.get("status") in {"completed", "failed", "cancelled"}
    )


@asynccontextmanager
async def open_checkpointer(home: Path):
    try:
        async with AsyncSqliteSaver.from_conn_string(str(home / "checkpoints.sqlite")) as saver:
            await saver.conn.execute("PRAGMA synchronous=FULL")
            await saver.setup()
            yield saver
    except sqlite3.DatabaseError:
        raise AppError("CHECKPOINT_ERROR", "检查点数据库不可用，已保留文件；请检查数据目录。") from None
