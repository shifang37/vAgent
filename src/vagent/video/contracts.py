"""M1-B video requests, provider protocol and persistent Job invariants."""

import hashlib
import json
from collections.abc import Mapping
from datetime import datetime
from typing import Literal, Protocol, runtime_checkable

from pydantic import Field, field_validator, model_validator

from vagent.contracts import Contract, Fingerprint, Identifier, JsonTuple, Name, PositiveSeconds, UtcTimestamp
from vagent.errors import AppError
from vagent.waiting import ToolExecutionContext

VIDEO_CONTRACT_VERSION = 1
VideoMode = Literal["off", "mock"]
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
        self, error: JobError, *, submission_outcome: Literal["not_accepted", "unknown"] | None = None
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
