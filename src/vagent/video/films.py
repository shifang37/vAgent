"""M3 film domain: append-only assembled films and the film-media union branch.

Strict contracts plus the Store-commit validators. Assembly itself lives in
vagent.video.assembler; tool arguments and registration in video/tools.py;
storage.py owns the ``films`` collection and calls these validators on open and
in every commit. No Agent-graph, provider, subprocess or FFmpeg code belongs here.
"""

import hashlib
import json
from fractions import Fraction
from typing import Literal

from pydantic import Field, JsonValue, model_validator

from vagent.contracts import (
    Contract,
    Fingerprint,
    Identifier,
    JsonTuple,
    PositiveSeconds,
    UtcTimestamp,
)
from vagent.errors import AppError
from vagent.video.audio import AudioMedia, is_audio_media
from vagent.video.contracts import (
    MediaAsset,
    MediaId,
    MediaMetadata,
)
from vagent.video.storyboards import (
    ShotVersion,
    Storyboard,
    latest_shot_version,
    storyboard_in_state,
)
from vagent.video.subtitle_font import FontRef

FILM_INPUT_LIMIT = 64
FILM_VERSION_LIMIT = 64
ASSEMBLY_VERSION = "ffmpeg-concat-copy-v1"
NORMALIZATION_VERSION = "ffmpeg-concat-normalize-v4"
NORMALIZATION_PROFILE = "h264-1280x720-30fps-aac-48k-stereo-v1"
COMPATIBILITY_VERSION = "mp4-copy-signature-v1"
FILM_MEDIA_CONTRACT_VERSION = 2
MEDIA_VALIDATION_VERSION = "mp4-avc-v1"

RENDER_VERSION = "ffmpeg-render-v1"
PROFILE_720P = "720p-h264-aac-v1"
PROFILE_1080P = "1080p-h264-aac-v1"
EXPORT_PROFILES = (PROFILE_720P, PROFILE_1080P)
SUBTITLE_MODES = ("soft", "burn", "both")
RENDER_TIMEOUT_SECONDS = 120.0
RENDER_JOB_TIMEOUT_SECONDS = 600.0
RENDER_MAX_SECONDS = 90
TITLE_CARD_LIMIT = 4
TRANSITION_SECONDS_MIN = 0.1
TRANSITION_SECONDS_MAX = 1.0
MAX_RENDER_AUDIO_TRACKS = 64
MICROSECONDS = 1_000_000


class Rational(Contract):
    """A non-negative exact time in seconds; canonical JSON stays exact."""

    numerator: int = Field(ge=0)
    denominator: int = Field(gt=0)

    @classmethod
    def of(cls, value: Fraction) -> "Rational":
        return cls(numerator=value.numerator, denominator=value.denominator)

    @classmethod
    def microseconds(cls, value: int) -> "Rational":
        """One exact microsecond count; the subtitle time base."""
        return cls(numerator=int(value), denominator=MICROSECONDS)

    def fraction(self) -> Fraction:
        return Fraction(self.numerator, self.denominator)

    def total_microseconds(self) -> int:
        """Round to the nearest microsecond (half up); the value is never negative."""
        scaled = self.numerator * MICROSECONDS
        return (scaled * 2 + self.denominator) // (2 * self.denominator)


def rational(value: Fraction) -> dict:
    return {"numerator": value.numerator, "denominator": value.denominator}


def canonical_sha256(value: JsonValue) -> str:
    encoded = json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


# --- M6 render records (E1 carries the subtitle-only subset) -----------------


FailureStage = Literal["source", "subtitle", "audio", "mix", "encode", "validate"]


