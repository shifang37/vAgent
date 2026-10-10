"""M1-B video requests, provider protocol and persistent Job invariants."""

import hashlib
import json
from collections.abc import Mapping
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Annotated, Literal, Protocol, runtime_checkable
from urllib.parse import urlsplit
from uuid import UUID

from pydantic import AfterValidator, BeforeValidator, Field, field_validator, model_validator

from vagent.contracts import Contract, Fingerprint, Identifier, JsonTuple, Name, PositiveSeconds, UtcTimestamp
from vagent.errors import AppError
from vagent.waiting import ToolExecutionContext

VIDEO_CONTRACT_VERSION = 1
VideoMode = Literal["off", "mock", "live"]
JobStatus = Literal["pending_submit", "submitting", "queued", "running", "succeeded", "failed", "unknown"]


class ArtifactRef(Contract):
    artifact_id: Identifier
    version: int = Field(ge=1)


class VideoSpec(Contract):
    duration_seconds: PositiveSeconds
    resolution: Name
    aspect_ratio: str = Field(max_length=20, pattern=r"^[1-9][0-9]*:[1-9][0-9]*$")


class VideoCapabilities(Contract):
    provider: Name
    model: Name
    capabilities_version: Name
    mode: Literal["mock"] = "mock"
    input_types: JsonTuple[Literal["text"]] = ("text",)
    specs: JsonTuple[VideoSpec] = Field(min_length=1, max_length=128)
    supports_cancel: bool = False
    supports_idempotent_submit: bool = False

    @model_validator(mode="after")
    def unique_capabilities(self):
        if len(self.input_types) != 1 or len(set(self.specs)) != len(self.specs):
            raise ValueError("Input types and complete specification combinations must be unique")
        return self


