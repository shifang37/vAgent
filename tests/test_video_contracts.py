import copy

import pytest
from pydantic import ValidationError

from vagent.errors import AppError
from vagent.tools import create_project_tools
from vagent.video.contracts import (
    ArtifactRef,
    CancellableProvider,
    Job,
    JobError,
    JobResult,
    ProviderCallError,
    ProviderTaskHandle,
    ProviderTaskSnapshot,
    VideoCapabilities,
    VideoProviderAdapter,
    VideoRequest,
    VideoSpec,
    job_transition_allowed,
    validate_video_request,
)
from vagent.waiting import ToolExecutionContext


def spec(**changes):
    return VideoSpec.model_validate(
        {"durationSeconds": 5, "resolution": "720p", "aspectRatio": "16:9", **changes}
    )


def capabilities():
    return VideoCapabilities(
        provider="mock",
        model="fixture-t2v",
        capabilities_version="v1",
        specs=[spec(), spec(durationSeconds=10, resolution="1080p", aspectRatio="9:16")],
    )


def request(**changes):
    return VideoRequest.model_validate(
        {
            "provider": "mock",
            "model": "fixture-t2v",
            "capabilitiesVersion": "v1",
            "prompt": "雨夜咖啡店",
            "spec": spec(),
            **changes,
        }
    )


def result(req=None):
    req = req or request()
    return JobResult(
        request_fingerprint=req.fingerprint(),
        spec=req.spec,
        source_refs=req.source_refs,
        summary="模拟视频任务完成，没有真实媒体文件。",
    )


def job(**changes):
    req = request()
    context = ToolExecutionContext(
        project_id="coffee",
        session_id="coffee",
        run_id="original-run",
        model_step=3,
        tool_call_id="generate",
    )
    return Job.model_validate(
        {
            "id": "local-job",
            "context": context,
            "operationKey": context.operation_key,
            "request": req,
            "requestFingerprint": req.fingerprint(),
            "capabilities": capabilities(),
            "createdAt": "2026-10-09T02:00:00+00:00",
            "updatedAt": "2026-10-09T02:00:00+00:00",
            **changes,
        }
    )


def test_request_checks_full_specification_combinations():
    validate_video_request(request(), capabilities(), project_id="coffee", artifacts={})
    with pytest.raises(AppError) as error:
        validate_video_request(
            request(spec=spec(resolution="1080p")), capabilities(), project_id="coffee", artifacts={}
        )
    assert error.value.code == "VIDEO_UNSUPPORTED_SPEC"
    other = VideoCapabilities(
        provider="another", model="different", capabilities_version="v7", specs=[spec(durationSeconds=17)]
    )
    validate_video_request(
        request(
            provider="another", model="different", capabilitiesVersion="v7", spec=spec(durationSeconds=17)
        ),
        other,
        project_id="coffee",
        artifacts={},
    )


@pytest.mark.parametrize(
    "changes,code",
    [
        ({"model": "missing"}, "VIDEO_MODEL_UNAVAILABLE"),
        ({"capabilitiesVersion": "v0"}, "VIDEO_CAPABILITIES_CHANGED"),
    ],
)
def test_request_rejects_stale_or_unconfigured_capabilities(changes, code):
    with pytest.raises(AppError) as error:
        validate_video_request(request(**changes), capabilities(), project_id="coffee", artifacts={})
    assert error.value.code == code


@pytest.mark.parametrize("extra", ["apiKey", "baseUrl", "projectId", "operationKey", "mode"])
def test_request_cannot_override_execution_or_provider_authority(extra):
    with pytest.raises(ValidationError):
        request(**{extra: "not-model-controlled"})


@pytest.mark.parametrize("duration", ["5", True, 0, -1, float("nan"), float("inf")])
def test_specs_reject_coercion_and_nonfinite_or_nonpositive_durations(duration):
    with pytest.raises(ValidationError):
        spec(durationSeconds=duration)


def test_request_snapshot_is_immutable_and_fingerprint_survives_json():
    refs = [{"artifactId": "original", "version": 1}]
    original = request(prompt="  雨夜咖啡店  ", sourceRefs=refs)
    refs[0]["version"] = 2
    refs.append({"artifactId": "later", "version": 1})
    assert original.source_refs == (ArtifactRef(artifact_id="original", version=1),)
    with pytest.raises(ValidationError):
        original.spec.duration_seconds = 10
    with pytest.raises(ValidationError):
        original.source_refs[0].version = 2
    wire = original.model_dump(mode="json", by_alias=True)
    reopened = VideoRequest.model_validate(dict(reversed(list(wire.items()))))
    assert reopened == original and reopened.fingerprint() == original.fingerprint()
    assert request(sourceRefs=wire["sourceRefs"]).fingerprint() == original.fingerprint()
    assert (
        request(sourceRefs=[{"artifactId": "original", "version": 2}]).fingerprint() != original.fingerprint()
    )


