"""M6 E1 export: soft mov_text, burn-in, both, idempotency and failure matrix."""

import hashlib
import subprocess

import pytest
from subtitle_support import film_with_dialogue

from vagent.errors import AppError
from vagent.video import subtitle_service as service_module
from vagent.video.assembler import bundled_engine
from vagent.video.films import find_film
from vagent.video.jobs import JobService
from vagent.video.providers.mock import MockVideoAdapter
from vagent.video.subtitle_service import SubtitleService, parse_streams
from vagent.video.subtitles import render_srt


def engine_available():
    try:
        return bundled_engine()
    except AppError:
        return None


ENGINE = engine_available()
pytestmark = pytest.mark.skipif(ENGINE is None, reason="bundled FFmpeg is unavailable")
WINDOWS_FFMPEG_SHA256 = "2ce797a0f88d7f067180338fb227f7b1928ea727bd9a4d7a1d022f7c52af71a3"
# Recorded from the bundled FFmpeg 7.1 build with the shipped VAgent Subtitle SC
# font: the exact burn-in pixels of the first subtitle at 1.0 s.
BURN_FRAME_SHA256 = {
    # Windows 11 + the pinned imageio-ffmpeg build (local acceptance).
    "7dd495726a92e56334e0e2be43aa1fbf0178e5bd76970caf2027d64cbc52bd6c",
    # Windows Server 2022/2025 GitHub runner images render the same bundled font
    # through a different system font stack; the PNG bytes differ, the subtitles do not.
    "9e705833f0af90f06bbde327d379ecc10c5d47653a7f02715a234a327d48df0e",
}


def subtitle_service(store):
    return SubtitleService(JobService(store, [MockVideoAdapter(store)]))


async def track_for(store, fixture):
    registry = fixture["registry"]
    result = registry.execute(
        "subtitle_save",
        {"scope": "storyboard", "storyboardId": fixture["storyboardId"]},
        store=store,
        project_id="coffee",
        operation_key="m6-export-track",
    )
    assert result["ok"], result
    return result["data"]


async def export(service, fixture, track, *, mode="soft", key="export-1", profile="720p-h264-aac-v1"):
    args = {"trackId": track["trackId"], "mode": mode, "profile": profile}
    return await service.export(
        project_id="coffee",
        track_id=track["trackId"],
        mode=mode,
        profile_name=profile,
        operation_key=key,
        args=args,
    )


def probe(path):
    process = subprocess.run(
        [str(ENGINE.path), "-hide_banner", "-nostdin", "-i", str(path)],
        capture_output=True,
        check=False,
    )
    return parse_streams(process.stderr.decode("utf-8", "replace"))


def frame_sha(path, at="1.0"):
    output = path.with_name("frame-" + path.stem + ".png")
    subprocess.run(
        [
            str(ENGINE.path),
            "-hide_banner",
            "-loglevel",
            "error",
            "-nostdin",
            "-ss",
            at,
            "-i",
            str(path),
            "-frames:v",
            "1",
            "-y",
            str(output),
        ],
        capture_output=True,
        check=True,
    )
    return hashlib.sha256(output.read_bytes()).hexdigest()


async def test_soft_export_appends_a_rendered_version(store):
    fixture = await film_with_dialogue(store)
    track = await track_for(store, fixture)
    service = subtitle_service(store)
    state = store.snapshot()
    film = find_film(state, "coffee", fixture["storyboardId"])
    base = film.versions[-1]
    result = await export(service, fixture, track)
    assert result["ok"], result
    data = result["data"]
    assert data["currentVersion"] == 2 and data["versionCount"] == 2
    latest = data["latest"]
    assert latest["render"]["renderVersion"] == "ffmpeg-render-v1"
    assert latest["render"]["subtitle"]["mode"] == "soft"
    assert latest["render"]["subtitle"]["trackVersion"] == 1
    assert latest["render"]["subtitle"]["sidecarSha256"]
    assert latest["render"]["exportProfile"]["name"] == "720p-h264-aac-v1"
    assert latest["mediaAvailable"] is True
    # The rendered version keeps the assembled version's inputs and timeline.
    assert latest["inputs"] == data["versions"][0]["inputs"]
    assert latest["timeline"] == data["versions"][0]["timeline"]
    assert [item["shotId"] for item in latest["timeline"]] == fixture["shotIds"]
    media = store.snapshot()["media"][latest["outputMediaId"]]
    path = store.home / media["relativePath"]
    assert path.is_file()
    assert media["sha256"] == hashlib.sha256(path.read_bytes()).hexdigest()
    assert media["filmVersion"] == 2
    streams = probe(path)
    assert streams.video["codec"] == "h264"
    assert (streams.video["width"], streams.video["height"]) == (1280, 720)
    assert any(item["codec"] == "mov_text" for item in streams.subtitles)
    assert abs(streams.duration_seconds - base.metadata.duration_seconds) < 0.05
    # The exported sidecar is exactly the frozen track's deterministic SRT.
    from vagent.video.subtitles import track_version

    _track, version = track_version(store.snapshot(), track["trackId"], 1)
    srt = render_srt(version.cues)
    assert hashlib.sha256(srt.encode("utf-8")).hexdigest() == latest["render"]["subtitle"]["sidecarSha256"]


