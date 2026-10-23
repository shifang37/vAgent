"""M7 E1: the season domain, its ordering rules and its zero-Job guarantee."""

import copy

import pytest
from shot_support import mock_tools
from subtitle_support import episode_args, plan

from vagent.errors import AppError
from vagent.video.seasons import (
    SEASON_PER_PROJECT_LIMIT,
    SEASON_TITLE_MAX_CHARS,
    Season,
    season_summaries,
)


def seed_episode(registry, store, title, key):
    args = episode_args(plan())
    args["title"] = title
    result = registry.execute("episode_save", args, store=store, project_id="coffee", operation_key=key)
    assert result["ok"], result
    return result["data"]["episodeId"]


def save(registry, store, args, key):
    return registry.execute("season_save", args, store=store, project_id="coffee", operation_key=key)


def read(registry, store, args, key):
    return registry.execute("season_read", args, store=store, project_id="coffee", operation_key=key)


def test_season_versions_are_ordered_and_append_only(store):
    registry, _service, _adapter, _context = mock_tools(store)
    first = seed_episode(registry, store, "第一集", "m7-episode-1")
    second = seed_episode(registry, store, "第二集", "m7-episode-2")
    third = seed_episode(registry, store, "第三集", "m7-episode-3")

    created = save(registry, store, {"title": "雨夜第一季", "episodeIds": [second, first]}, "m7-season-1")
    assert created["ok"], created
    season_id = created["data"]["seasonId"]
    assert (created["data"]["version"], created["data"]["revision"]) == (1, 0)
    assert created["data"]["episodeIds"] == [second, first]

    stored_v1 = copy.deepcopy(store.snapshot()["seasons"][season_id])
    assert [item["episodeIds"] for item in stored_v1["versions"]] == [[second, first]]

    updated = save(
        registry,
        store,
        {
            "seasonId": season_id,
            "title": "雨夜第一季",
            "episodeIds": [first, second, third],
            "expectedRevision": 0,
        },
        "m7-season-2",
    )
    assert updated["ok"], updated
    assert (updated["data"]["version"], updated["data"]["revision"]) == (2, 1)
    # The old version is preserved byte for byte; only currentVersion/revision move.
    stored = store.snapshot()["seasons"][season_id]
    assert stored["versions"][0] == stored_v1["versions"][0]
    assert stored["currentVersion"] == 2 and len(stored["versions"]) == 2

    view = read(registry, store, {"seasonId": season_id}, "m7-season-read-1")
    assert view["ok"], view
    data = view["data"]
    assert data["season"]["episodeIds"] == [first, second, third]
    assert [item["episodeId"] for item in data["episodes"]] == [first, second, third]
    assert [item["title"] for item in data["episodes"]] == ["第一集", "第二集", "第三集"]
    assert data["batch"] is None
    assert len(data["seasons"]) == 1
    # Saving a season never registers a Job, a provider request or a model call.
    assert store.snapshot()["jobs"] == {}


def test_identical_content_replays_instead_of_appending(store):
    registry, _service, _adapter, _context = mock_tools(store)
    first = seed_episode(registry, store, "第一集", "m7-replay-episode-1")
    second = seed_episode(registry, store, "第二集", "m7-replay-episode-2")
    created = save(registry, store, {"title": "第一季", "episodeIds": [first, second]}, "m7-replay-1")
    assert created["ok"], created
    season_id = created["data"]["seasonId"]

    # Without seasonId the identical title/order replays the same season.
    replayed = save(registry, store, {"title": "第一季", "episodeIds": [first, second]}, "m7-replay-2")
    assert replayed["ok"], replayed
    assert replayed["data"]["replayed"] is True
    assert replayed["data"]["seasonId"] == season_id
    assert replayed["data"]["version"] == 1

    # With seasonId the same content also replays, even from a different request.
    again = save(
        registry,
        store,
        {"seasonId": season_id, "title": "第一季", "episodeIds": [first, second], "expectedRevision": 0},
        "m7-replay-3",
    )
    assert again["ok"], again
    assert again["data"]["replayed"] is True
    state = store.snapshot()
    assert list(state["seasons"]) == [season_id]
    assert len(state["seasons"][season_id]["versions"]) == 1


