"""M2 storyboard domain: stable shots, append-only versions and image assets.

Strict contracts plus the Store-commit validators. Tool argument schemas and
registration live in video/tools.py; storage.py owns the storyboards and
assets collections and calls these validators on open and in every commit.
No Agent-graph, provider or filesystem code belongs here.
"""

from datetime import UTC, datetime
from typing import Literal
from uuid import uuid4

from pydantic import Field, JsonValue, model_validator

from vagent.contracts import Contract, Fingerprint, Identifier, JsonTuple, UtcTimestamp
from vagent.errors import AppError
from vagent.video.contracts import (
    SHOT_DURATION_MAX,
    SHOT_DURATION_MIN,
    ArtifactRef,
    CostEstimate,
    MediaId,
    VideoRequest,
    VideoRequestV3,
)

SHOT_LIMIT = 64
SHOT_DESCRIPTION_MAX = 2000
SOURCE_REF_LIMIT = 16
ASSET_FORMAT_VERSION = "asset-image-v1"
ASSET_EXTENSIONS = {"image/png": "png", "image/jpeg": "jpg", "image/webp": "webp"}

Framing = Literal["wide", "medium", "close", "extreme_close", "over_shoulder", "aerial"]
ShotMode = Literal["t2v", "i2v", "flf2v"]
REFERENCE_FRAMES = {"t2v": 0, "i2v": 1, "flf2v": 2}


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


class Shot(Contract):
    id: Identifier
    order: int = Field(ge=1, le=SHOT_LIMIT)
    description: str = Field(min_length=1, max_length=SHOT_DESCRIPTION_MAX)
    framing: Framing
    camera_movement: str | None = Field(default=None, min_length=1, max_length=200)
    duration_seconds: int = Field(ge=SHOT_DURATION_MIN, le=SHOT_DURATION_MAX)
    mode: ShotMode = "t2v"
    reference_asset_ids: JsonTuple[Identifier] = Field(default=(), max_length=2)
    # Clip extension is cut from M2; the link stays null for later stages.
    extend_source: None = None

    @model_validator(mode="after")
    def coherent_references(self):
        if len(self.reference_asset_ids) != REFERENCE_FRAMES[self.mode]:
            raise ValueError("Shot mode requires its exact reference frame count")
        if len(set(self.reference_asset_ids)) != len(self.reference_asset_ids):
            raise ValueError("Reference assets must be unique per shot")
        return self


class StoryboardVersion(Contract):
    version: int = Field(ge=1)
    shots: JsonTuple[Shot] = Field(min_length=1, max_length=SHOT_LIMIT)
    created_at: UtcTimestamp

    @model_validator(mode="after")
    def ordered_shots(self):
        if [shot.order for shot in self.shots] != list(range(1, len(self.shots) + 1)):
            raise ValueError("Shot order must be the continuous 1..N sequence")
        if len({shot.id for shot in self.shots}) != len(self.shots):
            raise ValueError("Shot IDs must be unique within a storyboard version")
        return self


class ShotVersion(Contract):
    id: Identifier
    shot_id: Identifier
    storyboard_version: int = Field(ge=1)
    job_id: Identifier
    request: JsonValue
    estimate: CostEstimate | None = None
    media_id: MediaId | None = None
    created_at: UtcTimestamp


