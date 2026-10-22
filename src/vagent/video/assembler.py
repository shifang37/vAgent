"""Local film assembly: bundled FFmpeg, concat demuxer, copy first, transcode fallback.

The executable is only ever the one shipped inside the ``imageio-ffmpeg`` install:
no PATH lookup, no environment-variable override, no runtime download. Inputs are
hard-linked (or copied) into a private work directory so the concat list never
contains a user path, and every input is re-verified before the process starts.

The default path is the frozen stream-copy concatenation. When its strict copy
signature cannot splice the inputs, ``assemble_normalized`` transcodes every input
to one canonical profile and concatenates the normalized streams with a single
filter graph, so timeline/priming, frame-rate, resolution and missing-audio
differences do not leak into the delivered film.

Assembly is a cancellable local operation. The caller runs it outside the Store
lock and commits the resulting file, FilmVersion and FilmMedia in one transaction.
"""

import asyncio
import errno
import hashlib
import json
import os
import shutil
import stat
import subprocess
import tempfile
from dataclasses import dataclass, replace
from decimal import Decimal
from fractions import Fraction
from pathlib import Path

from vagent.errors import AppError
from vagent.video.contracts import MediaMetadata
from vagent.video.copy_signature import (
    CopySignature,
    SignatureError,
    compatibility,
    read_copy_signature,
)
from vagent.video.mp4 import inspect_mp4

ASSEMBLY_VERSION = "ffmpeg-concat-copy-v1"
NORMALIZATION_VERSION = "ffmpeg-concat-normalize-v4"
NORMALIZATION_PROFILE = "h264-1280x720-30fps-aac-48k-stereo-v1"
TARGET_WIDTH = 1280
TARGET_HEIGHT = 720
TARGET_FPS = 30
TARGET_AUDIO_RATE = 48000
TARGET_AUDIO_CHANNELS = 2
MAX_INPUTS = 64
MAX_INPUT_BYTES = 268435456
MAX_TOTAL_INPUT_BYTES = 2 * 1024**3
MAX_OUTPUT_BYTES = 2 * 1024**3
ASSEMBLY_TIMEOUT_SECONDS = 120.0
PACKAGE_VERSION = "0.6.0"
WARNING_MARKERS = (
    "non-monotonic dts",
    "non monotonically increasing",
    "packet corrupt",
    "invalid dropping",
    "truncating packet",
    "application provided invalid",
    "dts out of order",
)
MESSAGES = {
    "ASSEMBLER_UNAVAILABLE": "本地拼接组件不可用，请重新安装完整程序。",
    "FILM_ASSEMBLY_FAILED": "本地拼接失败，未交付成片。",
    "FILM_INPUT_INCOMPATIBLE": "镜头文件的编码或时间线不能直接拼接。",
}


class AssemblyError(AppError):
    def __init__(self, code, detail=None):
        super().__init__(code, MESSAGES[code] if detail is None else f"{MESSAGES[code]}{detail}")


@dataclass(frozen=True)
class Engine:
    package_version: str
    ffmpeg_version: str
    binary_sha256: str
    path: Path

    def record(self) -> dict:
        return {
            "packageVersion": self.package_version,
            "ffmpegVersion": self.ffmpeg_version,
            "binarySha256": self.binary_sha256,
        }


@dataclass(frozen=True)
class AssemblyPlan:
    signatures: tuple[CopySignature, ...]
    timeline: tuple[dict, ...]
    compatibility: dict
    compatibility_sha256: str
    expected_video_seconds: Fraction
    expected_audio_seconds: Fraction | None
    max_frame_seconds: Fraction
    expected_presentation_seconds: Fraction
    mode: str = "copy"
    assembly_version: str = ASSEMBLY_VERSION
    profile: str | None = None
    normalization: dict | None = None


@dataclass(frozen=True)
class AssembledFilm:
    path: Path
    size_bytes: int
    sha256: str
    metadata: MediaMetadata
    signature: CopySignature
    plan: AssemblyPlan


def _is_reparse(info: os.stat_result) -> bool:
    return bool(getattr(info, "st_file_attributes", 0) & 0x400)


