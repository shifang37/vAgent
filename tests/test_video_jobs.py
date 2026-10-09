import copy
from concurrent.futures import ThreadPoolExecutor

import pytest
from conftest import video_request, video_run

from vagent.errors import AppError
from vagent.storage import FileStore
from vagent.tools import create_project_tools
from vagent.video.contracts import Job, PollingPolicy
from vagent.video.jobs import JobService, changed_job
from vagent.waiting import ToolExecutionContext


def context(call="generate", *, project="coffee", run="video-run"):
    return ToolExecutionContext(
        project_id=project, session_id=project, run_id=run, model_step=1, tool_call_id=call
    )


def adapter(service):
    return service._adapters[("mock", "mock-t2v")]


def test_registration_and_both_dedupe_layers_survive_restart(video_service):
    service = video_service
    provider = adapter(service)
    request = video_request(provider, prompt="  雨夜咖啡店  ")
    first = service.generate(request, context=context())
    job_id = first["data"]["jobId"]
    assert first["data"]["status"] == "pending_submit"
    assert first["data"]["simulated"] and not first["data"]["mediaAvailable"]
    assert service.generate(request, context=context()) == first
    conflict = service.generate({**request, "prompt": request["prompt"].strip()}, context=context())
    assert conflict["error"]["code"] == "OPERATION_CONFLICT"
    normalized = dict(reversed(list({**request, "prompt": request["prompt"].strip()}.items())))
    assert service.generate(normalized, context=context("new-id")) == first
    different = service.generate({**request, "prompt": "另一场景"}, context=context("different"))
    assert different["error"]["code"] == "JOB_ALREADY_EXISTS"
    assert job_id in different["error"]["message"]
    assert len(service.list(project_id="coffee")) == 1
    assert provider.ledger_snapshot()["submitCalls"] == 0
    original = service.store.snapshot()
    saved = original["jobs"][job_id]
    assert saved["operationKey"] == context().operation_key
    assert saved["request"]["prompt"] == "雨夜咖啡店"
    assert original["operations"][context().operation_key]["result"] == first
    home = service.store.home
    service.store.close()
    with FileStore.open(home) as reopened:
        # Committed outcomes remain usable even when the original adapter is absent.
        resumed = JobService(reopened, [])
        assert resumed.generate(request, context=context()) == first
        assert resumed.generate(normalized, context=context("later"))["data"]["jobId"] == job_id
        assert reopened.snapshot()["jobs"] == original["jobs"]


def test_concurrent_calls_share_one_run_slot(video_service):
    request = video_request(adapter(video_service))
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(
            pool.map(lambda n: video_service.generate(request, context=context(f"call-{n}")), range(8))
        )
    assert all(r["ok"] for r in results)
    assert len({r["data"]["jobId"] for r in results}) == 1
    assert len(video_service.store.snapshot()["jobs"]) == 1
    assert len(video_service.store.snapshot()["operations"]) == 8


@pytest.mark.parametrize(
    "changes,code",
    [
        ({"model": "missing"}, "VIDEO_MODEL_UNAVAILABLE"),
        ({"capabilitiesVersion": "old"}, "VIDEO_CAPABILITIES_CHANGED"),
        (
            {"spec": {"durationSeconds": 5, "resolution": "1080p", "aspectRatio": "16:9"}},
            "VIDEO_UNSUPPORTED_SPEC",
        ),
        ({"sourceRefs": [{"artifactId": "missing", "version": 1}]}, "SOURCE_NOT_FOUND"),
        ({"sourceRefs": [{"artifactId": "missing"}]}, "INVALID_ARGUMENTS"),
        ({"projectId": "other"}, "INVALID_ARGUMENTS"),
        ({"mode": "real"}, "INVALID_ARGUMENTS"),
        ({"apiKey": "do-not-record-this-secret"}, "INVALID_ARGUMENTS"),
    ],
)
def test_invalid_requests_create_no_job_or_upstream_call(video_service, changes, code):
    result = video_service.generate(video_request(adapter(video_service), **changes), context=context())
    assert result["error"]["code"] == code
    assert video_service.store.snapshot()["jobs"] == {}
    assert adapter(video_service).ledger_snapshot()["submitCalls"] == 0
    assert "do-not-record-this-secret" not in str(result)


