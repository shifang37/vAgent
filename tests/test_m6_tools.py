"""M6 E3 render tools; M7 E2 raised the published surface to 16/16 and froze 15/15."""

from shot_support import mock_tools, shot
from subtitle_support import plan, save_episode

from vagent.video import tools as video_tools
from vagent.video.tools import (
    LIVE_VIDEO_RULES_VERSION,
    LIVE_VIDEO_TOOLS_VERSION,
    VIDEO_RULES_VERSION,
    VIDEO_TOOLS_VERSION,
)


def names(registry):
    return {spec["function"]["name"] for spec in registry.specs()}


def test_tool_versions_are_thirteen_and_eleven_is_frozen():
    assert (VIDEO_TOOLS_VERSION, VIDEO_RULES_VERSION) == (18, 18)
    assert (LIVE_VIDEO_TOOLS_VERSION, LIVE_VIDEO_RULES_VERSION) == (18, 18)
    assert video_tools.M6_TOOL_NAMES == ("subtitle_save", "audio_generate", "film_render", "render_read")
    assert video_tools.M6_E1_TOOL_NAMES == ("subtitle_save",)
    assert video_tools.M6_E2_TOOL_NAMES == ("audio_generate",)
    assert video_tools.M6_E3_TOOL_NAMES == ("film_render", "render_read")


def test_new_runs_publish_thirteen_and_the_frozen_eleven_hides_the_render_tools(store):
    registry, _service, _adapter, _context = mock_tools(store)
    features = registry.features["video"]
    assert (features["toolsVersion"], features["rulesVersion"]) == (18, 18)
    assert features["tts"][0]["provider"] == "local"
    current = names(registry)
    assert {
        "subtitle_save",
        "audio_generate",
        "film_render",
        "render_read",
        "film_assemble",
        "episode_generate",
    } <= current
    assert "subtitle-align-zh-v1" in registry.instructions
    assert "audio_generate" in registry.instructions
    assert "tts-providers-v1" in registry.instructions
    assert "film_render" in registry.instructions

    eleven_features = {key: value for key, value in features.items() if key != "tts"} | {
        "toolsVersion": 11,
        "rulesVersion": 11,
    }
    eleven = registry.for_record_features({"video": eleven_features})
    assert eleven is not None
    assert eleven.features == {"video": eleven_features}
    assert "audio_generate" in names(eleven)
    assert "film_render" not in names(eleven)
    assert "render_read" not in names(eleven)
    assert "film_render" not in eleven.instructions

    ten_features = {key: value for key, value in eleven_features.items()} | {
        "toolsVersion": 10,
        "rulesVersion": 10,
    }
    ten = registry.for_record_features({"video": ten_features})
    assert ten is not None
    assert ten.features == {"video": ten_features}
    assert "subtitle_save" in names(ten)
    assert "audio_generate" not in names(ten)
    assert "audio_generate" not in ten.instructions
    assert "subtitle-align-zh-v1" in ten.instructions

    nine_features = {key: value for key, value in ten_features.items()} | {
        "toolsVersion": 9,
        "rulesVersion": 9,
    }
    nine = registry.for_record_features({"video": nine_features})
    assert nine is not None
    assert nine.features == {"video": nine_features}
    assert "subtitle_save" not in names(nine)
    assert "audio_generate" not in names(nine)
    assert "subtitle_save" not in nine.instructions
    assert {"film_assemble", "episode_generate", "shot_redo"} <= names(nine)
    assert "subtitle-align-zh-v1" not in nine.instructions

    eight_features = {key: value for key, value in nine_features.items()} | {
        "toolsVersion": 8,
        "rulesVersion": 8,
    }
    eight = registry.for_record_features({"video": eight_features})
    assert eight is not None and "subtitle_save" not in names(eight)
    assert "audio_generate" not in names(eight)


def test_read_only_view_has_no_post_production_writers(store):
    registry, _service, _adapter, _context = mock_tools(store)
    read_only = registry.read_only()
    assert "subtitle_save" not in names(read_only)
    assert "audio_generate" not in names(read_only)
    assert "film_render" not in names(read_only)
    assert {"storyboard_read", "episode_read", "film_read", "render_read"} <= names(read_only)


