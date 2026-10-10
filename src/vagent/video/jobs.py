"""Atomic Job registration, revision checks and provider-independent recovery."""

import copy
from collections.abc import Callable, Iterable
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING
from uuid import uuid4

from pydantic import ValidationError

from vagent.errors import AppError, failure
from vagent.video.contracts import (
    WAN_ADAPTER_VERSION,
    WAN_ENDPOINT_PROFILE,
    WAN_PROMPT_PREFIX,
    CostEstimate,
    CostRecord,
    DownloadPolicy,
    Job,
    JobError,
    JobErrorV2,
    JobV2,
    MediaAsset,
    PollingPolicy,
    VideoCapabilities,
    VideoIntentV2,
    VideoPrice,
    VideoProviderAdapter,
    VideoRequest,
    VideoRequestV2,
    WanParameters,
    job_transition_allowed,
    live_job_transition_allowed,
    live_polling_policy,
    parse_job,
    validate_video_request,
    wan_price,
)
from vagent.waiting import ToolExecutionContext

if TYPE_CHECKING:
    from vagent.config import Config
    from vagent.storage import FileStore


def utc_now() -> datetime:
    return datetime.now(UTC)


def after_seconds(timestamp: str, seconds: float) -> str:
    return (datetime.fromisoformat(timestamp) + timedelta(seconds=seconds)).isoformat()


def changed_job(job: Job, timestamp: str, **changes) -> Job:
    # A backwards wall-clock adjustment must not invalidate already committed history.
    timestamp = max(datetime.fromisoformat(timestamp), datetime.fromisoformat(job.updated_at)).isoformat()
    return parse_job(
        {
            **job.model_dump(mode="json", by_alias=True),
            **changes,
            "revision": job.revision + 1,
            "updatedAt": timestamp,
        }
    )


def query_failed(
    job: Job,
    error: JobError,
    timestamp: str,
    *,
    failure_at: str | None = None,
    retry_after_at: str | None = None,
    pause_reason: str | None = None,
) -> Job:
    errors = job.consecutive_query_errors + 1
    delays = job.policy.retry_delays_seconds
    retrying = errors <= len(delays) and pause_reason is None
    next_poll = after_seconds(failure_at or timestamp, delays[errors - 1]) if retrying else None
    if next_poll and retry_after_at:
        next_poll = max(datetime.fromisoformat(next_poll), datetime.fromisoformat(retry_after_at)).isoformat()
    extra = {}
    if isinstance(job, JobV2):
        extra = {
            "queryPauseReason": None if retrying else pause_reason or "retry_exhausted",
            "lastQueryRequestId": getattr(error, "request_id", None),
            "runtimeBlock": {
                "stage": "query",
                "code": error.code,
                "message": error.message,
                "blockedAt": timestamp,
            }
            if pause_reason == "configuration"
            else None,
        }
        if error.code == "PROVIDER_TASK_UNAVAILABLE":
            extra["lastProviderStatus"] = "UNKNOWN"
    return changed_job(
        job,
        timestamp,
        queryStartedAt=None,
        consecutiveQueryErrors=errors,
        queryState="retrying" if retrying else "paused",
        nextPollAt=next_poll,
        error=error.model_dump(mode="json", by_alias=True),
        **extra,
    )


