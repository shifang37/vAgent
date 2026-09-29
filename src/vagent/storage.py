"""Atomic, single-writer JSON storage; domain writes and operation results commit together."""

import copy
import hashlib
import json
import os
import threading
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, TypeVar
from uuid import uuid4

from langchain_core.messages import messages_from_dict
from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel

from vagent.config import assert_id
from vagent.errors import AppError, failure

T = TypeVar("T")


def now() -> str:
    return datetime.now(UTC).isoformat()


class Record(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True, extra="forbid", strict=True)


class PlanStep(Record):
    text: str
    status: Literal["pending", "in_progress", "completed"]


class Project(Record):
    id: str
    revision: int = Field(ge=0)
    goal: str
    audience: str
    style: str
    constraints: list[str]
    plan: list[PlanStep]


class StoredMessage(Record):
    type: Literal["human", "ai", "tool"]
    data: dict[str, Any]


class Session(Record):
    id: str
    messages: list[StoredMessage]


class ArtifactVersion(Record):
    version: int = Field(ge=1)
    title: str
    content: str
    created_at: str


class Artifact(Record):
    id: str
    project_id: str
    kind: Literal["brief", "script", "storyboard"]
    versions: list[ArtifactVersion] = Field(min_length=1)


class RunRecord(Record):
    id: str
    session_id: str
    request_id: str
    prompt: str
    model: str
    status: Literal["running", "completed", "failed", "cancelled", "interrupted"]
    messages: list[StoredMessage]
    model_steps: int = Field(ge=0)
    tool_calls: int = Field(ge=0)
    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)
    context_bytes: int = Field(default=0, ge=0)
    dropped_messages: int = Field(default=0, ge=0)
    answer: str
    error_code: str | None = None
    created_at: str
    updated_at: str


class Success(Record):
    ok: Literal[True]
    data: Any


class ErrorDetail(Record):
    code: str
    message: str


class Failure(Record):
    ok: Literal[False]
    error: ErrorDetail


class Operation(Record):
    fingerprint: str
    result: Success | Failure = Field(discriminator="ok")


class Database(Record):
    schema_version: Literal[1]
    projects: dict[str, Project]
    sessions: dict[str, Session]
    artifacts: dict[str, Artifact]
    runs: dict[str, RunRecord]
    operations: dict[str, Operation]


class FileStore:
    def __init__(self, home: Path, state: dict):
        self.home = home
        self._state = state
        self._mutex = threading.RLock()
        self._closed = False

    @classmethod
    def open(cls, home: Path | str) -> "FileStore":
        home = Path(home).resolve()
        home.mkdir(parents=True, exist_ok=True, mode=0o700)
        lock = home / "instance.lock"
        try:
            handle = lock.open("x", encoding="utf-8")
        except FileExistsError:
            raise AppError(
                "STORE_LOCKED", "数据目录已被锁定。确认没有 vagent 进程后，再手动移除 instance.lock。"
            ) from None
        try:
            with handle:
                json.dump({"pid": os.getpid(), "createdAt": now()}, handle)
            try:
                raw = (home / "state.json").read_text(encoding="utf-8")
            except FileNotFoundError:
                state = {
                    "schemaVersion": 1,
                    "projects": {},
                    "sessions": {},
                    "artifacts": {},
                    "runs": {},
                    "operations": {},
                }
            else:
                try:
                    state = json.loads(raw)
                    Database.model_validate(state)
                    for collection in (state["sessions"], state["runs"]):
                        for item in collection.values():
                            messages_from_dict(item["messages"])
                    # Preserve schema v1 domain data and operation fingerprints from the TS prototype.
                    for run in state["runs"].values():
                        run.setdefault("contextBytes", 0)
                        run.setdefault("droppedMessages", 0)
                except (ValueError, TypeError, KeyError):
                    raise AppError(
                        "INVALID_STORE", "本地状态损坏或版本不支持，已保留原文件，未重置数据。"
                    ) from None
            store = cls(home, state)

            def recover(draft: dict) -> None:
                for run in draft["runs"].values():
                    if run["status"] == "running":
                        run.update(status="interrupted", updatedAt=now())

            store.transaction(recover)
            return store
        except BaseException:
            lock.unlink(missing_ok=True)
            raise

    def snapshot(self) -> dict:
        with self._mutex:
            return copy.deepcopy(self._state)

    def transaction(self, mutate: Callable[[dict], T]) -> T:
        with self._mutex:
            if self._closed:
                raise AppError("STORE_CLOSED", "状态存储已关闭。")
            draft = copy.deepcopy(self._state)
            value = mutate(draft)
            Database.model_validate(draft)
            temporary = self.home / f"state-{uuid4()}.tmp"
            try:
                with temporary.open("x", encoding="utf-8", newline="\n") as handle:
                    json.dump(draft, handle, ensure_ascii=False, indent=2)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temporary, self.home / "state.json")
            finally:
                temporary.unlink(missing_ok=True)
            self._state = draft
            return copy.deepcopy(value)

    def ensure_session(self, session_id: str) -> None:
        assert_id(session_id)

        def create(draft: dict) -> None:
            draft["sessions"].setdefault(session_id, {"id": session_id, "messages": []})
            draft["projects"].setdefault(
                session_id,
                {
                    "id": session_id,
                    "revision": 0,
                    "goal": "",
                    "audience": "",
                    "style": "",
                    "constraints": [],
                    "plan": [],
                },
            )

        self.transaction(create)

    def operation(self, key: str, name: str, args: dict, mutate: Callable[[dict], Any]) -> dict:
        # Match the original JSON encoding so existing journal entries remain usable.
        encoded = json.dumps({"name": name, "args": args}, ensure_ascii=False, separators=(",", ":"))
        fingerprint = hashlib.sha256(encoded.encode("utf-8")).hexdigest()

        def execute(draft: dict) -> dict:
            existing = draft["operations"].get(key)
            if existing:
                if existing["fingerprint"] != fingerprint:
                    return failure("OPERATION_CONFLICT", "同一调用 ID 不能用于不同操作。")
                return existing["result"]
            before = copy.deepcopy(draft)
            try:
                result = {"ok": True, "data": copy.deepcopy(mutate(draft))}
            except Exception as error:
                draft.clear()
                draft.update(before)
                result = (
                    failure(error.code, str(error))
                    if isinstance(error, AppError)
                    else failure("TOOL_ERROR", "工具执行失败，未保存修改。")
                )
            draft["operations"][key] = {"fingerprint": fingerprint, "result": result}
            return result

        return self.transaction(execute)

    def close(self) -> None:
        with self._mutex:
            if not self._closed:
                (self.home / "instance.lock").unlink()
                self._closed = True

    def __enter__(self) -> "FileStore":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()
