"""M7 E3 timeline domain: one append-only edit sequence per episode.

A timeline re-orders, trims and binds already delivered shots. It never generates a
shot, copies a media file or owns a budget: every clip cites one
``(storyboardId, shotVersionId, shotId, sceneId)`` that still lives on the episode,
and every render re-reads the frozen episode version. Saving is therefore a pure
local metadata transaction (``timeline-zh-v1``), and rendering is a separate local
FFmpeg step in :mod:`vagent.video.render_service`.

Trims are integers in milliseconds and are shot-local (``0 <= inMs < outMs <=
duration``). The effective timeline is ``sum(clip lengths) - sum(transitions)``; it
reuses the exact M6 transition arithmetic so a timeline render and an episode render
of the same shots agree. Subtitle cues are re-mapped at render time by
``timeline-subtitle-remap-v1``; this module only validates the reference and freezes
the version bytes.
"""

import hashlib
import json
from fractions import Fraction
from typing import Literal
from uuid import uuid4

from pydantic import Field, JsonValue, model_validator

from vagent.contracts import Contract, Fingerprint, Identifier, JsonTuple, UtcTimestamp
from vagent.errors import AppError
from vagent.video.audio import AUDIO_KIND_LIMITS, AudioAsset
from vagent.video.episodes import Episode, Scene, utc_now
from vagent.video.films import Rational
from vagent.video.renders import (
    RenderError,
    RenderTitleCardArg,
    RenderTransitionArg,
    ShotEntry,
    effective_duration,
    effective_entries,
    validate_title_cards,
    validate_transitions,
)
from vagent.video.storyboards import Storyboard, storyboard_in_state
from vagent.video.subtitles import SubtitleTrack, track_by_id

TIMELINE_CONTRACT = "timeline-zh-v1"
TIMELINE_VERSION_LIMIT = 64
TIMELINE_CLIP_LIMIT = 20
TIMELINE_TRANSITION_LIMIT = 19
TIMELINE_AUDIO_LIMIT = 64
TIMELINE_TITLE_LIMIT = 4
TIMELINE_TRIM_MIN_MS = 1000
TIMELINE_MIN_SECONDS = 1
TIMELINE_MAX_SECONDS = 90
AUDIO_GAIN_MIN = -60.0
AUDIO_GAIN_MAX = 12.0
AUDIO_DEFAULT_GAIN = {"voice": 0.0, "bgm": -18.0, "sfx": -6.0}
MESSAGES = {
    "TIMELINE_INVALID": "时间线引用、顺序、转场、字幕轨或标题卡非法，未保存。",
    "TIMELINE_NOT_FOUND": "当前项目中没有这条时间线或指定版本。",
    "TIMELINE_TRIM_INVALID": "裁剪窗口越界、倒序或不足 1 秒，未保存。",
    "TIMELINE_SOURCE_MISSING": "时间线引用的镜头、媒体、字幕或音频未交付。",
    "TIMELINE_VERSION_CONFLICT": "时间线记录已变化，请重新读取 revision 后再保存。",
    "SUBTITLE_TRACK_MISSING": "时间线引用的整集字幕轨或版本不存在。",
    "AUDIO_ASSET_MISSING": "时间线引用的音频资产或版本不存在。",
}


def _error(code: str, detail: str = "") -> AppError:
    message = MESSAGES.get(code, "时间线操作失败，未保存。")
    return AppError(code, f"{message}{detail}" if detail else message)


def canonical_sha256(value: JsonValue) -> str:
    encoded = json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


# --- records -----------------------------------------------------------------


class TimelineClip(Contract):
    """One trimmed shot on the timeline; in/out are shot-local integer milliseconds."""

    shot_id: Identifier
    shot_version_id: Identifier
    storyboard_id: Identifier
    scene_id: Identifier | None = None
    in_ms: int = Field(ge=0, le=TIMELINE_MAX_SECONDS * 1000)
    out_ms: int = Field(gt=0, le=TIMELINE_MAX_SECONDS * 1000)


class TimelineTransition(Contract):
    after_shot_id: Identifier
    type: Literal["cut", "fade", "crossfade"] = "cut"
    duration_ms: int = Field(default=0, ge=0, le=1000)

    @model_validator(mode="after")
    def duration_matches_type(self):
        if self.type == "cut":
            if self.duration_ms != 0:
                raise ValueError("A cut transition does not consume time")
            return self
        if not 100 <= self.duration_ms <= 1000:
            raise ValueError("A fade or crossfade must last 0.1..1.0 seconds")
        return self


class TimelineSubtitleRef(Contract):
    track_id: Identifier
    track_version: int = Field(ge=1)