def profile_for(mode: str, *, name: str = PROFILE_720P) -> dict:
    """The frozen 720P/1080P parameter sets; 1080P is only an upscale profile."""
    if name == PROFILE_720P:
        width, height, upscaled = 1280, 720, False
    elif name == PROFILE_1080P:
        width, height, upscaled = 1920, 1080, True
    else:
        raise ValueError("Unknown export profile")
    if mode not in SUBTITLE_MODES:
        raise ValueError("Unknown subtitle mode")
    return {
        "name": name,
        "width": width,
        "height": height,
        "fps": 30,
        "videoCodec": "h264",
        "crf": 20,
        "videoBitrate": None,
        "pixFmt": "yuv420p",
        "audioCodec": "aac",
        "sampleRate": 48000,
        "channels": 2,
        "audioBitrate": "192k",
        "subtitleMode": mode,
        "upscaled": upscaled,
    }


class ExportProfile(Contract):
    name: Literal["720p-h264-aac-v1", "1080p-h264-aac-v1"]
    width: int = Field(gt=0)
    height: int = Field(gt=0)
    fps: int = Field(gt=0)
    video_codec: Literal["h264"] = "h264"
    crf: int | None = Field(default=20, ge=0, le=51)
    video_bitrate: str | None = Field(default=None, min_length=1, max_length=20)
    pix_fmt: Literal["yuv420p"] = "yuv420p"
    audio_codec: Literal["aac"] = "aac"
    sample_rate: int = Field(gt=0)
    channels: int = Field(gt=0)
    audio_bitrate: str = Field(min_length=1, max_length=20)
    subtitle_mode: Literal["soft", "burn", "both"]
    upscaled: bool

    @model_validator(mode="after")
    def matches_the_frozen_geometry(self):
        expected = {
            PROFILE_720P: (1280, 720, False),
            PROFILE_1080P: (1920, 1080, True),
        }[self.name]
        if (self.width, self.height, self.upscaled) != expected:
            raise ValueError("An export profile must use its frozen geometry")
        return self


class RenderEngine(Contract):
    version: str = Field(min_length=1, max_length=120)
    configuration_sha256: Fingerprint


class RenderSubtitle(Contract):
    track_id: Identifier
    track_version: int = Field(ge=1)
    track_sha256: Fingerprint
    mode: Literal["soft", "burn", "both"]
    font_ref: FontRef
    sidecar_sha256: Fingerprint | None = None


class RenderAudioTrack(Contract):
    audio_id: Identifier
    audio_version: int = Field(ge=1)
    kind: Literal["voice", "bgm", "sfx"]
    gain_db: float = Field(ge=-60, le=12, allow_inf_nan=False)
    start: Rational
    ducking: bool = False
    voice_binding: dict[str, object] | None = None


class RenderDucking(Contract):
    threshold_db: float = Field(default=-20, allow_inf_nan=False)
    ratio: float = Field(default=8, ge=1, allow_inf_nan=False)
    attack_ms: float = Field(default=20, ge=0, allow_inf_nan=False)
    release_ms: float = Field(default=400, ge=0, allow_inf_nan=False)


class RenderLoudness(Contract):
    target_lufs: float = Field(default=-16, allow_inf_nan=False)
    target_true_peak_db: float = Field(default=-1, allow_inf_nan=False)
    input: dict[str, object] | None = None
    output: dict[str, object] | None = None


class RenderAudio(Contract):
    tracks: JsonTuple[RenderAudioTrack] = Field(default=(), max_length=MAX_RENDER_AUDIO_TRACKS)
    ducking: RenderDucking = Field(default_factory=RenderDucking)
    loudness: RenderLoudness = Field(default_factory=RenderLoudness)


class RenderTransition(Contract):
    after_shot_id: Identifier
    type: Literal["fade", "crossfade"]
    duration: Rational

    @model_validator(mode="after")
    def bounded_duration(self):
        seconds = self.duration.fraction()
        if not TRANSITION_SECONDS_MIN <= seconds <= TRANSITION_SECONDS_MAX:
            raise ValueError("A transition must last between 0.1 and 1.0 seconds")
        return self


class RenderTitleCard(Contract):
    text: str = Field(min_length=1, max_length=120)
    start: Rational
    duration: Rational
    style_profile: str = Field(min_length=1, max_length=60)


