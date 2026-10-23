"""M6 E1 subtitle domain: deterministic alignment, append-only tracks, SRT/ASS.

The aligner has exactly two inputs, as the frozen contract requires: the dialogue
frozen in a scene version and an exact ordered shot timeline. It never re-reads the
source text and never guesses a line. Cue boundaries are computed in integer
microseconds with largest-remainder allocation, so the same input bytes always
produce the same cue bytes and no seam can accumulate float drift.

Tracks are append-only: a manual correction appends a new version and keeps every
earlier one. Nothing here starts a process or touches the network; the soft/burn
export lives in ``vagent.video.subtitle_service``.
"""

import hashlib
import json
from datetime import UTC, datetime
from fractions import Fraction
from typing import Literal
from uuid import uuid4

from pydantic import Field, JsonValue, model_validator

from vagent.contracts import Contract, Fingerprint, Identifier, JsonTuple, UtcTimestamp
from vagent.errors import AppError
from vagent.video.episodes import Episode, Scene, SceneDialogue
from vagent.video.films import MICROSECONDS, FilmVersion, Rational, find_film
from vagent.video.storyboards import storyboard_in_state
from vagent.video.subtitle_font import FONT_NAME, FontRef, bundled_font_path, font_ref

SUBTITLE_ALIGN_VERSION = "subtitle-align-zh-v1"
SUBTITLE_STYLE_PROFILE = "subtitle-zh-v1"
SUBTITLE_CUE_LIMIT = 200
SUBTITLE_CUE_MAX_CHARS = 48
SUBTITLE_LINE_MAX_CHARS = 24
SUBTITLE_SCENE_CUE_LIMIT = 40
SUBTITLE_VERSION_LIMIT = 64
SUBTITLE_TIMELINE_LIMIT = 200
SUBTITLE_MIN_CUE_MICROSECONDS = 500_000
SUBTITLE_GAP_MICROSECONDS = 50_000
SUBTITLE_TOOL_NAME = "subtitle_save"
MESSAGES = {
    "SUBTITLE_CUE_CONFLICT": "字幕时间与镜头时间轴冲突，或同一镜头的字幕无法排布。",
    "SUBTITLE_OVERFLOW": "字幕超出时长、字符或条数上限。",
    "SUBTITLE_TRACK_MISSING": "需要字幕轨，但没有找到可用的字幕版本。",
    "RENDER_SOURCE_MISSING": "字幕来源缺少绑定的场记或分镜。",
}

TERMINATORS = "。！？；…!?;"
CLOSERS = "”’」』）】》\"'"
SECONDARY = "，、：,;—"
ASS_STYLE = {
    "font_size": 52,
    "primary_colour": "&H00FFFFFF",
    "outline_colour": "&H00101010",
    "back_colour": "&H80000000",
    "outline": 2.5,
    "shadow": 1.0,
    "margin_v": 54,
    "margin_lr": 40,
}


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


# --- models -----------------------------------------------------------------


class SubtitleCue(Contract):
    """One display cue on the frozen timeline; times are exact microseconds."""

    start: Rational
    end: Rational
    text: str = Field(min_length=1, max_length=SUBTITLE_CUE_MAX_CHARS)
    speaker: str | None = Field(default=None, min_length=1, max_length=40)
    scene_id: Identifier | None = None
    shot_id: Identifier | None = None
    source_sha256: Fingerprint

    @model_validator(mode="after")
    def coherent_cue(self):
        if self.end.fraction() <= self.start.fraction():
            raise ValueError("A subtitle cue must end after it starts")
        if any(char in self.text for char in "\r\n\t"):
            raise ValueError("Cue text cannot contain control line breaks")
        if self.text != self.text.strip():
            raise ValueError("Cue text must not have leading or trailing whitespace")
        return self


class SubtitleTimelineEntry(Contract):
    scene_id: Identifier | None = None
    shot_id: Identifier
    start: Rational
    duration: Rational


class SubtitleTrackVersion(Contract):
    version: int = Field(ge=1)
    scope: Literal["storyboard", "episode"]
    storyboard_version: int | None = Field(default=None, ge=1)
    episode_version: int | None = Field(default=None, ge=1)
    timeline_source: Literal["film", "storyboard"]
    film_version: int | None = Field(default=None, ge=1)
    timeline: JsonTuple[SubtitleTimelineEntry] = Field(min_length=1, max_length=SUBTITLE_TIMELINE_LIMIT)
    cues: JsonTuple[SubtitleCue] = Field(default=(), max_length=SUBTITLE_CUE_LIMIT)
    style_profile: Literal["subtitle-zh-v1"] = SUBTITLE_STYLE_PROFILE
    font_ref: FontRef
    timeline_sha256: Fingerprint
    source_sha256: Fingerprint
    created_at: UtcTimestamp

    @model_validator(mode="after")
    def coherent_version(self):
        if self.scope == "storyboard":
            if self.storyboard_version is None or self.episode_version is not None:
                raise ValueError("A storyboard track names only its storyboard version")
        elif self.episode_version is None or self.storyboard_version is not None:
            raise ValueError("An episode track names only its episode version")
        if self.timeline_source == "film" and self.scope == "storyboard" and self.film_version is None:
            raise ValueError("A film timeline names the film version it froze")
        end_us = timeline_windows_us(self.timeline)[-1][1]
        offset = 0
        for entry in self.timeline:
            if entry.duration.fraction() <= 0:
                raise ValueError("Timeline entries must have a positive duration")
            if entry.start.fraction() != offset:
                raise ValueError("Timeline entries must be contiguous from zero")
            offset += entry.duration.fraction()
        scene_counts: dict[str, int] = {}
        for cue in self.cues:
            if cue.end.total_microseconds() > end_us:
                raise ValueError("A cue cannot leave the frozen timeline")
            if cue.shot_id is not None:
                # Cue times are exact microseconds while the film timeline is exact
                # rational seconds; compare both windows in the cue time base.
                windows = {
                    item.shot_id: window
                    for item, window in zip(self.timeline, timeline_windows_us(self.timeline), strict=True)
                }
                window = windows.get(cue.shot_id)
                if window is None:
                    raise ValueError("A cue must cite a shot of its timeline")
                if (
                    not window[0] <= cue.start.total_microseconds()
                    or cue.end.total_microseconds() > window[1]
                ):
                    raise ValueError("A cue cannot cross a shot boundary")
            if cue.scene_id is not None:
                scene_counts[cue.scene_id] = scene_counts.get(cue.scene_id, 0) + 1
        if any(count > SUBTITLE_SCENE_CUE_LIMIT for count in scene_counts.values()):
            raise ValueError("A scene cannot hold more than 40 subtitle cues")
        for previous, current in zip(self.cues, self.cues[1:], strict=False):
            if current.start.fraction() < previous.end.fraction():
                raise ValueError("Subtitle cues cannot overlap or run backwards")
        return self