class TimelineAudioTrack(Contract):
    audio_id: Identifier
    audio_version: int = Field(ge=1)
    kind: Literal["voice", "bgm", "sfx"]
    gain_db: float = Field(default=0.0, ge=AUDIO_GAIN_MIN, le=AUDIO_GAIN_MAX, allow_inf_nan=False)
    start_ms: int = Field(default=0, ge=0, le=TIMELINE_MAX_SECONDS * 1000)

    @model_validator(mode="after")
    def voice_starts_with_the_timeline(self):
        # A voice line is positioned by its re-mapped subtitle cue, never by startMs.
        if self.kind == "voice" and self.start_ms != 0:
            raise ValueError("A voice track starts at zero and follows its cue")
        return self


class TimelineTitleCard(Contract):
    text: str = Field(min_length=1, max_length=120)
    start_ms: int = Field(ge=0, le=TIMELINE_MAX_SECONDS * 1000)
    duration_ms: int = Field(gt=0, le=TIMELINE_MAX_SECONDS * 1000)
    style_profile: Literal["title-zh-v1"] = "title-zh-v1"

    @model_validator(mode="after")
    def no_control_characters(self):
        if any(character in self.text for character in "\r\n\t"):
            raise ValueError("A title card cannot contain control line breaks")
        if self.text != self.text.strip():
            raise ValueError("Title card text must not have leading or trailing whitespace")
        return self


class TimelineVersion(Contract):
    """One frozen edit: ordered trimmed clips plus their transitions and bindings."""

    version: int = Field(ge=1)
    episode_version: int = Field(ge=1)
    contract: Literal["timeline-zh-v1"] = TIMELINE_CONTRACT
    clips: JsonTuple[TimelineClip] = Field(min_length=1, max_length=TIMELINE_CLIP_LIMIT)
    transitions: JsonTuple[TimelineTransition] = Field(default=(), max_length=TIMELINE_TRANSITION_LIMIT)
    subtitle: TimelineSubtitleRef | None = None
    audio: JsonTuple[TimelineAudioTrack] = Field(default=(), max_length=TIMELINE_AUDIO_LIMIT)
    title_cards: JsonTuple[TimelineTitleCard] = Field(default=(), max_length=TIMELINE_TITLE_LIMIT)
    fingerprint: Fingerprint
    created_at: UtcTimestamp

    @model_validator(mode="after")
    def unique_clips(self):
        shot_ids = [clip.shot_id for clip in self.clips]
        if len(set(shot_ids)) != len(shot_ids):
            raise ValueError("A timeline cannot repeat the same shot")
        if len({item.after_shot_id for item in self.transitions}) != len(self.transitions):
            raise ValueError("A transition may appear only once per shot boundary")
        if any(item.kind == "voice" for item in self.audio) and self.subtitle is None:
            raise ValueError("A voice track requires a subtitle track")
        return self


class Timeline(Contract):
    id: Identifier
    project_id: Identifier
    episode_id: Identifier
    current_version: int = Field(ge=1)
    versions: JsonTuple[TimelineVersion] = Field(min_length=1, max_length=TIMELINE_VERSION_LIMIT)
    revision: int = Field(ge=0)
    created_at: UtcTimestamp

    @model_validator(mode="after")
    def append_only_history(self):
        if [item.version for item in self.versions] != list(range(1, len(self.versions) + 1)):
            raise ValueError("Timeline versions must be the continuous 1..N sequence")
        if self.current_version != len(self.versions):
            raise ValueError("currentVersion must identify the latest timeline version")
        return self

    @property
    def latest(self) -> TimelineVersion:
        return self.versions[-1]


# --- tool arguments ----------------------------------------------------------


class TimelineSaveArgs(Contract):
    episode_id: Identifier
    timeline_id: Identifier | None = None
    clips: JsonTuple[TimelineClip] = Field(min_length=1, max_length=TIMELINE_CLIP_LIMIT)
    transitions: JsonTuple[TimelineTransition] = Field(default=(), max_length=TIMELINE_TRANSITION_LIMIT)
    subtitle: TimelineSubtitleRef | None = None
    audio: JsonTuple[TimelineAudioTrack] = Field(default=(), max_length=TIMELINE_AUDIO_LIMIT)
    title_cards: JsonTuple[TimelineTitleCard] = Field(default=(), max_length=TIMELINE_TITLE_LIMIT)
    expected_revision: int | None = Field(default=None, ge=0)


class TimelineReadArgs(Contract):
    episode_id: Identifier | None = None
    timeline_id: Identifier | None = None
    version: int | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def at_least_one_target(self):
        if self.episode_id is None and self.timeline_id is None:
            raise ValueError("timeline_read needs episodeId or timelineId")
        return self


# --- canonical identity ------------------------------------------------------


def _clip_payload(clip: TimelineClip) -> dict:
    return clip.model_dump(mode="json", by_alias=True)


