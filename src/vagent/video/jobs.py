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
from vagent.video.assets import AssetService, verified_reference_data
from vagent.video.contracts import (
    FAL_MAX_JOB_USD,
    FAL_MAX_RUN_USD,
    KLING_MAX_JOB_UNITS,
    KLING_MAX_JOB_USD,
    KLING_MAX_RUN_UNITS,
    KLING_MAX_RUN_USD,
    LIVE_JOBS_PER_RUN,
    MODE_FRAMES,
    WAN_ADAPTER_VERSION,
    WAN_ENDPOINT_PROFILE,
    WAN_I2V_MODEL,
    WAN_MODEL,
    WAN_PROMPT_PREFIX,
    WAN_SHARED_ENDPOINT_PROFILE,
    AmountLimit,
    CostEstimate,
    CostRecord,
    CostRecordV3,
    DownloadPolicy,
    FalParameters,
    ImageSize,
    Job,
    JobCostV6,
    JobError,
    JobErrorV2,
    JobV2,
    JobV3,
    JobV4,
    JobV5,
    JobV6,
    KlingParameters,
    LiveJob,
    MediaAsset,
    PollingPolicy,
    ProviderActualCost,
    ProviderPrice,
    RunLimitsSnapshot,
    RunLimitsSnapshotV2,
    ShotGenerationArgs,
    ShotMediaInput,
    ShotRedoArgs,
    ShotRef,
    ShotSpecV3,
    VideoCapabilities,
    VideoCapabilitiesV4,
    VideoIntentV2,
    VideoIntentV6,
    VideoPrice,
    VideoProviderAdapter,
    VideoRequest,
    VideoRequestV2,
    VideoRequestV3,
    VideoRequestV4,
    VideoRequestV5,
    VideoRequestV6,
    WanParameters,
    WanParametersV3,
    canonical_fingerprint,
    job_transition_allowed,
    live_job_transition_allowed,
    live_polling_policy,
    parse_job,
    provider_price,
    resource_estimate,
    shot_price,
    validate_video_request,
    wan_price,
)
from vagent.video.storyboards import (
    Asset,
    ShotVersion,
    latest_shot_version,
    shot_of,
    stored_request_fingerprint,
    storyboard_in_state,
    with_shot_versions,
)
from vagent.waiting import ToolExecutionContext

# The first tool set that may register more than one Job inside a single Run.
M2_TOOLS_VERSION = 4

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
    if isinstance(job, LiveJob):
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


def _operation_registers(operation, job_id: str) -> bool:
    """A single-shot or batch result is the only thing that may register one Job."""
    if not operation or operation["result"].get("ok") is not True:
        return False
    data = operation["result"].get("data")
    if not isinstance(data, dict):
        return False
    if data.get("jobId") == job_id:
        return True
    shots = data.get("shots")
    return isinstance(shots, list) and any(
        isinstance(item, dict) and item.get("jobId") == job_id for item in shots
    )


def _adapter_capabilities(adapter) -> tuple[tuple, tuple]:
    """Split one adapter's published M2 entries from its frozen legacy entry."""
    declared = getattr(adapter, "shot_capabilities", None)
    if declared is None:
        return (adapter.capabilities(),), ()
    return tuple(declared()), (adapter.capabilities(),)


def _generation_tools_version(run: dict | None) -> int:
    feature = ((run or {}).get("toolFeatures") or {}).get("video") or {}
    version = feature.get("toolsVersion")
    return version if type(version) is int else 0


def _validate_wait_members(state: dict) -> None:
    """A jobset wait must cite existing Jobs of its own project, once each."""
    from vagent.waiting import parse_wait

    for raw in state["waits"].values():
        binding = parse_wait(raw)
        job_ids = getattr(binding, "job_ids", ())
        if not job_ids:
            continue
        if binding.resource.kind != "jobset" or len(set(job_ids)) != len(job_ids):
            raise AppError("WAIT_CONFLICT", "集合等待的成员定义不合法。")
        for job_id in job_ids:
            job = state["jobs"].get(job_id)
            if job is None or job["context"]["projectId"] != binding.context.project_id:
                raise AppError("WAIT_CONFLICT", "集合等待只能等待本项目的视频任务。")


def money(value) -> str:
    return format(Decimal(value), ".2f")


def headless_plan_fingerprint(plan: dict) -> str:
    """The canonical plan identity: fixed requests only, no local paths or times."""
    return canonical_fingerprint(
        {
            "mode": plan["mode"],
            "provider": plan["provider"],
            "storyboardId": plan["storyboardId"],
            "storyboardVersion": plan["storyboardVersion"],
            "storyboardRevision": plan["storyboardRevision"],
            "shots": [
                {"shotId": row["shotId"], "order": row["order"], "request": row["request"]}
                for row in plan["shots"]
            ],
        }
    )


