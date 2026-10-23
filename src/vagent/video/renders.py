"""M6 E3 render domain: transitions, title cards, task progress and effective time.

The render takes delivered shots (not a re-read script), one frozen subtitle track and
optional audio assets, and produces one film. Two pieces live here and stay pure:
the transition-aware effective timeline and the deterministic cue remap. FFmpeg work
lives in :mod:`vagent.video.render_service`.

A crossfade overlaps the tail of the outgoing shot with the head of the incoming one.
The output is therefore shorter than the sum of shot durations; the frozen subtitle
track keeps its source timeline, while the rendered sidecar uses cues clamped to the
effective shot windows. That mapping is integer/rational and never reads a clock.
"""

import hashlib
import json
from dataclasses import dataclass
from fractions import Fraction
from typing import Literal

from pydantic import Field, JsonValue, model_validator

from vagent.contracts import Contract, Fingerprint, Identifier, JsonTuple, PositiveSeconds, UtcTimestamp
from vagent.errors import AppError
from vagent.video.films import (
    MAX_RENDER_AUDIO_TRACKS,
    RENDER_MAX_SECONDS,
    TITLE_CARD_LIMIT,
    TRANSITION_SECONDS_MAX,
    TRANSITION_SECONDS_MIN,
    Film,
    Rational,
    canonical_sha256,
)

RENDER_TASK_LIMIT = 128
RENDER_SCENE_LIMIT = 12
TITLE_STYLE_PROFILE = "title-zh-v1"
TITLE_TEXT_LIMIT = 120
TRANSITION_TYPES = ("cut", "fade", "crossfade")
RENDER_STATUSES = ("running", "succeeded", "failed")
SCENE_STATUSES = ("pending", "running", "succeeded", "failed", "skipped")
MESSAGES = {
    "TRANSITION_INVALID": "转场类型、时长或边界非法，未渲染。",
    "TITLE_CARD_LIMIT": "标题卡超过 4 条或时间轴互相冲突，未渲染。",
    "RENDER_TIMELINE_MISMATCH": "输出时长、顺序或字幕时间线与冻结时间线不符，未渲染。",
    "RENDER_SOURCE_MISSING": "缺少已交付镜头、成片、字幕轨或音频资产，未渲染。",
    "RENDER_PROFILE_UNSUPPORTED": "导出档位或源规格不受支持，未渲染。",
    "RENDER_TIMEOUT": "本地渲染超时，未交付成片。",
    "RENDER_ENCODE_FAILED": "本地编码失败，未交付成片。",
    "RENDER_VALIDATION_FAILED": "渲染文件未通过结构、时长或字幕轨校验，未交付成片。",
    "LOUDNESS_OUT_OF_RANGE": "混音响度或 true peak 超出容差，未交付。",
    "SUBTITLE_TRACK_MISSING": "需要字幕轨，但没有找到可用的字幕版本。",
    "SUBTITLE_CUE_CONFLICT": "字幕时间与镜头时间轴冲突。",
    "SUBTITLE_OVERFLOW": "字幕超出时长、字符或条数上限。",
    "CONFIRMATION_REQUIRED": "渲染会写本地成片，需要显式确认。",
}


class RenderError(AppError):
    def __init__(self, code: str, detail: str = "", *, stage: str = "encode"):
        message = MESSAGES.get(code, "渲染失败，未交付成片。")
        super().__init__(code, f"{message}{detail}" if detail else message)
        self.stage = stage


# --- tool arguments ----------------------------------------------------------


class RenderTransitionArg(Contract):
    after_shot_id: Identifier
    type: Literal["cut", "fade", "crossfade"] = "cut"
    duration_ms: int = Field(default=0, ge=0, le=int(TRANSITION_SECONDS_MAX * 1000))

    @model_validator(mode="after")
    def duration_matches_type(self):
        if self.type == "cut":
            if self.duration_ms != 0:
                raise ValueError("A cut transition does not consume time")
            return self
        if not int(TRANSITION_SECONDS_MIN * 1000) <= self.duration_ms <= int(TRANSITION_SECONDS_MAX * 1000):
            raise ValueError("A fade or crossfade must last 0.1..1.0 seconds")
        return self