def timeline_payload(
    episode_id: str,
    episode_version: int,
    clips,
    transitions,
    subtitle,
    audio,
    title_cards,
) -> dict:
    return {
        "contract": TIMELINE_CONTRACT,
        "episodeId": episode_id,
        "episodeVersion": episode_version,
        "clips": [_clip_payload(clip) for clip in clips],
        "transitions": [item.model_dump(mode="json", by_alias=True) for item in transitions],
        "subtitle": subtitle.model_dump(mode="json", by_alias=True) if subtitle is not None else None,
        "audio": [item.model_dump(mode="json", by_alias=True) for item in audio],
        "titleCards": [item.model_dump(mode="json", by_alias=True) for item in title_cards],
    }


def timeline_fingerprint(
    episode_id: str,
    episode_version: int,
    clips,
    transitions,
    subtitle,
    audio,
    title_cards,
) -> str:
    """Output-affecting identity only: no version number, time or revision."""
    return canonical_sha256(
        timeline_payload(episode_id, episode_version, clips, transitions, subtitle, audio, title_cards)
    )


def timeline_version_sha256(version: TimelineVersion) -> str:
    """The frozen bytes of one version, for the render source record."""
    return canonical_sha256(
        {
            "contract": version.contract,
            "version": version.version,
            "episodeVersion": version.episode_version,
            "fingerprint": version.fingerprint,
        }
    )


# --- geometry ----------------------------------------------------------------


def transition_args(version: TimelineVersion) -> tuple[RenderTransitionArg, ...]:
    """Map the frozen transitions onto the M6 render argument (same arithmetic)."""
    return tuple(
        RenderTransitionArg(
            afterShotId=item.after_shot_id,
            type=item.type,
            durationMs=item.duration_ms,
        )
        for item in version.transitions
        if item.type != "cut"
    )


def clip_duration(clip: TimelineClip) -> Fraction:
    return Fraction(clip.out_ms - clip.in_ms, 1000)


def clip_entries(clips) -> list[ShotEntry]:
    """A contiguous timeline of the trimmed clip lengths, in edit order."""
    entries: list[ShotEntry] = []
    offset = Fraction(0)
    for clip in clips:
        duration = clip_duration(clip)
        entries.append(
            ShotEntry(
                shot_id=clip.shot_id,
                storyboard_id=clip.storyboard_id,
                scene_id=clip.scene_id,
                start=offset,
                duration=duration,
            )
        )
        offset += duration
    return entries


def effective_timeline(clips, transitions) -> tuple[list[ShotEntry], dict, list, Fraction]:
    """The frozen entries, active transitions, effective entries and total seconds."""
    entries = clip_entries(clips)
    args = tuple(
        RenderTransitionArg(
            afterShotId=item.after_shot_id,
            type=item.type,
            durationMs=item.duration_ms,
        )
        for item in transitions
        if item.type != "cut"
    )
    active = validate_transitions(entries, args)
    effective = effective_entries(entries, active)
    return entries, active, effective, effective_duration(effective)


def title_card_args(version: TimelineVersion) -> tuple[RenderTitleCardArg, ...]:
    return tuple(
        RenderTitleCardArg(
            text=card.text,
            startMs=card.start_ms,
            durationMs=card.duration_ms,
            styleProfile=card.style_profile,
        )
        for card in version.title_cards
    )


# --- resolution --------------------------------------------------------------


def find_timeline(state: dict, project_id: str, episode_id: str) -> Timeline | None:
    for raw in state["timelines"].values():
        if raw.get("projectId") == project_id and raw.get("episodeId") == episode_id:
            return Timeline.model_validate(raw)
    return None


def timeline_by_id(state: dict, project_id: str, timeline_id: str) -> Timeline:
    raw = state["timelines"].get(timeline_id)
    if raw is None or raw.get("projectId") != project_id:
        raise _error("TIMELINE_NOT_FOUND")
    return Timeline.model_validate(raw)


def timeline_version(record: Timeline, version: int | None) -> TimelineVersion:
    chosen = (
        next((item for item in record.versions if item.version == version), None)
        if version is not None
        else record.versions[-1]
    )
    if chosen is None:
        raise _error("TIMELINE_NOT_FOUND", "（指定版本不存在）")
    return chosen


def episode_record(state: dict, project_id: str, episode_id: str) -> Episode:
    raw = state["episodes"].get(episode_id)
    if raw is None or raw.get("projectId") != project_id:
        raise _error("TIMELINE_NOT_FOUND", "（分集不存在或不属于当前项目）")
    return Episode.model_validate(raw)