class SubtitleTrack(Contract):
    id: Identifier
    project_id: Identifier
    scope: Literal["storyboard", "episode"]
    storyboard_id: Identifier | None = None
    episode_id: Identifier | None = None
    current_version: int = Field(ge=1)
    versions: JsonTuple[SubtitleTrackVersion] = Field(min_length=1, max_length=SUBTITLE_VERSION_LIMIT)
    revision: int = Field(ge=0)
    created_at: UtcTimestamp

    @model_validator(mode="after")
    def append_only_history(self):
        if [item.version for item in self.versions] != list(range(1, len(self.versions) + 1)):
            raise ValueError("Subtitle versions must be the continuous 1..N sequence")
        if self.current_version != len(self.versions):
            raise ValueError("currentVersion must identify the latest subtitle version")
        if self.scope == "storyboard":
            if self.storyboard_id is None or self.episode_id is not None:
                raise ValueError("A storyboard track names only its storyboard")
        elif self.episode_id is None or self.storyboard_id is not None:
            raise ValueError("An episode track names only its episode")
        if any(item.scope != self.scope for item in self.versions):
            raise ValueError("Every version must keep the track scope")
        return self

    @property
    def latest(self) -> SubtitleTrackVersion:
        return self.versions[-1]


# --- canonical identity ------------------------------------------------------


def canonical_sha256(value: JsonValue) -> str:
    encoded = json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def cue_payload(cue: SubtitleCue) -> dict:
    return cue.model_dump(mode="json", by_alias=True)


def track_version_sha256(version: SubtitleTrackVersion) -> str:
    """Output-affecting identity: no version number, timestamp or revision."""
    return canonical_sha256(
        {
            "alignVersion": SUBTITLE_ALIGN_VERSION,
            "styleProfile": version.style_profile,
            "scope": version.scope,
            "storyboardVersion": version.storyboard_version,
            "episodeVersion": version.episode_version,
            "timelineSource": version.timeline_source,
            "timelineSha256": version.timeline_sha256,
            "sourceSha256": version.source_sha256,
            "fontRef": version.font_ref.model_dump(mode="json", by_alias=True),
            "cues": [cue_payload(cue) for cue in version.cues],
        }
    )


def _fraction_microseconds(value) -> int:
    """Round an exact rational second to the nearest microsecond (half up)."""
    return (value.numerator * MICROSECONDS * 2 + value.denominator) // (2 * value.denominator)


def timeline_windows_us(entries) -> list[tuple[int, int]]:
    """Canonical microsecond windows of a contiguous timeline.

    Each boundary is rounded exactly once: a shot ends where the next shot starts,
    so a cue that fills a shot can never appear to cross the next one by a rounding
    microsecond. The final window ends at the timeline's exact duration.
    """
    windows = []
    for index, entry in enumerate(entries):
        low = entry.start.total_microseconds()
        if index + 1 < len(entries):
            high = entries[index + 1].start.total_microseconds()
        else:
            high = _fraction_microseconds(entry.start.fraction() + entry.duration.fraction())
        windows.append((low, high))
    return windows


def timeline_payload(entries) -> dict:
    return {
        "alignVersion": SUBTITLE_ALIGN_VERSION,
        "entries": [
            {
                "sceneId": entry.scene_id,
                "shotId": entry.shot_id,
                "start": entry.start.model_dump(mode="json", by_alias=True),
                "duration": entry.duration.model_dump(mode="json", by_alias=True),
            }
            for entry in entries
        ],
    }


# --- lookups ----------------------------------------------------------------


def find_track(state: dict, project_id: str, scope: str, target_id: str) -> SubtitleTrack | None:
    for raw in state["subtitleTracks"].values():
        if raw["projectId"] != project_id or raw["scope"] != scope:
            continue
        if scope == "storyboard" and raw.get("storyboardId") == target_id:
            return SubtitleTrack.model_validate(raw)
        if scope == "episode" and raw.get("episodeId") == target_id:
            return SubtitleTrack.model_validate(raw)
    return None


def track_by_id(state: dict, track_id: str, project_id: str) -> SubtitleTrack | None:
    raw = state["subtitleTracks"].get(track_id)
    if raw is None or raw["projectId"] != project_id:
        return None
    return SubtitleTrack.model_validate(raw)


