"""M7 E1: tools/rules 15/15, the frozen 13/13 surface and the season tools."""

from shot_support import mock_tools
from subtitle_support import episode_args, plan

from vagent.video import tools as video_tools
from vagent.video.tools import (
    LIVE_VIDEO_RULES_VERSION,
    LIVE_VIDEO_TOOLS_VERSION,
    VIDEO_RULES_VERSION,
    VIDEO_TOOLS_VERSION,
)


def names(registry):
    return {spec["function"]["name"] for spec in registry.specs()}


def test_tool_versions_are_fifteen_and_thirteen_is_frozen():
    assert (VIDEO_TOOLS_VERSION, VIDEO_RULES_VERSION) == (15, 15)
    assert (LIVE_VIDEO_TOOLS_VERSION, LIVE_VIDEO_RULES_VERSION) == (15, 15)
    assert video_tools.M7_E1_TOOL_NAMES == ("season_save", "season_read")
    assert video_tools.M7_TOOL_NAMES == ("season_save", "season_read")
    assert set(video_tools.PRE_M7_E1_TOOL_NAMES) == (
        set(video_tools.M2_TOOL_NAMES)
        | set(video_tools.M3_TOOL_NAMES)
        | set(video_tools.M5_TOOL_NAMES)
        | set(video_tools.M5_E2_TOOL_NAMES)
        | set(video_tools.M5_E3_TOOL_NAMES)
        | set(video_tools.M6_TOOL_NAMES)
        | set(video_tools.M7_E1_TOOL_NAMES)
    )


def test_new_runs_publish_fifteen_and_frozen_thirteen_hides_the_season_tools(store):
    registry, _service, _adapter, _context = mock_tools(store)
    features = registry.features["video"]
    assert (features["toolsVersion"], features["rulesVersion"]) == (15, 15)
    current = names(registry)
    assert {"season_save", "season_read"} <= current
    assert {"film_render", "subtitle_save", "episode_generate"} <= current
    assert "season_save" in registry.instructions
    assert "season_read" in registry.instructions
    assert "13/13" not in registry.instructions

    # The 13/13 variant reproduces the exact M6 E3 published features, including TTS.
    thirteen_features = {**features, "toolsVersion": 13, "rulesVersion": 13}
    thirteen = registry.for_record_features({"video": thirteen_features})
    assert thirteen is not None
    assert thirteen.features == {"video": thirteen_features}
    assert "season_save" not in names(thirteen)
    assert "season_read" not in names(thirteen)
    assert "season_save" not in thirteen.instructions
    assert "film_render" in names(thirteen)
    assert "film_render" in thirteen.instructions
    assert "字幕用 subtitle_save" in thirteen.instructions


def test_season_tools_are_declared_with_the_right_effects(store):
    registry, _service, _adapter, _context = mock_tools(store)
    save = registry._definitions["season_save"]
    read = registry._definitions["season_read"]
    assert (save.effect, read.effect) == ("write", "read")
    assert save.execute is not None and save.execute.__name__ == "save_season"
    assert read.execute is not None and read.execute.__name__ == "read_season"
    assert {save.feature, read.feature} == {"video"}

    spec = next(item for item in registry.specs() if item["function"]["name"] == "season_save")
    assert {"title", "episodeIds"} <= set(spec["function"]["parameters"]["properties"])
    assert {"seasonId", "expectedRevision"} <= set(spec["function"]["parameters"]["properties"])
    read_spec = next(item for item in registry.specs() if item["function"]["name"] == "season_read")
    assert set(read_spec["function"]["parameters"]["properties"]) == {"seasonId"}


def test_read_only_view_keeps_season_read_and_hides_season_save(store):
    registry, _service, _adapter, _context = mock_tools(store)
    read_only = registry.read_only()
    assert "season_read" in names(read_only)
    assert "season_save" not in names(read_only)
    assert {"episode_read", "storyboard_read", "film_read"} <= names(read_only)


def test_season_save_and_read_round_trip_through_the_registry(store):
    registry, _service, _adapter, _context = mock_tools(store)
    args = episode_args(plan())
    args["title"] = "第一集"
    saved_episode = registry.execute(
        "episode_save", args, store=store, project_id="coffee", operation_key="m7-tools-episode"
    )
    assert saved_episode["ok"], saved_episode
    episode_id = saved_episode["data"]["episodeId"]

    saved = registry.execute(
        "season_save",
        {"title": "雨夜第一季", "episodeIds": [episode_id]},
        store=store,
        project_id="coffee",
        operation_key="m7-tools-season-save",
    )
    assert saved["ok"], saved
    assert saved["data"]["version"] == 1 and saved["data"]["replayed"] is False

    read = registry.execute(
        "season_read",
        {"seasonId": saved["data"]["seasonId"]},
        store=store,
        project_id="coffee",
        operation_key="m7-tools-season-read",
    )
    assert read["ok"], read
    assert read["data"]["episodes"][0]["episodeId"] == episode_id
    assert read["data"]["season"]["title"] == "雨夜第一季"
    assert read["data"]["batch"] is None
    # No Job was registered by either tool.
    assert store.snapshot()["jobs"] == {}
