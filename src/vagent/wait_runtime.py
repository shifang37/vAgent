"""Durable, provider-neutral coordination of graph interrupts and tool outcomes."""

import asyncio
import contextlib
import json
import math
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from weakref import WeakKeyDictionary

from langchain_core.messages import AIMessage, ToolMessage, messages_from_dict, messages_to_dict
from pydantic import TypeAdapter

from vagent.checkpoints import checkpoint_id, pending_interrupts
from vagent.errors import AppError, failure, public_error
from vagent.storage import FileStore, now
from vagent.waiting import (
    WAIT_EXECUTION_VERSION,
    DeferredToolResult,
    ResumeToken,
    ToolExecutionContext,
    ToolResult,
    WaitBinding,
)


def utc_now() -> datetime:
    return datetime.now(UTC)


@dataclass
class ExecutionControl:
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    run_id: str | None = None
    cancelled: asyncio.Event | None = None


_controls: WeakKeyDictionary = WeakKeyDictionary()


def execution_control(store: FileStore) -> ExecutionControl:
    return _controls.setdefault(store, ExecutionControl())


def close_external_time(record: dict, timestamp: str) -> None:
    started = record.get("externalWaitStartedAt")
    if started is not None:
        seconds = max(
            0, (datetime.fromisoformat(timestamp) - datetime.fromisoformat(started)).total_seconds()
        )
        record["externalWaitSeconds"] = record.get("externalWaitSeconds", 0) + seconds
        record["externalWaitStartedAt"] = None


def close_pending_calls(messages: list, error: AppError) -> list:
    pending = {}
    for message in messages:
        if isinstance(message, AIMessage):
            pending.update({call["id"]: call for call in message.tool_calls})
        elif isinstance(message, ToolMessage):
            pending.pop(message.tool_call_id, None)
    return [
        *messages,
        *[
            ToolMessage(
                content=json.dumps(failure(error.code, str(error)), ensure_ascii=False),
                tool_call_id=call["id"],
                name=call["name"],
            )
            for call in pending.values()
        ],
    ]


