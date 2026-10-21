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

from vagent.contracts import Contract, Fingerprint, Identifier, JsonTuple, UtcTimestamp
from vagent.errors import AppError
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

FILM_INPUT_LIMIT = 64
FILM_VERSION_LIMIT = 64
ASSEMBLY_VERSION = "ffmpeg-concat-copy-v1"
COMPATIBILITY_VERSION = "mp4-copy-signature-v1"
FILM_MEDIA_CONTRACT_VERSION = 2
MEDIA_VALIDATION_VERSION = "mp4-avc-v1"


class Rational(Contract):
    """A non-negative exact time in seconds; canonical JSON stays exact."""

    numerator: int = Field(ge=0)
    denominator: int = Field(gt=0)

    @classmethod
    def of(cls, value: Fraction) -> "Rational":
        return cls(numerator=value.numerator, denominator=value.denominator)

    def fraction(self) -> Fraction:
        return Fraction(self.numerator, self.denominator)


def rational(value: Fraction) -> dict:
    return {"numerator": value.numerator, "denominator": value.denominator}


def canonical_sha256(value: JsonValue) -> str:
    encoded = json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


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
    assembly_version: Literal["ffmpeg-concat-copy-v1"] = ASSEMBLY_VERSION
    compatibility_version: Literal["mp4-copy-signature-v1"] = COMPATIBILITY_VERSION
    engine: FilmEngine
    compatibility_sha256: Fingerprint
    timeline: JsonTuple[FilmSegment] = Field(min_length=1, max_length=FILM_INPUT_LIMIT)
    fingerprint: Fingerprint


class FilmVersion(Contract):
    version: int = Field(ge=1)
    storyboard_version: int = Field(ge=1)
    storyboard_revision: int = Field(ge=0)
    inputs: JsonTuple[FilmInput] = Field(min_length=1, max_length=FILM_INPUT_LIMIT)
    output_media_id: MediaId
    assembly: FilmAssembly
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


def parse_media(raw: dict) -> "MediaAsset | FilmMedia":
    """The media index is a union; film records are told apart by their frozen tag."""
    if raw.get("contractVersion") == FILM_MEDIA_CONTRACT_VERSION and raw.get("kind") == "film":
        return FilmMedia.model_validate(raw)
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
) -> str:
    """Output-affecting identity only: no time, path, revision or call ID."""
    return canonical_sha256(
        {
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
            "assemblyVersion": ASSEMBLY_VERSION,
            "compatibilityVersion": COMPATIBILITY_VERSION,
            "compatibilitySha256": compatibility_sha256,
            "engine": {
                "packageVersion": engine.package_version,
                "binarySha256": engine.binary_sha256,
            },
        }
    )


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
        if shot_version.media_id is None:
            not_ready.append(shot.id)
            continue
        media = _media_record(state, shot_version.media_id)
        job = state["jobs"].get(shot_version.job_id)
        if media is None or job is None:
            incompatible.append({"shotId": shot.id, "fields": ["mediaId", "jobId"]})
            continue
        if isinstance(media, FilmMedia) or media.project_id != project_id:
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
        old_raw = previous.get(key)
        if old_raw is None:
            if film.revision != 1:
                raise AppError("FILM_CONFLICT", "新成片必须先保存第 1 版。")
            continue
        old = Film.model_validate(old_raw)
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
