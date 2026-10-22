"""M6 E1 acceptance: a 60-second film whose subtitles have zero timeline drift."""

import hashlib
import re
import subprocess

import pytest
from conftest import VideoClock
from film_support import delivered_clip, link_shot_versions
from shot_support import mock_tools, shot
from subtitle_support import plan, save_episode

from vagent.errors import AppError
from vagent.video.assembler import bundled_engine
from vagent.video.film_service import FilmService
from vagent.video.jobs import JobService
from vagent.video.providers.mock import MockVideoAdapter
from vagent.video.subtitle_service import SubtitleService, parse_streams
from vagent.video.subtitles import render_srt

ENGINE = None
try:
    ENGINE = bundled_engine()
except AppError:
    pass

pytestmark = pytest.mark.skipif(ENGINE is None, reason="bundled FFmpeg is unavailable")
SHOTS = 12


def srt_timestamps(text: str) -> list[tuple[int, int]]:
    result = []
    for match in re.finditer(r"(\d+):(\d+):(\d+),(\d+) --> (\d+):(\d+):(\d+),(\d+)", text):
        values = [int(item) for item in match.groups()]
        start = ((values[0] * 60 + values[1]) * 60 + values[2]) * 1000 + values[3]
        end = ((values[4] * 60 + values[5]) * 60 + values[6]) * 1000 + values[7]
        result.append((start, end))
    return result


async def sixty_second_film(store):
    clock = VideoClock()
    registry, _service, _adapter, _context = mock_tools(store, clock=clock)
    plan_value = plan()
    episode = save_episode(registry, store, plan_value)
    scene_index = next(index for index, scene in enumerate(plan_value["scenes"]) if scene["dialogue"])
    saved = registry.execute(
        "storyboard_save",
        {
            "title": "六十字幕样例",
            "sceneId": episode["sceneIds"][scene_index],
            "shots": [shot(f"雨夜街道第 {number} 镜") for number in range(1, SHOTS + 1)],
        },
        store=store,
        project_id="coffee",
        operation_key="m6-acceptance-board",
    )
    assert saved["ok"], saved
    jobs = []
    for index in range(SHOTS):
        clock.advance(6)
        jobs.append(
            await delivered_clip(store, clock, run_id=f"m6-acceptance-{index}", prompt=f"雨夜第 {index} 镜")
        )
    revision = link_shot_versions(
        store,
        storyboard_id=saved["data"]["storyboardId"],
        storyboard_version=1,
        entries=[
            (
                shot_id,
                job.id,
                job.result.media_refs[0].media_id,
                job.request.model_dump(mode="json", by_alias=True),
            )
            for shot_id, job in zip(saved["data"]["shotIds"], jobs, strict=True)
        ],
    )
    film_service = FilmService(JobService(store, [MockVideoAdapter(store)]))
    assembled = await film_service.assemble(
        project_id="coffee",
        storyboard_id=saved["data"]["storyboardId"],
        expected_revision=revision,
        operation_key="m6-acceptance-film",
        args={"storyboardId": saved["data"]["storyboardId"], "expectedRevision": revision},
    )
    assert assembled["ok"], assembled
    return {
        "storyboardId": saved["data"]["storyboardId"],
        "registry": registry,
        "film": assembled["data"],
    }