def recover_interrupted_jobs(state: dict, timestamp: str) -> None:
    """Only the Store opener calls this, after acquiring the exclusive instance lock."""
    for job_id, raw in state["jobs"].items():
        job = parse_job(raw)
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
        elif isinstance(job, JobV2) and job.download and job.download.phase == "writing":
            from vagent.video.media import MediaError, failed_download

            recovered = failed_download(
                job,
                MediaError("DOWNLOAD_NETWORK", retryable=True),
                timestamp,
                failure_at=job.download.deadline_at,
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
        job = parse_job(raw)
        _check_context(state, job.context)
        if job.id != job_id or job.context.run_id in run_ids:
            raise AppError("JOB_CONFLICT", "Job ID 不匹配，或同一 Run 登记了多个视频任务。")
        run_ids.add(job.context.run_id)
        validate_video_request(
            job.request, job.capabilities, project_id=job.context.project_id, artifacts=state["artifacts"]
        )
        if isinstance(job, JobV2) and state["runs"][job.context.run_id].get("videoMode") != "live":
            raise AppError("JOB_CONTEXT_INVALID", "真实视频 Job 必须归属原 live Run。")
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
        old = parse_job(old_raw)
        if job == old:
            continue
        if any(getattr(job, field) != getattr(old, field) for field in immutable):
            raise AppError("JOB_IMMUTABLE", "Job 的请求、来源、执行上下文和策略快照不可修改。")
        if job.revision != old.revision + 1:
            raise AppError("JOB_REVISION_CONFLICT", "Job 版本已变化，请重新读取。")
        transition = live_job_transition_allowed if isinstance(job, JobV2) else job_transition_allowed
        terminal = {"failed", "unknown"} if isinstance(job, JobV2) else {"succeeded", "failed", "unknown"}
        if old.status in terminal or not transition(old.status, job.status):
            raise AppError("JOB_INVALID_TRANSITION", "不允许回退或重新提交这个视频任务。")
        if old.provider_task_id is not None and job.provider_task_id != old.provider_task_id:
            raise AppError("JOB_IMMUTABLE", "已确认的上游任务 ID 不可替换。")
        if (
            job.query_attempts < old.query_attempts
            or job.submit_attempts < old.submit_attempts
            or datetime.fromisoformat(job.updated_at) < datetime.fromisoformat(old.updated_at)
        ):
            raise AppError("JOB_CONFLICT", "Job 的累计调用次数和更新时间不能回退。")
        if isinstance(job, JobV2):
            if (
                job.intent_fingerprint != old.intent_fingerprint
                or job.cost.estimate != old.cost.estimate
                or job.download_policy != old.download_policy
                or (
                    old.submission_started_at is not None
                    and (
                        job.submission_started_at != old.submission_started_at
                        or job.query_deadline_at != old.query_deadline_at
                    )
                )
                or (
                    old.provider_task_id is not None
                    and job.provider_submission_request_id != old.provider_submission_request_id
                )
                or (old.provider_output is not None and job.provider_output != old.provider_output)
                or (old.result is not None and job.result != old.result)
            ):
                raise AppError("JOB_IMMUTABLE", "真实任务的请求、报价、提交身份和已交付结果不可修改。")
            if old.download is not None and (
                job.download is None
                or job.download.media_id != old.download.media_id
                or job.download.relative_path != old.download.relative_path
                or job.download.attempts < old.download.attempts
                or job.download.generation < old.download.generation
            ):
                raise AppError("JOB_IMMUTABLE", "媒体身份和累计下载次数不可替换或回退。")
            if old.download is not None:
                before, after = old.download, job.download
                new_window = after.generation == before.generation + 1
                if new_window:
                    allowed = before.phase == "failed" or (
                        old.status == "succeeded"
                        and before.phase == "committed"
                        and old.media_availability.status == "unavailable"
                    )
                    if not allowed or after.window_attempts or after.attempts != before.attempts:
                        raise AppError("JOB_CONFLICT", "仅停止的下载可登记新的恢复窗口。")
                elif (
                    after.generation != before.generation
                    or after.source_policy_version != before.source_policy_version
                    or not 0 <= after.window_attempts - before.window_attempts <= 1
                    or after.attempts - before.attempts != after.window_attempts - before.window_attempts
                ):
                    raise AppError("JOB_CONFLICT", "下载窗口和累计尝试次数不能重置或跳过。")
                if before.repair and not after.repair:
                    raise AppError("JOB_IMMUTABLE", "已交付媒体的修复记录不能变回初次交付。")
    if any(key != value["id"] for key, value in state["waits"].items()):
        raise AppError("WAIT_CONFLICT", "等待记录 ID 不匹配。")
    validate_media_changes(state.get("media", {}), state)


def validate_media_changes(previous: dict[str, dict], state: dict) -> None:
    media = state.get("media", {})
    if previous.keys() - media.keys() or any(media[key] != raw for key, raw in previous.items()):
        raise AppError("MEDIA_IMMUTABLE", "已交付媒体的身份、来源和完整性记录不可删除或修改。")
    seen_jobs = set()
    for media_id, raw in media.items():
        asset = MediaAsset.model_validate(raw)
        job_raw = state["jobs"].get(asset.job_id)
        job = parse_job(job_raw) if job_raw else None
        if (
            asset.id != media_id
            or asset.job_id in seen_jobs
            or not isinstance(job, JobV2)
            or job.status != "succeeded"
            or asset.project_id != job.context.project_id
            or asset.source_refs != job.request.source_refs
            or asset.id != job.result.media_refs[0].media_id
            or asset.relative_path != job.download.relative_path
            or job.download.prepared is None
            or (asset.size_bytes, asset.sha256, asset.metadata)
            != (
                job.download.prepared.size_bytes,
                job.download.prepared.sha256,
                job.download.prepared.metadata,
            )
        ):
            raise AppError("MEDIA_CONFLICT", "媒体索引必须与完整交付结果及原任务一起提交。")
        seen_jobs.add(asset.job_id)
    for raw in state["jobs"].values():
        if raw.get("contractVersion", 1) == 2 and raw["status"] == "succeeded" and raw["id"] not in seen_jobs:
            raise AppError("MEDIA_NOT_COMMITTED", "完整媒体索引提交前不能将真实任务标记为成功。")


class JobService:
    def __init__(
        self,
        store: "FileStore",
        adapters: Iterable[VideoProviderAdapter],
        *,
        clock: Callable[[], datetime] = utc_now,
        policy: PollingPolicy | None = None,
        on_change: Callable[[Job], None] | None = None,
        config: "Config | None" = None,
        price: VideoPrice | None = wan_price(),
        download_policy: DownloadPolicy | None = None,
    ):
        self.store, self.clock = store, clock
        self.policy = policy or PollingPolicy()
        self.config, self.price = config, price
        self.download_policy = download_policy or DownloadPolicy()
        self.on_change = on_change
        from vagent.video.media import MediaService

        self.media = MediaService(self)
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

    def capabilities(self, mode: str | None = None) -> tuple[VideoCapabilities, ...]:
        return tuple(
            self._capabilities[key]
            for key in sorted(self._capabilities)
            if mode is None or self._capabilities[key].mode == mode
        )

    def check_live_configuration(self) -> None:
        if self.config is None or not self.config.video_api_key:
            raise AppError("VIDEO_KEY_MISSING", "请配置视频服务 Key，文本模型 Key 不能代替视频 Key。")
        if not self.config.video_workspace_id:
            raise AppError("VIDEO_WORKSPACE_REQUIRED", "请配置北京视频 Key 对应的业务空间 ID。")

    def configuration_status(self) -> dict:
        try:
            self.check_live_configuration()
            if self.price is None:
                raise AppError("VIDEO_PRICE_UNKNOWN", "缺少已核实的视频价格，未创建或提交任务。")
            if Decimal(self.price.amount) > Decimal(self.config.video_max_job_cost):
                raise AppError("VIDEO_COST_LIMIT", "视频估算超过单 Job 金额上限，未创建或提交任务。")
            return {"configured": True, "permissionStatus": "unverified", "error": None}
        except AppError as error:
            return {
                "configured": False,
                "permissionStatus": "unverified",
                "error": {"code": error.code, "message": str(error)},
            }

    def adapter_for(self, job: Job) -> VideoProviderAdapter:
        key = (job.request.provider, job.request.model)
        if self._capabilities.get(key) != job.capabilities:
            raise AppError("VIDEO_PROVIDER_UNAVAILABLE", "缺少任务原有模式和能力版本的视频适配器。")
        return self._adapters[key]

    def check_runtime(self, job: Job, *, submit=False) -> VideoProviderAdapter:
        adapter = self.adapter_for(job)
        if isinstance(job, JobV2):
            self.check_live_configuration()
            if (job.request.workspace_id, job.request.region, job.request.provider, job.request.model) != (
                self.config.video_workspace_id,
                self.config.video_region,
                self.config.video_provider,
                self.config.video_model,
            ):
                raise AppError("VIDEO_PROVIDER_UNAVAILABLE", "缺少原任务业务空间、地域和模型的匹配配置。")
            if hasattr(adapter, "check_configuration"):
                adapter.check_configuration(job.request)
            if submit:
                if self.price is None:
                    raise AppError("VIDEO_PRICE_UNKNOWN", "缺少已核实的视频价格，原任务保持待提交。")
                if self.price.model_dump() != job.cost.estimate.model_dump(exclude={"max_job_cost"}):
                    raise AppError("VIDEO_PRICE_CHANGED", "视频价格版本已变化，保留原报价并暂停提交。")
                if Decimal(job.cost.estimate.amount) > min(
                    Decimal(job.cost.estimate.max_job_cost), Decimal(self.config.video_max_job_cost)
                ):
                    raise AppError("VIDEO_COST_LIMIT", "原视频报价超过当前金额上限，任务保持待提交。")
        return adapter

    def block(self, job: JobV2, error: AppError, *, stage: str) -> JobV2:
        """One durable configuration diagnostic, without consuming a HTTP attempt."""
        if job.runtime_block and (
            job.runtime_block.stage,
            job.runtime_block.code,
            job.runtime_block.message,
        ) == (stage, error.code, str(error)):
            return job
        changes = {
            "runtimeBlock": {
                "stage": stage,
                "code": error.code,
                "message": str(error),
                "blockedAt": self.timestamp(),
            }
        }
        if stage == "query":
            changes.update(
                queryState="paused",
                queryPauseReason="configuration",
                nextPollAt=None,
                queryStartedAt=None,
                error=JobErrorV2(stage="query", code=error.code, message=str(error)).model_dump(
                    mode="json", by_alias=True
                ),
            )
        return self.update(job, **changes)

    def query_deadline_passed(self, job: Job) -> bool:
        return (
            isinstance(job, JobV2)
            and job.query_deadline_at is not None
            and datetime.fromisoformat(self.timestamp()) >= datetime.fromisoformat(job.query_deadline_at)
        )

    def pause_expired_query(self, job: JobV2) -> JobV2:
        return self.update(
            job,
            queryState="paused",
            queryPauseReason="task_unavailable",
            queryStartedAt=None,
            nextPollAt=None,
            runtimeBlock=None,
            error=JobErrorV2(
                stage="query",
                code="PROVIDER_TASK_UNAVAILABLE",
                message="原任务已到达保守查询期限，查询已暂停；不会重新生成。",
            ).model_dump(mode="json", by_alias=True),
        )

    def generate(self, request: VideoRequest | dict, *, context: ToolExecutionContext) -> dict:
        """Return a journaled registration outcome; never call a provider here.

        Raw JSON arguments retain the existing Operation fingerprint semantics.
        The separate normalized request fingerprint enforces the per-Run slot.
        """
        if not isinstance(request, (dict, VideoRequest)):
            return failure("INVALID_ARGUMENTS", "视频请求不符合 Schema，未创建任务。")
        args = (
            copy.deepcopy(request)
            if isinstance(request, dict)
            else request.model_dump(mode="json", by_alias=True)
        )
        previous = self.store.operation_result(context.operation_key, "video_generate", args)
        if previous is not None:
            try:
                _check_context(self.store.snapshot(), context)
            except AppError as error:
                return failure(error.code, str(error))
            return previous
        live = args.get("provider") == "wan"
        try:
            if live:
                if (
                    args.keys() - {"provider", "model", "capabilitiesVersion", "prompt", "spec", "sourceRefs"}
                    or (
                        isinstance(args.get("spec"), dict)
                        and args["spec"].keys() - {"durationSeconds", "resolution", "aspectRatio"}
                    )
                    or any(
                        isinstance(ref, dict) and ref.keys() - {"artifactId", "version"}
                        for ref in args.get("sourceRefs", [])
                        if isinstance(args.get("sourceRefs"), (list, tuple))
                    )
                ):
                    raise ValueError("Only public camelCase arguments are accepted")
            parsed = (VideoIntentV2 if live else VideoRequest).model_validate(args)
        except (ValidationError, ValueError, TypeError):
            return failure("INVALID_ARGUMENTS", "视频请求不符合 Schema，未创建任务。")

        def register(draft):
            _check_context(draft, context)
            if draft["runs"][context.run_id].get("readOnly", False):
                raise AppError("READ_ONLY", "只读 Run 不能创建视频任务。")
            existing = next(
                (parse_job(j) for j in draft["jobs"].values() if j["context"]["runId"] == context.run_id),
                None,
            )
            if existing is not None:
                same = (
                    existing.intent_fingerprint == parsed.intent_fingerprint()
                    if isinstance(existing, JobV2) and isinstance(parsed, VideoIntentV2)
                    else not isinstance(existing, JobV2)
                    and existing.request_fingerprint == parsed.fingerprint()
                )
                if not same:
                    raise AppError(
                        "JOB_ALREADY_EXISTS", f"本次 Run 已登记视频任务 {existing.id}，不能再创建不同请求。"
                    )
                return self._registration(existing)
            capabilities = self._capabilities.get((parsed.provider, parsed.model))
            if capabilities is None:
                raise AppError("VIDEO_MODEL_UNAVAILABLE", "所选视频模型不在当前能力表中。")
            mode = draft["runs"][context.run_id].get("videoMode")
            if capabilities.mode != mode and not (mode is None and capabilities.mode == "mock"):
                raise AppError("VIDEO_MODEL_UNAVAILABLE", "原 Run 的视频模式不开放所选模型。")
            validate_video_request(
                parsed, capabilities, project_id=context.project_id, artifacts=draft["artifacts"]
            )
            timestamp = self.timestamp()
            extra = {}
            frozen = parsed
            if live:
                self.check_live_configuration()
                if len(parsed.prompt) > 4991:
                    raise AppError("VIDEO_PROMPT_TOO_LONG", "视频提示词正文最多 4991 个字符，未创建任务。")
                if self.price is None or self.price != capabilities.price:
                    raise AppError("VIDEO_PRICE_UNKNOWN", "缺少与当前视频能力匹配的已核实价格，未创建任务。")
                if Decimal(self.price.amount) > Decimal(self.config.video_max_job_cost):
                    raise AppError("VIDEO_COST_LIMIT", "视频估算超过单 Job 金额上限，未创建任务。")
                frozen = VideoRequestV2(
                    **parsed.model_dump(mode="json", by_alias=True),
                    region=self.config.video_region,
                    workspace_id=self.config.video_workspace_id,
                    adapter_version=WAN_ADAPTER_VERSION,
                    endpoint_profile=WAN_ENDPOINT_PROFILE,
                    provider_prompt=WAN_PROMPT_PREFIX + parsed.prompt,
                    parameters=WanParameters(),
                )
                extra = {
                    "intent_fingerprint": frozen.intent_fingerprint(),
                    "download_policy": self.download_policy,
                    "cost": CostRecord(
                        estimate=CostEstimate(
                            **self.price.model_dump(), max_job_cost=self.config.video_max_job_cost
                        )
                    ),
                }
            job = (JobV2 if live else Job)(
                id=str(uuid4()),
                context=context,
                operation_key=context.operation_key,
                request=frozen,
                request_fingerprint=frozen.fingerprint(),
                capabilities=capabilities,
                mode=capabilities.mode,
                policy=live_polling_policy() if live else self.policy,
                created_at=timestamp,
                updated_at=timestamp,
                **extra,
            )
            draft["jobs"][job.id] = job.model_dump(mode="json", by_alias=True)
            return self._registration(job)

        try:
            with self.store.locked():
                # Scope is checked even for a replay, whose mutation callback is skipped.
                _check_context(self.store.snapshot(), context)
                result = self.store.operation(context.operation_key, "video_generate", args, register)
            if result.get("ok"):
                self.publish(self.get(result["data"]["jobId"], project_id=context.project_id))
            return result
        except AppError as error:
            return failure(error.code, str(error))

    def publish(self, job: Job) -> None:
        # Notifications are advisory and only follow a successful durable commit.
        # A display/notification failure must not turn an accepted write into an error.
        if self.on_change:
            with suppress(Exception):
                self.on_change(job)

    @staticmethod
    def _registration(job: Job) -> dict:
        return {
            "jobId": job.id,
            "status": job.status,
            "mode": job.mode,
            "simulated": job.mode == "mock",
            "mediaAvailable": False,
        }

    def get(self, job_id: str, *, project_id: str | None = None) -> Job:
        """Local application lookup; Agent tools must supply their injected project ID."""
        raw = self.store.snapshot()["jobs"].get(job_id)
        if raw is None or (project_id is not None and raw["context"]["projectId"] != project_id):
            raise AppError("JOB_NOT_FOUND", "当前项目中没有这个视频任务。")
        return parse_job(raw)

    def list(self, *, project_id: str | None = None, session_id: str | None = None) -> tuple[Job, ...]:
        return tuple(
            parse_job(raw)
            for raw in self.store.snapshot()["jobs"].values()
            if (project_id is None or raw["context"]["projectId"] == project_id)
            and (session_id is None or raw["context"]["sessionId"] == session_id)
        )

    def save(self, job: Job, *, expected_revision: int) -> Job:
        def commit(draft):
            old = draft["jobs"].get(job.id)
            if old is None or old.get("revision", 0) != expected_revision:
                raise AppError("JOB_REVISION_CONFLICT", "Job 版本已变化，请重新读取。")
            draft["jobs"][job.id] = job.model_dump(mode="json", by_alias=True)
            return draft["jobs"][job.id]

        saved = parse_job(self.store.transaction(commit))
        self.publish(saved)
        return saved

    def update(self, job: Job, **changes) -> Job:
        return self.save(changed_job(job, self.timestamp(), **changes), expected_revision=job.revision)

    def retry_query(self, job_id: str, *, project_id: str) -> Job:
        job = self.get(job_id, project_id=project_id)
        if job.query_state != "paused" or not job.provider_task_id:
            raise AppError("JOB_QUERY_NOT_PAUSED", "只能恢复已有上游 ID 且查询已暂停的任务。")
        extra = {}
        if isinstance(job, JobV2):
            self.check_runtime(job)
            if self.query_deadline_passed(job):
                raise AppError(
                    "PROVIDER_TASK_UNAVAILABLE", "原任务已到达保守查询期限，不能恢复查询或重新生成。"
                )
            extra = {"queryPauseReason": None, "runtimeBlock": None}
        return self.update(
            job,
            queryState="polling",
            consecutiveQueryErrors=0,
            error=None,
            nextPollAt=self.timestamp(),
            **extra,
        )