class RenderTitleCardArg(Contract):
    text: str = Field(min_length=1, max_length=TITLE_TEXT_LIMIT)
    start_ms: int = Field(ge=0)
    duration_ms: int = Field(gt=0)
    style_profile: Literal["title-zh-v1"] = TITLE_STYLE_PROFILE

    @model_validator(mode="after")
    def no_control_characters(self):
        if any(character in self.text for character in "\r\n\t"):
            raise ValueError("A title card cannot contain control line breaks")
        if self.text != self.text.strip():
            raise ValueError("Title card text must not have leading or trailing whitespace")
        return self


class FilmRenderArgs(Contract):
    scope: Literal["storyboard", "episode"]
    storyboard_id: Identifier | None = None
    episode_id: Identifier | None = None
    track_id: Identifier | None = None
    track_version: int | None = Field(default=None, ge=1)
    profile: Literal["720p-h264-aac-v1", "1080p-h264-aac-v1"] = "720p-h264-aac-v1"
    subtitle_mode: Literal["soft", "burn", "both"] = "soft"
    transitions: JsonTuple[RenderTransitionArg] = Field(default=(), max_length=63)
    title_cards: JsonTuple[RenderTitleCardArg] = Field(default=(), max_length=TITLE_CARD_LIMIT)
    expected_revision: int | None = Field(default=None, ge=0)
    confirm: bool = False

    @model_validator(mode="after")
    def coherent_target(self):
        if self.scope == "storyboard":
            if self.storyboard_id is None or self.episode_id is not None:
                raise ValueError("A storyboard render names only its storyboard")
        elif self.episode_id is None or self.storyboard_id is not None:
            raise ValueError("An episode render names only its episode")
        if self.subtitle_mode != "soft" and self.track_id is None:
            raise ValueError("burn and both exports require a subtitle track")
        if self.track_version is not None and self.track_id is None:
            raise ValueError("trackVersion requires trackId")
        boundaries = [item.after_shot_id for item in self.transitions]
        if len(set(boundaries)) != len(boundaries):
            raise ValueError("A transition may appear only once per shot boundary")
        return self


class RenderReadArgs(Contract):
    render_id: Identifier | None = None
    episode_id: Identifier | None = None
    storyboard_id: Identifier | None = None
    film_id: Identifier | None = None

    @model_validator(mode="after")
    def at_least_one_target(self):
        if not any((self.render_id, self.episode_id, self.storyboard_id, self.film_id)):
            raise ValueError("render_read needs renderId, episodeId, storyboardId or filmId")
        return self


# --- persisted task ----------------------------------------------------------


class RenderSceneProgress(Contract):
    scene_id: Identifier | None = None
    storyboard_id: Identifier
    shot_ids: JsonTuple[Identifier] = Field(min_length=1, max_length=64)
    status: Literal["pending", "running", "succeeded", "failed", "skipped"] = "pending"
    failure_stage: Literal["source", "subtitle", "audio", "mix", "encode", "validate"] | None = None
    error_code: str | None = Field(default=None, min_length=1, max_length=80)
    duration_seconds: PositiveSeconds | None = None

    @model_validator(mode="after")
    def coherent_status(self):
        if self.status == "failed" and self.error_code is None:
            raise ValueError("A failed scene names its error")
        if self.status != "failed" and (self.error_code is not None or self.failure_stage is not None):
            raise ValueError("Only a failed scene carries failure details")
        return self


