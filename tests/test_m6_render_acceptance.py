"""M6 E3 acceptance: a 60-90 second episode with transitions, title cards and audio."""

import re
import subprocess

import pytest
from render_support import PROJECT, renderable_episode

from vagent.errors import AppError
from vagent.video.assembler import bundled_engine
from vagent.video.films import find_episode_film
from vagent.video.renders import render_sidecar_text
from vagent.video.subtitle_service import parse_streams

ENGINE = None
try:
    ENGINE = bundled_engine()
except AppError:
    pass

pytestmark = pytest.mark.skipif(ENGINE is None, reason="bundled FFmpeg is unavailable")
TIMESTAMP = re.compile(r"(\d+):(\d+):(\d+),(\d+) --> (\d+):(\d+):(\d+),(\d+)")


def milliseconds(groups) -> tuple[int, int]:
    values = [int(item) for item in groups]
    start = ((values[0] * 60 + values[1]) * 60 + values[2]) * 1000 + values[3]
    end = ((values[4] * 60 + values[5]) * 60 + values[6]) * 1000 + values[7]
    return start, end


def parse_srt_timestamps(text: str) -> list[tuple[int, int]]:
    return [milliseconds(match.groups()) for match in TIMESTAMP.finditer(text)]


def probe(path):
    result = subprocess.run(
        [str(ENGINE.path), "-hide_banner", "-nostdin", "-i", str(path)],
        capture_output=True,
        check=False,
    )
    return parse_streams(result.stderr.decode("utf-8", "replace"))


def black_frames(path) -> list[str]:
    result = subprocess.run(
        [
            str(ENGINE.path),
            "-hide_banner",
            "-nostdin",
            "-loglevel",
            "info",
            "-i",
            str(path),
            "-vf",
            "blackdetect=d=0.04:pix_th=0.08",
            "-an",
            "-f",
            "null",
            "-",
        ],
        capture_output=True,
        check=False,
    )
    return [line for line in result.stderr.decode("utf-8", "replace").splitlines() if "black_start" in line]


async def test_ninety_second_episode_is_playable_and_free_of_black_transitions(store):
    fixture = await renderable_episode(store)
    request = {
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
            },
            {
                "afterShotId": fixture["boards"][1]["shotIds"][0],
                "type": "fade",
                "durationMs": 300,
            },
        ],
        "titleCards": [
            {"text": "雨夜咖啡店", "startMs": 150, "durationMs": 1200},
            {"text": "未完待续", "startMs": 87_500, "durationMs": 1500},
        ],
        "confirm": True,
    }
    result = await fixture["service"].renders.render(
        request,
        project_id=PROJECT,
        operation_key="e3-acceptance-episode",
        args=request,
    )
    assert result["ok"], result
    data = result["data"]
    latest = data["film"]["latest"]
    duration = latest["durationSeconds"]
    assert 60 <= duration <= 90
    assert duration == 89.3  # 90.0 - 0.4 - 0.3
    film = find_episode_film(store.snapshot(), PROJECT, fixture["episode"]["episodeId"])
    assert film is not None
    render = film.versions[-1].render
    assert render.subtitle.mode == "both"
    assert render.subtitle.retimed is True
    assert [item.type for item in render.transitions] == ["crossfade", "fade"]
    assert [item.text for item in render.title_cards] == ["雨夜咖啡店", "未完待续"]
    assert len(render.audio.tracks) == 19
    assert len({item.start.fraction() for item in render.audio.tracks}) > 1
    verification = render.audio.loudness.output["verification"]
    assert abs(verification["integratedLufs"] + 16) <= 0.5
    assert verification["truePeakDb"] <= -1
    path = store.home / store.snapshot()["media"][latest["outputMediaId"]]["relativePath"]
    streams = probe(path)
    assert streams.video["codec"] == "h264"
    assert (streams.video["width"], streams.video["height"]) == (1280, 720)
    assert streams.audio["codec"] == "aac"
    assert (streams.audio["sample_rate"], streams.audio["channels"]) == (48000, 2)
    assert any(item["codec"] == "mov_text" for item in streams.subtitles)
    # The delivered timeline is within one frame of the effective timeline.
    assert abs(streams.duration_seconds - duration) <= 1 / 30 + 0.05
    assert not black_frames(path)
    # The sidecar is derived from the same remap and never runs past the film.
    text, filename = render_sidecar_text(store.snapshot(), film, film.versions[-1])
    assert filename == f"film-{film.id}-v1.srt"
    stamps = parse_srt_timestamps(text)
    assert stamps and stamps[0][0] >= 0
    assert stamps[-1][1] <= round(duration * 1000) + 1
    assert all(previous[1] <= current[0] for previous, current in zip(stamps, stamps[1:]))


async def test_sidecar_timestamps_follow_the_effective_episode_timeline(store):
    fixture = await renderable_episode(store)
    request = {
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
        "confirm": True,
    }
    result = await fixture["service"].renders.render(
        request, project_id=PROJECT, operation_key="e3-acceptance-sidecar", args=request
    )
    assert result["ok"], result
    film = find_episode_film(store.snapshot(), PROJECT, fixture["episode"]["episodeId"])
    version = film.versions[-1]
    text, _filename = render_sidecar_text(store.snapshot(), film, version)
    stamps = parse_srt_timestamps(text)
    assert stamps
    assert stamps[-1][1] <= round(version.metadata.duration_seconds * 1000) + 1
    # The first scene's transition moved every later scene 0.4 s earlier.
    from vagent.video.subtitles import track_version

    _track, frozen = track_version(
        store.snapshot(),
        version.render.subtitle.track_id,
        version.render.subtitle.track_version,
    )
    source_last = frozen.cues[-1].end.total_microseconds() // 1000
    assert stamps[-1][1] <= source_last
