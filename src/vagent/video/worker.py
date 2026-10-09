"""One serial, restartable Worker; provider calls never run inside a Store transaction."""

import asyncio
import math
from contextlib import suppress
from datetime import datetime
from weakref import WeakKeyDictionary

from vagent.errors import AppError
from vagent.video.contracts import (
    Job,
    JobError,
    ProviderCallError,
    ProviderTaskHandle,
    ProviderTaskSnapshot,
    VideoProviderAdapter,
)
from vagent.video.jobs import JobService, after_seconds, changed_job, query_failed


class JobWorker:
    _store_locks: WeakKeyDictionary = WeakKeyDictionary()

    def __init__(self, service: JobService, *, idle_interval_seconds: float = 0.25):
        if not math.isfinite(idle_interval_seconds) or idle_interval_seconds <= 0:
            raise ValueError("Worker idle interval must be finite and positive")
        self.service = service
        self.idle_interval_seconds = idle_interval_seconds
        # Multiple owners must still share the one serial Worker boundary for
        # this Store, including when lifecycle code replaces a Worker instance.
        self._turn_lock = self._store_locks.setdefault(service.store, asyncio.Lock())
        self._stopping = asyncio.Event()
        self._task: asyncio.Task | None = None

    def start(self) -> asyncio.Task:
        if self._task is None or self._task.done():
            self._stopping.clear()
            self._task = asyncio.create_task(self.run(), name="video-job-worker")
        return self._task

    async def stop(self) -> None:
        self._stopping.set()
        if self._task is not None:
            if not self._task.done():
                self._task.cancel()
            with suppress(asyncio.CancelledError):
                await self._task

    async def run(self) -> None:
        while not self._stopping.is_set():
            result = await self.run_once()
            if result is not None:
                # Give cancellation and other application tasks a scheduling turn.
                await asyncio.sleep(0)
                continue
            try:
                await asyncio.wait_for(self._stopping.wait(), timeout=self.idle_interval_seconds)
            except TimeoutError:
                pass

    async def run_once(self) -> Job | None:
        """Process one due Job, re-reading the durable queue each time."""
        async with self._turn_lock:
            if self._stopping.is_set():
                return None
            timestamp = self.service.timestamp()
            current_time = datetime.fromisoformat(timestamp)
            due = []
            for raw in self.service.store.snapshot()["jobs"].values():
                job = Job.model_validate(raw)
                scheduled = job.created_at if job.status == "pending_submit" else job.next_poll_at
                if scheduled is not None and job.query_started_at is None:
                    when = datetime.fromisoformat(scheduled)
                    if when <= current_time:
                        due.append((when, job.id, job))
            for _, _, job in sorted(due):
                try:
                    adapter = self.service.adapter_for(job)
                except AppError:
                    # A missing old adapter must not block other queued jobs or
                    # silently replace the original request/capability snapshot.
                    continue
                if job.status == "pending_submit":
                    return await self._submit(job, adapter)
                return await self._query(job, adapter)
            return None

    def _snapshot(self, job: Job, snapshot: ProviderTaskSnapshot) -> Job:
        if job.provider_task_id is not None and snapshot.task_id != job.provider_task_id:
            raise ValueError("A query cannot replace the confirmed upstream ID")
        status = snapshot.status
        if job.status == "running" and status == "queued":
            status = "running"  # Preserve the last confirmed forward generation state.
        active = status in {"queued", "running"}
        timestamp = self.service.timestamp()
        return changed_job(
            job,
            timestamp,
            status=status,
            providerTaskId=snapshot.task_id,
            result=snapshot.result.model_dump(mode="json", by_alias=True) if snapshot.result else None,
            error=snapshot.error.model_dump(mode="json", by_alias=True) if snapshot.error else None,
            queryState="polling" if active else "idle",
            queryStartedAt=None,
            consecutiveQueryErrors=0,
            nextPollAt=after_seconds(timestamp, job.policy.interval_seconds) if active else None,
        )

    def _submit_error(self, job: Job, error: JobError | None = None, *, rejected: bool = False) -> Job:
        return self.service.update(
            job,
            status="failed" if rejected else "unknown",
            error=(
                error
                or JobError(
                    stage="submit",
                    code="SUBMISSION_UNKNOWN",
                    message="提交结果不确定，未取得已确认的上游 ID；不会自动重新提交。",
                )
            ).model_dump(mode="json", by_alias=True),
        )

    async def _submit(self, job: Job, adapter: VideoProviderAdapter) -> Job:
        job = self.service.update(job, status="submitting", submitAttempts=1)
        try:
            raw = await asyncio.wait_for(
                adapter.submit(job.request, job.operation_key), timeout=job.policy.submit_timeout_seconds
            )
            handle = ProviderTaskHandle.model_validate(
                raw.model_dump(mode="json", by_alias=True) if isinstance(raw, ProviderTaskHandle) else raw
            )
        except asyncio.CancelledError:
            self._submit_error(job)
            raise
        except ProviderCallError as error:
            if error.error.stage == "submit":
                return self._submit_error(
                    job, error.error, rejected=error.submission_outcome == "not_accepted"
                )
            return self._submit_error(job)
        except Exception:
            # Includes timeouts, malformed handles and unclassified provider
            # failures. None prove that a paid upstream did not accept a request.
            return self._submit_error(job)

        snapshot = handle.snapshot or ProviderTaskSnapshot(task_id=handle.task_id, status="queued")
        try:
            accepted = self._snapshot(job, snapshot)
        except ValueError:
            # The handle is valid even if its optional result has wrong provenance.
            # Keep that original ID and query it; never discard it and resubmit.
            accepted = self._snapshot(job, ProviderTaskSnapshot(task_id=handle.task_id, status="queued"))
        return self.service.save(accepted, expected_revision=job.revision)

    def _query_error(self, job: Job, error: JobError) -> Job:
        failed = query_failed(job, error, self.service.timestamp())
        return self.service.save(failed, expected_revision=job.revision)

    async def _query(self, job: Job, adapter: VideoProviderAdapter) -> Job:
        timestamp = self.service.timestamp()
        job = self.service.update(
            job,
            queryAttempts=job.query_attempts + 1,
            queryStartedAt=timestamp,
            nextPollAt=after_seconds(timestamp, job.policy.query_timeout_seconds),
        )
        try:
            raw = await asyncio.wait_for(
                adapter.query(job.provider_task_id), timeout=job.policy.query_timeout_seconds
            )
            snapshot = ProviderTaskSnapshot.model_validate(
                raw.model_dump(mode="json", by_alias=True) if isinstance(raw, ProviderTaskSnapshot) else raw
            )
            completed = self._snapshot(job, snapshot)
        except asyncio.CancelledError:
            self._query_error(
                job, JobError(stage="query", code="QUERY_INTERRUPTED", message="本地 Worker 已停止本次查询。")
            )
            raise
        except ProviderCallError as error:
            safe = (
                error.error
                if error.error.stage == "query"
                else JobError(stage="query", code="QUERY_ERROR", message="上游查询失败，生成状态保持不变。")
            )
            return self._query_error(job, safe)
        except TimeoutError:
            return self._query_error(
                job, JobError(stage="query", code="QUERY_TIMEOUT", message="上游查询超时，生成状态保持不变。")
            )
        except ValueError:
            return self._query_error(
                job,
                JobError(
                    stage="query", code="QUERY_INVALID_RESPONSE", message="上游查询返回了无效任务结果。"
                ),
            )
        except Exception:
            return self._query_error(
                job, JobError(stage="query", code="QUERY_ERROR", message="上游查询失败，生成状态保持不变。")
            )
        # Persistence errors escape; the recorded query intent remains recoverable.
        return self.service.save(completed, expected_revision=job.revision)
