"""M6 E2 ``mix-zh-v1``: voice/BGM/SFX bus, side-chain ducking, two-pass loudness.

Pure local FFmpeg. The public entry point runs outside the Store lock, writes a
16-bit 48 kHz stereo PCM WAV and returns the exact measurements so a render
version can freeze them. Gain defaults are the M6 contract: voice 0 dB, BGM
-18 dB with ducking, SFX -6 dB; ``amix`` always uses ``normalize=0`` so those
gains are not silently divided by the input count. The two loudnorm passes are
measure-then-apply; a third read-only pass verifies the delivered file and
returns ``LOUDNESS_OUT_OF_RANGE`` instead of accepting a drifted mix. If a sparse
dialogue mix drifts at delivery, one dynamic retry plus an EBU-measured gain/limiter
correction is applied before the file is refused; the delivered measurement remains the
gate.
"""

import asyncio
import hashlib
import json
import os
import re
import shutil
import tempfile
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path

from vagent.errors import AppError
from vagent.video.assembler import Engine, bundled_engine, format_seconds
from vagent.video.tts import inspect_wav

MIX_VERSION = "mix-zh-v1"
TARGET_LUFS = -16.0
TARGET_TRUE_PEAK_DB = -1.0
TARGET_LRA = 11.0
LUFS_TOLERANCE = 0.5
DEFAULT_GAINS = {"voice": 0.0, "bgm": -18.0, "sfx": -6.0}
DUCKING_THRESHOLD_DB = -20.0
DUCKING_RATIO = 8.0
DUCKING_ATTACK_MS = 20.0
DUCKING_RELEASE_MS = 400.0
MIX_SAMPLE_RATE = 48000
MIX_CHANNELS = 2
RENDER_TIMEOUT_SECONDS = 120.0
RENDER_JOB_TIMEOUT_SECONDS = 600.0
MESSAGES = {
    "LOUDNESS_OUT_OF_RANGE": "混音响度或 true peak 超出容差，未交付。",
    "RENDER_SOURCE_MISSING": "缺少混音所需的音频文件或时间线。",
    "RENDER_TIMEOUT": "本地混音超时，未交付。",
    "RENDER_ENCODE_FAILED": "本地混音或编码失败，未交付。",
    "RENDER_VALIDATION_FAILED": "混音产物未通过结构、时长或响度校验。",
}
_JSON_BLOCK = re.compile(r"\{[^{}]*\"input_i\"[^{}]*\}", re.DOTALL)
_EBU_SUMMARY = re.compile(r"Summary:\s*(?P<body>.*)\Z", re.DOTALL)
_EBU_I = re.compile(r"Integrated loudness:\s*I:\s*(-?\d+(?:\.\d+)?) LUFS")
_EBU_LRA = re.compile(r"LRA:\s*(-?\d+(?:\.\d+)?) LU")
_EBU_TP = re.compile(r"True peak:\s*Peak:\s*(-?\d+(?:\.\d+)?) dBFS")


class MixError(AppError):
    def __init__(self, code: str, detail: str = "", *, stage: str = "mix"):
        message = MESSAGES[code]
        super().__init__(code, f"{message}{detail}" if detail else message)
        self.stage = stage


@dataclass(frozen=True)
class MixTrack:
    audio_id: str
    audio_version: int
    kind: str
    path: Path
    duration_seconds: float
    start_seconds: Fraction
    gain_db: float
    ducking: bool = False
    sha256: str = ""

    def __post_init__(self):
        if self.kind not in DEFAULT_GAINS:
            raise ValueError("Unknown mix track kind")
        if not -60.0 <= self.gain_db <= 12.0:
            raise ValueError("Mix gain must stay within -60..12 dB")
        if self.start_seconds < 0:
            raise ValueError("Mix start cannot be negative")


@dataclass(frozen=True)
class MixResult:
    sha256: str
    size_bytes: int
    duration_seconds: float
    sample_rate: int
    channels: int
    loudness_input: dict
    loudness_output: dict
    loudness_verification: dict
    fingerprint: str
    warning_count: int


