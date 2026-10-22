"""M2 D3 client Runs: explicit local redo and batch registration."""

import httpx
import pytest
from conftest import video_run
from shot_support import (
    generation_args,
    m2_run,
    mock_tools,
    save_storyboard,
    shot,
    shot_versions,
    storyboard_record,
)
from wan_support import live_service, provider_response

from vagent.config import Config
from vagent.errors import AppError
from vagent.video import clients
from vagent.video.jobs import JobService
from vagent.video.providers.mock import MockVideoAdapter
from vagent.video.tools import (
    LIVE_VIDEO_RULES_VERSION,
    LIVE_VIDEO_TOOLS_VERSION,
    VIDEO_RULES_VERSION,
    VIDEO_TOOLS_VERSION,
)
from vagent.waiting import ToolExecutionContext


def context(run_id, call_id="call"):
    return ToolExecutionContext(
        project_id="coffee",
        session_id="coffee",
        run_id=run_id,
        model_step=1,
        tool_call_id=call_id,
    )


def client_service(store, clock, *, mode="mock", **changes):
    """A registration-only service with one explicit active video mode."""
    return JobService(
        store,
        [MockVideoAdapter(store, clock=clock)],
        clock=clock,
        config=Config(home=store.home, api_key=None, video_mode=mode, **changes),
    )


def test_a_client_run_is_durable_but_never_the_conversation(store, video_clock):
    store.ensure_session("coffee")
    video_run(store, "chat-run")
    service = client_service(store, video_clock)
    context_one = clients.client_context(
        service, session_id="coffee", client_request_id="redo-request", group="shot_redo"
    )
    state = store.snapshot()
    record = state["runs"][context_one.run_id]
    assert context_one.run_id == clients.client_run_id("coffee", "redo-request")
    assert record["status"] == "completed" and record["modelSteps"] == 0
    assert record["videoMode"] == "mock" and record["requestId"] == "redo-request"
    assert record["model"] == clients.CLIENT_RUN_MODEL and record["resumable"] is False
    assert record["contextVersion"] == 2 and record["executionVersion"] == 3
    features = record["toolFeatures"]["video"]
    assert features["toolsVersion"] == VIDEO_TOOLS_VERSION == 10
    assert features["rulesVersion"] == VIDEO_RULES_VERSION == 10
    assert features["mode"] == "mock" and features["capabilities"] == [
        item.model_dump(mode="json", by_alias=True) for item in service.capabilities("mock")
    ]
    # The conversation stays the session's latest Run, so it stays resumable.
    assert state["sessions"]["coffee"]["latestRunId"] == "chat-run"
    assert (
        clients.client_context(
            service, session_id="coffee", client_request_id="redo-request", group="shot_redo"
        )
        == context_one
    )
    # One request ID owns one Run; a different request gets its own.
    other = clients.client_context(
        service, session_id="coffee", client_request_id="second-request", group="shot_redo"
    )
    assert other.run_id != context_one.run_id
    # A different action under the same request is a different operation key.
    batch = clients.client_context(
        service, session_id="coffee", client_request_id="redo-request", group="shot_generate"
    )
    assert batch.run_id == context_one.run_id and batch.operation_key != context_one.operation_key
    assert len(store.snapshot()["runs"]) == 3


def test_client_actions_need_an_active_mode(store, video_clock):
    store.ensure_session("coffee")
    service = client_service(store, video_clock, mode="off")
    with pytest.raises(AppError) as caught:
        clients.client_context(
            service, session_id="coffee", client_request_id="off-request", group="shot_redo"
        )
    assert caught.value.code == "VIDEO_MODEL_UNAVAILABLE"
    assert not store.snapshot()["runs"]


def test_a_client_request_id_cannot_change_project_or_mode(store, video_clock):
    store.ensure_session("coffee")
    service = client_service(store, video_clock)
    first = clients.client_context(
        service, session_id="coffee", client_request_id="fixed-request", group="shot_redo"
    )
    assert (
        clients.client_context(
            service, session_id="coffee", client_request_id="fixed-request", group="shot_redo"
        )
        == first
    )
    live = client_service(
        store, video_clock, mode="live", video_api_key="live-key", video_workspace_id="live-space"
    )
    with pytest.raises(AppError) as caught:
        clients.client_context(
            live, session_id="coffee", client_request_id="fixed-request", group="shot_redo"
        )
    assert caught.value.code == "OPERATION_CONFLICT"
    with pytest.raises(AppError) as caught:
        clients.redo_shot(
            live,
            session_id="other",
            client_request_id="cross-project",
            storyboard_id="44444444-4444-4444-4444-444444444444",
            storyboard_version=1,
            shot_id="44444444-4444-4444-4444-444444444444",
            expected_revision=0,
        )
    assert caught.value.code == "NOT_FOUND"
    assert clients.client_run_id("other", "cross-project") not in store.snapshot()["runs"]