def test_source_validation_uses_real_project_artifact_versions(store):
    store.ensure_session("coffee")
    tools = create_project_tools()
    saved = tools.execute(
        "artifact_save",
        {"kind": "brief", "title": "source", "content": "original"},
        store=store,
        project_id="coffee",
        operation_key="source-save",
    )["data"]
    artifact_id = saved["artifactId"]
    original = request(sourceRefs=[{"artifactId": artifact_id, "version": 1}])
    validate_video_request(
        original, capabilities(), project_id="coffee", artifacts=store.snapshot()["artifacts"]
    )
    for project_id, version, code in [
        ("other", 1, "SOURCE_NOT_FOUND"),
        ("coffee", 2, "SOURCE_VERSION_NOT_FOUND"),
    ]:
        with pytest.raises(AppError) as error:
            validate_video_request(
                request(sourceRefs=[{"artifactId": artifact_id, "version": version}]),
                capabilities(),
                project_id=project_id,
                artifacts=store.snapshot()["artifacts"],
            )
        assert error.value.code == code
    assert tools.execute(
        "artifact_save",
        {
            "artifactId": artifact_id,
            "expectedVersion": 1,
            "kind": "brief",
            "title": "updated",
            "content": "new",
        },
        store=store,
        project_id="coffee",
        operation_key="source-update",
    )["ok"]
    validate_video_request(
        original, capabilities(), project_id="coffee", artifacts=store.snapshot()["artifacts"]
    )
    assert original.source_refs[0].version == 1
    assert store.snapshot()["artifacts"][artifact_id]["versions"][0]["content"] == "original"


def test_unknown_submission_and_query_pause_are_distinct_persistent_states():
    unknown = job(
        status="unknown",
        submitAttempts=1,
        error=JobError(stage="submit", code="SUBMISSION_UNKNOWN", message="提交结果不确定"),
    )
    paused = job(
        status="running",
        submitAttempts=1,
        providerTaskId="upstream-1",
        queryAttempts=4,
        consecutiveQueryErrors=4,
        queryState="paused",
        error=JobError(stage="query", code="QUERY_UNAVAILABLE", message="查询暂不可用"),
    )
    for value in (unknown, paused):
        assert Job.model_validate(value.model_dump(mode="json", by_alias=True)) == value
    assert paused.status == "running" and paused.provider_task_id == "upstream-1"
    assert not job_transition_allowed("unknown", "pending_submit")
    assert not job_transition_allowed("running", "submitting")
    assert not job_transition_allowed("running", "queued")
    assert job_transition_allowed("running", "running")
    assert job_transition_allowed("running", "succeeded")


def test_query_retry_window_uses_saved_policy_and_cumulative_attempts():
    fields = {
        "status": "running",
        "submitAttempts": 1,
        "providerTaskId": "upstream",
        "queryAttempts": 1,
        "consecutiveQueryErrors": 1,
        "queryState": "retrying",
        "nextPollAt": "2026-10-09T02:00:01Z",
        "error": JobError(stage="query", code="QUERY_UNAVAILABLE", message="查询中断"),
    }
    retrying = job(**fields)
    assert retrying.policy.retry_delays_seconds == (1.0, 2.0, 4.0)
    for changes in (
        {"consecutiveQueryErrors": 0},
        {"consecutiveQueryErrors": 4, "queryAttempts": 4},
        {"queryState": "paused", "nextPollAt": None},
    ):
        with pytest.raises(ValidationError):
            job(**{**fields, **changes})


@pytest.mark.parametrize(
    "changes",
    [
        {"status": "unknown", "submitAttempts": 1},
        {"status": "submitting", "submitAttempts": 2},
        {
            "status": "queued",
            "submitAttempts": 1,
            "queryState": "polling",
            "nextPollAt": "2026-10-09T02:00:02Z",
        },
        {"operationKey": "another-run:3:generate"},
        {"requestFingerprint": "0" * 64},
        {"createdAt": "2026-10-09T02:00:00"},
        {"contractVersion": 2},
        {"result": result()},
        {"mode": "real"},
    ],
)
def test_impossible_job_states_are_rejected_before_persistence(changes):
    with pytest.raises(ValidationError):
        job(**changes)


def test_success_requires_matching_result_and_never_claims_media():
    succeeded = job(status="succeeded", submitAttempts=1, providerTaskId="upstream", result=result())
    assert succeeded.result.simulated and not succeeded.result.media_available
    assert succeeded.result.artifact_refs == ()
    changed = result(request(prompt="different request"))
    with pytest.raises(ValidationError):
        job(status="succeeded", submitAttempts=1, providerTaskId="upstream", result=changed)
    for change in (
        {"mediaAvailable": True},
        {"simulated": False},
        {"artifactRefs": [{"artifactId": "fake", "version": 1}]},
    ):
        raw = result().model_dump(mode="json", by_alias=True)
        with pytest.raises(ValidationError):
            JobResult.model_validate({**raw, **change})


def test_provider_contract_separates_transport_errors_and_optional_cancel():
    class Adapter:
        def capabilities(self):
            return capabilities()

        async def submit(self, request, operation_key):
            return ProviderTaskHandle(task_id="upstream")

        async def query(self, task_id):
            return ProviderTaskSnapshot(task_id=task_id, status="queued")

    assert isinstance(Adapter(), VideoProviderAdapter)
    assert not isinstance(Adapter(), CancellableProvider)
    error = JobError(stage="submit", code="SUBMISSION_UNKNOWN", message="结果不确定")
    assert ProviderCallError(error, submission_outcome="unknown").submission_outcome == "unknown"
    with pytest.raises(ValueError):
        ProviderCallError(error)
    with pytest.raises(ValidationError):
        ProviderTaskSnapshot(
            task_id="upstream",
            status="failed",
            error=JobError(stage="query", code="QUERY_ERROR", message="查询中断"),
        )
    successful = ProviderTaskSnapshot(task_id="upstream", status="succeeded", result=result())
    assert ProviderTaskHandle(task_id="upstream", snapshot=successful).snapshot == successful
    with pytest.raises(ValidationError):
        ProviderTaskHandle(task_id="different", snapshot=successful)
    assert copy.deepcopy(successful).result == result()