def scene_for_storyboard(state: dict, storyboard_id: str) -> Scene | None:
    for raw in state["scenes"].values():
        if raw.get("storyboardId") == storyboard_id:
            return Scene.model_validate(raw)
    return None


# --- splitting --------------------------------------------------------------


def _strip(text: str) -> str:
    return text.strip()


def split_dialogue(text: str) -> list[str]:
    """Split one dialogue line into deterministic cues of at most 48 characters."""
    text = _strip(text)
    if not text:
        return []
    sentences, buffer = [], []
    index = 0
    while index < len(text):
        char = text[index]
        buffer.append(char)
        if char in TERMINATORS:
            # Keep a run of terminators and closing quotes with the same sentence.
            while index + 1 < len(text) and (text[index + 1] in TERMINATORS or text[index + 1] in CLOSERS):
                index += 1
                buffer.append(text[index])
            sentences.append("".join(buffer))
            buffer = []
        index += 1
    if buffer:
        sentences.append("".join(buffer))
    cues: list[str] = []
    for sentence in sentences:
        cues.extend(_split_length(_strip(sentence)))
    return [cue for cue in cues if cue]


def _split_length(segment: str, limit: int = SUBTITLE_LINE_MAX_CHARS) -> list[str]:
    """Cut at secondary punctuation to ``limit``; keep an unbreakable 25..48 run whole."""
    if len(segment) <= limit:
        return [segment]
    positions = [index + 1 for index, char in enumerate(segment) if char in SECONDARY and index + 1 <= limit]
    if positions:
        cut = positions[-1]
        return [segment[:cut], *_split_length(segment[cut:], limit)]
    if len(segment) <= SUBTITLE_CUE_MAX_CHARS:
        return [segment]
    return [segment[:SUBTITLE_CUE_MAX_CHARS], *_split_length(segment[SUBTITLE_CUE_MAX_CHARS:], limit)]


def wrap_lines(text: str) -> list[str]:
    """Lay one cue out as at most two lines of at most 24 characters each."""
    if len(text) <= SUBTITLE_LINE_MAX_CHARS:
        return [text]
    positions = [
        index + 1
        for index, char in enumerate(text)
        if char in SECONDARY and 0 < index + 1 <= SUBTITLE_LINE_MAX_CHARS
    ]
    cut = positions[-1] if positions else (len(text) + 1) // 2
    first, second = text[:cut], text[cut:]
    return [part for part in (first, second) if part]


# --- alignment --------------------------------------------------------------


def _dialogue_lines(scene: Scene | None, scene_version: int | None = None) -> list[SceneDialogue]:
    if scene is None:
        return []
    version = (
        next((item for item in scene.versions if item.version == scene_version), None)
        if scene_version is not None
        else scene.latest
    )
    return list(version.dialogue) if version is not None else []