class VideoRequest(Contract):
    provider: Name
    model: Name
    capabilities_version: Name
    prompt: str = Field(min_length=1, max_length=8000)
    spec: VideoSpec
    source_refs: JsonTuple[ArtifactRef] = Field(default=(), max_length=16)

    @field_validator("prompt")
    @classmethod
    def nonblank_prompt(cls, value):
        if not value.strip():
            raise ValueError("Prompt cannot be blank")
        return value.strip()

    @model_validator(mode="after")
    def unique_sources(self):
        if len(set(self.source_refs)) != len(self.source_refs):
            raise ValueError("Source version references must be unique")
        return self

    def fingerprint(self) -> str:
        # Reference order is meaningful; only object-key order/outer prompt space is normalized.
        encoded = json.dumps(
            self.model_dump(mode="json", by_alias=True),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def validate_video_request(
    request: VideoRequest,
    capabilities: VideoCapabilities,
    *,
    project_id: str,
    artifacts: Mapping[str, dict],
) -> None:
    """Validate a server-selected capability and the caller's project snapshot."""
    if (request.provider, request.model) != (capabilities.provider, capabilities.model):
        raise AppError("VIDEO_MODEL_UNAVAILABLE", "所选视频模型不在当前能力表中。")
    if request.capabilities_version != capabilities.capabilities_version:
        raise AppError("VIDEO_CAPABILITIES_CHANGED", "视频能力版本已变化，请重新读取能力表。")
    if request.spec not in capabilities.specs:
        raise AppError("VIDEO_UNSUPPORTED_SPEC", "时长、分辨率和画幅的组合不受支持，未创建任务。")
    for reference in request.source_refs:
        source = artifacts.get(reference.artifact_id)
        if source is None or source["projectId"] != project_id:
            raise AppError("SOURCE_NOT_FOUND", "当前项目中没有所引用的源产物。")
        if not any(version["version"] == reference.version for version in source["versions"]):
            raise AppError("SOURCE_VERSION_NOT_FOUND", "源产物版本不存在，未创建任务。")


class JobError(Contract):
    stage: Literal["submit", "query", "generate"]
    code: str = Field(min_length=1, max_length=64, pattern=r"^[A-Z][A-Z0-9_]*$")
    message: str = Field(min_length=1, max_length=1000)


class ProviderCallError(Exception):
    """Adapters supply sanitized errors, with explicit submission uncertainty."""

    def __init__(
        self,
        error: JobError,
        *,
        submission_outcome: Literal["not_accepted", "unknown"] | None = None,
        retry_after_at: str | None = None,
        pause_reason: Literal["configuration", "task_unavailable"] | None = None,
    ):
        if error.stage not in {"submit", "query"}:
            raise ValueError("Confirmed generation errors belong in task snapshots")
        if error.stage == "submit" and submission_outcome not in {"not_accepted", "unknown"}:
            raise ValueError("Submission errors must explicitly identify their uncertainty")
        if error.stage == "query" and submission_outcome is not None:
            raise ValueError("Query errors cannot change submission outcome")
        super().__init__(error.message)
        self.error = error
        self.submission_outcome = submission_outcome
        self.retry_after_at = retry_after_at
        self.pause_reason = pause_reason


class JobResult(Contract):
    simulated: bool = True
    media_available: bool = False
    request_fingerprint: Fingerprint
    spec: VideoSpec
    source_refs: JsonTuple[ArtifactRef] = Field(default=(), max_length=16)
    artifact_refs: JsonTuple[ArtifactRef] = Field(default=(), max_length=0)
    summary: str = Field(min_length=1, max_length=2000)

    @model_validator(mode="after")
    def simulation_only(self):
        if not self.simulated or self.media_available:
            raise ValueError("M1-B results describe a simulation without playable media")
        return self


class ProviderTaskSnapshot(Contract):
    task_id: str = Field(min_length=1, max_length=256, pattern=r"^\S+$")
    status: Literal["queued", "running", "succeeded", "failed"]
    result: JobResult | None = None
    error: JobError | None = None

    @model_validator(mode="after")
    def terminal_payload(self):
        if (self.status == "succeeded") != (self.result is not None):
            raise ValueError("Only provider success contains a result")
        if (self.status == "failed") != (self.error is not None):
            raise ValueError("Only confirmed provider failure contains a generation error")
        if self.error is not None and self.error.stage != "generate":
            raise ValueError("A query transport error is not a failed generation snapshot")
        return self


class ProviderTaskHandle(Contract):
    task_id: str = Field(min_length=1, max_length=256, pattern=r"^\S+$")
    snapshot: ProviderTaskSnapshot | None = None

    @model_validator(mode="after")
    def same_task(self):
        if self.snapshot is not None and self.snapshot.task_id != self.task_id:
            raise ValueError("Submission handle and initial snapshot must identify the same task")
        return self


@runtime_checkable
class VideoProviderAdapter(Protocol):
    def capabilities(self) -> VideoCapabilities: ...

    async def submit(self, request: VideoRequest, operation_key: str) -> ProviderTaskHandle: ...

    async def query(self, task_id: str) -> ProviderTaskSnapshot: ...


@runtime_checkable
class CancellableProvider(Protocol):
    async def cancel(self, task_id: str) -> None: ...


class PollingPolicy(Contract):
    interval_seconds: PositiveSeconds = 2.0
    submit_timeout_seconds: PositiveSeconds = 15.0
    query_timeout_seconds: PositiveSeconds = 15.0
    retry_delays_seconds: JsonTuple[PositiveSeconds] = Field(default=(1.0, 2.0, 4.0), max_length=10)


class Job(Contract):
    contract_version: int = Field(default=VIDEO_CONTRACT_VERSION, ge=1, le=VIDEO_CONTRACT_VERSION)
    id: Identifier
    context: ToolExecutionContext
    operation_key: str = Field(min_length=1, max_length=300)
    revision: int = Field(default=0, ge=0)
    mode: Literal["mock"] = "mock"
    request: VideoRequest
    request_fingerprint: Fingerprint
    capabilities: VideoCapabilities
    status: JobStatus = "pending_submit"
    provider_task_id: str | None = Field(default=None, min_length=1, max_length=256, pattern=r"^\S+$")
    submit_attempts: int = Field(default=0, ge=0, le=1)
    query_attempts: int = Field(default=0, ge=0)
    consecutive_query_errors: int = Field(default=0, ge=0)
    query_state: Literal["idle", "polling", "retrying", "paused"] = "idle"
    query_started_at: UtcTimestamp | None = None
    policy: PollingPolicy = Field(default_factory=PollingPolicy)
    next_poll_at: UtcTimestamp | None = None
    created_at: UtcTimestamp
    updated_at: UtcTimestamp
    error: JobError | None = None
    result: JobResult | None = None

    @model_validator(mode="after")
    def coherent_state(self):
        if self.operation_key != self.context.operation_key:
            raise ValueError("Job operation key must match its server execution context")
        if self.request_fingerprint != self.request.fingerprint():
            raise ValueError("Job request fingerprint does not match its frozen request")
        if (self.request.provider, self.request.model, self.request.capabilities_version) != (
            self.capabilities.provider,
            self.capabilities.model,
            self.capabilities.capabilities_version,
        ) or self.request.spec not in self.capabilities.specs:
            raise ValueError("Frozen request does not match its capability snapshot")
        if datetime.fromisoformat(self.updated_at) < datetime.fromisoformat(self.created_at):
            raise ValueError("Job cannot be updated before it was created")
        if self.submit_attempts != (0 if self.status == "pending_submit" else 1):
            raise ValueError("Submission intent must be recorded before an attempt")
        if self.status in {"pending_submit", "submitting", "unknown"} and self.provider_task_id:
            raise ValueError("An unconfirmed submission cannot have a confirmed provider task ID")
        if self.status in {"queued", "running", "succeeded"} and not self.provider_task_id:
            raise ValueError("Accepted jobs require the original provider task ID")
        if (self.status == "succeeded") != (self.result is not None):
            raise ValueError("Only success contains a committed result")
        if self.result is not None and (
            self.result.request_fingerprint != self.request_fingerprint
            or self.result.spec != self.request.spec
            or self.result.source_refs != self.request.source_refs
        ):
            raise ValueError("Result provenance must match the Job request")
        if self.status in {"unknown", "failed"}:
            if self.error is None or self.error.stage == "query":
                raise ValueError("Submission uncertainty and failure require the correct error stage")
            if self.status == "unknown" and self.error.stage != "submit":
                raise ValueError("Unknown means an uncertain submission")
            if (self.error.stage == "generate") != bool(self.provider_task_id):
                raise ValueError("Only a confirmed generation failure has a provider task ID")
        elif self.error is not None and (
            self.status not in {"queued", "running"}
            or self.error.stage != "query"
            or self.query_state not in {"retrying", "paused"}
        ):
            raise ValueError("Query errors retain the last confirmed generation state")
        active = self.status in {"queued", "running"}
        if (self.query_state != "idle") != active:
            raise ValueError("Only active accepted jobs have a query lifecycle")
        if (self.query_state in {"polling", "retrying"}) != (self.next_poll_at is not None):
            raise ValueError("Only scheduled queries have a next poll time")
        if self.query_started_at is not None and (
            self.query_state not in {"polling", "retrying"}
            or not self.query_attempts
            or datetime.fromisoformat(self.query_started_at) > datetime.fromisoformat(self.updated_at)
        ):
            raise ValueError("An in-flight query requires a recorded attempt and an active query lifecycle")
        if self.query_state in {"retrying", "paused"} and self.error is None:
            raise ValueError("Retrying or paused queries need a query error")
        if self.consecutive_query_errors > self.query_attempts:
            raise ValueError("Query errors cannot exceed total query attempts")
        if not self.provider_task_id and self.query_attempts:
            raise ValueError("Queries require a confirmed provider task ID")
        retries = len(self.policy.retry_delays_seconds)
        if self.query_state == "retrying" and not 1 <= self.consecutive_query_errors <= retries:
            raise ValueError("Query retries must stay within the saved retry window")
        if self.query_state == "paused" and self.consecutive_query_errors != retries + 1:
            raise ValueError("Query pause follows exhaustion of the saved retry window")
        if self.query_state in {"idle", "polling"} and self.consecutive_query_errors:
            raise ValueError("Healthy or finished queries have no consecutive query errors")
        return self


_JOB_TRANSITIONS = {
    "pending_submit": {"submitting"},
    "submitting": {"queued", "running", "succeeded", "failed", "unknown"},
    "queued": {"running", "succeeded", "failed"},
    "running": {"succeeded", "failed"},
    "succeeded": set(),
    "failed": set(),
    "unknown": set(),
}


def job_transition_allowed(current: JobStatus, following: JobStatus) -> bool:
    """Same-state updates may change query metadata, never immutable request identity."""
    return current in _JOB_TRANSITIONS and (following == current or following in _JOB_TRANSITIONS[current])


# Keep the v1 models above (including their JSON schemas/defaults) unchanged: they
# are part of historical tool/checkpoint signatures. Live uses separate models.
WAN_MODEL = "wan2.7-t2v-2026-06-12"
WAN_REGION = "cn-beijing"
WAN_CAPABILITIES_VERSION = "wan27-t2v-beijing-v1"
WAN_ADAPTER_VERSION = "wan-http-v1"
WAN_ENDPOINT_PROFILE = "beijing-workspace-v1"
WAN_PRICE_VERSION = "wan27-beijing-720p-20261010"
WAN_PRICE_SOURCE = "https://help.aliyun.com/zh/model-studio/model-pricing#ba6f7744d5e0o"
WAN_PROMPT_PREFIX = "生成单镜头视频。\n"
SOURCE_POLICY_VERSION = "wan-result-hosts-v1"
MEDIA_VALIDATION_VERSION = "mp4-avc-v1"

WorkspaceId = Annotated[str, Field(min_length=1, max_length=63, pattern=r"^[a-z0-9](?:[a-z0-9-]*[a-z0-9])?$")]
ProviderId = Annotated[str, Field(min_length=1, max_length=256, pattern=r"^[A-Za-z0-9_-]+$")]


def _money(value: str) -> str:
    return format(Decimal(value), ".2f")


Money = Annotated[
    str,
    Field(max_length=32, pattern=r"^(?:0|[1-9][0-9]*)(?:\.[0-9]{1,2})?$"),
    AfterValidator(_money),
]


def _uuid(value: str) -> str:
    if str(UUID(value)) != value:
        raise ValueError("Media IDs must be canonical UUIDs")
    return value


MediaId = Annotated[str, Field(min_length=36, max_length=36), AfterValidator(_uuid)]


def canonical_fingerprint(value: dict) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode(
            "utf-8"
        )
    ).hexdigest()