class RenderWatermark(Contract):
    required: Literal[True] = True
    source: Literal["provider"] = "provider"


class RenderVideoFacts(Contract):
    codec: Literal["h264"]
    width: int = Field(gt=0)
    height: int = Field(gt=0)
    # A soft export copies the assembled film's cadence; E3's full render encodes the
    # profile's nominal rate. The value is the observed measurement either way.
    fps: float = Field(gt=0, allow_inf_nan=False)


class RenderAudioFacts(Contract):
    codec: str = Field(min_length=1, max_length=20)
    sample_rate: int = Field(gt=0)
    channels: int = Field(gt=0)


class RenderMeasurements(Contract):
    duration_seconds: PositiveSeconds
    size_bytes: int = Field(gt=0)
    sha256: Fingerprint
    video: RenderVideoFacts
    audio: RenderAudioFacts | None = None


class RenderSubtitleSource(Contract):
    track_id: Identifier
    track_version: int = Field(ge=1)
    track_sha256: Fingerprint


class RenderSources(Contract):
    episode_id: Identifier | None = None
    storyboard_version: int = Field(ge=1)
    shot_version_ids: JsonTuple[Identifier] = Field(min_length=1, max_length=64)
    audio_assets: JsonTuple[Identifier] = Field(default=(), max_length=MAX_RENDER_AUDIO_TRACKS)
    subtitle_track: RenderSubtitleSource
    font: FontRef

    @model_validator(mode="after")
    def unique_shots(self):
        if len(set(self.shot_version_ids)) != len(self.shot_version_ids):
            raise ValueError("Rendered shot versions must be distinct")
        return self


class FilmRender(Contract):
    """A rendered film: the frozen export profile plus everything that shaped it."""

    render_version: Literal["ffmpeg-render-v1"] = RENDER_VERSION
    export_profile: ExportProfile
    engine: RenderEngine
    subtitle: RenderSubtitle
    audio: RenderAudio = Field(default_factory=RenderAudio)
    transitions: JsonTuple[RenderTransition] = Field(default=(), max_length=63)
    title_cards: JsonTuple[RenderTitleCard] = Field(default=(), max_length=TITLE_CARD_LIMIT)
    watermark: RenderWatermark = Field(default_factory=RenderWatermark)
    measurements: RenderMeasurements
    sources: RenderSources
    failure_stage: FailureStage | None = None
    fingerprint: Fingerprint

    @model_validator(mode="after")
    def coherent_render(self):
        if self.subtitle.mode != self.export_profile.subtitle_mode:
            raise ValueError("The subtitle mode must match the export profile")
        if (self.measurements.video.width, self.measurements.video.height) != (
            self.export_profile.width,
            self.export_profile.height,
        ):
            raise ValueError("Rendered dimensions must match the export profile")
        if self.subtitle.mode in {"burn", "both"} and (
            abs(self.measurements.video.fps - self.export_profile.fps) > 0.01
        ):
            raise ValueError("A re-encoded render must use the profile frame rate")
        if self.subtitle.sidecar_sha256 is None and self.subtitle.mode in {"soft", "both"}:
            raise ValueError("soft and both exports carry a sidecar subtitle digest")
        if self.failure_stage is not None:
            raise ValueError("Only successful renders are committed as a film version")
        return self


class FilmInput(Contract):
    shot_id: Identifier
    shot_version_id: Identifier
    job_id: Identifier
    media_id: MediaId
    sha256: Fingerprint
    size_bytes: int = Field(gt=0)


class FilmEngine(Contract):
    package_version: str = Field(min_length=1, max_length=40)
    ffmpeg_version: str = Field(min_length=1, max_length=120)
    binary_sha256: Fingerprint