def episode_source(state: dict, project_id: str, episode: Episode) -> dict[str, dict]:
    """Every shot of the episode's current version, with its scene source window.

    The order is the same scene/shot order the episode subtitle aligner froze, so a
    cue's source time can be converted to a shot-local offset without a second rule.
    """
    shots: dict[str, dict] = {}
    offset = Fraction(0)
    for reference in episode.latest.scenes:
        raw = state["scenes"].get(reference.scene_id)
        if raw is None or raw.get("projectId") != project_id:
            raise _error("TIMELINE_SOURCE_MISSING", "（场记缺失）")
        scene = Scene.model_validate(raw)
        if scene.episode_id != episode.id:
            raise _error("TIMELINE_SOURCE_MISSING", "（场记不属于该分集）")
        if scene.storyboard_id is None:
            # An unbound scene contributes no shots yet; a timeline may still cover
            # the scenes that are already bound. Rendering reports the missing ones.
            continue
        record = storyboard_in_state(state, project_id, scene.storyboard_id)
        for shot in sorted(record.versions[-1].shots, key=lambda item: item.order):
            shots[shot.id] = {
                "shotId": shot.id,
                "sceneId": scene.id,
                "storyboardId": record.id,
                "storyboardVersion": record.current_version,
                "title": shot.description,
                "framing": shot.framing,
                "durationSeconds": shot.duration_seconds,
                "durationMs": shot.duration_seconds * 1000,
                "sourceStart": offset,
                "order": shot.order,
                "storyboard": record,
            }
            offset += Fraction(shot.duration_seconds)
    return shots


def _shot_version(record: Storyboard, clip: TimelineClip):
    candidate = next((item for item in record.shot_versions if item.id == clip.shot_version_id), None)
    if candidate is None or candidate.shot_id != clip.shot_id:
        raise _error("TIMELINE_INVALID", "（裁剪引用的镜头版本不存在或不属于该镜头）")
    frozen = next((item for item in record.versions if item.version == candidate.storyboard_version), None)
    if frozen is None:
        raise _error("TIMELINE_INVALID", "（镜头版本引用的分镜版本不存在）")
    shot = next((item for item in frozen.shots if item.id == clip.shot_id), None)
    if shot is None:
        raise _error("TIMELINE_INVALID", "（镜头版本引用的镜头不存在）")
    return candidate, shot


def _validate_clips(state: dict, project_id: str, version: TimelineVersion, source: dict[str, dict]) -> None:
    seen: set[str] = set()
    for clip in version.clips:
        if clip.shot_id in seen:
            raise _error("TIMELINE_INVALID", "（同一镜头不能重复出现）")
        seen.add(clip.shot_id)
        located = source.get(clip.shot_id)
        if located is None:
            raise _error("TIMELINE_INVALID", "（镜头不在该分集当前版本的场记中）")
        if clip.storyboard_id != located["storyboardId"]:
            raise _error("TIMELINE_INVALID", "（clip 的分镜引用与当前场记不一致）")
        if clip.scene_id is not None and clip.scene_id != located["sceneId"]:
            raise _error("TIMELINE_INVALID", "（clip 的场记引用与当前分集不一致）")
        record = located["storyboard"]
        _candidate, shot = _shot_version(record, clip)
        # The trim window is checked against the storyboard version that owns the
        # cited ShotVersion; a resized board can therefore keep an older, shorter shot.
        duration_ms = shot.duration_seconds * 1000
        if clip.out_ms > duration_ms:
            raise _error("TIMELINE_TRIM_INVALID", "（裁剪终点超出镜头时长）")
        if clip.out_ms - clip.in_ms < TIMELINE_TRIM_MIN_MS:
            raise _error("TIMELINE_TRIM_INVALID", "（单条裁剪不足 1 秒）")


def _validate_subtitle(state: dict, project_id: str, episode_id: str, version: TimelineVersion) -> None:
    reference = version.subtitle
    if reference is None:
        return
    track = track_by_id(state, reference.track_id, project_id)
    if track is None:
        raise _error("SUBTITLE_TRACK_MISSING")
    if track.scope != "episode" or track.episode_id != episode_id:
        raise _error("SUBTITLE_TRACK_MISSING", "（时间线只能引用该集的整集字幕轨）")
    chosen = next((item for item in track.versions if item.version == reference.track_version), None)
    if chosen is None:
        raise _error("SUBTITLE_TRACK_MISSING", "（指定字幕版本不存在）")
    if chosen.episode_version != version.episode_version:
        raise _error("SUBTITLE_TRACK_MISSING", "（字幕版本与时间线依据的分集版本不一致，请重新生成字幕）")


def _validate_audio(
    state: dict, project_id: str, episode_id: str, version: TimelineVersion, total: Fraction
) -> None:
    counts: dict[str, int] = {}
    seen: set[str] = set()
    for track in version.audio:
        if track.audio_id in seen:
            raise _error("TIMELINE_INVALID", "（同一音频资产不能重复出现）")
        seen.add(track.audio_id)
        raw = state["audio"].get(track.audio_id)
        if raw is None or raw.get("projectId") != project_id:
            raise _error("AUDIO_ASSET_MISSING")
        asset = AudioAsset.model_validate(raw)
        if asset.kind != track.kind:
            raise _error("TIMELINE_INVALID", "（音轨 kind 与音频资产不一致）")
        if not any(item.version == track.audio_version for item in asset.versions):
            raise _error("AUDIO_ASSET_MISSING", "（指定音频版本不存在）")
        from vagent.video.audio import ProviderAudioSource

        chosen = next(item for item in asset.versions if item.version == track.audio_version)
        if track.kind == "voice" and (
            not isinstance(chosen.source, ProviderAudioSource) or chosen.source.episode_id != episode_id
        ):
            raise _error("TIMELINE_INVALID", "（配音音轨必须属于该分集）")
        counts[track.kind] = counts.get(track.kind, 0) + 1
        if counts[track.kind] > AUDIO_KIND_LIMITS[track.kind]:
            raise _error("TIMELINE_INVALID", f"（{track.kind} 音轨超过上限）")
        if track.kind != "voice" and Fraction(track.start_ms, 1000) >= total:
            raise _error("TIMELINE_INVALID", "（音轨起点超出有效时间线）")