class VideoSpecV2(VideoSpec):
    duration_seconds: int = Field(gt=0)


class VideoIntentV2(VideoRequest):
    """Only these six public fields may come from a tool call."""

    spec: VideoSpecV2

    def intent_fingerprint(self) -> str:
        return canonical_fingerprint(
            {"intentVersion": 2, "arguments": self.model_dump(mode="json", by_alias=True)}
        )


class WanParameters(Contract):
    # These aliases are the exact provider protocol, not the public camelCase API.
    resolution: Literal["720P"] = "720P"
    ratio: Literal["16:9"] = "16:9"
    duration: int = Field(default=5, ge=5, le=5)
    prompt_extend: bool = Field(default=False, alias="prompt_extend")
    watermark: bool = True
    seed: int = Field(default=0, ge=0, le=0)

    @model_validator(mode="after")
    def fixed_parameters(self):
        if self.prompt_extend or not self.watermark:
            raise ValueError("Wan v1 requires prompt_extend=false and watermark=true")
        return self


class VideoRequestV2(VideoIntentV2):
    region: Literal["cn-beijing"]
    workspace_id: WorkspaceId
    endpoint_profile: Literal["beijing-workspace-v1"]
    adapter_version: Literal["wan-http-v1"]
    provider_prompt: str = Field(min_length=10, max_length=5000)
    parameters: WanParameters

    @model_validator(mode="after")
    def complete_request(self):
        if (
            self.provider != "wan"
            or self.model != WAN_MODEL
            or self.provider_prompt != WAN_PROMPT_PREFIX + self.prompt
            or self.spec != VideoSpecV2(duration_seconds=5, resolution="720p", aspect_ratio="16:9")
        ):
            raise ValueError("The frozen Wan request must match its public intent and exact parameters")
        return self

    def public_arguments(self) -> dict:
        return {
            name: self.model_dump(mode="json", by_alias=True)[name]
            for name in ("provider", "model", "capabilitiesVersion", "prompt", "spec", "sourceRefs")
        }

    def intent_fingerprint(self) -> str:
        return canonical_fingerprint({"intentVersion": 2, "arguments": self.public_arguments()})

    def fingerprint(self) -> str:
        return canonical_fingerprint(
            {"requestVersion": 2, "request": self.model_dump(mode="json", by_alias=True)}
        )


