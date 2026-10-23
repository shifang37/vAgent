"""M7 E2: tools/rules 16/16, the frozen 15/15 surface and the season batch tool."""

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


def test_tool_versions_are_sixteen_and_fifteen_is_frozen():
    assert (VIDEO_TOOLS_VERSION, VIDEO_RULES_VERSION) == (16, 16)
    assert (LIVE_VIDEO_TOOLS_VERSION, LIVE_VIDEO_RULES_VERSION) == (16, 16)
    assert video_tools.M7_E1_TOOL_NAMES == ("season_save", "season_read")
    assert video_tools.M7_E2_TOOL_NAMES == ("season_generate",)
    assert video_tools.M7_TOOL_NAMES == ("season_save", "season_read", "season_generate")
    assert set(video_tools.PRE_M7_E2_TOOL_NAMES) == (
        set(video_tools.M2_TOOL_NAMES)
        | set(video_tools.M3_TOOL_NAMES)
        | set(video_tools.M5_TOOL_NAMES)
        | set(video_tools.M5_E2_TOOL_NAMES)
        | set(video_tools.M5_E3_TOOL_NAMES)
        | set(video_tools.M6_TOOL_NAMES)
        | set(video_tools.M7_E1_TOOL_NAMES)
        | set(video_tools.M7_E2_TOOL_NAMES)
    )


def test_new_runs_publish_sixteen_and_frozen_fifteen_hides_the_batch_tool(store):
    registry, _service, _adapter, _context = mock_tools(store)
    features = registry.features["video"]
    assert (features["toolsVersion"], features["rulesVersion"]) == (16, 16)
    current = names(registry)
    assert {"season_save", "season_read", "season_generate"} <= current
    assert {"film_render", "subtitle_save", "episode_generate"} <= current
    assert "season_generate" in registry.instructions
    assert "resumeBatchId" in registry.instructions

    # The 15/15 variant reproduces the exact M7 E1 published features, including TTS.
    fifteen_features = {**features, "toolsVersion": 15, "rulesVersion": 15}
    fifteen = registry.for_record_features({"video": fifteen_features})
    assert fifteen is not None
    assert fifteen.features == {"video": fifteen_features}
    assert "season_generate" not in names(fifteen)
    assert "season_generate" not in fifteen.instructions
    assert "season_save" in names(fifteen) and "season_read" in names(fifteen)
    assert "season_save" in fifteen.instructions
    # The 13/13 variant still hides both season generations.
    thirteen_features = {**features, "toolsVersion": 13, "rulesVersion": 13}
    thirteen = registry.for_record_features({"video": thirteen_features})
    assert thirteen is not None
    assert "season_save" not in names(thirteen) and "season_generate" not in names(thirteen)


def test_season_tools_are_declared_with_the_right_effects(store):
    registry, _service, _adapter, _context = mock_tools(store)
    save = registry._definitions["season_save"]
    read = registry._definitions["season_read"]
    generate = registry._definitions["season_generate"]
    assert (save.effect, read.effect, generate.effect) == ("write", "read", "write")
    assert save.execute is not None and save.execute.__name__ == "save_season"
    assert read.execute is not None and read.execute.__name__ == "read_season"
    assert generate.context_execute is not None and generate.execute is None
    assert {save.feature, read.feature, generate.feature} == {"video"}

    spec = next(item for item in registry.specs() if item["function"]["name"] == "season_generate")
    properties = set(spec["function"]["parameters"]["properties"])
    assert {"seasonId", "confirm"} <= properties
    assert {"episodeIds", "resumeBatchId"} <= properties
    read_spec = next(item for item in registry.specs() if item["function"]["name"] == "season_read")
    assert set(read_spec["function"]["parameters"]["properties"]) == {"seasonId", "batchId"}


def test_read_only_view_keeps_season_read_and_hides_both_season_writers(store):
    registry, _service, _adapter, _context = mock_tools(store)
    read_only = registry.read_only()
    assert "season_read" in names(read_only)
    assert "season_save" not in names(read_only)
    assert "season_generate" not in names(read_only)
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
    assert read["data"]["batch"] is None and read["data"]["batches"] == []
    # No Job was registered by either tool.
    assert store.snapshot()["jobs"] == {}
    assert store.snapshot()["batches"] == {}