def _validate_geometry(version: TimelineVersion) -> tuple[Fraction, Fraction]:
    """Rules that need no Store lookup: trims, transitions, duration, titles, counts."""
    for clip in version.clips:
        if clip.out_ms <= clip.in_ms:
            raise _error("TIMELINE_TRIM_INVALID", "（裁剪窗口倒序或为空）")
        if clip.out_ms - clip.in_ms < TIMELINE_TRIM_MIN_MS:
            raise _error("TIMELINE_TRIM_INVALID", "（单条裁剪不足 1 秒）")
    try:
        entries, _active, _effective, total = effective_timeline(version.clips, version.transitions)
    except RenderError as error:
        raise _error("TIMELINE_INVALID", f"（{error}）") from None
    if total < TIMELINE_MIN_SECONDS or total > TIMELINE_MAX_SECONDS:
        raise _error("TIMELINE_INVALID", "（有效时长必须在 1.000–90.000 秒之间）")
    try:
        validate_title_cards(title_card_args(version), total)
    except RenderError:
        raise _error("TIMELINE_INVALID", "（标题卡超过 4 条、重叠或越出有效时间线）") from None
    counts: dict[str, int] = {}
    for track in version.audio:
        counts[track.kind] = counts.get(track.kind, 0) + 1
        if counts[track.kind] > AUDIO_KIND_LIMITS[track.kind]:
            raise _error("TIMELINE_INVALID", f"（{track.kind} 音轨超过上限）")
        if track.kind != "voice" and Fraction(track.start_ms, 1000) >= total:
            raise _error("TIMELINE_INVALID", "（音轨起点超出有效时间线）")
    raw_total = sum((entry.duration for entry in entries), Fraction(0))
    return raw_total, total


def _validate_timeline_version(
    state: dict,
    project_id: str,
    episode: Episode,
    version: TimelineVersion,
    source: dict[str, dict],
) -> tuple[Fraction, Fraction]:
    """Return the raw total and the effective total, after every frozen rule."""
    _validate_clips(state, project_id, version, source)
    _validate_subtitle(state, project_id, episode.id, version)
    raw_total, total = _validate_geometry(version)
    _validate_audio(state, project_id, episode.id, version, total)
    return raw_total, total


# --- save --------------------------------------------------------------------


def _version_from_args(
    args: TimelineSaveArgs,
    *,
    number: int,
    episode_version: int,
    timestamp: str,
) -> TimelineVersion:
    fingerprint = timeline_fingerprint(
        args.episode_id,
        episode_version,
        args.clips,
        args.transitions,
        args.subtitle,
        args.audio,
        args.title_cards,
    )
    return TimelineVersion(
        version=number,
        episode_version=episode_version,
        clips=args.clips,
        transitions=args.transitions,
        subtitle=args.subtitle,
        audio=args.audio,
        title_cards=args.title_cards,
        fingerprint=fingerprint,
        created_at=timestamp,
    )


def save_timeline(args: dict, state: dict, project_id: str) -> dict:
    """Create or append one TimelineVersion; never a Job, model call or file write."""
    try:
        parsed = TimelineSaveArgs.model_validate(args)
    except ValueError:
        raise _error("TIMELINE_INVALID", "（参数结构不符合 timeline-zh-v1）") from None
    episode = episode_record(state, project_id, parsed.episode_id)
    existing = find_timeline(state, project_id, episode.id)
    if parsed.timeline_id is not None and (existing is None or existing.id != parsed.timeline_id):
        raise _error("TIMELINE_NOT_FOUND", "（timelineId 与分集不匹配）")
    source = episode_source(state, project_id, episode)
    try:
        version = _version_from_args(
            parsed,
            number=existing.current_version + 1 if existing is not None else 1,
            episode_version=episode.current_version,
            timestamp=utc_now(),
        )
    except ValueError:
        raise _error("TIMELINE_INVALID", "（同一镜头重复、转场重复或配音缺少字幕轨）") from None
    raw_total, total = _validate_timeline_version(state, project_id, episode, version, source)
    if existing is not None and existing.latest.fingerprint == version.fingerprint:
        return {
            **timeline_view(state, existing, project_id),
            "replayed": True,
            "rawDurationMs": _ms(raw_total),
            "effectiveDurationMs": _ms(total),
        }
    if existing is not None and existing.revision != parsed.expected_revision:
        raise _error("TIMELINE_VERSION_CONFLICT")
    if existing is not None and len(existing.versions) >= TIMELINE_VERSION_LIMIT:
        raise _error("TIMELINE_INVALID", "时间线版本数已达上限，未保存新版本。")
    timestamp = version.created_at
    if existing is None:
        record = Timeline(
            id=str(uuid4()),
            project_id=project_id,
            episode_id=episode.id,
            current_version=1,
            versions=(version,),
            revision=0,
            created_at=timestamp,
        )
    else:
        record = existing.model_copy(
            update={
                "current_version": version.version,
                "versions": (*existing.versions, version),
                "revision": existing.revision + 1,
            }
        )
    state["timelines"][record.id] = record.model_dump(mode="json", by_alias=True)
    return {
        **timeline_view(state, record, project_id),
        "replayed": False,
        "rawDurationMs": _ms(raw_total),
        "effectiveDurationMs": _ms(total),
    }