class VideoPrice(Contract):
    currency: Literal["CNY"] = "CNY"
    unit: Literal["output_second"] = "output_second"
    unit_price: Money
    quantity: str = Field(pattern=r"^[1-9][0-9]{0,8}$")
    amount: Money
    price_version: Name
    checked_at: str = Field(pattern=r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$")
    source_url: str = Field(min_length=1, max_length=1000, pattern=r"^https://")

    @model_validator(mode="after")
    def correct_amount(self):
        if Decimal(self.unit_price) * Decimal(self.quantity) != Decimal(self.amount):
            raise ValueError("Estimate must equal its frozen unit price times quantity")
        # Validate the date without converting the persisted string.
        datetime.strptime(self.checked_at, "%Y-%m-%d")
        return self


def wan_price() -> VideoPrice:
    return VideoPrice(
        unit_price="0.60",
        quantity="5",
        amount="3.00",
        price_version=WAN_PRICE_VERSION,
        checked_at="2026-10-10",
        source_url=WAN_PRICE_SOURCE,
    )


class CostEstimate(VideoPrice):
    max_job_cost: Money


class ProviderUsage(Contract):
    duration: float | None = Field(default=None, ge=0)
    input_video_duration: float | None = Field(default=None, ge=0)
    output_video_duration: float | None = Field(default=None, ge=0)
    video_count: int | None = Field(default=None, ge=1)
    resolution: Literal["720P", "1080P"] | None = None
    aspect_ratio: str | None = Field(default=None, max_length=20, pattern=r"^[1-9][0-9]*:[1-9][0-9]*$")


class ActualCost(Contract):
    status: Literal["unknown", "reported"] = "unknown"
    currency: Literal["CNY"] = "CNY"
    amount: Money | None = None
    source: str | None = Field(default=None, min_length=1, max_length=1000)

    @model_validator(mode="after")
    def bill_evidence(self):
        if (self.status == "reported") != (self.amount is not None and self.source is not None):
            raise ValueError("Actual costs require a traceable bill, not HTTP usage")
        if self.status == "unknown" and (self.amount is not None or self.source is not None):
            raise ValueError("Unknown actual costs have no amount or billing source")
        return self


class CostRecord(Contract):
    estimate: CostEstimate
    provider_usage: ProviderUsage | None = None
    actual: ActualCost = Field(default_factory=ActualCost)


class VideoCapabilitiesV2(VideoCapabilities):
    mode: Literal["live"] = "live"
    region: Literal["cn-beijing"] = "cn-beijing"
    specs: JsonTuple[VideoSpecV2] = Field(min_length=1, max_length=128)
    max_prompt_characters: int = Field(default=4991, ge=4991, le=4991)
    shot_mode: Literal["single"] = "single"
    audio_mode: Literal["auto"] = "auto"
    prompt_extend: bool = False
    watermark: bool = True
    seed: int = Field(default=0, ge=0, le=0)
    supports_result_url_refresh: bool = False
    price: VideoPrice = Field(default_factory=wan_price)

    @model_validator(mode="after")
    def no_unsupported_options(self):
        if (
            self.supports_cancel
            or self.supports_idempotent_submit
            or self.supports_result_url_refresh
            or self.prompt_extend
            or not self.watermark
        ):
            raise ValueError("Live v2 cannot advertise unsupported provider options")
        return self


def wan_capabilities() -> VideoCapabilitiesV2:
    return VideoCapabilitiesV2(
        provider="wan",
        model=WAN_MODEL,
        capabilities_version=WAN_CAPABILITIES_VERSION,
        specs=(VideoSpecV2(duration_seconds=5, resolution="720p", aspect_ratio="16:9"),),
    )


class JobErrorV2(JobError):
    stage: Literal["submit", "query", "generate", "download"]
    http_status: int | None = Field(default=None, ge=100, le=599)
    provider_code: str | None = Field(default=None, max_length=128, pattern=r"^[A-Za-z][A-Za-z0-9_.-]*$")
    request_id: ProviderId | None = None

    def public(self) -> dict:
        return {"stage": self.stage, "code": self.code, "message": self.message}


class ProviderTimes(Contract):
    submit_time: str | None = Field(default=None, max_length=64)
    scheduled_time: str | None = Field(default=None, max_length=64)
    end_time: str | None = Field(default=None, max_length=64)


class ProviderOutput(Contract):
    task_id: ProviderId
    received_at: UtcTimestamp
    video_url: str = Field(min_length=1, max_length=8192, repr=False)
    url_expires_at: UtcTimestamp | None = None
    expiry_source: Literal["signature", "unknown"] = "unknown"
    provider_times: ProviderTimes = Field(default_factory=ProviderTimes)
    usage: ProviderUsage | None = None

    @field_validator("video_url")
    @classmethod
    def https_syntax(cls, value):
        parsed = urlsplit(value)
        if parsed.scheme != "https" or not parsed.hostname or any(c.isspace() or ord(c) < 32 for c in value):
            raise ValueError("Provider output must contain a bounded HTTPS URL")
        _ = parsed.port
        return value

    @model_validator(mode="after")
    def expiry_evidence(self):
        if (self.expiry_source == "signature") != (self.url_expires_at is not None):
            raise ValueError("Expiry must be derived from a parseable signature")
        return self


class ProviderTaskSnapshotV2(Contract):
    task_id: ProviderId
    request_id: ProviderId | None = None
    status: Literal["queued", "running", "succeeded", "failed"]
    output: ProviderOutput | None = None
    error: JobErrorV2 | None = None

    @model_validator(mode="after")
    def terminal_payload(self):
        if (self.status == "succeeded") != (self.output is not None):
            raise ValueError("Only provider success contains private output")
        if self.output is not None and self.output.task_id != self.task_id:
            raise ValueError("Output must identify the original task")
        if (self.status == "failed") != (self.error is not None):
            raise ValueError("Confirmed failure requires a generation error")
        if self.error is not None and self.error.stage != "generate":
            raise ValueError("Query failures cannot change generation state")
        return self

    @property
    def provider_status(self) -> str:
        if self.error and self.error.code == "PROVIDER_CANCELED":
            return "CANCELED"
        return {"queued": "PENDING", "running": "RUNNING", "succeeded": "SUCCEEDED", "failed": "FAILED"}[
            self.status
        ]


class ProviderTaskHandleV2(Contract):
    task_id: ProviderId
    request_id: ProviderId | None = None
    snapshot: ProviderTaskSnapshotV2 | None = None

    @model_validator(mode="after")
    def same_task(self):
        if self.snapshot is not None and self.snapshot.task_id != self.task_id:
            raise ValueError("Handle and snapshot must identify the same task")
        return self


def live_polling_policy() -> PollingPolicy:
    return PollingPolicy(
        interval_seconds=15.0,
        submit_timeout_seconds=30.0,
        query_timeout_seconds=30.0,
        retry_delays_seconds=(15.0, 30.0, 60.0),
    )


class FrameRate(Contract):
    numerator: int = Field(gt=0)
    denominator: int = Field(gt=0)


class MediaMetadata(Contract):
    width: int = Field(gt=0)
    height: int = Field(gt=0)
    duration_seconds: PositiveSeconds
    video_codec: Literal["h264"]
    has_audio: bool
    audio_codec: Name | None = None
    frame_rate: FrameRate | None = None

    @model_validator(mode="after")
    def audio_metadata(self):
        if not self.has_audio and self.audio_codec is not None:
            raise ValueError("A silent file has no audio codec")
        return self


class MediaAsset(Contract):
    id: MediaId
    project_id: Identifier
    job_id: Identifier
    source_refs: JsonTuple[ArtifactRef] = Field(default=(), max_length=16)
    relative_path: str = Field(max_length=150)
    mime_type: Literal["video/mp4"] = "video/mp4"
    size_bytes: int = Field(gt=0)
    sha256: Fingerprint
    created_at: UtcTimestamp
    metadata: MediaMetadata
    validation_version: Literal["mp4-avc-v1"] = MEDIA_VALIDATION_VERSION

    @model_validator(mode="after")
    def controlled_path(self):
        if self.relative_path != f"media/{self.project_id}/{self.id}.mp4":
            raise ValueError("Media paths are determined exclusively by server IDs")
        return self


class MediaRef(Contract):
    media_id: MediaId


class JobResultV2(JobResult):
    simulated: bool = False
    media_available: bool = True
    spec: VideoSpecV2
    media_refs: JsonTuple[MediaRef] = Field(min_length=1, max_length=1)

    @model_validator(mode="after")
    def simulation_only(self):
        if self.simulated or not self.media_available:
            raise ValueError("Live results represent committed local media")
        return self


class DownloadPolicy(Contract):
    max_bytes: int = Field(default=268435456, gt=0)
    connect_timeout_seconds: PositiveSeconds = 10.0
    read_timeout_seconds: PositiveSeconds = 30.0
    total_timeout_seconds: PositiveSeconds = 180.0
    retry_delays_seconds: JsonTuple[PositiveSeconds] = (5.0, 30.0)
    max_attempts_per_window: int = Field(default=3, ge=1, le=3)
    max_redirects: int = Field(default=3, ge=0, le=3)
    concurrency: int = Field(default=1, ge=1, le=1)

    @model_validator(mode="after")
    def bounded_attempts(self):
        if len(self.retry_delays_seconds) + 1 != self.max_attempts_per_window:
            raise ValueError("Download delays must match the finite attempt window")
        return self


class PreparedMedia(Contract):
    size_bytes: int = Field(gt=0)
    sha256: Fingerprint
    metadata: MediaMetadata
    validated_at: UtcTimestamp


class DownloadRecord(Contract):
    media_id: MediaId
    relative_path: str = Field(max_length=150)
    phase: Literal["pending", "writing", "prepared", "committed", "failed"] = "pending"
    generation: int = Field(default=1, ge=1)
    attempts: int = Field(default=0, ge=0)
    window_attempts: int = Field(default=0, ge=0, le=3)
    next_attempt_at: UtcTimestamp | None = None
    started_at: UtcTimestamp | None = None
    deadline_at: UtcTimestamp | None = None
    prepared: PreparedMedia | None = None
    error: JobErrorV2 | None = None
    repair: bool = False
    source_policy_version: Name = SOURCE_POLICY_VERSION

    @model_validator(mode="after")
    def recoverable_intent(self):
        if self.window_attempts > self.attempts:
            raise ValueError("Window attempts cannot exceed lifetime attempts")
        if (self.started_at is None) != (self.deadline_at is None):
            raise ValueError("An attempt must save its start and deadline together")
        if self.started_at is not None and (
            not self.attempts
            or datetime.fromisoformat(self.deadline_at) <= datetime.fromisoformat(self.started_at)
        ):
            raise ValueError("A download attempt needs a future deadline and a consumed attempt")
        if self.phase == "writing" and self.started_at is None:
            raise ValueError("Writing requires a durable attempt")
        if self.phase in {"prepared", "committed"} and self.prepared is None:
            raise ValueError("Prepared and committed media require integrity metadata")
        if self.phase in {"committed", "failed"} and self.next_attempt_at is not None:
            raise ValueError("Stopped downloads cannot have an automatic retry")
        if self.error is not None and self.error.stage != "download":
            raise ValueError("Downloads only carry download errors")
        if self.phase == "failed" and self.error is None:
            raise ValueError("Failed downloads require an actionable error")
        return self


class RuntimeBlock(Contract):
    stage: Literal["submit", "query", "download"]
    code: str = Field(min_length=1, max_length=64, pattern=r"^[A-Z][A-Z0-9_]*$")
    message: str = Field(min_length=1, max_length=1000)
    blocked_at: UtcTimestamp


class MediaAvailability(Contract):
    status: Literal["available", "unavailable"] = "unavailable"
    reason: str | None = Field(default="not_delivered", max_length=64, pattern=r"^[a-z][a-z0-9_]*$")
    checked_at: UtcTimestamp | None = None

    @model_validator(mode="after")
    def verified_availability(self):
        if self.status == "available" and (self.reason is not None or self.checked_at is None):
            raise ValueError("Availability requires verification and no failure reason")
        if self.status == "unavailable" and self.reason is None:
            raise ValueError("Unavailable media needs a reason")
        return self


LiveJobStatus = Literal[
    "pending_submit",
    "submitting",
    "queued",
    "running",
    "downloading",
    "download_failed",
    "succeeded",
    "failed",
    "unknown",
]


class JobV2(Job):
    contract_version: int = Field(default=2, ge=2, le=2)
    mode: Literal["live"] = "live"
    request: VideoRequestV2
    intent_fingerprint: Fingerprint
    capabilities: VideoCapabilitiesV2
    status: LiveJobStatus = "pending_submit"
    provider_task_id: ProviderId | None = None
    policy: PollingPolicy = Field(default_factory=live_polling_policy)
    error: JobErrorV2 | None = None
    result: JobResultV2 | None = None
    submission_started_at: UtcTimestamp | None = None
    query_deadline_at: UtcTimestamp | None = None
    provider_submission_request_id: ProviderId | None = None
    last_query_request_id: ProviderId | None = None
    last_provider_status: (
        Literal["PENDING", "RUNNING", "SUCCEEDED", "FAILED", "CANCELED", "UNKNOWN"] | None
    ) = None
    query_pause_reason: Literal["retry_exhausted", "configuration", "task_unavailable"] | None = None
    runtime_block: RuntimeBlock | None = None
    cost: CostRecord
    provider_output: ProviderOutput | None = Field(default=None, repr=False)
    download_policy: DownloadPolicy = Field(default_factory=DownloadPolicy)
    download: DownloadRecord | None = None
    media_availability: MediaAvailability = Field(default_factory=MediaAvailability)

    @model_validator(mode="after")
    def coherent_state(self):
        if self.operation_key != self.context.operation_key:
            raise ValueError("Job operation key must match its server execution context")
        if (
            self.request_fingerprint != self.request.fingerprint()
            or self.intent_fingerprint != self.request.intent_fingerprint()
        ):
            raise ValueError("Job fingerprints must match the complete frozen request and intent")
        if (
            self.request.provider,
            self.request.model,
            self.request.capabilities_version,
            self.request.region,
        ) != (
            self.capabilities.provider,
            self.capabilities.model,
            self.capabilities.capabilities_version,
            self.capabilities.region,
        ) or self.request.spec not in self.capabilities.specs:
            raise ValueError("Frozen request does not match the capability snapshot")
        estimate = self.cost.estimate
        if (
            estimate.model_dump(exclude={"max_job_cost"}) != self.capabilities.price.model_dump()
            or Decimal(estimate.amount) > Decimal(estimate.max_job_cost)
            or Decimal(estimate.quantity) != self.request.spec.duration_seconds
        ):
            raise ValueError("Job must retain an affordable quote for its original specification")
        if datetime.fromisoformat(self.updated_at) < datetime.fromisoformat(self.created_at):
            raise ValueError("Job cannot be updated before creation")
        if self.submit_attempts != (0 if self.status == "pending_submit" else 1):
            raise ValueError("Only one durably recorded submission is allowed")
        if self.submit_attempts:
            if (
                self.submission_started_at is None
                or self.query_deadline_at is None
                or (
                    datetime.fromisoformat(self.query_deadline_at)
                    - datetime.fromisoformat(self.submission_started_at)
                )
                != timedelta(hours=24)
            ):
                raise ValueError("Submission records a fixed 24-hour query window")
        elif self.submission_started_at is not None or self.query_deadline_at is not None:
            raise ValueError("Unsubmitted jobs have no submission/query window")
        if self.status in {"pending_submit", "submitting", "unknown"} and self.provider_task_id is not None:
            raise ValueError("Unconfirmed submission cannot have a confirmed task ID")
        if (
            self.status in {"queued", "running", "downloading", "download_failed", "succeeded"}
            and self.provider_task_id is None
        ):
            raise ValueError("Accepted jobs require the original task ID")
        if (self.status == "succeeded") != (self.result is not None):
            raise ValueError("Only committed local success contains a result")
        generated = self.status in {"downloading", "download_failed", "succeeded"}
        if generated != (self.provider_output is not None) or generated != (self.download is not None):
            raise ValueError("Cloud success must atomically register one output and download intent")
        if generated:
            if (
                self.provider_output.task_id != self.provider_task_id
                or self.last_provider_status != "SUCCEEDED"
            ):
                raise ValueError("Private output must belong to the confirmed successful task")
            if self.download.relative_path != f"media/{self.context.project_id}/{self.download.media_id}.mp4":
                raise ValueError("Download location must be derived from server IDs")
            if self.download.window_attempts > self.download_policy.max_attempts_per_window:
                raise ValueError("Download retries cannot exceed the saved window")
            if self.cost.provider_usage != self.provider_output.usage:
                raise ValueError("Provider usage comes only from the original task output")
        if self.result is not None and (
            self.result.request_fingerprint != self.request_fingerprint
            or self.result.spec != self.request.spec
            or self.result.source_refs != self.request.source_refs
            or self.result.media_refs[0].media_id != self.download.media_id
            or (not self.download.repair and self.download.phase != "committed")
        ):
            raise ValueError("Local result and download must retain the Job's provenance")
        if self.status != "succeeded" and (
            self.media_availability.status == "available" or (self.download and self.download.repair)
        ):
            raise ValueError("Media is available or repairable only after its first local delivery")
        if self.status in {"unknown", "failed", "download_failed"}:
            if self.error is None or self.error.stage == "query":
                raise ValueError("Terminal failure requires the correct error stage")
            if self.status == "unknown" and (
                self.error.stage != "submit" or self.error.code != "SUBMISSION_UNKNOWN"
            ):
                raise ValueError("Unknown means submission uncertainty")
            if self.status == "failed" and (
                self.error.stage not in {"submit", "generate"}
                or (self.error.stage == "generate") != bool(self.provider_task_id)
            ):
                raise ValueError("Only a confirmed generation failure has a task ID")
            if self.status == "download_failed" and (
                self.error.stage != "download" or self.download.phase != "failed"
            ):
                raise ValueError("Download failure must retain its failed download intent")
        elif self.error is not None and (
            self.status not in {"queued", "running"}
            or self.error.stage != "query"
            or self.query_state not in {"retrying", "paused"}
        ):
            raise ValueError("Query errors preserve the last confirmed generation state")
        active = self.status in {"queued", "running"}
        if (self.query_state != "idle") != active:
            raise ValueError("Only unfinished cloud tasks have a query lifecycle")
        if (self.query_state in {"polling", "retrying"}) != (self.next_poll_at is not None):
            raise ValueError("Only scheduled queries have a next poll time")
        if (self.query_state == "paused") != (self.query_pause_reason is not None):
            raise ValueError("A paused query must preserve its reason")
        if self.query_started_at is not None and (
            self.query_state not in {"polling", "retrying"}
            or not self.query_attempts
            or datetime.fromisoformat(self.query_started_at) > datetime.fromisoformat(self.updated_at)
        ):
            raise ValueError("In-flight queries need a persisted attempt")
        if self.query_state in {"retrying", "paused"} and self.error is None:
            raise ValueError("Unhealthy queries require a safe error")
        if self.consecutive_query_errors > self.query_attempts or (
            not self.provider_task_id and self.query_attempts
        ):
            raise ValueError("Query counters require original task attempts")
        retries = len(self.policy.retry_delays_seconds)
        if self.query_state == "retrying" and not 1 <= self.consecutive_query_errors <= retries:
            raise ValueError("Query retry window is bounded")
        if self.query_pause_reason == "retry_exhausted" and self.consecutive_query_errors != retries + 1:
            raise ValueError("Retry exhaustion consumes the saved retry window")
        if self.query_state in {"idle", "polling"} and self.consecutive_query_errors:
            raise ValueError("Healthy or completed queries have no consecutive errors")
        if self.runtime_block is not None and (
            (self.status == "pending_submit" and self.runtime_block.stage != "submit")
            or (
                active and (self.runtime_block.stage != "query" or self.query_pause_reason != "configuration")
            )
            or (self.status != "pending_submit" and not active)
        ):
            raise ValueError("Runtime blocks describe pending submission or paused query configuration")
        return self


def parse_job(value) -> Job | JobV2:
    if isinstance(value, Job):
        value = value.model_dump(mode="json", by_alias=True)
    if not isinstance(value, dict):
        raise ValueError("Job must be an object")
    version = value.get("contractVersion", value.get("contract_version", 1))
    if type(version) is not int or version not in {1, 2}:
        raise ValueError("Unsupported Job contract version")
    return (Job if version == 1 else JobV2).model_validate(value)


StoredJob = Annotated[Job | JobV2, BeforeValidator(parse_job)]


_LIVE_JOB_TRANSITIONS = {
    "pending_submit": {"submitting"},
    "submitting": {"queued", "running", "downloading", "failed", "unknown"},
    "queued": {"running", "downloading", "failed"},
    "running": {"downloading", "failed"},
    "downloading": {"succeeded", "download_failed"},
    "download_failed": {"downloading"},
    "succeeded": set(),
    "failed": set(),
    "unknown": set(),
}


def live_job_transition_allowed(current: LiveJobStatus, following: LiveJobStatus) -> bool:
    return current in _LIVE_JOB_TRANSITIONS and (
        following == current or following in _LIVE_JOB_TRANSITIONS[current]
    )
