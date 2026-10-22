"""E1: the frozen mp4-copy-signature-v1 parser and the local concat assembler."""

import asyncio
import os
import subprocess
import sys
from dataclasses import replace
from fractions import Fraction
from pathlib import Path

import pytest

import vagent.video.assembler as assembler_module
from vagent.video.assembler import (
    NORMALIZATION_PROFILE,
    AssemblyError,
    _command,
    assemble,
    assemble_normalized,
    bundled_engine,
    format_seconds,
    plan_assembly,
    plan_normalization,
)
from vagent.video.copy_signature import (
    SignatureError,
    _aac_config,
    compatibility,
    read_copy_signature,
)

FIXTURE = Path(__file__).parent / "fixtures" / "m1c" / "sample-720p.mp4"
FIXTURE_SHA256 = "b179cca0ebb91e2b49a1e212df743037a8e292a3b754ee040c2124b31a3b6a54"
# E0 measured this exact concatenation with the pinned win_amd64 wheel.
E0_TWO_CLIP_SHA256 = "41be1ed90fa7abfcdb3b3d46775d96695ca3e441f92adb15363be400ec21fd8a"
E0_TWO_CLIP_SIZE = 418395
WINDOWS_FFMPEG_SHA256 = "2ce797a0f88d7f067180338fb227f7b1928ea727bd9a4d7a1d022f7c52af71a3"


def signature(path: Path = FIXTURE):
    with path.open("rb") as handle:
        return read_copy_signature(handle, os.path.getsize(path))


def test_fixture_signature_records_samples_edits_and_priming():
    parsed = signature()
    assert parsed.version == "mp4-copy-signature-v1"
    video, audio = parsed.video, parsed.audio
    assert (video.timescale, video.samples, video.runs) == (10240, 50, ((50, 1024),))
    assert (video.media_time, video.composition_offsets, video.sync_samples) == (2048, "ctts-v0", 1)
    assert video.frame_seconds == Fraction(1, 10) and video.priming_seconds == Fraction(1, 5)
    assert (audio.timescale, audio.samples, audio.runs) == (48000, 236, ((235, 1024), (1, 384)))
    assert (audio.media_time, audio.composition_offsets, audio.sync_samples) == (1024, "none", 236)
    assert parsed.duration_seconds == Fraction(1883, 375)  # The audio track is longest.
    assert parsed.max_frame_seconds == Fraction(1, 10)
    assert parsed.config["video"]["avcC"].startswith("0164001f")
    assert parsed.config["audio"]["sampleRate"] == 48000


def test_signature_hashes_are_stable_and_cover_both_timeline_and_config():
    parsed = signature()
    assert parsed.sha256() == parsed.sha256()
    assert parsed.config_sha256() != parsed.sha256()
    other = replace(
        parsed, timeline={**parsed.timeline, "video": {**parsed.timeline["video"], "samples": 49}}
    )
    assert other.sha256() != parsed.sha256()


def test_compatible_inputs_share_one_joint_fingerprint():
    parsed = signature()
    joint, digest = compatibility([parsed, parsed])
    assert joint["configs"] == [parsed.config_sha256(), parsed.config_sha256()]
    assert digest == compatibility([signature(), signature()])[1]


def test_compatibility_explains_the_first_encoding_difference():
    parsed = signature()
    changed = replace(parsed, config={**parsed.config, "video": {**parsed.config["video"], "avcC": "00"}})
    with pytest.raises(SignatureError) as error:
        compatibility([parsed, changed])
    assert "video.avcC" in str(error.value)


def _esds(max_bitrate, avg_bitrate, audio_specific=bytes.fromhex("121056e500")):
    """One ES descriptor whose decoder config carries the given bitrate metadata."""
    decoder = (
        bytes([0x40, 0x15])
        + b"\x00\x00\x00"
        + max_bitrate.to_bytes(4, "big")
        + avg_bitrate.to_bytes(4, "big")
        + bytes([0x05, len(audio_specific)])
        + audio_specific
        + bytes([0x06, 0x01, 0x02])
    )
    body = b"\x00\x00\x00" + bytes([0x04, len(decoder)]) + decoder
    return b"\x00\x00\x00\x00" + bytes([0x03, len(body)]) + body


