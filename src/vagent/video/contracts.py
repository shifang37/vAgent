"""M1-B video requests, provider protocol and persistent Job invariants."""

import hashlib
import ipaddress
import json
import re
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

    async def query(self, task_id: str, *, endpoint_profile: str | None = None) -> ProviderTaskSnapshot: ...


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
# K1 keys-only track: the shared Beijing host is selected by the recorded form alone.
WAN_SHARED_ENDPOINT_PROFILE = "beijing-shared-v1"
WAN_PRICE_VERSION = "wan27-beijing-720p-20261010"
WAN_PRICE_SOURCE = "https://help.aliyun.com/zh/model-studio/model-pricing#ba6f7744d5e0o"
WAN_PROMPT_PREFIX = "生成单镜头视频。\n"

# M2 (D2) multi-shot generation keeps one shot per Job and one verified entry per
# mode. The first-frame model is the only verified image input path; clip
# extension stays cut, so `first_clip` never appears in a request.
WAN_I2V_MODEL = "wan2.7-i2v-2026-04-25"
WAN_SHOT_CAPABILITIES_VERSION = "wan27-shot-beijing-v2"
WAN_SHOT_ADAPTER_VERSION = "wan-http-v2"
WAN_I2V_PRICE_VERSION = "wan27-i2v-beijing-720p-20261016"
SHOT_DURATION_MIN = 2
SHOT_DURATION_MAX = 15
SHOT_DURATIONS = tuple(range(SHOT_DURATION_MIN, SHOT_DURATION_MAX + 1))
# Frozen ceiling: one generation Run registers at most this many real Jobs.
LIVE_JOBS_PER_RUN = 4
# M5 E3 publishes an episode-level ceiling in the same snapshot type: a 9/9 tool
# surface may place one episode's Jobs in a single Run, so the per-episode frozen
# ``ShotBudget.jobCap`` (default 20) is the batch boundary instead of the per-Run 4.
EPISODE_JOB_CAP = 20
# The first tool surface allowed to register one episode's batch at once.
EPISODE_TOOLS_VERSION = 9
IMAGE_MIME_TYPES = ("image/png", "image/jpeg", "image/webp")
IMAGE_MIN_SIDE = 240
IMAGE_MAX_SIDE = 8000
IMAGE_MAX_BYTES = 20 * 1024 * 1024
IMAGE_ASPECT_BOUNDS = (1, 8)

# v2 adds the documented Beijing result host; v1 stays valid for historical jobs.
SOURCE_POLICY_VERSION = "wan-result-hosts-v2"
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


class VideoRequestV4(VideoIntentV2):
    """The keys-only single-shot live request: identical intent, no workspace scope."""

    region: Literal["cn-beijing"]
    endpoint_profile: Literal["beijing-shared-v1"]
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
            raise ValueError("The frozen keys-only Wan request must match its public intent")
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
            {"requestVersion": 4, "request": self.model_dump(mode="json", by_alias=True)}
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


class ShotSpecV3(Contract):
    """One shot's verified specification; image modes inherit the input frame size."""

    mode: Literal["t2v", "i2v", "flf2v"] = "t2v"
    duration_seconds: int = Field(ge=SHOT_DURATION_MIN, le=SHOT_DURATION_MAX)
    resolution: Literal["720P"] = "720P"
    aspect_ratio: Literal["16:9"] | None = "16:9"

    @model_validator(mode="after")
    def ratio_follows_mode(self):
        if (self.mode == "t2v") != (self.aspect_ratio == "16:9"):
            raise ValueError("Only text-to-video fixes a 16:9 output frame")
        return self


class ImageInputRule(Contract):
    """Published reference-image bounds; the same bounds are enforced before submission."""

    mime_types: JsonTuple[Literal["image/png", "image/jpeg", "image/webp"]] = IMAGE_MIME_TYPES
    min_side: int = Field(default=IMAGE_MIN_SIDE, ge=1)
    max_side: int = Field(default=IMAGE_MAX_SIDE, ge=1)
    max_bytes: int = Field(default=IMAGE_MAX_BYTES, gt=0)
    min_aspect: int = Field(default=IMAGE_ASPECT_BOUNDS[0], ge=1)
    max_aspect: int = Field(default=IMAGE_ASPECT_BOUNDS[1], ge=1)
    allows_alpha: bool = False

    @model_validator(mode="after")
    def ordered_bounds(self):
        if self.min_side > self.max_side or self.min_aspect > self.max_aspect:
            raise ValueError("Image bounds must be ordered")
        return self


class WanParametersV3(Contract):
    # These aliases are the exact provider protocol, not the public camelCase API.
    resolution: Literal["720P"] = "720P"
    ratio: Literal["16:9"] | None = None
    duration: int = Field(ge=SHOT_DURATION_MIN, le=SHOT_DURATION_MAX)
    prompt_extend: bool = Field(default=False, alias="prompt_extend")
    watermark: bool = True
    seed: int = Field(default=0, ge=0, le=0)

    @model_validator(mode="after")
    def fixed_parameters(self):
        if self.prompt_extend or not self.watermark:
            raise ValueError("Shot requests require prompt_extend=false and watermark=true")
        return self


class ImageSize(Contract):
    width: int = Field(ge=1)
    height: int = Field(ge=1)


class ShotGenerationArgs(Contract):
    """Public arguments of one batch generation call; only shot IDs are caller-chosen."""

    storyboard_id: Identifier
    storyboard_version: int = Field(ge=1)
    shot_ids: JsonTuple[Identifier] = Field(min_length=1, max_length=64)
    confirm: bool = False

    @model_validator(mode="after")
    def unique_shots(self):
        if len(set(self.shot_ids)) != len(self.shot_ids):
            raise ValueError("Shot IDs must be unique inside one generation call")
        return self


class ShotRedoArgs(Contract):
    """One explicit user redo: the target shot plus the revision the caller read."""

    storyboard_id: Identifier
    storyboard_version: int = Field(ge=1)
    shot_id: Identifier
    expected_revision: int = Field(ge=0)
    client_request_id: Identifier


class EpisodeGenerationArgs(Contract):
    """One confirmed episode batch: only the episode ID and the confirmation are chosen."""

    episode_id: Identifier
    confirm: bool = False


