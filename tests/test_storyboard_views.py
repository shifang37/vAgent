"""M2 D3 storyboard projections shared by the CLI, the Web panel and the tools."""

import pytest
from shot_support import (
    generation_args,
    m2_run,
    mock_tools,
    redo_args,
    revise_storyboard,
    save_storyboard,
    shot,
    storyboard_record,
)

from vagent.config import Config
from vagent.errors import AppError
from vagent.video.jobs import JobService
from vagent.video.providers.mock import MockVideoAdapter
from vagent.video.storyboards import Storyboard, storyboard_in_state
from vagent.video.views import (
    current_storyboard_view,
    delivered_media,
    storyboard_summaries,
    storyboard_view,
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


def args(saved, shot_ids=None, *, version=1):
    return {
        "storyboardId": saved["storyboardId"],
        "storyboardVersion": version,
        "shotIds": list(shot_ids if shot_ids is not None else saved["shotIds"]),
        "confirm": False,
    }


def record_of(store, storyboard_id) -> Storyboard:
    return Storyboard.model_validate(store.snapshot()["storyboards"][storyboard_id])


def preview_service(store, clock):
    """A service whose active mode is mock, as the CLI and Web always configure."""
    return JobService(
        store,
        [MockVideoAdapter(store, clock=clock)],
        clock=clock,
        config=Config(home=store.home, api_key=None, video_mode="mock"),
    )


def test_view_orders_shots_and_links_their_registered_jobs(store, video_clock):
    registry, service, _, _ = mock_tools(store, clock=video_clock)
    saved = save_storyboard(registry, store, [shot("镜头一"), shot("镜头二"), shot("镜头三")])
    m2_run(store, "shot-run", mode="mock")
    registered = service.shot_generate(generation_args(saved), context=context("shot-run"))
    assert registered["ok"], registered
    view = storyboard_view(store.snapshot(), record_of(store, saved["storyboardId"]))
    assert view["storyboardId"] == saved["storyboardId"] and view["currentVersion"] == 1
    assert [item["order"] for item in view["shots"]] == [1, 2, 3]
    assert [item["id"] for item in view["shots"]] == saved["shotIds"]
    assert view["shotCount"] == 3 and view["versionCount"] == 1 and view["revision"] == 1
    for item, entry in zip(view["shots"], registered["data"]["shots"], strict=True):
        assert len(item["versions"]) == 1
        latest = item["latest"]
        assert latest["jobId"] == entry["jobId"] and latest["current"] is True
        assert latest["status"] == "pending_submit" and latest["simulated"] is True
        assert latest["mediaAvailable"] is False and latest["mediaUrl"] is None
        # M2 links delivered media through the Job; ShotVersion.mediaId stays null.
        assert latest["mediaId"] is None


def test_a_redo_appends_one_version_and_leaves_the_others_alone(store, video_clock):
    registry, service, _, _ = mock_tools(store, clock=video_clock)
    saved = save_storyboard(registry, store, [shot("镜头一"), shot("镜头二")])
    m2_run(store, "shot-run", mode="mock")
    first = service.shot_generate(generation_args(saved), context=context("shot-run"))
    target = saved["shotIds"][1]
    m2_run(store, "redo-run", mode="mock")
    revision = storyboard_record(store, saved["storyboardId"])["revision"]
    redone = service.redo_shot(redo_args(saved, target, revision=revision), context=context("redo-run"))
    assert redone["ok"], redone
    view = storyboard_view(store.snapshot(), record_of(store, saved["storyboardId"]))
    untouched, changed = view["shots"]
    assert len(untouched["versions"]) == 1
    assert untouched["versions"][0]["jobId"] == first["data"]["shots"][0]["jobId"]
    assert len(changed["versions"]) == 2
    assert [item["jobId"] for item in changed["versions"]] == [
        first["data"]["shots"][1]["jobId"],
        redone["data"]["shots"][0]["jobId"],
    ]
    assert changed["latest"]["jobId"] == redone["data"]["shots"][0]["jobId"]
    # A new storyboard version makes earlier registrations visibly not-current.
    revise_storyboard(
        registry,
        store,
        saved,
        [{"id": saved["shotIds"][0], **shot("镜头一改")}, {"id": target, **shot("镜头二")}],
    )
    view = storyboard_view(store.snapshot(), record_of(store, saved["storyboardId"]))
    assert view["currentVersion"] == 2 and view["versionCount"] == 2
    assert all(item["current"] is False for item in view["shots"][0]["versions"])
    assert all(item["current"] is False for item in view["shots"][1]["versions"])


def test_summaries_and_the_current_view_are_project_scoped(store, video_clock):
    registry, _, _, _ = mock_tools(store, clock=video_clock)
    store.ensure_session("other")
    coffee = save_storyboard(registry, store, [shot("镜头一")])
    other = save_storyboard(
        registry, store, [shot("别人的一"), shot("别人的二")], project_id="other", key="other-save"
    )
    state = store.snapshot()
    summaries = storyboard_summaries(state, "coffee")
    assert [item["storyboardId"] for item in summaries] == [coffee["storyboardId"]]
    assert summaries[0]["shotCount"] == 1 and summaries[0]["projectId"] == "coffee"
    assert summaries[0]["shotVersionCount"] == 0 and summaries[0]["revision"] == 0
    assert storyboard_summaries(state, "missing") == []
    assert current_storyboard_view(state, "coffee")["storyboardId"] == coffee["storyboardId"]
    assert current_storyboard_view(state, "other")["shotCount"] == 2
    assert current_storyboard_view(state, "none") is None
    with pytest.raises(AppError) as caught:
        storyboard_in_state(state, "coffee", other["storyboardId"])
    assert caught.value.code == "NOT_FOUND"


def test_delivered_media_is_read_from_the_job(store, video_clock):
    registry, service, _, _ = mock_tools(store, clock=video_clock)
    saved = save_storyboard(registry, store, [shot("镜头一")])
    m2_run(store, "shot-run", mode="mock")
    registered = service.shot_generate(generation_args(saved), context=context("shot-run"))
    job = service.get(registered["data"]["shots"][0]["jobId"])
    assert delivered_media(job) == (None, False)
    assert delivered_media(None) == (None, False)


def test_preview_reports_estimates_and_reuse_without_registering(store, video_clock):
    registry, _, adapter, _ = mock_tools(store, clock=video_clock)
    service = preview_service(store, video_clock)
    saved = save_storyboard(registry, store, [shot("镜头一"), shot("镜头二")])
    m2_run(store, "shot-run", mode="mock")
    preview = service.preview_shots("coffee", args(saved))
    assert preview["mode"] == "mock" and preview["simulated"] is True
    assert preview["totalEstimate"] is None and preview["limits"] is None
    assert preview["storyboardRevision"] == 0
    assert [row["reused"] for row in preview["shots"]] == [False, False]
    assert all(row["estimate"] is None and row["jobId"] is None for row in preview["shots"])
    assert (
        not store.snapshot()["jobs"] and not storyboard_record(store, saved["storyboardId"])["shotVersions"]
    )
    registered = service.shot_generate(generation_args(saved), context=context("shot-run"))
    after = service.preview_shots("coffee", args(saved, [saved["shotIds"][1]]))
    assert after["shots"][0]["reused"] is True
    assert after["shots"][0]["jobId"] == registered["data"]["shots"][1]["jobId"]
    assert len(store.snapshot()["jobs"]) == 2
    assert adapter.ledger_snapshot()["submitCalls"] == 0
    with pytest.raises(AppError) as caught:
        service.preview_shots("coffee", args(saved, version=2))
    assert caught.value.code == "VERSION_CONFLICT"
    with pytest.raises(AppError) as caught:
        service.preview_shots("coffee", args(saved, ["44444444-4444-4444-4444-444444444444"]))
    assert caught.value.code == "SHOT_NOT_FOUND"


def test_preview_needs_the_active_video_mode(store, video_clock):
    registry, _, _, _ = mock_tools(store, clock=video_clock)
    saved = save_storyboard(registry, store, [shot("镜头一")])
    unconfigured = JobService(store, [MockVideoAdapter(store, clock=video_clock)], clock=video_clock)
    with pytest.raises(AppError) as caught:
        unconfigured.preview_shots("coffee", args(saved))
    assert caught.value.code == "VIDEO_MODEL_UNAVAILABLE"