def _ms(value: Fraction) -> int:
    return round(value * 1000)


# --- views -------------------------------------------------------------------


def _shot_delivery(state: dict, shot_version_id: str) -> dict:
    from vagent.video.contracts import LiveJob, parse_job

    for raw in state["storyboards"].values():
        record = Storyboard.model_validate(raw)
        for item in record.shot_versions:
            if item.id != shot_version_id:
                continue
            job = parse_job(state["jobs"][item.job_id]) if item.job_id in state["jobs"] else None
            media_id = item.media_id
            if media_id is None and isinstance(job, LiveJob) and job.result is not None:
                refs = job.result.media_refs
                media_id = refs[0].media_id if refs else None
            available = bool(
                media_id
                and isinstance(job, LiveJob)
                and job.status == "succeeded"
                and job.media_availability.status == "available"
                and media_id in state["media"]
            )
            return {
                "jobId": item.job_id,
                "mediaId": media_id,
                "mediaAvailable": available,
                "mediaUrl": f"/api/media/{media_id}/content" if available else None,
                "status": job.status if job is not None else None,
            }
    return {"jobId": None, "mediaId": None, "mediaAvailable": False, "mediaUrl": None, "status": None}


def clip_view(
    state: dict,
    project_id: str,
    clip: TimelineClip,
    *,
    order: int,
    source: dict[str, dict] | None,
    effective_start: Fraction | None,
    effective_duration_value: Fraction | None,
) -> dict:
    located = (source or {}).get(clip.shot_id)
    delivery = _shot_delivery(state, clip.shot_version_id)
    return {
        "order": order,
        "shotId": clip.shot_id,
        "shotVersionId": clip.shot_version_id,
        "storyboardId": clip.storyboard_id,
        "sceneId": clip.scene_id,
        "inMs": clip.in_ms,
        "outMs": clip.out_ms,
        "durationMs": clip.out_ms - clip.in_ms,
        "sourceDurationMs": located["durationMs"] if located else None,
        "title": located["title"] if located else None,
        "framing": located["framing"] if located else None,
        "effectiveStartMs": _ms(effective_start) if effective_start is not None else None,
        "effectiveDurationMs": _ms(effective_duration_value)
        if effective_duration_value is not None
        else None,
        **delivery,
    }


def _version_view(
    state: dict,
    record: Timeline,
    version: TimelineVersion,
    project_id: str,
    *,
    include_clips: bool,
    source: dict[str, dict] | None = None,
    episode_version: int | None = None,
) -> dict:
    transitions = [item.model_dump(mode="json", by_alias=True) for item in version.transitions]
    subtitle = None
    if version.subtitle is not None:
        subtitle = {**version.subtitle.model_dump(mode="json", by_alias=True)}
        track = track_by_id(state, version.subtitle.track_id, project_id)
        if track is not None:
            chosen = next(
                (item for item in track.versions if item.version == version.subtitle.track_version), None
            )
            subtitle["cueCount"] = len(chosen.cues) if chosen is not None else 0
            subtitle["episodeVersion"] = chosen.episode_version if chosen is not None else None
    audio = []
    for item in version.audio:
        raw = state["audio"].get(item.audio_id)
        asset = (
            AudioAsset.model_validate(raw) if raw is not None and raw.get("projectId") == project_id else None
        )
        audio.append(
            {
                **item.model_dump(mode="json", by_alias=True),
                "title": _audio_title(asset),
                "durationSeconds": _audio_duration(asset, item.audio_version),
                "mediaUrl": _audio_url(state, asset, item.audio_version),
            }
        )
    view = {
        "timelineId": record.id,
        "episodeId": record.episode_id,
        "version": version.version,
        "currentVersion": record.current_version,
        "revision": record.revision,
        "episodeVersion": version.episode_version,
        "contract": version.contract,
        "fingerprint": version.fingerprint,
        "sha256": timeline_version_sha256(version),
        "titleCards": [item.model_dump(mode="json", by_alias=True) for item in version.title_cards],
        "transitions": transitions,
        "subtitle": subtitle,
        "audio": audio,
        "createdAt": version.created_at,
        "stale": episode_version is not None and version.episode_version != episode_version,
        "clipCount": len(version.clips),
    }
    if include_clips:
        if source is None:
            episode = episode_record(state, project_id, record.episode_id)
            source = episode_source(state, project_id, episode)
        _entries, _active, effective, total = effective_timeline(version.clips, version.transitions)
        by_shot = {item.shot_id: item for item in effective}
        clips = []
        for index, clip in enumerate(version.clips):
            window = by_shot.get(clip.shot_id)
            clips.append(
                clip_view(
                    state,
                    project_id,
                    clip,
                    order=index + 1,
                    source=source,
                    effective_start=window.start if window is not None else None,
                    effective_duration_value=window.duration if window is not None else None,
                )
            )
        view["clips"] = clips
        view["rawDurationMs"] = sum(clip.out_ms - clip.in_ms for clip in version.clips)
        view["effectiveDurationMs"] = _ms(total)
        view["transitionMs"] = sum(item.duration_ms for item in version.transitions)
    return view