class FilmNormalization(Contract):
    """The canonical transcode profile used when copy compatibility is impossible."""

    version: Literal[
        "ffmpeg-concat-normalize-v1",
        "ffmpeg-concat-normalize-v2",
        "ffmpeg-concat-normalize-v3",
        "ffmpeg-concat-normalize-v4",
    ] = NORMALIZATION_VERSION
    profile: str = Field(min_length=1, max_length=120)
    width: int = Field(gt=0)
    height: int = Field(gt=0)
    frame_rate: int = Field(gt=0)
    video_codec: Literal["h264"] = "h264"
    audio_codec: Literal["aac"] = "aac"
    audio_sample_rate: int = Field(gt=0)
    audio_channels: int = Field(gt=0)


class FilmSegment(Contract):
    """One input's rational start offset and duration on the output timeline."""

    shot_id: Identifier
    shot_version_id: Identifier
    start: Rational
    duration: Rational
    signature_sha256: Fingerprint
    # First sample presentation time and the audio priming skipped by the edit list.
    priming: Rational


class FilmAssembly(Contract):
    assembly_version: Literal[
        "ffmpeg-concat-copy-v1",
        "ffmpeg-concat-normalize-v1",
        "ffmpeg-concat-normalize-v2",
        "ffmpeg-concat-normalize-v3",
        "ffmpeg-concat-normalize-v4",
    ] = ASSEMBLY_VERSION
    compatibility_version: Literal["mp4-copy-signature-v1"] = COMPATIBILITY_VERSION
    engine: FilmEngine
    compatibility_sha256: Fingerprint
    timeline: JsonTuple[FilmSegment] = Field(min_length=1, max_length=FILM_INPUT_LIMIT)
    fingerprint: Fingerprint
    normalization: FilmNormalization | None = None

    @model_validator(mode="after")
    def coherent_version(self):
        normalized = self.assembly_version != ASSEMBLY_VERSION
        if (self.normalization is not None) != normalized:
            raise ValueError("Normalization metadata must match the assembly version")
        if self.normalization is not None and self.normalization.version != self.assembly_version:
            raise ValueError("Normalization version must match the assembly version")
        return self


class FilmVersion(Contract):
    version: int = Field(ge=1)
    storyboard_version: int = Field(ge=1)
    storyboard_revision: int = Field(ge=0)
    inputs: JsonTuple[FilmInput] = Field(min_length=1, max_length=FILM_INPUT_LIMIT)
    output_media_id: MediaId
    assembly: FilmAssembly
    # E1 renders (soft/burn subtitles) append to the same film history; an
    # assembled copy film keeps this null and a rendered film never omits it.
    render: FilmRender | None = None
    size_bytes: int = Field(gt=0)
    sha256: Fingerprint
    metadata: MediaMetadata
    created_at: UtcTimestamp

    @model_validator(mode="after")
    def ordered_timeline(self):
        if len(self.assembly.timeline) != len(self.inputs):
            raise ValueError("Every input needs exactly one timeline segment")
        if [segment.shot_id for segment in self.assembly.timeline] != [item.shot_id for item in self.inputs]:
            raise ValueError("Timeline segments must follow the input order")
        if len({item.shot_version_id for item in self.inputs}) != len(self.inputs):
            raise ValueError("Film inputs must reference distinct shot versions")
        return self


class Film(Contract):
    id: Identifier
    project_id: Identifier
    storyboard_id: Identifier
    current_version: int = Field(ge=1)
    versions: JsonTuple[FilmVersion] = Field(min_length=1, max_length=FILM_VERSION_LIMIT)
    revision: int = Field(ge=0)
    created_at: UtcTimestamp

    @model_validator(mode="after")
    def append_only_history(self):
        if [version.version for version in self.versions] != list(range(1, len(self.versions) + 1)):
            raise ValueError("Film versions must be the continuous 1..N sequence")
        if self.current_version != len(self.versions):
            raise ValueError("currentVersion must identify the latest film version")
        return self


