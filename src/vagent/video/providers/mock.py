"""A durable simulated upstream, committed independently from the local Job store."""

import copy
import json
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Literal
from uuid import uuid4

from pydantic import Field, model_validator

from vagent.contracts import Contract, Fingerprint, Identifier, JsonTuple, UtcTimestamp
from vagent.errors import AppError
from vagent.storage import FileStore, atomic_write_json
from vagent.video.contracts import (
    JobError,
    JobResult,
    ProviderCallError,
    ProviderTaskHandle,
    ProviderTaskSnapshot,
    VideoCapabilities,
    VideoRequest,
    VideoSpec,
)
from vagent.video.jobs import utc_now


class MockScenario(Contract):
    """A server-side fixture, never part of the model's generation arguments."""

    submission: Literal["accepted", "rejected", "response_lost"] = "accepted"
    states: JsonTuple[Literal["queued", "running", "succeeded", "failed"]] = Field(
        default=("queued", "running", "succeeded"), min_length=1, max_length=64
    )
    query_error_calls: JsonTuple[int] = Field(default=(), max_length=100)

    @model_validator(mode="after")
    def coherent_scenario(self):
        if len(set(self.query_error_calls)) != len(self.query_error_calls) or any(
            n < 1 for n in self.query_error_calls
        ):
            raise ValueError("Mock query failures identify unique positive call numbers")
        for current, following in zip(self.states, self.states[1:]):
            if current in {"succeeded", "failed"} or (current == "running" and following == "queued"):
                raise ValueError("Mock generation states cannot reverse or follow a terminal state")
        return self


class MockSubmission(Contract):
    operation_key: str = Field(min_length=1, max_length=300)
    request_fingerprint: Fingerprint
    attempted_at: UtcTimestamp
    outcome: Literal["accepted", "rejected", "response_lost"]
    task_id: Identifier | None

    @model_validator(mode="after")
    def confirmed_acceptance(self):
        if (self.outcome == "rejected") != (self.task_id is None):
            raise ValueError("Only rejected mock submissions lack an upstream task")
        return self


class MockTask(Contract):
    id: Identifier
    request: VideoRequest
    scenario: MockScenario
    accepted_at: UtcTimestamp
    query_calls: int = Field(default=0, ge=0)
    state_index: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def valid_position(self):
        if self.state_index >= len(self.scenario.states) or self.state_index > self.query_calls:
            raise ValueError("Mock trajectory position exceeds its recorded queries")
        return self


class MockLedger(Contract):
    schema_version: Literal[1]
    submit_calls: int = Field(default=0, ge=0)
    query_calls: int = Field(default=0, ge=0)
    submissions: list[MockSubmission] = Field(default_factory=list)
    tasks: dict[str, MockTask] = Field(default_factory=dict)

    @model_validator(mode="after")
    def coherent_ledger(self):
        ids = [s.task_id for s in self.submissions if s.task_id is not None]
        if (
            self.submit_calls != len(self.submissions)
            or len(ids) != len(set(ids))
            or set(ids) != self.tasks.keys()
            or any(task.id != key for key, task in self.tasks.items())
            or self.query_calls < sum(task.query_calls for task in self.tasks.values())
        ):
            raise ValueError("Mock ledger counters and accepted tasks must agree")
        for submission in self.submissions:
            if submission.task_id is not None:
                task = self.tasks[submission.task_id]
                if (
                    task.request.fingerprint() != submission.request_fingerprint
                    or task.accepted_at != submission.attempted_at
                    or task.scenario.submission != submission.outcome
                ):
                    raise ValueError("Mock acceptance must preserve the submitted request and scenario")
        return self