def _audio_title(asset: AudioAsset | None) -> str | None:
    if asset is None:
        return None
    source = asset.versions[-1].source
    return getattr(source, "title", None) or getattr(source, "text", None)


def _audio_duration(asset: AudioAsset | None, version: int) -> float | None:
    if asset is None:
        return None
    chosen = next((item for item in asset.versions if item.version == version), None)
    return chosen.duration_seconds if chosen is not None else None


def _audio_url(state: dict, asset: AudioAsset | None, version: int) -> str | None:
    if asset is None:
        return None
    chosen = next((item for item in asset.versions if item.version == version), None)
    if chosen is None or chosen.media_id not in state["media"]:
        return None
    return f"/api/media/{chosen.media_id}/content"


def timeline_view(state: dict, record: Timeline, project_id: str, *, version: int | None = None) -> dict:
    episode = episode_record(state, project_id, record.episode_id)
    chosen = timeline_version(record, version)
    current = chosen.version == record.current_version
    return {
        "timelineId": record.id,
        "projectId": record.project_id,
        "episodeId": record.episode_id,
        "episodeTitle": episode.title,
        "currentVersion": record.current_version,
        "revision": record.revision,
        "versionCount": len(record.versions),
        "createdAt": record.created_at,
        "latest": _version_view(
            state,
            record,
            chosen,
            project_id,
            include_clips=True,
            episode_version=episode.current_version,
        ),
        "versions": [
            _version_view(
                state,
                record,
                item,
                project_id,
                include_clips=False,
                episode_version=episode.current_version,
            )
            for item in record.versions
        ],
        "stale": current and chosen.episode_version != episode.current_version,
    }


def read_timeline(args: dict, state: dict, project_id: str) -> dict:
    """The current or a named version of one episode timeline; read-only."""
    parsed = TimelineReadArgs.model_validate(args)
    if parsed.timeline_id is not None:
        record = timeline_by_id(state, project_id, parsed.timeline_id)
        if parsed.episode_id is not None and record.episode_id != parsed.episode_id:
            raise _error("TIMELINE_NOT_FOUND", "（时间线不属于指定分集）")
    elif parsed.episode_id is not None:
        raw_episode = state["episodes"].get(parsed.episode_id)
        if raw_episode is None or raw_episode.get("projectId") != project_id:
            raise _error("TIMELINE_NOT_FOUND", "（分集不存在或不属于当前项目）")
        record = find_timeline(state, project_id, parsed.episode_id)
        if record is None:
            return {
                "timelineId": None,
                "episodeId": parsed.episode_id,
                "timeline": None,
                "versions": [],
            }
    else:
        raise _error("TIMELINE_NOT_FOUND")
    view = timeline_view(state, record, project_id, version=parsed.version)
    return {"timelineId": record.id, "episodeId": record.episode_id, **view, "timeline": view["latest"]}


