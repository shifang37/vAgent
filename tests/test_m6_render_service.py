"""M6 E3 storyboard render: transitions, title cards, profiles and failure isolation."""

import hashlib
import subprocess

import pytest
from render_support import PROJECT, renderable_storyboard

from vagent.errors import AppError
from vagent.video.assembler import bundled_engine
from vagent.video.films import find_film
from vagent.video.subtitle_service import parse_streams
from vagent.video.subtitles import render_srt, track_version

ENGINE = None
try:
    ENGINE = bundled_engine()
except AppError:
    pass

pytestmark = pytest.mark.skipif(ENGINE is None, reason="bundled FFmpeg is unavailable")


def args(fixture, **changes):
    base = {
        "scope": "storyboard",
        "storyboardId": fixture["storyboardId"],
        "trackId": fixture["trackId"],
        "profile": "720p-h264-aac-v1",
        "subtitleMode": "soft",
        "expectedRevision": fixture["revision"],
        "confirm": True,
    }
    base.update(changes)
    return base


async def render(fixture, store, *, key="e3-storyboard-render", **changes):
    return await fixture["service"].renders.render(
        args(fixture, **changes),
        project_id=PROJECT,
        operation_key=key,
        args=args(fixture, **changes),
    )


def probe(path):
    result = subprocess.run(
        [str(ENGINE.path), "-hide_banner", "-nostdin", "-i", str(path)],
        capture_output=True,
        check=False,
    )
    return parse_streams(result.stderr.decode("utf-8", "replace"))


async def test_soft_render_appends_a_film_version_and_replays_the_same_fingerprint(store):
    fixture = await renderable_storyboard(store)
    result = await render(fixture, store)
    assert result["ok"], result
    data = result["data"]
    assert data["status"] == "succeeded" and data["filmVersion"] == 1
    assert data["scenes"][0]["status"] == "succeeded"
    film = find_film(store.snapshot(), PROJECT, fixture["storyboardId"])
    version = film.versions[-1]
    assert version.render is not None and version.render.render_version == "ffmpeg-render-v1"
    assert version.assembly.assembly_version == "ffmpeg-render-native-v1"
    assert version.render.subtitle is not None and version.render.subtitle.retimed is False
    assert version.render.fingerprint == data["fingerprint"]
    path = store.home / store.snapshot()["media"][version.output_media_id]["relativePath"]
    streams = probe(path)
    assert streams.video["codec"] == "h264"
    assert (streams.video["width"], streams.video["height"]) == (1280, 720)
    assert abs(streams.duration_seconds - 20) <= 1 / 30 + 0.05
    assert any(item["codec"] == "mov_text" for item in streams.subtitles)
    _track, frozen = track_version(store.snapshot(), fixture["trackId"], 1)
    assert (
        version.render.subtitle.sidecar_sha256
        == hashlib.sha256(render_srt(frozen.cues).encode("utf-8")).hexdigest()
    )
    # A new operation key with the same inputs returns the existing film by fingerprint.
    replay = await render(fixture, store, key="e3-storyboard-replay")
    assert replay["ok"], replay
    assert replay["data"]["replay"] is True
    assert replay["data"]["filmVersion"] == 1
    assert len(find_film(store.snapshot(), PROJECT, fixture["storyboardId"]).versions) == 1


async def test_crossfade_and_fade_shorten_the_render_and_retime_the_sidecar(store):
    fixture = await renderable_storyboard(store)
    result = await render(
        fixture,
        store,
        profile="1080p-h264-aac-v1",
        subtitleMode="both",
        transitions=[
            {"afterShotId": fixture["shotIds"][0], "type": "crossfade", "durationMs": 500},
            {"afterShotId": fixture["shotIds"][1], "type": "fade", "durationMs": 300},
        ],
        titleCards=[{"text": "雨夜咖啡店", "startMs": 200, "durationMs": 1500}],
    )
    assert result["ok"], result
    data = result["data"]
    assert data["filmVersion"] == 1
    latest = data["film"]["latest"]
    assert latest["durationSeconds"] == 19.2
    assert (latest["width"], latest["height"]) == (1920, 1080)
    render_record = latest["render"]
    assert render_record["exportProfile"]["upscaled"] is True
    assert render_record["subtitle"]["retimed"] is True
    assert render_record["subtitle"]["sidecarUrl"] == (
        f"/api/films/{data['filmId']}/versions/1/subtitles.srt"
    )
    assert [item["type"] for item in render_record["transitions"]] == ["crossfade", "fade"]
    assert render_record["titleCards"][0]["text"] == "雨夜咖啡店"
    assert render_record["measurements"]["video"]["width"] == 1920
    path = store.home / store.snapshot()["media"][latest["outputMediaId"]]["relativePath"]
    streams = probe(path)
    assert abs(streams.duration_seconds - 19.2) <= 1 / 30 + 0.05
    assert any(item["codec"] == "mov_text" for item in streams.subtitles)