class Storyboard(Contract):
    id: Identifier
    project_id: Identifier
    title: str = Field(min_length=1, max_length=120)
    source_refs: JsonTuple[ArtifactRef] = Field(default=(), max_length=SOURCE_REF_LIMIT)
    current_version: int = Field(ge=1)
    versions: JsonTuple[StoryboardVersion] = Field(min_length=1)
    shot_versions: JsonTuple[ShotVersion] = ()
    revision: int = Field(ge=0)
    created_at: UtcTimestamp

    @model_validator(mode="after")
    def append_only_history(self):
        if [version.version for version in self.versions] != list(range(1, len(self.versions) + 1)):
            raise ValueError("Storyboard versions must be the continuous 1..N sequence")
        if self.current_version != len(self.versions):
            raise ValueError("currentVersion must identify the latest saved version")
        if len(set(self.source_refs)) != len(self.source_refs):
            raise ValueError("Source version references must be unique")
        if len({item.id for item in self.shot_versions}) != len(self.shot_versions):
            raise ValueError("Shot version IDs must be unique")
        known = self.known_shot_ids
        if any(item.shot_id not in known for item in self.shot_versions):
            raise ValueError("Shot versions must cite a shot of this storyboard")
        return self

    @property
    def known_shot_ids(self) -> set[str]:
        return {shot.id for version in self.versions for shot in version.shots}


class ImageDimensions(Contract):
    width: int = Field(ge=1)
    height: int = Field(ge=1)


class Asset(Contract):
    id: MediaId
    project_id: Identifier
    kind: Literal["image"] = "image"
    relative_path: str = Field(max_length=150)
    mime_type: Literal["image/png", "image/jpeg", "image/webp"]
    size_bytes: int = Field(gt=0)
    sha256: Fingerprint
    dimensions: ImageDimensions
    created_at: UtcTimestamp
    source: Literal["user_import"] = "user_import"
    asset_format_version: Literal["asset-image-v1"] = ASSET_FORMAT_VERSION

    @model_validator(mode="after")
    def controlled_path(self):
        if self.relative_path != f"assets/{self.project_id}/{self.id}.{ASSET_EXTENSIONS[self.mime_type]}":
            raise ValueError("Asset paths are determined exclusively by server IDs")
        return self


def validate_source_refs(state: dict, project_id: str, refs) -> None:
    """Text sources stay scoped to the current project and a frozen version."""
    for reference in refs:
        source = state["artifacts"].get(reference.artifact_id)
        if source is None or source["projectId"] != project_id:
            raise AppError("SOURCE_NOT_FOUND", "当前项目中没有所引用的源产物。")
        if not any(item["version"] == reference.version for item in source["versions"]):
            raise AppError("SOURCE_VERSION_NOT_FOUND", "源产物版本不存在，未保存分镜。")


def _validate_shot_assets(state: dict, shot: Shot) -> None:
    for asset_id in shot.reference_asset_ids:
        asset = state["assets"].get(asset_id)
        if asset is None:
            raise AppError("ASSET_NOT_FOUND", f"参考素材 {asset_id} 不存在，未保存分镜。")
        if asset.get("kind") != "image":
            raise AppError("ASSET_NOT_IMAGE", "镜头参考图必须是素材库中的图像。")


def validate_storyboard_changes(previous: dict[str, dict], state: dict) -> None:
    """Every Store commit re-checks append-only history and reference scope."""
    storyboards = state["storyboards"]
    if previous.keys() - storyboards.keys():
        raise AppError("STORYBOARD_IMMUTABLE", "已保存的分镜不能删除。")
    for key, raw in storyboards.items():
        record = Storyboard.model_validate(raw)
        if record.id != key or record.project_id not in state["projects"]:
            raise AppError("STORYBOARD_CONFLICT", "分镜 ID 或所属项目不匹配。")
        validate_source_refs(state, record.project_id, record.source_refs)
        for shot in record.versions[-1].shots:
            _validate_shot_assets(state, shot)
        old_raw = previous.get(key)
        if old_raw is None:
            if record.current_version != 1 or record.revision != 0:
                raise AppError("STORYBOARD_CONFLICT", "新分镜必须先保存第 1 版。")
            continue
        old = Storyboard.model_validate(old_raw)
        if (
            record.project_id != old.project_id
            or record.created_at != old.created_at
            or record.revision < old.revision
            or len(record.versions) < len(old.versions)
            or record.versions[: len(old.versions)] != old.versions
            or len(record.shot_versions) < len(old.shot_versions)
            or record.shot_versions[: len(old.shot_versions)] != old.shot_versions
        ):
            raise AppError("STORYBOARD_IMMUTABLE", "分镜版本只增不改，旧版本与已交付记录必须保留。")