class FilmMedia(Contract):
    """A local film file. It never fakes a generation Job or reuses MediaAsset."""

    contract_version: Literal[2] = FILM_MEDIA_CONTRACT_VERSION
    kind: Literal["film"] = "film"
    id: MediaId
    project_id: Identifier
    film_id: Identifier
    film_version: int = Field(ge=1)
    relative_path: str = Field(max_length=150)
    mime_type: Literal["video/mp4"] = "video/mp4"
    size_bytes: int = Field(gt=0)
    sha256: Fingerprint
    metadata: MediaMetadata
    validation_version: Literal["mp4-avc-v1"] = MEDIA_VALIDATION_VERSION
    created_at: UtcTimestamp

    @model_validator(mode="after")
    def controlled_path(self):
        if self.relative_path != f"media/{self.project_id}/{self.id}.mp4":
            raise ValueError("Film media paths are determined exclusively by server IDs")
        return self


def parse_media(raw: dict) -> "MediaAsset | FilmMedia | AudioMedia":
    """The media index is a union; records are told apart by their frozen tag."""
    if raw.get("contractVersion") == FILM_MEDIA_CONTRACT_VERSION and raw.get("kind") == "film":
        return FilmMedia.model_validate(raw)
    if is_audio_media(raw):
        return AudioMedia.model_validate(raw)
    return MediaAsset.model_validate(raw)


def film_media_path(project_id: str, media_id: str) -> str:
    return f"media/{project_id}/{media_id}.mp4"


def _media_record(state: dict, media_id: str) -> "MediaAsset | FilmMedia | None":
    raw = state["media"].get(media_id)
    return parse_media(raw) if raw is not None else None


def assembly_fingerprint(
    *,
    project_id: str,
    storyboard_id: str,
    storyboard_version: int,
    inputs: tuple[FilmInput, ...],
    compatibility_sha256: str,
    engine: FilmEngine,
    assembly_version: str = ASSEMBLY_VERSION,
    normalization: dict | None = None,
) -> str:
    """Output-affecting identity only: no time, path, revision or call ID.

    The copy payload is kept byte-identical for pre-normalization versions, so an
    existing film still replays instead of being re-assembled under a new engine.
    """
    payload = {
        "projectId": project_id,
        "storyboardId": storyboard_id,
        "storyboardVersion": storyboard_version,
        "inputs": [
            {
                "shotId": item.shot_id,
                "shotVersionId": item.shot_version_id,
                "jobId": item.job_id,
                "mediaId": item.media_id,
                "sha256": item.sha256,
                "sizeBytes": item.size_bytes,
            }
            for item in inputs
        ],
        "assemblyVersion": assembly_version,
        "compatibilityVersion": COMPATIBILITY_VERSION,
        "compatibilitySha256": compatibility_sha256,
        "engine": {
            "packageVersion": engine.package_version,
            "binarySha256": engine.binary_sha256,
        },
    }
    if normalization is not None:
        payload["normalization"] = normalization
    return canonical_sha256(payload)


