"""M6 E1 local subtitle export: soft ``mov_text``, burn-in and the sidecar path.

Export takes one assembled film version and one immutable subtitle track version and
appends a rendered film version. It never re-encodes audio in E1 (the source audio
stream is copied) and never calls a provider. The long FFmpeg work runs outside the
Store lock, exactly like film assembly: the caller re-verifies the frozen inputs and
commits the file, the new ``FilmVersion``, its ``FilmMedia`` and the Operation in one
transaction.
"""

import asyncio
import errno
import hashlib
import os
import re
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from vagent.errors import AppError
from vagent.storage import now
from vagent.video.assembler import Engine, bundled_engine
from vagent.video.films import (
    PROFILE_720P,
    RENDER_JOB_TIMEOUT_SECONDS,
    RENDER_TIMEOUT_SECONDS,
    RENDER_VERSION,
    SUBTITLE_MODES,
    ExportProfile,
    Film,
    FilmMedia,
    FilmRender,
    FilmVersion,
    RenderAudio,
    RenderAudioFacts,
    RenderEngine,
    RenderMeasurements,
    RenderSources,
    RenderSubtitle,
    RenderSubtitleSource,
    RenderVideoFacts,
    canonical_sha256,
    film_media,
    film_media_path,
    film_view,
    find_film,
    profile_for,
)
from vagent.video.mp4 import inspect_mp4
from vagent.video.subtitle_font import FontRef, bundled_font_path, font_ref
from vagent.video.subtitles import (
    MESSAGES as SUBTITLE_MESSAGES,
)
from vagent.video.subtitles import (
    SubtitleTrack,
    SubtitleTrackVersion,
    render_ass,
    render_srt,
    track_version_sha256,
)

TOOL_NAME = "subtitle_export"
MESSAGES = {
    "RENDER_PROFILE_UNSUPPORTED": "导出档位或源规格不受支持，未渲染。",
    "RENDER_SOURCE_MISSING": "缺少已交付成片、字幕轨或镜头来源，未渲染。",
    "RENDER_TIMELINE_MISMATCH": "字幕时间线不是这个成片版本的时间线，请按该成片重新保存字幕。",
    "RENDER_TIMEOUT": "本地导出超时，未交付成片。",
    "RENDER_ENCODE_FAILED": "本地编码失败，未交付成片。",
    "RENDER_VALIDATION_FAILED": "导出文件未通过结构、时长或字幕轨校验，未交付成片。",
}
WARNING_MARKERS = (
    "non-monotonic dts",
    "non monotonically increasing",
    "packet corrupt",
    "invalid dropping",
    "truncating packet",
    "dts out of order",
)


class RenderError(AppError):
    def __init__(self, code: str, detail: str = "", *, stage: str = "encode"):
        message = MESSAGES[code]
        super().__init__(code, f"{message}{detail}" if detail else message)
        self.stage = stage


@dataclass(frozen=True)
class ProbedStreams:
    duration_seconds: float | None
    video: dict | None
    audio: dict | None
    subtitles: list[dict]