def test_audio_bitrate_metadata_is_not_part_of_the_copy_configuration():
    """Providers vary the esds max/avg bitrate; the AAC decoder config is unchanged."""
    first = _aac_config(_esds(128_043, 128_043))
    second = _aac_config(_esds(127_719, 127_719))
    assert first == second
    assert first == {
        "objectTypeIndication": 0x40,
        "streamType": 0x15,
        "audioSpecificConfig": "121056e500",
    }
    changed = _aac_config(_esds(128_043, 128_043, audio_specific=bytes.fromhex("118856e500")))
    assert changed["audioSpecificConfig"] != first["audioSpecificConfig"]

    parsed = signature()
    compatible = replace(
        parsed,
        config={**parsed.config, "audio": {**parsed.config["audio"], "esds": first}},
    )
    assert compatibility([compatible, compatible])[0]["configs"] == [
        compatible.config_sha256(),
        compatible.config_sha256(),
    ]
    incompatible = replace(
        parsed,
        config={**parsed.config, "audio": {**parsed.config["audio"], "esds": changed}},
    )
    with pytest.raises(SignatureError) as error:
        compatibility([compatible, incompatible])
    assert "audio.esds" in str(error.value)


def test_unknown_esds_structure_is_refused():
    with pytest.raises(SignatureError):
        _aac_config(b"\x00\x00\x00\x00\x03\x00")
    with pytest.raises(SignatureError):
        _aac_config(b"\x00\x00\x00\x00" + bytes([0x03, 0x03, 0, 0, 0]))


def test_unknown_top_level_box_is_refused(tmp_path):
    crafted = tmp_path / "unknown.mp4"
    payload = FIXTURE.read_bytes()
    crafted.write_bytes(payload + (16).to_bytes(4, "big") + b"uuid" + bytes(8))
    with crafted.open("rb") as handle:
        with pytest.raises(SignatureError):
            read_copy_signature(handle, os.path.getsize(crafted))


def test_fixture_edit_list_is_a_single_real_edit(tmp_path):
    """The committed fixture carries the explained one-edit form, not an empty edit."""
    payload = bytearray(FIXTURE.read_bytes())
    marker = payload.find(b"elst")
    assert marker > 0
    count_offset = marker + 4 + 4
    assert int.from_bytes(payload[count_offset : count_offset + 4], "big") == 1
    edited = tmp_path / "copy.mp4"
    edited.write_bytes(bytes(payload))
    with edited.open("rb") as handle:
        parsed = read_copy_signature(handle, os.path.getsize(edited))
    assert [edit.media_time for edit in parsed.video.edits] == [2048]


def test_plan_records_rational_offsets_and_expectations():
    parsed = signature()
    plan = plan_assembly([parsed, parsed, parsed])
    assert [entry["start"] for entry in plan.timeline] == [
        Fraction(0),
        Fraction(1883, 375),
        Fraction(3766, 375),
    ]
    assert [entry["duration"] for entry in plan.timeline] == [Fraction(1883, 375)] * 3
    assert plan.expected_video_seconds == Fraction(5641, 375)
    assert plan.expected_audio_seconds == Fraction(5649, 375)
    assert plan.compatibility_sha256 == compatibility([parsed, parsed, parsed])[1]


def test_plan_rejects_variable_frame_duration_and_audio_mismatch():
    parsed = signature()
    variable = replace(parsed, video=replace(parsed.video, runs=((49, 1024), (1, 1024))))
    with pytest.raises(AssemblyError) as error:
        plan_assembly([parsed, variable])
    assert error.value.code in {"FILM_INPUT_INCOMPATIBLE", "FILM_ASSEMBLY_FAILED"}
    silent = replace(parsed, audio=None)
    with pytest.raises(AssemblyError) as error:
        plan_assembly([parsed, silent])
    assert error.value.code == "FILM_INPUT_INCOMPATIBLE"


