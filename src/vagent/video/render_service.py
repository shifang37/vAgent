"""M6 E3 local full render: transitions, title cards, audio mix, subtitles, export.

The render is entirely local FFmpeg work. It resolves delivered shots into a frozen
timeline, validates transitions/title cards against it, renders each scene to an
intermediate (so one failed scene can be reported without aborting the others), mixes
the voice/BGM/SFX buses with :func:`vagent.video.mixer.mix_tracks`, and appends one
``FilmVersion`` with a ``FilmRender``. The Store lock is held only for reads and the
final commit; all encoding happens outside it.
"""

import asyncio
import hashlib
import shutil
import tempfile
from dataclasses import dataclass, field
from fractions import Fraction
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

from vagent.errors import AppError
from vagent.storage import now
from vagent.video.assembler import (
    PACKAGE_VERSION,
    Engine,
    bundled_engine,
    format_seconds,
)
from vagent.video.audio import NARRATOR_SPEAKER, AudioAsset, AudioMedia, ProviderAudioSource
from vagent.video.episodes import Episode, Scene
from vagent.video.films import (
    EXPORT_PROFILES,
    MAX_RENDER_AUDIO_TRACKS,
    RENDER_JOB_TIMEOUT_SECONDS,
    RENDER_NATIVE_ASSEMBLY,
    RENDER_TIMEOUT_SECONDS,
    RENDER_VERSION,
    Film,
    FilmAssembly,
    FilmEngine,
    FilmInput,
    FilmMedia,
    FilmRender,
    FilmSegment,
    FilmVersion,
    Rational,
    RenderAudio,
    RenderAudioFacts,
    RenderAudioTrack,
    RenderDucking,
    RenderEngine,
    RenderLoudness,
    RenderMeasurements,
    RenderSources,
    RenderSubtitle,
    RenderSubtitleSource,
    RenderTitleCard,
    RenderTransition,
    RenderVideoFacts,
    canonical_sha256,
    film_media_path,
    film_view,
    find_episode_film,
    find_film,
    parse_media,
    profile_for,
)
from vagent.video.mixer import MIX_VERSION, MixError, MixTrack, mix_tracks
from vagent.video.mp4 import inspect_mp4
from vagent.video.renders import (
    EffectiveEntry,
    FilmRenderArgs,
    RenderError,
    RenderReadArgs,
    RenderSceneProgress,
    RenderTask,
    RenderTitleCardArg,
    ShotEntry,
    build_task,
    effective_duration,
    effective_entries,
    estimated_render_seconds,
    latest_render_task,
    remap_cues,
    render_fingerprint,
    render_read_view,
    task_from_state,
    validate_title_cards,
    validate_transitions,
)
from vagent.video.storyboards import latest_shot_version, storyboard_in_state
from vagent.video.subtitle_font import FontRef, bundled_font_path, font_ref
from vagent.video.subtitle_service import ProbedStreams, parse_streams
from vagent.video.subtitles import (
    SubtitleCue,
    SubtitleTrack,
    SubtitleTrackVersion,
    find_track,
    render_ass,
    render_srt,
    track_by_id,
    track_version_sha256,
)

TOOL_NAME = "film_render"
READ_TOOL_NAME = "render_read"
WARNING_MARKERS = (
    "non-monotonic dts",
    "non monotonically increasing",
    "packet corrupt",
    "invalid dropping",
    "truncating packet",
    "dts out of order",
)
RENDER_SUBDIR = "render"
FRAME_SECONDS = Fraction(1, 30)
MESSAGES = {
    "RENDER_PROFILE_UNSUPPORTED": "导出档位或源规格不受支持，未渲染。",
    "RENDER_SOURCE_MISSING": "缺少已交付镜头、成片、字幕轨或音频资产，未渲染。",
    "RENDER_TIMELINE_MISMATCH": "输出时长、顺序或字幕时间线与冻结时间线不符，未渲染。",
    "RENDER_TIMEOUT": "本地渲染超时，未交付成片。",
    "RENDER_ENCODE_FAILED": "本地编码失败，未交付成片。",
    "RENDER_VALIDATION_FAILED": "渲染文件未通过结构、时长或字幕轨校验，未交付成片。",
    "LOUDNESS_OUT_OF_RANGE": "混音响度或 true peak 超出容差，未交付。",
    "FONT_MISSING": "包内字体缺失或 SHA-256 不符，未渲染。",
    "CONFIRMATION_REQUIRED": "渲染会写本地成片，需要显式确认。",
}


@dataclass
class PlannedShot:
    shot_id: str
    shot_version_id: str | None
    job_id: str | None
    media_id: str | None
    relative_path: str | None
    sha256: str | None
    size_bytes: int | None
    start: Fraction
    duration: Fraction
    scene_id: str | None


@dataclass
class PlannedScene:
    scene_id: str | None
    storyboard_id: str
    storyboard_version: int
    storyboard_revision: int
    shots: list[PlannedShot]
    failure_code: str | None = None
    failure_stage: str | None = None

    @property
    def shot_ids(self) -> tuple[str, ...]:
        return tuple(shot.shot_id for shot in self.shots)


@dataclass
class AudioPlan:
    tracks: list[MixTrack] = field(default_factory=list)
    rendered: list[RenderAudioTrack] = field(default_factory=list)
    assets: list[str] = field(default_factory=list)
    ducking: RenderDucking = field(default_factory=RenderDucking)


@dataclass
class RenderPlan:
    project_id: str
    scope: str
    storyboard_id: str | None
    episode_id: str | None
    storyboard_version: int | None
    episode_version: int | None
    revision: int
    scenes: list[PlannedScene]
    shots: list[PlannedShot]
    entries: list[ShotEntry]
    transitions: dict[str, tuple[str, Fraction]]
    title_cards: tuple[RenderTitleCardArg, ...]
    effective: list[EffectiveEntry]
    total: Fraction
    profile: dict
    mode: str
    track: SubtitleTrack | None
    track_version: SubtitleTrackVersion | None
    track_sha256: str | None
    font: FontRef
    engine: Engine
    audio: AudioPlan
    retimed: bool
    fingerprint: str

    @property
    def target_id(self) -> str:
        return self.storyboard_id or self.episode_id or ""