class RenderTask(Contract):
    id: Identifier
    project_id: Identifier
    scope: Literal["storyboard", "episode"]
    storyboard_id: Identifier | None = None
    episode_id: Identifier | None = None
    track_id: Identifier | None = None
    track_version: int | None = Field(default=None, ge=1)
    profile_name: Literal["720p-h264-aac-v1", "1080p-h264-aac-v1"]
    subtitle_mode: Literal["soft", "burn", "both"]
    fingerprint: Fingerprint
    status: Literal["running", "succeeded", "failed"]
    scenes: JsonTuple[RenderSceneProgress] = Field(min_length=1, max_length=RENDER_SCENE_LIMIT)
    estimated_seconds: float = Field(ge=0, allow_inf_nan=False)
    film_id: Identifier | None = None
    film_version: int | None = Field(default=None, ge=1)
    failure_stage: Literal["source", "subtitle", "audio", "mix", "encode", "validate"] | None = None
    error_code: str | None = Field(default=None, min_length=1, max_length=80)
    created_at: UtcTimestamp
    updated_at: UtcTimestamp
    revision: int = Field(ge=0)

    @model_validator(mode="after")
    def coherent_task(self):
        if (self.storyboard_id is None) == (self.episode_id is None):
            raise ValueError("A render task belongs to exactly one storyboard or episode")
        if self.scope == "storyboard" and self.storyboard_id is None:
            raise ValueError("A storyboard task names its storyboard")
        if self.scope == "episode" and self.episode_id is None:
            raise ValueError("An episode task names its episode")
        if self.status == "succeeded":
            if self.film_id is None or self.film_version is None:
                raise ValueError("A succeeded task names the film version it committed")
            if self.error_code is not None or self.failure_stage is not None:
                raise ValueError("A succeeded task carries no failure")
        elif self.status == "failed":
            if self.error_code is None:
                raise ValueError("A failed task names its error")
        elif self.film_id is not None or self.film_version is not None:
            raise ValueError("A running task has not committed a film")
        if self.scope == "storyboard" and len(self.scenes) != 1:
            raise ValueError("A storyboard render has exactly one scene")
        if any(scene.storyboard_id is None for scene in self.scenes):
            raise ValueError("Every scene names its storyboard")
        return self


def render_task_sha256(task: RenderTask) -> str:
    return canonical_sha256(task.model_dump(mode="json", by_alias=True))


# --- timeline ----------------------------------------------------------------


@dataclass(frozen=True)
class ShotEntry:
    """One source shot on the frozen, contiguous timeline."""

    shot_id: str
    storyboard_id: str
    scene_id: str | None
    start: Fraction
    duration: Fraction
    source: str = "storyboard"


@dataclass(frozen=True)
class EffectiveEntry:
    shot_id: str
    storyboard_id: str
    scene_id: str | None
    start: Fraction
    duration: Fraction
    source_start: Fraction
    incoming: str | None = None
    outgoing: str | None = None


def _transition_seconds(transition: RenderTransitionArg) -> Fraction:
    return Fraction(transition.duration_ms, 1000)


def _seconds(value: Fraction) -> float:
    return float(value)


def validate_transitions(
    entries: list[ShotEntry], transitions: tuple[RenderTransitionArg, ...]
) -> dict[str, tuple[str, Fraction]]:
    """Validate boundaries against the contiguous shot timeline; return active cuts."""
    positions = {entry.shot_id: index for index, entry in enumerate(entries)}
    active: dict[str, tuple[str, Fraction]] = {}
    total = sum((entry.duration for entry in entries), Fraction(0))
    for transition in transitions:
        index = positions.get(transition.after_shot_id)
        if index is None:
            raise RenderError("TRANSITION_INVALID", "（转场引用了不存在的镜头）", stage="source")
        if index == len(entries) - 1:
            raise RenderError("TRANSITION_INVALID", "（最后一个镜头之后不能有转场）", stage="source")
        if transition.type == "cut":
            continue
        duration = _transition_seconds(transition)
        if duration <= 0:
            raise RenderError("TRANSITION_INVALID", "（转场时长必须为正）", stage="source")
        before, after = entries[index], entries[index + 1]
        if duration > before.duration / 3 or duration > after.duration / 3:
            raise RenderError(
                "TRANSITION_INVALID", "（转场不得超过相邻任一镜头时长的三分之一）", stage="source"
            )
        active[transition.after_shot_id] = (transition.type, duration)
    if sum((duration for _kind, duration in active.values()), Fraction(0)) >= total:
        raise RenderError("TRANSITION_INVALID", "（转场总时长超过时间线）", stage="source")
    return active


def effective_entries(
    entries: list[ShotEntry], active: dict[str, tuple[str, Fraction]]
) -> list[EffectiveEntry]:
    """Drop each outgoing shot's transition tail; the next shot keeps its full length."""
    result: list[EffectiveEntry] = []
    offset = Fraction(0)
    for index, entry in enumerate(entries):
        incoming = entries[index - 1].shot_id if index else None
        outgoing = entry.shot_id if entry.shot_id in active else None
        cut = active[outgoing][1] if outgoing is not None else Fraction(0)
        duration = entry.duration - cut
        if duration <= 0:
            raise RenderError("TRANSITION_INVALID", "（转场吃掉了整个镜头）", stage="source")
        result.append(
            EffectiveEntry(
                shot_id=entry.shot_id,
                storyboard_id=entry.storyboard_id,
                scene_id=entry.scene_id,
                start=offset,
                duration=duration,
                source_start=entry.start,
                incoming=incoming,
                outgoing=outgoing,
            )
        )
        offset += duration
    return result


