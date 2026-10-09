"""Atomic Job registration, revision checks and provider-independent recovery."""

import copy
from collections.abc import Callable, Iterable
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING
from uuid import uuid4

from pydantic import ValidationError

from vagent.errors import AppError, failure
from vagent.video.contracts import (
    Job,
    JobError,
    PollingPolicy,
    VideoCapabilities,
    VideoProviderAdapter,
    VideoRequest,
    job_transition_allowed,
    validate_video_request,
)
from vagent.waiting import ToolExecutionContext

if TYPE_CHECKING:
    from vagent.storage import FileStore


def utc_now() -> datetime:
    return datetime.now(UTC)


def after_seconds(timestamp: str, seconds: float) -> str:
    return (datetime.fromisoformat(timestamp) + timedelta(seconds=seconds)).isoformat()


def changed_job(job: Job, timestamp: str, **changes) -> Job:
    # A backwards wall-clock adjustment must not invalidate already committed history.
    timestamp = max(datetime.fromisoformat(timestamp), datetime.fromisoformat(job.updated_at)).isoformat()
    return Job.model_validate(
        {
            **job.model_dump(mode="json", by_alias=True),
            **changes,
            "revision": job.revision + 1,
            "updatedAt": timestamp,
        }
    )


def query_failed(job: Job, error: JobError, timestamp: str, *, failure_at: str | None = None) -> Job:
    errors = job.consecutive_query_errors + 1
    delays = job.policy.retry_delays_seconds
    retrying = errors <= len(delays)
    return changed_job(
        job,
        timestamp,
        queryStartedAt=None,
        consecutiveQueryErrors=errors,
        queryState="retrying" if retrying else "paused",
        nextPollAt=after_seconds(failure_at or timestamp, delays[errors - 1]) if retrying else None,
        error=error.model_dump(mode="json", by_alias=True),
    )


def recover_interrupted_jobs(state: dict, timestamp: str) -> None:
    """Only the Store opener calls this, after acquiring the exclusive instance lock."""
    for job_id, raw in state["jobs"].items():
        job = Job.model_validate(raw)
        if job.status == "submitting":
            recovered = changed_job(
                job,
                timestamp,
                status="unknown",
                error={
                    "stage": "submit",
                    "code": "SUBMISSION_UNKNOWN",
                    "message": "提交过程中进程退出，受理结果不确定；不会自动重新提交。",
                },
            )
        elif job.query_started_at is not None:
            # The saved timeout deadline anchors retries across repeated restarts.
            recovered = query_failed(
                job,
                JobError(stage="query", code="QUERY_INTERRUPTED", message="查询过程中进程退出。"),
                timestamp,
                failure_at=job.next_poll_at,
            )
        else:
            continue
        state["jobs"][job_id] = recovered.model_dump(mode="json", by_alias=True)


def _check_context(state: dict, context: ToolExecutionContext) -> None:
    run = state["runs"].get(context.run_id)
    if (
        context.project_id not in state["projects"]
        or context.session_id not in state["sessions"]
        or context.project_id != context.session_id
        or run is None
        or run["sessionId"] != context.session_id
    ):
        raise AppError("JOB_CONTEXT_INVALID", "视频任务的项目、会话或 Run 不匹配。")