def test_client_redo_registers_one_version_and_one_job(store, video_clock):
    registry, service, _, _ = mock_tools(store, clock=video_clock)
    local = client_service(store, video_clock)
    saved = save_storyboard(registry, store, [shot("镜头一"), shot("镜头二")])
    m2_run(store, "shot-run", mode="mock")
    assert service.shot_generate(generation_args(saved), context=context("shot-run"))["ok"]
    target = saved["shotIds"][1]
    revision = storyboard_record(store, saved["storyboardId"])["revision"]
    untouched_before = shot_versions(store, saved["storyboardId"], saved["shotIds"][0])
    result = clients.redo_shot(
        local,
        session_id="coffee",
        client_request_id="redo-a",
        storyboard_id=saved["storyboardId"],
        storyboard_version=1,
        shot_id=target,
        expected_revision=revision,
    )
    assert result["mode"] == "mock" and result["storyboardRevision"] == revision + 1
    assert result["runId"] == clients.client_run_id("coffee", "redo-a")
    assert [row["shotId"] for row in result["shots"]] == [target]
    assert result["shots"][0]["reused"] is False
    assert len(shot_versions(store, saved["storyboardId"], target)) == 2
    assert shot_versions(store, saved["storyboardId"], saved["shotIds"][0]) == untouched_before
    assert len(store.snapshot()["jobs"]) == 3
    replay = clients.redo_shot(
        local,
        session_id="coffee",
        client_request_id="redo-a",
        storyboard_id=saved["storyboardId"],
        storyboard_version=1,
        shot_id=target,
        expected_revision=revision,
    )
    assert replay == result and len(store.snapshot()["jobs"]) == 3
    assert len(shot_versions(store, saved["storyboardId"], target)) == 2
    # A genuinely new request is a real second redo of the same shot.
    second = clients.redo_shot(
        local,
        session_id="coffee",
        client_request_id="redo-b",
        storyboard_id=saved["storyboardId"],
        storyboard_version=1,
        shot_id=target,
        expected_revision=result["storyboardRevision"],
    )
    assert second["shots"][0]["jobId"] != result["shots"][0]["jobId"]
    assert len(shot_versions(store, saved["storyboardId"], target)) == 3
    assert len(store.snapshot()["jobs"]) == 4


def test_stale_revision_and_unknown_shot_leave_no_registration(store, video_clock):
    registry, service, _, _ = mock_tools(store, clock=video_clock)
    local = client_service(store, video_clock)
    saved = save_storyboard(registry, store, [shot("镜头一")])
    m2_run(store, "shot-run", mode="mock")
    assert service.shot_generate(generation_args(saved), context=context("shot-run"))["ok"]
    revision = storyboard_record(store, saved["storyboardId"])["revision"]
    assert revision == 1
    with pytest.raises(AppError) as caught:
        clients.redo_shot(
            local,
            session_id="coffee",
            client_request_id="stale",
            storyboard_id=saved["storyboardId"],
            storyboard_version=1,
            shot_id=saved["shotIds"][0],
            expected_revision=0,
        )
    assert caught.value.code == "VERSION_CONFLICT"
    with pytest.raises(AppError) as caught:
        clients.redo_shot(
            local,
            session_id="coffee",
            client_request_id="unknown-shot",
            storyboard_id=saved["storyboardId"],
            storyboard_version=1,
            shot_id="44444444-4444-4444-4444-444444444444",
            expected_revision=revision,
        )
    assert caught.value.code == "SHOT_NOT_FOUND"
    assert len(store.snapshot()["jobs"]) == 1
    assert len(shot_versions(store, saved["storyboardId"], saved["shotIds"][0])) == 1
    # A doomed action never even creates its Run.
    runs = store.snapshot()["runs"]
    assert clients.client_run_id("coffee", "stale") not in runs
    assert clients.client_run_id("coffee", "unknown-shot") not in runs