class SubtitleService:
    def __init__(self, jobs):
        self.jobs = jobs
        self.store = jobs.store
        self.media = jobs.media
        self.files = jobs.media.files

    # -- paths -----------------------------------------------------------------
    def _work_root(self) -> Path:
        root = self.store.home / "render"
        root.mkdir(mode=0o700, exist_ok=True)
        return root

    def _part_path(self, relative: str) -> Path:
        with self.files.parent(relative, create=True) as (path, _descriptor, name):
            return path / name

    # -- read side -------------------------------------------------------------
    def view(self, project_id: str, *, storyboard_id=None, episode_id=None, track_id=None) -> dict:
        state = self.store.snapshot()
        track = None
        if track_id is not None:
            raw = state["subtitleTracks"].get(track_id)
            if raw is not None and raw["projectId"] == project_id:
                track = SubtitleTrack.model_validate(raw)
        elif storyboard_id is not None:
            from vagent.video.subtitles import find_track

            track = find_track(state, project_id, "storyboard", storyboard_id)
        elif episode_id is not None:
            from vagent.video.subtitles import find_track

            track = find_track(state, project_id, "episode", episode_id)
        if track is None:
            return {"storyboardId": storyboard_id, "episodeId": episode_id, "track": None}
        from vagent.video.subtitles import subtitle_view

        return {
            "storyboardId": storyboard_id,
            "episodeId": episode_id,
            "track": subtitle_view(state, track),
        }

    # -- export ----------------------------------------------------------------
    async def export(
        self,
        *,
        project_id: str,
        track_id: str,
        mode: str,
        profile_name: str = PROFILE_720P,
        track_version: int | None = None,
        operation_key: str,
        args: dict,
        cancelled=None,
    ) -> dict:
        previous = self.store.operation_result(operation_key, TOOL_NAME, args)
        if previous is not None:
            return previous
        if mode not in SUBTITLE_MODES:
            raise RenderError("RENDER_PROFILE_UNSUPPORTED", "（字幕模式）", stage="source")
        if profile_name != PROFILE_720P:
            # 1080P is a parameterised upscale profile; E1 does not upscale.
            raise RenderError("RENDER_PROFILE_UNSUPPORTED", "（1080P 上采样属于后续阶段）", stage="source")
        engine = bundled_engine()
        font_path = bundled_font_path()
        reference = font_ref()
        state = self.store.snapshot()
        track = self._track(state, track_id, project_id)
        version = self._track_version(track, track_version)
        if track.scope != "storyboard" or track.storyboard_id is None:
            raise RenderError("RENDER_SOURCE_MISSING", "（整集导出属于后续阶段）", stage="source")
        film = find_film(state, project_id, track.storyboard_id)
        if film is None:
            raise RenderError("RENDER_SOURCE_MISSING", "（还没有成片）", stage="source")
        base = self._base_version(film)
        if not version.cues:
            raise AppError("SUBTITLE_TRACK_MISSING", SUBTITLE_MESSAGES["SUBTITLE_TRACK_MISSING"])
        if not self._timeline_matches(version, base):
            raise RenderError("RENDER_TIMELINE_MISMATCH", "", stage="subtitle")
        profile = ExportProfile.model_validate(profile_for(mode, name=profile_name))
        if (base.metadata.width, base.metadata.height) != (profile.width, profile.height):
            raise RenderError("RENDER_PROFILE_UNSUPPORTED", "（源分辨率与档位不一致）", stage="source")
        track_sha = track_version_sha256(version)
        fingerprint = self._fingerprint(film, base, track, version, track_sha, profile, engine, reference)
        replay = self._replay(state, film, fingerprint)
        if replay is not None:
            return self.store.operation(operation_key, TOOL_NAME, args, lambda _: replay)
        media = film_media(state, base.output_media_id)
        if media is None:
            raise RenderError("RENDER_SOURCE_MISSING", "（成片媒体记录缺失）", stage="source")
        try:
            lease = self.files.verified(media.relative_path, media)
        except FileNotFoundError:
            raise RenderError("RENDER_SOURCE_MISSING", "（成片文件缺失）", stage="source") from None
        except OSError:
            raise RenderError("RENDER_VALIDATION_FAILED", "（成片文件损坏）", stage="source") from None
        lease.close()
        media_id = str(uuid4())
        final_relative = film_media_path(project_id, media_id)
        part_relative = final_relative + ".part"
        output = self._part_path(part_relative)
        try:
            produced = await self._render(
                engine=engine,
                base_metadata=base.metadata,
                source=self.store.home / media.relative_path,
                cues=version.cues,
                profile=profile,
                mode=mode,
                font_path=font_path,
                work_root=self._work_root(),
                output=output,
                cancelled=cancelled,
            )
            result = self.store.operation(
                operation_key,
                TOOL_NAME,
                args,
                lambda draft: self._commit(
                    draft,
                    project_id=project_id,
                    film=film,
                    base=base,
                    track=track,
                    version=version,
                    track_sha=track_sha,
                    profile=profile,
                    engine=engine,
                    reference=reference,
                    produced=produced,
                    media_id=media_id,
                    final_relative=final_relative,
                    part_relative=part_relative,
                    fingerprint=fingerprint,
                ),
            )
            return result
        except BaseException:
            output.unlink(missing_ok=True)
            raise

    # -- frozen inputs ---------------------------------------------------------
    @staticmethod
    def _track(state: dict, track_id: str, project_id: str) -> SubtitleTrack:
        raw = state["subtitleTracks"].get(track_id)
        if raw is None or raw["projectId"] != project_id:
            raise AppError("SUBTITLE_TRACK_MISSING", SUBTITLE_MESSAGES["SUBTITLE_TRACK_MISSING"])
        return SubtitleTrack.model_validate(raw)

    @staticmethod
    def _track_version(track: SubtitleTrack, version: int | None) -> SubtitleTrackVersion:
        chosen = (
            next((item for item in track.versions if item.version == version), None)
            if version is not None
            else track.versions[-1]
        )
        if chosen is None:
            raise AppError("SUBTITLE_TRACK_MISSING", SUBTITLE_MESSAGES["SUBTITLE_TRACK_MISSING"])
        return chosen

    @staticmethod
    def _base_version(film: Film) -> FilmVersion:
        base = next((item for item in reversed(film.versions) if item.render is None), None)
        if base is None:
            raise RenderError("RENDER_SOURCE_MISSING", "（没有可渲染的拼接版本）", stage="source")
        return base

    @staticmethod
    def _timeline_matches(version: SubtitleTrackVersion, base: FilmVersion) -> bool:
        segments = base.assembly.timeline
        if len(segments) != len(version.timeline):
            return False
        for segment, entry in zip(segments, version.timeline, strict=True):
            if segment.shot_id != entry.shot_id:
                return False
            # Assembly timestamps and subtitle timestamps are both exact rationals, but
            # the film records its segment starts rounded to microseconds. Compare in the
            # contract's microsecond time base so the same timeline is recognized.
            if (
                segment.start.total_microseconds() != entry.start.total_microseconds()
                or segment.duration.total_microseconds() != entry.duration.total_microseconds()
            ):
                return False
        return True

    @staticmethod
    def _fingerprint(
        film: Film,
        base: FilmVersion,
        track: SubtitleTrack,
        version: SubtitleTrackVersion,
        track_sha: str,
        profile: ExportProfile,
        engine: Engine,
        reference: FontRef,
    ) -> str:
        return canonical_sha256(
            {
                "renderVersion": RENDER_VERSION,
                "filmId": film.id,
                "sourceVersion": base.version,
                "sourceSha256": base.sha256,
                "storyboardVersion": base.storyboard_version,
                "shotVersionIds": [item.shot_version_id for item in base.inputs],
                "trackId": track.id,
                "trackVersion": version.version,
                "trackSha256": track_sha,
                "profile": profile.model_dump(mode="json", by_alias=True),
                "engine": {"version": engine.ffmpeg_version, "binarySha256": engine.binary_sha256},
                "font": reference.model_dump(mode="json", by_alias=True),
            }
        )

    @staticmethod
    def _replay(state: dict, film: Film, fingerprint: str):
        if any(
            item.render is not None and item.render.fingerprint == fingerprint
            for item in reversed(film.versions)
        ):
            return film_view(state, film)
        return None

    # -- rendering -------------------------------------------------------------
    async def _render(
        self,
        *,
        engine: Engine,
        base_metadata,
        source: Path,
        cues,
        profile: ExportProfile,
        mode: str,
        font_path: Path,
        work_root: Path,
        output: Path,
        cancelled,
    ) -> dict:
        work = Path(tempfile.mkdtemp(prefix="subtitle-", dir=work_root))
        deadline = asyncio.get_running_loop().time() + RENDER_JOB_TIMEOUT_SECONDS
        try:
            source_name = "source.mp4"
            self._link_or_copy(source, work / source_name)
            (work / "fonts").mkdir()
            self._link_or_copy(font_path, work / "fonts" / "subtitle-font.ttf")
            srt_bytes = render_srt(cues).encode("utf-8")
            ass_text = render_ass(cues, width=profile.width, height=profile.height)
            (work / "sub.srt").write_bytes(srt_bytes)
            (work / "sub.ass").write_text(ass_text, encoding="utf-8", newline="\n")
            if mode == "soft":
                await self._run(
                    engine,
                    work,
                    self._soft_command(source_name, output),
                    output,
                    cancelled=cancelled,
                    deadline=deadline,
                    strict=True,
                )
            elif mode == "burn":
                await self._run(
                    engine,
                    work,
                    self._burn_command(source_name, output, profile),
                    output,
                    cancelled=cancelled,
                    deadline=deadline,
                    strict=True,
                )
            else:
                burned = work / "burned.mp4"
                await self._run(
                    engine,
                    work,
                    self._burn_command(source_name, burned, profile),
                    burned,
                    cancelled=cancelled,
                    deadline=deadline,
                    strict=True,
                )
                await self._run(
                    engine,
                    work,
                    self._soft_command("burned.mp4", output),
                    output,
                    cancelled=cancelled,
                    deadline=deadline,
                    strict=True,
                )
            size, digest = _hash_file(output)
            with output.open("rb") as handle:
                metadata = inspect_mp4(handle, size)
            streams = await self._probe(engine, output, deadline)
            self._validate(streams, profile, mode, metadata, base_metadata)
            sidecar_sha = hashlib.sha256(srt_bytes).hexdigest() if mode in {"soft", "both"} else None
            return {
                "size_bytes": size,
                "sha256": digest,
                "metadata": metadata,
                "streams": streams,
                "sidecar_sha256": sidecar_sha,
                "srt_bytes": srt_bytes,
            }
        finally:
            shutil.rmtree(work, ignore_errors=True)

    def _soft_command(self, source_name: str, output: Path) -> list[str]:
        return [
            "ffmpeg",
            "-hide_banner",
            "-nostdin",
            "-loglevel",
            "warning",
            "-nostats",
            "-i",
            source_name,
            "-i",
            "sub.srt",
            "-map",
            "0:v:0",
            "-map",
            "0:a:0?",
            "-map",
            "1:0",
            "-c:v",
            "copy",
            "-c:a",
            "copy",
            "-c:s",
            "mov_text",
            "-metadata:s:s:0",
            "language=zho",
            "-disposition:s:0",
            "0",
            "-movflags",
            "+faststart",
            "-f",
            "mp4",
            "-y",
            str(output),
        ]

    def _burn_command(self, source_name: str, output: Path, profile: ExportProfile) -> list[str]:
        return [
            "ffmpeg",
            "-hide_banner",
            "-nostdin",
            "-loglevel",
            "warning",
            "-nostats",
            "-i",
            source_name,
            "-vf",
            "subtitles=sub.ass:fontsdir=fonts",
            "-map",
            "0:v:0",
            "-map",
            "0:a:0?",
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-crf",
            str(profile.crf),
            "-profile:v",
            "high",
            "-level:v",
            "4.0",
            "-pix_fmt",
            profile.pix_fmt,
            "-fps_mode",
            "cfr",
            "-r",
            str(profile.fps),
            "-c:a",
            "copy",
            "-movflags",
            "+faststart",
            "-f",
            "mp4",
            "-y",
            str(output),
        ]

    async def _run(
        self,
        engine: Engine,
        work: Path,
        command: list[str],
        output: Path,
        *,
        cancelled,
        deadline,
        strict: bool,
    ) -> None:
        command = [str(engine.path), *command[1:]]
        try:
            process = await asyncio.create_subprocess_exec(
                *command,
                cwd=str(work),
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except OSError:
            raise RenderError("RENDER_ENCODE_FAILED", "（无法启动本地编码器）", stage="encode") from None
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
            raise RenderError("RENDER_ENCODE_FAILED", "（导出已停止）", stage="encode")
        if loop.time() >= limit:
            # The watcher kills the process at the per-call or job deadline.
            raise RenderError("RENDER_TIMEOUT", "", stage="encode")
        if process.returncode != 0:
            raise RenderError("RENDER_ENCODE_FAILED", f"（{_first_error(text)}）", stage="encode")
        if strict and any(marker in text.lower() for marker in WARNING_MARKERS):
            raise RenderError("RENDER_VALIDATION_FAILED", "（时间戳告警）", stage="validate")

    async def _probe(self, engine: Engine, path: Path, deadline: float) -> ProbedStreams:
        try:
            process = await asyncio.create_subprocess_exec(
                str(engine.path),
                "-hide_banner",
                "-nostdin",
                "-i",
                str(path),
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except OSError:
            raise RenderError("RENDER_VALIDATION_FAILED", "（无法读取导出文件）", stage="validate") from None
        try:
            _stdout, stderr = await asyncio.wait_for(
                process.communicate(),
                timeout=max(1.0, min(30.0, deadline - asyncio.get_running_loop().time())),
            )
        except TimeoutError:
            await _terminate(process)
            raise RenderError("RENDER_TIMEOUT", "（读取导出文件超时）", stage="validate") from None
        return parse_streams((stderr or b"").decode("utf-8", "replace"))

    @staticmethod
    def _validate(streams: ProbedStreams, profile: ExportProfile, mode: str, metadata, base) -> None:
        if streams.video is None:
            raise RenderError("RENDER_VALIDATION_FAILED", "（缺少视频轨）", stage="validate")
        if streams.video["codec"] != "h264":
            raise RenderError("RENDER_VALIDATION_FAILED", "（视频编码不是 H.264）", stage="validate")
        if (streams.video["width"], streams.video["height"]) != (profile.width, profile.height):
            raise RenderError("RENDER_VALIDATION_FAILED", "（分辨率与档位不一致）", stage="validate")
        if streams.video["fps"] is None:
            raise RenderError("RENDER_VALIDATION_FAILED", "（读不到帧率）", stage="validate")
        if mode == "soft":
            # A copy export keeps the assembled film's cadence exactly.
            expected = float(base.frame_rate.numerator) / base.frame_rate.denominator
        else:
            expected = float(profile.fps)
        if abs(streams.video["fps"] - expected) > 0.05:
            raise RenderError("RENDER_VALIDATION_FAILED", "（帧率与来源或档位不一致）", stage="validate")
        if metadata.video_codec != "h264":
            raise RenderError("RENDER_VALIDATION_FAILED", "（MP4 结构不是 H.264）", stage="validate")
        present = [item for item in streams.subtitles if item["codec"] == "mov_text"]
        if mode in {"soft", "both"} and not present:
            raise RenderError("RENDER_VALIDATION_FAILED", "（缺少 mov_text 字幕轨）", stage="validate")
        if mode == "burn" and streams.subtitles:
            raise RenderError("RENDER_VALIDATION_FAILED", "（烧录输出不应再有字幕轨）", stage="validate")

    @staticmethod
    def _link_or_copy(source: Path, target: Path) -> None:
        try:
            os.link(source, target)
        except OSError as error:
            if error.errno in {errno.EXDEV, errno.EPERM, errno.ENOTSUP, errno.EACCES}:
                shutil.copyfile(source, target)
            else:
                raise

    # -- commit ----------------------------------------------------------------
    def _commit(
        self,
        draft: dict,
        *,
        project_id: str,
        film: Film,
        base: FilmVersion,
        track: SubtitleTrack,
        version: SubtitleTrackVersion,
        track_sha: str,
        profile: ExportProfile,
        engine: Engine,
        reference: FontRef,
        produced: dict,
        media_id: str,
        final_relative: str,
        part_relative: str,
        fingerprint: str,
    ) -> dict:
        part = Path(self.store.home / part_relative)
        try:
            size, digest = _hash_file(part)
        except OSError:
            raise RenderError("RENDER_ENCODE_FAILED", "（导出产物丢失）", stage="encode") from None
        if (size, digest) != (produced["size_bytes"], produced["sha256"]):
            raise RenderError("RENDER_VALIDATION_FAILED", "（导出产物校验值不一致）", stage="validate")
        raw = draft["media"].get(base.output_media_id)
        if raw is None or (raw.get("sizeBytes"), raw.get("sha256")) != (base.size_bytes, base.sha256):
            raise RenderError("RENDER_SOURCE_MISSING", "（成片在导出期间被替换）", stage="source")
        try:
            self.files.promote(final_relative)
        except FileExistsError:
            raise RenderError(
                "RENDER_VALIDATION_FAILED", "（目标成片文件已存在）", stage="validate"
            ) from None
        timestamp = now()
        streams = produced["streams"]
        audio_facts = (
            RenderAudioFacts(
                codec=streams.audio["codec"],
                sample_rate=streams.audio["sample_rate"],
                channels=streams.audio["channels"],
            )
            if streams.audio is not None
            else None
        )
        measurements = RenderMeasurements(
            duration_seconds=produced["metadata"].duration_seconds,
            size_bytes=produced["size_bytes"],
            sha256=produced["sha256"],
            video=RenderVideoFacts(
                codec="h264",
                width=streams.video["width"],
                height=streams.video["height"],
                fps=streams.video["fps"],
            ),
            audio=audio_facts,
        )
        render = FilmRender(
            export_profile=profile,
            engine=RenderEngine(
                version=engine.ffmpeg_version,
                configuration_sha256=canonical_sha256(
                    {
                        "renderVersion": RENDER_VERSION,
                        "profile": profile.model_dump(mode="json", by_alias=True),
                        "binarySha256": engine.binary_sha256,
                        "audio": "copy",
                        "video": {"codec": "h264", "fps": profile.fps, "pixFmt": profile.pix_fmt},
                    }
                ),
            ),
            subtitle=RenderSubtitle(
                track_id=track.id,
                track_version=version.version,
                track_sha256=track_sha,
                mode=profile.subtitle_mode,
                font_ref=reference,
                sidecar_sha256=produced["sidecar_sha256"],
            ),
            audio=RenderAudio(),
            measurements=measurements,
            sources=RenderSources(
                storyboard_version=base.storyboard_version,
                shot_version_ids=tuple(item.shot_version_id for item in base.inputs),
                audio_assets=(),
                subtitle_track=RenderSubtitleSource(
                    track_id=track.id, track_version=version.version, track_sha256=track_sha
                ),
                font=reference,
            ),
            fingerprint=fingerprint,
        )
        film_id = film.id
        number = film.current_version + 1
        new_version = base.model_copy(
            update={
                "version": number,
                "output_media_id": media_id,
                "size_bytes": produced["size_bytes"],
                "sha256": produced["sha256"],
                "metadata": produced["metadata"],
                "render": render,
                "created_at": timestamp,
            }
        )
        updated = film.model_copy(
            update={
                "current_version": number,
                "versions": (*film.versions, new_version),
                "revision": film.revision + 1,
            }
        )
        media = FilmMedia(
            id=media_id,
            project_id=project_id,
            film_id=film_id,
            film_version=number,
            relative_path=final_relative,
            size_bytes=produced["size_bytes"],
            sha256=produced["sha256"],
            metadata=produced["metadata"],
            created_at=timestamp,
        )
        draft["films"][film_id] = updated.model_dump(mode="json", by_alias=True)
        draft["media"][media_id] = media.model_dump(mode="json", by_alias=True)
        return film_view(draft, updated)


async def _watch(process, cancelled, deadline) -> None:
    while process.returncode is None:
        if cancelled is not None and cancelled.is_set():
            await _terminate(process)
            return
        if asyncio.get_running_loop().time() >= deadline:
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


def _first_error(text: str) -> str:
    for line in text.splitlines():
        stripped = line.strip()
        if stripped:
            return stripped[:300]
    return ""


_STREAM = re.compile(r"Stream #\d+:\d+(?:\[[^\]]+\])?(?:\((\w+)\))?: (Video|Audio|Subtitle): (.*)")


def parse_streams(text: str) -> ProbedStreams:
    """Parse FFmpeg's stream listing; the bundled build has no ffprobe."""
    duration = None
    match = re.search(r"Duration: (\d+):(\d+):(\d+(?:\.\d+)?)", text)
    if match:
        duration = int(match.group(1)) * 3600 + int(match.group(2)) * 60 + float(match.group(3))
    video = audio = None
    subtitles: list[dict] = []
    for line in text.splitlines():
        match = _STREAM.search(line)
        if match is None:
            continue
        language, kind, detail = match.group(1), match.group(2), match.group(3)
        if kind == "Video" and video is None:
            codec = detail.split(" ", 1)[0].strip(" ,")
            # Avoid the fourcc hex blob (0x31637661); real dimensions are 3-4 digits.
            size = re.search(r"(\d{3,4})x(\d{3,4})", detail)
            fps = re.search(r"(\d+(?:\.\d+)?) fps", detail)
            video = {
                "codec": codec,
                "width": int(size.group(1)) if size else 0,
                "height": int(size.group(2)) if size else 0,
                "fps": float(fps.group(1)) if fps else None,
            }
        elif kind == "Audio" and audio is None:
            codec = detail.split(" ", 1)[0].strip(" ,")
            rate = re.search(r"(\d+) Hz", detail)
            layout = re.search(r", (mono|stereo|5\.1|7\.1)", detail)
            audio = {
                "codec": codec,
                "sample_rate": int(rate.group(1)) if rate else 0,
                "channels": {"mono": 1, "stereo": 2, "5.1": 6, "7.1": 8}.get(
                    layout.group(1) if layout else "", 0
                ),
            }
        elif kind == "Subtitle":
            codec = detail.split(" ", 1)[0].strip(" ,")
            subtitles.append({"codec": codec, "language": language})
    return ProbedStreams(duration_seconds=duration, video=video, audio=audio, subtitles=subtitles)