def validate_job_changes(previous: dict[str, dict], state: dict) -> None:
    """Check identities and transitions inside every Store commit, not just the Worker."""
    jobs = state["jobs"]
    if previous.keys() - jobs.keys():
        raise AppError("JOB_IMMUTABLE", "已登记的视频任务不能删除。")
    run_ids = set()
    immutable = (
        "contract_version",
        "id",
        "context",
        "operation_key",
        "mode",
        "request",
        "request_fingerprint",
        "capabilities",
        "policy",
        "created_at",
    )
    for job_id, raw in jobs.items():
        job = Job.model_validate(raw)
        _check_context(state, job.context)
        if job.id != job_id or job.context.run_id in run_ids:
            raise AppError("JOB_CONFLICT", "Job ID 不匹配，或同一 Run 登记了多个视频任务。")
        run_ids.add(job.context.run_id)
        operation = state["operations"].get(job.operation_key)
        if (
            not operation
            or operation["result"].get("ok") is not True
            or not isinstance(operation["result"].get("data"), dict)
            or operation["result"]["data"].get("jobId") != job_id
        ):
            raise AppError("JOB_OPERATION_MISSING", "Job 必须与原生成调用的成功结果一起保存。")
        old_raw = previous.get(job_id)
        if old_raw is None:
            if job.status != "pending_submit" or job.revision != 0:
                raise AppError("JOB_CONFLICT", "新任务必须先登记，之后才能提交。")
            continue
        old = Job.model_validate(old_raw)
        if job == old:
            continue
        if any(getattr(job, field) != getattr(old, field) for field in immutable):
            raise AppError("JOB_IMMUTABLE", "Job 的请求、来源、执行上下文和策略快照不可修改。")
        if job.revision != old.revision + 1:
            raise AppError("JOB_REVISION_CONFLICT", "Job 版本已变化，请重新读取。")
        if old.status in {"succeeded", "failed", "unknown"} or not job_transition_allowed(
            old.status, job.status
        ):
            raise AppError("JOB_INVALID_TRANSITION", "不允许回退或重新提交这个视频任务。")
        if old.provider_task_id is not None and job.provider_task_id != old.provider_task_id:
            raise AppError("JOB_IMMUTABLE", "已确认的上游任务 ID 不可替换。")
        if (
            job.query_attempts < old.query_attempts
            or job.submit_attempts < old.submit_attempts
            or datetime.fromisoformat(job.updated_at) < datetime.fromisoformat(old.updated_at)
        ):
            raise AppError("JOB_CONFLICT", "Job 的累计调用次数和更新时间不能回退。")
    if any(key != value["id"] for key, value in state["waits"].items()):
        raise AppError("WAIT_CONFLICT", "等待记录 ID 不匹配。")