class RenderService:
    def __init__(self, jobs):
        self.jobs = jobs
        self.store = jobs.store
        self.media = jobs.media
        self.files = jobs.media.files

    # -- paths -----------------------------------------------------------------
    def _work_root(self) -> Path:
        root = self.store.home / RENDER_SUBDIR
        root.mkdir(mode=0o700, exist_ok=True)
        return root

    def _part_path(self, relative: str) -> Path:
        with self.files.parent(relative, create=True) as (path, _descriptor, name):
            return path / name

    # -- read side -------------------------------------------------------------
    def read(self, args: dict, project_id: str) -> dict:
        parsed = RenderReadArgs.model_validate(args)
        state = self.store.snapshot()
        if parsed.render_id is not None:
            task = task_from_state(state, parsed.render_id, project_id)
            return render_read_view(state, task)
        if parsed.film_id is not None:
            raw = state["films"].get(parsed.film_id)
            if raw is None or raw["projectId"] != project_id:
                raise AppError("FILM_NOT_FOUND", "当前项目中没有这个成片。")
            task = latest_render_task(state, project_id, film_id=parsed.film_id)
            return render_read_view(state, task, film=Film.model_validate(raw))
        task = latest_render_task(
            state,
            project_id,
            episode_id=parsed.episode_id,
            storyboard_id=parsed.storyboard_id,
        )
        film = None
        if parsed.episode_id is not None:
            film = find_episode_film(state, project_id, parsed.episode_id)
        elif parsed.storyboard_id is not None:
            film = find_film(state, project_id, parsed.storyboard_id)
        return render_read_view(state, task, film=film)

    def episode_summary(self, state: dict, project_id: str, episode_id: str | None) -> dict | None:
        if episode_id is None:
            return None
        task = latest_render_task(state, project_id, episode_id=episode_id)
        film = find_episode_film(state, project_id, episode_id)
        view = render_read_view(state, task, film=film)
        return view["render"]

    # -- render ----------------------------------------------------------------
    async def render(self, args_dict: dict, *, project_id: str, operation_key: str, args: dict) -> dict:
        previous = self.store.operation_result(operation_key, TOOL_NAME, args)
        if previous is not None:
            return previous
        args_model = FilmRenderArgs.model_validate(args_dict)
        if not args_model.confirm:
            raise RenderError("CONFIRMATION_REQUIRED", "", stage="source")
        if args_model.profile not in EXPORT_PROFILES:
            raise RenderError("RENDER_PROFILE_UNSUPPORTED", "（未知档位）", stage="source")
        engine = bundled_engine()
        reference = font_ref()
        bundled_font_path()
        state = self.store.snapshot()
        plan = self._plan(state, project_id, args_model, engine, reference)
        replay = self._replay(state, plan)
        if replay is not None:
            return self.store.operation(operation_key, TOOL_NAME, args, lambda _: replay)
        task = build_task(
            task_id=str(uuid4()),
            project_id=project_id,
            args=args_model,
            fingerprint=plan.fingerprint,
            scenes=[
                RenderSceneProgress(
                    scene_id=scene.scene_id,
                    storyboard_id=scene.storyboard_id,
                    shot_ids=scene.shot_ids,
                    status="failed" if scene.failure_code else "pending",
                    failure_stage=scene.failure_stage if scene.failure_code else None,
                    error_code=scene.failure_code,
                )
                for scene in plan.scenes
            ],
            estimated_seconds=estimated_render_seconds(plan.total, len(plan.audio.tracks)),
            timestamp=now(),
        )
        self._insert_task(task)
        try:
            produced = await self._produce(plan, task, args_model, operation_key)
        except BaseException as error:
            self._fail_task(task, plan, error)
            raise
        try:
            result = self.store.operation(
                operation_key,
                TOOL_NAME,
                args,
                lambda draft: self._commit(
                    draft,
                    plan=plan,
                    task=task,
                    produced=produced,
                    args_model=args_model,
                ),
            )
        except BaseException as error:
            self._fail_task(task, plan, error)
            raise
        return result

    # -- planning --------------------------------------------------------------
    def _plan(
        self,
        state: dict,
        project_id: str,
        args: FilmRenderArgs,
        engine: Engine,
        reference: FontRef,
    ) -> RenderPlan:
        profile = profile_for(args.subtitle_mode, name=args.profile)
        if args.scope == "storyboard":
            plan = self._plan_storyboard(state, project_id, args, profile, engine, reference)
        else:
            plan = self._plan_episode(state, project_id, args, profile, engine, reference)
        active = validate_transitions(plan.entries, args.transitions)
        effective = effective_entries(plan.entries, active)
        total = effective_duration(effective)
        if total <= 0 or total > 90:
            raise RenderError("RENDER_PROFILE_UNSUPPORTED", "（成片时长必须不超过 90 秒）", stage="source")
        validate_title_cards(args.title_cards, total)
        track, frozen, track_sha = self._resolve_track(state, project_id, args, plan.entries)
        plan.track, plan.track_version, plan.track_sha256 = track, frozen, track_sha
        plan.transitions = active
        plan.effective = effective
        plan.total = total
        plan.retimed = bool(active)
        plan.audio = self._audio_plan(state, project_id, plan, args)
        plan.fingerprint = self._fingerprint(plan, args)
        return plan

    def _plan_storyboard(self, state, project_id, args, profile, engine, reference) -> RenderPlan:
        record = storyboard_in_state(state, project_id, args.storyboard_id)
        if args.expected_revision is not None and record.revision != args.expected_revision:
            raise AppError("VERSION_CONFLICT", "分镜记录已变化，请重新读取后再渲染。")
        scene = self._scene_for_storyboard(state, record.id)
        shots = self._collect_shots(state, project_id, record, scene.id if scene else None)
        missing = [shot.shot_id for shot in shots if shot.media_id is None]
        if missing:
            raise RenderError(
                "RENDER_SOURCE_MISSING", f"（镜头未交付：{', '.join(missing)}）", stage="source"
            )
        self._assign_timeline(state, project_id, record, shots)
        entries = self._entries(shots, record.id)
        planned_scene = PlannedScene(
            scene_id=scene.id if scene else None,
            storyboard_id=record.id,
            storyboard_version=record.current_version,
            storyboard_revision=record.revision,
            shots=shots,
        )
        return RenderPlan(
            project_id=project_id,
            scope="storyboard",
            storyboard_id=record.id,
            episode_id=scene.episode_id if scene is not None else None,
            storyboard_version=record.current_version,
            episode_version=None,
            revision=record.revision,
            scenes=[planned_scene],
            shots=shots,
            entries=entries,
            transitions={},
            title_cards=args.title_cards,
            effective=[],
            total=Fraction(0),
            profile=profile,
            mode=args.subtitle_mode,
            track=None,
            track_version=None,
            track_sha256=None,
            font=reference,
            engine=engine,
            audio=AudioPlan(),
            retimed=False,
            fingerprint="",
        )

    def _plan_episode(self, state, project_id, args, profile, engine, reference) -> RenderPlan:
        raw = state["episodes"].get(args.episode_id)
        if raw is None or raw["projectId"] != project_id:
            raise AppError("EPISODE_NOT_FOUND", "当前项目中没有这个分集，未渲染。")
        episode = Episode.model_validate(raw)
        if args.expected_revision is not None and episode.revision != args.expected_revision:
            raise AppError("VERSION_CONFLICT", "分集记录已变化，请重新读取后再渲染。")
        scenes: list[PlannedScene] = []
        entries: list[ShotEntry] = []
        all_shots: list[PlannedShot] = []
        offset = Fraction(0)
        for reference_scene in episode.latest.scenes:
            scene = Scene.model_validate(state["scenes"][reference_scene.scene_id])
            if scene.storyboard_id is None:
                raise RenderError(
                    "RENDER_SOURCE_MISSING",
                    f"（场次 {reference_scene.scene_id} 尚未绑定分镜）",
                    stage="source",
                )
            record = storyboard_in_state(state, project_id, scene.storyboard_id)
            shots = self._collect_shots(state, project_id, record, scene.id)
            self._assign_timeline(state, project_id, record, shots)
            scene_entries = self._entries(shots, record.id)
            for entry in scene_entries:
                entries.append(
                    ShotEntry(
                        shot_id=entry.shot_id,
                        storyboard_id=entry.storyboard_id,
                        scene_id=entry.scene_id,
                        start=offset + entry.start,
                        duration=entry.duration,
                        source=entry.source,
                    )
                )
            offset += sum(entry.duration for entry in scene_entries)
            missing = [shot.shot_id for shot in shots if shot.media_id is None]
            scenes.append(
                PlannedScene(
                    scene_id=scene.id,
                    storyboard_id=record.id,
                    storyboard_version=record.current_version,
                    storyboard_revision=record.revision,
                    shots=shots,
                    failure_code="RENDER_SOURCE_MISSING" if missing else None,
                    failure_stage="source" if missing else None,
                )
            )
            all_shots.extend(shots)
        if not entries:
            raise RenderError("RENDER_SOURCE_MISSING", "（分集没有已交付镜头）", stage="source")
        return RenderPlan(
            project_id=project_id,
            scope="episode",
            storyboard_id=None,
            episode_id=episode.id,
            storyboard_version=None,
            episode_version=episode.current_version,
            revision=episode.revision,
            scenes=scenes,
            shots=all_shots,
            entries=entries,
            transitions={},
            title_cards=args.title_cards,
            effective=[],
            total=Fraction(0),
            profile=profile,
            mode=args.subtitle_mode,
            track=None,
            track_version=None,
            track_sha256=None,
            font=reference,
            engine=engine,
            audio=AudioPlan(),
            retimed=False,
            fingerprint="",
        )

    # -- frozen sources --------------------------------------------------------
    @staticmethod
    def _scene_for_storyboard(state, storyboard_id):
        from vagent.video.subtitles import scene_for_storyboard

        return scene_for_storyboard(state, storyboard_id)

    def _collect_shots(self, state, project_id, record, scene_id) -> list[PlannedShot]:
        version = record.current_version
        rows: list[PlannedShot] = []
        for shot in sorted(record.versions[-1].shots, key=lambda item: item.order):
            shot_version = latest_shot_version(record, version, shot.id)
            media = job = None
            if shot_version is not None:
                job = state["jobs"].get(shot_version.job_id)
                media_id = shot_version.media_id
                if media_id is None and isinstance(job, dict):
                    refs = (job.get("result") or {}).get("mediaRefs") or []
                    media_id = refs[0].get("mediaId") if refs else None
                if media_id is not None:
                    raw_media = state["media"].get(media_id)
                    if raw_media is not None:
                        try:
                            parsed = parse_media(raw_media)
                        except Exception:
                            parsed = None
                        if (
                            parsed is not None
                            and not isinstance(parsed, (FilmMedia, AudioMedia))
                            and parsed.project_id == project_id
                            and job is not None
                            and job.get("status") == "succeeded"
                            and parsed.metadata.video_codec == "h264"
                            and (job.get("result") or {}).get("mediaRefs", [{}])[0].get("mediaId")
                            == parsed.id
                        ):
                            media = parsed
            rows.append(
                PlannedShot(
                    shot_id=shot.id,
                    shot_version_id=shot_version.id
                    if shot_version is not None and media is not None
                    else None,
                    job_id=shot_version.job_id if shot_version is not None and media is not None else None,
                    media_id=media.id if media is not None else None,
                    relative_path=media.relative_path if media is not None else None,
                    sha256=media.sha256 if media is not None else None,
                    size_bytes=media.size_bytes if media is not None else None,
                    start=Fraction(0),
                    duration=Fraction(shot.duration_seconds),
                    scene_id=scene_id,
                )
            )
        return rows

    def _assign_timeline(self, state, project_id, record, shots, *, use_film=True) -> None:
        """Use the assembled film's exact timeline when it still matches the shots."""
        segment_by_shot = {}
        if use_film:
            film = find_film(state, project_id, record.id)
            if film is not None:
                base = next((item for item in reversed(film.versions) if item.render is None), None)
                if base is not None and [item.shot_id for item in base.inputs] == [
                    shot.shot_id for shot in shots
                ]:
                    segment_by_shot = {item.shot_id: item for item in base.assembly.timeline}
        if segment_by_shot:
            # Film segment starts are rounded to microseconds; rebuild them from the
            # exact durations so the render timeline is contiguous and can match a
            # subtitle track saved from the same film.
            offset = Fraction(0)
            for shot in shots:
                segment = segment_by_shot.get(shot.shot_id)
                shot.start = offset
                if segment is not None:
                    shot.duration = segment.duration.fraction()
                offset += shot.duration
            return
        offset = Fraction(0)
        for shot in shots:
            shot.start = offset
            offset += shot.duration

    @staticmethod
    def _entries(shots: list[PlannedShot], storyboard_id: str) -> list[ShotEntry]:
        return [
            ShotEntry(
                shot_id=shot.shot_id,
                storyboard_id=storyboard_id,
                scene_id=shot.scene_id,
                start=shot.start,
                duration=shot.duration,
            )
            for shot in shots
        ]

    def _resolve_track(self, state, project_id, args: FilmRenderArgs, entries: list[ShotEntry]):
        track = None
        if args.track_id is not None:
            track = track_by_id(state, args.track_id, project_id)
        elif args.scope == "storyboard":
            track = find_track(state, project_id, "storyboard", args.storyboard_id)
        else:
            track = find_track(state, project_id, "episode", args.episode_id)
        if track is None:
            if args.subtitle_mode != "soft":
                raise RenderError("SUBTITLE_TRACK_MISSING", "", stage="subtitle")
            return None, None, None
        target = args.storyboard_id if args.scope == "storyboard" else args.episode_id
        if (
            track.scope != args.scope
            or (args.scope == "storyboard" and track.storyboard_id != target)
            or (args.scope == "episode" and track.episode_id != target)
        ):
            raise RenderError("SUBTITLE_TRACK_MISSING", "（字幕轨不属于这个渲染目标）", stage="subtitle")
        version = (
            next((item for item in track.versions if item.version == args.track_version), None)
            if args.track_version is not None
            else track.versions[-1]
        )
        if version is None:
            raise RenderError("SUBTITLE_TRACK_MISSING", "", stage="subtitle")
        if len(version.timeline) != len(entries):
            raise RenderError("RENDER_TIMELINE_MISMATCH", "", stage="subtitle")
        for frozen_entry, entry in zip(version.timeline, entries, strict=True):
            if (
                frozen_entry.shot_id != entry.shot_id
                or frozen_entry.start.fraction() != entry.start
                or frozen_entry.duration.fraction() != entry.duration
            ):
                raise RenderError("RENDER_TIMELINE_MISMATCH", "", stage="subtitle")
        if not version.cues:
            if args.subtitle_mode != "soft":
                raise RenderError("SUBTITLE_TRACK_MISSING", "（字幕版本没有 cue）", stage="subtitle")
            return None, None, None
        return track, version, track_version_sha256(version)

    # -- audio planning --------------------------------------------------------
    def _audio_plan(self, state, project_id, plan: RenderPlan, args: FilmRenderArgs) -> AudioPlan:
        result = AudioPlan()
        lines = self._dialogue_lines(state, plan)
        voice_assets = self._voice_assets(state, project_id, plan)
        cues = list(plan.track_version.cues) if plan.track_version is not None else []
        cue_starts = self._cue_starts(lines, cues)
        used: set[str] = set()
        for index, line in enumerate(lines):
            asset = next(
                (
                    item
                    for item in voice_assets
                    if item.id not in used
                    and item.versions[-1].source.text == line[0]
                    and (item.versions[-1].source.speaker or NARRATOR_SPEAKER)
                    == (line[1] or NARRATOR_SPEAKER)
                ),
                None,
            )
            if asset is None:
                continue
            used.add(asset.id)
            cue = cue_starts.get(index)
            start = self._effective_cue_start(cue, plan) if cue is not None else Fraction(0)
            version = asset.versions[-1]
            media = self._audio_media(state, version)
            result.tracks.append(
                MixTrack(
                    audio_id=asset.id,
                    audio_version=version.version,
                    kind="voice",
                    path=media[0],
                    duration_seconds=version.duration_seconds,
                    start_seconds=start,
                    gain_db=0.0,
                    ducking=False,
                    sha256=version.sha256,
                )
            )
            result.assets.append(asset.id)
            result.rendered.append(
                RenderAudioTrack(
                    audio_id=asset.id,
                    audio_version=version.version,
                    kind="voice",
                    gain_db=0.0,
                    start=Rational.of(start),
                    ducking=False,
                    voice_binding=self._voice_binding(version.source),
                )
            )
        for asset in self._imported_assets(state, project_id):
            version = asset.versions[-1]
            media = self._audio_media(state, version)
            gain = -18.0 if asset.kind == "bgm" else -6.0
            ducking = asset.kind == "bgm"
            result.tracks.append(
                MixTrack(
                    audio_id=asset.id,
                    audio_version=version.version,
                    kind=asset.kind,
                    path=media[0],
                    duration_seconds=version.duration_seconds,
                    start_seconds=Fraction(0),
                    gain_db=gain,
                    ducking=ducking,
                    sha256=version.sha256,
                )
            )
            result.assets.append(asset.id)
            result.rendered.append(
                RenderAudioTrack(
                    audio_id=asset.id,
                    audio_version=version.version,
                    kind=asset.kind,
                    gain_db=gain,
                    start=Rational.of(Fraction(0)),
                    ducking=ducking,
                )
            )
        if len(result.tracks) > MAX_RENDER_AUDIO_TRACKS:
            raise RenderError("RENDER_SOURCE_MISSING", "（音频轨超过上限）", stage="audio")
        return result

    @staticmethod
    def _dialogue_lines(state, plan: RenderPlan):
        from vagent.video.episodes import Scene

        lines: list[tuple[str, str | None]] = []
        if plan.scope == "episode" and plan.episode_id is not None:
            episode = state["episodes"][plan.episode_id]
            for reference in episode["versions"][-1]["scenes"]:
                scene = Scene.model_validate(state["scenes"][reference["sceneId"]])
                frozen = next(item for item in scene.versions if item.version == reference["sceneVersion"])
                lines.extend((line.text, line.speaker) for line in frozen.dialogue)
        elif plan.storyboard_id is not None:
            scene = RenderService._scene_for_storyboard(state, plan.storyboard_id)
            if scene is not None:
                lines.extend((line.text, line.speaker) for line in scene.latest.dialogue)
        return lines

    @staticmethod
    def _voice_assets(state, project_id, plan: RenderPlan):
        episode_id = plan.episode_id
        assets = []
        for raw in state["audio"].values():
            asset = AudioAsset.model_validate(raw)
            if asset.project_id != project_id or asset.kind != "voice":
                continue
            source = asset.versions[-1].source
            if not isinstance(source, ProviderAudioSource):
                continue
            if episode_id is not None and source.episode_id != episode_id:
                continue
            assets.append(asset)
        return assets

    @staticmethod
    def _imported_assets(state, project_id):
        assets = []
        for raw in state["audio"].values():
            asset = AudioAsset.model_validate(raw)
            if asset.project_id == project_id and asset.kind in {"bgm", "sfx"}:
                assets.append(asset)
        return sorted(assets, key=lambda item: (item.kind, item.created_at, item.id))

    def _audio_media(self, state, version) -> tuple[Path, str]:
        raw = state["media"].get(version.media_id)
        if raw is None or raw.get("kind") != "audio":
            raise RenderError("AUDIO_ASSET_MISSING", "（音频媒体缺失）", stage="audio")
        media = AudioMedia.model_validate(raw)
        if media.sha256 != version.sha256 or media.size_bytes != version.size_bytes:
            raise RenderError("AUDIO_ASSET_MISSING", "（音频媒体与版本不一致）", stage="audio")
        path = self.store.home / media.relative_path
        try:
            lease = self.files.verified(media.relative_path, media)
            lease.close()
        except (OSError, FileNotFoundError, AppError):
            raise RenderError("RENDER_SOURCE_MISSING", "（音频文件缺失）", stage="audio") from None
        return path, media.relative_path

    @staticmethod
    def _voice_binding(source: ProviderAudioSource) -> dict:
        return {
            "provider": source.provider,
            "model": source.model,
            "voiceId": source.voice_id,
            "speed": source.speed,
        }

    @staticmethod
    def _cue_starts(lines, cues) -> dict[int, SubtitleCue]:
        """Map each dialogue line to the cue covering its first character.

        The subtitle splitter keeps every character, so the concatenated cue text is
        the dialogue stream with each line's outer whitespace removed. Positions are
        therefore exact and do not depend on punctuation heuristics.
        """
        dialogue = "".join(text.strip() for text, _speaker in lines)
        windows = []
        cursor = 0
        for cue in cues:
            windows.append((cursor, cursor + len(cue.text)))
            cursor += len(cue.text)
        result: dict[int, SubtitleCue] = {}
        search_from = 0
        for index, (text, _speaker) in enumerate(lines):
            stripped = text.strip()
            position = dialogue.find(stripped, search_from)
            if position < 0:
                continue
            search_from = position + len(stripped)
            for cue, (low, high) in zip(cues, windows, strict=True):
                if low <= position < high:
                    result[index] = cue
                    break
        return result

    @staticmethod
    def _effective_cue_start(cue: SubtitleCue, plan: RenderPlan) -> Fraction:
        index = next(
            (number for number, entry in enumerate(plan.entries) if entry.shot_id == cue.shot_id),
            0,
        )
        source = Fraction(cue.start.total_microseconds(), 1_000_000)
        target = plan.effective[index]
        mapped = target.start + (source - plan.entries[index].start)
        return min(max(mapped, target.start), target.start + target.duration)

    # -- fingerprint / replay --------------------------------------------------
    @staticmethod
    def _fingerprint(plan: RenderPlan, args: FilmRenderArgs) -> str:
        payload = {
            "renderVersion": RENDER_VERSION,
            "scope": plan.scope,
            "projectId": plan.project_id,
            "storyboardId": plan.storyboard_id,
            "episodeId": plan.episode_id,
            "storyboardVersion": plan.storyboard_version,
            "episodeVersion": plan.episode_version,
            "shots": [
                {
                    "shotId": shot.shot_id,
                    "shotVersionId": shot.shot_version_id,
                    "mediaId": shot.media_id,
                    "sha256": shot.sha256,
                    "start": format_seconds(shot.start),
                    "duration": format_seconds(shot.duration),
                }
                for shot in plan.shots
            ],
            "trackId": plan.track.id if plan.track else None,
            "trackVersion": plan.track_version.version if plan.track_version else None,
            "trackSha256": plan.track_sha256,
            "profile": plan.profile,
            "subtitleMode": plan.mode,
            "transitions": [
                {
                    "afterShotId": shot_id,
                    "type": kind,
                    "duration": format_seconds(duration),
                }
                for shot_id, (kind, duration) in sorted(
                    plan.transitions.items(),
                    key=lambda item: [shot.shot_id for shot in plan.shots].index(item[0]),
                )
            ],
            "titleCards": [card.model_dump(mode="json", by_alias=True) for card in plan.title_cards],
            "audio": [
                {
                    "audioId": track.audio_id,
                    "audioVersion": track.audio_version,
                    "kind": track.kind,
                    "start": format_seconds(track.start_seconds),
                    "sha256": track.sha256,
                }
                for track in plan.audio.tracks
            ],
            "engine": {
                "version": plan.engine.ffmpeg_version,
                "binarySha256": plan.engine.binary_sha256,
            },
            "font": plan.font.model_dump(mode="json", by_alias=True),
            "mixVersion": MIX_VERSION,
        }
        return render_fingerprint(payload)

    @staticmethod
    def _replay(state: dict, plan: RenderPlan) -> dict | None:
        film = (
            find_film(state, plan.project_id, plan.storyboard_id)
            if plan.storyboard_id is not None
            else find_episode_film(state, plan.project_id, plan.episode_id)
        )
        if film is None:
            return None
        match = next(
            (
                item
                for item in reversed(film.versions)
                if item.render is not None and item.render.fingerprint == plan.fingerprint
            ),
            None,
        )
        if match is None:
            return None
        task = next(
            (
                RenderTask.model_validate(raw)
                for raw in state.get("renders", {}).values()
                if raw["projectId"] == plan.project_id
                and raw["scope"] == plan.scope
                and (
                    raw.get("storyboardId") == plan.storyboard_id
                    if plan.scope == "storyboard"
                    else raw.get("episodeId") == plan.episode_id
                )
                and raw["fingerprint"] == plan.fingerprint
                and raw["status"] == "succeeded"
            ),
            None,
        )
        result = {
            "replay": True,
            "renderId": task.id if task is not None and task.film_version == match.version else None,
            "status": "succeeded",
            "scope": plan.scope,
            "storyboardId": plan.storyboard_id,
            "episodeId": plan.episode_id,
            "profile": plan.profile["name"],
            "subtitleMode": plan.mode,
            "estimatedSeconds": task.estimated_seconds if task is not None else 0,
            "scenes": (
                [scene.model_dump(mode="json", by_alias=True) for scene in task.scenes]
                if task is not None
                else []
            ),
            "filmId": film.id,
            "filmVersion": match.version,
            "fingerprint": plan.fingerprint,
            "film": film_view(state, film),
        }
        return result

    # -- task persistence ------------------------------------------------------
    def _insert_task(self, task: RenderTask) -> None:
        def mutate(draft):
            draft["renders"][task.id] = task.model_dump(mode="json", by_alias=True)

        self.store.transaction(mutate)

    def _mark_scene(self, task: RenderTask, index: int, scene: RenderSceneProgress) -> None:
        def mutate(draft):
            raw = draft["renders"][task.id]
            current = RenderTask.model_validate(raw)
            scenes = list(current.scenes)
            scenes[index] = scene
            updated = current.model_copy(
                update={"scenes": tuple(scenes), "revision": current.revision + 1, "updated_at": now()}
            )
            draft["renders"][task.id] = updated.model_dump(mode="json", by_alias=True)

        self.store.transaction(mutate)

    def _update_task(self, task: RenderTask, **changes) -> RenderTask:
        def mutate(draft):
            raw = draft["renders"][task.id]
            current = RenderTask.model_validate(raw)
            updated = current.model_copy(
                update={**changes, "revision": current.revision + 1, "updated_at": now()}
            )
            draft["renders"][task.id] = updated.model_dump(mode="json", by_alias=True)
            return updated

        return self.store.transaction(mutate)

    def _fail_task(self, task: RenderTask, plan: RenderPlan, error: BaseException) -> None:
        try:
            raw = self.store.snapshot()["renders"].get(task.id)
            if raw is None:
                return
            current = RenderTask.model_validate(raw)
            if current.status != "running":
                return
            code = error.code if isinstance(error, AppError) else "RENDER_ENCODE_FAILED"
            stage = getattr(error, "stage", None) or "encode"
            self._update_task(
                current,
                status="failed",
                failure_stage=stage,
                error_code=code,
            )
        except BaseException:
            # A failed progress write must not mask the original render error.
            pass

    # -- production ------------------------------------------------------------
    async def _produce(
        self, plan: RenderPlan, task: RenderTask, args_model: FilmRenderArgs, operation_key: str
    ) -> dict:
        work = Path(tempfile.mkdtemp(prefix="render-", dir=self._work_root()))
        loop = asyncio.get_running_loop()
        deadline = loop.time() + RENDER_JOB_TIMEOUT_SECONDS
        media_id = str(uuid4())
        final_relative = film_media_path(plan.project_id, media_id)
        part_relative = final_relative + ".part"
        output = self._part_path(part_relative)
        try:
            (work / "fonts").mkdir()
            shutil.copyfile(bundled_font_path(), work / "fonts" / "subtitle-font.ttf")
            scene_paths: list[tuple[Path, Fraction]] = []
            failed: list[tuple[int, RenderError]] = []
            for index, scene in enumerate(plan.scenes):
                if scene.failure_code is not None:
                    failed.append(
                        (
                            index,
                            RenderError(scene.failure_code, "", stage=scene.failure_stage or "source"),
                        )
                    )
                    continue
                self._mark_scene(
                    task,
                    index,
                    RenderSceneProgress(
                        scene_id=scene.scene_id,
                        storyboard_id=scene.storyboard_id,
                        shot_ids=scene.shot_ids,
                        status="running",
                    ),
                )
                try:
                    path, duration = await self._render_scene(
                        plan, scene, index, work, deadline, engine=plan.engine
                    )
                except RenderError as error:
                    self._mark_scene(
                        task,
                        index,
                        RenderSceneProgress(
                            scene_id=scene.scene_id,
                            storyboard_id=scene.storyboard_id,
                            shot_ids=scene.shot_ids,
                            status="failed",
                            failure_stage=error.stage,
                            error_code=error.code,
                        ),
                    )
                    failed.append((index, error))
                    continue
                scene_paths.append((path, duration))
                self._mark_scene(
                    task,
                    index,
                    RenderSceneProgress(
                        scene_id=scene.scene_id,
                        storyboard_id=scene.storyboard_id,
                        shot_ids=scene.shot_ids,
                        status="succeeded",
                        duration_seconds=float(duration),
                    ),
                )
            if failed:
                raise failed[0][1]
            mixed = None
            if plan.audio.tracks:
                try:
                    mixed = await mix_tracks(
                        tracks=plan.audio.tracks,
                        output=work / "mixed.wav",
                        duration_seconds=plan.total,
                        engine=plan.engine,
                        work_root=work,
                    )
                except MixError as error:
                    raise RenderError(error.code, f"（{error}）", stage=error.stage) from None
            derived = (
                remap_cues(list(plan.track_version.cues), plan.entries, plan.effective)
                if plan.track_version is not None
                else []
            )
            produced = await self._render_final(
                plan,
                args_model,
                work,
                output,
                scene_paths,
                mixed,
                derived,
                deadline,
            )
            produced.update(
                {
                    "media_id": media_id,
                    "final_relative": final_relative,
                    "part_relative": part_relative,
                    "mixed": mixed,
                    "derived_cues": derived,
                }
            )
            return produced
        except BaseException:
            output.unlink(missing_ok=True)
            raise
        finally:
            shutil.rmtree(work, ignore_errors=True)

    async def _render_scene(
        self,
        plan: RenderPlan,
        scene: PlannedScene,
        index: int,
        work: Path,
        deadline: float,
        *,
        engine: Engine,
    ) -> tuple[Path, Fraction]:
        scene_dir = work / f"scene-{index}"
        scene_dir.mkdir()
        paths = []
        for shot in scene.shots:
            if shot.relative_path is None:
                raise RenderError("RENDER_SOURCE_MISSING", "（镜头媒体缺失）", stage="source")
            path = self.store.home / shot.relative_path
            try:
                lease = self.files.verified(
                    shot.relative_path,
                    SimpleNamespace(size_bytes=shot.size_bytes, sha256=shot.sha256),
                )
                lease.close()
            except (OSError, FileNotFoundError, AppError):
                raise RenderError("RENDER_SOURCE_MISSING", "（镜头文件缺失或损坏）", stage="source") from None
            paths.append(path)
        duration = self._scene_duration(plan, scene)
        active = self._scene_transitions(plan, scene)
        command, output = self._scene_command(plan, scene, paths, active, duration, scene_dir)
        await self._run(engine, scene_dir, command, output, deadline=deadline, cancelled=None, strict=True)
        streams = await self._probe(engine, output, deadline)
        if streams.video is None or streams.duration_seconds is None:
            raise RenderError("RENDER_VALIDATION_FAILED", "（逐场产物无法解析）", stage="validate")
        if (streams.video["width"], streams.video["height"]) != (
            plan.profile["width"],
            plan.profile["height"],
        ):
            raise RenderError("RENDER_VALIDATION_FAILED", "（逐场分辨率与档位不一致）", stage="validate")
        if abs(streams.duration_seconds - float(duration)) > 1 / 30 + 0.05:
            raise RenderError("RENDER_VALIDATION_FAILED", "（逐场时长与时间线不一致）", stage="validate")
        return output, duration

    @staticmethod
    def _scene_duration(plan: RenderPlan, scene: PlannedScene) -> Fraction:
        shots = {shot.shot_id: shot for shot in scene.shots}
        durations = [shot.duration for shot in scene.shots]
        total = sum(durations, Fraction(0))
        for shot_id, (_kind, duration) in plan.transitions.items():
            if shot_id in shots and shot_id != scene.shots[-1].shot_id:
                total -= duration
        return total

    @staticmethod
    def _scene_transitions(plan: RenderPlan, scene: PlannedScene) -> dict[str, tuple[str, Fraction]]:
        shots = {shot.shot_id for shot in scene.shots}
        last = scene.shots[-1].shot_id
        return {
            shot_id: value
            for shot_id, value in plan.transitions.items()
            if shot_id in shots and shot_id != last
        }

    # -- FFmpeg commands -------------------------------------------------------
    def _normalize_filter(self, index: int, shot: PlannedShot, plan: RenderPlan) -> str:
        width, height = plan.profile["width"], plan.profile["height"]
        duration = format_seconds(shot.duration)
        return (
            f"[{index}:v]scale={width}:{height}:force_original_aspect_ratio=decrease,"
            f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2,setsar=1,fps={plan.profile['fps']},"
            f"format={plan.profile['pixFmt']},tpad=stop_mode=clone:stop_duration={duration},"
            f"trim=duration={duration},setpts=PTS-STARTPTS,fps={plan.profile['fps']},settb=AVTB[v{index}]"
        )

    def _scene_command(
        self,
        plan: RenderPlan,
        scene: PlannedScene,
        paths: list[Path],
        active: dict[str, tuple[str, Fraction]],
        duration: Fraction,
        work: Path,
    ) -> tuple[list[str], Path]:
        output = work / "scene.mp4"
        filters = [self._normalize_filter(index, shot, plan) for index, shot in enumerate(scene.shots)]
        labels = [f"v{index}" for index in range(len(scene.shots))]
        groups: list[str] = []
        current = labels[0]
        current_duration = scene.shots[0].duration
        for number in range(1, len(scene.shots)):
            shot = scene.shots[number]
            transition = active.get(scene.shots[number - 1].shot_id)
            if transition is None:
                groups.append(current)
                current = labels[number]
                current_duration = shot.duration
                continue
            kind, seconds = transition
            label = labels[number]
            if kind == "fade":
                outgoing = f"fade{number}"
                start = current_duration - seconds
                filters.append(
                    f"[{current}]fade=t=out:st={format_seconds(start)}:d={format_seconds(seconds)}[{outgoing}]"
                )
                current = outgoing
            offset = current_duration - seconds
            combined = f"xf{number}"
            filters.append(
                f"[{current}][{label}]xfade=transition=fade:duration={format_seconds(seconds)}:"
                f"offset={format_seconds(offset)}[{combined}]"
            )
            current = combined
            current_duration = current_duration + shot.duration - seconds
        groups.append(current)
        if len(groups) == 1:
            joined = groups[0]
        else:
            joined = "joined"
            filters.append(
                "".join(f"[{label}]" for label in groups) + f"concat=n={len(groups)}:v=1:a=0[{joined}]"
            )
        filters.append(f"[{joined}]null[outv]")
        command = [str(plan.engine.path), "-hide_banner", "-nostdin", "-loglevel", "warning", "-nostats"]
        for path in paths:
            command += ["-i", str(path)]
        command += [
            "-filter_complex",
            ";".join(filters),
            "-map",
            "[outv]",
            "-an",
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-crf",
            "18",
            "-profile:v",
            "high",
            "-level:v",
            "4.0",
            "-pix_fmt",
            plan.profile["pixFmt"],
            "-fps_mode",
            "cfr",
            "-r",
            str(plan.profile["fps"]),
            "-t",
            format_seconds(duration),
            "-movflags",
            "+faststart",
            "-f",
            "mp4",
            "-y",
            str(output),
        ]
        return command, output

    async def _render_final(
        self,
        plan: RenderPlan,
        args: FilmRenderArgs,
        work: Path,
        output: Path,
        scenes: list[tuple[Path, Fraction]],
        mixed,
        derived: list[SubtitleCue],
        deadline: float,
    ) -> dict:
        filters = [
            f"[{index}:v]setpts=PTS-STARTPTS,fps={plan.profile['fps']},settb=AVTB[s{index}]"
            for index in range(len(scenes))
        ]
        labels = [f"s{index}" for index in range(len(scenes))]
        # Cross-scene boundaries are the last shot of every scene but the last one.
        boundary = []
        for index, scene in enumerate(plan.scenes):
            if index == len(plan.scenes) - 1:
                continue
            transition = plan.transitions.get(scene.shots[-1].shot_id)
            boundary.append(transition)
        groups: list[str] = []
        current = labels[0] if labels else "joined"
        current_duration = scenes[0][1] if scenes else Fraction(0)
        for index in range(1, len(scenes)):
            transition = boundary[index - 1]
            label = labels[index]
            if transition is None:
                groups.append(current)
                current = label
                current_duration = scenes[index][1]
                continue
            kind, seconds = transition
            if kind == "fade":
                outgoing = f"cfade{index}"
                filters.append(
                    f"[{current}]fade=t=out:st={format_seconds(current_duration - seconds)}:"
                    f"d={format_seconds(seconds)}[{outgoing}]"
                )
                current = outgoing
            offset = current_duration - seconds
            combined = f"cxf{index}"
            filters.append(
                f"[{current}][{label}]xfade=transition=fade:duration={format_seconds(seconds)}:"
                f"offset={format_seconds(offset)}[{combined}]"
            )
            current = combined
            current_duration = current_duration + scenes[index][1] - seconds
        groups.append(current)
        if len(groups) == 1:
            joined = groups[0]
        else:
            joined = "sceneconcat"
            filters.append(
                "".join(f"[{label}]" for label in groups) + f"concat=n={len(groups)}:v=1:a=0[{joined}]"
            )
        title_label = joined
        for number, (start, end, card) in enumerate(validate_title_cards(plan.title_cards, plan.total)):
            text_file = f"title-{number}.txt"
            (work / text_file).write_text(card.text, encoding="utf-8", newline="\n")
            target = f"title{number}"
            size = 48 if plan.profile["width"] == 1280 else 72
            filters.append(
                f"[{title_label}]drawtext=fontfile=fonts/subtitle-font.ttf:textfile={text_file}:"
                f"fontcolor=white:fontsize={size}:box=1:boxcolor=black@0.45:boxborderw=24:"
                f"x=(w-text_w)/2:y=(h-text_h)/2:enable='between(t,{format_seconds(start)},{format_seconds(end)})'"
                f"[{target}]"
            )
            title_label = target
        video_label = title_label
        if args.subtitle_mode in {"burn", "both"}:
            filters.append(f"[{video_label}]subtitles=sub.ass:fontsdir=fonts[burned]")
            video_label = "burned"
        filters.append(f"[{video_label}]null[finalv]")
        srt_bytes = render_srt(derived).encode("utf-8") if derived else b""
        ass_text = render_ass(derived, width=plan.profile["width"], height=plan.profile["height"])
        if plan.track_version is not None:
            (work / "sub.srt").write_bytes(srt_bytes)
            (work / "sub.ass").write_text(ass_text, encoding="utf-8", newline="\n")
        command = [str(plan.engine.path), "-hide_banner", "-nostdin", "-loglevel", "warning", "-nostats"]
        for path, _duration in scenes:
            command += ["-i", str(path)]
        audio_index = len(scenes)
        if mixed is not None:
            command += ["-i", str(work / "mixed.wav")]
        else:
            command += [
                "-f",
                "lavfi",
                "-i",
                "anullsrc=channel_layout=stereo:sample_rate=48000",
            ]
        subtitle_index = len(scenes) + 1
        if plan.track_version is not None and args.subtitle_mode in {"soft", "both"}:
            command += ["-i", "sub.srt"]
        command += ["-filter_complex", ";".join(filters), "-map", "[finalv]", "-map", f"{audio_index}:a"]
        if plan.track_version is not None and args.subtitle_mode in {"soft", "both"}:
            command += ["-map", f"{subtitle_index}:0", "-c:s", "mov_text", "-metadata:s:s:0", "language=zho"]
            command += ["-disposition:s:0", "0"]
        command += [
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-crf",
            str(plan.profile["crf"]),
            "-profile:v",
            "high",
            "-level:v",
            "4.0",
            "-pix_fmt",
            plan.profile["pixFmt"],
            "-fps_mode",
            "cfr",
            "-r",
            str(plan.profile["fps"]),
            "-c:a",
            "aac",
            "-b:a",
            plan.profile["audioBitrate"],
            "-ar",
            str(plan.profile["sampleRate"]),
            "-ac",
            str(plan.profile["channels"]),
            "-t",
            format_seconds(plan.total),
            "-movflags",
            "+faststart",
            "-f",
            "mp4",
            "-y",
            str(output),
        ]
        await self._run(plan.engine, work, command, output, deadline=deadline, cancelled=None, strict=True)
        try:
            size = output.stat().st_size
            with output.open("rb") as handle:
                metadata = inspect_mp4(handle, size)
        except (OSError, ValueError):
            raise RenderError("RENDER_VALIDATION_FAILED", "（MP4 结构无效）", stage="validate") from None
        streams = await self._probe(plan.engine, output, deadline)
        self._validate(plan, args, streams, metadata)
        digest = _hash_file(output)
        return {
            "size_bytes": size,
            "sha256": digest,
            "metadata": metadata,
            "streams": streams,
            "sidecar_sha256": hashlib.sha256(srt_bytes).hexdigest()
            if plan.track_version is not None and args.subtitle_mode in {"soft", "both"}
            else None,
        }

    @staticmethod
    def _validate(plan: RenderPlan, args: FilmRenderArgs, streams: ProbedStreams, metadata) -> None:
        if streams.video is None:
            raise RenderError("RENDER_VALIDATION_FAILED", "（缺少视频轨）", stage="validate")
        if streams.video["codec"] != "h264":
            raise RenderError("RENDER_VALIDATION_FAILED", "（视频编码不是 H.264）", stage="validate")
        if (streams.video["width"], streams.video["height"]) != (
            plan.profile["width"],
            plan.profile["height"],
        ):
            raise RenderError("RENDER_VALIDATION_FAILED", "（分辨率与档位不一致）", stage="validate")
        if streams.video["fps"] is None or abs(streams.video["fps"] - plan.profile["fps"]) > 0.05:
            raise RenderError("RENDER_VALIDATION_FAILED", "（帧率与档位不一致）", stage="validate")
        if streams.audio is None or streams.audio["codec"] != "aac":
            raise RenderError("RENDER_VALIDATION_FAILED", "（缺少 AAC 音轨）", stage="validate")
        if streams.audio["sample_rate"] != plan.profile["sampleRate"]:
            raise RenderError("RENDER_VALIDATION_FAILED", "（采样率与档位不一致）", stage="validate")
        if streams.audio["channels"] != plan.profile["channels"]:
            raise RenderError("RENDER_VALIDATION_FAILED", "（声道数与档位不一致）", stage="validate")
        if metadata.video_codec != "h264" or not metadata.has_audio:
            raise RenderError("RENDER_VALIDATION_FAILED", "（MP4 结构缺少视频或音频）", stage="validate")
        mov_text = [item for item in streams.subtitles if item["codec"] == "mov_text"]
        if args.subtitle_mode in {"soft", "both"} and plan.track_version is not None and not mov_text:
            raise RenderError("RENDER_VALIDATION_FAILED", "（缺少 mov_text 字幕轨）", stage="validate")
        if args.subtitle_mode == "burn" and streams.subtitles:
            raise RenderError("RENDER_VALIDATION_FAILED", "（烧录输出不应再有字幕轨）", stage="validate")
        measured = streams.duration_seconds
        if measured is None:
            raise RenderError("RENDER_VALIDATION_FAILED", "（读不到成片时长）", stage="validate")
        if abs(measured - float(plan.total)) > 1 / 30 + 0.05:
            raise RenderError("RENDER_TIMELINE_MISMATCH", "（成片时长与时间线不一致）", stage="validate")
        if abs(metadata.duration_seconds - float(plan.total)) > 1 / 30 + 0.05:
            raise RenderError("RENDER_TIMELINE_MISMATCH", "（MP4 时长与时间线不一致）", stage="validate")

    # -- FFmpeg execution ------------------------------------------------------
    async def _run(
        self,
        engine: Engine,
        work: Path,
        command: list[str],
        output: Path,
        *,
        deadline: float,
        cancelled,
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
            raise RenderError("RENDER_ENCODE_FAILED", "（渲染已停止）", stage="encode")
        if loop.time() >= limit:
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
            raise RenderError("RENDER_VALIDATION_FAILED", "（无法读取渲染文件）", stage="validate") from None
        try:
            _stdout, stderr = await asyncio.wait_for(
                process.communicate(),
                timeout=max(1.0, min(30.0, deadline - asyncio.get_running_loop().time())),
            )
        except TimeoutError:
            await _terminate(process)
            raise RenderError("RENDER_TIMEOUT", "（读取渲染文件超时）", stage="validate") from None
        return parse_streams((stderr or b"").decode("utf-8", "replace"))

    # -- commit ----------------------------------------------------------------
    def _commit(
        self,
        draft: dict,
        *,
        plan: RenderPlan,
        task: RenderTask,
        produced: dict,
        args_model: FilmRenderArgs,
    ) -> dict:
        part = Path(self.store.home / produced["part_relative"])
        try:
            size = part.stat().st_size
            digest = _hash_file(part)
        except OSError:
            raise RenderError("RENDER_ENCODE_FAILED", "（渲染产物丢失）", stage="encode") from None
        if (size, digest) != (produced["size_bytes"], produced["sha256"]):
            raise RenderError("RENDER_VALIDATION_FAILED", "（渲染产物校验值不一致）", stage="validate")
        for shot in plan.shots:
            raw = draft["media"].get(shot.media_id)
            if raw is None or raw.get("sha256") != shot.sha256 or raw.get("sizeBytes") != shot.size_bytes:
                raise RenderError("RENDER_SOURCE_MISSING", "（渲染期间镜头媒体被替换）", stage="source")
        try:
            self.files.promote(produced["final_relative"])
        except FileExistsError:
            raise RenderError(
                "RENDER_VALIDATION_FAILED", "（目标成片文件已存在）", stage="validate"
            ) from None
        timestamp = now()
        streams = produced["streams"]
        mixed = produced["mixed"]
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
            audio=RenderAudioFacts(
                codec=streams.audio["codec"],
                sample_rate=streams.audio["sample_rate"],
                channels=streams.audio["channels"],
            )
            if streams.audio is not None
            else None,
        )
        subtitle = None
        sources_subtitle = None
        if plan.track is not None and plan.track_version is not None:
            subtitle = RenderSubtitle(
                track_id=plan.track.id,
                track_version=plan.track_version.version,
                track_sha256=plan.track_sha256,
                mode=args_model.subtitle_mode,
                font_ref=plan.font,
                sidecar_sha256=produced["sidecar_sha256"],
                retimed=plan.retimed,
            )
            sources_subtitle = RenderSubtitleSource(
                track_id=plan.track.id,
                track_version=plan.track_version.version,
                track_sha256=plan.track_sha256,
            )
        loudness = RenderLoudness()
        if mixed is not None:
            loudness = RenderLoudness(
                input=mixed.loudness_input,
                output={
                    **mixed.loudness_output,
                    "verification": mixed.loudness_verification,
                },
            )
        configuration_sha = canonical_sha256(
            {
                "renderVersion": RENDER_VERSION,
                "profile": plan.profile,
                "binarySha256": plan.engine.binary_sha256,
                "transitions": [
                    {
                        "afterShotId": shot_id,
                        "type": kind,
                        "duration": format_seconds(duration),
                    }
                    for shot_id, (kind, duration) in plan.transitions.items()
                ],
                "titleCards": [card.model_dump(mode="json", by_alias=True) for card in plan.title_cards],
                "mixVersion": MIX_VERSION,
            }
        )
        render = FilmRender(
            export_profile=profile_for(args_model.subtitle_mode, name=args_model.profile),
            engine=RenderEngine(
                version=plan.engine.ffmpeg_version,
                configuration_sha256=configuration_sha,
            ),
            subtitle=subtitle,
            audio=RenderAudio(
                tracks=tuple(plan.audio.rendered),
                ducking=plan.audio.ducking,
                loudness=loudness,
            ),
            transitions=tuple(
                RenderTransition(
                    after_shot_id=shot_id,
                    type=kind,
                    duration=Rational.of(duration),
                )
                for shot_id, (kind, duration) in sorted(
                    plan.transitions.items(),
                    key=lambda item: [shot.shot_id for shot in plan.shots].index(item[0]),
                )
            ),
            title_cards=tuple(
                RenderTitleCard(
                    text=card.text,
                    start=Rational.microseconds(card.start_ms * 1000),
                    duration=Rational.microseconds(card.duration_ms * 1000),
                    style_profile=card.style_profile,
                )
                for card in plan.title_cards
            ),
            measurements=measurements,
            sources=RenderSources(
                episode_id=plan.episode_id if plan.scope == "episode" else None,
                storyboard_version=plan.storyboard_version,
                episode_version=plan.episode_version,
                shot_version_ids=tuple(shot.shot_version_id for shot in plan.shots),
                audio_assets=tuple(plan.audio.assets),
                subtitle_track=sources_subtitle,
                font=plan.font,
            ),
            fingerprint=plan.fingerprint,
        )
        film = (
            find_film(draft, plan.project_id, plan.storyboard_id)
            if plan.storyboard_id is not None
            else find_episode_film(draft, plan.project_id, plan.episode_id)
        )
        if film is None:
            film_id = str(uuid4())
            number = 1
            version = self._native_version(plan, produced, render, number, timestamp, film_id)
            film = Film(
                id=film_id,
                project_id=plan.project_id,
                storyboard_id=plan.storyboard_id if plan.scope == "storyboard" else None,
                episode_id=plan.episode_id if plan.scope == "episode" else None,
                current_version=1,
                versions=(version,),
                revision=1,
                created_at=timestamp,
            )
        else:
            film_id = film.id
            number = film.current_version + 1
            base = next((item for item in reversed(film.versions) if item.render is None), None)
            if base is not None and plan.scope == "storyboard":
                version = base.model_copy(
                    update={
                        "version": number,
                        "output_media_id": produced["media_id"],
                        "size_bytes": produced["size_bytes"],
                        "sha256": produced["sha256"],
                        "metadata": produced["metadata"],
                        "render": render,
                        "created_at": timestamp,
                    }
                )
            else:
                version = self._native_version(plan, produced, render, number, timestamp, film_id)
            film = film.model_copy(
                update={
                    "current_version": number,
                    "versions": (*film.versions, version),
                    "revision": film.revision + 1,
                }
            )
        media = FilmMedia(
            id=produced["media_id"],
            project_id=plan.project_id,
            film_id=film_id,
            film_version=number,
            relative_path=produced["final_relative"],
            size_bytes=produced["size_bytes"],
            sha256=produced["sha256"],
            metadata=produced["metadata"],
            created_at=timestamp,
        )
        current_task = RenderTask.model_validate(draft["renders"][task.id])
        updated_task = current_task.model_copy(
            update={
                "status": "succeeded",
                "film_id": film_id,
                "film_version": number,
                "revision": current_task.revision + 1,
                "updated_at": timestamp,
            }
        )
        draft["films"][film_id] = film.model_dump(mode="json", by_alias=True)
        draft["media"][produced["media_id"]] = media.model_dump(mode="json", by_alias=True)
        draft["renders"][task.id] = updated_task.model_dump(mode="json", by_alias=True)
        return self._result(draft, plan, updated_task, film, number)

    def _native_version(self, plan, produced, render, number, timestamp, film_id) -> FilmVersion:
        timeline = tuple(
            FilmSegment(
                shot_id=shot.shot_id,
                shot_version_id=shot.shot_version_id,
                start=Rational.of(shot.start),
                duration=Rational.of(shot.duration),
                signature_sha256=shot.sha256,
                priming=Rational.microseconds(0),
            )
            for shot in plan.shots
        )
        inputs = tuple(
            FilmInput(
                shot_id=shot.shot_id,
                shot_version_id=shot.shot_version_id,
                job_id=shot.job_id,
                media_id=shot.media_id,
                sha256=shot.sha256,
                size_bytes=shot.size_bytes,
            )
            for shot in plan.shots
        )
        assembly = FilmAssembly(
            assembly_version=RENDER_NATIVE_ASSEMBLY,
            engine=FilmEngine(
                package_version=PACKAGE_VERSION,
                ffmpeg_version=plan.engine.ffmpeg_version,
                binary_sha256=plan.engine.binary_sha256,
            ),
            compatibility_sha256=canonical_sha256(
                {
                    "scope": plan.scope,
                    "profile": plan.profile,
                    "shots": [{"shotId": shot.shot_id, "sha256": shot.sha256} for shot in plan.shots],
                }
            ),
            timeline=timeline,
            fingerprint=canonical_sha256(
                {
                    "assemblyVersion": RENDER_NATIVE_ASSEMBLY,
                    "renderFingerprint": plan.fingerprint,
                    "timeline": [segment.model_dump(mode="json", by_alias=True) for segment in timeline],
                }
            ),
        )
        return FilmVersion(
            version=number,
            storyboard_version=plan.storyboard_version,
            episode_version=plan.episode_version,
            storyboard_revision=plan.revision,
            inputs=inputs,
            output_media_id=produced["media_id"],
            assembly=assembly,
            render=render,
            size_bytes=produced["size_bytes"],
            sha256=produced["sha256"],
            metadata=produced["metadata"],
            created_at=timestamp,
        )

    @staticmethod
    def _result(state: dict, plan: RenderPlan, task: RenderTask, film: Film, number: int) -> dict:
        view = film_view(state, film)
        return {
            "replay": False,
            "renderId": task.id,
            "status": "succeeded",
            "scope": plan.scope,
            "storyboardId": plan.storyboard_id,
            "episodeId": plan.episode_id,
            "profile": plan.profile["name"],
            "subtitleMode": plan.mode,
            "estimatedSeconds": task.estimated_seconds,
            "scenes": [scene.model_dump(mode="json", by_alias=True) for scene in task.scenes],
            "filmId": film.id,
            "filmVersion": number,
            "fingerprint": plan.fingerprint,
            "film": view,
        }


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


def _hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _first_error(text: str) -> str:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    for line in reversed(lines):
        lowered = line.lower()
        if any(
            marker in lowered
            for marker in ("error", "invalid", "failed", "unable", "no such", "cannot", "unrecognized")
        ):
            return line[:300]
    return lines[-1][:300] if lines else ""
