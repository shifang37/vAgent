"""M7 E3: tools/rules 18/18, the frozen 16/16 surface and the timeline tools."""

from shot_support import mock_tools

from vagent.video import tools as video_tools
from vagent.video.tools import (
    LIVE_VIDEO_RULES_VERSION,
    LIVE_VIDEO_TOOLS_VERSION,
    VIDEO_RULES_VERSION,
    VIDEO_TOOLS_VERSION,
)


def names(registry):
    return {spec["function"]["name"] for spec in registry.specs()}


def test_tool_versions_are_eighteen_and_sixteen_is_frozen():
    assert (VIDEO_TOOLS_VERSION, VIDEO_RULES_VERSION) == (18, 18)
    assert (LIVE_VIDEO_TOOLS_VERSION, LIVE_VIDEO_RULES_VERSION) == (18, 18)
    assert video_tools.M7_E1_TOOL_NAMES == ("season_save", "season_read")
    assert video_tools.M7_E2_TOOL_NAMES == ("season_generate",)
    assert video_tools.M7_E3_TOOL_NAMES == ("timeline_save", "timeline_read")
    assert video_tools.M7_TOOL_NAMES == (
        "season_save",
        "season_read",
        "season_generate",
        "timeline_save",
        "timeline_read",
    )
    assert set(video_tools.PRE_M7_E3_TOOL_NAMES) == (
        set(video_tools.M2_TOOL_NAMES)
        | set(video_tools.M3_TOOL_NAMES)
        | set(video_tools.M5_TOOL_NAMES)
        | set(video_tools.M5_E2_TOOL_NAMES)
        | set(video_tools.M5_E3_TOOL_NAMES)
        | set(video_tools.M6_TOOL_NAMES)
        | set(video_tools.M7_E1_TOOL_NAMES)
        | set(video_tools.M7_E2_TOOL_NAMES)
        | set(video_tools.M7_E3_TOOL_NAMES)
    )


def test_new_runs_publish_eighteen_and_frozen_sixteen_hides_the_timeline_tools(store):
    registry, _service, _adapter, _context = mock_tools(store)
    features = registry.features["video"]
    assert (features["toolsVersion"], features["rulesVersion"]) == (18, 18)
    current = names(registry)
    assert {"timeline_save", "timeline_read", "season_generate", "film_render"} <= current
    assert "timeline-zh-v1" in registry.instructions
    assert "timeline-subtitle-remap-v1" in registry.instructions
    assert "scope=timeline" in registry.instructions

    # The 16/16 variant reproduces the exact M7 E2 published features, including TTS.
    sixteen_features = {**features, "toolsVersion": 16, "rulesVersion": 16}
    sixteen = registry.for_record_features({"video": sixteen_features})
    assert sixteen is not None
    assert sixteen.features == {"video": sixteen_features}
    assert "timeline_save" not in names(sixteen) and "timeline_read" not in names(sixteen)
    assert "timeline-zh-v1" not in sixteen.instructions
    assert "season_generate" in names(sixteen)
    assert "season_generate" in sixteen.instructions

    # The 15/15 variant still hides the season batch, and 13/13 hides both seasons.
    fifteen_features = {**features, "toolsVersion": 15, "rulesVersion": 15}
    fifteen = registry.for_record_features({"video": fifteen_features})
    assert fifteen is not None
    assert "season_generate" not in names(fifteen) and "timeline_save" not in names(fifteen)
    thirteen_features = {**features, "toolsVersion": 13, "rulesVersion": 13}
    thirteen = registry.for_record_features({"video": thirteen_features})
    assert thirteen is not None
    assert "season_save" not in names(thirteen) and "timeline_save" not in names(thirteen)


def test_timeline_tools_are_declared_with_the_right_effects(store):
    registry, _service, _adapter, _context = mock_tools(store)
    save = registry._definitions["timeline_save"]
    read = registry._definitions["timeline_read"]
    assert (save.effect, read.effect) == ("write", "read")
    assert save.execute is not None and save.execute.__name__ == "save_timeline"
    assert read.execute is not None and read.execute.__name__ == "read_timeline"
    assert {save.feature, read.feature} == {"video"}

    save_spec = next(item for item in registry.specs() if item["function"]["name"] == "timeline_save")
    properties = set(save_spec["function"]["parameters"]["properties"])
    assert {
        "episodeId",
        "clips",
        "transitions",
        "subtitle",
        "audio",
        "titleCards",
        "expectedRevision",
    } <= properties
    read_spec = next(item for item in registry.specs() if item["function"]["name"] == "timeline_read")
    assert {"episodeId", "timelineId", "version"} <= set(read_spec["function"]["parameters"]["properties"])


def test_read_only_view_keeps_timeline_read_and_hides_timeline_save(store):
    registry, _service, _adapter, _context = mock_tools(store)
    read_only = registry.read_only()
    assert "timeline_read" in names(read_only)
    assert "timeline_save" not in names(read_only)
    assert "season_read" in names(read_only)
    assert {"episode_read", "storyboard_read", "film_read"} <= names(read_only)