class SeasonGenerationArgs(Contract):
    """One confirmed season batch: 1-3 current members or one old batch to resume.

    ``episodeIds`` and ``resumeBatchId`` are mutually exclusive; an empty selection
    defaults to every current member, which is then rejected by ``BATCH_LIMIT`` when
    the season holds four episodes.
    """

    season_id: Identifier
    episode_ids: JsonTuple[Identifier] | None = None
    resume_batch_id: Identifier | None = None
    confirm: bool = False

    @model_validator(mode="after")
    def coherent_selection(self):
        if self.episode_ids is not None:
            if not 1 <= len(self.episode_ids) <= 3:
                raise ValueError("A season batch selects one to three episodes")
            if len(set(self.episode_ids)) != len(self.episode_ids):
                raise ValueError("Season batch episode IDs must be unique")
        if self.episode_ids is not None and self.resume_batch_id is not None:
            raise ValueError("episodeIds and resumeBatchId are mutually exclusive")
        return self


class ShotRedoToolArgs(Contract):
    """A model-turn redo: the target shot plus the storyboard revision the model read.

    The local CLI/Web redo keeps ``clientRequestId`` (``ShotRedoArgs``); a tool call's
    own operation key already gives the replay identity, so the model never supplies
    one.
    """

    storyboard_id: Identifier
    storyboard_version: int = Field(ge=1)
    shot_id: Identifier
    expected_revision: int = Field(ge=0)


class ShotMediaInput(Contract):
    """A frozen reference to an imported asset; the bytes stay in the local asset store."""

    frame: Literal["first_frame", "last_frame"]
    asset_id: MediaId
    mime_type: Literal["image/png", "image/jpeg", "image/webp"]
    sha256: Fingerprint
    size_bytes: int = Field(gt=0)
    size: ImageSize


MODE_FRAMES = {"t2v": (), "i2v": ("first_frame",), "flf2v": ("first_frame", "last_frame")}
SHOT_MODES = ("t2v", "i2v", "flf2v")


class VideoRequestV3(Contract):
    """One frozen shot request: the shot's intent plus the server-selected execution."""

    provider: Literal["wan"] = "wan"
    model: Name
    capabilities_version: Literal["wan27-shot-beijing-v2"]
    prompt: str = Field(min_length=1, max_length=4991)
    spec: ShotSpecV3
    source_refs: JsonTuple[ArtifactRef] = Field(default=(), max_length=16)
    region: Literal["cn-beijing"] = "cn-beijing"
    workspace_id: WorkspaceId
    endpoint_profile: Literal["beijing-workspace-v1"] = "beijing-workspace-v1"
    adapter_version: Literal["wan-http-v2"] = "wan-http-v2"
    provider_prompt: str = Field(min_length=10, max_length=5000)
    parameters: WanParametersV3
    media: JsonTuple[ShotMediaInput] = Field(default=(), max_length=2)

    @model_validator(mode="after")
    def complete_request(self):
        if self.model != (WAN_MODEL if self.spec.mode == "t2v" else WAN_I2V_MODEL):
            raise ValueError("The frozen shot request must use the verified model of its mode")
        if self.provider_prompt != WAN_PROMPT_PREFIX + self.prompt:
            raise ValueError("A shot prompt is the fixed prefix plus the shot description")
        if (self.parameters.duration, self.parameters.resolution) != (
            self.spec.duration_seconds,
            self.spec.resolution,
        ):
            raise ValueError("Frozen parameters must match the shot specification")
        if tuple(item.frame for item in self.media) != MODE_FRAMES[self.spec.mode]:
            raise ValueError("A shot mode requires its exact ordered reference frames")
        if len({item.asset_id for item in self.media}) != len(self.media):
            raise ValueError("Reference assets must be unique per shot")
        if self.spec.mode == "t2v":
            if self.parameters.ratio != "16:9":
                raise ValueError("Text-to-video sends the frozen 16:9 ratio")
        elif self.parameters.ratio is not None:
            raise ValueError("Image modes must not send a ratio; the input frame decides it")
        return self

    def public_arguments(self) -> dict:
        """The public camelCase arguments a shot Job may show; never private fields.

        `live_job_snapshot` reads this for every live Job, so a shot request has to
        publish the same public shape as the frozen single-shot request.
        """
        return {
            name: self.model_dump(mode="json", by_alias=True)[name]
            for name in ("provider", "model", "capabilitiesVersion", "prompt", "spec", "sourceRefs")
        }

    def intent_fingerprint(self) -> str:
        return canonical_fingerprint(
            {
                "intentVersion": 3,
                "prompt": self.prompt,
                "spec": self.spec.model_dump(mode="json", by_alias=True),
                "media": [
                    {"frame": item.frame, "assetId": item.asset_id, "sha256": item.sha256}
                    for item in self.media
                ],
            }
        )

    def fingerprint(self) -> str:
        return canonical_fingerprint(
            {"requestVersion": 3, "request": self.model_dump(mode="json", by_alias=True)}
        )


class VideoRequestV5(Contract):
    """One keys-only shot request: the M2 protocol minus the workspace scope."""

    provider: Literal["wan"] = "wan"
    model: Name
    capabilities_version: Literal["wan27-shot-beijing-v2"]
    prompt: str = Field(min_length=1, max_length=4991)
    spec: ShotSpecV3
    source_refs: JsonTuple[ArtifactRef] = Field(default=(), max_length=16)
    region: Literal["cn-beijing"] = "cn-beijing"
    endpoint_profile: Literal["beijing-shared-v1"] = "beijing-shared-v1"
    adapter_version: Literal["wan-http-v2"] = "wan-http-v2"
    provider_prompt: str = Field(min_length=10, max_length=5000)
    parameters: WanParametersV3
    media: JsonTuple[ShotMediaInput] = Field(default=(), max_length=2)

    @model_validator(mode="after")
    def complete_request(self):
        if self.model != (WAN_MODEL if self.spec.mode == "t2v" else WAN_I2V_MODEL):
            raise ValueError("The frozen shot request must use the verified model of its mode")
        if self.provider_prompt != WAN_PROMPT_PREFIX + self.prompt:
            raise ValueError("A shot prompt is the fixed prefix plus the shot description")
        if (self.parameters.duration, self.parameters.resolution) != (
            self.spec.duration_seconds,
            self.spec.resolution,
        ):
            raise ValueError("Frozen parameters must match the shot specification")
        if tuple(item.frame for item in self.media) != MODE_FRAMES[self.spec.mode]:
            raise ValueError("A shot mode requires its exact ordered reference frames")
        if len({item.asset_id for item in self.media}) != len(self.media):
            raise ValueError("Reference assets must be unique per shot")
        if self.spec.mode == "t2v":
            if self.parameters.ratio != "16:9":
                raise ValueError("Text-to-video sends the frozen 16:9 ratio")
        elif self.parameters.ratio is not None:
            raise ValueError("Image modes must not send a ratio; the input frame decides it")
        return self

    def public_arguments(self) -> dict:
        return {
            name: self.model_dump(mode="json", by_alias=True)[name]
            for name in ("provider", "model", "capabilitiesVersion", "prompt", "spec", "sourceRefs")
        }

    def intent_fingerprint(self) -> str:
        return canonical_fingerprint(
            {
                "intentVersion": 3,
                "prompt": self.prompt,
                "spec": self.spec.model_dump(mode="json", by_alias=True),
                "media": [
                    {"frame": item.frame, "assetId": item.asset_id, "sha256": item.sha256}
                    for item in self.media
                ],
            }
        )

    def fingerprint(self) -> str:
        return canonical_fingerprint(
            {"requestVersion": 5, "request": self.model_dump(mode="json", by_alias=True)}
        )