def effective_duration(entries: list[EffectiveEntry]) -> Fraction:
    return entries[-1].start + entries[-1].duration if entries else Fraction(0)


def remap_microseconds(value: int, entries: list[ShotEntry], effective: list[EffectiveEntry]) -> int:
    """Map a source microsecond to the effective timeline, clamping at shot edges."""
    microseconds = Fraction(value, 1_000_000)
    windows = [(entry, entry.start + entry.duration) for entry in entries]
    for index, (entry, high) in enumerate(windows):
        if value < 0:
            break
        if not entry.start <= microseconds < high:
            if microseconds == high and index + 1 == len(windows):
                return round(effective[-1].start + effective[-1].duration) * 1_000_000 if value else value
            continue
        mapped = effective[index].start + (microseconds - entry.start)
        low = effective[index].start
        upper = effective[index].start + effective[index].duration
        mapped = min(max(mapped, low), upper)
        return round(mapped * 1_000_000)
    return round(value)


def remap_cues(cues, entries: list[ShotEntry], effective: list[EffectiveEntry]):
    """Clamp every cue into its effective shot window; never re-time a boundary."""
    positions = {entry.shot_id: index for index, entry in enumerate(entries)}
    windows = {entry.shot_id: entry for entry in entries}
    the_end = round(effective_duration(effective) * 1_000_000)
    result = []
    previous_end = 0
    for cue in cues:
        index = positions.get(cue.shot_id) if cue.shot_id is not None else None
        if index is None:
            # A cue without a shot citation is mapped by its source time.
            source_low = cue.start.total_microseconds()
            index = next(
                (
                    number
                    for number, entry in enumerate(entries)
                    if entry.start * 1_000_000 <= source_low < (entry.start + entry.duration) * 1_000_000
                ),
                0,
            )
        shot = windows[cue.shot_id] if cue.shot_id in windows else entries[index]
        target = effective[index]
        start = target.start + (Fraction(cue.start.total_microseconds(), 1_000_000) - shot.start)
        end = target.start + (Fraction(cue.end.total_microseconds(), 1_000_000) - shot.start)
        low, high = target.start, target.start + target.duration
        start = min(max(start, low), high)
        end = min(max(end, low), high)
        start_us, end_us = round(start * 1_000_000), round(end * 1_000_000)
        start_us = max(start_us, previous_end)
        end_us = min(end_us, the_end)
        if end_us - start_us < 1_000:
            continue
        previous_end = end_us
        result.append(
            cue.model_copy(
                update={
                    "start": Rational.microseconds(start_us),
                    "end": Rational.microseconds(end_us),
                }
            )
        )
    return result


def validate_title_cards(cards: tuple[RenderTitleCardArg, ...], total_seconds: Fraction) -> list[tuple]:
    if len(cards) > TITLE_CARD_LIMIT:
        raise RenderError("TITLE_CARD_LIMIT", "（超过 4 条）", stage="source")
    spans = []
    for card in cards:
        start = Fraction(card.start_ms, 1000)
        duration = Fraction(card.duration_ms, 1000)
        end = start + duration
        if end > total_seconds:
            raise RenderError("TITLE_CARD_LIMIT", "（标题卡超出成片时长）", stage="source")
        spans.append((start, end, card))
    spans.sort(key=lambda item: (item[0], item[1]))
    for previous, current in zip(spans, spans[1:]):
        if current[0] < previous[1]:
            raise RenderError("TITLE_CARD_LIMIT", "（标题卡时间重叠）", stage="source")
    return spans


# --- fingerprint -------------------------------------------------------------


def render_fingerprint(payload: dict) -> str:
    return canonical_sha256(payload)


def canonical_json(value: JsonValue) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False, separators=(",", ":"))


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


# --- persisted views ---------------------------------------------------------


def _scene_view(scene: RenderSceneProgress) -> dict:
    return scene.model_dump(mode="json", by_alias=True)