def validate_asset_changes(previous: dict[str, dict], state: dict) -> None:
    """Assets are import-only in M2: identities cannot be deleted or rewritten."""
    assets = state.get("assets", {})
    if previous.keys() - assets.keys() or any(assets[key] != raw for key, raw in previous.items()):
        raise AppError("ASSET_IMMUTABLE", "已导入素材的身份与完整性记录不可删除或修改。")
    seen = set()
    for asset_id, raw in assets.items():
        asset = Asset.model_validate(raw)
        if (
            asset.id != asset_id
            or asset.project_id not in state["projects"]
            or (asset.project_id, asset.sha256) in seen
        ):
            raise AppError("ASSET_CONFLICT", "素材 ID、所属项目或同项目哈希唯一性不满足。")
        seen.add((asset.project_id, asset.sha256))


def save_storyboard(args: dict, state: dict, project_id: str) -> dict:
    """Append one StoryboardVersion from validated tool arguments, keeping history."""
    storyboard_id = args.get("storyboardId")
    raw = state["storyboards"].get(storyboard_id) if storyboard_id else None
    if storyboard_id and (raw is None or raw["projectId"] != project_id):
        raise AppError("NOT_FOUND", "当前项目中没有这个分镜。")
    existing = Storyboard.model_validate(raw) if raw is not None else None
    if existing is not None and existing.current_version != args["expectedVersion"]:
        raise AppError("VERSION_CONFLICT", "分镜版本已变化，请重新读取后再修改。")
    if "sourceRefs" in args:
        refs = tuple(ArtifactRef.model_validate(reference) for reference in args["sourceRefs"])
    else:
        refs = existing.source_refs if existing is not None else ()
    if len(set(refs)) != len(refs):
        raise AppError("INVALID_ARGUMENTS", "来源引用不能重复。")
    validate_source_refs(state, project_id, refs)
    known = existing.known_shot_ids if existing is not None else set()
    shots = []
    seen: set[str] = set()
    for index, item in enumerate(args["shots"]):
        shot_id = item.get("id") or str(uuid4())
        if shot_id in seen:
            raise AppError("SHOT_DUPLICATE", "同一批镜头不能重复使用相同镜头 ID。")
        if item.get("id") and shot_id not in known:
            raise AppError("SHOT_NOT_FOUND", "该镜头 ID 不属于这个分镜的既有版本。")
        seen.add(shot_id)
        try:
            shot = Shot(
                id=shot_id,
                order=index + 1,
                description=item["description"],
                framing=item["framing"],
                cameraMovement=item.get("cameraMovement"),
                durationSeconds=item.get("durationSeconds", 5),
                mode=item.get("mode", "t2v"),
                referenceAssetIds=item.get("referenceAssetIds", []),
            )
        except ValueError:
            raise AppError("INVALID_ARGUMENTS", "镜头字段不符合要求，未保存分镜。") from None
        _validate_shot_assets(state, shot)
        shots.append(shot)
    timestamp = utc_now()
    if existing is None:
        record = Storyboard(
            id=storyboard_id or str(uuid4()),
            project_id=project_id,
            title=args["title"],
            source_refs=refs,
            current_version=1,
            versions=(StoryboardVersion(version=1, shots=tuple(shots), created_at=timestamp),),
            revision=0,
            created_at=timestamp,
        )
    else:
        version = StoryboardVersion(
            version=existing.current_version + 1, shots=tuple(shots), created_at=timestamp
        )
        record = existing.model_copy(
            update={
                "title": args["title"],
                "source_refs": refs,
                "current_version": version.version,
                "versions": (*existing.versions, version),
                "revision": existing.revision + 1,
            }
        )
    state["storyboards"][record.id] = record.model_dump(mode="json", by_alias=True)
    return {
        "storyboardId": record.id,
        "version": record.current_version,
        "shotIds": [shot.id for shot in shots],
    }