class VideoCapabilitiesV3(VideoCapabilitiesV2):
    """One verified M2 entry: text-to-video or first-frame/first-and-last-frame."""

    mode: Literal["live"] = "live"
    capabilities_version: Literal["wan27-shot-beijing-v2"] = WAN_SHOT_CAPABILITIES_VERSION
    shot_modes: JsonTuple[Literal["t2v", "i2v", "flf2v"]] = Field(min_length=1, max_length=3)
    specs: JsonTuple[ShotSpecV3] = Field(min_length=1, max_length=128)
    input_types: JsonTuple[Literal["text", "image"]] = ("text",)
    image_input: ImageInputRule | None = None

    @model_validator(mode="after")
    def unique_capabilities(self):
        if not self.input_types or len(set(self.input_types)) != len(self.input_types):
            raise ValueError("Input types must be unique and non-empty")
        if len(set(self.specs)) != len(self.specs) or len(set(self.shot_modes)) != len(self.shot_modes):
            raise ValueError("Complete specification combinations and modes must be unique")
        if {item.mode for item in self.specs} != set(self.shot_modes):
            raise ValueError("Every declared mode needs at least one verified specification")
        if ("image" in self.input_types) != (self.image_input is not None):
            raise ValueError("Image input modes publish their exact image constraints")
        return self


def shot_price(rate: VideoPrice, duration: int) -> VideoPrice:
    """The per-second rate applied to one shot's verified duration."""
    return VideoPrice(
        currency=rate.currency,
        unit=rate.unit,
        unit_price=rate.unit_price,
        quantity=str(duration),
        amount=_money(Decimal(rate.unit_price) * duration),
        price_version=rate.price_version,
        checked_at=rate.checked_at,
        source_url=rate.source_url,
    )


def _shot_rate(price_version: str) -> VideoPrice:
    return VideoPrice(
        unit_price="0.60",
        quantity="1",
        amount="0.60",
        price_version=price_version,
        checked_at="2026-10-16",
        source_url=WAN_PRICE_SOURCE,
    )