def mix_fingerprint(tracks: list[MixTrack], duration_seconds: Fraction, engine: Engine) -> str:
    payload = {
        "mixVersion": MIX_VERSION,
        "duration": format_seconds(duration_seconds),
        "sampleRate": MIX_SAMPLE_RATE,
        "channels": MIX_CHANNELS,
        "targets": {
            "lufs": TARGET_LUFS,
            "truePeakDb": TARGET_TRUE_PEAK_DB,
            "lra": TARGET_LRA,
        },
        "ducking": {
            "thresholdDb": DUCKING_THRESHOLD_DB,
            "ratio": DUCKING_RATIO,
            "attackMs": DUCKING_ATTACK_MS,
            "releaseMs": DUCKING_RELEASE_MS,
        },
        "tracks": [
            {
                "audioId": track.audio_id,
                "audioVersion": track.audio_version,
                "kind": track.kind,
                "gainDb": round(track.gain_db, 4),
                "start": format_seconds(track.start_seconds),
                "ducking": track.ducking,
                "sha256": track.sha256,
            }
            for track in tracks
        ],
        "engine": {"version": engine.ffmpeg_version, "binarySha256": engine.binary_sha256},
    }
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _bus(labels: list[str], name: str, filters: list[str]) -> str:
    if not labels:
        return ""
    if len(labels) == 1:
        return labels[0]
    joined = "".join(f"[{label}]" for label in labels)
    filters.append(
        f"{joined}amix=inputs={len(labels)}:duration=longest:dropout_transition=0:normalize=0[{name}]"
    )
    return name


def build_filter_graph(tracks: list[MixTrack], duration_seconds: Fraction) -> tuple[str, str]:
    """Return (filter_complex without the loudnorm stage, pre-mix label)."""
    filters: list[str] = []
    labels: dict[str, list[str]] = {"voice": [], "bgm": [], "sfx": []}
    for index, track in enumerate(tracks):
        delay = max(0, int(round(float(track.start_seconds) * 1000)))
        chain = (
            f"[{index}:a]aresample={MIX_SAMPLE_RATE},"
            f"aformat=sample_fmts=fltp:channel_layouts=stereo,"
            f"volume={track.gain_db}dB,adelay=delays={delay}:all=1,apad,"
            f"atrim=duration={format_seconds(duration_seconds)},asetpts=PTS-STARTPTS[t{index}]"
        )
        filters.append(chain)
        labels[track.kind].append(f"t{index}")
    voice = _bus(labels["voice"], "voicebus", filters)
    bgm = _bus(labels["bgm"], "bgmbus", filters)
    sfx = _bus(labels["sfx"], "sfxbus", filters)
    ducking_needed = bool(voice and bgm and any(track.kind == "bgm" and track.ducking for track in tracks))
    if ducking_needed:
        filters.append(f"[{voice}]asplit=2[voiceout][duckkey]")
        filters.append(
            f"[{bgm}][duckkey]sidechaincompress="
            f"threshold={10 ** (DUCKING_THRESHOLD_DB / 20):.6f}:"
            f"ratio={DUCKING_RATIO}:attack={DUCKING_ATTACK_MS}:release={DUCKING_RELEASE_MS}:makeup=1[bgmduck]"
        )
        voice, bgm = "voiceout", "bgmduck"
    present = [label for label in (voice, bgm, sfx) if label]
    if not present:
        raise MixError("RENDER_SOURCE_MISSING", "（没有可混音的轨道）", stage="source")
    if len(present) == 1:
        filters.append(f"[{present[0]}]anull[premix]")
    else:
        joined = "".join(f"[{label}]" for label in present)
        filters.append(
            f"{joined}amix=inputs={len(present)}:duration=longest:dropout_transition=0:normalize=0,"
            f"apad,atrim=duration={format_seconds(duration_seconds)},asetpts=PTS-STARTPTS[premix]"
        )
    return ";".join(filters), "premix"


def parse_loudnorm(text: str) -> dict | None:
    blocks = _JSON_BLOCK.findall(text)
    if not blocks:
        return None
    try:
        value = json.loads(blocks[-1])
    except ValueError:
        return None
    return value if isinstance(value, dict) else None


def _number(value) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    if parsed != parsed or parsed in (float("inf"), float("-inf")):
        return None
    return parsed