def test_client_batch_registers_only_the_confirmed_shots(store, video_clock):
    registry, _, _, _ = mock_tools(store, clock=video_clock)
    local = client_service(store, video_clock)
    saved = save_storyboard(registry, store, [shot("镜头一"), shot("镜头二"), shot("镜头三")])
    result = clients.generate_shots(
        local,
        session_id="coffee",
        client_request_id="batch-a",
        storyboard_id=saved["storyboardId"],
        storyboard_version=1,
        shot_ids=saved["shotIds"][:2],
    )
    assert [row["shotId"] for row in result["shots"]] == saved["shotIds"][:2]
    assert len(store.snapshot()["jobs"]) == 2
    assert shot_versions(store, saved["storyboardId"], saved["shotIds"][2]) == []
    assert result["storyboardRevision"] == 1
    replayed = clients.generate_shots(
        local,
        session_id="coffee",
        client_request_id="batch-a",
        storyboard_id=saved["storyboardId"],
        storyboard_version=1,
        shot_ids=saved["shotIds"][:2],
    )
    assert replayed == result and len(store.snapshot()["jobs"]) == 2


async def test_every_client_request_gets_its_own_run_quota(store, video_clock):
    def handler(request):
        return httpx.Response(200, json=provider_response("PENDING", task_id="client-task"))

    async with live_service(store, video_clock, handler) as (service, _):
        registry, _, _, _ = mock_tools(store, clock=video_clock)
        saved = save_storyboard(
            registry, store, [shot(f"镜头{index}") for index in range(5)], key="five", title="五镜头"
        )
        first = clients.generate_shots(
            service,
            session_id="coffee",
            client_request_id="live-batch-1",
            storyboard_id=saved["storyboardId"],
            storyboard_version=1,
            shot_ids=saved["shotIds"][:4],
        )
        assert first["limits"]["usedNewJobs"] == 4 and first["limits"]["usedAmount"] == "12.00"
        assert len(store.snapshot()["jobs"]) == 4
        # One request owns one registration: the same ID cannot change its shot set.
        with pytest.raises(AppError) as caught:
            clients.generate_shots(
                service,
                session_id="coffee",
                client_request_id="live-batch-1",
                storyboard_id=saved["storyboardId"],
                storyboard_version=1,
                shot_ids=[saved["shotIds"][4]],
            )
        assert caught.value.code == "OPERATION_CONFLICT"
        assert len(store.snapshot()["jobs"]) == 4
        # A new explicit user request is a new Run with its own quota.
        second = clients.generate_shots(
            service,
            session_id="coffee",
            client_request_id="live-batch-2",
            storyboard_id=saved["storyboardId"],
            storyboard_version=1,
            shot_ids=[saved["shotIds"][4]],
        )
        assert second["runId"] != first["runId"]
        assert second["limits"]["usedNewJobs"] == 1
        assert len(store.snapshot()["jobs"]) == 5
        over = save_storyboard(
            registry, store, [shot("六秒镜头", durationSeconds=6)], key="six", title="六秒"
        )
        with pytest.raises(AppError) as caught:
            clients.generate_shots(
                service,
                session_id="coffee",
                client_request_id="live-batch-3",
                storyboard_id=over["storyboardId"],
                storyboard_version=1,
                shot_ids=over["shotIds"],
            )
        assert caught.value.code == "VIDEO_COST_LIMIT"
        assert len(store.snapshot()["jobs"]) == 5
        # The rejected call is journaled as a failure, so a retry replays it.
        rejected = clients.client_run_id("coffee", "live-batch-3") + ":1:shot_generate"
        assert store.snapshot()["operations"][rejected]["result"]["ok"] is False


async def test_live_client_runs_record_the_live_tool_features(store, video_clock):
    def handler(request):
        return httpx.Response(200, json=provider_response("PENDING", task_id="features-task"))

    async with live_service(store, video_clock, handler) as (service, _):
        registry, _, _, _ = mock_tools(store, clock=video_clock)
        saved = save_storyboard(registry, store, [shot("镜头一")])
        clients.generate_shots(
            service,
            session_id="coffee",
            client_request_id="live-features",
            storyboard_id=saved["storyboardId"],
            storyboard_version=1,
            shot_ids=saved["shotIds"],
        )
        record = store.snapshot()["runs"][clients.client_run_id("coffee", "live-features")]
    features = record["toolFeatures"]["video"]
    assert record["videoMode"] == "live"
    assert features["toolsVersion"] == LIVE_VIDEO_TOOLS_VERSION and features["mode"] == "live"
    assert features["rulesVersion"] == LIVE_VIDEO_RULES_VERSION
    assert len(features["capabilities"]) == 2