def test_stale_revision_and_scope_errors(store):
    registry, _service, _adapter, _context = mock_tools(store)
    first = seed_episode(registry, store, "第一集", "m7-error-episode-1")
    second = seed_episode(registry, store, "第二集", "m7-error-episode-2")
    created = save(registry, store, {"title": "第一季", "episodeIds": [first]}, "m7-error-1")
    season_id = created["data"]["seasonId"]

    stale = save(
        registry,
        store,
        {"seasonId": season_id, "title": "第一季", "episodeIds": [first, second], "expectedRevision": 7},
        "m7-error-2",
    )
    assert stale["ok"] is False and stale["error"]["code"] == "SEASON_VERSION_CONFLICT"

    missing = save(
        registry,
        store,
        {"title": "第二季", "episodeIds": ["ffffffff-ffff-4fff-8fff-ffffffffffff"]},
        "m7-error-3",
    )
    assert missing["ok"] is False and missing["error"]["code"] == "SEASON_EPISODE_CONFLICT"

    unknown = save(
        registry,
        store,
        {
            "seasonId": "ffffffff-ffff-4fff-8fff-ffffffffffff",
            "title": "第二季",
            "episodeIds": [second],
            "expectedRevision": 0,
        },
        "m7-error-4",
    )
    assert unknown["ok"] is False and unknown["error"]["code"] == "SEASON_NOT_FOUND"

    read_missing = read(registry, store, {"seasonId": "ffffffff-ffff-4fff-8fff-ffffffffffff"}, "m7-error-5")
    assert read_missing["ok"] is False and read_missing["error"]["code"] == "SEASON_NOT_FOUND"


def test_an_episode_belongs_to_one_current_season(store):
    registry, _service, _adapter, _context = mock_tools(store)
    first = seed_episode(registry, store, "第一集", "m7-own-episode-1")
    second = seed_episode(registry, store, "第二集", "m7-own-episode-2")
    created = save(registry, store, {"title": "第一季", "episodeIds": [first]}, "m7-own-1")
    season_id = created["data"]["seasonId"]

    clash = save(registry, store, {"title": "第二季", "episodeIds": [first]}, "m7-own-2")
    assert clash["ok"] is False and clash["error"]["code"] == "SEASON_EPISODE_CONFLICT"

    # Moving requires removing it from the original season's new version first.
    moved_out = save(
        registry,
        store,
        {"seasonId": season_id, "title": "第一季", "episodeIds": [second], "expectedRevision": 0},
        "m7-own-3",
    )
    assert moved_out["ok"], moved_out
    moved_in = save(registry, store, {"title": "第二季", "episodeIds": [first]}, "m7-own-4")
    assert moved_in["ok"], moved_in
    # The old membership is still readable in the frozen season history.
    old = Season.model_validate(store.snapshot()["seasons"][season_id])
    assert old.versions[0].episode_ids == (first,)

    duplicate = save(registry, store, {"title": "第三季", "episodeIds": [second, second]}, "m7-own-5")
    assert duplicate["ok"] is False and duplicate["error"]["code"] == "SEASON_EPISODE_CONFLICT"