def render_task_view(state: dict, task: RenderTask) -> dict:
    """A bounded projection: statuses, measurements and controlled media URLs."""
    film = state["films"].get(task.film_id) if task.film_id else None
    film_version = None
    render = None
    media_url = None
    if film is not None:
        parsed = Film.model_validate(film)
        film_version = next((item for item in parsed.versions if item.version == task.film_version), None)
        if film_version is not None:
            render = film_version.render
            if film_version.output_media_id in state["media"]:
                media_url = f"/api/media/{film_version.output_media_id}/content"
    from vagent.video.films import render_view

    return {
        "renderId": task.id,
        "projectId": task.project_id,
        "scope": task.scope,
        "storyboardId": task.storyboard_id,
        "episodeId": task.episode_id,
        "trackId": task.track_id,
        "trackVersion": task.track_version,
        "profile": task.profile_name,
        "subtitleMode": task.subtitle_mode,
        "fingerprint": task.fingerprint,
        "status": task.status,
        "scenes": [_scene_view(scene) for scene in task.scenes],
        "estimatedSeconds": task.estimated_seconds,
        "filmId": task.film_id,
        "filmVersion": task.film_version,
        "failureStage": task.failure_stage,
        "errorCode": task.error_code,
        "createdAt": task.created_at,
        "updatedAt": task.updated_at,
        "revision": task.revision,
        "mediaUrl": media_url,
        "render": (
            render_view(render, film_id=task.film_id, film_version=task.film_version)
            if render is not None
            else None
        ),
    }


def latest_render_task(
    state: dict,
    project_id: str,
    *,
    episode_id: str | None = None,
    storyboard_id: str | None = None,
    film_id: str | None = None,
) -> RenderTask | None:
    candidates = []
    for raw in state.get("renders", {}).values():
        if raw["projectId"] != project_id:
            continue
        if episode_id is not None and raw.get("episodeId") != episode_id:
            continue
        if storyboard_id is not None and raw.get("storyboardId") != storyboard_id:
            continue
        if film_id is not None and raw.get("filmId") != film_id:
            continue
        candidates.append(RenderTask.model_validate(raw))
    if not candidates:
        return None
    return max(candidates, key=lambda item: (item.created_at, item.id))


def rendered_cues(state: dict, film: Film, version):
    """The cues a rendered sidecar must contain, re-derived from the frozen track."""
    from vagent.video.subtitles import track_version

    render = version.render
    if render is None or render.subtitle is None:
        return None
    _track, frozen = track_version(state, render.subtitle.track_id, render.subtitle.track_version)
    cues = list(frozen.cues)
    if not render.subtitle.retimed:
        return cues
    entries = [
        ShotEntry(
            shot_id=item.shot_id,
            storyboard_id="",
            scene_id=None,
            start=item.start.fraction(),
            duration=item.duration.fraction(),
        )
        for item in frozen.timeline
    ]
    active = {item.after_shot_id: (item.type, item.duration.fraction()) for item in render.transitions}
    return remap_cues(cues, entries, effective_entries(entries, active))


def render_sidecar_text(state: dict, film: Film, version, *, kind: str = "srt") -> tuple[str, str]:
    from vagent.video.subtitles import render_ass, render_srt

    cues = rendered_cues(state, film, version)
    if cues is None:
        raise AppError("SUBTITLE_TRACK_MISSING", "这个渲染版本没有字幕。")
    text = render_srt(cues) if kind == "srt" else render_ass(cues)
    suffix = "srt" if kind == "srt" else "ass"
    return text, f"film-{film.id}-v{version.version}.{suffix}"


def render_read_view(state: dict, task: RenderTask | None, *, film=None) -> dict:
    """render_read result: the latest task, or a film-only projection for old renders."""
    if task is not None:
        return {"render": render_task_view(state, task)}
    if film is None:
        return {"render": None}
    from vagent.video.films import Film as FilmModel
    from vagent.video.films import render_view

    parsed = film if isinstance(film, FilmModel) else FilmModel.model_validate(film)
    versions = [item for item in parsed.versions if item.render is not None]
    if not versions:
        return {"render": None}
    version = versions[-1]
    return {
        "render": {
            "renderId": None,
            "projectId": parsed.project_id,
            "scope": "episode" if parsed.episode_id is not None else "storyboard",
            "storyboardId": parsed.storyboard_id,
            "episodeId": parsed.episode_id,
            "status": "succeeded",
            "filmId": parsed.id,
            "filmVersion": version.version,
            "render": render_view(version.render, film_id=parsed.id, film_version=version.version),
            "scenes": [],
            "failureStage": None,
            "errorCode": None,
        }
    }