def parse_ebur128(text: str) -> dict | None:
    """The delivered-file measurement: EBU R128 integrated loudness, LRA and true peak."""
    summary = _EBU_SUMMARY.search(text)
    body = summary.group("body") if summary else text
    integrated = _EBU_I.search(body)
    true_peak = _EBU_TP.search(body)
    if integrated is None or true_peak is None:
        return None
    lra = _EBU_LRA.search(body)
    return {
        "integratedLufs": float(integrated.group(1)),
        "truePeakDb": float(true_peak.group(1)),
        "lra": float(lra.group(1)) if lra else None,
        "method": "ebur128",
    }


def loudness_in_tolerance(measurement: dict) -> bool:
    if measurement.get("method") == "ebur128":
        lufs = _number(measurement.get("integratedLufs"))
        true_peak = _number(measurement.get("truePeakDb"))
    else:
        lufs = _number(measurement.get("input_i"))
        true_peak = _number(measurement.get("input_tp"))
    if lufs is None or true_peak is None:
        return False
    if abs(lufs - TARGET_LUFS) > LUFS_TOLERANCE:
        return False
    return true_peak <= TARGET_TRUE_PEAK_DB + 1e-6


def _measured_number(value, *, low: float, high: float) -> str | None:
    """A JSON measurement is usable only when it is finite and in a sane range.

    A near-silent first pass can report ``-inf``; FFmpeg then rejects the second
    pass with ``Error parsing global options: Result too large``. Falling back to the
    unmeasured dynamic stage is the correct behaviour for silence.
    """
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number != number or number in (float("inf"), float("-inf")):
        return None
    if number < low or number > high:
        return None
    return str(value)


def _loudnorm_stage(measured: dict | None, *, linear: bool = True) -> str:
    stage = f"loudnorm=I={TARGET_LUFS:g}:TP={TARGET_TRUE_PEAK_DB:g}:LRA={TARGET_LRA:g}"
    if measured is not None:
        fields = {
            "measured_I": _measured_number(measured.get("input_i"), low=-100.0, high=0.0),
            "measured_TP": _measured_number(measured.get("input_tp"), low=-100.0, high=20.0),
            "measured_LRA": _measured_number(measured.get("input_lra"), low=0.0, high=100.0),
            "measured_thresh": _measured_number(measured.get("input_thresh"), low=-120.0, high=0.0),
            "offset": _measured_number(measured.get("target_offset"), low=-100.0, high=100.0) or "0",
        }
        if all(value is not None for value in fields.values()):
            stage += (
                f":measured_I={fields['measured_I']}:measured_TP={fields['measured_TP']}"
                f":measured_LRA={fields['measured_LRA']}:measured_thresh={fields['measured_thresh']}"
                f":offset={fields['offset']}"
            )
            if linear:
                stage += ":linear=true"
    return stage + ":print_format=json"