def bundled_engine() -> Engine:
    """Locate and fingerprint the packaged FFmpeg binary, or report it unavailable."""
    try:
        import imageio_ffmpeg

        binary = Path(imageio_ffmpeg.get_ffmpeg_exe())
        package_dir = Path(imageio_ffmpeg.__file__).resolve().parent
        package_version = imageio_ffmpeg.__version__
    except Exception:
        raise AssemblyError("ASSEMBLER_UNAVAILABLE") from None
    try:
        resolved = binary.resolve(strict=True)
    except OSError:
        raise AssemblyError("ASSEMBLER_UNAVAILABLE") from None
    binaries = package_dir / "binaries"
    if binaries not in resolved.parents or resolved.parent != binaries.resolve():
        # Only a regular file directly inside the packaged binaries directory.
        raise AssemblyError("ASSEMBLER_UNAVAILABLE")
    info = resolved.stat()
    if stat.S_ISLNK(info.st_mode) or _is_reparse(info) or not stat.S_ISREG(info.st_mode):
        raise AssemblyError("ASSEMBLER_UNAVAILABLE")
    digest = hashlib.sha256()
    with resolved.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    version = _ffmpeg_version(resolved)
    return Engine(
        package_version=str(package_version),
        ffmpeg_version=version,
        binary_sha256=digest.hexdigest(),
        path=resolved,
    )


def _ffmpeg_version(binary: Path) -> str:
    result = subprocess.run(
        [str(binary), "-version"],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        timeout=10,
        check=False,
    )
    if result.returncode != 0 or not result.stdout:
        raise AssemblyError("ASSEMBLER_UNAVAILABLE")
    first = result.stdout.decode("utf-8", "replace").splitlines()[0].strip()
    if not first.lower().startswith("ffmpeg version"):
        raise AssemblyError("ASSEMBLER_UNAVAILABLE")
    return first


def format_seconds(value: Fraction) -> str:
    """Nine decimal places are enough for any 90 kHz-plus timestamp we accept."""
    scaled = (Decimal(value.numerator) / Decimal(value.denominator)).quantize(Decimal("0.000000001"))
    return format(scaled, "f")


def plan_assembly(signatures) -> AssemblyPlan:
    """Freeze per-input durations and offsets from the samples and edit lists."""
    signatures = tuple(signatures)
    if not signatures:
        raise AssemblyError("FILM_INPUT_INCOMPATIBLE")
    if len(signatures) > MAX_INPUTS:
        raise AssemblyError("FILM_INPUT_INCOMPATIBLE", "镜头数量超过上限。")
    try:
        joint, joint_sha = compatibility(signatures)
    except SignatureError as error:
        raise AssemblyError("FILM_INPUT_INCOMPATIBLE", str(error)) from None
    first = signatures[0]
    for index, signature in enumerate(signatures, start=1):
        if signature.has_audio != first.has_audio:
            raise AssemblyError("FILM_INPUT_INCOMPATIBLE", f"输入 {index} 的音轨与其他镜头不同。")
        if signature.video.frame_ticks is None:
            raise AssemblyError("FILM_INPUT_INCOMPATIBLE", f"输入 {index} 不是恒定帧时长。")
        if signature.video.frame_ticks != first.video.frame_ticks:
            raise AssemblyError("FILM_INPUT_INCOMPATIBLE", f"输入 {index} 的帧率与其他镜头不同。")
        if signature.audio is not None:
            gap = signature.duration_seconds - signature.video.raw_end
            if gap > signature.max_frame_seconds:
                raise AssemblyError("FILM_INPUT_INCOMPATIBLE", f"输入 {index} 的音视频时间线相差超过一帧。")
    timeline, offset, video_total, audio_total = [], Fraction(0), Fraction(0), Fraction(0)
    for signature in signatures:
        duration = signature.duration_seconds
        timeline.append(
            {
                "start": offset,
                "duration": duration,
                "signature": signature,
            }
        )
        offset += duration
        video_total += signature.video.raw_end
        if signature.audio is not None:
            audio_total += signature.audio.raw_end
    # The concat offsets are cumulative segment durations, so the last input's own
    # trailing gap (audio priming) is what the seam model removes from its track.
    last = signatures[-1]
    expected_video = offset - (last.duration_seconds - last.video.raw_end)
    expected_audio = offset - (last.duration_seconds - last.audio.raw_end) if last.audio else None
    return AssemblyPlan(
        signatures=signatures,
        timeline=tuple(timeline),
        compatibility=joint,
        compatibility_sha256=joint_sha,
        expected_video_seconds=expected_video,
        expected_audio_seconds=expected_audio,
        max_frame_seconds=max(signature.max_frame_seconds for signature in signatures),
        expected_presentation_seconds=offset,
    )