def _preview_context(project_id: str) -> ToolExecutionContext:
    """A stand-in context for read-only previews: it owns no Jobs, so usage is zero."""
    return ToolExecutionContext(
        project_id=project_id,
        session_id=project_id,
        run_id=str(uuid4()),
        model_step=1,
        tool_call_id="preview",
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
        elif isinstance(job, LiveJob) and job.download and job.download.phase == "writing":
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
    owned: dict[str, list[Job]] = {}
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
        if job.id != job_id:
            raise AppError("JOB_CONFLICT", "Job ID 不匹配。")
        owned.setdefault(job.context.run_id, []).append(job)
        validate_video_request(
            job.request, job.capabilities, project_id=job.context.project_id, artifacts=state["artifacts"]
        )
        if isinstance(job, LiveJob) and state["runs"][job.context.run_id].get("videoMode") != "live":
            raise AppError("JOB_CONTEXT_INVALID", "真实视频 Job 必须归属原 live Run。")
        if not _operation_registers(state["operations"].get(job.operation_key), job_id):
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
        transition = live_job_transition_allowed if isinstance(job, LiveJob) else job_transition_allowed
        terminal = {"failed", "unknown"} if isinstance(job, LiveJob) else {"succeeded", "failed", "unknown"}
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
        if isinstance(job, LiveJob):
            if (
                job.intent_fingerprint != old.intent_fingerprint
                or job.cost.estimate != old.cost.estimate
                or job.download_policy != old.download_policy
                or (isinstance(job, JobV6) and getattr(old, "shot_ref", None) != job.shot_ref)
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
                or (
                    isinstance(job, JobV6)
                    and old.result_source is not None
                    and job.result_source != old.result_source
                )
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
    for run_id, jobs_of_run in owned.items():
        # Pre-M2 Runs keep the frozen single-Job rule; only M2 generation Runs may
        # register a bounded batch. The ceiling is re-checked here so a service-level
        # regression cannot commit an over-budget batch.
        if len(jobs_of_run) > 1 and _generation_tools_version(state["runs"].get(run_id)) < M2_TOOLS_VERSION:
            raise AppError("JOB_CONFLICT", "非 M2 的 Run 每个最多登记一个视频任务。")
        if len([job for job in jobs_of_run if job.mode == "live"]) > LIVE_JOBS_PER_RUN:
            raise AppError("JOB_CONFLICT", "同一 Run 登记的新真实视频任务超过冻结上限。")
    if any(key != value["id"] for key, value in state["waits"].items()):
        raise AppError("WAIT_CONFLICT", "等待记录 ID 不匹配。")
    _validate_wait_members(state)
    validate_media_changes(state.get("media", {}), state)


def validate_media_changes(previous: dict[str, dict], state: dict) -> None:
    media = state.get("media", {})
    if previous.keys() - media.keys() or any(media[key] != raw for key, raw in previous.items()):
        raise AppError("MEDIA_IMMUTABLE", "已交付媒体的身份、来源和完整性记录不可删除或修改。")
    from vagent.video.films import FilmMedia, verify_film_media

    seen_jobs = set()
    for media_id, raw in media.items():
        if raw.get("kind") == "film":
            # The film branch owns its own invariants: it never fakes a generation Job.
            film_media = FilmMedia.model_validate(raw)
            if film_media.id != media_id:
                raise AppError("MEDIA_CONFLICT", "成片媒体 ID 不匹配。")
            verify_film_media(state, film_media)
            continue
        asset = MediaAsset.model_validate(raw)
        job_raw = state["jobs"].get(asset.job_id)
        job = parse_job(job_raw) if job_raw else None
        if (
            asset.id != media_id
            or asset.job_id in seen_jobs
            or not isinstance(job, LiveJob)
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
        # Both single-shot live contract versions carry the frozen media-commit duty;
        # the generic v6 contract keeps it for every delivered clip.
        single_shot = raw.get("contractVersion", 1) in {2, 4, 6}
        if single_shot and raw["status"] == "succeeded" and raw["id"] not in seen_jobs:
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
        self.assets = AssetService(store)
        from vagent.video.film_service import FilmService

        # Local assembly shares this service's store and media index; it never
        # registers a Job or calls a provider.
        self.films = FilmService(self)
        self._adapters = {}
        self._capabilities = {}
        self._published = []
        for adapter in adapters:
            published, legacy = _adapter_capabilities(adapter)
            self._published.extend(
                (capabilities.provider, capabilities.model, capabilities.capabilities_version)
                for capabilities in published
            )
            for capabilities in (*published, *legacy):
                key = (capabilities.provider, capabilities.model, capabilities.capabilities_version)
                if key in self._adapters:
                    raise ValueError("A provider/model/capabilities version can only be registered once")
                self._adapters[key] = adapter
                self._capabilities[key] = capabilities

    def timestamp(self) -> str:
        value = self.clock()
        if value.utcoffset() is None:
            raise ValueError("Job clocks must return timezone-aware datetimes")
        return value.astimezone(UTC).isoformat()

    def capabilities(self, mode: str | None = None) -> tuple[VideoCapabilities, ...]:
        """Only the entries this build publishes; legacy ones stay registered for old Jobs."""
        return tuple(
            self._capabilities[key]
            for key in sorted(self._published)
            if mode is None or self._capabilities[key].mode == mode
        )

    def _capability_for(self, provider: str, model: str, capabilities_version: str):
        """Resolve one frozen capability snapshot, or report a changed or missing model."""
        entry = self._capabilities.get((provider, model, capabilities_version))
        if entry is not None:
            return entry
        # A known model at another version is a capability change, not a missing model.
        return next(
            (
                capabilities
                for (known_provider, known_model, _), capabilities in self._capabilities.items()
                if (known_provider, known_model) == (provider, model)
            ),
            None,
        )

    def provider_key(self, provider: str = "wan") -> str | None:
        """The saved Key of one provider; providers never share credentials."""
        if self.config is None:
            return None
        return {
            "wan": self.config.video_api_key,
            "kling": getattr(self.config, "kling_api_key", None),
            "fal": getattr(self.config, "fal_api_key", None),
        }.get(provider)

    def check_live_configuration(self, provider: str = "wan") -> None:
        # K1 keys-only track: the video Key alone is a complete live configuration;
        # a workspace stays optional and selects the dedicated endpoint at registration.
        if self.config is None or not self.provider_key(provider):
            if provider == "wan":
                raise AppError("VIDEO_KEY_MISSING", "请配置视频服务 Key，文本模型 Key 不能代替视频 Key。")
            raise AppError("VIDEO_KEY_MISSING", f"请配置 {provider} 视频服务 Key。")

    def configuration_status(self, provider: str = "wan") -> dict:
        try:
            self.check_live_configuration(provider)
            if provider == "wan":
                if self.price is None:
                    raise AppError("VIDEO_PRICE_UNKNOWN", "缺少已核实的视频价格，未创建或提交任务。")
                if Decimal(self.price.amount) > Decimal(self.config.video_max_job_cost):
                    raise AppError("VIDEO_COST_LIMIT", "视频估算超过单 Job 金额上限，未创建或提交任务。")
            elif self._capability_for_provider(provider) is None:
                raise AppError("VIDEO_MODEL_UNAVAILABLE", "缺少这个供应商的能力表，未创建任务。")
            # Kling and fal publish no verified zero-cost auth probe; saved keys stay
            # explicitly unverified instead of running a random paid task.
            return {"configured": True, "permissionStatus": "unverified", "error": None}
        except AppError as error:
            return {
                "configured": False,
                "permissionStatus": "unverified",
                "error": {"code": error.code, "message": str(error)},
            }

    def provider_statuses(self) -> list[dict]:
        return [
            {"provider": provider, **self.configuration_status(provider)}
            for provider in ("wan", "kling", "fal")
        ]

    def _capability_for_provider(self, provider: str):
        return next(
            (capabilities for capabilities in self.capabilities("live") if capabilities.provider == provider),
            None,
        )

    def default_adapter(self, provider: str, model: str) -> VideoProviderAdapter:
        """The adapter serving one provider/model pair, whatever its entry version."""
        entry = next(
            (
                adapter
                for (known, known_model, _), adapter in self._adapters.items()
                if (known, known_model) == (provider, model)
            ),
            None,
        )
        if entry is None:
            raise AppError("VIDEO_PROVIDER_UNAVAILABLE", "缺少这个模型对应的视频适配器。")
        return entry

    def adapter_for(self, job: Job) -> VideoProviderAdapter:
        key = (job.request.provider, job.request.model, job.request.capabilities_version)
        if self._capabilities.get(key) != job.capabilities:
            raise AppError("VIDEO_PROVIDER_UNAVAILABLE", "缺少任务原有模式和能力版本的视频适配器。")
        return self._adapters[key]

    def check_runtime(self, job: Job, *, submit=False) -> VideoProviderAdapter:
        adapter = self.adapter_for(job)
        if isinstance(job, JobV6):
            self.check_live_configuration(job.request.provider)
            if hasattr(adapter, "check_configuration"):
                adapter.check_configuration(job.request)
            if submit:
                # The v6 quote and ceiling are frozen at registration; a changed
                # capability entry surfaces as a missing adapter/price instead.
                expected = provider_price(job.capabilities.rate, job.request.spec.duration_seconds)
                if expected != job.cost.estimate:
                    raise AppError("VIDEO_PRICE_CHANGED", "视频价格版本已变化，保留原报价并暂停提交。")
            return adapter
        if isinstance(job, LiveJob):
            self.check_live_configuration()
            models = (WAN_MODEL, WAN_I2V_MODEL) if isinstance(job, JobV3) else (self.config.video_model,)
            if job.request.endpoint_profile == WAN_SHARED_ENDPOINT_PROFILE:
                # Keys-only Jobs fix the shared endpoint by recorded form: recovery
                # never depends on the workspace configuration.
                if (job.request.region, job.request.provider) != (
                    self.config.video_region,
                    self.config.video_provider,
                ) or job.request.model not in models:
                    raise AppError("VIDEO_PROVIDER_UNAVAILABLE", "缺少原任务地域和模型的匹配配置。")
            elif (job.request.workspace_id, job.request.region, job.request.provider) != (
                self.config.video_workspace_id,
                self.config.video_region,
                self.config.video_provider,
            ) or job.request.model not in models:
                raise AppError("VIDEO_PROVIDER_UNAVAILABLE", "缺少原任务业务空间、地域和模型的匹配配置。")
            if hasattr(adapter, "check_configuration"):
                adapter.check_configuration(job.request)
            if submit:
                # M2 shot Jobs freeze their quote with their capability entry, so a
                # changed rate surfaces as a missing capability/adapter instead.
                expected = (
                    shot_price(job.capabilities.price, job.request.spec.duration_seconds)
                    if isinstance(job, JobV3)
                    else self.price
                )
                if expected is None:
                    raise AppError("VIDEO_PRICE_UNKNOWN", "缺少已核实的视频价格，原任务保持待提交。")
                if expected.model_dump() != job.cost.estimate.model_dump(exclude={"max_job_cost"}):
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
            isinstance(job, LiveJob)
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
        live = args.get("provider") in {"wan", "kling", "fal"}
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
            parsed = (VideoIntentV6 if live else VideoRequest).model_validate(args)
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
                    if isinstance(existing, JobV2) and isinstance(parsed, (VideoIntentV2, VideoIntentV6))
                    else not isinstance(existing, JobV2)
                    and existing.request_fingerprint == parsed.fingerprint()
                )
                if not same:
                    raise AppError(
                        "JOB_ALREADY_EXISTS", f"本次 Run 已登记视频任务 {existing.id}，不能再创建不同请求。"
                    )
                return self._registration(existing)
            capabilities = self._capability_for(parsed.provider, parsed.model, parsed.capabilities_version)
            if capabilities is None:
                raise AppError("VIDEO_MODEL_UNAVAILABLE", "所选视频模型不在当前能力表中。")
            mode = draft["runs"][context.run_id].get("videoMode")
            if capabilities.mode != mode and not (mode is None and capabilities.mode == "mock"):
                raise AppError("VIDEO_MODEL_UNAVAILABLE", "原 Run 的视频模式不开放所选模型。")
            if isinstance(capabilities, VideoCapabilitiesV4):
                if (
                    parsed.provider,
                    parsed.model,
                    parsed.capabilities_version,
                ) != (
                    capabilities.provider,
                    capabilities.model,
                    capabilities.capabilities_version,
                ):
                    raise AppError("VIDEO_CAPABILITIES_CHANGED", "视频能力版本已变化，请重新读取能力表。")
                for reference in parsed.source_refs:
                    source = draft["artifacts"].get(reference.artifact_id)
                    if source is None or source["projectId"] != context.project_id:
                        raise AppError("SOURCE_NOT_FOUND", "当前项目中没有所引用的源产物。")
                    if not any(item["version"] == reference.version for item in source["versions"]):
                        raise AppError("SOURCE_VERSION_NOT_FOUND", "源产物版本不存在，未创建任务。")
            else:
                validate_video_request(
                    parsed, capabilities, project_id=context.project_id, artifacts=draft["artifacts"]
                )
            timestamp = self.timestamp()
            extra = {}
            frozen = parsed
            job_class = Job
            if live:
                self.check_live_configuration(parsed.provider)
                if isinstance(capabilities, VideoCapabilitiesV4):
                    if len(parsed.prompt) > capabilities.max_prompt_characters:
                        raise AppError("VIDEO_PROMPT_TOO_LONG", "视频提示词正文超过供应商上限，未创建任务。")
                    frozen = self._freeze_provider_request(parsed, capabilities)
                    limits = self._single_limits(draft, context, capabilities, frozen)
                    job_class = JobV6
                    extra = {
                        "intent_fingerprint": frozen.intent_fingerprint(),
                        "download_policy": self.download_policy,
                        "cost": self._job_cost_v6(capabilities, frozen, limits),
                    }
                else:
                    if len(parsed.prompt) > 4991:
                        raise AppError(
                            "VIDEO_PROMPT_TOO_LONG", "视频提示词正文最多 4991 个字符，未创建任务。"
                        )
                    if self.price is None or self.price != capabilities.price:
                        raise AppError(
                            "VIDEO_PRICE_UNKNOWN", "缺少与当前视频能力匹配的已核实价格，未创建任务。"
                        )
                    if Decimal(self.price.amount) > Decimal(self.config.video_max_job_cost):
                        raise AppError("VIDEO_COST_LIMIT", "视频估算超过单 Job 金额上限，未创建任务。")
                    if self.config.video_workspace_id:
                        frozen = VideoRequestV2(
                            **parsed.model_dump(mode="json", by_alias=True),
                            region=self.config.video_region,
                            workspace_id=self.config.video_workspace_id,
                            adapter_version=WAN_ADAPTER_VERSION,
                            endpoint_profile=WAN_ENDPOINT_PROFILE,
                            provider_prompt=WAN_PROMPT_PREFIX + parsed.prompt,
                            parameters=WanParameters(),
                        )
                    else:
                        frozen = VideoRequestV4(
                            **parsed.model_dump(mode="json", by_alias=True),
                            region=self.config.video_region,
                            adapter_version=WAN_ADAPTER_VERSION,
                            endpoint_profile=WAN_SHARED_ENDPOINT_PROFILE,
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
                    job_class = JobV4 if isinstance(frozen, VideoRequestV4) else JobV2
            job = job_class(
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

    def shot_generate(self, args: dict, *, context: ToolExecutionContext, authorization=None) -> dict:
        """Register one real Job per confirmed shot; any failure registers nothing.

        The per-shot request, the reference-image proof and the quota snapshot are
        all derived inside a single Store transaction, so one call either matches
        one frozen storyboard version completely or writes nothing at all. Headless
        delivery passes the persisted authorization so the same transaction also
        re-proves the plan fingerprint and every budget bucket.
        """
        parsed = self._generation_args(ShotGenerationArgs, args)
        if isinstance(parsed, dict):
            return parsed
        return self._register_batch(
            context, parsed, "shot_generate", context.operation_key, redo=False, authorization=authorization
        )

    def redo_shot(self, args: dict, *, context: ToolExecutionContext) -> dict:
        """Explicit user redo: a new ShotVersion plus one new Job, nothing else moves."""
        parsed = self._generation_args(ShotRedoArgs, args)
        if isinstance(parsed, dict):
            return parsed
        # The journal key must be the Job's own operation key, so the Store can prove
        # every Job came from one recorded successful call. A caller expresses
        # idempotency with one context per client request: repeating it replays that
        # call's original result instead of registering a second Job.
        return self._register_batch(context, parsed, "shot_redo", context.operation_key, redo=True)

    def generation_mode(self) -> str:
        """The active video mode; only mock and live can register shot Jobs."""
        mode = getattr(self.config, "video_mode", "off")
        if mode not in {"mock", "live"}:
            raise AppError("VIDEO_MODEL_UNAVAILABLE", "当前视频模式不开放镜头生成。")
        return mode

    def _shot_estimate(self, capabilities, request) -> CostEstimate | ProviderPrice:
        """The frozen quote of one shot: the verified rate times its own duration."""
        if isinstance(capabilities, VideoCapabilitiesV4):
            return provider_price(capabilities.rate, request.spec.duration_seconds)
        return CostEstimate(
            **shot_price(capabilities.price, request.spec.duration_seconds).model_dump(),
            max_job_cost=self.config.video_max_job_cost,
        )

    def preview_shots(self, project_id: str, args: dict) -> dict:
        """Read-only estimate preview for the local confirmation dialog.

        It derives exactly the requests a registration would freeze and applies the
        same ceilings, but assumes a fresh Run: nothing is registered, submitted or
        written, and reused shots are reported as reused.
        """
        parsed = self._generation_args(ShotGenerationArgs, args)
        if isinstance(parsed, dict):
            raise AppError(parsed["error"]["code"], parsed["error"]["message"])
        state = self.store.snapshot()
        mode = self.generation_mode()
        live = mode == "live"
        if live:
            self.check_live_configuration(self.config.video_provider)
        record = storyboard_in_state(state, project_id, parsed.storyboard_id)
        version = parsed.storyboard_version
        if record.current_version != version:
            raise AppError("VERSION_CONFLICT", "分镜版本已变化，请重新读取后再生成。")
        plan, rows = [], []
        for shot_id in parsed.shot_ids:
            shot = shot_of(record, version, shot_id)
            capabilities, request = self._derive_shot(state, record, shot, live=live)
            previous = latest_shot_version(record, version, shot_id)
            reuse = (
                previous is not None and stored_request_fingerprint(previous.request) == request.fingerprint()
            )
            estimate = self._shot_estimate(capabilities, request) if live else None
            if not reuse:
                plan.append((shot, capabilities, request))
            rows.append(
                {
                    "shotId": shot.id,
                    "order": shot.order,
                    "mode": shot.mode,
                    "durationSeconds": shot.duration_seconds,
                    "reused": reuse,
                    "jobId": previous.job_id if reuse else None,
                    "estimate": estimate.model_dump(mode="json", by_alias=True) if estimate else None,
                }
            )
        limits = self._batch_limits(state, _preview_context(project_id), plan) if live else None
        total = sum((Decimal(row["estimate"]["amount"]) for row in rows if row["estimate"]), Decimal(0))
        currency = next((row["estimate"]["currency"] for row in rows if row["estimate"]), "CNY")
        return {
            "storyboardId": record.id,
            "storyboardVersion": version,
            "storyboardRevision": record.revision,
            "mode": mode,
            "simulated": not live,
            "shots": rows,
            "totalEstimate": {"currency": currency, "amount": money(total)} if live else None,
            "limits": limits.model_dump(mode="json", by_alias=True) if limits else None,
        }

    @staticmethod
    def _generation_args(model, args):
        if not isinstance(args, dict):
            return failure("INVALID_ARGUMENTS", "生成参数不符合 Schema，未创建任务。")
        try:
            return model.model_validate(args)
        except (ValidationError, ValueError, TypeError):
            return failure("INVALID_ARGUMENTS", "生成参数不符合 Schema，未创建任务。")

    def plan_shots(self, project_id: str, storyboard_id: str, storyboard_version: int) -> dict:
        """Derive the frozen requests and quotes of one storyboard without writing.

        This is the headless planning primitive: no Job, Operation, revision or
        provider call is created. Registration later re-derives the same requests in
        its own transaction and compares them to this frozen plan before writing.
        """
        state = self.store.snapshot()
        mode = self.generation_mode()
        live = mode == "live"
        if live:
            self.check_live_configuration(self.config.video_provider)
        record = storyboard_in_state(state, project_id, storyboard_id)
        if record.current_version != storyboard_version:
            raise AppError("VERSION_CONFLICT", "分镜版本已变化，请重新读取后再生成。")
        version = next(item for item in record.versions if item.version == storyboard_version)
        rows, buckets = [], {}
        for shot in sorted(version.shots, key=lambda item: item.order):
            capabilities, request = self._derive_shot(state, record, shot, live=live)
            estimate = self._shot_estimate(capabilities, request) if live else None
            resource = (
                resource_estimate(capabilities.resource, request.spec.duration_seconds)
                if live and getattr(capabilities, "resource", None) is not None
                else None
            )
            previous = latest_shot_version(record, storyboard_version, shot.id)
            reuse = (
                previous is not None and stored_request_fingerprint(previous.request) == request.fingerprint()
            )
            provider_rate = getattr(capabilities, "rate", None)
            rows.append(
                {
                    "shotId": shot.id,
                    "order": shot.order,
                    "mode": shot.mode,
                    "durationSeconds": shot.duration_seconds,
                    "provider": capabilities.provider,
                    "model": capabilities.model,
                    "capabilitiesVersion": capabilities.capabilities_version,
                    "endpointProfile": getattr(capabilities, "endpoint_profile", None),
                    "adapterVersion": getattr(capabilities, "adapter_version", None),
                    "priceVersion": (
                        provider_rate.price_version
                        if provider_rate is not None
                        else getattr(getattr(capabilities, "price", None), "price_version", None)
                    ),
                    "sourcePolicyVersion": getattr(capabilities, "source_policy_version", None),
                    "request": request.model_dump(mode="json", by_alias=True),
                    "requestFingerprint": request.fingerprint(),
                    "estimate": estimate.model_dump(mode="json", by_alias=True) if estimate else None,
                    "resource": resource.model_dump(mode="json", by_alias=True) if resource else None,
                    "reused": reuse,
                    "jobId": previous.job_id if reuse else None,
                }
            )
            if reuse or not live:
                continue
            key = (capabilities.provider, estimate.currency)
            bucket = buckets.setdefault(
                key,
                {
                    "provider": capabilities.provider,
                    "currency": estimate.currency,
                    "amount": "0.00",
                    "units": 0 if resource is not None else None,
                    "jobCount": 0,
                    "shots": [],
                },
            )
            bucket["amount"] = money(Decimal(bucket["amount"]) + Decimal(estimate.amount))
            if resource is not None:
                bucket["units"] += resource.units
            bucket["jobCount"] += 1
            bucket["shots"].append(shot.id)
        ceilings = []
        for provider, currency in sorted(buckets, key=lambda item: (item[0], item[1])):
            max_job_amount, max_run_amount, max_job_units, max_run_units = self._provider_ceiling(provider)
            ceilings.append(
                {
                    "provider": provider,
                    "currency": currency,
                    "maxJobAmount": money(max_job_amount),
                    "maxRunAmount": money(max_run_amount),
                    "maxJobUnits": max_job_units,
                    "maxRunUnits": max_run_units,
                }
            )
        return {
            "mode": mode,
            "provider": self.config.video_provider,
            "storyboardId": record.id,
            "storyboardVersion": storyboard_version,
            "storyboardRevision": record.revision,
            "shots": rows,
            "newJobCount": sum(1 for row in rows if not row["reused"]),
            "buckets": [buckets[key] for key in sorted(buckets, key=lambda item: (item[0], item[1]))],
            "limits": {
                "maxJobs": min(int(self.config.video_max_new_jobs_per_run), LIVE_JOBS_PER_RUN),
                "buckets": ceilings,
            },
        }

    def budget_ceiling(self, provider: str):
        """The effective per-provider ceilings after configured tightening."""
        return self._provider_ceiling(provider)

    def _register_batch(self, context, parsed, name, key, *, redo: bool, authorization=None) -> dict:
        raw = parsed.model_dump(mode="json", by_alias=True)

        def register(draft):
            return self._register_generation(draft, context, parsed, redo=redo, authorization=authorization)

        try:
            with self.store.locked():
                # Scope is checked even for a replay, whose mutation callback is skipped.
                _check_context(self.store.snapshot(), context)
                result = self.store.operation(key, name, raw, register)
            if result.get("ok"):
                for entry in result["data"]["shots"]:
                    self.publish(self.get(entry["jobId"], project_id=context.project_id))
            return result
        except AppError as error:
            return failure(error.code, str(error))

    def _register_generation(self, draft, context, parsed, *, redo: bool, authorization=None) -> dict:
        _check_context(draft, context)
        run = draft["runs"][context.run_id]
        if run.get("readOnly", False):
            raise AppError("READ_ONLY", "只读 Run 不能创建视频任务。")
        mode = run.get("videoMode")
        if mode not in {"mock", "live"}:
            raise AppError("VIDEO_MODEL_UNAVAILABLE", "原 Run 的视频模式不开放镜头生成。")
        record = storyboard_in_state(draft, context.project_id, parsed.storyboard_id)
        version = parsed.storyboard_version
        if record.current_version != version:
            raise AppError("VERSION_CONFLICT", "分镜版本已变化，请重新读取后再生成。")
        if redo:
            if record.revision != parsed.expected_revision:
                raise AppError("VERSION_CONFLICT", "分镜记录已变化，请重新读取后再重做。")
            order = [parsed.shot_id]
        else:
            if not parsed.confirm:
                raise AppError(
                    "CONFIRMATION_REQUIRED", "批量生成需要显式确认：先展示逐镜头与合计估算，再提交。"
                )
            order = list(parsed.shot_ids)
        live = mode == "live"
        if live:
            self.check_live_configuration(self.config.video_provider)
        plan, reused = [], {}
        for shot_id in order:
            shot = shot_of(record, version, shot_id)
            capabilities, request = self._derive_shot(draft, record, shot, live=live)
            previous = None if redo else latest_shot_version(record, version, shot_id)
            if previous is not None and stored_request_fingerprint(previous.request) == request.fingerprint():
                reused[shot_id] = previous
            else:
                plan.append((shot, capabilities, request))
        limits = self._batch_limits(draft, context, plan) if live else None
        if authorization is not None:
            self._check_headless_authorization(
                draft, context, record, version, order, plan, reused, limits, authorization
            )
        return self._commit_generation(
            draft,
            context,
            record,
            version,
            order,
            plan,
            reused,
            limits,
            headless=authorization is not None,
        )

    def _check_headless_authorization(
        self, draft, context, record, version, order, plan, reused, limits, authorization
    ) -> None:
        """Re-prove the frozen plan and every budget inside the registration transaction."""
        run = draft["runs"][context.run_id]
        headless = run.get("headless") or {}
        frozen = headless.get("plan")
        stored = headless.get("authorization")
        if not frozen or not stored or headless.get("status") != "authorized":
            raise AppError(
                "HEADLESS_AUTHORIZATION_REQUIRED", "这个 Run 没有已保存的 headless 授权，未登记任何任务。"
            )
        if headless.get("planFingerprint") != headless_plan_fingerprint(frozen):
            raise AppError("HEADLESS_PLAN_CHANGED", "计划内容与已保存指纹不一致，未登记任何任务。")
        if authorization.get("fingerprint") != stored.get("fingerprint"):
            raise AppError("HEADLESS_AUTHORIZATION_MISMATCH", "授权指纹不匹配，未登记任何任务。")
        if (
            frozen["storyboardId"] != record.id
            or frozen["storyboardVersion"] != version
            or frozen["storyboardRevision"] != record.revision
        ):
            raise AppError("HEADLESS_PLAN_STALE", "分镜在计划后已变化，请重新规划并授权。")
        rows = {row["shotId"]: row for row in frozen["shots"]}
        if set(rows) != set(order):
            raise AppError("HEADLESS_PLAN_CHANGED", "请求集合与已授权计划不一致，未登记任何任务。")
        for shot, capabilities, request in plan:
            row = rows[shot.id]
            if row["reused"]:
                raise AppError("HEADLESS_PLAN_CHANGED", "已授权计划不包含这个新任务，未登记任何任务。")
            if (
                request.fingerprint() != row["requestFingerprint"]
                or capabilities.provider != row["provider"]
                or capabilities.capabilities_version != row["capabilitiesVersion"]
            ):
                raise AppError("HEADLESS_PLAN_CHANGED", "能力、价格或请求在计划后已变化，请重新规划。")
        for shot_id, previous in reused.items():
            row = rows.get(shot_id)
            if row is None or not row["reused"] or row["jobId"] != previous.job_id:
                raise AppError("HEADLESS_PLAN_CHANGED", "复用结果与已授权请求集合不一致，未登记任何任务。")
        if len(plan) != frozen["newJobCount"]:
            raise AppError("HEADLESS_PLAN_CHANGED", "新任务数量与已授权计划不一致，未登记任何任务。")
        if limits is None:
            # Mock registration is local and free; it has no paid bucket snapshot.
            if len(plan) + len(reused) != len(order):
                raise AppError("HEADLESS_PLAN_CHANGED", "请求集合与已授权计划不一致，未登记任何任务。")
            if len(plan) > int(authorization["maxJobs"]):
                raise AppError("HEADLESS_BUDGET_NOT_COVERED", "新任务数量超过授权上限，整批未登记。")
            return
        if limits.used_new_jobs > int(authorization["maxJobs"]):
            raise AppError("HEADLESS_BUDGET_NOT_COVERED", "新任务数量超过授权上限，整批未登记。")
        authorized_cost = {
            (item["provider"], item["currency"]): item["amount"] for item in authorization["maxCost"]
        }
        authorized_units = {item["provider"]: item["units"] for item in authorization["maxUnits"]}
        if not isinstance(limits, RunLimitsSnapshotV2):
            # Wan keeps its frozen single-currency snapshot (CNY only).
            for expected in frozen["buckets"]:
                key = (expected["provider"], expected["currency"])
                if key[0] != "wan" or Decimal(limits.used_amount) != Decimal(expected["amount"]):
                    raise AppError("HEADLESS_BUDGET_NOT_COVERED", "金额使用与已授权计划不一致，整批未登记。")
                if Decimal(limits.used_amount) > Decimal(authorized_cost.get(key, "0")):
                    raise AppError("HEADLESS_BUDGET_NOT_COVERED", "金额超过授权上限，整批未登记。")
            return
        by_bucket = {(bucket.provider, bucket.currency): bucket for bucket in limits.buckets}
        for expected in frozen["buckets"]:
            key = (expected["provider"], expected["currency"])
            bucket = by_bucket.get(key)
            if bucket is None or Decimal(bucket.used_amount) != Decimal(expected["amount"]):
                raise AppError("HEADLESS_BUDGET_NOT_COVERED", "金额使用与已授权计划不一致，整批未登记。")
            if Decimal(bucket.used_amount) > Decimal(authorized_cost.get(key, "0")):
                raise AppError("HEADLESS_BUDGET_NOT_COVERED", "金额超过授权上限，整批未登记。")
            if expected["units"] is None:
                continue
            if bucket.used_units != expected["units"]:
                raise AppError("HEADLESS_BUDGET_NOT_COVERED", "资源单位使用与计划不一致，整批未登记。")
            if bucket.used_units > int(authorized_units.get(key[0], -1)):
                raise AppError("HEADLESS_BUDGET_NOT_COVERED", "资源单位超过授权上限，整批未登记。")

    def _shot_capability(self, mode: str, *, live: bool):
        """The published entry that verifies this shot mode for the Run's provider."""
        expected = "live" if live else "mock"
        provider = self.config.video_provider if live else None
        for capabilities in self.capabilities():
            if capabilities.mode != expected:
                continue
            if provider is not None and capabilities.provider != provider:
                continue
            if mode in tuple(getattr(capabilities, "shot_modes", None) or ("t2v",)):
                return capabilities
        raise AppError("VIDEO_UNSUPPORTED_MODE", "当前能力表不支持这个镜头的生成模式，未创建任务。")

    def _derive_shot(self, draft, record, shot, *, live: bool):
        """Derive the frozen request of one shot without changing any state."""
        prompt = shot.description.strip()
        if not prompt:
            raise AppError("SHOT_DESCRIPTION_EMPTY", "镜头描述为空，未创建任务。")
        capabilities = self._shot_capability(shot.mode, live=live)
        if live and isinstance(capabilities, VideoCapabilitiesV4):
            # The multi-provider table publishes only text-to-video 5 s / 720p / 16:9.
            if shot.mode != "t2v":
                raise AppError("VIDEO_UNSUPPORTED_MODE", "当前供应商只开放文生视频，未创建任务。")
            spec = ShotSpecV3(mode="t2v", duration_seconds=shot.duration_seconds, aspect_ratio="16:9")
            if spec not in capabilities.specs:
                raise AppError(
                    "VIDEO_UNSUPPORTED_SPEC", "镜头的时长不在已核实的供应商规格组合中，未创建任务。"
                )
            if len(prompt) > capabilities.max_prompt_characters:
                raise AppError("VIDEO_PROMPT_TOO_LONG", "镜头描述超过供应商提示词上限，未创建任务。")
            request = VideoRequestV6(
                provider=capabilities.provider,
                model=capabilities.model,
                region=capabilities.region,
                endpoint_profile=capabilities.endpoint_profile,
                adapter_version=capabilities.adapter_version,
                capabilities_version=capabilities.capabilities_version,
                price_version=capabilities.price_version,
                source_policy_version=capabilities.source_policy_version,
                prompt=prompt,
                spec=spec,
                source_refs=record.source_refs,
                provider_prompt=prompt,
                parameters=KlingParameters() if capabilities.provider == "kling" else FalParameters(),
            )
            return capabilities, request
        if live:
            spec = ShotSpecV3(
                mode=shot.mode,
                duration_seconds=shot.duration_seconds,
                aspect_ratio="16:9" if shot.mode == "t2v" else None,
            )
            if spec not in capabilities.specs:
                raise AppError("VIDEO_UNSUPPORTED_SPEC", "镜头的时长不在已核实的规格组合中，未创建任务。")
            common = {
                "provider": "wan",
                "model": capabilities.model,
                "capabilities_version": capabilities.capabilities_version,
                "prompt": prompt,
                "spec": spec,
                "source_refs": record.source_refs,
                "provider_prompt": WAN_PROMPT_PREFIX + prompt,
                "parameters": WanParametersV3(
                    duration=spec.duration_seconds, ratio="16:9" if shot.mode == "t2v" else None
                ),
                "media": self._shot_media(draft, shot, capabilities),
            }
            if self.config.video_workspace_id:
                request = VideoRequestV3(**common, workspace_id=self.config.video_workspace_id)
            else:
                request = VideoRequestV5(**common)
            return capabilities, request
        if shot.mode != "t2v":
            raise AppError("VIDEO_UNSUPPORTED_MODE", "当前模拟能力不支持图生视频，未创建任务。")
        spec = next(
            (
                item
                for item in capabilities.specs
                if float(item.duration_seconds) == shot.duration_seconds
                and item.resolution == "720p"
                and item.aspect_ratio == "16:9"
            ),
            None,
        )
        if spec is None:
            raise AppError("VIDEO_UNSUPPORTED_SPEC", "镜头的时长不在当前模拟规格组合中，未创建任务。")
        return capabilities, VideoRequest(
            provider=capabilities.provider,
            model=capabilities.model,
            capabilities_version=capabilities.capabilities_version,
            prompt=prompt,
            spec=spec,
            source_refs=record.source_refs,
        )

    def _shot_media(self, draft, shot, capabilities):
        """Prove every reference image before it becomes part of a frozen request."""
        frames = MODE_FRAMES[shot.mode]
        if not frames:
            return ()
        rule = capabilities.image_input
        if rule is None:
            raise AppError("VIDEO_UNSUPPORTED_MODE", "当前能力不支持图生视频参考图，未创建任务。")
        media = []
        for frame, asset_id in zip(frames, shot.reference_asset_ids, strict=True):
            raw = draft["assets"].get(asset_id)
            if raw is None:
                raise AppError("ASSET_NOT_FOUND", "镜头参考图不存在，未创建任务。")
            asset = Asset.model_validate(raw)
            _, facts = verified_reference_data(self.store.home, asset, rule)
            media.append(
                ShotMediaInput(
                    frame=frame,
                    asset_id=asset.id,
                    mime_type=asset.mime_type,
                    sha256=asset.sha256,
                    size_bytes=asset.size_bytes,
                    size=ImageSize(width=facts.size.width, height=facts.size.height),
                )
            )
        return tuple(media)

    def _provider_ceiling(self, provider: str):
        """The effective per-provider ceilings: stored values may only tighten."""
        config = self.config
        if provider == "kling":
            return (
                min(Decimal(config.kling_max_job_cost), Decimal(KLING_MAX_JOB_USD)),
                min(Decimal(config.kling_max_run_cost), Decimal(KLING_MAX_RUN_USD)),
                min(int(config.kling_max_job_units), KLING_MAX_JOB_UNITS),
                min(int(config.kling_max_run_units), KLING_MAX_RUN_UNITS),
            )
        if provider == "fal":
            return (
                min(Decimal(config.fal_max_job_cost), Decimal(FAL_MAX_JOB_USD)),
                min(Decimal(config.fal_max_run_cost), Decimal(FAL_MAX_RUN_USD)),
                None,
                None,
            )
        return (
            Decimal(config.video_max_job_cost),
            Decimal(config.video_max_run_cost),
            None,
            None,
        )

    def _freeze_provider_request(
        self, parsed: "VideoIntentV2 | VideoIntentV6", capabilities: VideoCapabilitiesV4
    ):
        """Freeze one public intent into the generic v6 provider request."""
        if parsed.spec.resolution != "720p" or parsed.spec.aspect_ratio != "16:9":
            raise AppError("VIDEO_UNSUPPORTED_SPEC", "当前供应商只开放 720p / 16:9，未创建任务。")
        spec = ShotSpecV3(mode="t2v", duration_seconds=parsed.spec.duration_seconds, aspect_ratio="16:9")
        if spec not in capabilities.specs:
            raise AppError("VIDEO_UNSUPPORTED_SPEC", "镜头的时长不在已核实的供应商规格组合中，未创建任务。")
        return VideoRequestV6(
            provider=capabilities.provider,
            model=capabilities.model,
            region=capabilities.region,
            endpoint_profile=capabilities.endpoint_profile,
            adapter_version=capabilities.adapter_version,
            capabilities_version=capabilities.capabilities_version,
            price_version=capabilities.price_version,
            source_policy_version=capabilities.source_policy_version,
            prompt=parsed.prompt,
            spec=spec,
            source_refs=parsed.source_refs,
            provider_prompt=parsed.prompt,
            parameters=KlingParameters() if capabilities.provider == "kling" else FalParameters(),
        )

    def _single_limits(self, draft, context, capabilities, request):
        return self._v6_limits(draft, context, capabilities, [request])

    def _v6_limits(self, draft, context, capabilities, requests):
        """Enforce every provider/currency bucket and the shared Job ceiling."""
        owned = [
            parse_job(raw)
            for raw in draft["jobs"].values()
            if raw["context"]["runId"] == context.run_id and raw.get("mode") == "live"
        ]
        used_jobs = len(owned)
        ceiling_jobs = min(int(self.config.video_max_new_jobs_per_run), LIVE_JOBS_PER_RUN)
        amounts = {("wan", "CNY"): Decimal(0), ("kling", "USD"): Decimal(0), ("fal", "USD"): Decimal(0)}
        units = {"kling": 0}
        for job in owned:
            if isinstance(job, JobV6):
                key = (job.request.provider, job.cost.estimate.currency)
                amounts[key] = amounts.get(key, Decimal(0)) + Decimal(job.cost.estimate.amount)
                if job.cost.resource_estimate is not None:
                    units[job.request.provider] = (
                        units.get(job.request.provider, 0) + job.cost.resource_estimate.units
                    )
            elif isinstance(job, LiveJob):
                amounts[("wan", "CNY")] += Decimal(job.cost.estimate.amount)
        provider = capabilities.provider
        job_amount, run_amount, job_units, run_units = self._provider_ceiling(provider)
        estimates = [provider_price(capabilities.rate, request.spec.duration_seconds) for request in requests]
        new_amount = sum((Decimal(item.amount) for item in estimates), Decimal(0))
        unit_estimates = []
        if capabilities.resource is not None:
            unit_estimates = [
                resource_estimate(capabilities.resource, request.spec.duration_seconds).units
                for request in requests
            ]
        new_units = sum(unit_estimates)
        for item in estimates:
            if Decimal(item.amount) > job_amount:
                raise AppError("VIDEO_COST_LIMIT", "有镜头的估算超过单 Job 金额上限，整批未登记。")
        for per_job_units in unit_estimates:
            if job_units is not None and per_job_units > job_units:
                raise AppError("VIDEO_COST_LIMIT", "有镜头的资源单位超过单 Job 上限，整批未登记。")
        if used_jobs + len(requests) > ceiling_jobs:
            raise AppError(
                "VIDEO_RUN_JOB_LIMIT",
                f"本次 Run 最多新建 {ceiling_jobs} 个真实视频任务，整批未登记。",
            )
        key = (provider, estimates[0].currency)
        total = amounts.get(key, Decimal(0)) + new_amount
        if total > run_amount:
            raise AppError("VIDEO_RUN_COST_LIMIT", "本次 Run 的累计估算超过金额上限，整批未登记。")
        total_units = units.get(provider, 0) + new_units
        if run_units is not None and total_units > run_units:
            raise AppError("VIDEO_RUN_UNIT_LIMIT", "本次 Run 的累计资源单位超过上限，整批未登记。")
        buckets = []
        for name in ("wan", "kling", "fal"):
            amount_key = (name, "CNY" if name == "wan" else "USD")
            bucket_amount, bucket_run, bucket_job_units, bucket_run_units = self._provider_ceiling(name)
            used = amounts.get(amount_key, Decimal(0)) + (new_amount if name == provider else Decimal(0))
            used_units = (
                units.get(name, 0) + (new_units if name == provider else 0)
                if bucket_job_units is not None
                else None
            )
            buckets.append(
                AmountLimit(
                    provider=name,
                    currency=amount_key[1],
                    max_job_amount=money(bucket_amount),
                    max_run_amount=money(bucket_run),
                    used_amount=money(used),
                    remaining_amount=money(bucket_run - used),
                    max_job_units=bucket_job_units,
                    max_run_units=bucket_run_units,
                    used_units=used_units,
                    remaining_units=None if used_units is None else bucket_run_units - used_units,
                )
            )
        return RunLimitsSnapshotV2(
            max_new_jobs_per_run=ceiling_jobs,
            used_new_jobs=used_jobs + len(requests),
            buckets=tuple(buckets),
        )

    def _job_cost_v6(self, capabilities, request, limits) -> JobCostV6:
        estimate = provider_price(capabilities.rate, request.spec.duration_seconds)
        bucket = next(
            item
            for item in limits.buckets
            if item.provider == capabilities.provider and item.currency == estimate.currency
        )
        return JobCostV6(
            estimate=estimate,
            max_job_amount=bucket.max_job_amount,
            resource_estimate=(
                resource_estimate(capabilities.resource, request.spec.duration_seconds)
                if capabilities.resource is not None
                else None
            ),
            limits=limits,
            actual=ProviderActualCost(currency=estimate.currency),
        )

    def _batch_limits(self, draft, context, plan):
        """Enforce the frozen ceilings before anything is registered."""
        if plan and isinstance(plan[0][1], VideoCapabilitiesV4):
            return self._v6_limits(draft, context, plan[0][1], [request for _, _, request in plan])
        owned = [
            parse_job(raw)
            for raw in draft["jobs"].values()
            if raw["context"]["runId"] == context.run_id and raw.get("mode") == "live"
        ]
        used_jobs = len(owned)
        used_amount = sum((Decimal(job.cost.estimate.amount) for job in owned), Decimal(0))
        ceiling_jobs = min(int(self.config.video_max_new_jobs_per_run), LIVE_JOBS_PER_RUN)
        ceiling_cost = Decimal(self.config.video_max_run_cost)
        estimates = [
            shot_price(capabilities.price, request.spec.duration_seconds) for _, capabilities, request in plan
        ]
        for estimate in estimates:
            if Decimal(estimate.amount) > Decimal(self.config.video_max_job_cost):
                raise AppError("VIDEO_COST_LIMIT", "有镜头的估算超过单 Job 金额上限，整批未登记。")
        if used_jobs + len(plan) > ceiling_jobs:
            raise AppError(
                "VIDEO_RUN_JOB_LIMIT",
                f"本次 Run 最多新建 {ceiling_jobs} 个真实视频任务，整批未登记。",
            )
        total = used_amount + sum((Decimal(estimate.amount) for estimate in estimates), Decimal(0))
        if total > ceiling_cost:
            raise AppError("VIDEO_RUN_COST_LIMIT", "本次 Run 的累计估算超过金额上限，整批未登记。")
        return RunLimitsSnapshot(
            max_job_cost=self.config.video_max_job_cost,
            max_new_jobs_per_run=ceiling_jobs,
            max_run_cost=self.config.video_max_run_cost,
            used_new_jobs=used_jobs + len(plan),
            used_amount=money(total),
            remaining_amount=money(ceiling_cost - total),
        )

    def _commit_generation(
        self, draft, context, record, version, order, plan, reused, limits, *, headless=False
    ) -> dict:
        live = limits is not None
        timestamp = self.timestamp()
        entries, created = [], {}
        for shot, capabilities, request in plan:
            shot_version_id, job_id = str(uuid4()), str(uuid4())
            estimate = None
            if live:
                estimate = self._shot_estimate(capabilities, request)
                shot_ref = ShotRef(
                    storyboard_id=record.id,
                    storyboard_version=version,
                    shot_id=shot.id,
                    shot_version_id=shot_version_id,
                )
                if isinstance(request, VideoRequestV6):
                    job = JobV6(
                        id=job_id,
                        context=context,
                        operation_key=context.operation_key,
                        request=request,
                        request_fingerprint=request.fingerprint(),
                        intent_fingerprint=request.intent_fingerprint(),
                        capabilities=capabilities,
                        policy=live_polling_policy(),
                        created_at=timestamp,
                        updated_at=timestamp,
                        shot_ref=shot_ref,
                        cost=self._job_cost_v6(capabilities, request, limits),
                    )
                else:
                    job = (JobV5 if isinstance(request, VideoRequestV5) else JobV3)(
                        id=job_id,
                        context=context,
                        operation_key=context.operation_key,
                        request=request,
                        request_fingerprint=request.fingerprint(),
                        intent_fingerprint=request.intent_fingerprint(),
                        capabilities=capabilities,
                        policy=live_polling_policy(),
                        created_at=timestamp,
                        updated_at=timestamp,
                        shot_ref=shot_ref,
                        cost=CostRecordV3(estimate=estimate, limits=limits),
                    )
            else:
                job = Job(
                    id=job_id,
                    context=context,
                    operation_key=context.operation_key,
                    request=request,
                    request_fingerprint=request.fingerprint(),
                    capabilities=capabilities,
                    policy=self.policy,
                    created_at=timestamp,
                    updated_at=timestamp,
                )
            draft["jobs"][job.id] = job.model_dump(mode="json", by_alias=True)
            entries.append(
                ShotVersion(
                    id=shot_version_id,
                    shot_id=shot.id,
                    storyboard_version=version,
                    job_id=job.id,
                    request=request.model_dump(mode="json", by_alias=True),
                    estimate=estimate,
                    created_at=timestamp,
                )
            )
            created[shot.id] = {
                "shotId": shot.id,
                "shotVersionId": shot_version_id,
                "jobId": job.id,
                "status": job.status,
                "reused": False,
                "estimate": estimate.model_dump(mode="json", by_alias=True) if estimate else None,
            }
        if entries:
            # Same transaction as the Jobs: a crash cannot leave a half-registered batch.
            draft["storyboards"][record.id] = with_shot_versions(record, entries).model_dump(
                mode="json", by_alias=True
            )
        shots = []
        for shot_id in order:
            shot = shot_of(record, version, shot_id)
            entry = created.get(shot_id)
            if entry is None:
                previous = reused[shot_id]
                if previous.job_id not in draft["jobs"]:
                    raise AppError("JOB_NOT_FOUND", "原镜头任务已不存在，请重新生成。")
                job = parse_job(draft["jobs"][previous.job_id])
                entry = {
                    "shotId": shot_id,
                    "shotVersionId": previous.id,
                    "jobId": previous.job_id,
                    "status": job.status,
                    "reused": True,
                    "estimate": previous.estimate.model_dump(mode="json", by_alias=True)
                    if previous.estimate
                    else None,
                }
            shots.append({**entry, "mode": shot.mode, "durationSeconds": shot.duration_seconds})
        if headless:
            run = draft["runs"][context.run_id]
            headless_state = run["headless"]
            headless_state["registration"] = {
                "revision": record.revision + (1 if entries else 0),
                "shots": [
                    {
                        "shotId": item["shotId"],
                        "shotVersionId": item["shotVersionId"],
                        "jobId": item["jobId"],
                        "reused": item["reused"],
                    }
                    for item in shots
                ],
                "jobIds": [item["jobId"] for item in shots],
                "registeredAt": timestamp,
            }
            headless_state["status"] = "authorized"
            run["headless"] = headless_state
            run["updatedAt"] = timestamp
        total = sum((Decimal(item["estimate"]["amount"]) for item in shots if item["estimate"]), Decimal(0))
        currency = next((item["estimate"]["currency"] for item in shots if item["estimate"]), "CNY")
        return {
            "storyboardId": record.id,
            "storyboardVersion": version,
            "simulated": not live,
            "mediaAvailable": False,
            "shots": shots,
            "totalEstimate": {"currency": currency, "amount": money(total)} if live else None,
            "limits": limits.model_dump(mode="json", by_alias=True) if live else None,
        }

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
        if isinstance(job, LiveJob):
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