def read_storyboard(args: dict, state: dict, project_id: str) -> dict:
    """Current storyboard view with ordered shots and per-shot version summaries."""
    storyboard_id = args.get("storyboardId")
    if storyboard_id:
        raw = state["storyboards"].get(storyboard_id)
        if raw is None or raw["projectId"] != project_id:
            raise AppError("NOT_FOUND", "当前项目中没有这个分镜。")
    else:
        candidates = [item for item in state["storyboards"].values() if item["projectId"] == project_id]
        if not candidates:
            raise AppError("NOT_FOUND", "当前项目还没有结构化分镜，请先保存。")
        raw = max(candidates, key=lambda item: (item["createdAt"], item["id"]))
    record = Storyboard.model_validate(raw)
    versions_by_shot: dict[str, list[ShotVersion]] = {}
    for shot_version in record.shot_versions:
        versions_by_shot.setdefault(shot_version.shot_id, []).append(shot_version)
    shots = []
    for shot in sorted(record.versions[-1].shots, key=lambda item: item.order):
        summary = shot.model_dump(mode="json", by_alias=True)
        summary["versions"] = [
            {
                "shotVersionId": item.id,
                "storyboardVersion": item.storyboard_version,
                "jobId": item.job_id,
                "mediaId": item.media_id,
                "createdAt": item.created_at,
            }
            for item in versions_by_shot.get(shot.id, [])
        ]
        shots.append(summary)
    return {
        "storyboardId": record.id,
        "title": record.title,
        "revision": record.revision,
        "currentVersion": record.current_version,
        "versionCount": len(record.versions),
        "sourceRefs": [reference.model_dump(mode="json", by_alias=True) for reference in record.source_refs],
        "createdAt": record.created_at,
        "shots": shots,
    }


def version_record(record: Storyboard, version: int) -> StoryboardVersion:
    """The frozen snapshot of one saved storyboard version."""
    for candidate in record.versions:
        if candidate.version == version:
            return candidate
    raise AppError("NOT_FOUND", "这个分镜版本不存在。")


def shot_of(record: Storyboard, version: int, shot_id: str) -> Shot:
    """Locate one shot by its stable server-generated ID inside a saved version."""
    for shot in version_record(record, version).shots:
        if shot.id == shot_id:
            return shot
    raise AppError("SHOT_NOT_FOUND", "该镜头 ID 不属于这个分镜版本。")


def stored_request_fingerprint(stored) -> str | None:
    """Recompute the exact fingerprint a stored request would hash to today."""
    if not isinstance(stored, dict):
        return None
    for model in (VideoRequestV3, VideoRequest):
        try:
            return model.model_validate(stored).fingerprint()
        except ValueError:
            continue
    return None


def latest_shot_version(record: Storyboard, storyboard_version: int, shot_id: str) -> ShotVersion | None:
    """The most recent registration for one (version, shot) key, if any."""
    return next(
        (
            item
            for item in reversed(record.shot_versions)
            if item.storyboard_version == storyboard_version and item.shot_id == shot_id
        ),
        None,
    )


def with_shot_versions(record: Storyboard, entries) -> Storyboard:
    """Append registrations without touching saved versions or earlier deliveries."""
    entries = tuple(entries)
    if not entries:
        return record
    return record.model_copy(
        update={"shot_versions": (*record.shot_versions, *entries), "revision": record.revision + 1}
    )


def storyboard_in_state(state: dict, project_id: str, storyboard_id) -> Storyboard:
    """Load one storyboard limited to the current project."""
    if not storyboard_id:
        raise AppError("INVALID_ARGUMENTS", "缺少 storyboardId，未创建任务。")
    raw = state["storyboards"].get(storyboard_id)
    if raw is None or raw["projectId"] != project_id:
        raise AppError("NOT_FOUND", "当前项目中没有这个分镜。")
    return Storyboard.model_validate(raw)