class JobService:
    def __init__(
        self,
        store: "FileStore",
        adapters: Iterable[VideoProviderAdapter],
        *,
        clock: Callable[[], datetime] = utc_now,
        policy: PollingPolicy | None = None,
    ):
        self.store, self.clock = store, clock
        self.policy = policy or PollingPolicy()
        self._adapters = {}
        self._capabilities = {}
        for adapter in adapters:
            capabilities = adapter.capabilities()
            key = (capabilities.provider, capabilities.model)
            if key in self._adapters:
                raise ValueError("A provider/model pair can only be registered once")
            self._adapters[key] = adapter
            self._capabilities[key] = capabilities

    def timestamp(self) -> str:
        value = self.clock()
        if value.utcoffset() is None:
            raise ValueError("Job clocks must return timezone-aware datetimes")
        return value.astimezone(UTC).isoformat()

    def capabilities(self) -> tuple[VideoCapabilities, ...]:
        return tuple(self._capabilities[key] for key in sorted(self._capabilities))

    def adapter_for(self, job: Job) -> VideoProviderAdapter:
        key = (job.request.provider, job.request.model)
        if self._capabilities.get(key) != job.capabilities:
            raise AppError("VIDEO_PROVIDER_UNAVAILABLE", "缺少任务原有模式和能力版本的视频适配器。")
        return self._adapters[key]

    def generate(self, request: VideoRequest | dict, *, context: ToolExecutionContext) -> dict:
        """Return a journaled registration outcome; never call a provider here.

        Raw JSON arguments retain the existing Operation fingerprint semantics.
        The separate normalized request fingerprint enforces the per-Run slot.
        """
        try:
            parsed = VideoRequest.model_validate(request)
        except ValidationError:
            return failure("INVALID_ARGUMENTS", "视频请求不符合 Schema，未创建任务。")
        args = (
            copy.deepcopy(request)
            if isinstance(request, dict)
            else parsed.model_dump(mode="json", by_alias=True)
        )

        def register(draft):
            _check_context(draft, context)
            if draft["runs"][context.run_id].get("readOnly", False):
                raise AppError("READ_ONLY", "只读 Run 不能创建视频任务。")
            existing = next(
                (
                    Job.model_validate(j)
                    for j in draft["jobs"].values()
                    if j["context"]["runId"] == context.run_id
                ),
                None,
            )
            if existing is not None:
                if existing.request_fingerprint != parsed.fingerprint():
                    raise AppError(
                        "JOB_ALREADY_EXISTS", f"本次 Run 已登记视频任务 {existing.id}，不能再创建不同请求。"
                    )
                return self._registration(existing)
            capabilities = self._capabilities.get((parsed.provider, parsed.model))
            if capabilities is None:
                raise AppError("VIDEO_MODEL_UNAVAILABLE", "所选视频模型不在当前能力表中。")
            validate_video_request(
                parsed, capabilities, project_id=context.project_id, artifacts=draft["artifacts"]
            )
            timestamp = self.timestamp()
            job = Job(
                id=str(uuid4()),
                context=context,
                operation_key=context.operation_key,
                request=parsed,
                request_fingerprint=parsed.fingerprint(),
                capabilities=capabilities,
                mode=capabilities.mode,
                policy=self.policy,
                created_at=timestamp,
                updated_at=timestamp,
            )
            draft["jobs"][job.id] = job.model_dump(mode="json", by_alias=True)
            return self._registration(job)

        try:
            with self.store.locked():
                # Scope is checked even for a replay, whose mutation callback is skipped.
                _check_context(self.store.snapshot(), context)
                return self.store.operation(context.operation_key, "video_generate", args, register)
        except AppError as error:
            return failure(error.code, str(error))

    @staticmethod
    def _registration(job: Job) -> dict:
        return {
            "jobId": job.id,
            "status": job.status,
            "mode": job.mode,
            "simulated": True,
            "mediaAvailable": False,
        }

    def get(self, job_id: str, *, project_id: str) -> Job:
        raw = self.store.snapshot()["jobs"].get(job_id)
        if raw is None or raw["context"]["projectId"] != project_id:
            raise AppError("JOB_NOT_FOUND", "当前项目中没有这个视频任务。")
        return Job.model_validate(raw)

    def list(self, *, project_id: str, session_id: str | None = None) -> tuple[Job, ...]:
        return tuple(
            Job.model_validate(raw)
            for raw in self.store.snapshot()["jobs"].values()
            if raw["context"]["projectId"] == project_id
            and (session_id is None or raw["context"]["sessionId"] == session_id)
        )

    def save(self, job: Job, *, expected_revision: int) -> Job:
        def commit(draft):
            old = draft["jobs"].get(job.id)
            if old is None or old.get("revision", 0) != expected_revision:
                raise AppError("JOB_REVISION_CONFLICT", "Job 版本已变化，请重新读取。")
            draft["jobs"][job.id] = job.model_dump(mode="json", by_alias=True)
            return draft["jobs"][job.id]

        return Job.model_validate(self.store.transaction(commit))

    def update(self, job: Job, **changes) -> Job:
        return self.save(changed_job(job, self.timestamp(), **changes), expected_revision=job.revision)

    def retry_query(self, job_id: str, *, project_id: str) -> Job:
        job = self.get(job_id, project_id=project_id)
        if job.query_state != "paused" or not job.provider_task_id:
            raise AppError("JOB_QUERY_NOT_PAUSED", "只能恢复已有上游 ID 且查询已暂停的任务。")
        return self.update(
            job,
            queryState="polling",
            consecutiveQueryErrors=0,
            error=None,
            nextPollAt=self.timestamp(),
        )
