import copy
import json
from datetime import datetime

import pytest
from conftest import video_request, video_run

from vagent.storage import FileStore
from vagent.tools import ToolDefinition, ToolRegistry, create_project_tools
from vagent.video.jobs import JobService
from vagent.video.providers.mock import MockScenario, MockVideoAdapter
from vagent.video.tools import register_video_tools
from vagent.video.worker import JobWorker
from vagent.waiting import DeferredToolResult, ToolExecutionContext, WaitBinding


def setup_tools(store, clock, *, scenario=None):
    context = video_run(store)
    adapter = MockVideoAdapter(store, clock=clock, scenario=scenario)
    service = JobService(store, [adapter], clock=clock)
    return register_video_tools(create_project_tools(), service), service, adapter, context


def next_call(context, call_id, **changes):
    return ToolExecutionContext.model_validate(
        {**context.model_dump(by_alias=True), "toolCallId": call_id, **changes}
    )


def execute(registry, store, execution_context, name, args, **changes):
    return registry.execute(
        name,
        args,
        **{
            "store": store,
            "project_id": execution_context.project_id,
            "operation_key": execution_context.operation_key,
            "context": execution_context,
            **changes,
        },
    )


def test_generate_is_journaled_once_and_keeps_both_fingerprints(store, video_clock):
    registry, service, adapter, context = setup_tools(store, video_clock)
    args = video_request(adapter, prompt="  雨夜咖啡店  ")
    first = execute(registry, store, context, "video_generate", args)
    assert first["ok"] and first["data"]["status"] == "pending_submit"
    assert set(first["data"]) == {"jobId", "status", "mode", "simulated", "mediaAvailable"}
    job = service.get(first["data"]["jobId"], project_id="coffee")
    assert job.context == context and job.request.prompt == "雨夜咖啡店"
    assert job.submit_attempts == 0 and adapter.ledger_snapshot()["submitCalls"] == 0
    operation = store.snapshot()["operations"][context.operation_key]
    assert operation["result"] == first
    assert operation["fingerprint"] == store.operation_fingerprint("video_generate", args)
    assert execute(registry, store, context, "video_generate", args) == first
    normalized = {**args, "prompt": "雨夜咖啡店", "sourceRefs": []}
    assert (
        execute(registry, store, context, "video_generate", normalized)["error"]["code"]
        == "OPERATION_CONFLICT"
    )
    same = execute(registry, store, next_call(context, "same-request"), "video_generate", normalized)
    assert same["data"]["jobId"] == job.id
    different = execute(
        registry, store, next_call(context, "different-request"), "video_generate", {**args, "prompt": "day"}
    )
    assert different["error"]["code"] == "JOB_ALREADY_EXISTS" and job.id in different["error"]["message"]
    assert len(store.snapshot()["jobs"]) == 1


@pytest.mark.parametrize(
    "field",
    [
        "projectId",
        "sessionId",
        "runId",
        "modelStep",
        "toolCallId",
        "operationKey",
        "mode",
        "apiKey",
        "baseUrl",
        "scenario",
    ],
)
def test_model_cannot_supply_execution_scope_or_provider_controls(store, video_clock, field):
    registry, _, adapter, context = setup_tools(store, video_clock)
    result = execute(registry, store, context, "video_generate", {**video_request(adapter), field: "secret"})
    assert result["error"]["code"] == "INVALID_ARGUMENTS" and "secret" not in json.dumps(result)
    assert not store.snapshot()["jobs"] and not store.snapshot()["operations"]