def select_inputs(state: dict, project_id: str, storyboard_id: str, expected_revision: int) -> dict:
    """Freeze the current storyboard version's latest delivered media per shot.

    The latest ShotVersion is authoritative: a newer registration that has not been
    delivered refuses the whole assembly instead of silently reusing an older clip.
    """
    record = storyboard_in_state(state, project_id, storyboard_id)
    if record.revision != expected_revision:
        raise AppError("VERSION_CONFLICT", "分镜记录已变化，请重新读取后再拼接。")
    version = record.current_version
    inputs, missing, not_ready, incompatible = [], [], [], []
    for shot in sorted(record.versions[-1].shots, key=lambda item: item.order):
        shot_version = latest_shot_version(record, version, shot.id)
        if shot_version is None:
            missing.append(shot.id)
            continue
        job = state["jobs"].get(shot_version.job_id)
        # M2 records delivery on the Job; the ShotVersion media link stays a
        # historical M1 field. Read the Job's committed media when it is null.
        media_id = shot_version.media_id
        if media_id is None and isinstance(job, dict):
            refs = (job.get("result") or {}).get("mediaRefs") or []
            media_id = refs[0].get("mediaId") if refs else None
        if media_id is None:
            not_ready.append(shot.id)
            continue
        media = _media_record(state, media_id)
        if media is None or job is None:
            incompatible.append({"shotId": shot.id, "fields": ["mediaId", "jobId"]})
            continue
        if isinstance(media, (FilmMedia, AudioMedia)) or media.project_id != project_id:
            incompatible.append({"shotId": shot.id, "fields": ["mediaId"]})
            continue
        if job.get("status") != "succeeded" or job.get("context", {}).get("projectId") != project_id:
            not_ready.append(shot.id)
            continue
        if (
            job.get("result", {}).get("mediaRefs", [{}])[0].get("mediaId") != media.id
            or media.job_id != shot_version.job_id
            or media.metadata.video_codec != "h264"
        ):
            incompatible.append({"shotId": shot.id, "fields": ["mediaId", "jobId"]})
            continue
        inputs.append(
            FilmInput(
                shot_id=shot.id,
                shot_version_id=shot_version.id,
                job_id=shot_version.job_id,
                media_id=media.id,
                sha256=media.sha256,
                size_bytes=media.size_bytes,
            )
        )
    if missing:
        raise AppError("FILM_INPUT_MISSING", f"以下镜头尚无任何生成记录：{', '.join(missing)}。")
    if not_ready:
        raise AppError("FILM_INPUT_NOT_READY", f"以下镜头的最新版本尚未交付：{', '.join(not_ready)}。")
    if incompatible:
        raise AppError("FILM_INPUT_INCOMPATIBLE", "以下镜头的媒体引用不一致，无法拼接。")
    if not inputs:
        raise AppError("FILM_INPUT_MISSING", "分镜没有可拼接的镜头。")
    return {
        "storyboard": record,
        "storyboardVersion": version,
        "storyboardRevision": record.revision,
        "title": record.title,
        "shots": [shot for shot in record.versions[-1].shots],
        "inputs": tuple(inputs),
        "shotVersions": {shot.shot_id: shot for shot in inputs},
    }


def latest_shot_version_for(record: Storyboard, storyboard_version: int, shot_id: str) -> ShotVersion | None:
    return latest_shot_version(record, storyboard_version, shot_id)


def find_film(state: dict, project_id: str, storyboard_id: str) -> Film | None:
    for raw in state["films"].values():
        if raw["projectId"] == project_id and raw["storyboardId"] == storyboard_id:
            return Film.model_validate(raw)
    return None


def film_media(state: dict, media_id: str) -> FilmMedia | None:
    raw = state["media"].get(media_id)
    if raw is None or raw.get("kind") != "film":
        return None
    return FilmMedia.model_validate(raw)


def verify_film_media(state: dict, media: FilmMedia) -> None:
    """A film media record must belong to the FilmVersion that produced it."""
    raw = state["films"].get(media.film_id)
    film = Film.model_validate(raw) if raw is not None else None
    version = None
    if film is not None:
        version = next((item for item in film.versions if item.version == media.film_version), None)
    if (
        film is None
        or version is None
        or film.project_id != media.project_id
        or version.output_media_id != media.id
        or (version.size_bytes, version.sha256, version.metadata)
        != (media.size_bytes, media.sha256, media.metadata)
    ):
        raise AppError("FILM_MEDIA_INVALID", "成片媒体与对应的成片版本不一致。")