class WaitService:
    """All wait mutations use the same JSON transaction as the owning Run."""

    def __init__(self, store: FileStore, *, clock=utc_now, on_event=None):
        self.store, self.clock, self.on_event = store, clock, on_event

    def timestamp(self) -> str:
        return self.clock().astimezone(UTC).isoformat()

    def emit(self, binding: WaitBinding, phase: str) -> None:
        if self.on_event:
            with contextlib.suppress(Exception):
                self.on_event(
                    {"type": f"wait.{phase}", "runId": binding.context.run_id, "waitId": binding.id}
                )

    def get(self, wait_id: str) -> WaitBinding:
        raw = self.store.snapshot()["waits"].get(wait_id)
        if raw is None:
            raise AppError("WAIT_NOT_FOUND", "没有此持久等待记录，未重新执行原调用。")
        return WaitBinding.model_validate(raw)

    def bindings(self, run_id: str) -> list[WaitBinding]:
        return [
            WaitBinding.model_validate(raw)
            for raw in self.store.snapshot()["waits"].values()
            if raw["context"]["runId"] == run_id
        ]

    @staticmethod
    def write(draft: dict, binding: WaitBinding, **changes) -> WaitBinding:
        current = draft["waits"][binding.id]
        if current["revision"] != binding.revision:
            raise AppError("STALE_WAIT", "等待记录已变化，请重新读取。")
        updated = WaitBinding.model_validate({**current, **changes, "revision": binding.revision + 1})
        draft["waits"][binding.id] = updated.model_dump(mode="json", by_alias=True)
        return updated

    def check_call(self, marker, context, call, index) -> WaitBinding:
        binding = self.get(marker.wait_id)
        if (
            binding.context != context
            or binding.deferred() != marker
            or binding.operation_fingerprint != self.store.operation_fingerprint(call["name"], call["args"])
            or binding.batch_index not in {None, index}
        ):
            raise AppError("WAIT_CONTEXT_INVALID", "等待与原 Run、工具调用或批次位置不匹配。")
        return binding

    def prepare(self, marker, context, call, index, messages, journal) -> WaitBinding:
        binding = self.check_call(marker, context, call, index)
        if binding.status == "stopped" or not binding.auto_resume:
            raise AppError("CANCELLED", "等待已停止，外部任务仍独立跟踪。")
        if binding.status in {"ready", "claimed", "delivered"}:
            return binding  # Still consume the original interrupt position on replay.
        active_seconds = journal.pause()

        def suspend(draft):
            record = draft["runs"][context.run_id]
            if record["status"] == "cancelled":
                raise AppError("CANCELLED", "等待已停止，外部任务仍独立跟踪。")
            updated = self.write(draft, binding, batchIndex=index)
            record.update(
                status="waiting_external",
                activeWaitId=binding.id,
                externalWaitStartedAt=record.get("externalWaitStartedAt") or binding.started_at,
                activeSeconds=active_seconds,
                inFlightSeconds=0,
                messages=messages_to_dict(messages),
                errorCode=None,
                answer="",
                resumable=True,
                updatedAt=now(),
            )
            return updated

        binding = self.store.transaction(suspend)
        self.emit(binding, "prepared")
        return binding

    def synchronize(self, snapshot, run_id: str) -> list[WaitBinding]:
        """Only an actual synchronous graph checkpoint may arm or confirm delivery."""
        if not snapshot.values:
            return []
        armed = []
        for item in pending_interrupts(snapshot):
            marker = DeferredToolResult.model_validate(item.value)
            binding = self.get(marker.wait_id)
            state = snapshot.values
            calls = state["messages"][-1].tool_calls if isinstance(state["messages"][-1], AIMessage) else []
            index = next(
                (i for i, call in enumerate(calls) if call["id"] == binding.context.tool_call_id), None
            )
            if (
                index is None
                or binding.context.run_id != run_id
                or binding.context.model_step != state["model_steps"]
                or binding.resource != marker.resource
                or marker.generation > binding.generation
            ):
                raise AppError("WAIT_CONTEXT_INVALID", "图中断与持久等待不匹配。")
            self.check_call(binding.deferred(), binding.context, calls[index], index)
            if binding.status == "preparing":
                binding = self.store.transaction(
                    lambda draft: self.write(
                        draft,
                        binding,
                        status="armed",
                        batchIndex=index,
                        checkpointId=checkpoint_id(snapshot),
                        interruptId=item.id,
                    )
                )
                self.emit(binding, "armed")
            elif binding.status not in {"stopped", "delivered"} and binding.interrupt_id != item.id:
                raise AppError("WAIT_CONTEXT_INVALID", "等待的图中断标识已变化。")
            armed.append(binding)
        deliveries = snapshot.values.get("wait_deliveries", {})
        for binding in self.bindings(run_id):
            if (
                binding.status not in {"claimed", "stopped"}
                or deliveries.get(binding.id) != binding.generation
            ):
                continue
            operation = self.store.snapshot()["operations"].get(binding.context.operation_key)
            result = binding.result.model_dump(mode="json", by_alias=True) if binding.result else None
            if (
                operation is None
                or operation["fingerprint"] != binding.operation_fingerprint
                or operation["result"] != result
                or not any(
                    isinstance(message, ToolMessage)
                    and message.tool_call_id == binding.context.tool_call_id
                    and json.loads(message.content) == result
                    for message in snapshot.values["messages"]
                )
            ):
                raise AppError("WAIT_CONTEXT_INVALID", "图交付记录缺少匹配的原工具结果。")

            def deliver(draft):
                updated = self.write(
                    draft, binding, status="delivered", deliveredCheckpointId=checkpoint_id(snapshot)
                )
                record = draft["runs"][run_id]
                if record.get("activeWaitId") == binding.id:
                    record["activeWaitId"] = None
                return updated

            delivered = self.store.transaction(deliver)
            self.emit(delivered, "delivered")
        return armed

    def resolve(self, binding: WaitBinding, resolvers: Mapping[str, Callable]) -> WaitBinding:
        if binding.status != "armed" or not binding.auto_resume:
            return binding
        if self.clock() >= datetime.fromisoformat(binding.deadline_at):
            result = failure(binding.timeout_error.code, binding.timeout_error.message)
        else:
            resolve = resolvers.get(binding.resource.kind)
            if resolve is None:
                raise AppError("WAIT_RESOLVER_UNAVAILABLE", "原外部资源的等待解析器不可用。")
            result = resolve(binding)
        if result is None:
            return binding
        outcome = TypeAdapter(ToolResult).validate_python(result).model_dump(mode="json", by_alias=True)
        binding = self.store.transaction(
            lambda draft: self.write(draft, binding, status="ready", result=outcome)
        )
        self.emit(binding, "ready")
        return binding

    def claim(self, binding: WaitBinding) -> WaitBinding:
        def claim(draft):
            record = draft["runs"][binding.context.run_id]
            current = WaitBinding.model_validate(draft["waits"][binding.id])
            if current.revision != binding.revision:
                raise AppError("STALE_WAIT", "等待记录已变化，未领取恢复权。")
            if not current.auto_resume or current.status not in {"ready", "claimed"}:
                raise AppError("WAIT_NOT_READY", "等待尚未就绪或已停止。")
            if draft["sessions"][record["sessionId"]].get("latestRunId") != record["id"]:
                raise AppError("STALE_RUN", "会话已有更新请求，不能自动继续旧 Run。")
            if record["status"] not in {"waiting_external", "interrupted", "running"}:
                raise AppError("WAIT_NOT_READY", "原 Run 已停止，不能自动继续。")
            updated = self.write(
                draft,
                binding,
                status="claimed",
                claimedModelSteps=binding.claimed_model_steps
                if binding.claimed_model_steps is not None
                else record["modelSteps"],
            )
            record.update(status="running", errorCode=None, answer="", waitResumeError=None, updatedAt=now())
            return updated

        binding = self.store.transaction(claim)
        self.emit(binding, "claimed")
        return binding

    def commit_result(self, token: ResumeToken, context: ToolExecutionContext, call: dict) -> dict:
        binding = self.get(token.wait_id)
        if binding.context != context:
            raise AppError("WAIT_CONTEXT_INVALID", "恢复指针不属于当前工具调用。")
        confirmed = binding.confirmed_result(token)

        def commit(draft):
            current = WaitBinding.model_validate(draft["waits"][binding.id])
            current.confirmed_result(token)
            record = draft["runs"][context.run_id]
            if record["status"] == "cancelled" or not current.auto_resume:
                raise AppError("CANCELLED", "等待已停止，未交付工具结果。")
            if current.status not in {"claimed", "delivered"}:
                raise AppError("WAIT_NOT_CLAIMED", "等待尚未领取恢复权。")
            fingerprint = self.store.operation_fingerprint(call["name"], call["args"])
            if fingerprint != binding.operation_fingerprint:
                raise AppError("OPERATION_CONFLICT", "同一等待调用不能改变参数。")
            outcome = confirmed.model_dump(mode="json", by_alias=True)
            previous = self.store._operation_result(draft, context.operation_key, fingerprint)
            if previous is not None and previous != outcome:
                raise AppError("OPERATION_CONFLICT", "原调用已有不同的持久结果。")
            draft["operations"][context.operation_key] = {"fingerprint": fingerprint, "result": outcome}
            if record.get("activeWaitId") == binding.id:
                close_external_time(record, self.timestamp())
            record.update(status="running", updatedAt=now())
            return outcome

        result = self.store.transaction(commit)
        self.emit(binding, "result_committed")
        return result

    def rearm(self, run_id: str) -> None:
        """Explicit continuation alone may restore a stopped original interrupt."""

        def restore(draft):
            for binding in self.bindings(run_id):
                if binding.status == "delivered" or (binding.auto_resume and binding.status != "stopped"):
                    continue
                operation = draft["operations"].get(binding.context.operation_key)
                result = (
                    operation["result"]
                    if operation
                    else (binding.result.model_dump(mode="json", by_alias=True) if binding.result else None)
                )
                changes = {"autoResume": True, "claimedModelSteps": None, "deliveredCheckpointId": None}
                if result is not None:
                    # A committed outcome and any SQLite resume value keep their identity.
                    changes.update(status="ready", result=result)
                else:
                    timestamp = self.timestamp()
                    duration = datetime.fromisoformat(binding.deadline_at) - datetime.fromisoformat(
                        binding.started_at
                    )
                    changes.update(
                        status="armed" if binding.checkpoint_id else "preparing",
                        generation=binding.generation + 1,
                        startedAt=timestamp,
                        deadlineAt=(datetime.fromisoformat(timestamp) + duration).isoformat(),
                        result=None,
                    )
                self.write(draft, binding, **changes)
            record = draft["runs"][run_id]
            record.update(
                status="waiting_external", errorCode=None, answer="", waitResumeError=None, updatedAt=now()
            )

        self.store.transaction(restore)

    def stop_run(self, run_id: str) -> bool:
        def stop(draft):
            record = draft["runs"].get(run_id)
            if record is None:
                raise AppError("NOT_FOUND", "没有这个 Run。")
            if record["status"] not in {"running", "waiting_external", "interrupted"}:
                return False
            for binding in self.bindings(run_id):
                if binding.status != "delivered":
                    self.write(draft, binding, status="stopped", autoResume=False, deliveredCheckpointId=None)
            close_external_time(record, self.timestamp())
            error = AppError("CANCELLED", "执行已停止，外部任务仍独立跟踪，保留已完成产物。")
            record.update(
                status="cancelled",
                errorCode=error.code,
                answer=str(error),
                resumable=True,
                waitResumeError=None,
                updatedAt=now(),
                messages=messages_to_dict(close_pending_calls(messages_from_dict(record["messages"]), error)),
            )
            return True

        stopped = self.store.transaction(stop)
        control = execution_control(self.store)
        if stopped and control.run_id == run_id and control.cancelled:
            control.cancelled.set()
        return stopped

    def block_auto(self, run_id: str, error: AppError) -> None:
        value = {"code": error.code, "message": str(error)}
        if self.store.snapshot()["runs"][run_id].get("waitResumeError") == value:
            return
        self.store.transaction(
            lambda draft: draft["runs"][run_id].update(waitResumeError=value, updatedAt=now())
        )

    def recovery_binding(self, run_id: str) -> WaitBinding | None:
        bindings = [
            binding
            for binding in self.bindings(run_id)
            if binding.auto_resume and binding.status != "stopped"
        ]
        return max(
            bindings, key=lambda b: (b.context.model_step, b.batch_index or 0, b.started_at), default=None
        )