@pytest.mark.parametrize(
    "change,code",
    [
        (
            {"spec": {"durationSeconds": 5, "resolution": "1080p", "aspectRatio": "16:9"}},
            "VIDEO_UNSUPPORTED_SPEC",
        ),
        (
            {"spec": {"durationSeconds": True, "resolution": "720p", "aspectRatio": "16:9"}},
            "INVALID_ARGUMENTS",
        ),
        ({"provider": "unconfigured"}, "VIDEO_MODEL_UNAVAILABLE"),
        ({"capabilitiesVersion": "old"}, "VIDEO_CAPABILITIES_CHANGED"),
        ({"sourceRefs": [{"artifactId": "missing", "version": 1}]}, "SOURCE_NOT_FOUND"),
        ({"sourceRefs": [{"artifactId": "missing"}]}, "INVALID_ARGUMENTS"),
        ({"source_refs": []}, "INVALID_ARGUMENTS"),
    ],
)
def test_invalid_requests_never_reserve_a_job(store, video_clock, change, code):
    registry, _, adapter, context = setup_tools(store, video_clock)
    result = execute(registry, store, context, "video_generate", {**video_request(adapter), **change})
    assert result["error"]["code"] == code
    assert not store.snapshot()["jobs"] and adapter.ledger_snapshot()["submitCalls"] == 0


def test_sources_are_scoped_and_freeze_an_existing_version(store, video_clock):
    registry, service, adapter, context = setup_tools(store, video_clock)
    source = create_project_tools().execute(
        "artifact_save",
        {"kind": "brief", "title": "source", "content": "version one"},
        store=store,
        project_id="coffee",
        operation_key="source",
    )["data"]["artifactId"]
    other = video_run(store, "other-run", project_id="other")
    args = video_request(adapter, sourceRefs=[{"artifactId": source, "version": 1}])
    assert execute(registry, store, other, "video_generate", args)["error"]["code"] == "SOURCE_NOT_FOUND"
    missing_version = {**args, "sourceRefs": [{"artifactId": source, "version": 2}]}
    assert (
        execute(registry, store, next_call(context, "missing-version"), "video_generate", missing_version)[
            "error"
        ]["code"]
        == "SOURCE_VERSION_NOT_FOUND"
    )
    result = execute(registry, store, context, "video_generate", args)
    create_project_tools().execute(
        "artifact_save",
        {
            "kind": "brief",
            "title": "source",
            "content": "version two",
            "artifactId": source,
            "expectedVersion": 1,
        },
        store=store,
        project_id="coffee",
        operation_key="source-update",
    )
    assert service.get(result["data"]["jobId"], project_id="coffee").request.source_refs[0].version == 1
    assert store.snapshot()["artifacts"][source]["versions"][0]["content"] == "version one"


@pytest.mark.parametrize(
    "change", [{"context": None}, {"project_id": "other"}, {"operation_key": "forged-key"}]
)
def test_context_is_required_and_matches_the_executor_before_any_replay(store, video_clock, change):
    registry, _, adapter, context = setup_tools(store, video_clock)
    args = video_request(adapter)
    assert execute(registry, store, context, "video_generate", args)["ok"]
    before = store.snapshot()
    result = execute(registry, store, context, "video_generate", args, **change)
    assert result["error"]["code"] == "TOOL_CONTEXT_INVALID"
    assert store.snapshot() == before


def test_forged_cross_session_context_and_different_store_are_rejected(store, video_clock, tmp_path):
    registry, _, adapter, context = setup_tools(store, video_clock)
    video_run(store, "other-run", project_id="other")
    forged = next_call(context, context.tool_call_id, projectId="other", sessionId="other")
    args = video_request(adapter)
    assert execute(registry, store, forged, "video_generate", args)["error"]["code"] == "TOOL_CONTEXT_INVALID"
    with FileStore.open(tmp_path / "other-store") as other_store:
        other_context = video_run(other_store)
        result = execute(registry, other_store, other_context, "video_generate", args)
        assert result["error"]["code"] == "TOOL_CONTEXT_INVALID"
        assert not other_store.snapshot()["operations"]
    assert not store.snapshot()["jobs"]