class MockVideoAdapter:
    def __init__(
        self,
        store: FileStore,
        *,
        scenario: MockScenario | None = None,
        clock: Callable[[], datetime] = utc_now,
        capabilities: VideoCapabilities | None = None,
    ):
        self.store, self.clock = store, clock
        self.scenario = scenario or MockScenario()
        self.path = store.home / "mock-video.json"
        self._capabilities = capabilities or VideoCapabilities(
            provider="mock",
            model="mock-t2v",
            capabilities_version="v1",
            specs=[
                VideoSpec(duration_seconds=5, resolution="720p", aspect_ratio="16:9"),
                VideoSpec(duration_seconds=10, resolution="1080p", aspect_ratio="9:16"),
            ],
        )
        if self._capabilities.supports_cancel or self._capabilities.supports_idempotent_submit:
            raise ValueError("The mock adapter does not provide cancellation or idempotent submission")
        with self.store.locked():
            self._read()

    def capabilities(self) -> VideoCapabilities:
        return self._capabilities

    def _read(self) -> dict:
        try:
            raw = self.path.read_bytes()
        except FileNotFoundError:
            return MockLedger(schema_version=1).model_dump(mode="json", by_alias=True)
        try:
            value = json.loads(raw)
            if not isinstance(value, dict) or type(value.get("schemaVersion")) is not int:
                raise ValueError("Mock ledger requires an explicit schema version")
            return MockLedger.model_validate(value).model_dump(mode="json", by_alias=True)
        except (ValueError, TypeError):
            raise AppError("INVALID_MOCK_LEDGER", "模拟上游账本损坏或版本不支持，已保留原文件。") from None

    def _transaction(self, mutate: Callable[[dict], object]):
        # Shares the instance lock, but never the Job's state.json commit. A crash
        # after this replace can leave an accepted upstream with no local handle.
        with self.store.locked():
            ledger = self._read()
            result = mutate(ledger)
            MockLedger.model_validate(ledger)
            atomic_write_json(self.path, ledger)
            return copy.deepcopy(result)

    def ledger_snapshot(self) -> dict:
        """Diagnostic evidence for fixtures; the Worker must only use the provider protocol."""
        with self.store.locked():
            return self._read()

    @staticmethod
    def _snapshot(task: dict) -> ProviderTaskSnapshot:
        saved = MockTask.model_validate(task)
        status = saved.scenario.states[saved.state_index]
        return ProviderTaskSnapshot(
            task_id=saved.id,
            status=status,
            result=JobResult(
                request_fingerprint=saved.request.fingerprint(),
                spec=saved.request.spec,
                source_refs=saved.request.source_refs,
                summary="模拟视频任务完成，仅保存结果描述，没有生成可播放媒体。",
            )
            if status == "succeeded"
            else None,
            error=JobError(stage="generate", code="MOCK_GENERATION_FAILED", message="模拟视频生成失败。")
            if status == "failed"
            else None,
        )

    async def submit(self, request: VideoRequest, operation_key: str) -> ProviderTaskHandle:
        capabilities = self.capabilities()
        if (request.provider, request.model, request.capabilities_version) != (
            capabilities.provider,
            capabilities.model,
            capabilities.capabilities_version,
        ) or request.spec not in capabilities.specs:
            raise ProviderCallError(
                JobError(
                    stage="submit", code="MOCK_REQUEST_REJECTED", message="模拟上游拒绝了不支持的请求。"
                ),
                submission_outcome="not_accepted",
            )

        def accept(ledger):
            timestamp = self.clock()
            if timestamp.utcoffset() is None:
                raise ValueError("Mock clocks must return timezone-aware datetimes")
            timestamp = timestamp.astimezone(UTC).isoformat()
            task_id = None if self.scenario.submission == "rejected" else f"mock-{uuid4()}"
            ledger["submitCalls"] += 1
            ledger["submissions"].append(
                MockSubmission(
                    operation_key=operation_key,
                    request_fingerprint=request.fingerprint(),
                    attempted_at=timestamp,
                    outcome=self.scenario.submission,
                    task_id=task_id,
                ).model_dump(mode="json", by_alias=True)
            )
            if task_id is None:
                return None
            task = MockTask(
                id=task_id, request=request, scenario=self.scenario, accepted_at=timestamp
            ).model_dump(mode="json", by_alias=True)
            ledger["tasks"][task_id] = task
            return task

        task = self._transaction(accept)
        if task is None:
            raise ProviderCallError(
                JobError(stage="submit", code="MOCK_SUBMIT_REJECTED", message="模拟上游明确拒绝受理。"),
                submission_outcome="not_accepted",
            )
        if self.scenario.submission == "response_lost":
            raise ProviderCallError(
                JobError(
                    stage="submit", code="SUBMISSION_UNKNOWN", message="模拟提交响应丢失，受理结果不确定。"
                ),
                submission_outcome="unknown",
            )
        return ProviderTaskHandle(task_id=task["id"], snapshot=self._snapshot(task))

    async def query(self, task_id: str) -> ProviderTaskSnapshot:
        def query(ledger):
            ledger["queryCalls"] += 1
            task = ledger["tasks"].get(task_id)
            if task is None or (task["request"]["provider"], task["request"]["model"]) != (
                self._capabilities.provider,
                self._capabilities.model,
            ):
                return None, "MOCK_TASK_NOT_FOUND"
            task["queryCalls"] += 1
            if task["queryCalls"] in task["scenario"]["queryErrorCalls"]:
                return None, "MOCK_QUERY_UNAVAILABLE"
            task["stateIndex"] = min(task["stateIndex"] + 1, len(task["scenario"]["states"]) - 1)
            return task, None

        task, error = self._transaction(query)
        if error:
            raise ProviderCallError(JobError(stage="query", code=error, message="模拟上游查询暂不可用。"))
        return self._snapshot(task)
