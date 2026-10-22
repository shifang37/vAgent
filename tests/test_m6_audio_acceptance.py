"""M6 E2 offline acceptance: episode dialogue -> local TTS -> mix -> playable MP4."""

import json
import subprocess
from fractions import Fraction

import pytest

from vagent.errors import AppError
from vagent.video.assembler import bundled_engine
from vagent.video.audio import AudioAsset, ProviderAudioSource
from vagent.video.mixer import MixTrack, mix_tracks
from vagent.video.tts import inspect_wav, synthesize_local_wav


def required_engine():
    try:
        return bundled_engine()
    except AppError:
        return None


ENGINE = required_engine()
pytestmark = pytest.mark.skipif(ENGINE is None, reason="bundled FFmpeg is unavailable")


def test_episode_to_tts_to_mix_to_a_playable_mp4(store):
    from shot_support import mock_tools
    from subtitle_support import plan, save_episode

    registry, service, _adapter, context = mock_tools(store)
    value = plan()
    episode = save_episode(registry, store, value)
    scene_index = next(index for index, scene in enumerate(value["scenes"]) if scene["dialogue"])
    scene = value["scenes"][scene_index]
    scene_id = episode["sceneIds"][scene_index]
    speakers = sorted({(line["speaker"] or "画外") for line in scene["dialogue"]})
    args = {
        "episodeId": episode["episodeId"],
        "voices": [{"speaker": speaker, "voiceId": "local-narrator"} for speaker in speakers],
        "confirm": True,
        "lines": [{"sceneId": scene_id, "paragraph": line["paragraph"]} for line in scene["dialogue"]],
    }
    scoped = context.model_copy(update={"tool_call_id": "acceptance-audio"})

    import asyncio

    result = asyncio.run(
        registry.aexecute(
            "audio_generate",
            args,
            store=store,
            project_id="coffee",
            operation_key=scoped.operation_key,
            context=scoped,
        )
    )
    assert result["ok"], result
    state = store.snapshot()
    assert state["jobs"] == {}
    assert len(state["audio"]) == len(scene["dialogue"])
    tracks = []
    for index, line in enumerate(result["data"]["lines"]):
        asset = AudioAsset.model_validate(state["audio"][line["audioId"]])
        version = asset.versions[0]
        source = version.source
        assert isinstance(source, ProviderAudioSource)
        assert source.provider == "local"
        media = state["media"][version.media_id]
        path = store.home / media["relativePath"]
        assert path.is_file() and path.read_bytes()[:4] == b"RIFF"
        tracks.append(
            MixTrack(
                audio_id=asset.id,
                audio_version=version.version,
                kind="voice",
                path=path,
                duration_seconds=version.duration_seconds,
                start_seconds=Fraction(index * 2),
                gain_db=0.0,
                ducking=False,
                sha256=version.sha256,
            )
        )
    duration = 12
    bgm_path = store.home / "acceptance-bgm.wav"
    bgm_path.write_bytes(synthesize_local_wav("背景音乐" * 20, "local-male"))
    tracks.append(
        MixTrack(
            audio_id="bgm-acceptance",
            audio_version=1,
            kind="bgm",
            path=bgm_path,
            duration_seconds=inspect_wav(bgm_path.read_bytes()).duration_seconds,
            start_seconds=Fraction(0),
            gain_db=-18.0,
            ducking=True,
        )
    )
    mixed = store.home / "acceptance-mix.wav"

    async def mix():
        return await mix_tracks(tracks=tracks, output=mixed, duration_seconds=Fraction(duration))

    mix_result = asyncio.run(mix())
    assert abs(mix_result.loudness_verification["integratedLufs"] - (-16.0)) <= 0.5
    assert mix_result.loudness_verification["truePeakDb"] <= -1.0
    assert (mix_result.sample_rate, mix_result.channels) == (48000, 2)

    video = store.home / "acceptance-video.mp4"
    final = store.home / "acceptance-film.mp4"
    subprocess.run(
        [
            str(ENGINE.path),
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            f"testsrc2=size=640x360:rate=10:duration={duration}",
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-crf",
            "35",
            "-pix_fmt",
            "yuv420p",
            str(video),
        ],
        check=True,
    )
    subprocess.run(
        [
            str(ENGINE.path),
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-i",
            str(video),
            "-i",
            str(mixed),
            "-map",
            "0:v:0",
            "-map",
            "1:a:0",
            "-c:v",
            "copy",
            "-c:a",
            "aac",
            "-b:a",
            "192k",
            "-ar",
            "48000",
            "-ac",
            "2",
            "-movflags",
            "+faststart",
            "-shortest",
            str(final),
        ],
        check=True,
    )
    probe = subprocess.run(
        [str(ENGINE.path), "-hide_banner", "-nostdin", "-i", str(final)],
        capture_output=True,
        check=False,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    assert probe.returncode != 0  # ffmpeg -i without an output is informational
    assert "Audio: aac" in probe.stderr and "48000 Hz, stereo" in probe.stderr
    assert "Video: h264" in probe.stderr
    assert final.stat().st_size > 0
    # The acceptance path is offline: only local assets and local FFmpeg were used.
    assert state["operations"]
    assert json.loads((store.home / "state.json").read_text(encoding="utf-8"))["jobs"] == {}