async def test_sixty_second_film_subtitles_are_pixel_exact_on_the_frozen_timeline(store):
    fixture = await sixty_second_film(store)
    film = fixture["film"]
    latest_film = film["latest"]
    assert latest_film["durationSeconds"] >= 60
    total_microseconds = round(latest_film["durationSeconds"] * 1_000_000)
    result = fixture["registry"].execute(
        "subtitle_save",
        {"scope": "storyboard", "storyboardId": fixture["storyboardId"]},
        store=store,
        project_id="coffee",
        operation_key="m6-acceptance-subtitles",
    )
    assert result["ok"], result
    track = result["data"]
    latest = track["latest"]
    assert latest["timelineSource"] == "film"
    assert latest["filmVersion"] == 1
    assert latest["cueCount"] > 0
    # Zero drift: every cue is inside its own film segment and the last cue ends on
    # the exact frozen timeline end (integer microseconds, no float accumulation).
    from vagent.video.subtitles import timeline_windows_us, track_version

    _track, frozen = track_version(store.snapshot(), track["trackId"], 1)
    windows = {
        entry.shot_id: window
        for entry, window in zip(frozen.timeline, timeline_windows_us(frozen.timeline), strict=True)
    }
    assert len(windows) == SHOTS
    for cue in frozen.cues:
        low, high = windows[cue.shot_id]
        assert low <= cue.start.total_microseconds()
        assert cue.end.total_microseconds() <= high
    assert frozen.cues[-1].end.total_microseconds() == timeline_windows_us(frozen.timeline)[-1][1]
    # The track's frozen timeline is the film assembly timeline, not the shorter MP4
    # metadata duration (the presentation timeline includes the audio priming).
    from vagent.video.films import find_film

    film_record = find_film(store.snapshot(), "coffee", fixture["storyboardId"])
    film_windows = timeline_windows_us(film_record.versions[0].assembly.timeline)
    assert timeline_windows_us(frozen.timeline) == film_windows
    assert frozen.cues[-1].end.total_microseconds() == film_windows[-1][1]
    assert film_windows[-1][1] >= 60_000_000
    assert total_microseconds > 60_000_000
    # The sidecar SRT carries exactly those millisecond timestamps.
    stamps = srt_timestamps(render_srt_from(latest))
    assert len(stamps) == latest["cueCount"]
    for (start, end), cue in zip(stamps, latest["cues"], strict=True):
        assert start == cue["startMs"] and end == cue["endMs"]
    # A soft export appends a rendered version whose sidecar digest is the SRT bytes.
    service = SubtitleService(JobService(store, [MockVideoAdapter(store)]))
    exported = await service.export(
        project_id="coffee",
        track_id=track["trackId"],
        mode="soft",
        operation_key="m6-acceptance-export",
        args={"trackId": track["trackId"], "mode": "soft", "profile": "720p-h264-aac-v1"},
    )
    assert exported["ok"], exported
    rendered = exported["data"]["latest"]
    assert rendered["render"]["subtitle"]["mode"] == "soft"
    media = store.snapshot()["media"][rendered["outputMediaId"]]
    path = store.home / media["relativePath"]
    probe = subprocess.run(
        [str(ENGINE.path), "-hide_banner", "-nostdin", "-i", str(path)],
        capture_output=True,
        check=False,
    )
    streams = parse_streams(probe.stderr.decode("utf-8", "replace"))
    assert any(item["codec"] == "mov_text" for item in streams.subtitles)
    assert abs(streams.duration_seconds - latest_film["durationSeconds"]) <= 1 / 30 + 0.01
    from vagent.video.subtitles import track_version

    _track, frozen = track_version(store.snapshot(), track["trackId"], 1)
    srt = render_srt(frozen.cues)
    assert hashlib.sha256(srt.encode("utf-8")).hexdigest() == rendered["render"]["subtitle"]["sidecarSha256"]


def render_srt_from(view: dict) -> str:
    """Rebuild the SRT of the saved track through the real serializer."""
    from vagent.video.films import Rational
    from vagent.video.subtitles import SubtitleCue, render_srt

    cues = [
        SubtitleCue(
            start=Rational.microseconds(cue["startMs"] * 1000),
            end=Rational.microseconds(cue["endMs"] * 1000),
            text=cue["text"],
            speaker=cue.get("speaker"),
            scene_id=cue.get("sceneId"),
            shot_id=cue.get("shotId"),
            source_sha256="0" * 64,
        )
        for cue in view["cues"]
    ]
    return render_srt(cues)