def test_title_and_member_limits(store):
    registry, _service, _adapter, _context = mock_tools(store)
    first = seed_episode(registry, store, "第一集", "m7-limit-episode-1")
    second = seed_episode(registry, store, "第二集", "m7-limit-episode-2")

    empty = save(registry, store, {"title": "第一季", "episodeIds": []}, "m7-limit-1")
    assert empty["ok"] is False and empty["error"]["code"] in {"SEASON_LIMIT", "INVALID_ARGUMENTS"}

    too_many = save(
        registry,
        store,
        {"title": "第一季", "episodeIds": [first, second, first, second, first]},
        "m7-limit-2",
    )
    assert too_many["ok"] is False
    assert too_many["error"]["code"] in {"SEASON_LIMIT", "INVALID_ARGUMENTS"}

    blank = save(
        registry,
        store,
        {"title": " " * (SEASON_TITLE_MAX_CHARS + 1), "episodeIds": [first]},
        "m7-limit-3",
    )
    assert blank["ok"] is False and blank["error"]["code"] in {"SEASON_LIMIT", "INVALID_ARGUMENTS"}
    assert store.snapshot()["seasons"] == {}


def test_one_project_holds_at_most_eight_seasons(store):
    registry, _service, _adapter, _context = mock_tools(store)
    episodes = [
        seed_episode(registry, store, f"第{index}集", f"m7-eight-episode-{index}")
        for index in range(1, SEASON_PER_PROJECT_LIMIT + 2)
    ]
    for index in range(SEASON_PER_PROJECT_LIMIT):
        created = save(
            registry,
            store,
            {"title": f"第{index + 1}季", "episodeIds": [episodes[index]]},
            f"m7-eight-season-{index}",
        )
        assert created["ok"], created
    rejected = save(
        registry,
        store,
        {"title": "第九季", "episodeIds": [episodes[-1]]},
        "m7-eight-season-extra",
    )
    assert rejected["ok"] is False and rejected["error"]["code"] == "SEASON_LIMIT"


def test_season_read_without_saved_seasons_is_empty(store):
    registry, _service, _adapter, _context = mock_tools(store)
    result = read(registry, store, {}, "m7-empty-read")
    assert result["ok"], result
    assert result["data"] == {"seasonId": None, "season": None, "episodes": [], "seasons": [], "batch": None}
    assert season_summaries(store.snapshot(), "coffee") == []


def test_store_rejects_rewriting_frozen_season_history(store):
    registry, _service, _adapter, _context = mock_tools(store)
    first = seed_episode(registry, store, "第一集", "m7-immutable-episode-1")
    second = seed_episode(registry, store, "第二集", "m7-immutable-episode-2")
    created = save(registry, store, {"title": "第一季", "episodeIds": [first]}, "m7-immutable-1")
    season_id = created["data"]["seasonId"]
    save(
        registry,
        store,
        {"seasonId": season_id, "title": "第一季", "episodeIds": [first, second], "expectedRevision": 0},
        "m7-immutable-2",
    )
    frozen = copy.deepcopy(store.snapshot()["seasons"][season_id])

    def rewrite_members(draft):
        draft["seasons"][season_id]["versions"][0]["episodeIds"] = [second]

    with pytest.raises(AppError) as error:
        store.transaction(rewrite_members)
    assert error.value.code == "SEASON_VERSION_CONFLICT"
    assert store.snapshot()["seasons"][season_id] == frozen

    def delete_season(draft):
        draft["seasons"].pop(season_id)

    with pytest.raises(AppError) as error:
        store.transaction(delete_season)
    assert error.value.code == "SEASON_VERSION_CONFLICT"


def test_episode_can_be_read_through_a_season_and_again_after_history(store):
    registry, _service, _adapter, _context = mock_tools(store)
    first = seed_episode(registry, store, "第一集", "m7-latest-episode-1")
    second = seed_episode(registry, store, "第二集", "m7-latest-episode-2")
    older = save(registry, store, {"title": "更早的季", "episodeIds": [first]}, "m7-latest-1")
    newer = save(registry, store, {"title": "最新的季", "episodeIds": [second]}, "m7-latest-2")
    assert older["ok"] and newer["ok"]
    latest = read(registry, store, {}, "m7-latest-read")
    assert latest["ok"], latest
    assert latest["data"]["seasonId"] == newer["data"]["seasonId"]
    assert latest["data"]["seasons"][-1]["title"] == "最新的季"