def test_plan_rejects_input_whose_audio_timeline_drifts_beyond_one_frame():
    parsed = signature()
    runs = ((235, 1024), (1, 384), (50, 1024))
    drifted = replace(
        parsed,
        audio=replace(parsed.audio, runs=runs, duration_ticks=sum(count * delta for count, delta in runs)),
    )
    assert drifted.duration_seconds - drifted.video.raw_end > drifted.max_frame_seconds
    with pytest.raises(AssemblyError) as error:
        plan_assembly([drifted])
    assert error.value.code == "FILM_INPUT_INCOMPATIBLE"


def test_normalization_plan_accepts_timeline_and_audio_differences():
    parsed = signature()
    silent = replace(parsed, audio=None)
    plan = plan_normalization([parsed, silent])
    assert plan.mode == "normalize"
    assert plan.assembly_version == "ffmpeg-concat-normalize-v4"
    assert plan.profile == NORMALIZATION_PROFILE
    assert plan.normalization["width"] == 1280
    assert plan.normalization["height"] == 720
    assert plan.expected_presentation_seconds == Fraction(10)
    assert plan.expected_audio_seconds is not None
    assert plan.compatibility_sha256 != compatibility([parsed, parsed])[1]


def test_normalized_assembly_adds_silent_audio(engine, tmp_path):
    silent = tmp_path / "silent.mp4"
    subprocess.run(
        [
            str(engine.path),
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-i",
            str(FIXTURE),
            "-an",
            "-c:v",
            "copy",
            "-movflags",
            "+faststart",
            str(silent),
        ],
        check=True,
    )
    output = tmp_path / "film.mp4.part"
    film = asyncio.run(
        assemble_normalized(
            engine=engine,
            inputs=[FIXTURE, silent],
            work_root=tmp_path,
            output=output,
        )
    )
    assert film.plan.mode == "normalize"
    assert film.metadata.width == 1280 and film.metadata.height == 720
    assert film.metadata.has_audio and film.metadata.audio_codec == "aac"
    assert abs(film.metadata.duration_seconds - float(film.plan.expected_presentation_seconds)) < 0.5
    assert not list(tmp_path.glob("normalize-*"))


def test_normalized_assembly_scales_and_reframes(engine, tmp_path):
    small = tmp_path / "small.mp4"
    subprocess.run(
        [
            str(engine.path),
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-i",
            str(FIXTURE),
            "-vf",
            "scale=640:360,fps=15",
            "-an",
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-crf",
            "20",
            "-pix_fmt",
            "yuv420p",
            "-movflags",
            "+faststart",
            str(small),
        ],
        check=True,
    )
    output = tmp_path / "film.mp4.part"
    film = asyncio.run(
        assemble_normalized(
            engine=engine,
            inputs=[FIXTURE, small],
            work_root=tmp_path,
            output=output,
        )
    )
    assert (film.metadata.width, film.metadata.height) == (1280, 720)
    assert (film.metadata.frame_rate.numerator, film.metadata.frame_rate.denominator) == (30, 1)
    assert film.metadata.has_audio and film.metadata.audio_codec == "aac"
    assert not list(tmp_path.glob("normalize-*"))


def test_copy_assembler_still_rejects_audio_mismatch(engine, tmp_path):
    silent = tmp_path / "silent.mp4"
    subprocess.run(
        [
            str(engine.path),
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-i",
            str(FIXTURE),
            "-an",
            "-c:v",
            "copy",
            "-movflags",
            "+faststart",
            str(silent),
        ],
        check=True,
    )
    with pytest.raises(AssemblyError) as error:
        asyncio.run(
            assemble(
                engine=engine,
                inputs=[FIXTURE, silent],
                work_root=tmp_path,
                output=tmp_path / "film.mp4.part",
            )
        )
    assert error.value.code == "FILM_INPUT_INCOMPATIBLE"


def test_concat_seconds_use_exact_rational_arithmetic():
    assert format_seconds(Fraction(1883, 375)) == "5.021333333"
    assert format_seconds(Fraction(10)) == "10.000000000"


