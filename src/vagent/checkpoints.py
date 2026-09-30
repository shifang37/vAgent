"""Local LangGraph persistence, opened under the existing FileStore instance lock."""

import sqlite3
from contextlib import asynccontextmanager
from pathlib import Path

from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

from vagent.errors import AppError

CHECKPOINT_VERSION = 1


@asynccontextmanager
async def open_checkpointer(home: Path):
    try:
        async with AsyncSqliteSaver.from_conn_string(str(home / "checkpoints.sqlite")) as saver:
            await saver.conn.execute("PRAGMA synchronous=FULL")
            await saver.setup()
            yield saver
    except sqlite3.DatabaseError:
        raise AppError("CHECKPOINT_ERROR", "检查点数据库不可用，已保留文件；请检查数据目录。") from None
