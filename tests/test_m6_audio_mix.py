"""M6 E2 ``mix-zh-v1``: two-pass loudness, gains, ducking and failure isolation."""

import math
import struct
import wave
from fractions import Fraction
from pathlib import Path

import pytest

from vagent.errors import AppError
from vagent.video.assembler import bundled_engine
from vagent.video.mixer import (
    MixError,
    MixTrack,
    build_filter_graph,
    loudness_in_tolerance,
    mix_fingerprint,
    mix_tracks,
    parse_ebur128,
    parse_loudnorm,
)
from vagent.video.tts import inspect_wav, synthesize_local_wav


def required_engine():
    try:
        return bundled_engine()
    except AppError:
        return None


ENGINE = required_engine()
pytestmark = pytest.mark.skipif(ENGINE is None, reason="bundled FFmpeg is unavailable")


def write_voice(path: Path, text: str, voice: str = "local-narrator") -> Path:
    path.write_bytes(synthesize_local_wav(text, voice))
    return path


def write_silence(path: Path, seconds: float) -> Path:
    rate = 48000
    frames = b"\0\0" * int(rate * seconds)
    with path.open("wb") as raw, wave.open(raw, "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        handle.writeframes(frames)
    return path


def rms(path: Path, start: float, end: float) -> float:
    with wave.open(str(path), "rb") as handle:
        rate, channels, width = handle.getframerate(), handle.getnchannels(), handle.getsampwidth()
        assert (width, channels) == (2, 2)
        first, last = int(start * rate), int(end * rate)
        handle.setpos(first)
        raw = handle.readframes(last - first)
    values = struct.unpack("<" + "h" * (len(raw) // 2), raw)
    return math.sqrt(sum(value * value for value in values) / max(1, len(values)))


def track(path: Path, kind: str, start: float, gain: float, *, ducking=False, name=None) -> MixTrack:
    facts = inspect_wav(path.read_bytes())
    return MixTrack(
        audio_id=name or f"{kind}-1",
        audio_version=1,
        kind=kind,
        path=path,
        duration_seconds=facts.duration_seconds,
        start_seconds=Fraction(start).limit_denominator(1000),
        gain_db=gain,
        ducking=ducking,
    )


async def test_mix_produces_48k_stereo_and_freezes_both_loudness_passes(tmp_path):
    voice = write_voice(tmp_path / "voice.wav", "今天雨很大，你带伞了吗？")
    bgm = write_voice(
        tmp_path / "bgm.wav",
        "背景音乐背景音乐背景音乐背景音乐背景音乐背景音乐背景音乐背景音乐背景音乐背景音乐",
        "local-male",
    )
    sfx = write_voice(tmp_path / "sfx.wav", "推门", "local-narrator")
    output = tmp_path / "mixed.wav"
    tracks = [
        track(voice, "voice", 0.5, 0.0),
        track(bgm, "bgm", 0.0, -18.0, ducking=True),
        track(sfx, "sfx", 4.0, -6.0),
    ]
    result = await mix_tracks(tracks=tracks, output=output, duration_seconds=Fraction(8))
    assert output.is_file()
    assert (result.sample_rate, result.channels) == (48000, 2)
    assert result.duration_seconds == 8.0
    assert result.size_bytes == output.stat().st_size
    assert "input_i" in result.loudness_input
    assert "output_i" in result.loudness_output
    assert result.loudness_verification["method"] == "ebur128"
    assert abs(result.loudness_verification["integratedLufs"] - (-16.0)) <= 0.5
    assert result.loudness_verification["truePeakDb"] <= -1.0
    assert loudness_in_tolerance(result.loudness_verification)
    assert result.fingerprint == mix_fingerprint(tracks, Fraction(8), ENGINE)


def render_bus(tracks: list[MixTrack], graph: str, bus: str, output: Path) -> Path:
    command = [str(ENGINE.path), "-hide_banner", "-nostdin", "-loglevel", "error", "-nostats"]
    for item in tracks:
        command += ["-i", str(item.path)]
    command += [
        "-filter_complex",
        graph,
        "-map",
        f"[{bus}]",
        "-ar",
        "48000",
        "-ac",
        "2",
        "-c:a",
        "pcm_s16le",
        "-f",
        "wav",
        "-y",
        str(output),
    ]
    import subprocess

    subprocess.run(command, check=True)
    return output


def ducking_graph(ducking: bool) -> tuple[str, str]:
    voice = (
        "[0:a]aresample=48000,aformat=sample_fmts=fltp:channel_layouts=stereo,volume=0dB,"
        "adelay=delays=0:all=1,apad,atrim=duration=6,asetpts=PTS-STARTPTS[voice]"
    )
    bgm = (
        "[1:a]aresample=48000,aformat=sample_fmts=fltp:channel_layouts=stereo,volume=-18dB,"
        "adelay=delays=0:all=1,apad,atrim=duration=6,asetpts=PTS-STARTPTS[bgm]"
    )
    if not ducking:
        return f"{voice};{bgm};[voice]anullsink;[bgm]anull[out]", "out"
    return (
        f"{voice};{bgm};[voice]asplit=2[voiceout][key];[voiceout]anullsink;"
        "[bgm][key]sidechaincompress=threshold=0.1:ratio=8:attack=20:release=400:makeup=1[ducked];"
        "[ducked]anull[out]",
        "out",
    )


async def test_ducking_lowers_the_sidechain_compressed_bgm_bus(tmp_path):
    voice = write_voice(tmp_path / "voice.wav", "这一句会持续压住背景音乐好几秒钟时间呢", "local-female")
    bgm = write_voice(
        tmp_path / "bgm.wav",
        "背景音乐背景音乐背景音乐背景音乐背景音乐背景音乐背景音乐背景音乐背景音乐背景音乐",
        "local-male",
    )
    tracks = [track(voice, "voice", 0.0, 0.0), track(bgm, "bgm", 0.0, -18.0, ducking=True)]
    duck_graph, duck_bus = ducking_graph(True)
    plain_graph, plain_bus = ducking_graph(False)
    ducked = render_bus(tracks, duck_graph, duck_bus, tmp_path / "ducked-bus.wav")
    plain = render_bus(tracks, plain_graph, plain_bus, tmp_path / "plain-bus.wav")
    ducked_rms = rms(ducked, 0.5, 1.5)
    plain_rms = rms(plain, 0.5, 1.5)
    assert ducked_rms < plain_rms
    assert 20 * math.log10(plain_rms / ducked_rms) > 1.0


async def test_silent_mix_is_rejected_by_the_loudness_gate(tmp_path):
    silent = write_silence(tmp_path / "silent.wav", 3.0)
    output = tmp_path / "mixed.wav"
    tracks = [track(silent, "voice", 0.0, 0.0)]
    with pytest.raises(MixError) as error:
        await mix_tracks(tracks=tracks, output=output, duration_seconds=Fraction(3))
    assert error.value.code in {"LOUDNESS_OUT_OF_RANGE", "RENDER_ENCODE_FAILED", "RENDER_VALIDATION_FAILED"}
    assert not output.exists()


async def test_missing_file_and_empty_tracks_are_source_errors(tmp_path):
    silent = write_silence(tmp_path / "silent.wav", 1.0)
    with pytest.raises(MixError) as error:
        await mix_tracks(tracks=[], output=tmp_path / "x.wav", duration_seconds=Fraction(1))
    assert error.value.code == "RENDER_SOURCE_MISSING"
    missing = track(silent, "voice", 0.0, 0.0)
    missing = MixTrack(
        missing.audio_id,
        missing.audio_version,
        missing.kind,
        tmp_path / "missing.wav",
        missing.duration_seconds,
        missing.start_seconds,
        missing.gain_db,
    )
    with pytest.raises(MixError) as error:
        await mix_tracks(tracks=[missing], output=tmp_path / "y.wav", duration_seconds=Fraction(1))
    assert error.value.code == "RENDER_SOURCE_MISSING"


def test_filter_graph_keeps_the_frozen_gains_and_ducking_parameters(tmp_path):
    voice = write_voice(tmp_path / "voice.wav", "台词")
    bgm = write_voice(tmp_path / "bgm.wav", "背景音乐", "local-male")
    graph, premix = build_filter_graph(
        [track(voice, "voice", 0.5, 0.0), track(bgm, "bgm", 0.0, -18.0, ducking=True)],
        Fraction(4),
    )
    assert premix == "premix"
    assert "volume=0.0dB" in graph
    assert "volume=-18.0dB" in graph
    assert "sidechaincompress=threshold=0.100000:ratio=8.0:attack=20.0:release=400.0" in graph
    assert "amix=inputs=2:duration=longest:dropout_transition=0:normalize=0" in graph
    assert "adelay=delays=500:all=1" in graph
    assert "atrim=duration=4.000000000" in graph


def test_fingerprint_changes_with_gain_and_duration(tmp_path):
    voice = write_voice(tmp_path / "voice.wav", "台词")
    first = track(voice, "voice", 0.0, 0.0)
    louder = track(voice, "voice", 0.0, 3.0)
    assert mix_fingerprint([first], Fraction(3), ENGINE) != mix_fingerprint([louder], Fraction(3), ENGINE)
    assert mix_fingerprint([first], Fraction(3), ENGINE) != mix_fingerprint([first], Fraction(4), ENGINE)
    assert mix_fingerprint([first], Fraction(3), ENGINE) == mix_fingerprint([first], Fraction(3), ENGINE)


def test_loudnorm_and_ebur128_parsers_extract_the_frozen_keys():
    loudness = """[Parsed_loudnorm_0 @ 0000]
{
\t"input_i" : "-16.00",
\t"input_tp" : "-1.50",
\t"input_lra" : "7.00",
\t"input_thresh" : "-26.00",
\t"output_i" : "-16.00",
\t"output_tp" : "-1.50",
\t"output_lra" : "7.00",
\t"output_thresh" : "-26.00",
\t"normalization_type" : "linear",
\t"target_offset" : "0.00"
}"""
    parsed = parse_loudnorm(loudness)
    assert parsed["input_i"] == "-16.00" and parsed["normalization_type"] == "linear"
    ebu = """Summary:

  Integrated loudness:
    I:         -16.0 LUFS
    Threshold: -27.3 LUFS

  Loudness range:
    LRA:        7.0 LU

  True peak:
    Peak:      -1.5 dBFS
"""
    measured = parse_ebur128(ebu)
    assert measured["integratedLufs"] == -16.0
    assert measured["truePeakDb"] == -1.5
    assert measured["lra"] == 7.0
    assert loudness_in_tolerance(measured)
    assert not loudness_in_tolerance({**measured, "integratedLufs": -17.0})
    assert not loudness_in_tolerance({**measured, "truePeakDb": -0.5})


def test_loudnorm_second_pass_ignores_silence_sentinels():
    """A near-silent first pass reports -3.4e38 / -inf; FFmpeg rejects them."""
    from vagent.video.mixer import _loudnorm_stage

    silence = {
        "input_i": "-3.402823e+38",
        "input_tp": "-inf",
        "input_lra": "0.00",
        "input_thresh": "-70.00",
        "target_offset": "0.00",
    }
    stage = _loudnorm_stage(silence)
    assert "measured_I" not in stage and "linear=true" not in stage
    normal = {
        "input_i": "-16.00",
        "input_tp": "-1.50",
        "input_lra": "7.00",
        "input_thresh": "-26.00",
        "target_offset": "0.00",
    }
    assert "measured_I=-16.00" in _loudnorm_stage(normal)
    assert "linear=true" in _loudnorm_stage(normal)