def wan_shot_capabilities() -> tuple[VideoCapabilitiesV3, VideoCapabilitiesV3]:
    """Two verified live entries: text-to-video, and the image model for i2v/flf2v."""
    text_specs = tuple(
        ShotSpecV3(mode="t2v", duration_seconds=duration, aspect_ratio="16:9") for duration in SHOT_DURATIONS
    )
    image_specs = tuple(
        ShotSpecV3(mode=mode, duration_seconds=duration, aspect_ratio=None)
        for mode in ("i2v", "flf2v")
        for duration in SHOT_DURATIONS
    )
    return (
        VideoCapabilitiesV3(
            provider="wan",
            model=WAN_MODEL,
            shot_modes=("t2v",),
            specs=text_specs,
            price=_shot_rate(WAN_PRICE_VERSION),
        ),
        VideoCapabilitiesV3(
            provider="wan",
            model=WAN_I2V_MODEL,
            shot_modes=("i2v", "flf2v"),
            specs=image_specs,
            input_types=("text", "image"),
            image_input=ImageInputRule(),
            price=_shot_rate(WAN_I2V_PRICE_VERSION),
        ),
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


class JobResultV3(JobResultV2):
    """An M2 shot result: the delivered local media belongs to one shot specification.

    `JobResultV2.spec` is the frozen single-shot specification, so a shot Job needs
    its own result type; `JobV3` coherence compares this spec with the frozen shot
    request, which a V2 result could never equal.
    """

    spec: ShotSpecV3


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


def _coherent_live_state(job: "JobV2 | JobV3 | JobV4 | JobV5") -> None:
    if datetime.fromisoformat(job.updated_at) < datetime.fromisoformat(job.created_at):
        raise ValueError("Job cannot be updated before creation")
    if job.submit_attempts != (0 if job.status == "pending_submit" else 1):
        raise ValueError("Only one durably recorded submission is allowed")
    if job.submit_attempts:
        if (
            job.submission_started_at is None
            or job.query_deadline_at is None
            or (
                datetime.fromisoformat(job.query_deadline_at)
                - datetime.fromisoformat(job.submission_started_at)
            )
            != timedelta(hours=24)
        ):
            raise ValueError("Submission records a fixed 24-hour query window")
    elif job.submission_started_at is not None or job.query_deadline_at is not None:
        raise ValueError("Unsubmitted jobs have no submission/query window")
    if job.status in {"pending_submit", "submitting", "unknown"} and job.provider_task_id is not None:
        raise ValueError("Unconfirmed submission cannot have a confirmed task ID")
    if (
        job.status in {"queued", "running", "downloading", "download_failed", "succeeded"}
        and job.provider_task_id is None
    ):
        raise ValueError("Accepted jobs require the original task ID")
    if (job.status == "succeeded") != (job.result is not None):
        raise ValueError("Only committed local success contains a result")
    generated = job.status in {"downloading", "download_failed", "succeeded"}
    if generated != (job.provider_output is not None) or generated != (job.download is not None):
        raise ValueError("Cloud success must atomically register one output and download intent")
    if generated:
        if job.provider_output.task_id != job.provider_task_id or job.last_provider_status != "SUCCEEDED":
            raise ValueError("Private output must belong to the confirmed successful task")
        if job.download.relative_path != f"media/{job.context.project_id}/{job.download.media_id}.mp4":
            raise ValueError("Download location must be derived from server IDs")
        if job.download.window_attempts > job.download_policy.max_attempts_per_window:
            raise ValueError("Download retries cannot exceed the saved window")
        if job.download.prepared and job.download.prepared.size_bytes > job.download_policy.max_bytes:
            raise ValueError("Prepared media cannot exceed the frozen resource limit")
        if job.cost.provider_usage != job.provider_output.usage:
            raise ValueError("Provider usage comes only from the original task output")
    if job.result is not None and (
        job.result.request_fingerprint != job.request_fingerprint
        or job.result.spec != job.request.spec
        or job.result.source_refs != job.request.source_refs
        or job.result.media_refs[0].media_id != job.download.media_id
        or (not job.download.repair and job.download.phase != "committed")
    ):
        raise ValueError("Local result and download must retain the Job's provenance")
    if job.status != "succeeded" and (
        job.media_availability.status == "available" or (job.download and job.download.repair)
    ):
        raise ValueError("Media is available or repairable only after its first local delivery")
    if job.status in {"unknown", "failed", "download_failed"}:
        if job.error is None or job.error.stage == "query":
            raise ValueError("Terminal failure requires the correct error stage")
        if job.status == "unknown" and (
            job.error.stage != "submit" or job.error.code != "SUBMISSION_UNKNOWN"
        ):
            raise ValueError("Unknown means submission uncertainty")
        if job.status == "failed" and (
            job.error.stage not in {"submit", "generate"}
            or (job.error.stage == "generate") != bool(job.provider_task_id)
        ):
            raise ValueError("Only a confirmed generation failure has a task ID")
        if job.status == "download_failed" and (
            job.error.stage != "download" or job.download.phase != "failed"
        ):
            raise ValueError("Download failure must retain its failed download intent")
    elif job.error is not None and (
        job.status not in {"queued", "running"}
        or job.error.stage != "query"
        or job.query_state not in {"retrying", "paused"}
    ):
        raise ValueError("Query errors preserve the last confirmed generation state")
    active = job.status in {"queued", "running"}
    if (job.query_state != "idle") != active:
        raise ValueError("Only unfinished cloud tasks have a query lifecycle")
    if (job.query_state in {"polling", "retrying"}) != (job.next_poll_at is not None):
        raise ValueError("Only scheduled queries have a next poll time")
    if (job.query_state == "paused") != (job.query_pause_reason is not None):
        raise ValueError("A paused query must preserve its reason")
    if job.query_started_at is not None and (
        job.query_state not in {"polling", "retrying"}
        or not job.query_attempts
        or datetime.fromisoformat(job.query_started_at) > datetime.fromisoformat(job.updated_at)
    ):
        raise ValueError("In-flight queries need a persisted attempt")
    if job.query_state in {"retrying", "paused"} and job.error is None:
        raise ValueError("Unhealthy queries require a safe error")
    if job.consecutive_query_errors > job.query_attempts or (not job.provider_task_id and job.query_attempts):
        raise ValueError("Query counters require original task attempts")
    retries = len(job.policy.retry_delays_seconds)
    if job.query_state == "retrying" and not 1 <= job.consecutive_query_errors <= retries:
        raise ValueError("Query retry window is bounded")
    if job.query_pause_reason == "retry_exhausted" and job.consecutive_query_errors != retries + 1:
        raise ValueError("Retry exhaustion consumes the saved retry window")
    if job.query_state in {"idle", "polling"} and job.consecutive_query_errors:
        raise ValueError("Healthy or completed queries have no consecutive errors")
    if job.runtime_block is not None and (
        (job.status == "pending_submit" and job.runtime_block.stage != "submit")
        or (active and (job.runtime_block.stage != "query" or job.query_pause_reason != "configuration"))
        or (job.status != "pending_submit" and not active)
    ):
        raise ValueError("Runtime blocks describe pending submission or paused query configuration")


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
        # Pydantic keeps this frozen v2 validator on subclasses as well; the generic
        # v6 contract has its own quote and bucket invariants.
        if isinstance(self, JobV6):
            return self
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
        _coherent_live_state(self)
        return self


class ShotRef(Contract):
    """The immutable link from a real Job back to one storyboard shot version."""

    storyboard_id: Identifier
    storyboard_version: int = Field(ge=1)
    shot_id: Identifier
    shot_version_id: Identifier


class RunLimitsSnapshot(Contract):
    """Registration-time snapshot of the frozen ceilings one batch was checked against.

    ``scope="run"`` keeps the M2/M3 meaning byte-for-byte: at most
    ``LIVE_JOBS_PER_RUN`` Jobs of this Run are registered. M5 E3 adds
    ``scope="episode"``: the same fields then describe one episode's frozen
    ``shotBudget`` bucket (``jobCap`` Jobs and ``episodeCapAmount``), because an
    episode may span several Runs and every Job must prove the episode boundary it
    was registered under. The scope is stored on the Job, so the additive field
    needs no new Job contract version and old records default to ``run``.
    """

    max_job_cost: Money
    max_new_jobs_per_run: int = Field(ge=1, le=EPISODE_JOB_CAP)
    max_run_cost: Money
    used_new_jobs: int = Field(ge=0, le=EPISODE_JOB_CAP)
    used_amount: Money
    remaining_amount: Money
    scope: Literal["run", "episode"] = "run"
    episode_id: Identifier | None = None

    @model_validator(mode="after")
    def consistent_usage(self):
        if self.scope == "episode":
            if self.episode_id is None:
                raise ValueError("An episode-scoped snapshot must name its episode")
        else:
            if self.episode_id is not None:
                raise ValueError("A Run-scoped snapshot cannot carry an episode ID")
            if self.max_new_jobs_per_run > LIVE_JOBS_PER_RUN or self.used_new_jobs > LIVE_JOBS_PER_RUN:
                raise ValueError("A Run-scoped snapshot keeps the frozen per-Run Job ceiling")
        if self.used_new_jobs > self.max_new_jobs_per_run:
            raise ValueError("Recorded usage cannot exceed the frozen Job ceiling")
        if Decimal(self.used_amount) > Decimal(self.max_run_cost) or Decimal(
            self.remaining_amount
        ) != Decimal(self.max_run_cost) - Decimal(self.used_amount):
            raise ValueError("Remaining budget is the frozen ceiling minus recorded usage")
        return self


class CostRecordV3(CostRecord):
    limits: RunLimitsSnapshot


class JobV3(JobV2):
    """One shot of a storyboard version as a real Job with its own frozen quote."""

    contract_version: int = Field(default=3, ge=3, le=3)
    request: VideoRequestV3
    capabilities: VideoCapabilitiesV3
    shot_ref: ShotRef
    cost: CostRecordV3
    result: JobResultV3 | None = None

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
        ):
            raise ValueError("Frozen request does not match the capability snapshot")
        if (
            self.request.spec not in self.capabilities.specs
            or self.request.spec.mode not in self.capabilities.shot_modes
        ):
            raise ValueError("Frozen shot specification is not a verified combination")
        estimate = self.cost.estimate
        if estimate.model_dump(exclude={"max_job_cost"}) != shot_price(
            self.capabilities.price, self.request.spec.duration_seconds
        ).model_dump() or Decimal(estimate.amount) > Decimal(estimate.max_job_cost):
            raise ValueError("Job must retain an affordable quote for its original shot")
        _coherent_live_state(self)
        return self