def validate_render_changes(previous: dict[str, dict], state: dict) -> None:
    """Tasks are append-only attempts; progress may advance but the target cannot move."""
    tasks = state.get("renders", {})
    if previous.keys() - tasks.keys():
        raise AppError("RENDER_CONFLICT", "已保存的渲染任务不能删除。")
    if len(tasks) > RENDER_TASK_LIMIT:
        raise AppError("RENDER_CONFLICT", "渲染任务数量超过本地上限。")
    for key, raw in tasks.items():
        task = RenderTask.model_validate(raw)
        if task.id != key or task.project_id not in state["projects"]:
            raise AppError("RENDER_CONFLICT", "渲染任务 ID 或所属项目不匹配。")
        if task.storyboard_id is not None and task.storyboard_id not in state["storyboards"]:
            raise AppError("RENDER_CONFLICT", "渲染任务引用了不存在的分镜。")
        if task.episode_id is not None and task.episode_id not in state["episodes"]:
            raise AppError("RENDER_CONFLICT", "渲染任务引用了不存在的分集。")
        if task.film_id is not None:
            film = state["films"].get(task.film_id)
            if film is None:
                raise AppError("RENDER_CONFLICT", "渲染任务引用了不存在的成片。")
            Film.model_validate(film)
        old_raw = previous.get(key)
        if old_raw is None:
            if task.revision != 0:
                raise AppError("RENDER_CONFLICT", "新渲染任务必须从 revision 0 开始。")
            continue
        old = RenderTask.model_validate(old_raw)
        if task == old:
            continue
        if (
            task.project_id != old.project_id
            or task.scope != old.scope
            or task.storyboard_id != old.storyboard_id
            or task.episode_id != old.episode_id
            or task.created_at != old.created_at
            or task.revision != old.revision + 1
        ):
            raise AppError("RENDER_CONFLICT", "渲染任务只可推进进度，不可改变目标或历史。")
        if old.status != "running" and task != old:
            raise AppError("RENDER_CONFLICT", "终态渲染任务不可再改写。")


def build_task(
    *,
    task_id: str,
    project_id: str,
    args: FilmRenderArgs,
    fingerprint: str,
    scenes: list[RenderSceneProgress],
    estimated_seconds: float,
    timestamp: str,
) -> RenderTask:
    return RenderTask(
        id=task_id,
        project_id=project_id,
        scope=args.scope,
        storyboard_id=args.storyboard_id,
        episode_id=args.episode_id,
        track_id=args.track_id,
        track_version=args.track_version,
        profile_name=args.profile,
        subtitle_mode=args.subtitle_mode,
        fingerprint=fingerprint,
        status="running",
        scenes=tuple(scenes),
        estimated_seconds=estimated_seconds,
        created_at=timestamp,
        updated_at=timestamp,
        revision=0,
    )


def finite_seconds(value: Fraction) -> float:
    return float(value)


def estimated_render_seconds(total_seconds: Fraction, tracks: int = 0) -> float:
    """A conservative local upper bound: two encodes plus the audio passes."""
    base = _seconds(total_seconds) * 2.0 + 20.0 + max(0, tracks) * 2.0
    return round(min(base, 600.0), 1)


def audio_track_limit() -> int:
    return MAX_RENDER_AUDIO_TRACKS


def render_duration_limit() -> int:
    return RENDER_MAX_SECONDS


def profile_is_known(value: str) -> bool:
    return value in {"720p-h264-aac-v1", "1080p-h264-aac-v1"}


def task_from_state(state: dict, render_id: str, project_id: str) -> RenderTask:
    raw = state.get("renders", {}).get(render_id)
    if raw is None or raw["projectId"] != project_id:
        raise AppError("RENDER_NOT_FOUND", "当前项目中没有这个渲染任务。")
    return RenderTask.model_validate(raw)


def positive_duration(value: Fraction) -> PositiveSeconds:
    return float(value)


def fraction_of_milliseconds(value: int) -> Fraction:
    return Fraction(value, 1000)


def total_microseconds(value: Fraction) -> int:
    return round(value * 1_000_000)