async def test_burn_export_has_no_subtitle_track(store):
    fixture = await renderable_storyboard(store)
    result = await render(fixture, store, subtitleMode="burn")
    assert result["ok"], result
    latest = result["data"]["film"]["latest"]
    path = store.home / store.snapshot()["media"][latest["outputMediaId"]]["relativePath"]
    streams = probe(path)
    assert not streams.subtitles
    assert latest["render"]["subtitle"]["sidecarUrl"] is None


async def test_invalid_transition_is_rejected_before_any_film_is_written(store):
    fixture = await renderable_storyboard(store)
    with pytest.raises(Exception) as error:
        await render(
            fixture,
            store,
            transitions=[
                {
                    "afterShotId": fixture["shotIds"][-1],
                    "type": "crossfade",
                    "durationMs": 500,
                }
            ],
        )
    assert getattr(error.value, "code", None) == "TRANSITION_INVALID"
    assert find_film(store.snapshot(), PROJECT, fixture["storyboardId"]) is None


async def test_render_read_reports_scene_progress(store):
    fixture = await renderable_storyboard(store)
    result = await render(fixture, store)
    assert result["ok"], result
    read = fixture["service"].renders.read({"storyboardId": fixture["storyboardId"]}, PROJECT)
    assert read["render"]["status"] == "succeeded"
    assert read["render"]["renderId"] == result["data"]["renderId"]
    assert read["render"]["scenes"][0]["shotIds"] == list(fixture["shotIds"])
    assert read["render"]["filmVersion"] == 1


async def test_corrupt_input_marks_the_scene_and_task_failed(store):
    fixture = await renderable_storyboard(store)
    state = store.snapshot()
    job = next(iter(state["jobs"].values()))
    media = state["media"][(job["result"] or {}).get("mediaRefs", [{}])[0]["mediaId"]]
    path = store.home / media["relativePath"]
    path.write_bytes(path.read_bytes()[:64])
    with pytest.raises(AppError) as error:
        await render(fixture, store)
    assert error.value.code == "RENDER_SOURCE_MISSING"
    assert find_film(store.snapshot(), PROJECT, fixture["storyboardId"]) is None
    read = fixture["service"].renders.read({"storyboardId": fixture["storyboardId"]}, PROJECT)
    assert read["render"]["status"] == "failed"
    assert read["render"]["scenes"][0]["status"] == "failed"
    assert read["render"]["scenes"][0]["failureStage"] == "source"


async def test_film_render_tool_requires_confirmation(store):
    fixture = await renderable_storyboard(store)
    context = fixture["context"].model_copy(update={"tool_call_id": "render-tool", "model_step": 9})
    args_value = {
        "scope": "storyboard",
        "storyboardId": fixture["storyboardId"],
        "trackId": fixture["trackId"],
        "confirm": False,
    }
    refused = await fixture["registry"].aexecute(
        "film_render",
        args_value,
        store=store,
        project_id=PROJECT,
        operation_key=context.operation_key,
        context=context,
    )
    assert refused["ok"] is False and refused["error"]["code"] == "CONFIRMATION_REQUIRED"
    context = context.model_copy(update={"tool_call_id": "render-tool-2", "model_step": 10})
    accepted = await fixture["registry"].aexecute(
        "film_render",
        {**args_value, "confirm": True},
        store=store,
        project_id=PROJECT,
        operation_key=context.operation_key,
        context=context,
    )
    assert accepted["ok"], accepted
    read = fixture["registry"].execute(
        "render_read",
        {"storyboardId": fixture["storyboardId"]},
        store=store,
        project_id=PROJECT,
        operation_key="e3-render-read-tool",
    )
    assert read["ok"], read
    assert read["data"]["render"]["status"] == "succeeded"


def test_scene_durations_fixture_is_exact():
    from render_support import scene_durations

    assert scene_durations({"shotCount": 3, "durationSeconds": 14}) == [5, 5, 4]
    assert sum(scene_durations({"shotCount": 3, "durationSeconds": 14})) == 14