def render_view(render: FilmRender | None) -> dict | None:
    """Bounded projection of a rendered version: no path, only controlled URLs."""
    if render is None:
        return None
    subtitle = render.subtitle
    sidecar = (
        f"/api/subtitles/{subtitle.track_id}/{subtitle.track_version}/srt"
        if subtitle.mode in {"soft", "both"}
        else None
    )
    return {
        "renderVersion": render.render_version,
        "exportProfile": render.export_profile.model_dump(mode="json", by_alias=True),
        "engine": render.engine.model_dump(mode="json", by_alias=True),
        "subtitle": {
            **subtitle.model_dump(mode="json", by_alias=True),
            "sidecarUrl": sidecar,
        },
        "audio": render.audio.model_dump(mode="json", by_alias=True),
        "transitions": [item.model_dump(mode="json", by_alias=True) for item in render.transitions],
        "titleCards": [item.model_dump(mode="json", by_alias=True) for item in render.title_cards],
        "watermark": render.watermark.model_dump(mode="json", by_alias=True),
        "measurements": render.measurements.model_dump(mode="json", by_alias=True),
        "sources": render.sources.model_dump(mode="json", by_alias=True),
        "failureStage": None,
        "fingerprint": render.fingerprint,
    }


def verify_film_render(state: dict, film: Film, version: FilmVersion) -> None:
    """A rendered version only appends to one assembled source version."""
    render = version.render
    if render is None:
        return
    if [item.shot_version_id for item in version.inputs] != list(render.sources.shot_version_ids):
        raise AppError("FILM_MEDIA_INVALID", "渲染记录的镜头来源与成片版本不一致。")
    raw = state.get("subtitleTracks", {}).get(render.subtitle.track_id)
    track_version = None
    if raw is not None:
        track_version = next(
            (item for item in raw["versions"] if item["version"] == render.subtitle.track_version), None
        )
    if track_version is None:
        raise AppError("SUBTITLE_TRACK_MISSING", "渲染记录引用的字幕版本不存在。")
    from vagent.video.subtitles import SubtitleTrackVersion, track_version_sha256

    frozen = SubtitleTrackVersion.model_validate(track_version)
    if track_version_sha256(frozen) != render.subtitle.track_sha256:
        raise AppError("FILM_MEDIA_INVALID", "渲染引用的字幕版本与登记的指纹不一致。")
    if (render.measurements.size_bytes, render.measurements.sha256) != (version.size_bytes, version.sha256):
        raise AppError("FILM_MEDIA_INVALID", "渲染测量值与成片版本不一致。")
    earlier = [item for item in film.versions if item.version < version.version]
    source = next((item for item in reversed(earlier) if item.render is None), None)
    if source is None or (
        source.inputs != version.inputs
        or source.assembly != version.assembly
        or source.storyboard_version != version.storyboard_version
        or source.storyboard_revision != version.storyboard_revision
    ):
        raise AppError("FILM_IMMUTABLE", "渲染版本必须追加在一个未渲染的拼接版本之后，且时间线不变。")


def validate_film_changes(previous: dict[str, dict], state: dict) -> None:
    """Every Store commit re-checks append-only film history and media identity."""
    films = state.get("films", {})
    if previous.keys() - films.keys():
        raise AppError("FILM_IMMUTABLE", "已保存的成片不能删除。")
    owned: set[tuple[str, str]] = set()
    for key, raw in films.items():
        film = Film.model_validate(raw)
        if film.id != key or film.project_id not in state["projects"]:
            raise AppError("FILM_CONFLICT", "成片 ID 或所属项目不匹配。")
        if state["storyboards"].get(film.storyboard_id, {}).get("projectId") != film.project_id:
            raise AppError("FILM_CONFLICT", "成片必须引用本项目分镜。")
        if (film.project_id, film.storyboard_id) in owned:
            raise AppError("FILM_CONFLICT", "同一项目的同一分镜至多一个成片。")
        owned.add((film.project_id, film.storyboard_id))
        for version in film.versions:
            media = film_media(state, version.output_media_id)
            if media is None:
                raise AppError("FILM_MEDIA_INVALID", "成片版本缺少对应的本地媒体记录。")
            verify_film_media(state, media)
            verify_film_render(state, film, version)
        old_raw = previous.get(key)
        if old_raw is None:
            if film.revision != 1:
                raise AppError("FILM_CONFLICT", "新成片必须先保存第 1 版。")
            continue
        old = Film.model_validate(old_raw)
        if film == old:
            continue
        if (
            film.project_id != old.project_id
            or film.storyboard_id != old.storyboard_id
            or film.created_at != old.created_at
            or film.revision != old.revision + 1
            or len(film.versions) != len(old.versions) + 1
            or film.versions[: len(old.versions)] != old.versions
        ):
            raise AppError("FILM_IMMUTABLE", "成片版本只增不改，旧版本与文件必须保留。")
    for raw in state["media"].values():
        if raw.get("kind") == "film":
            verify_film_media(state, FilmMedia.model_validate(raw))