def _largest_remainder(total: int, weights: list[int]) -> list[int]:
    """Split ``total`` integer ticks exactly by integer weights, deterministically."""
    weight_sum = sum(weights)
    if total <= 0 or weight_sum <= 0:
        return [0] * len(weights)
    shares = [total * weight // weight_sum for weight in weights]
    remainders = [total * weight % weight_sum for weight in weights]
    remaining = total - sum(shares)
    order = sorted(range(len(weights)), key=lambda index: (-remainders[index], index))
    for index in order[:remaining]:
        shares[index] += 1
    return shares


def _allocate_shot(duration: int, texts: list[str]) -> list[int]:
    """Give every cue at least 0.5 s plus a character-proportional share, exact sum."""
    count = len(texts)
    if count == 0:
        return []
    available = duration - SUBTITLE_GAP_MICROSECONDS * (count - 1)
    if available < count * SUBTITLE_MIN_CUE_MICROSECONDS:
        raise AppError("SUBTITLE_CUE_CONFLICT", MESSAGES["SUBTITLE_CUE_CONFLICT"])
    extra = available - count * SUBTITLE_MIN_CUE_MICROSECONDS
    shares = _largest_remainder(extra, [len(text) for text in texts])
    return [SUBTITLE_MIN_CUE_MICROSECONDS + share for share in shares]


def _merge_pair(texts: list[str], speakers: list[str | None]):
    """Merge the cheapest adjacent pair whose combined text still fits one cue."""
    best = None
    for index in range(len(texts) - 1):
        combined = texts[index] + texts[index + 1]
        if len(combined) > SUBTITLE_CUE_MAX_CHARS:
            continue
        cost = (len(combined), index)
        if best is None or cost < best[0]:
            best = (cost, index)
    if best is None:
        return None
    index = best[1]
    merged_speaker = speakers[index] if speakers[index] == speakers[index + 1] else None
    return (
        [*texts[:index], texts[index] + texts[index + 1], *texts[index + 2 :]],
        [*speakers[:index], merged_speaker, *speakers[index + 2 :]],
    )


def _align_scene(
    scene_id: str | None,
    entries: list[SubtitleTimelineEntry],
    lines: list[SceneDialogue],
    source_sha256: str,
) -> list[SubtitleCue]:
    """Place one scene's dialogue on its own shots without ever crossing a boundary.

    The scene's dialogue stream is mapped onto the scene duration by character
    position: shot *i* owns the characters between two proportional boundaries. A
    sentence crossing a boundary is therefore split there, and both halves keep the
    original speaker. Inside one shot the duration is distributed by character count
    from an exact integer microsecond budget.
    """
    if not lines or not entries:
        return []
    pieces: list[tuple[str, str | None]] = []
    for line in lines:
        for piece in split_dialogue(line.text):
            pieces.append((piece, line.speaker))
    if not pieces:
        return []
    ranges = []
    cursor = 0
    for text, speaker in pieces:
        ranges.append((cursor, cursor + len(text), text, speaker))
        cursor += len(text)
    total_chars = cursor
    windows = timeline_windows_us(entries)
    entries_us = [(low, high - low) for low, high in windows]
    scene_duration = windows[-1][1] - windows[0][0]
    if scene_duration <= 0:
        raise AppError("SUBTITLE_CUE_CONFLICT", MESSAGES["SUBTITLE_CUE_CONFLICT"])
    boundaries = [0]
    elapsed = 0
    for _start, duration in entries_us[:-1]:
        elapsed += duration
        boundaries.append(total_chars * elapsed // scene_duration)
    boundaries.append(total_chars)
    cues: list[SubtitleCue] = []
    for index, (start_us, duration_us) in enumerate(entries_us):
        low, high = boundaries[index], boundaries[index + 1]
        if low >= high:
            continue
        texts: list[str] = []
        speakers: list[str | None] = []
        for begin, end, text, speaker in ranges:
            overlap_start, overlap_end = max(begin, low), min(end, high)
            if overlap_start >= overlap_end:
                continue
            chunk = text[overlap_start - begin : overlap_end - begin].strip()
            if chunk:
                texts.append(chunk)
                speakers.append(speaker)
        if not texts:
            continue
        # Merge adjacent cues inside the shot when the 0.5 s minimum cannot fit.
        while len(texts) > 1:
            available = duration_us - SUBTITLE_GAP_MICROSECONDS * (len(texts) - 1)
            if available >= len(texts) * SUBTITLE_MIN_CUE_MICROSECONDS:
                break
            merged = _merge_pair(texts, speakers)
            if merged is None:
                raise AppError("SUBTITLE_CUE_CONFLICT", MESSAGES["SUBTITLE_CUE_CONFLICT"])
            texts, speakers = merged
        allocations = _allocate_shot(duration_us, texts)
        cue_start = start_us
        for chunk, speaker, allocation in zip(texts, speakers, allocations, strict=True):
            cues.append(
                SubtitleCue(
                    start=Rational.microseconds(cue_start),
                    end=Rational.microseconds(cue_start + allocation),
                    text=chunk,
                    speaker=speaker,
                    scene_id=scene_id,
                    shot_id=entries[index].shot_id,
                    source_sha256=source_sha256,
                )
            )
            cue_start += allocation + SUBTITLE_GAP_MICROSECONDS
    if len(cues) > SUBTITLE_CUE_LIMIT:
        raise AppError("SUBTITLE_OVERFLOW", MESSAGES["SUBTITLE_OVERFLOW"])
    counts: dict[str | None, int] = {}
    for cue in cues:
        counts[cue.scene_id] = counts.get(cue.scene_id, 0) + 1
    if any(count > SUBTITLE_SCENE_CUE_LIMIT for count in counts.values()):
        raise AppError("SUBTITLE_OVERFLOW", MESSAGES["SUBTITLE_OVERFLOW"])
    scene_end = windows[-1][1]
    if cues and cues[-1].end.total_microseconds() > scene_end:
        raise AppError("SUBTITLE_OVERFLOW", MESSAGES["SUBTITLE_OVERFLOW"])
    return cues


def align_subtitles(
    entries: list[SubtitleTimelineEntry],
    dialogue: dict[str, list[SceneDialogue]],
    *,
    source_payload: dict,
) -> tuple[list[SubtitleCue], str]:
    """Align the frozen dialogue to the frozen timeline; return cues and source hash."""
    if not entries:
        raise AppError("RENDER_SOURCE_MISSING", MESSAGES["RENDER_SOURCE_MISSING"])
    source_sha256 = canonical_sha256(source_payload)
    cues: list[SubtitleCue] = []
    group: list[SubtitleTimelineEntry] = []
    for entry in entries:
        if group and entry.scene_id != group[-1].scene_id:
            cues.extend(
                _align_scene(
                    group[0].scene_id, group, dialogue.get(group[0].scene_id or "", []), source_sha256
                )
            )
            group = []
        group.append(entry)
    if group:
        cues.extend(
            _align_scene(group[0].scene_id, group, dialogue.get(group[0].scene_id or "", []), source_sha256)
        )
    return cues, source_sha256


# --- timeline resolution ----------------------------------------------------


def _film_base_version(film) -> FilmVersion:
    base = next((item for item in reversed(film.versions) if item.render is None), None)
    if base is None:
        raise AppError("SUBTITLE_TRACK_MISSING", MESSAGES["SUBTITLE_TRACK_MISSING"])
    return base


def _film_entries(state: dict, project_id: str, storyboard_id: str, scene_id: str | None):
    film = find_film(state, project_id, storyboard_id)
    if film is None:
        return None, None
    base = _film_base_version(film)
    # Assembly starts are rounded to microseconds, while the segment durations are exact
    # media durations. Rebuild the starts from the durations so one timeline is exactly
    # contiguous; a cue window must not depend on which rounding won.
    entries = []
    offset = Fraction(0)
    for segment in base.assembly.timeline:
        entries.append(
            SubtitleTimelineEntry(
                scene_id=scene_id,
                shot_id=segment.shot_id,
                start=Rational.of(offset),
                duration=segment.duration,
            )
        )
        offset += segment.duration.fraction()
    return entries, base


def _plan_entries(record, scene_id: str | None) -> list[SubtitleTimelineEntry]:
    entries = []
    offset = 0
    for shot in sorted(record.versions[record.current_version - 1].shots, key=lambda item: item.order):
        duration = shot.duration_seconds * MICROSECONDS
        entries.append(
            SubtitleTimelineEntry(
                scene_id=scene_id,
                shot_id=shot.id,
                start=Rational.microseconds(offset),
                duration=Rational.microseconds(duration),
            )
        )
        offset += duration
    return entries


def resolve_storyboard_source(state: dict, project_id: str, storyboard_id: str) -> dict:
    """Freeze one storyboard's timeline and its bound scene dialogue."""
    record = storyboard_in_state(state, project_id, storyboard_id)
    scene = scene_for_storyboard(state, storyboard_id)
    scene_id = scene.id if scene is not None else None
    entries, base = _film_entries(state, project_id, storyboard_id, scene_id)
    if entries is None:
        entries = _plan_entries(record, scene_id)
        source = "storyboard"
    else:
        source = "film"
    dialogue = {scene_id: _dialogue_lines(scene)} if scene_id is not None else {}
    return {
        "scope": "storyboard",
        "storyboardId": storyboard_id,
        "storyboardVersion": base.storyboard_version if base is not None else record.current_version,
        "episodeVersion": None,
        "timelineSource": source,
        "filmVersion": base.version if base is not None else None,
        "entries": entries,
        "dialogue": dialogue,
        "scenes": [scene_id] if scene_id is not None else [],
        "has_dialogue": scene is not None,
        "payload": {
            "alignVersion": SUBTITLE_ALIGN_VERSION,
            "scope": "storyboard",
            "storyboardId": storyboard_id,
            "storyboardVersion": base.storyboard_version if base is not None else record.current_version,
            "timelineSource": source,
            "filmVersion": base.version if base is not None else None,
            "timeline": timeline_payload(entries)["entries"],
            "dialogue": [
                {
                    "sceneId": scene_id,
                    "lines": [
                        {"text": line.text, "speaker": line.speaker, "paragraph": line.paragraph}
                        for line in _dialogue_lines(scene)
                    ],
                }
                if scene_id is not None
                else {"sceneId": None, "lines": []}
            ],
        },
    }


def resolve_episode_source(state: dict, project_id: str, episode_id: str) -> dict:
    """Freeze every bound scene of one episode into one continuous timeline."""
    raw = state["episodes"].get(episode_id)
    if raw is None or raw["projectId"] != project_id:
        raise AppError("EPISODE_NOT_FOUND", "当前项目中没有这个分集，未保存字幕。")
    episode = Episode.model_validate(raw)
    entries: list[SubtitleTimelineEntry] = []
    dialogue: dict[str, list[SceneDialogue]] = {}
    scenes: list[str] = []
    payload_scenes = []
    offset = 0
    films = 0
    for reference in episode.latest.scenes:
        scene = Scene.model_validate(state["scenes"][reference.scene_id])
        frozen = next(item for item in scene.versions if item.version == reference.scene_version)
        if scene.storyboard_id is None:
            raise AppError("RENDER_SOURCE_MISSING", MESSAGES["RENDER_SOURCE_MISSING"])
        record = storyboard_in_state(state, project_id, scene.storyboard_id)
        scene_entries, base = _film_entries(state, project_id, record.id, scene.id)
        if scene_entries is None:
            scene_entries = _plan_entries(record, scene.id)
        else:
            films += 1
        for entry in scene_entries:
            entries.append(
                SubtitleTimelineEntry(
                    scene_id=scene.id,
                    shot_id=entry.shot_id,
                    start=Rational.of(offset + entry.start.fraction()),
                    duration=entry.duration,
                )
            )
        offset += sum(entry.duration.fraction() for entry in scene_entries)
        dialogue[scene.id] = list(frozen.dialogue)
        scenes.append(scene.id)
        payload_scenes.append(
            {
                "sceneId": scene.id,
                "sceneVersion": frozen.version,
                "storyboardId": record.id,
                "lines": [
                    {"text": line.text, "speaker": line.speaker, "paragraph": line.paragraph}
                    for line in frozen.dialogue
                ],
            }
        )
    return {
        "scope": "episode",
        "episodeId": episode_id,
        "storyboardId": None,
        "storyboardVersion": None,
        "episodeVersion": episode.current_version,
        "timelineSource": "film" if films == len(episode.latest.scenes) else "storyboard",
        "filmVersion": None,
        "entries": entries,
        "dialogue": dialogue,
        "scenes": scenes,
        "has_dialogue": any(dialogue.values()),
        "payload": {
            "alignVersion": SUBTITLE_ALIGN_VERSION,
            "scope": "episode",
            "episodeId": episode_id,
            "episodeVersion": episode.current_version,
            "scenes": payload_scenes,
            "timeline": timeline_payload(entries)["entries"],
        },
    }


# --- manual cues ------------------------------------------------------------


def _entry_for_window(entries: list[SubtitleTimelineEntry], start_us: int, end_us: int):
    """The timeline entry owning a window; admit up to 2 ms of millisecond rounding."""
    best = None
    for entry, (low, high) in zip(entries, timeline_windows_us(entries), strict=True):
        overlap = min(end_us, high) - max(start_us, low)
        if overlap <= 0:
            continue
        slack = max(0, low - start_us) + max(0, end_us - high)
        if slack > 2000:
            continue
        if best is None or overlap > best[0]:
            best = (overlap, entry, (low, high))
    return (best[1], best[2]) if best is not None else (None, None)


def _snap_to_entry(window: tuple[int, int], start_us: int, end_us: int) -> tuple[int, int]:
    """A window within 1 ms of a shot edge is that edge (millisecond input rounding)."""
    low, high = window
    if abs(start_us - low) <= 1000:
        start_us = low
    if abs(end_us - high) <= 1000:
        end_us = high
    return start_us, end_us


def normalize_cues(raw_cues, source: dict) -> list[SubtitleCue]:
    """Validate caller-supplied cues against the frozen timeline; never re-time them."""
    if not isinstance(raw_cues, list):
        raise AppError("SUBTITLE_CUE_CONFLICT", MESSAGES["SUBTITLE_CUE_CONFLICT"])
    if len(raw_cues) > SUBTITLE_CUE_LIMIT:
        raise AppError("SUBTITLE_OVERFLOW", MESSAGES["SUBTITLE_OVERFLOW"])
    if not raw_cues:
        return []
    entries = source["entries"]
    source_sha256 = canonical_sha256(source["payload"])
    cues: list[SubtitleCue] = []
    previous_end = 0
    for raw in raw_cues:
        text = str(raw.get("text", "")).strip()
        if not text or len(text) > SUBTITLE_CUE_MAX_CHARS:
            raise AppError("SUBTITLE_OVERFLOW", MESSAGES["SUBTITLE_OVERFLOW"])
        start_ms, end_ms = raw.get("startMs"), raw.get("endMs")
        if type(start_ms) is not int or type(end_ms) is not int or start_ms < 0 or end_ms <= start_ms:
            raise AppError("SUBTITLE_CUE_CONFLICT", MESSAGES["SUBTITLE_CUE_CONFLICT"])
        start_us, end_us = start_ms * 1000, end_ms * 1000
        entry, window = _entry_for_window(entries, start_us, end_us)
        if entry is None:
            raise AppError("SUBTITLE_CUE_CONFLICT", MESSAGES["SUBTITLE_CUE_CONFLICT"])
        start_us, end_us = _snap_to_entry(window, start_us, end_us)
        low, high = window
        if start_us < low or end_us > high or end_us <= start_us:
            raise AppError("SUBTITLE_CUE_CONFLICT", MESSAGES["SUBTITLE_CUE_CONFLICT"])
        shot_id = raw.get("shotId")
        if shot_id is not None and shot_id != entry.shot_id:
            raise AppError("SUBTITLE_CUE_CONFLICT", MESSAGES["SUBTITLE_CUE_CONFLICT"])
        scene_id = raw.get("sceneId")
        if scene_id is not None and scene_id != entry.scene_id:
            raise AppError("SUBTITLE_CUE_CONFLICT", MESSAGES["SUBTITLE_CUE_CONFLICT"])
        if start_us < previous_end:
            raise AppError("SUBTITLE_CUE_CONFLICT", MESSAGES["SUBTITLE_CUE_CONFLICT"])
        previous_end = end_us
        cues.append(
            SubtitleCue(
                start=Rational.microseconds(start_us),
                end=Rational.microseconds(end_us),
                text=text,
                speaker=(str(raw["speaker"]).strip() if raw.get("speaker") else None),
                scene_id=entry.scene_id,
                shot_id=entry.shot_id,
                source_sha256=source_sha256,
            )
        )
    counts: dict[str | None, int] = {}
    for cue in cues:
        counts[cue.scene_id] = counts.get(cue.scene_id, 0) + 1
    if any(count > SUBTITLE_SCENE_CUE_LIMIT for count in counts.values()):
        raise AppError("SUBTITLE_OVERFLOW", MESSAGES["SUBTITLE_OVERFLOW"])
    return cues


# --- save -------------------------------------------------------------------


def _source_for(state: dict, project_id: str, scope: str, target_id: str) -> dict:
    if scope == "storyboard":
        return resolve_storyboard_source(state, project_id, target_id)
    return resolve_episode_source(state, project_id, target_id)


def save_subtitles(args: dict, state: dict, project_id: str) -> dict:
    """Append one subtitle version (or replay the identical one). Never re-times cues."""
    scope = args["scope"]
    target_id = args["storyboardId"] if scope == "storyboard" else args["episodeId"]
    track_id = args.get("trackId")
    track = (
        track_by_id(state, track_id, project_id)
        if track_id
        else find_track(state, project_id, scope, target_id)
    )
    if track_id and track is None:
        raise AppError("SUBTITLE_TRACK_MISSING", MESSAGES["SUBTITLE_TRACK_MISSING"])
    if track is not None and (
        track.scope != scope
        or track.storyboard_id != (target_id if scope == "storyboard" else None)
        or track.episode_id != (target_id if scope == "episode" else None)
    ):
        raise AppError("SUBTITLE_TRACK_MISSING", MESSAGES["SUBTITLE_TRACK_MISSING"])
    if track is not None and args.get("expectedRevision") is not None:
        if track.revision != args["expectedRevision"]:
            raise AppError("VERSION_CONFLICT", "字幕记录已变化，请重新读取后再保存。")
    source = _source_for(state, project_id, scope, target_id)
    bundled_font_path()
    reference = font_ref()
    if args.get("cues") is not None:
        cues = normalize_cues(args["cues"], source)
    else:
        if not source["has_dialogue"] or not any(source["dialogue"].values()):
            raise AppError("RENDER_SOURCE_MISSING", MESSAGES["RENDER_SOURCE_MISSING"])
        cues, source_sha256 = align_subtitles(
            source["entries"], source["dialogue"], source_payload=source["payload"]
        )
        source = {**source, "sourceSha256": source_sha256}
    source_sha256 = source.get("sourceSha256") or canonical_sha256(source["payload"])
    timeline_sha256 = canonical_sha256(timeline_payload(source["entries"]))
    timestamp = utc_now()
    version = SubtitleTrackVersion(
        version=track.current_version + 1 if track is not None else 1,
        scope=scope,
        storyboard_version=source["storyboardVersion"],
        episode_version=source["episodeVersion"],
        timeline_source=source["timelineSource"],
        film_version=source["filmVersion"],
        timeline=source["entries"],
        cues=cues,
        font_ref=reference,
        timeline_sha256=timeline_sha256,
        source_sha256=source_sha256,
        created_at=timestamp,
    )
    if track is not None:
        latest = track.latest
        if (
            track_version_sha256(latest) == track_version_sha256(version)
            and latest.timeline == version.timeline
            and latest.cues == version.cues
        ):
            return subtitle_view(state, track)
        if len(track.versions) >= SUBTITLE_VERSION_LIMIT:
            raise AppError("SUBTITLE_OVERFLOW", "字幕版本数已达上限，未保存新版本。")
        updated = track.model_copy(
            update={
                "current_version": version.version,
                "versions": (*track.versions, version),
                "revision": track.revision + 1,
            }
        )
    else:
        updated = SubtitleTrack(
            id=str(uuid4()),
            project_id=project_id,
            scope=scope,
            storyboard_id=target_id if scope == "storyboard" else None,
            episode_id=target_id if scope == "episode" else None,
            current_version=1,
            versions=(version,),
            revision=0,
            created_at=timestamp,
        )
    state["subtitleTracks"][updated.id] = updated.model_dump(mode="json", by_alias=True)
    return subtitle_view(state, updated)


# --- serialization ----------------------------------------------------------


def _srt_timestamp(microseconds: int) -> str:
    milliseconds = microseconds // 1000
    hours, rest = divmod(milliseconds, 3_600_000)
    minutes, rest = divmod(rest, 60_000)
    seconds, millis = divmod(rest, 1000)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d},{millis:03d}"


def _ass_timestamp(microseconds: int) -> str:
    centiseconds = microseconds // 10_000
    hours, rest = divmod(centiseconds, 360_000)
    minutes, rest = divmod(rest, 6_000)
    seconds, centis = divmod(rest, 100)
    return f"{hours}:{minutes:02d}:{seconds:02d}.{centis:02d}"


def render_srt(cues) -> str:
    """Deterministic UTF-8 SRT bytes source; lines are wrapped to at most 24 chars."""
    blocks = []
    for index, cue in enumerate(cues, start=1):
        body = "\n".join(wrap_lines(cue.text))
        blocks.append(
            f"{index}\n{_srt_timestamp(cue.start.total_microseconds())} --> "
            f"{_srt_timestamp(cue.end.total_microseconds())}\n{body}"
        )
    return "\n\n".join(blocks) + ("\n" if blocks else "")


def render_ass(cues, *, width: int = 1280, height: int = 720) -> str:
    """Deterministic ASS source using the bundled font family."""
    font_size = round(height * ASS_STYLE["font_size"] / 720)
    margin_v = round(height * ASS_STYLE["margin_v"] / 720)
    margin_lr = round(width * ASS_STYLE["margin_lr"] / 1280)
    header = (
        "[Script Info]\n"
        "ScriptType: v4.00+\n"
        f"PlayResX: {width}\n"
        f"PlayResY: {height}\n"
        "WrapStyle: 2\n"
        "ScaledBorderAndShadow: yes\n"
        f"Title: {SUBTITLE_STYLE_PROFILE}\n\n"
        "[V4+ Styles]\n"
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, "
        "Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, "
        "Shadow, Alignment, MarginL, MarginR, MarginV, Encoding\n"
        f"Style: Default,{FONT_NAME},{font_size},{ASS_STYLE['primary_colour']},&H000000FF,"
        f"{ASS_STYLE['outline_colour']},{ASS_STYLE['back_colour']},0,0,0,0,100,100,0,0,1,"
        f"{ASS_STYLE['outline']},{ASS_STYLE['shadow']},2,{margin_lr},{margin_lr},{margin_v},1\n\n"
        "[Events]\n"
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
    )
    lines = []
    for cue in cues:
        text = "\\N".join(wrap_lines(cue.text))
        lines.append(
            f"Dialogue: 0,{_ass_timestamp(cue.start.total_microseconds())},"
            f"{_ass_timestamp(cue.end.total_microseconds())},Default,,0,0,0,,{text}"
        )
    return header + "\n".join(lines) + ("\n" if lines else "")


# --- views ------------------------------------------------------------------


def _seconds(value: Rational) -> float:
    return round(value.total_microseconds() / MICROSECONDS, 6)


def cue_view(cue: SubtitleCue) -> dict:
    return {
        "startSeconds": _seconds(cue.start),
        "endSeconds": _seconds(cue.end),
        "startMs": cue.start.total_microseconds() // 1000,
        "endMs": cue.end.total_microseconds() // 1000,
        "text": cue.text,
        "speaker": cue.speaker,
        "sceneId": cue.scene_id,
        "shotId": cue.shot_id,
    }


def version_view(
    track: SubtitleTrack,
    version: SubtitleTrackVersion,
    *,
    include_cues: bool = True,
    include_digests: bool = True,
    include_timeline: bool = True,
) -> dict:
    view = {
        "version": version.version,
        "scope": version.scope,
        "storyboardVersion": version.storyboard_version,
        "episodeVersion": version.episode_version,
        "timelineSource": version.timeline_source,
        "filmVersion": version.film_version,
        "cueCount": len(version.cues),
        "sceneCount": len({cue.scene_id for cue in version.cues if cue.scene_id is not None}),
        "styleProfile": version.style_profile,
        "fontRef": version.font_ref.model_dump(mode="json", by_alias=True),
        "timelineSha256": version.timeline_sha256,
        "sourceSha256": version.source_sha256,
        "sha256": track_version_sha256(version),
        "srtUrl": f"/api/subtitles/{track.id}/{version.version}/srt",
        "assUrl": f"/api/subtitles/{track.id}/{version.version}/ass",
        "createdAt": version.created_at,
    }
    if include_timeline:
        view["timeline"] = [
            {
                "sceneId": entry.scene_id,
                "shotId": entry.shot_id,
                "startSeconds": _seconds(entry.start),
                "durationSeconds": _seconds(entry.duration),
                "startMs": entry.start.total_microseconds() // 1000,
                "durationMs": entry.duration.total_microseconds() // 1000,
            }
            for entry in version.timeline
        ]
    if include_digests:
        srt = render_srt(version.cues)
        ass = render_ass(version.cues)
        view["srtSha256"] = hashlib.sha256(srt.encode("utf-8")).hexdigest()
        view["assSha256"] = hashlib.sha256(ass.encode("utf-8")).hexdigest()
    if include_cues:
        view["cues"] = [cue_view(cue) for cue in version.cues]
    return view


def subtitle_view(
    state: dict,
    track: SubtitleTrack,
    *,
    include_cues: bool = True,
    include_digests: bool = True,
) -> dict:
    """The latest version is complete; older versions are bounded summaries."""
    history = [
        version_view(
            track, version, include_cues=False, include_digests=include_digests, include_timeline=False
        )
        for version in track.versions[:-1]
    ]
    latest = version_view(
        track,
        track.versions[-1],
        include_cues=include_cues,
        include_digests=include_digests,
    )
    versions = [*history, latest]
    return {
        "trackId": track.id,
        "projectId": track.project_id,
        "scope": track.scope,
        "storyboardId": track.storyboard_id,
        "episodeId": track.episode_id,
        "revision": track.revision,
        "currentVersion": track.current_version,
        "versionCount": len(versions),
        "createdAt": track.created_at,
        "latest": latest,
        "versions": versions,
    }


def subtitle_for(
    state: dict,
    project_id: str,
    *,
    storyboard_id: str | None = None,
    episode_id: str | None = None,
    include_cues: bool = True,
    include_digests: bool = True,
):
    """The current track summary of one target, or None."""
    if storyboard_id is not None:
        track = find_track(state, project_id, "storyboard", storyboard_id)
    elif episode_id is not None:
        track = find_track(state, project_id, "episode", episode_id)
    else:
        return None
    return (
        subtitle_view(state, track, include_cues=include_cues, include_digests=include_digests)
        if track is not None
        else None
    )


def track_version(state: dict, track_id: str, version: int) -> tuple[SubtitleTrack, SubtitleTrackVersion]:
    raw = state["subtitleTracks"].get(track_id)
    if raw is None:
        raise AppError("SUBTITLE_TRACK_MISSING", MESSAGES["SUBTITLE_TRACK_MISSING"])
    track = SubtitleTrack.model_validate(raw)
    selected = next((item for item in track.versions if item.version == version), None)
    if selected is None:
        raise AppError("SUBTITLE_TRACK_MISSING", MESSAGES["SUBTITLE_TRACK_MISSING"])
    return track, selected


# --- store validation -------------------------------------------------------


def validate_subtitle_changes(previous: dict[str, dict], state: dict) -> None:
    """Every Store commit re-checks track targets and append-only version history."""
    tracks = state.get("subtitleTracks", {})
    if previous.keys() - tracks.keys():
        raise AppError("SUBTITLE_IMMUTABLE", "已保存的字幕轨不能删除。")
    targets: set[tuple[str, str, str]] = set()
    for key, raw in tracks.items():
        track = SubtitleTrack.model_validate(raw)
        if track.id != key or track.project_id not in state["projects"]:
            raise AppError("SUBTITLE_CONFLICT", "字幕轨 ID 或所属项目不匹配。")
        if track.scope == "storyboard":
            record = state["storyboards"].get(track.storyboard_id)
            if record is None or record["projectId"] != track.project_id:
                raise AppError("SUBTITLE_CONFLICT", "字幕轨必须引用本项目的分镜。")
            target = ("storyboard", track.project_id, track.storyboard_id)
        else:
            record = state["episodes"].get(track.episode_id)
            if record is None or record["projectId"] != track.project_id:
                raise AppError("SUBTITLE_CONFLICT", "字幕轨必须引用本项目的分集。")
            target = ("episode", track.project_id, track.episode_id)
        if target in targets:
            raise AppError("SUBTITLE_CONFLICT", "同一目标至多一个字幕轨。")
        targets.add(target)
        for version in track.versions:
            if version.scope != track.scope:
                raise AppError("SUBTITLE_CONFLICT", "字幕版本与字幕轨的作用域不一致。")
        old_raw = previous.get(key)
        if old_raw is None:
            if track.revision != 0 or len(track.versions) != 1:
                raise AppError("SUBTITLE_CONFLICT", "新字幕轨必须先保存第 1 版。")
            continue
        old = SubtitleTrack.model_validate(old_raw)
        if track == old:
            continue
        if (
            track.project_id != old.project_id
            or track.scope != old.scope
            or track.storyboard_id != old.storyboard_id
            or track.episode_id != old.episode_id
            or track.created_at != old.created_at
            or track.revision != old.revision + 1
            or len(track.versions) != len(old.versions) + 1
            or track.versions[: len(old.versions)] != old.versions
        ):
            raise AppError("SUBTITLE_IMMUTABLE", "字幕版本只增不改，旧版本必须保留。")