def test_ffmpeg_command_is_frozen():
    command = _command(Path("ffmpeg"), Path("list.ffconcat"), Path("out.mp4"))
    assert command[1:4] == ["-hide_banner", "-nostdin", "-loglevel"]
    assert "-auto_convert" in command and command[command.index("-auto_convert") + 1] == "0"
    assert command[command.index("-protocol_whitelist") + 1] == "file"
    assert command[command.index("-c") + 1] == "copy"
    assert command[command.index("-movflags") + 1] == "+faststart"
    assert "-y" in command and command[-1] == "out.mp4"


def test_missing_packaged_binary_reports_assembler_unavailable(monkeypatch):
    import imageio_ffmpeg

    def missing():
        raise RuntimeError("no binary")

    monkeypatch.setattr(imageio_ffmpeg, "get_ffmpeg_exe", missing)
    with pytest.raises(AssemblyError) as error:
        bundled_engine()
    assert error.value.code == "ASSEMBLER_UNAVAILABLE"


@pytest.fixture()
def engine():
    try:
        return bundled_engine()
    except AssemblyError:
        pytest.skip("bundled FFmpeg is unavailable on this platform")


def test_assembler_runs_a_real_stream_copy(engine, tmp_path):
    output = tmp_path / "film.mp4.part"
    film = asyncio.run(assemble(engine=engine, inputs=[FIXTURE, FIXTURE], work_root=tmp_path, output=output))
    assert film.size_bytes == os.path.getsize(output) > 0
    assert film.sha256 == film.sha256.lower() and len(film.sha256) == 64
    assert (film.metadata.width, film.metadata.height) == (1280, 720)
    assert film.signature.video.samples == 100
    assert film.signature.audio.samples == 472
    assert abs(film.metadata.duration_seconds - 10.0212890625) < 0.000001
    assert film.plan.compatibility_sha256 == compatibility([signature(), signature()])[1]
    if sys.platform == "win32" and engine.binary_sha256 == WINDOWS_FFMPEG_SHA256:
        # Byte-for-byte the E0 "fixture-corrected" variant, now derived from samples.
        assert (film.size_bytes, film.sha256) == (E0_TWO_CLIP_SIZE, E0_TWO_CLIP_SHA256)
    assert not list(tmp_path.glob("assemble-*"))


def test_assembler_keeps_the_input_fixture_untouched(engine, tmp_path):
    before = FIXTURE.read_bytes()
    output = tmp_path / "film.mp4.part"
    asyncio.run(assemble(engine=engine, inputs=[FIXTURE], work_root=tmp_path, output=output))
    assert FIXTURE.read_bytes() == before
    assert output.exists()


def test_cancelled_assembly_terminates_and_leaves_no_output(engine, tmp_path):
    cancelled = asyncio.Event()
    cancelled.set()

    async def scenario():
        with pytest.raises(AssemblyError) as error:
            await assemble(
                engine=engine,
                inputs=[FIXTURE, FIXTURE],
                work_root=tmp_path,
                output=tmp_path / "film.mp4.part",
                cancelled=cancelled,
            )
        return error.value

    error = asyncio.run(scenario())
    assert error.code == "FILM_ASSEMBLY_FAILED"
    assert not (tmp_path / "film.mp4.part").exists()
    assert not list(tmp_path.glob("assemble-*"))


def test_assembler_refuses_inputs_that_are_not_copy_compatible(engine, tmp_path, monkeypatch):
    original = assembler_module.read_copy_signature
    calls = []

    def only_first(handle, size):
        parsed = original(handle, size)
        calls.append(parsed)
        if len(calls) == 2:
            return replace(
                parsed, config={**parsed.config, "video": {**parsed.config["video"], "height": 1080}}
            )
        return parsed

    monkeypatch.setattr(assembler_module, "read_copy_signature", only_first)
    with pytest.raises(AssemblyError) as error:
        asyncio.run(
            assemble(
                engine=engine,
                inputs=[FIXTURE, FIXTURE],
                work_root=tmp_path,
                output=tmp_path / "film.mp4.part",
            )
        )
    assert error.value.code == "FILM_INPUT_INCOMPATIBLE"


def test_input_fixture_hash_is_still_the_committed_media():
    import hashlib

    assert hashlib.sha256(FIXTURE.read_bytes()).hexdigest() == FIXTURE_SHA256