async def test_same_inputs_replay_the_rendered_version(store):
    fixture = await film_with_dialogue(store)
    track = await track_for(store, fixture)
    service = subtitle_service(store)
    first = (await export(service, fixture, track))["data"]
    second = (
        await service.export(
            project_id="coffee",
            track_id=track["trackId"],
            mode="soft",
            profile_name="720p-h264-aac-v1",
            operation_key="export-2",
            args={"trackId": track["trackId"], "mode": "soft", "profile": "720p-h264-aac-v1"},
        )
    )["data"]
    assert second["currentVersion"] == first["currentVersion"] == 2
    assert second["latest"]["outputMediaId"] == first["latest"]["outputMediaId"]
    assert len(store.snapshot()["films"][first["filmId"]]["versions"]) == 2


async def test_burn_export_has_no_subtitle_track_and_changes_the_picture(store):
    fixture = await film_with_dialogue(store)
    track = await track_for(store, fixture)
    service = subtitle_service(store)
    state = store.snapshot()
    film = find_film(state, "coffee", fixture["storyboardId"])
    source = store.home / state["media"][film.versions[-1].output_media_id]["relativePath"]
    result = (await export(service, fixture, track, mode="burn"))["data"]
    assert result["currentVersion"] == 2
    render = result["latest"]["render"]
    assert render["subtitle"]["mode"] == "burn"
    assert render["subtitle"]["sidecarSha256"] is None
    assert render["subtitle"]["fontRef"]["name"] == "VAgent Subtitle SC"
    output = store.home / store.snapshot()["media"][result["latest"]["outputMediaId"]]["relativePath"]
    streams = probe(output)
    assert streams.subtitles == []
    burned_frame = frame_sha(output)
    assert burned_frame != frame_sha(source)
    if ENGINE.binary_sha256 == WINDOWS_FFMPEG_SHA256:
        # The pinned pixels prove the bundled subset font rendered the Chinese text.
        assert burned_frame in BURN_FRAME_SHA256
    assert abs(streams.duration_seconds - film.versions[-1].metadata.duration_seconds) < 0.05


async def test_both_export_burns_and_keeps_the_soft_track(store):
    fixture = await film_with_dialogue(store)
    track = await track_for(store, fixture)
    service = subtitle_service(store)
    result = (await export(service, fixture, track, mode="both"))["data"]
    assert result["currentVersion"] == 2
    render = result["latest"]["render"]
    assert render["subtitle"]["mode"] == "both"
    assert render["subtitle"]["sidecarSha256"]
    output = store.home / store.snapshot()["media"][result["latest"]["outputMediaId"]]["relativePath"]
    streams = probe(output)
    assert any(item["codec"] == "mov_text" for item in streams.subtitles)


async def test_1080p_and_unsupported_modes_are_refused(store):
    fixture = await film_with_dialogue(store)
    track = await track_for(store, fixture)
    service = subtitle_service(store)
    with pytest.raises(AppError) as error:
        await export(service, fixture, track, profile="1080p-h264-aac-v1")
    assert error.value.code == "RENDER_PROFILE_UNSUPPORTED"
    with pytest.raises(AppError) as error:
        await export(service, fixture, track, mode="sidecar")
    assert error.value.code == "RENDER_PROFILE_UNSUPPORTED"


async def test_missing_bundled_font_refuses_the_render(store, monkeypatch):
    fixture = await film_with_dialogue(store)
    track = await track_for(store, fixture)
    service = subtitle_service(store)

    def missing():
        raise AppError("FONT_MISSING", "bundle broken")

    monkeypatch.setattr(service_module, "bundled_font_path", missing)
    with pytest.raises(AppError) as error:
        await export(service, fixture, track)
    assert error.value.code == "FONT_MISSING"
    assert len(store.snapshot()["films"][fixture["film"]["filmId"]]["versions"]) == 1


async def test_a_plan_timeline_cannot_export_against_the_delivered_film(store):
    fixture = await film_with_dialogue(store)
    # Rebuild the pre-assembly plan track: same storyboard, but no film source.
    from vagent.video.subtitles import save_subtitles

    state = store.snapshot()
    film = find_film(state, "coffee", fixture["storyboardId"])
    stripped = {**state}
    stripped["films"] = {}
    plan = save_subtitles(
        {"scope": "storyboard", "storyboardId": fixture["storyboardId"]},
        stripped,
        "coffee",
    )
    assert plan["latest"]["timelineSource"] == "storyboard"
    # Commit that plan track into the real store, then export against the film.
    from vagent.video.subtitles import SubtitleTrack

    track_record = SubtitleTrack.model_validate(stripped["subtitleTracks"][plan["trackId"]])
    store.transaction(
        lambda draft: draft["subtitleTracks"].__setitem__(
            track_record.id, track_record.model_dump(mode="json", by_alias=True)
        )
    )
    service = subtitle_service(store)
    with pytest.raises(AppError) as error:
        await service.export(
            project_id="coffee",
            track_id=plan["trackId"],
            mode="soft",
            operation_key="plan-export",
            args={"trackId": plan["trackId"], "mode": "soft"},
        )
    assert error.value.code == "RENDER_TIMELINE_MISMATCH"
    assert len(store.snapshot()["films"][film.id]["versions"]) == 1