def _normalized_frames(signature: CopySignature) -> int:
    """Frame-exact target frame count at the canonical 30 fps."""
    frame_ticks, timescale = signature.video.frame_ticks, signature.video.timescale
    if frame_ticks and timescale:
        frames = round(signature.video.samples * TARGET_FPS * frame_ticks / timescale)
    else:
        frames = round(signature.duration_seconds * TARGET_FPS)
    return max(1, int(frames))


def _normalized_duration(signature: CopySignature) -> Fraction:
    """Frame-exact target duration at the canonical 30 fps."""
    return Fraction(_normalized_frames(signature), TARGET_FPS)


def plan_normalization(signatures) -> AssemblyPlan:
    """Plan the transcode fallback: every input becomes the same canonical profile.

    The copy signature is still parsed for provenance and duration, but timeline,
    frame-rate, resolution and audio-presence differences are deliberately accepted
    here. The output timeline uses each input's presentation duration; the filter
    graph trims both streams to that duration before concatenation.
    """
    signatures = tuple(signatures)
    if not signatures:
        raise AssemblyError("FILM_INPUT_INCOMPATIBLE")
    if len(signatures) > MAX_INPUTS:
        raise AssemblyError("FILM_INPUT_INCOMPATIBLE", "镜头数量超过上限。")
    timeline, offset = [], Fraction(0)
    for index, signature in enumerate(signatures, start=1):
        duration = _normalized_duration(signature)
        if duration <= 0:
            raise AssemblyError("FILM_INPUT_INCOMPATIBLE", f"输入 {index} 的时长为零。")
        timeline.append({"start": offset, "duration": duration, "signature": signature})
        offset += duration
    normalization = {
        "version": NORMALIZATION_VERSION,
        "profile": NORMALIZATION_PROFILE,
        "width": TARGET_WIDTH,
        "height": TARGET_HEIGHT,
        "frameRate": TARGET_FPS,
        "videoCodec": "h264",
        "audioCodec": "aac",
        "audioSampleRate": TARGET_AUDIO_RATE,
        "audioChannels": TARGET_AUDIO_CHANNELS,
    }
    compatibility = {
        "version": NORMALIZATION_VERSION,
        "profile": normalization,
        "inputs": [
            {
                "signatureSha256": signature.sha256(),
                "duration": format_seconds(_normalized_duration(signature)),
                "hasAudio": signature.has_audio,
            }
            for signature in signatures
        ],
    }
    encoded = json.dumps(compatibility, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return AssemblyPlan(
        signatures=signatures,
        timeline=tuple(timeline),
        compatibility=compatibility,
        compatibility_sha256=hashlib.sha256(encoded.encode("utf-8")).hexdigest(),
        expected_video_seconds=offset,
        expected_audio_seconds=offset,
        max_frame_seconds=Fraction(1, TARGET_FPS),
        expected_presentation_seconds=offset,
        mode="normalize",
        assembly_version=NORMALIZATION_VERSION,
        profile=NORMALIZATION_PROFILE,
        normalization=normalization,
    )


def _write_concat_list(work: Path, names: list[str], plan: AssemblyPlan) -> Path:
    lines = ["ffconcat version 1.0"]
    for name, entry in zip(names, plan.timeline, strict=True):
        lines.append(f"file '{name}'")
        lines.append(f"duration {format_seconds(entry['duration'])}")
    target = work / "list.ffconcat"
    target.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")
    return target


def _link_or_copy(source: Path, target: Path) -> None:
    try:
        os.link(source, target)
    except OSError as error:
        if error.errno in {errno.EXDEV, errno.EPERM, errno.ENOTSUP, errno.EACCES}:
            shutil.copyfile(source, target)
        else:
            raise


def _command(binary: Path, listing: Path, output: Path) -> list[str]:
    return [
        str(binary),
        "-hide_banner",
        "-nostdin",
        "-loglevel",
        "warning",
        "-nostats",
        "-f",
        "concat",
        "-safe",
        "0",
        "-auto_convert",
        "0",
        "-protocol_whitelist",
        "file",
        "-i",
        str(listing),
        "-map",
        "0:v:0",
        "-map",
        "0:a:0?",
        "-c",
        "copy",
        "-movflags",
        "+faststart",
        "-f",
        "mp4",
        "-y",
        str(output),
    ]


def _normalization_command(binary: Path, names: list[str], plan: AssemblyPlan, output: Path) -> list[str]:
    """One filter graph: scale/fps/trim each input, add silence when needed, concat."""
    command = [
        str(binary),
        "-hide_banner",
        "-nostdin",
        "-loglevel",
        "warning",
        "-nostats",
    ]
    for name in names:
        command.extend(["-i", name])
    filters = []
    for index, entry in enumerate(plan.timeline):
        frames = _normalized_frames(entry["signature"])
        duration = format_seconds(Fraction(frames, TARGET_FPS))
        filters.append(
            f"[{index}:v:0]scale={TARGET_WIDTH}:{TARGET_HEIGHT}:force_original_aspect_ratio=decrease,"
            f"pad={TARGET_WIDTH}:{TARGET_HEIGHT}:(ow-iw)/2:(oh-ih)/2,setsar=1,fps={TARGET_FPS},"
            f"trim=end_frame={frames},setpts=PTS-STARTPTS,format=yuv420p[v{index}]"
        )
        if entry["signature"].has_audio:
            filters.append(
                f"[{index}:a:0]aresample={TARGET_AUDIO_RATE},"
                f"aformat=sample_fmts=fltp:channel_layouts=stereo,apad,"
                f"atrim=duration={duration},asetpts=PTS-STARTPTS[a{index}]"
            )
        else:
            filters.append(
                f"anullsrc=channel_layout=stereo:sample_rate={TARGET_AUDIO_RATE},"
                f"atrim=duration={duration},asetpts=PTS-STARTPTS[a{index}]"
            )
    streams = "".join(f"[v{index}][a{index}]" for index in range(len(names)))
    filters.append(f"{streams}concat=n={len(names)}:v=1:a=1[v][a]")
    command.extend(
        [
            "-filter_complex",
            ";".join(filters),
            "-map",
            "[v]",
            "-map",
            "[a]",
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-crf",
            "20",
            "-profile:v",
            "high",
            "-level:v",
            "4.0",
            "-pix_fmt",
            "yuv420p",
            "-fps_mode",
            "cfr",
            "-r",
            str(TARGET_FPS),
            "-c:a",
            "aac",
            "-b:a",
            "192k",
            "-ar",
            str(TARGET_AUDIO_RATE),
            "-ac",
            str(TARGET_AUDIO_CHANNELS),
            "-max_muxing_queue_size",
            "1024",
            "-movflags",
            "+faststart",
            "-f",
            "mp4",
            "-y",
            str(output),
        ]
    )
    return command


async def assemble(
    *,
    engine: Engine,
    inputs: list[Path],
    work_root: Path,
    output: Path,
    cancelled=None,
    deadline: float | None = None,
) -> AssembledFilm:
    """Concatenate verified inputs; the caller owns promotion and the Store commit."""
    if not inputs or len(inputs) > MAX_INPUTS:
        raise AssemblyError("FILM_INPUT_INCOMPATIBLE", "镜头数量超出上限。")
    signatures, total = [], 0
    for path in inputs:
        try:
            size = path.stat().st_size
        except OSError:
            raise AssemblyError("FILM_ASSEMBLY_FAILED", "输入文件不可读。") from None
        if size <= 0 or size > MAX_INPUT_BYTES:
            raise AssemblyError("FILM_INPUT_INCOMPATIBLE", "输入文件大小超出上限。")
        total += size
        if total > MAX_TOTAL_INPUT_BYTES:
            raise AssemblyError("FILM_INPUT_INCOMPATIBLE", "输入总大小超出上限。")
        with path.open("rb") as handle:
            try:
                signatures.append(read_copy_signature(handle, size))
            except (ValueError, OSError):
                raise AssemblyError("FILM_INPUT_INCOMPATIBLE", "输入不是可拼接的 AVC/AAC MP4。") from None
    plan = plan_assembly(signatures)
    work = Path(tempfile.mkdtemp(prefix="assemble-", dir=work_root))
    try:
        names = []
        for index, path in enumerate(inputs, start=1):
            name = f"in-{index:03d}.mp4"
            _link_or_copy(path, work / name)
            names.append(name)
        listing = _write_concat_list(work, names, plan)
        await _run_ffmpeg(engine, work, listing, output, cancelled=cancelled, deadline=deadline)
        size, digest = _hash_file(output)
        if size > MAX_OUTPUT_BYTES:
            raise AssemblyError("FILM_ASSEMBLY_FAILED", "输出文件超出上限。")
        with output.open("rb") as handle:
            try:
                metadata = inspect_mp4(handle, size)
            except (ValueError, OverflowError):
                raise AssemblyError("FILM_ASSEMBLY_FAILED", "输出文件不是完整的 H.264 MP4。") from None
            handle.seek(0)
            try:
                signature = read_copy_signature(handle, size)
            except (ValueError, OverflowError):
                raise AssemblyError("FILM_ASSEMBLY_FAILED", "输出文件的时间线结构无法验证。") from None
        _verify_output(metadata, signature, plan)
        return AssembledFilm(
            path=output,
            size_bytes=size,
            sha256=digest,
            metadata=metadata,
            signature=signature,
            plan=plan,
        )
    finally:
        shutil.rmtree(work, ignore_errors=True)


async def assemble_normalized(
    *,
    engine: Engine,
    inputs: list[Path],
    work_root: Path,
    output: Path,
    cancelled=None,
    deadline: float | None = None,
) -> AssembledFilm:
    """Transcode every input to the canonical profile and concatenate in one pass."""
    if not inputs or len(inputs) > MAX_INPUTS:
        raise AssemblyError("FILM_INPUT_INCOMPATIBLE", "镜头数量超出上限。")
    signatures, total = [], 0
    for path in inputs:
        try:
            size = path.stat().st_size
        except OSError:
            raise AssemblyError("FILM_ASSEMBLY_FAILED", "输入文件不可读。") from None
        if size <= 0 or size > MAX_INPUT_BYTES:
            raise AssemblyError("FILM_INPUT_INCOMPATIBLE", "输入文件大小超出上限。")
        total += size
        if total > MAX_TOTAL_INPUT_BYTES:
            raise AssemblyError("FILM_INPUT_INCOMPATIBLE", "输入总大小超出上限。")
        with path.open("rb") as handle:
            try:
                signatures.append(read_copy_signature(handle, size))
            except (ValueError, OSError):
                raise AssemblyError("FILM_INPUT_INCOMPATIBLE", "输入不是可拼接的 AVC/AAC MP4。") from None
    plan = plan_normalization(signatures)
    work = Path(tempfile.mkdtemp(prefix="normalize-", dir=work_root))
    try:
        names = []
        for index, path in enumerate(inputs, start=1):
            name = f"in-{index:03d}.mp4"
            _link_or_copy(path, work / name)
            names.append(name)
        command = _normalization_command(engine.path, names, plan, output)
        await _run_ffmpeg_command(
            engine,
            work,
            command,
            output,
            cancelled=cancelled,
            deadline=deadline,
            strict_warnings=False,
        )
        size, digest = _hash_file(output)
        if size > MAX_OUTPUT_BYTES:
            raise AssemblyError("FILM_ASSEMBLY_FAILED", "输出文件超出上限。")
        with output.open("rb") as handle:
            try:
                metadata = inspect_mp4(handle, size)
            except (ValueError, OverflowError):
                raise AssemblyError("FILM_ASSEMBLY_FAILED", "输出文件不是完整的 H.264 MP4。") from None
            handle.seek(0)
            try:
                signature = read_copy_signature(handle, size)
            except (ValueError, OverflowError):
                raise AssemblyError("FILM_ASSEMBLY_FAILED", "输出文件的时间线结构无法验证。") from None
        _verify_normalized_output(metadata, plan)
        plan = _fit_normalized_timeline(plan, signature.video.raw_end)
        return AssembledFilm(
            path=output,
            size_bytes=size,
            sha256=digest,
            metadata=metadata,
            signature=signature,
            plan=plan,
        )
    finally:
        shutil.rmtree(work, ignore_errors=True)


def _fit_normalized_timeline(plan: AssemblyPlan, actual: Fraction) -> AssemblyPlan:
    """Make the committed segment timeline equal the encoded video duration.

    Frame-rate conversion can drop a boundary frame at a seam. The output remains
    valid and within the verification tolerance; scaling the frozen segment
    durations keeps the public timeline consistent with the actual file instead of
    claiming a duration the encoder did not produce.
    """
    if actual <= 0 or plan.expected_presentation_seconds <= 0:
        raise AssemblyError("FILM_ASSEMBLY_FAILED", "输出时长无效。")
    scale = actual / plan.expected_presentation_seconds
    timeline, offset = [], Fraction(0)
    for entry in plan.timeline:
        duration = entry["duration"] * scale
        timeline.append({**entry, "start": offset, "duration": duration})
        offset += duration
    return replace(
        plan,
        timeline=tuple(timeline),
        expected_video_seconds=actual,
        expected_audio_seconds=actual,
        expected_presentation_seconds=actual,
    )


def _verify_normalized_output(metadata: MediaMetadata, plan: AssemblyPlan) -> None:
    if (metadata.width, metadata.height) != (TARGET_WIDTH, TARGET_HEIGHT):
        raise AssemblyError("FILM_ASSEMBLY_FAILED", "输出分辨率不是 1280×720。")
    if metadata.video_codec != "h264" or not metadata.has_audio or metadata.audio_codec != "aac":
        raise AssemblyError("FILM_ASSEMBLY_FAILED", "输出不是 H.264 + AAC。")
    if (metadata.frame_rate.numerator, metadata.frame_rate.denominator) != (TARGET_FPS, 1):
        raise AssemblyError("FILM_ASSEMBLY_FAILED", "输出帧率不是 30fps。")
    observed = Fraction(metadata.duration_seconds).limit_denominator(1_000_000)
    tolerance = max(Fraction(1, 2), plan.max_frame_seconds * 2)
    if abs(observed - plan.expected_presentation_seconds) > tolerance:
        raise AssemblyError("FILM_ASSEMBLY_FAILED", "输出时长偏离归一化时间线。")


def _verify_output(metadata: MediaMetadata, signature: CopySignature, plan: AssemblyPlan) -> None:
    first = plan.signatures[0]
    if (metadata.width, metadata.height) != (first.config["video"]["width"], first.config["video"]["height"]):
        raise AssemblyError("FILM_ASSEMBLY_FAILED", "输出分辨率与输入不一致。")
    if metadata.has_audio != first.has_audio:
        raise AssemblyError("FILM_ASSEMBLY_FAILED", "输出音轨与输入不一致。")
    if signature.config["video"]["avcC"] != first.config["video"]["avcC"]:
        raise AssemblyError("FILM_ASSEMBLY_FAILED", "输出的编码配置与输入不一致。")
    if first.has_audio and (
        signature.config["audio"] is None
        or signature.config["audio"]["esds"] != first.config["audio"]["esds"]
    ):
        raise AssemblyError("FILM_ASSEMBLY_FAILED", "输出的音频配置与输入不一致。")
    expected_samples = sum(item.video.samples for item in plan.signatures)
    if signature.video.samples != expected_samples:
        raise AssemblyError("FILM_ASSEMBLY_FAILED", "输出的视频样本数与输入不一致。")
    if first.has_audio:
        expected_audio = sum(item.audio.samples for item in plan.signatures if item.audio)
        if signature.audio is None or signature.audio.samples != expected_audio:
            raise AssemblyError("FILM_ASSEMBLY_FAILED", "输出的音频样本数与输入不一致。")
    video_tolerance = plan.max_frame_seconds + Fraction(1, signature.video.timescale)
    observed = Fraction(metadata.duration_seconds).limit_denominator(1_000_000)
    if abs(observed - plan.expected_video_seconds) > video_tolerance:
        raise AssemblyError("FILM_ASSEMBLY_FAILED", "输出视频时长偏离预期的样本时间线。")
    if plan.expected_audio_seconds is not None:
        if signature.audio is None:
            raise AssemblyError("FILM_ASSEMBLY_FAILED", "输出缺少音频轨。")
        audio_tolerance = plan.max_frame_seconds + Fraction(1, signature.audio.timescale)
        if abs(signature.audio.raw_end - plan.expected_audio_seconds) > audio_tolerance:
            raise AssemblyError("FILM_ASSEMBLY_FAILED", "输出音频时长偏离预期的样本时间线。")
    # A leading empty edit is the only explained way FFmpeg shifts the whole video
    # track; it may not exceed one accepted seam offset.
    if signature.video.empty_seconds > plan.max_frame_seconds + Fraction(1, signature.video.movie_timescale):
        raise AssemblyError("FILM_ASSEMBLY_FAILED", "输出的空编辑偏移超过允许的接缝偏移。")
    if signature.video.media_time != first.video.media_time:
        raise AssemblyError("FILM_ASSEMBLY_FAILED", "输出的视频合成偏移与首个输入不一致。")


async def _run_ffmpeg(engine: Engine, work: Path, listing: Path, output: Path, *, cancelled, deadline):
    command = _command(engine.path, listing, output)
    await _run_ffmpeg_command(
        engine, work, command, output, cancelled=cancelled, deadline=deadline, strict_warnings=True
    )


async def _run_ffmpeg_command(
    engine: Engine, work: Path, command: list[str], output: Path, *, cancelled, deadline, strict_warnings
):
    try:
        process = await asyncio.create_subprocess_exec(
            *command,
            cwd=str(work),
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except OSError as error:
        raise AssemblyError("ASSEMBLER_UNAVAILABLE", f"（{type(error).__name__}）") from None
    watcher = asyncio.ensure_future(_watch(process, cancelled, deadline))
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
        raise AssemblyError("FILM_ASSEMBLY_FAILED", "拼接已停止。")
    if process.returncode != 0:
        raise AssemblyError("FILM_ASSEMBLY_FAILED", _first_error(text))
    lowered = text.lower()
    if strict_warnings and any(marker in lowered for marker in WARNING_MARKERS):
        # FFmpeg can return 0 after silently shifting timestamps; refuse to deliver.
        raise AssemblyError("FILM_ASSEMBLY_FAILED", "时间戳告警，拒绝交付。")


def _first_error(text: str) -> str:
    for line in text.splitlines():
        stripped = line.strip()
        if stripped:
            return stripped[:300]
    return ""


async def _watch(process, cancelled, deadline) -> None:
    while process.returncode is None:
        if cancelled is not None and cancelled.is_set():
            await _terminate(process)
            return
        if deadline is not None and asyncio.get_running_loop().time() >= deadline:
            await _terminate(process)
            return
        await asyncio.sleep(0.05)


async def _terminate(process) -> None:
    if process.returncode is None:
        try:
            process.kill()
        except ProcessLookupError:
            pass
    try:
        await asyncio.wait_for(process.wait(), timeout=5)
    except TimeoutError:
        pass


def _hash_file(path: Path) -> tuple[int, str]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
            size += len(chunk)
    return size, digest.hexdigest()
