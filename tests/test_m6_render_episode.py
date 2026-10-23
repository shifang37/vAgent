"""M6 E3 episode render: cross-scene transitions, whole-episode audio and isolation."""

import subprocess

import pytest
from render_support import PROJECT, renderable_episode

from vagent.errors import AppError
from vagent.video.assembler import bundled_engine
from vagent.video.films import find_episode_film
from vagent.video.subtitle_service import parse_streams

ENGINE = None
try:
    ENGINE = bundled_engine()
except AppError:
    pass

pytestmark = pytest.mark.skipif(ENGINE is None, reason="bundled FFmpeg is unavailable")


def args(fixture, **changes):
    base = {
        "scope": "episode",
        "episodeId": fixture["episode"]["episodeId"],
        "trackId": fixture["trackId"],
        "profile": "720p-h264-aac-v1",
        "subtitleMode": "both",
        "transitions": [
            {
                "afterShotId": fixture["boards"][0]["shotIds"][-1],
                "type": "crossfade",
                "durationMs": 400,
            }
        ],
        "titleCards": [
            {"text": "雨夜咖啡店", "startMs": 100, "durationMs": 1200},
            {"text": "未完待续", "startMs": 88_000, "durationMs": 1500},
        ],
        "confirm": True,
    }
    base.update(changes)
    return base


async def test_whole_episode_renders_cross_scene_with_voice_and_loudness(store):
    fixture = await renderable_episode(store)
    result = await fixture["service"].renders.render(
        args(fixture),
        project_id=PROJECT,
        operation_key="e3-episode-render",
        args=args(fixture),
    )
    assert result["ok"], result
    data = result["data"]
    assert data["status"] == "succeeded" and data["filmVersion"] == 1
    assert len(data["scenes"]) == len(fixture["plan"]["scenes"]) == 8
    assert all(scene["status"] == "succeeded" for scene in data["scenes"])
    film = find_episode_film(store.snapshot(), PROJECT, fixture["episode"]["episodeId"])
    assert film is not None and film.episode_id == fixture["episode"]["episodeId"]
    version = film.versions[-1]
    assert version.episode_version == fixture["episode"]["version"]
    assert version.render.sources.episode_version == fixture["episode"]["version"]
    assert version.render.sources.storyboard_version is None
    assert version.render.subtitle.retimed is True
    assert len(version.render.transitions) == 1
    assert [card.text for card in version.render.title_cards] == ["雨夜咖啡店", "未完待续"]
    # Every frozen dialogue line has a voice asset placed on the effective timeline.
    assert len(version.render.audio.tracks) == len(fixture["audio"]["lines"])
    starts = {item.start.fraction() for item in version.render.audio.tracks}
    assert len(starts) > 1
    loudness = version.render.audio.loudness
    assert loudness.input is not None and loudness.output is not None
    assert abs(float(loudness.output["verification"]["integratedLufs"]) + 16) <= 0.5
    assert float(loudness.output["verification"]["truePeakDb"]) <= -1
    latest = data["film"]["latest"]
    assert latest["durationSeconds"] == 89.6
    assert latest["render"]["subtitle"]["sidecarUrl"].endswith("/subtitles.srt")
    path = store.home / store.snapshot()["media"][latest["outputMediaId"]]["relativePath"]
    probe = subprocess.run(
        [str(ENGINE.path), "-hide_banner", "-nostdin", "-i", str(path)],
        capture_output=True,
        check=False,
    )
    streams = parse_streams(probe.stderr.decode("utf-8", "replace"))
    assert abs(streams.duration_seconds - 89.6) <= 1 / 30 + 0.05
    assert any(item["codec"] == "mov_text" for item in streams.subtitles)
    # The episode read view exposes the committed render.
    read = fixture["registry"].execute(
        "episode_read",
        {"episodeId": fixture["episode"]["episodeId"]},
        store=store,
        project_id=PROJECT,
        operation_key="e3-episode-read",
    )
    assert read["ok"], read
    assert read["data"]["render"]["status"] == "succeeded"
    assert read["data"]["render"]["filmVersion"] == 1


async def test_the_same_episode_fingerprint_replays_without_a_second_render(store):
    fixture = await renderable_episode(store)
    first = await fixture["service"].renders.render(
        args(fixture), project_id=PROJECT, operation_key="e3-episode-1", args=args(fixture)
    )
    assert first["ok"], first
    replay = await fixture["service"].renders.render(
        args(fixture), project_id=PROJECT, operation_key="e3-episode-2", args=args(fixture)
    )
    assert replay["ok"], replay
    assert replay["data"]["replay"] is True
    assert replay["data"]["filmVersion"] == 1
    film = find_episode_film(store.snapshot(), PROJECT, fixture["episode"]["episodeId"])
    assert len(film.versions) == 1


async def test_one_undelivered_scene_fails_that_scene_but_renders_the_others(store):
    fixture = await renderable_episode(store, skip_scene=2)
    with pytest.raises(AppError) as error:
        await fixture["service"].renders.render(
            args(fixture),
            project_id=PROJECT,
            operation_key="e3-episode-isolated",
            args=args(fixture),
        )
    assert error.value.code == "RENDER_SOURCE_MISSING"
    assert find_episode_film(store.snapshot(), PROJECT, fixture["episode"]["episodeId"]) is None
    read = fixture["service"].renders.read({"episodeId": fixture["episode"]["episodeId"]}, PROJECT)
    scenes = read["render"]["scenes"]
    assert read["render"]["status"] == "failed"
    assert scenes[2]["status"] == "failed" and scenes[2]["failureStage"] == "source"
    assert sum(scene["status"] == "succeeded" for scene in scenes) == 7
    # The delivered per-scene clips are untouched.
    assert len(store.snapshot()["jobs"]) == sum(
        scene["shotCount"] for index, scene in enumerate(fixture["plan"]["scenes"]) if index != 2
    )