async def mix_tracks(
    *,
    tracks: list[MixTrack],
    output: Path,
    duration_seconds: Fraction,
    engine: Engine | None = None,
    work_root: Path | None = None,
    cancelled=None,
) -> MixResult:
    if duration_seconds <= 0:
        raise MixError("RENDER_SOURCE_MISSING", "（时间线为空）", stage="source")
    engine = engine or bundled_engine()
    missing = [track.path for track in tracks if not track.path.is_file()]
    if missing:
        raise MixError("RENDER_SOURCE_MISSING", "（音频文件缺失）", stage="source")
    work = (
        Path(tempfile.mkdtemp(prefix="mix-", dir=work_root))
        if work_root
        else Path(tempfile.mkdtemp(prefix="mix-"))
    )
    deadline = asyncio.get_running_loop().time() + RENDER_JOB_TIMEOUT_SECONDS
    try:
        graph, premix = build_filter_graph(tracks, duration_seconds)
        first = await _run(
            engine,
            tracks,
            f"{graph};[{premix}]{_loudnorm_stage(None)}[out]",
            None,
            cancelled=cancelled,
            deadline=deadline,
        )
        measured = parse_loudnorm(first)
        if measured is None or not all(
            key in measured for key in ("input_i", "input_tp", "input_lra", "input_thresh")
        ):
            raise MixError("RENDER_VALIDATION_FAILED", "（读不到响度测量）", stage="validate")
        second = await _run(
            engine,
            tracks,
            f"{graph};[{premix}]{_loudnorm_stage(measured)}[out]",
            output,
            cancelled=cancelled,
            deadline=deadline,
        )
        try:
            applied = parse_loudnorm(second) or measured
            verification_text = await _run(
                engine,
                [],
                None,
                None,
                cancelled=cancelled,
                deadline=deadline,
                input_path=output,
                verify=True,
            )
            verification = parse_ebur128(verification_text)
            if verification is None or not loudness_in_tolerance(verification):
                # Sparse dialogue can make the linear second pass drift at delivery.
                # Retry with dynamic normalization (and a sample-peak limiter) before
                # refusing the file; the delivered measurement remains the gate.
                retry = await _run(
                    engine,
                    tracks,
                    f"{graph};[{premix}]{_loudnorm_stage(None, linear=False)},"
                    f"alimiter=limit=0.85:level=0[out]",
                    output,
                    cancelled=cancelled,
                    deadline=deadline,
                )
                applied = parse_loudnorm(retry) or applied
                verification_text = await _run(
                    engine,
                    [],
                    None,
                    None,
                    cancelled=cancelled,
                    deadline=deadline,
                    input_path=output,
                    verify=True,
                )
                verification = parse_ebur128(verification_text)
            for _attempt in range(2):
                if verification is not None and loudness_in_tolerance(verification):
                    break
                if verification is None:
                    break
                correction = TARGET_LUFS - float(verification["integratedLufs"])
                fixed = output.with_name("fixed.wav")
                await _transform(
                    engine,
                    output,
                    fixed,
                    f"volume={correction:.3f}dB,alimiter=limit=0.80:level=0",
                    deadline=deadline,
                    cancelled=cancelled,
                )
                os.replace(fixed, output)
                verification_text = await _run(
                    engine,
                    [],
                    None,
                    None,
                    cancelled=cancelled,
                    deadline=deadline,
                    input_path=output,
                    verify=True,
                )
                verification = parse_ebur128(verification_text)
            if verification is None or not loudness_in_tolerance(verification):
                raise MixError("LOUDNESS_OUT_OF_RANGE", f"（{verification}）", stage="validate")
            facts = inspect_wav(output.read_bytes())
            expected_samples = round(float(duration_seconds) * MIX_SAMPLE_RATE)
            actual_samples = round(facts.duration_seconds * MIX_SAMPLE_RATE)
            if abs(actual_samples - expected_samples) > MIX_SAMPLE_RATE // 30:
                raise MixError("RENDER_VALIDATION_FAILED", "（时长与时间线不一致）", stage="validate")
            size = facts.size_bytes
            digest = hashlib.sha256(output.read_bytes()).hexdigest()
            return MixResult(
                sha256=digest,
                size_bytes=size,
                duration_seconds=facts.duration_seconds,
                sample_rate=facts.sample_rate,
                channels=facts.channels,
                loudness_input=measured,
                loudness_output=applied,
                loudness_verification=verification,
                fingerprint=mix_fingerprint(tracks, duration_seconds, engine),
                warning_count=0,
            )
        except BaseException:
            output.unlink(missing_ok=True)
            raise
    finally:
        shutil.rmtree(work, ignore_errors=True)