async def test_reads_are_scoped_bounded_and_replay_original_snapshot(store, video_clock):
    registry, service, adapter, context = setup_tools(store, video_clock)
    job_id = execute(registry, store, context, "video_generate", video_request(adapter))["data"]["jobId"]
    lookup = next_call(context, "read")
    initial = execute(registry, store, lookup, "job_get", {"jobId": job_id})
    assert initial["data"]["status"] == "pending_submit"
    assert not {"operationKey", "context", "providerTaskId", "capabilities"} & initial["data"].keys()
    await JobWorker(service).run_once()
    ledger = adapter.ledger_snapshot()
    assert execute(registry, store, lookup, "job_get", {"jobId": job_id}) == initial
    current = execute(registry, store, next_call(context, "fresh-read"), "job_get", {"jobId": job_id})
    assert current["data"]["status"] == "queued" and current["data"]["revision"] > initial["data"]["revision"]
    other = video_run(store, "other-run", project_id="other")
    for name in ("job_get", "await_job"):
        not_found = execute(registry, store, next_call(other, name), name, {"jobId": job_id})
        missing = execute(registry, store, next_call(other, name + "-missing"), name, {"jobId": "missing"})
        assert not_found == missing and not_found["error"]["code"] == "JOB_NOT_FOUND"
    assert adapter.ledger_snapshot() == ledger and not store.snapshot()["waits"]


async def test_read_only_hides_generation_and_allows_wait_bookkeeping(store, video_clock):
    registry, _, adapter, context = setup_tools(store, video_clock)
    args = video_request(adapter)
    job_id = execute(registry, store, context, "video_generate", args)["data"]["jobId"]
    readonly = registry.read_only()
    reader = video_run(store, "reader", read_only=True)
    before = store.snapshot()
    assert "video_generate" not in {item["function"]["name"] for item in readonly.specs()}
    assert {"video_capabilities", "job_get", "await_job"} <= {
        item["function"]["name"] for item in readonly.specs()
    }
    assert execute(readonly, store, reader, "video_generate", args)["error"]["code"] == "UNKNOWN_TOOL"
    assert execute(registry, store, reader, "video_generate", args)["error"]["code"] == "READ_ONLY"
    assert execute(readonly, store, next_call(reader, "caps"), "video_capabilities", {})["ok"]
    assert execute(readonly, store, next_call(reader, "get"), "job_get", {"jobId": job_id})["ok"]
    waiting = execute(readonly, store, next_call(reader, "wait"), "await_job", {"jobId": job_id})
    assert isinstance(waiting, DeferredToolResult)
    after = store.snapshot()
    for field in ("jobs", "projects", "artifacts", "runs"):
        assert before[field] == after[field]
    assert len(after["waits"]) == 1 and readonly.bypass_answer_cache
    assert readonly.features == registry.features and not readonly.identities


@pytest.mark.parametrize(
    "scenario,code",
    [
        (MockScenario(states=["succeeded"]), None),
        (MockScenario(states=["failed"]), "JOB_FAILED"),
        (MockScenario(submission="rejected"), "JOB_FAILED"),
        (MockScenario(submission="response_lost"), "JOB_SUBMISSION_UNKNOWN"),
        (MockScenario(query_error_calls=[1, 2, 3, 4]), "JOB_QUERY_PAUSED"),
    ],
)
async def test_await_immediate_outcomes_are_stable_without_a_binding(store, video_clock, scenario, code):
    registry, service, adapter, context = setup_tools(store, video_clock, scenario=scenario)
    job_id = execute(registry, store, context, "video_generate", video_request(adapter))["data"]["jobId"]
    worker = JobWorker(service)
    job = await worker.run_once()
    while job.next_poll_at:
        video_clock.due(job)
        job = await worker.run_once()
    waiting = next_call(context, "await")
    outcome = execute(registry, store, waiting, "await_job", {"jobId": job_id})
    assert outcome["ok"] == (code is None)
    if code:
        assert outcome["error"]["code"] == code and job_id in outcome["error"]["message"]
    else:
        assert outcome["data"]["result"]["simulated"] and not outcome["data"]["mediaAvailable"]
    assert not store.snapshot()["waits"]
    assert store.snapshot()["operations"][waiting.operation_key]["result"] == outcome
    if code == "JOB_QUERY_PAUSED":
        service.retry_query(job_id, project_id="coffee")
    assert execute(registry, store, waiting, "await_job", {"jobId": job_id}) == outcome