class WaitCoordinator:
    """Restart scans are authoritative; in-memory notifications only reduce latency."""

    def __init__(
        self, store, runner_factory, *, resolvers=None, clock=utc_now, on_event=None, interval_seconds=0.25
    ):
        if not math.isfinite(interval_seconds) or interval_seconds <= 0:
            raise ValueError("Wait scan interval must be finite and positive")
        self.store, self.runner_factory = store, runner_factory
        self.resolvers = resolvers or {}
        self.waits = WaitService(store, clock=clock, on_event=on_event)
        self.interval_seconds = interval_seconds
        self.shutdown = asyncio.Event()
        self._wake = asyncio.Event()
        self._task = None

    def notify(self) -> None:
        self._wake.set()

    def start(self) -> asyncio.Task:
        if self._task is None or self._task.done():
            self.shutdown.clear()
            self._task = asyncio.create_task(self.run(), name="external-wait-coordinator")
        return self._task

    async def stop(self) -> None:
        self.shutdown.set()
        self._wake.set()
        if self._task:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task

    def stop_run(self, run_id: str) -> bool:
        return self.waits.stop_run(run_id)

    async def run(self) -> None:
        while not self.shutdown.is_set():
            self._wake.clear()
            await self.run_once()
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=self.interval_seconds)
            except TimeoutError:
                pass

    async def run_once(self) -> dict | None:
        if self.shutdown.is_set() or execution_control(self.store).lock.locked():
            return None
        for record in self.store.snapshot()["runs"].values():
            if (
                record.get("executionVersion") != WAIT_EXECUTION_VERSION
                or record["status"] not in {"waiting_external", "interrupted", "running"}
                or self.waits.recovery_binding(record["id"]) is None
            ):
                continue
            try:
                # Results survive missing/changed model configuration. Only graph
                # invocation needs the original model, tools and remaining budget.
                for binding in self.waits.bindings(record["id"]):
                    self.waits.resolve(binding, self.resolvers)
                runner = self.runner_factory(record)
                return await runner.advance_wait(record["id"], shutdown=self.shutdown)
            except AppError as error:
                if error.code not in {"RUN_BUSY", "STALE_WAIT"}:
                    self.waits.block_auto(record["id"], error)
            except Exception as error:
                self.waits.block_auto(record["id"], public_error(error))
            return self.store.snapshot()["runs"][record["id"]]
        return None