class JobV4(JobV2):
    """One keys-only single-shot live Job: the frozen V2 lifecycle, shared endpoint."""

    contract_version: int = Field(default=4, ge=4, le=4)
    request: VideoRequestV4


class JobV5(JobV3):
    """One keys-only shot live Job: the frozen V3 lifecycle, shared endpoint."""

    contract_version: int = Field(default=5, ge=5, le=5)
    request: VideoRequestV5


# --- M3 E2: generic multi-provider requests (contract 6) ---------------------
# E2 adds one direct provider and one relay beside the frozen Wan models. Only the
# minimal verified combinations are published: text-to-video, 5 s, 720p, 16:9.
# Historical requests 1-5 stay their own classes; Wan is never re-encoded as 6.

MULTI_PROVIDER_CAPABILITIES_VERSION = "multi-provider-v3"

ProviderModel = Annotated[
    str, Field(min_length=1, max_length=256, pattern=r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,255}$")
]
# Unit prices keep their public precision (0.112 USD/s); amounts stay 2 decimals.
UnitPrice = Annotated[str, Field(max_length=32, pattern=r"^(?:0|[1-9][0-9]*)(?:\.[0-9]{1,4})?$")]
UnitsPerSecond = Annotated[str, Field(max_length=32, pattern=r"^(?:0\.[0-9]{1,4}|[1-9][0-9]*)$")]
Currency = Literal["CNY", "USD"]
LiveProvider = Literal["wan", "kling", "fal"]

KLING_PROVIDER = "kling"
KLING_MODEL = "kling-3.0-turbo"
KLING_REGION = "singapore"
KLING_ENDPOINT_PROFILE = "kling-singapore-v1"
KLING_ADAPTER_VERSION = "kling-http-v1"
KLING_PRICE_VERSION = "kling30-turbo-720p-20261021"
KLING_SOURCE_POLICY_VERSION = "kling-auth-result-origin-v1"
KLING_PRICE_SOURCE = "https://kling.ai/document-api/pricing/base/video.md"
KLING_RATE_USD = "0.112"
KLING_UNITS_PER_SECOND = "0.8"
FAL_PROVIDER = "fal"
FAL_MODEL = "fal-ai/kling-video/v3/standard/text-to-video"
FAL_REGION = "global"
FAL_ENDPOINT_PROFILE = "fal-queue-v1"
FAL_ADAPTER_VERSION = "fal-http-v1"
FAL_PRICE_VERSION = "fal-kling30-standard-silent-20261021"
FAL_SOURCE_POLICY_VERSION = "fal-auth-result-origin-v1"
FAL_PRICE_SOURCE = "https://fal.ai/models/fal-ai/kling-video/v3/standard/text-to-video"
FAL_RATE_USD = "0.084"
MULTI_PROVIDER_PRICE_CHECKED_AT = "2026-10-21"

# Frozen per-provider ceilings. Users may tighten them in local settings; the
# effective ceiling is the smaller of the stored value and these constants.
KLING_MAX_JOB_USD = "0.60"
KLING_MAX_RUN_USD = "2.40"
KLING_MAX_JOB_UNITS = 4
KLING_MAX_RUN_UNITS = 16
FAL_MAX_JOB_USD = "0.50"
FAL_MAX_RUN_USD = "2.00"

AUTH_RESULT_ORIGIN_POLICIES = frozenset({KLING_SOURCE_POLICY_VERSION, FAL_SOURCE_POLICY_VERSION})

_PROVIDER_PROFILES = {
    KLING_PROVIDER: (
        KLING_MODEL,
        KLING_REGION,
        KLING_ENDPOINT_PROFILE,
        KLING_ADAPTER_VERSION,
        KLING_PRICE_VERSION,
        KLING_SOURCE_POLICY_VERSION,
    ),
    FAL_PROVIDER: (
        FAL_MODEL,
        FAL_REGION,
        FAL_ENDPOINT_PROFILE,
        FAL_ADAPTER_VERSION,
        FAL_PRICE_VERSION,
        FAL_SOURCE_POLICY_VERSION,
    ),
}