async def _run(
    engine: Engine,
    tracks: list[MixTrack],
    graph: str | None,
    output: Path | None,
    *,
    cancelled,
    deadline: float,
    input_path: Path | None = None,
    verify: bool = False,
) -> str:
    if verify and input_path is not None:
        command = [
            str(engine.path),
            "-hide_banner",
            "-nostdin",
            "-loglevel",
            "info",
            "-nostats",
            "-i",
            str(input_path),
            "-af",
            "ebur128=peak=true",
            "-f",
            "null",
            "-",
        ]
    else:
        command = [
            str(engine.path),
            "-hide_banner",
            "-nostdin",
            "-loglevel",
            "info",
            "-nostats",
        ]
        for track in tracks:
            command += ["-i", str(track.path)]
        command += ["-filter_complex", graph or "", "-map", "[out]"]
        if output is None:
            command += ["-f", "null", "-"]
        else:
            command += [
                "-ar",
                str(MIX_SAMPLE_RATE),
                "-ac",
                str(MIX_CHANNELS),
                "-c:a",
                "pcm_s16le",
                "-f",
                "wav",
                "-y",
                str(output),
            ]
    try:
        process = await asyncio.create_subprocess_exec(
            *command,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except OSError:
        raise MixError("RENDER_ENCODE_FAILED", "（无法启动本地编码器）", stage="encode") from None
    loop = asyncio.get_running_loop()
    limit = min(deadline, loop.time() + RENDER_TIMEOUT_SECONDS)
    watcher = asyncio.ensure_future(_watch(process, cancelled, limit))
    try:
        _, stderr = await process.communicate()
    except asyncio.CancelledError:
        await _terminate(process)
        raise
    finally:
        watcher.cancel()
        await asyncio.gather(watcher, return_exceptions=True)
    text = (stderr or b"").decode("utf-8", "replace")
    if cancelled is not None and cancelled.is_set():
        raise MixError("RENDER_ENCODE_FAILED", "（混音已停止）", stage="encode")
    if loop.time() >= limit:
        raise MixError("RENDER_TIMEOUT", "", stage="encode")
    if process.returncode != 0:
        raise MixError("RENDER_ENCODE_FAILED", f"（{_first_error(text)}）", stage="encode")
    return text


async def _transform(
    engine: Engine,
    input_path: Path,
    output: Path,
    filter_text: str,
    *,
    deadline: float,
    cancelled,
) -> str:
    """Apply one corrective audio filter to an already mixed file."""
    command = [
        str(engine.path),
        "-hide_banner",
        "-nostdin",
        "-loglevel",
        "info",
        "-nostats",
        "-i",
        str(input_path),
        "-af",
        filter_text,
        "-ar",
        str(MIX_SAMPLE_RATE),
        "-ac",
        str(MIX_CHANNELS),
        "-c:a",
        "pcm_s16le",
        "-f",
        "wav",
        "-y",
        str(output),
    ]
    try:
        process = await asyncio.create_subprocess_exec(
            *command,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except OSError:
        raise MixError("RENDER_ENCODE_FAILED", "（无法启动本地编码器）", stage="encode") from None
    loop = asyncio.get_running_loop()
    limit = min(deadline, loop.time() + RENDER_TIMEOUT_SECONDS)
    watcher = asyncio.ensure_future(_watch(process, cancelled, limit))
    try:
        _, stderr = await process.communicate()
    except asyncio.CancelledError:
        await _terminate(process)
        raise
    finally:
        watcher.cancel()
        await asyncio.gather(watcher, return_exceptions=True)
    text = (stderr or b"").decode("utf-8", "replace")
    if loop.time() >= limit:
        raise MixError("RENDER_TIMEOUT", "", stage="encode")
    if process.returncode != 0:
        raise MixError("RENDER_ENCODE_FAILED", f"（{_first_error(text)}）", stage="encode")
    return text


async def _watch(process, cancelled, deadline: float) -> None:
    loop = asyncio.get_running_loop()
    while process.returncode is None:
        if (cancelled is not None and cancelled.is_set()) or loop.time() >= deadline:
            await _terminate(process)
            return
        await asyncio.sleep(0.05)


async def _terminate(process) -> None:
    if process.returncode is None:
        try:
            process.kill()
        except ProcessLookupError:
            return
    try:
        await asyncio.wait_for(process.wait(), timeout=5)
    except TimeoutError:
        pass


def _first_error(text: str) -> str:
    lines = [line.strip() for line in text.splitlines() if line.strip() and not line.strip().startswith("{")]
    for line in reversed(lines):
        lowered = line.lower()
        if any(
            marker in lowered
            for marker in ("error", "invalid", "failed", "unable", "no such", "cannot", "unrecognized")
        ):
            return line[:300]
    return lines[-1][:300] if lines else "本地编码失败"


def default_gain(kind: str) -> float:
    return DEFAULT_GAINS[kind]