def editor_view(state: dict, project_id: str, episode_id: str) -> dict:
    """The Web workbench read: the timeline plus every source a user can pick."""
    episode = episode_record(state, project_id, episode_id)
    record = find_timeline(state, project_id, episode_id)
    source = episode_source(state, project_id, episode)
    shots = []
    for shot_id, located in source.items():
        record_board = located["storyboard"]
        versions = [
            item
            for item in record_board.shot_versions
            if item.shot_id == shot_id and item.storyboard_version == located["storyboardVersion"]
        ]
        versions.sort(key=lambda item: item.created_at)
        latest = versions[-1] if versions else None
        delivery = _shot_delivery(state, latest.id) if latest is not None else None
        shots.append(
            {
                "shotId": shot_id,
                "sceneId": located["sceneId"],
                "storyboardId": located["storyboardId"],
                "title": located["title"],
                "framing": located["framing"],
                "durationMs": located["durationMs"],
                "order": located["order"],
                "shotVersionId": latest.id if latest is not None else None,
                **(
                    delivery
                    or {
                        "jobId": None,
                        "mediaId": None,
                        "mediaAvailable": False,
                        "mediaUrl": None,
                        "status": None,
                    }
                ),
            }
        )
    tracks = []
    for raw in state["subtitleTracks"].values():
        if (
            raw.get("projectId") == project_id
            and raw.get("scope") == "episode"
            and raw.get("episodeId") == episode_id
        ):
            track = SubtitleTrack.model_validate(raw)
            tracks.append(
                {
                    "trackId": track.id,
                    "currentVersion": track.current_version,
                    "revision": track.revision,
                    "versions": [
                        {
                            "version": item.version,
                            "episodeVersion": item.episode_version,
                            "cueCount": len(item.cues),
                        }
                        for item in track.versions
                    ],
                }
            )
    audio = []
    for raw in state["audio"].values():
        if raw.get("projectId") != project_id:
            continue
        asset = AudioAsset.model_validate(raw)
        if asset.kind == "voice":
            from vagent.video.audio import ProviderAudioSource

            source_value = asset.versions[-1].source
            if not isinstance(source_value, ProviderAudioSource) or source_value.episode_id != episode_id:
                continue
        audio.append(
            {
                "audioId": asset.id,
                "audioVersion": asset.current_version,
                "kind": asset.kind,
                "title": _audio_title(asset),
                "durationSeconds": _audio_duration(asset, asset.current_version),
                "sampleRate": asset.versions[-1].sample_rate,
                "mediaUrl": _audio_url(state, asset, asset.current_version),
            }
        )
    audio.sort(key=lambda item: (item["kind"], item["title"] or "", item["audioId"]))
    return {
        "episodeId": episode.id,
        "episodeTitle": episode.title,
        "episodeVersion": episode.current_version,
        "timeline": timeline_view(state, record, project_id) if record is not None else None,
        "shots": shots,
        "subtitleTracks": tracks,
        "audio": audio,
    }


# --- store validation --------------------------------------------------------


def validate_timeline_changes(previous: dict[str, dict], state: dict) -> None:
    """Every Store commit re-checks ownership and append-only version history.

    Clip resolution is deliberately *not* re-run here: an episode edit or a resized
    storyboard must leave the old timeline readable (``stale=true``) instead of making
    the whole Store unopenable. Rendering re-resolves the sources and refuses.
    """
    timelines = state.get("timelines", {})
    if previous.keys() - timelines.keys():
        raise _error("TIMELINE_VERSION_CONFLICT", "已保存的时间线不能删除。")
    owners: set[tuple[str, str]] = set()
    for key, raw in timelines.items():
        try:
            record = Timeline.model_validate(raw)
        except ValueError:
            raise _error("TIMELINE_INVALID", "时间线记录不完整或版本不连续。") from None
        if record.id != key or record.project_id not in state["projects"]:
            raise _error("TIMELINE_INVALID", "时间线 ID 或所属项目不匹配。")
        episode_raw = state["episodes"].get(record.episode_id)
        if episode_raw is None or episode_raw.get("projectId") != record.project_id:
            raise _error("TIMELINE_INVALID", "时间线必须引用本项目的分集。")
        if (record.project_id, record.episode_id) in owners:
            raise _error("TIMELINE_INVALID", "每集恰好一条时间线。")
        owners.add((record.project_id, record.episode_id))
        for version in record.versions:
            expected = timeline_fingerprint(
                record.episode_id,
                version.episode_version,
                version.clips,
                version.transitions,
                version.subtitle,
                version.audio,
                version.title_cards,
            )
            if expected != version.fingerprint:
                raise _error("TIMELINE_INVALID", "时间线内容指纹与冻结版本不一致。")
            _validate_geometry(version)
        old_raw = previous.get(key)
        if old_raw is None:
            if record.current_version != 1 or record.revision != 0:
                raise _error("TIMELINE_VERSION_CONFLICT", "新时间线必须先保存第 1 版。")
            continue
        old = Timeline.model_validate(old_raw)
        if record == old:
            continue
        if (
            record.project_id != old.project_id
            or record.episode_id != old.episode_id
            or record.created_at != old.created_at
            or record.revision < old.revision
            or len(record.versions) < len(old.versions)
            or record.versions[: len(old.versions)] != old.versions
        ):
            raise _error("TIMELINE_VERSION_CONFLICT", "时间线版本只增不改，旧版本必须保留。")


def timeline_duration_ms(version: TimelineVersion) -> int:
    _entries, _active, _effective, total = effective_timeline(version.clips, version.transitions)
    return _ms(total)


def timeline_rational_duration(version: TimelineVersion) -> Rational:
    return Rational.of(Fraction(timeline_duration_ms(version), 1000))
