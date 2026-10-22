"""M6 E1 service tests: subtitle_save, versions, correction, and plan/film timelines."""

import pytest
from subtitle_support import all_scenes_bound, film_with_dialogue

from vagent.errors import AppError
from vagent.video.assembler import bundled_engine
from vagent.video.subtitles import find_track, track_version_sha256


def requires_ffmpeg():
    try:
        bundled_engine()
        return True
    except AppError:
        return False


pytestmark = pytest.mark.skipif(not requires_ffmpeg(), reason="bundled FFmpeg is unavailable")


def save(registry, store, args, key):
    result = registry.execute("subtitle_save", args, store=store, project_id="coffee", operation_key=key)
    return result


def ok(registry, store, args, key):
    result = save(registry, store, args, key)
    assert result["ok"], result
    return result["data"]


async def test_save_generates_track_and_replays(store):
    fixture = await film_with_dialogue(store)
    registry = fixture["registry"]
    args = {"scope": "storyboard", "storyboardId": fixture["storyboardId"]}
    first = ok(registry, store, args, "sub-save-1")
    assert first["currentVersion"] == 1 and first["versionCount"] == 1
    latest = first["latest"]
    assert latest["timelineSource"] == "film"
    assert latest["filmVersion"] == 1
    assert latest["cueCount"] == len(latest["cues"]) > 0
    assert latest["srtUrl"] == f"/api/subtitles/{first['trackId']}/1/srt"
    assert latest["assUrl"] == f"/api/subtitles/{first['trackId']}/1/ass"
    assert latest["srtSha256"] and latest["assSha256"]
    assert latest["sha256"] == track_version_sha256(
        find_track(store.snapshot(), "coffee", "storyboard", fixture["storyboardId"]).latest
    )
    # The same inputs under another call key reuse the same version.
    second = ok(registry, store, args, "sub-save-2")
    assert second["currentVersion"] == 1 and second["versionCount"] == 1
    assert second["latest"] == latest
    # storyboard_read exposes the track through the shared read surface.
    read = registry.execute(
        "storyboard_read",
        {"storyboardId": fixture["storyboardId"]},
        store=store,
        project_id="coffee",
        operation_key="sub-read",
    )
    assert read["ok"], read
    assert read["data"]["subtitle"]["trackId"] == first["trackId"]
    assert read["data"]["subtitle"]["latest"]["cueCount"] == latest["cueCount"]


async def test_manual_correction_appends_a_version_and_keeps_the_old_one(store):
    fixture = await film_with_dialogue(store)
    registry = fixture["registry"]
    track = ok(registry, store, {"scope": "storyboard", "storyboardId": fixture["storyboardId"]}, "sub-1")
    cues = track["latest"]["cues"]
    corrected = [dict(cue) for cue in cues]
    corrected[0]["text"] = "改过的第一句。"
    args = {
        "scope": "storyboard",
        "storyboardId": fixture["storyboardId"],
        "trackId": track["trackId"],
        "expectedRevision": track["revision"],
        "cues": [
            {
                "startMs": cue["startMs"],
                "endMs": cue["endMs"],
                "text": cue["text"],
                "speaker": cue.get("speaker"),
            }
            for cue in corrected
        ],
    }
    second = ok(registry, store, args, "sub-2")
    assert second["currentVersion"] == 2 and second["versionCount"] == 2
    # Older versions stay as bounded summaries; the latest carries the cues.
    assert second["versions"][0]["version"] == 1
    assert second["versions"][0]["sha256"] == track["latest"]["sha256"]
    assert "cues" not in second["versions"][0]
    assert second["latest"]["cues"][0]["text"] == "改过的第一句。"
    assert second["latest"]["sha256"] != track["latest"]["sha256"]
    # A stale revision is refused, not silently appended.
    stale = save(registry, store, {**args, "cues": args["cues"], "expectedRevision": 0}, "sub-3")
    assert stale["ok"] is False and stale["error"]["code"] == "VERSION_CONFLICT"


async def test_manual_cues_reject_overlap_overflow_and_crossing(store):
    fixture = await film_with_dialogue(store)
    registry = fixture["registry"]
    track = ok(registry, store, {"scope": "storyboard", "storyboardId": fixture["storyboardId"]}, "sub-1")
    cues = track["latest"]["cues"]
    base = {
        "scope": "storyboard",
        "storyboardId": fixture["storyboardId"],
        "trackId": track["trackId"],
    }

    def crash(cues_value, key):
        return save(registry, store, {**base, "cues": cues_value}, key)

    overlap = crash(
        [
            {"startMs": cues[0]["startMs"], "endMs": cues[0]["endMs"], "text": "甲"},
            {"startMs": cues[0]["endMs"] - 100, "endMs": cues[0]["endMs"] + 100, "text": "乙"},
        ],
        "sub-overlap",
    )
    assert overlap["ok"] is False and overlap["error"]["code"] in {
        "SUBTITLE_CUE_CONFLICT",
        "INVALID_ARGUMENTS",
    }
    too_long = crash([{"startMs": 0, "endMs": 500, "text": "字" * 49}], "sub-long")
    assert too_long["ok"] is False and too_long["error"]["code"] in {"SUBTITLE_OVERFLOW", "INVALID_ARGUMENTS"}
    crossing = crash(
        [
            {
                "startMs": 1000,
                "endMs": cues[0]["endMs"] + 2000,
                "text": "跨界",
            }
        ],
        "sub-cross",
    )
    assert crossing["ok"] is False and crossing["error"]["code"] == "SUBTITLE_CUE_CONFLICT"


async def test_episode_track_needs_every_scene_bound(store):
    fixture = await film_with_dialogue(store)
    registry = fixture["registry"]
    result = save(
        registry,
        store,
        {"scope": "episode", "episodeId": fixture["episodeId"]},
        "sub-episode",
    )
    assert result["ok"] is False
    assert result["error"]["code"] == "RENDER_SOURCE_MISSING"


async def test_episode_track_aligns_across_bound_scenes(store):
    from shot_support import mock_tools

    registry, _service, _adapter, _context = mock_tools(store)
    bound = all_scenes_bound(store, registry)
    result = save(
        registry,
        store,
        {"scope": "episode", "episodeId": bound["episodeId"]},
        "sub-episode",
    )
    assert result["ok"], result
    data = result["data"]
    assert data["scope"] == "episode"
    latest = data["latest"]
    assert latest["storyboardVersion"] is None and latest["episodeVersion"] == 1
    assert latest["cueCount"] > 0
    total = latest["timeline"][-1]["startSeconds"] + latest["timeline"][-1]["durationSeconds"]
    assert latest["cues"][-1]["endSeconds"] <= total + 1e-6
    assert {cue["sceneId"] for cue in latest["cues"]} <= set(bound["sceneIds"])