def test_audio_generate_is_declared_as_an_async_write_tool(store):
    registry, _service, _adapter, _context = mock_tools(store)
    definition = registry._definitions["audio_generate"]
    assert definition.effect == "write"
    assert definition.async_write_execute is not None
    assert definition.feature == "video"
    spec = next(item for item in registry.specs() if item["function"]["name"] == "audio_generate")
    parameters = spec["function"]["parameters"]
    assert {"episodeId", "lines", "voices", "confirm"} <= set(parameters["properties"])


def test_subtitle_save_is_still_declared_as_a_write_tool(store):
    registry, _service, _adapter, _context = mock_tools(store)
    definition = registry._definitions["subtitle_save"]
    assert definition.effect == "write"
    assert definition.execute is not None
    assert definition.feature == "video"


def test_subtitle_save_generates_for_a_bound_storyboard(store):
    registry, _service, _adapter, _context = mock_tools(store)
    plan_value = plan()
    episode = save_episode(registry, store, plan_value)
    saved = registry.execute(
        "storyboard_save",
        {
            "title": "雨夜咖啡店",
            "sceneId": episode["sceneIds"][1],
            "shots": [shot("雨夜街道"), shot("推门入店")],
        },
        store=store,
        project_id="coffee",
        operation_key="m6-tools-storyboard",
    )
    assert saved["ok"], saved
    storyboard_id = saved["data"]["storyboardId"]
    revision = store.snapshot()["storyboards"][storyboard_id]["revision"]
    result = registry.execute(
        "subtitle_save",
        {"scope": "storyboard", "storyboardId": storyboard_id},
        store=store,
        project_id="coffee",
        operation_key="m6-tools-save",
    )
    assert result["ok"], result
    assert result["data"]["latest"]["cueCount"] > 0
    assert result["data"]["latest"]["timelineSource"] == "storyboard"
    read = registry.execute(
        "storyboard_read",
        {"storyboardId": storyboard_id},
        store=store,
        project_id="coffee",
        operation_key="m6-tools-read",
    )
    assert read["ok"], read
    assert read["data"]["subtitle"]["latest"]["cueCount"] == result["data"]["latest"]["cueCount"]
    # Saving subtitles never registers a Job or changes the storyboard revision.
    assert store.snapshot()["jobs"] == {}
    assert store.snapshot()["storyboards"][storyboard_id]["revision"] == revision


def test_subtitle_save_without_a_bound_scene_is_refused(store):
    registry, _service, _adapter, _context = mock_tools(store)
    saved = registry.execute(
        "storyboard_save",
        {"title": "无场记分镜", "shots": [shot("雨夜街道")]},
        store=store,
        project_id="coffee",
        operation_key="m6-tools-unbound",
    )
    assert saved["ok"], saved
    result = registry.execute(
        "subtitle_save",
        {"scope": "storyboard", "storyboardId": saved["data"]["storyboardId"]},
        store=store,
        project_id="coffee",
        operation_key="m6-tools-unbound-save",
    )
    assert result["ok"] is False
    assert result["error"]["code"] == "RENDER_SOURCE_MISSING"
    assert store.snapshot()["subtitleTracks"] == {}


def test_episode_read_includes_subtitle_and_audio_summaries(store):
    registry, _service, _adapter, _context = mock_tools(store)
    plan_value = plan()
    episode = save_episode(registry, store, plan_value)
    read = registry.execute(
        "episode_read",
        {"episodeId": episode["episodeId"]},
        store=store,
        project_id="coffee",
        operation_key="m6-tools-episode-read",
    )
    assert read["ok"], read
    assert "subtitle" in read["data"]
    assert read["data"]["subtitle"] is None
    assert read["data"]["episodeId"] == episode["episodeId"]
    assert read["data"]["audio"]["provider"]["provider"] == "local"
    assert read["data"]["audio"]["usage"]["requests"] == 0
    assert read["data"]["audio"]["provider"]["voices"][0]["voiceId"] == "local-narrator"
    assert read["data"]["render"] is None