class ProviderRate(Contract):
    """The published per-output-second rate of one provider/model entry."""

    currency: Currency
    unit: Literal["output_second"] = "output_second"
    unit_price: UnitPrice
    price_version: Name
    checked_at: str = Field(pattern=r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$")
    source_url: str = Field(min_length=1, max_length=1000, pattern=r"^https://")

    @model_validator(mode="after")
    def valid_date(self):
        datetime.strptime(self.checked_at, "%Y-%m-%d")
        return self


class ProviderPrice(Contract):
    """One frozen quote in the provider's own billing currency (never converted)."""

    currency: Currency
    unit: Literal["output_second"] = "output_second"
    unit_price: UnitPrice
    quantity: str = Field(pattern=r"^[1-9][0-9]{0,8}$")
    amount: Money
    price_version: Name
    checked_at: str = Field(pattern=r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$")
    source_url: str = Field(min_length=1, max_length=1000, pattern=r"^https://")

    @model_validator(mode="after")
    def correct_amount(self):
        if Decimal(self.unit_price) * Decimal(self.quantity) != Decimal(self.amount):
            raise ValueError("Estimate must equal its frozen unit price times quantity")
        datetime.strptime(self.checked_at, "%Y-%m-%d")
        return self


class ResourceRate(Contract):
    """Kling's second billing dimension: resource units per output second."""

    unit: Literal["VIDEO_UNIT"] = "VIDEO_UNIT"
    units_per_second: UnitsPerSecond
    price_version: Name


class ResourceEstimate(Contract):
    unit: Literal["VIDEO_UNIT"] = "VIDEO_UNIT"
    units_per_second: UnitsPerSecond
    quantity: str = Field(pattern=r"^[1-9][0-9]{0,8}$")
    units: int = Field(ge=0)
    price_version: Name

    @model_validator(mode="after")
    def exact_units(self):
        product = Decimal(self.units_per_second) * Decimal(self.quantity)
        if product != Decimal(self.units):
            raise ValueError("Resource units must equal the frozen rate times duration")
        return self


def provider_price(rate: ProviderRate, duration_seconds: int) -> ProviderPrice:
    amount = Decimal(rate.unit_price) * duration_seconds
    return ProviderPrice(
        currency=rate.currency,
        unit=rate.unit,
        unit_price=rate.unit_price,
        quantity=str(duration_seconds),
        amount=_money(amount),
        price_version=rate.price_version,
        checked_at=rate.checked_at,
        source_url=rate.source_url,
    )


def resource_estimate(rate: ResourceRate, duration_seconds: int) -> ResourceEstimate:
    product = Decimal(rate.units_per_second) * duration_seconds
    if product != product.to_integral_value():
        raise ValueError("A resource estimate must be a whole number of units")
    return ResourceEstimate(
        units_per_second=rate.units_per_second,
        quantity=str(duration_seconds),
        units=int(product),
        price_version=rate.price_version,
    )


class VideoCapabilitiesV4(Contract):
    """One verified live entry of the multi-provider capability table (v3)."""

    provider: Literal["kling", "fal"]
    model: ProviderModel
    capabilities_version: Literal["multi-provider-v3"] = MULTI_PROVIDER_CAPABILITIES_VERSION
    mode: Literal["live"] = "live"
    region: Name
    endpoint_profile: Name
    adapter_version: Name
    source_policy_version: Name
    price_version: Name
    shot_modes: JsonTuple[Literal["t2v"]] = ("t2v",)
    input_types: JsonTuple[Literal["text"]] = ("text",)
    specs: JsonTuple[ShotSpecV3] = Field(min_length=1, max_length=8)
    audio_mode: Literal["native", "silent"]
    max_prompt_characters: int = Field(default=2500, ge=2500, le=2500)
    supports_cancel: bool = False
    supports_idempotent_submit: bool = False
    supports_result_url_refresh: bool = False
    rate: ProviderRate
    resource: ResourceRate | None = None

    @model_validator(mode="after")
    def frozen_entry(self):
        if self.supports_cancel or self.supports_idempotent_submit or self.supports_result_url_refresh:
            raise ValueError("M3 providers do not advertise unsupported options")
        expected = _PROVIDER_PROFILES[self.provider]
        if (
            self.model,
            self.region,
            self.endpoint_profile,
            self.adapter_version,
            self.price_version,
            self.source_policy_version,
        ) != expected:
            raise ValueError("A capability entry must use its provider's frozen profile")
        if self.price_version != self.rate.price_version:
            raise ValueError("The frozen rate and price version must agree")
        if len(set(self.specs)) != len(self.specs) or {item.mode for item in self.specs} != {"t2v"}:
            raise ValueError("Only unique, verified text-to-video specifications are published")
        native = self.provider == KLING_PROVIDER
        if (self.audio_mode == "native") != native:
            raise ValueError("Kling publishes native audio; the fal standard entry is silent")
        if (self.resource is not None) != native:
            raise ValueError("Only Kling bills resource units in this table")
        if self.resource is not None and self.resource.price_version != self.price_version:
            raise ValueError("The resource rate belongs to the same price version")
        return self


def _multi_provider_specs() -> tuple[ShotSpecV3, ...]:
    return (ShotSpecV3(mode="t2v", duration_seconds=5, aspect_ratio="16:9"),)


def kling_capabilities() -> VideoCapabilitiesV4:
    return VideoCapabilitiesV4(
        provider=KLING_PROVIDER,
        model=KLING_MODEL,
        region=KLING_REGION,
        endpoint_profile=KLING_ENDPOINT_PROFILE,
        adapter_version=KLING_ADAPTER_VERSION,
        source_policy_version=KLING_SOURCE_POLICY_VERSION,
        price_version=KLING_PRICE_VERSION,
        specs=_multi_provider_specs(),
        audio_mode="native",
        rate=ProviderRate(
            currency="USD",
            unit_price=KLING_RATE_USD,
            price_version=KLING_PRICE_VERSION,
            checked_at=MULTI_PROVIDER_PRICE_CHECKED_AT,
            source_url=KLING_PRICE_SOURCE,
        ),
        resource=ResourceRate(units_per_second=KLING_UNITS_PER_SECOND, price_version=KLING_PRICE_VERSION),
    )


def fal_capabilities() -> VideoCapabilitiesV4:
    return VideoCapabilitiesV4(
        provider=FAL_PROVIDER,
        model=FAL_MODEL,
        region=FAL_REGION,
        endpoint_profile=FAL_ENDPOINT_PROFILE,
        adapter_version=FAL_ADAPTER_VERSION,
        source_policy_version=FAL_SOURCE_POLICY_VERSION,
        price_version=FAL_PRICE_VERSION,
        specs=_multi_provider_specs(),
        audio_mode="silent",
        rate=ProviderRate(
            currency="USD",
            unit_price=FAL_RATE_USD,
            price_version=FAL_PRICE_VERSION,
            checked_at=MULTI_PROVIDER_PRICE_CHECKED_AT,
            source_url=FAL_PRICE_SOURCE,
        ),
    )


def multi_provider_capabilities() -> tuple[VideoCapabilitiesV4, VideoCapabilitiesV4]:
    return (kling_capabilities(), fal_capabilities())


class KlingParameters(Contract):
    """The exact Kling `settings` block; nothing else is sent."""

    duration: Literal[5] = 5
    resolution: Literal["720p"] = "720p"
    aspect_ratio: Literal["16:9"] = "16:9"


class FalParameters(Contract):
    """The exact fal input; the silent tier is fixed so no audio fee is implied."""

    duration: Literal["5"] = "5"
    aspect_ratio: Literal["16:9"] = "16:9"
    generate_audio: Literal[False] = False
    shot_type: Literal["customize"] = "customize"
    enable_safety_checker: Literal[True] = True
    cfg_scale: Literal[0.5] = 0.5
    negative_prompt: Literal["blur, distort, and low quality"] = "blur, distort, and low quality"


ProviderParameters = KlingParameters | FalParameters
_PARAMETER_TYPES = {KLING_PROVIDER: KlingParameters, FAL_PROVIDER: FalParameters}


class VideoRequestV6(Contract):
    """One frozen multi-provider text-to-video request."""

    provider: Literal["kling", "fal"]
    model: ProviderModel
    mode: Literal["t2v"] = "t2v"
    region: Name
    endpoint_profile: Name
    adapter_version: Name
    capabilities_version: Literal["multi-provider-v3"]
    price_version: Name
    source_policy_version: Name
    prompt: str = Field(min_length=1, max_length=2500)
    spec: ShotSpecV3
    source_refs: JsonTuple[ArtifactRef] = Field(default=(), max_length=16)
    provider_prompt: str = Field(min_length=1, max_length=2500)
    parameters: ProviderParameters

    @field_validator("prompt")
    @classmethod
    def nonblank_prompt(cls, value):
        if not value.strip():
            raise ValueError("Prompt cannot be blank")
        return value.strip()

    @model_validator(mode="after")
    def complete_request(self):
        expected = _PROVIDER_PROFILES[self.provider]
        if (
            self.model,
            self.region,
            self.endpoint_profile,
            self.adapter_version,
            self.price_version,
            self.source_policy_version,
        ) != expected:
            raise ValueError("The frozen request must use its provider's exact profile")
        if type(self.parameters) is not _PARAMETER_TYPES[self.provider]:
            raise ValueError("The frozen parameters must match the provider protocol")
        if self.provider_prompt != self.prompt:
            raise ValueError("A multi-provider prompt is sent exactly as written")
        if self.spec != ShotSpecV3(mode="t2v", duration_seconds=5, aspect_ratio="16:9"):
            raise ValueError("Only the frozen 5 s / 720p / 16:9 text-to-video entry is published")
        return self

    def public_arguments(self) -> dict:
        """The public camelCase arguments a Job may show; never private fields."""
        return {
            name: self.model_dump(mode="json", by_alias=True)[name]
            for name in ("provider", "model", "capabilitiesVersion", "prompt", "spec", "sourceRefs")
        }

    def intent_fingerprint(self) -> str:
        return canonical_fingerprint({"intentVersion": 4, "arguments": self.public_arguments()})

    def fingerprint(self) -> str:
        return canonical_fingerprint(
            {"requestVersion": 6, "request": self.model_dump(mode="json", by_alias=True)}
        )


_HOSTNAME = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)+")


class VideoIntentV6(Contract):
    """The six public arguments of one multi-provider registration call."""

    provider: Literal["wan", "kling", "fal"]
    model: ProviderModel
    capabilities_version: Name
    prompt: str = Field(min_length=1, max_length=8000)
    spec: VideoSpecV2
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

    def public_arguments(self) -> dict:
        return self.model_dump(mode="json", by_alias=True)

    def intent_fingerprint(self) -> str:
        # The Wan path freezes its own request (intent 2); the generic providers
        # keep intent 4 so a recorded Run replays the same normalized intent.
        version = 2 if self.provider == "wan" else 4
        return canonical_fingerprint({"intentVersion": version, "arguments": self.public_arguments()})

    def fingerprint(self) -> str:
        return canonical_fingerprint({"intentVersion": 4, "arguments": self.public_arguments()})


class ResultSource(Contract):
    """The authenticated task result's exact origin and URL fingerprint."""

    task_id: ProviderId
    origin: str = Field(min_length=10, max_length=300, pattern=r"^https://[a-z0-9.-]+:443$")
    url_fingerprint: Fingerprint
    source_policy_version: Name


def bound_result_source(task_id: str, url: str, source_policy_version: str) -> ResultSource:
    """Bind one success URL to its exact HTTPS origin, as returned under auth.

    The fingerprint covers the URL bytes exactly as received: no percent decoding,
    query reordering or path normalization.
    """
    parsed = urlsplit(url)
    host = parsed.hostname
    if (
        not isinstance(url, str)
        or len(url) > 8192
        or any(ord(char) < 33 or ord(char) == 127 for char in url)
        or parsed.scheme != "https"
        or not host
        or parsed.username is not None
        or parsed.password is not None
        or parsed.port not in {None, 443}
        or bool(parsed.fragment)
    ):
        raise ValueError("A provider result must be a bounded HTTPS URL")
    try:
        ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        raise ValueError("An IP literal is not an acceptable result origin")
    try:
        normalized = host.encode("idna").decode("ascii").lower()
    except UnicodeError:
        raise ValueError("The result host cannot be normalized") from None
    if not _HOSTNAME.fullmatch(normalized):
        raise ValueError("The result host is not a canonical DNS name")
    return ResultSource(
        task_id=task_id,
        origin=f"https://{normalized}:443",
        url_fingerprint=hashlib.sha256(url.encode("utf-8")).hexdigest(),
        source_policy_version=source_policy_version,
    )


class ProviderOutputV2(ProviderOutput):
    result_source: ResultSource


class ProviderTaskSnapshotV3(Contract):
    task_id: ProviderId
    request_id: ProviderId | None = None
    status: Literal["queued", "running", "succeeded", "failed"]
    output: ProviderOutputV2 | None = None
    error: JobErrorV2 | None = None

    @model_validator(mode="after")
    def terminal_payload(self):
        if (self.status == "succeeded") != (self.output is not None):
            raise ValueError("Only provider success contains private output")
        if self.output is not None and (
            self.output.task_id != self.task_id or self.output.result_source.task_id != self.task_id
        ):
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


class ProviderTaskHandleV3(Contract):
    task_id: ProviderId
    request_id: ProviderId | None = None
    snapshot: ProviderTaskSnapshotV3 | None = None

    @model_validator(mode="after")
    def same_task(self):
        if self.snapshot is not None and self.snapshot.task_id != self.task_id:
            raise ValueError("Handle and snapshot must identify the same task")
        return self


class ProviderActualCost(Contract):
    """No bill evidence in this stage: an actual cost is always unknown."""

    status: Literal["unknown"] = "unknown"
    currency: Currency
    amount: None = None
    source: None = None


class AmountLimit(Contract):
    """One provider/currency budget bucket; USD never pools across providers."""

    provider: LiveProvider
    currency: Currency
    max_job_amount: Money
    max_run_amount: Money
    used_amount: Money
    remaining_amount: Money
    max_job_units: int | None = Field(default=None, ge=0)
    max_run_units: int | None = Field(default=None, ge=0)
    used_units: int | None = Field(default=None, ge=0)
    remaining_units: int | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def coherent_bucket(self):
        if Decimal(self.used_amount) > Decimal(self.max_run_amount):
            raise ValueError("A bucket cannot use more than its Run ceiling")
        if Decimal(self.remaining_amount) != Decimal(self.max_run_amount) - Decimal(self.used_amount):
            raise ValueError("Remaining budget is the ceiling minus recorded usage")
        units = (self.max_job_units, self.max_run_units, self.used_units, self.remaining_units)
        if any(value is None for value in units) and any(value is not None for value in units):
            raise ValueError("A bucket either tracks units or does not")
        if self.max_run_units is not None:
            if self.max_job_units > self.max_run_units:
                raise ValueError("The per-Job unit ceiling cannot exceed the Run ceiling")
            if self.used_units > self.max_run_units:
                raise ValueError("A bucket cannot use more units than its Run ceiling")
            if self.remaining_units != self.max_run_units - self.used_units:
                raise ValueError("Remaining units are the ceiling minus recorded usage")
        return self


class RunLimitsSnapshotV2(Contract):
    """Registration-time snapshot of every provider/currency bucket of one Run."""

    max_new_jobs_per_run: int = Field(ge=1, le=LIVE_JOBS_PER_RUN)
    used_new_jobs: int = Field(ge=0, le=LIVE_JOBS_PER_RUN)
    buckets: JsonTuple[AmountLimit] = Field(min_length=1, max_length=8)

    @model_validator(mode="after")
    def consistent_usage(self):
        if self.used_new_jobs > self.max_new_jobs_per_run:
            raise ValueError("Recorded usage cannot exceed the frozen Job ceiling")
        keys = [(bucket.provider, bucket.currency) for bucket in self.buckets]
        if len(set(keys)) != len(keys):
            raise ValueError("Each provider/currency bucket appears once")
        return self


class JobCostV6(Contract):
    estimate: ProviderPrice
    max_job_amount: Money
    resource_estimate: ResourceEstimate | None = None
    limits: RunLimitsSnapshotV2
    provider_usage: ProviderUsage | None = None
    actual: ProviderActualCost

    @model_validator(mode="after")
    def affordable_quote(self):
        if Decimal(self.estimate.amount) > Decimal(self.max_job_amount):
            raise ValueError("A Job quote cannot exceed its per-Job ceiling")
        if self.actual.currency != self.estimate.currency:
            raise ValueError("Actual and estimated costs share one currency")
        return self


class JobV6(JobV2):
    """One multi-provider live Job with per-provider/currency budget buckets."""

    contract_version: int = Field(default=6, ge=6, le=6)
    request: VideoRequestV6
    capabilities: VideoCapabilitiesV4
    shot_ref: ShotRef | None = None
    cost: JobCostV6
    provider_output: ProviderOutputV2 | None = None
    result: JobResultV3 | None = None
    result_source: ResultSource | None = None

    @model_validator(mode="after")
    def coherent_state(self):
        if self.operation_key != self.context.operation_key:
            raise ValueError("Job operation key must match its server execution context")
        if (
            self.request_fingerprint != self.request.fingerprint()
            or self.intent_fingerprint != self.request.intent_fingerprint()
        ):
            raise ValueError("Job fingerprints must match the complete frozen request and intent")
        expected = _PROVIDER_PROFILES[self.request.provider]
        if (
            self.request.provider,
            self.request.model,
            self.request.capabilities_version,
            self.request.region,
            self.request.endpoint_profile,
            self.request.adapter_version,
            self.request.price_version,
            self.request.source_policy_version,
        ) != (
            self.capabilities.provider,
            self.capabilities.model,
            self.capabilities.capabilities_version,
            self.capabilities.region,
            self.capabilities.endpoint_profile,
            self.capabilities.adapter_version,
            self.capabilities.price_version,
            self.capabilities.source_policy_version,
        ) or expected != (
            self.request.model,
            self.request.region,
            self.request.endpoint_profile,
            self.request.adapter_version,
            self.request.price_version,
            self.request.source_policy_version,
        ):
            raise ValueError("Frozen request does not match its capability snapshot")
        if (
            self.request.spec not in self.capabilities.specs
            or self.request.spec.mode not in self.capabilities.shot_modes
        ):
            raise ValueError("Frozen shot specification is not a verified combination")
        estimate = provider_price(self.capabilities.rate, self.request.spec.duration_seconds)
        if self.cost.estimate != estimate:
            raise ValueError("Job must retain the frozen quote of its original shot")
        if self.capabilities.resource is None:
            if self.cost.resource_estimate is not None:
                raise ValueError("A silent provider has no resource estimate")
        elif self.cost.resource_estimate != resource_estimate(
            self.capabilities.resource, self.request.spec.duration_seconds
        ):
            raise ValueError("Job must retain the frozen resource estimate of its shot")
        bucket = next(
            (
                item
                for item in self.cost.limits.buckets
                if item.provider == self.request.provider and item.currency == estimate.currency
            ),
            None,
        )
        if bucket is None or bucket.max_job_amount != self.cost.max_job_amount:
            raise ValueError("The Job must retain its provider/currency budget bucket")
        if self.result_source is not None:
            if (
                self.provider_output is None
                or self.provider_task_id != self.result_source.task_id
                or self.result_source.source_policy_version != self.capabilities.source_policy_version
                or self.provider_output.result_source != self.result_source
            ):
                raise ValueError("A result source must bind the confirmed authenticated task")
        elif self.provider_output is not None:
            raise ValueError("A private output always carries its bound result source")
        _coherent_live_state(self)
        return self


# Every live contract version shares one lifecycle; only the frozen request differs.
LiveJob = (JobV2, JobV3, JobV4, JobV5, JobV6)

LiveJobModel = JobV2 | JobV3 | JobV4 | JobV5 | JobV6

JobDispatch = {1: Job, 2: JobV2, 3: JobV3, 4: JobV4, 5: JobV5, 6: JobV6}


def parse_job(value) -> LiveJobModel | Job:
    if isinstance(value, Job):
        value = value.model_dump(mode="json", by_alias=True)
    if not isinstance(value, dict):
        raise ValueError("Job must be an object")
    version = value.get("contractVersion", value.get("contract_version", 1))
    if type(version) is not int or version not in JobDispatch:
        raise ValueError("Unsupported Job contract version")
    return JobDispatch[version].model_validate(value)


StoredJob = Annotated[Job | JobV2 | JobV3 | JobV4 | JobV5 | JobV6, BeforeValidator(parse_job)]


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