def test_sources_and_policy_are_frozen_and_validated_in_registration_transaction(video_service, monkeypatch):
    service = video_service
    tools = create_project_tools()
    source_args = {"kind": "brief", "title": "source", "content": "original"}
    saved = tools.execute(
        "artifact_save", source_args, store=service.store, project_id="coffee", operation_key="source"
    )["data"]
    source_id = saved["artifactId"]
    request = video_request(adapter(service), sourceRefs=[{"artifactId": source_id, "version": 1}])
    bad_version = copy.deepcopy(request)
    bad_version["sourceRefs"][0]["version"] = 2
    assert (
        service.generate(bad_version, context=context("bad-version"))["error"]["code"]
        == "SOURCE_VERSION_NOT_FOUND"
    )
    other_context = video_run(service.store, "other-run", project_id="other")
    assert service.generate(request, context=other_context)["error"]["code"] == "SOURCE_NOT_FOUND"
    registered = service.generate(request, context=context())
    assert registered["ok"]
    job_id = registered["data"]["jobId"]
    tools.execute(
        "artifact_save",
        {**source_args, "artifactId": source_id, "expectedVersion": 1, "content": "updated"},
        store=service.store,
        project_id="coffee",
        operation_key="update-source",
    )
    request["sourceRefs"][0]["version"] = 2
    service.policy = PollingPolicy(interval_seconds=99)
    job = service.get(job_id, project_id="coffee")
    assert job.request.source_refs[0].version == 1
    assert job.policy.interval_seconds == 2
    assert service.store.snapshot()["artifacts"][source_id]["versions"][0]["content"] == "original"

    # Remove a source after entering generate, immediately before its Store operation.
    new_context = video_run(service.store, "race-run")
    original_operation = service.store.operation

    def race(*args):
        service.store.transaction(lambda draft: draft["artifacts"].pop(source_id))
        return original_operation(*args)

    monkeypatch.setattr(service.store, "operation", race)
    assert service.generate(request, context=new_context)["error"]["code"] == "SOURCE_NOT_FOUND"
    assert len(service.store.snapshot()["jobs"]) == 1


def test_queries_are_project_scoped_and_registration_checks_trusted_context(video_service):
    service = video_service
    request = video_request(adapter(service))
    job_id = service.generate(request, context=context())["data"]["jobId"]
    assert service.list(project_id="other") == ()
    assert service.list(project_id="coffee", session_id="other") == ()
    with pytest.raises(AppError) as error:
        service.get(job_id, project_id="other")
    assert error.value.code == "JOB_NOT_FOUND"
    video_run(service.store, "other-run", project_id="other")
    # Same operation key with a different project must not expose the stored outcome.
    forged = service.generate(request, context=context(project="other"))
    assert forged["error"]["code"] == "JOB_CONTEXT_INVALID"
    read_only = video_run(service.store, "readonly-run", read_only=True)
    assert service.generate(request, context=read_only)["error"]["code"] == "READ_ONLY"


def test_registration_disk_failure_commits_neither_job_nor_operation(video_service, monkeypatch):
    import vagent.storage

    before = video_service.store.snapshot()
    raw = (video_service.store.home / "state.json").read_bytes()

    def fail(*_):
        raise OSError("disk unavailable")

    monkeypatch.setattr(vagent.storage.os, "replace", fail)
    with pytest.raises(OSError):
        video_service.generate(video_request(adapter(video_service)), context=context())
    assert video_service.store.snapshot() == before
    assert (video_service.store.home / "state.json").read_bytes() == raw
    assert not list(video_service.store.home.glob("*.tmp"))


def test_revision_and_immutable_fields_cannot_be_bypassed(video_service):
    service = video_service
    job_id = service.generate(video_request(adapter(service)), context=context())["data"]["jobId"]
    original = service.get(job_id, project_id="coffee")
    submitted = service.update(original, status="submitting", submitAttempts=1)
    with pytest.raises(AppError) as error:
        service.update(original, status="submitting", submitAttempts=1)
    assert error.value.code == "JOB_REVISION_CONFLICT"
    for change, code in [
        ({"policy": PollingPolicy(interval_seconds=17)}, "JOB_IMMUTABLE"),
        ({"status": "pending_submit", "submitAttempts": 0}, "JOB_INVALID_TRANSITION"),
    ]:
        with pytest.raises(AppError) as error:
            service.update(submitted, **change)
        assert error.value.code == code
    with pytest.raises(AppError):
        service.store.transaction(lambda draft: draft["jobs"].pop(job_id))
    changed = changed_job(
        submitted,
        service.timestamp(),
        status="unknown",
        error={"stage": "submit", "code": "SUBMISSION_UNKNOWN", "message": "unknown"},
    )
    no_revision = changed.model_dump(mode="json", by_alias=True)
    no_revision["revision"] = submitted.revision
    with pytest.raises(AppError) as error:
        service.save(Job.model_validate(no_revision), expected_revision=submitted.revision)
    assert error.value.code == "JOB_REVISION_CONFLICT"
    assert service.get(job_id, project_id="coffee") == submitted


def test_closed_store_cannot_write_the_mock_ledger(video_service):
    provider = adapter(video_service)
    video_service.store.close()
    with pytest.raises(AppError) as error:
        provider.ledger_snapshot()
    assert error.value.code == "STORE_CLOSED"