def cleanup_stale_parts(home, state: dict) -> int:
    """Remove crash leftovers from assembly; committed files and the index stay intact.

    A ``.part`` file is never indexed, so it cannot represent a delivered film. Files
    still owned by a Job download (``download.mediaId``) belong to the M1-C download
    window and are left untouched, because that window may resume from prepared bytes.
    An unreferenced *final* file left by a failed commit is deliberately kept: the store
    must not infer success from an orphan, and a later assembly simply uses a new ID.
    """
    media = home / "media"
    if not media.is_dir():
        return 0
    owned = {
        raw["download"]["mediaId"]
        for raw in state.get("jobs", {}).values()
        if isinstance(raw.get("download"), dict) and raw["download"].get("mediaId")
    }
    removed = 0
    for path in sorted(media.glob("*/*.part")):
        if path.name.endswith(".mp4.part"):
            if path.name[: -len(".mp4.part")] in owned:
                continue
        elif not path.name.endswith((".wav.part", ".m4a.part")):
            continue
        try:
            info = path.lstat()
            if info.st_size >= 0 and not path.is_symlink() and path.is_file():
                path.unlink()
                removed += 1
        except OSError:
            continue
    return removed


def film_view(state: dict, film: Film) -> dict:
    """Bounded public projection: IDs, versions and controlled media URLs only."""
    versions = []
    for version in film.versions:
        media = film_media(state, version.output_media_id)
        versions.append(
            {
                "version": version.version,
                "storyboardVersion": version.storyboard_version,
                "storyboardRevision": version.storyboard_revision,
                "outputMediaId": version.output_media_id,
                "sizeBytes": version.size_bytes,
                "sha256": version.sha256,
                "durationSeconds": version.metadata.duration_seconds,
                "width": version.metadata.width,
                "height": version.metadata.height,
                "hasAudio": version.metadata.has_audio,
                "createdAt": version.created_at,
                "mediaAvailable": media is not None,
                "mediaUrl": f"/api/media/{version.output_media_id}/content" if media is not None else None,
                "inputs": [
                    {
                        "shotId": item.shot_id,
                        "shotVersionId": item.shot_version_id,
                        "mediaId": item.media_id,
                        "sha256": item.sha256,
                        "sizeBytes": item.size_bytes,
                    }
                    for item in version.inputs
                ],
                "timeline": [
                    {
                        "shotId": segment.shot_id,
                        "startSeconds": _seconds(segment.start),
                        "durationSeconds": _seconds(segment.duration),
                    }
                    for segment in version.assembly.timeline
                ],
                "engine": version.assembly.engine.model_dump(mode="json", by_alias=True),
                "assemblyVersion": version.assembly.assembly_version,
                "normalization": (
                    version.assembly.normalization.model_dump(mode="json", by_alias=True)
                    if version.assembly.normalization is not None
                    else None
                ),
                "render": render_view(version.render),
                "fingerprint": version.assembly.fingerprint,
            }
        )
    latest = versions[-1]
    return {
        "filmId": film.id,
        "projectId": film.project_id,
        "storyboardId": film.storyboard_id,
        "revision": film.revision,
        "currentVersion": film.current_version,
        "versionCount": len(versions),
        "createdAt": film.created_at,
        "latest": latest,
        "versions": versions,
    }


def _seconds(value: Rational) -> float:
    fraction = value.fraction()
    return round(fraction.numerator / fraction.denominator, 6)
