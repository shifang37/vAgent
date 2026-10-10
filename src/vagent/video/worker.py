"""One serial, restartable Worker; provider calls never run inside a Store transaction."""

import asyncio
import math
from contextlib import suppress
from datetime import datetime
from uuid import uuid4
from weakref import WeakKeyDictionary

from vagent.errors import AppError
from vagent.video.contracts import (
    DownloadRecord,
    Job,
    JobError,
    JobV2,
    ProviderCallError,
    ProviderTaskHandle,
    ProviderTaskHandleV2,
    ProviderTaskSnapshot,
    ProviderTaskSnapshotV2,
    VideoProviderAdapter,
    parse_job,
)
from vagent.video.jobs import JobService, after_seconds, changed_job, query_failed


class JobWorker:
    _store_locks: WeakKeyDictionary = WeakKeyDictionary()
    _store_next_calls: WeakKeyDictionary = WeakKeyDictionary()

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
                job = parse_job(raw)
                scheduled = job.created_at if job.status == "pending_submit" else job.next_poll_at
                if (
                    isinstance(job, JobV2)
                    and job.query_state in {"polling", "retrying"}
                    and self.service.query_deadline_passed(job)
                ):
                    scheduled = job.query_deadline_at
                if scheduled is not None and job.query_started_at is None:
                    when = datetime.fromisoformat(scheduled)
                    if when <= current_time:
                        due.append((when, job.id, job))
            for _, _, job in sorted(due):
                if (
                    isinstance(job, JobV2)
                    and self.service.query_deadline_passed(job)
                    and job.status in {"queued", "running"}
                ):
                    return self.service.pause_expired_query(job)
                try:
                    adapter = self.service.check_runtime(job, submit=job.status == "pending_submit")
                except AppError as error:
                    # A missing old adapter must not block other queued jobs or
                    # silently replace the original request/capability snapshot.
                    if isinstance(job, JobV2):
                        blocked = self.service.block(
                            job, error, stage="submit" if job.status == "pending_submit" else "query"
                        )
                        if blocked.revision != job.revision:
                            return blocked
                    continue
                live = isinstance(job, JobV2)
                if live and current_time < self._store_next_calls.get(self.service.store, current_time):
                    continue
                try:
                    if job.status == "pending_submit":
                        return await self._submit(job, adapter)
                    return await self._query(job, adapter)
                finally:
                    if live:
                        self._store_next_calls[self.service.store] = datetime.fromisoformat(
                            after_seconds(self.service.timestamp(), 1)
                        )
            return None

    def _snapshot(self, job: Job, snapshot: ProviderTaskSnapshot) -> Job:
        if isinstance(job, JobV2):
            return self._live_snapshot(job, snapshot)
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

    def _live_snapshot(self, job: JobV2, snapshot: ProviderTaskSnapshotV2) -> JobV2:
        if snapshot.task_id != job.provider_task_id:
            raise ValueError("A snapshot cannot replace the confirmed task ID")
        if job.provider_output is not None:
            # Duplicate terminal responses never allocate a second media ID.
            return job
        timestamp = self.service.timestamp()
        status = snapshot.status
        if job.status == "running" and status == "queued":
            status = "running"
        active = status in {"queued", "running"}
        extra = {}
        if snapshot.output is not None:
            media_id = str(uuid4())
            extra = {
                "providerOutput": snapshot.output.model_dump(mode="json", by_alias=True),
                "download": DownloadRecord(
                    media_id=media_id,
                    relative_path=f"media/{job.context.project_id}/{media_id}.mp4",
                    next_attempt_at=timestamp,
                ).model_dump(mode="json", by_alias=True),
                "cost": {
                    **job.cost.model_dump(mode="json", by_alias=True),
                    "providerUsage": snapshot.output.usage.model_dump(mode="json", by_alias=True)
                    if snapshot.output.usage
                    else None,
                },
            }
            status = "downloading"
        if job.query_started_at is not None:
            extra["lastQueryRequestId"] = snapshot.request_id
        return changed_job(
            job,
            timestamp,
            status=status,
            lastProviderStatus=snapshot.provider_status,
            error=snapshot.error.model_dump(mode="json", by_alias=True) if snapshot.error else None,
            queryState="polling" if active else "idle",
            queryStartedAt=None,
            consecutiveQueryErrors=0,
            queryPauseReason=None,
            runtimeBlock=None,
            nextPollAt=after_seconds(timestamp, job.policy.interval_seconds) if active else None,
            **extra,
        )

    def _submit_error(self, job: Job, error: JobError | None = None, *, rejected: bool = False) -> Job:
        extra = {}
        if isinstance(job, JobV2):
            extra["providerSubmissionRequestId"] = getattr(error, "request_id", None)
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
            **extra,
        )

    async def _submit(self, job: Job, adapter: VideoProviderAdapter) -> Job:
        extra = {}
        if isinstance(job, JobV2):
            timestamp = self.service.timestamp()
            extra = {
                "submissionStartedAt": timestamp,
                "queryDeadlineAt": after_seconds(timestamp, 86400),
                "runtimeBlock": None,
            }
        job = self.service.update(job, status="submitting", submitAttempts=1, **extra)
        try:
            raw = await asyncio.wait_for(
                adapter.submit(job.request, job.operation_key), timeout=job.policy.submit_timeout_seconds
            )
            if isinstance(job, JobV2):
                value = (
                    raw.model_dump(mode="json", by_alias=True)
                    if isinstance(raw, ProviderTaskHandleV2)
                    else raw
                )
                # Confirm the ID independently of any optional initial snapshot.
                handle = ProviderTaskHandleV2.model_validate(
                    {key: item for key, item in value.items() if key != "snapshot"}
                )
                try:
                    snapshot = (
                        ProviderTaskSnapshotV2.model_validate(value["snapshot"])
                        if value.get("snapshot")
                        else None
                    )
                    if snapshot and snapshot.task_id != handle.task_id:
                        snapshot = None
                except ValueError:
                    snapshot = None
            else:
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

        if isinstance(job, JobV2):
            # Once committed, even a crash while interpreting the optional snapshot
            # can only query this ID. Never turn it back into an unconfirmed submit.
            accepted = self.service.update(
                job,
                status="queued",
                providerTaskId=handle.task_id,
                providerSubmissionRequestId=handle.request_id,
                queryState="polling",
                nextPollAt=after_seconds(self.service.timestamp(), job.policy.interval_seconds),
            )
            if snapshot is None:
                return accepted
            try:
                completed = self._live_snapshot(accepted, snapshot)
            except ValueError:
                return accepted
            return self.service.save(completed, expected_revision=accepted.revision)

        snapshot = handle.snapshot or ProviderTaskSnapshot(task_id=handle.task_id, status="queued")
        try:
            accepted = self._snapshot(job, snapshot)
        except ValueError:
            # The handle is valid even if its optional result has wrong provenance.
            # Keep that original ID and query it; never discard it and resubmit.
            accepted = self._snapshot(job, ProviderTaskSnapshot(task_id=handle.task_id, status="queued"))
        return self.service.save(accepted, expected_revision=job.revision)

    def _query_error(self, job: Job, error: JobError, *, retry_after_at=None, pause_reason=None) -> Job:
        failed = query_failed(
            job, error, self.service.timestamp(), retry_after_at=retry_after_at, pause_reason=pause_reason
        )
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
            snapshot_type = ProviderTaskSnapshotV2 if isinstance(job, JobV2) else ProviderTaskSnapshot
            snapshot = snapshot_type.model_validate(
                raw.model_dump(mode="json", by_alias=True) if isinstance(raw, snapshot_type) else raw
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
            return self._query_error(
                job, safe, retry_after_at=error.retry_after_at, pause_reason=error.pause_reason
            )
        except TimeoutError:
            return self._query_error(
                job, JobError(stage="query", code="QUERY_TIMEOUT", message="上游查询超时，生成状态保持不变。")
            )
        except ValueError:
            return self._query_error(
                job,
                JobError(
                    stage="query",
                    code="QUERY_PROTOCOL_ERROR" if isinstance(job, JobV2) else "QUERY_INVALID_RESPONSE",
                    message="上游查询返回了无效任务结果。",
                ),
            )
        except Exception:
            return self._query_error(
                job, JobError(stage="query", code="QUERY_ERROR", message="上游查询失败，生成状态保持不变。")
            )
        # Persistence errors escape; the recorded query intent remains recoverable.
        return self.service.save(completed, expected_revision=job.revision)