async def test_pending_wait_replays_same_marker_after_restart_job_and_operation_completion(
    store, video_clock
):
    registry, service, adapter, context = setup_tools(
        store, video_clock, scenario=MockScenario(states=["succeeded"])
    )
    job_id = execute(registry, store, context, "video_generate", video_request(adapter))["data"]["jobId"]
    waiting = next_call(context, "await")
    args = {"jobId": job_id}
    marker = execute(registry, store, waiting, "await_job", args)
    binding = WaitBinding.model_validate(store.snapshot()["waits"][marker.wait_id])
    assert isinstance(marker, DeferredToolResult) and marker.resource.id == job_id
    assert binding.status == "preparing" and binding.context == waiting and binding.generation == 1
    assert (
        datetime.fromisoformat(binding.deadline_at) - datetime.fromisoformat(binding.started_at)
    ).total_seconds() == 600
    assert waiting.operation_key not in store.snapshot()["operations"]
    assert (
        execute(registry, store, waiting, "await_job", {"jobId": "different"})["error"]["code"]
        == "OPERATION_CONFLICT"
    )
    assert (await JobWorker(service).run_once()).status == "succeeded"
    # Simulate B3's final Operation commit before graph replay, while retaining
    # the interrupt position established by the original binding.
    final = store.operation(waiting.operation_key, "await_job", args, lambda _: {"jobId": job_id})
    home = store.home
    store.close()
    video_clock.advance(1000)
    with FileStore.open(home) as reopened:
        service = JobService(reopened, [MockVideoAdapter(reopened, clock=video_clock)], clock=video_clock)
        registry = register_video_tools(create_project_tools(), service)
        assert execute(registry, reopened, waiting, "await_job", args) == marker
        assert reopened.snapshot()["waits"][marker.wait_id] == binding.model_dump(mode="json", by_alias=True)
        assert reopened.snapshot()["operations"][waiting.operation_key]["result"] == final
        assert len(reopened.snapshot()["waits"]) == 1


async def test_pending_wait_key_cannot_be_reused_by_local_or_mcp_tool(store, video_clock):
    registry, _, adapter, context = setup_tools(store, video_clock)
    job_id = execute(registry, store, context, "video_generate", video_request(adapter))["data"]["jobId"]
    waiting = next_call(context, "await")
    execute(registry, store, waiting, "await_job", {"jobId": job_id})
    before = store.snapshot()
    assert (
        execute(registry, store, waiting, "project_update", {"expectedRevision": 0, "style": "changed"})[
            "error"
        ]["code"]
        == "OPERATION_CONFLICT"
    )
    invoked = []

    async def external(args):
        invoked.append(args)
        return "external result"

    registry.register(ToolDefinition("external", "test", {"type": "object"}, "read", async_execute=external))
    result = await registry.aexecute(
        "external", {}, store=store, project_id="coffee", operation_key=waiting.operation_key
    )
    assert result["error"]["code"] == "OPERATION_CONFLICT" and not invoked
    assert store.snapshot() == before


def test_failed_wait_commit_does_not_leave_success_or_partial_binding(store, video_clock, monkeypatch):
    registry, _, adapter, context = setup_tools(store, video_clock)
    job_id = execute(registry, store, context, "video_generate", video_request(adapter))["data"]["jobId"]
    waiting = next_call(context, "await")
    before = copy.deepcopy(store.snapshot())

    def fail_write(*_):
        raise OSError("private-write-error")

    monkeypatch.setattr("vagent.storage.atomic_write_json", fail_write)
    result = execute(registry, store, waiting, "await_job", {"jobId": job_id})
    assert result["ok"] is False and "private-write-error" not in json.dumps(result)
    assert store.snapshot() == before


def test_external_async_writes_remain_forbidden():
    async def external(_):
        return {}

    with pytest.raises(ValueError, match="read-only"):
        ToolRegistry().register(
            ToolDefinition("external", "unsafe", {"type": "object"}, "write", async_execute=external)
        )
