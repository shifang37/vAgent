"""M5 episode domain: episodes, scenes and the character/style bibles.

E0 freezes the objects that schema v6 keeps. This module holds only strict
contracts and the Store-commit validators; storage.py owns the four new
collections and calls these validators on open and in every commit. Text
splitting, tool arguments, CLI/Web views and provider calls arrive in E1-E3 and
must not live here.

A scene owns at most one storyboard (1:1). Shots, reference assets, generation
Jobs and film assembly therefore keep using the frozen M2/M3/M4 machinery keyed
by ``storyboardId``; an episode only orders scenes and freezes their versions.
"""

from decimal import Decimal
from typing import Literal

from pydantic import Field, model_validator

from vagent.contracts import Contract, Identifier, JsonTuple, Name, UtcTimestamp
from vagent.errors import AppError
from vagent.video.contracts import (
    SHOT_DURATION_MAX,
    SHOT_DURATION_MIN,
    WAN_PRICE_VERSION,
    ArtifactRef,
    Currency,
    Money,
)
from vagent.video.storyboards import validate_source_refs

# --- Frozen M5 E0 constants -------------------------------------------------
# The text-adaptation contract id is stored in every EpisodeVersion, so a later
# algorithm must publish a new id instead of silently changing saved history.
TEXT_SPLIT_CONTRACT = "text-split-zh-v1"
# M5 adapts one document into one episode; the acceptance corpus is 1000-2000
# characters and 20000 is the hard ceiling for the deterministic splitter.
SOURCE_TEXT_MAX_CHARACTERS = 20000
EPISODE_MIN_SECONDS = 60
EPISODE_MAX_SECONDS = 90
EPISODE_MIN_SHOTS = 10
EPISODE_MAX_SHOTS = 20
# A scene is a beat, not a film: a handful of shots and one storyboard.
SCENE_MAX_SHOTS = 6
SCENE_LIMIT = 12
SCENE_MAX_SECONDS = SHOT_DURATION_MAX * SCENE_MAX_SHOTS
# Episode-level batch ceiling. The frozen per-Run ceiling (LIVE_JOBS_PER_RUN=4)
# keeps constraining 6/6 and older tool surfaces; M5 publishes both numbers.
EPISODE_JOB_CAP = 20
EPISODE_COST_CAP = "60.00"
SHOT_ESTIMATE_AMOUNT = "3.00"
SHOT_ESTIMATE_CURRENCY: Currency = "CNY"
CHARACTER_ALIAS_LIMIT = 8
CHARACTER_RELATION_LIMIT = 16
CHARACTER_REFERENCE_LIMIT = 4
BIBLE_KINDS = ("character", "style")

TimeOfDay = Literal["day", "night", "dawn", "dusk", "unknown"]
BibleKind = Literal["character", "style"]


def _require_continuous_versions(versions, label: str) -> None:
    if [item.version for item in versions] != list(range(1, len(versions) + 1)):
        raise ValueError(f"{label} versions must be the continuous 1..N sequence")


class ShotBudget(Contract):
    """The frozen per-episode plan: how many shots, how long, and what it may cost."""

    shot_count: int = Field(ge=EPISODE_MIN_SHOTS, le=EPISODE_MAX_SHOTS)
    target_duration_seconds: int = Field(ge=EPISODE_MIN_SECONDS, le=EPISODE_MAX_SECONDS)
    scene_max_shots: int = Field(default=SCENE_MAX_SHOTS, ge=1, le=SCENE_MAX_SHOTS)
    job_cap: int = Field(default=EPISODE_JOB_CAP, ge=1, le=EPISODE_JOB_CAP)
    per_shot_amount: Money = SHOT_ESTIMATE_AMOUNT
    currency: Currency = SHOT_ESTIMATE_CURRENCY
    episode_cap_amount: Money = EPISODE_COST_CAP
    price_version: Name = WAN_PRICE_VERSION

    @model_validator(mode="after")
    def coherent_ceiling(self):
        if self.job_cap < self.shot_count:
            raise ValueError("The episode job cap cannot be smaller than its shot count")
        if Decimal(self.per_shot_amount) * self.shot_count > Decimal(self.episode_cap_amount):
            raise ValueError("Per-shot estimates exceed the episode cost cap")
        return self


class SceneVersion(Contract):
    """One planned beat: heading, cast, shot count and its share of the duration."""

    version: int = Field(ge=1)
    heading: str = Field(min_length=1, max_length=120)
    location: str | None = Field(default=None, min_length=1, max_length=120)
    time_of_day: TimeOfDay = "unknown"
    synopsis: str = Field(min_length=1, max_length=1000)
    character_ids: JsonTuple[Identifier] = Field(default=(), max_length=CHARACTER_RELATION_LIMIT)
    shot_count: int = Field(ge=1, le=SCENE_MAX_SHOTS)
    duration_seconds: int = Field(ge=SHOT_DURATION_MIN, le=SCENE_MAX_SECONDS)
    created_at: UtcTimestamp

    @model_validator(mode="after")
    def duration_fits_shots(self):
        low = SHOT_DURATION_MIN * self.shot_count
        high = SHOT_DURATION_MAX * self.shot_count
        if not low <= self.duration_seconds <= high:
            raise ValueError("A scene duration must fit its own shot count")
        if len(set(self.character_ids)) != len(self.character_ids):
            raise ValueError("A scene cannot cite the same character twice")
        return self


class Scene(Contract):
    """Stable scene identity plus append-only versions, like a storyboard."""

    id: Identifier
    project_id: Identifier
    episode_id: Identifier
    # Bound at most once: the storyboard that renders this scene (1:1).
    storyboard_id: Identifier | None = None
    current_version: int = Field(ge=1)
    versions: JsonTuple[SceneVersion] = Field(min_length=1)
    revision: int = Field(ge=0)
    created_at: UtcTimestamp

    @model_validator(mode="after")
    def append_only_history(self):
        _require_continuous_versions(self.versions, "Scene")
        if self.current_version != len(self.versions):
            raise ValueError("currentVersion must identify the latest scene version")
        return self

    @property
    def latest(self) -> SceneVersion:
        return self.versions[-1]


class VoiceProfile(Contract):
    """Descriptive only: no TTS provider, voice id or clone consent belongs here."""

    description: str = Field(min_length=1, max_length=200)
    timbre: str | None = Field(default=None, min_length=1, max_length=60)
    pace: Literal["slow", "normal", "fast"] = "normal"


class CharacterLink(Contract):
    character_id: Identifier
    relation: str = Field(min_length=1, max_length=60)


class CharacterVersion(Contract):
    version: int = Field(ge=1)
    name: str = Field(min_length=1, max_length=60)
    aliases: JsonTuple[str] = Field(default=(), max_length=CHARACTER_ALIAS_LIMIT)
    appearance: str = Field(min_length=1, max_length=600)
    wardrobe: str | None = Field(default=None, min_length=1, max_length=300)
    voice: VoiceProfile | None = None
    relationships: JsonTuple[CharacterLink] = Field(default=(), max_length=CHARACTER_RELATION_LIMIT)
    reference_asset_ids: JsonTuple[Identifier] = Field(default=(), max_length=CHARACTER_REFERENCE_LIMIT)
    created_at: UtcTimestamp

    @model_validator(mode="after")
    def coherent_bible_entry(self):
        if len(set(self.aliases)) != len(self.aliases):
            raise ValueError("Character aliases must be unique")
        if len(set(self.reference_asset_ids)) != len(self.reference_asset_ids):
            raise ValueError("Character reference assets must be unique")
        if len({link.character_id for link in self.relationships}) != len(self.relationships):
            raise ValueError("A character cannot repeat the same relationship")
        return self


class Character(Contract):
    id: Identifier
    project_id: Identifier
    current_version: int = Field(ge=1)
    versions: JsonTuple[CharacterVersion] = Field(min_length=1)
    revision: int = Field(ge=0)
    created_at: UtcTimestamp

    @model_validator(mode="after")
    def append_only_history(self):
        _require_continuous_versions(self.versions, "Character")
        if self.current_version != len(self.versions):
            raise ValueError("currentVersion must identify the latest character version")
        return self

    @property
    def latest(self) -> CharacterVersion:
        return self.versions[-1]


class StyleVersion(Contract):
    """Look and language only. Subtitles, tracks, transitions and aspect stay out."""

    version: int = Field(ge=1)
    name: str = Field(min_length=1, max_length=60)
    genre: JsonTuple[str] = Field(default=(), max_length=8)
    palette: str = Field(min_length=1, max_length=200)
    camera_language: str = Field(min_length=1, max_length=300)
    created_at: UtcTimestamp

    @model_validator(mode="after")
    def unique_genres(self):
        if len(set(self.genre)) != len(self.genre):
            raise ValueError("Style genres must be unique")
        return self


class Style(Contract):
    id: Identifier
    project_id: Identifier
    current_version: int = Field(ge=1)
    versions: JsonTuple[StyleVersion] = Field(min_length=1)
    revision: int = Field(ge=0)
    created_at: UtcTimestamp

    @model_validator(mode="after")
    def append_only_history(self):
        _require_continuous_versions(self.versions, "Style")
        if self.current_version != len(self.versions):
            raise ValueError("currentVersion must identify the latest style version")
        return self

    @property
    def latest(self) -> StyleVersion:
        return self.versions[-1]


class SceneRef(Contract):
    """One frozen (scene, version) pair inside an episode version."""

    scene_id: Identifier
    scene_version: int = Field(ge=1)


class EpisodeVersion(Contract):
    version: int = Field(ge=1)
    target_duration_seconds: int = Field(ge=EPISODE_MIN_SECONDS, le=EPISODE_MAX_SECONDS)
    shot_budget: ShotBudget
    scenes: JsonTuple[SceneRef] = Field(min_length=1, max_length=SCENE_LIMIT)
    sources: JsonTuple[ArtifactRef] = Field(default=(), max_length=16)
    text_split_contract: Literal["text-split-zh-v1"] = TEXT_SPLIT_CONTRACT
    created_at: UtcTimestamp

    @model_validator(mode="after")
    def one_shot_budget(self):
        if len({item.scene_id for item in self.scenes}) != len(self.scenes):
            raise ValueError("An episode version cannot cite the same scene twice")
        if self.shot_budget.target_duration_seconds != self.target_duration_seconds:
            raise ValueError("The shot budget must target this episode duration")
        return self


class Episode(Contract):
    id: Identifier
    project_id: Identifier
    title: str = Field(min_length=1, max_length=120)
    style_id: Identifier | None = None
    current_version: int = Field(ge=1)
    versions: JsonTuple[EpisodeVersion] = Field(min_length=1)
    revision: int = Field(ge=0)
    created_at: UtcTimestamp

    @model_validator(mode="after")
    def append_only_history(self):
        _require_continuous_versions(self.versions, "Episode")
        if self.current_version != len(self.versions):
            raise ValueError("currentVersion must identify the latest episode version")
        return self

    @property
    def latest(self) -> EpisodeVersion:
        return self.versions[-1]


def _scene_record(state: dict, scene_id: str) -> Scene:
    raw = state["scenes"].get(scene_id)
    if raw is None:
        raise AppError("SCENE_NOT_FOUND", f"场记 {scene_id} 不存在，未保存。")
    return Scene.model_validate(raw)


def _style_record(state: dict, style_id: str) -> Style:
    raw = state["styles"].get(style_id)
    if raw is None:
        raise AppError("STYLE_NOT_FOUND", f"风格圣经 {style_id} 不存在，未保存。")
    return Style.model_validate(raw)


def _character_record(state: dict, character_id: str) -> Character:
    raw = state["characters"].get(character_id)
    if raw is None:
        raise AppError("CHARACTER_NOT_FOUND", f"角色 {character_id} 不存在，未保存。")
    return Character.model_validate(raw)


def _validate_episode_budget(state: dict, episode: Episode, version: EpisodeVersion) -> None:
    """Every frozen scene version must agree with the episode's own numbers."""
    shots = 0
    seconds = 0
    for reference in version.scenes:
        scene = _scene_record(state, reference.scene_id)
        if scene.project_id != episode.project_id:
            raise AppError("SCENE_CONFLICT", "场记必须属于同一项目。")
        if scene.episode_id != episode.id:
            raise AppError("SCENE_CONFLICT", "场记不属于这个分集。")
        frozen = next((item for item in scene.versions if item.version == reference.scene_version), None)
        if frozen is None:
            raise AppError("SCENE_CONFLICT", "引用的场记版本不存在。")
        if frozen.shot_count > version.shot_budget.scene_max_shots:
            raise AppError("EPISODE_SHOT_BUDGET", "单个场记的镜头数超过冻结上限。")
        shots += frozen.shot_count
        seconds += frozen.duration_seconds
        for character_id in frozen.character_ids:
            character = _character_record(state, character_id)
            if character.project_id != episode.project_id:
                raise AppError("CHARACTER_CONFLICT", "角色必须属于同一项目。")
    if shots != version.shot_budget.shot_count:
        raise AppError("EPISODE_SHOT_BUDGET", "分集镜头数与逐场镜头之和不一致。")
    if seconds != version.target_duration_seconds:
        raise AppError("EPISODE_DURATION_LIMIT", "分集时长与逐场时长之和不一致。")
    if episode.style_id is not None:
        style = _style_record(state, episode.style_id)
        if style.project_id != episode.project_id:
            raise AppError("STYLE_CONFLICT", "风格圣经必须属于同一项目。")


def validate_episode_changes(previous: dict[str, dict], state: dict) -> None:
    """Every Store commit re-checks append-only episodes and their frozen budgets."""
    episodes = state["episodes"]
    if previous.keys() - episodes.keys():
        raise AppError("EPISODE_IMMUTABLE", "已保存的分集不能删除。")
    for key, raw in episodes.items():
        record = Episode.model_validate(raw)
        if record.id != key or record.project_id not in state["projects"]:
            raise AppError("EPISODE_CONFLICT", "分集 ID 或所属项目不匹配。")
        validate_source_refs(state, record.project_id, record.latest.sources)
        _validate_episode_budget(state, record, record.latest)
        old_raw = previous.get(key)
        if old_raw is None:
            if record.current_version != 1 or record.revision != 0:
                raise AppError("EPISODE_CONFLICT", "新分集必须先保存第 1 版。")
            continue
        old = Episode.model_validate(old_raw)
        if (
            record.project_id != old.project_id
            or record.created_at != old.created_at
            or record.revision < old.revision
            or len(record.versions) < len(old.versions)
            or record.versions[: len(old.versions)] != old.versions
            or (old.style_id is not None and record.style_id != old.style_id)
        ):
            raise AppError("EPISODE_IMMUTABLE", "分集版本只增不改，旧版本与已冻结的预算必须保留。")


def validate_scene_changes(previous: dict[str, dict], state: dict) -> None:
    """Scenes keep their identity and bind at most one storyboard, once."""
    scenes = state["scenes"]
    if previous.keys() - scenes.keys():
        raise AppError("SCENE_IMMUTABLE", "已保存的场记不能删除。")
    bound: set[str] = set()
    for key, raw in scenes.items():
        record = Scene.model_validate(raw)
        if record.id != key or record.project_id not in state["projects"]:
            raise AppError("SCENE_CONFLICT", "场记 ID 或所属项目不匹配。")
        episode_raw = state["episodes"].get(record.episode_id)
        if episode_raw is None or episode_raw["projectId"] != record.project_id:
            raise AppError("SCENE_CONFLICT", "场记必须归属于本项目的分集。")
        for character_id in record.latest.character_ids:
            character = _character_record(state, character_id)
            if character.project_id != record.project_id:
                raise AppError("CHARACTER_CONFLICT", "角色必须属于同一项目。")
        if record.storyboard_id is not None:
            board = state["storyboards"].get(record.storyboard_id)
            if board is None or board["projectId"] != record.project_id:
                raise AppError("SCENE_CONFLICT", "场记只能绑定本项目的分镜。")
            if record.storyboard_id in bound:
                raise AppError("SCENE_CONFLICT", "同一分镜只能绑定一个场记。")
            bound.add(record.storyboard_id)
        old_raw = previous.get(key)
        if old_raw is None:
            if record.current_version != 1 or record.revision != 0:
                raise AppError("SCENE_CONFLICT", "新场记必须先保存第 1 版。")
            continue
        old = Scene.model_validate(old_raw)
        if (
            record.project_id != old.project_id
            or record.episode_id != old.episode_id
            or record.created_at != old.created_at
            or record.revision < old.revision
            or len(record.versions) < len(old.versions)
            or record.versions[: len(old.versions)] != old.versions
            or (old.storyboard_id is not None and record.storyboard_id != old.storyboard_id)
        ):
            raise AppError("SCENE_IMMUTABLE", "场记版本只增不改，已绑定的分镜不可替换。")


def validate_character_changes(previous: dict[str, dict], state: dict) -> None:
    """Characters are append-only bibles; reference images must already exist."""
    characters = state["characters"]
    if previous.keys() - characters.keys():
        raise AppError("CHARACTER_IMMUTABLE", "已保存的角色不能删除。")
    for key, raw in characters.items():
        record = Character.model_validate(raw)
        if record.id != key or record.project_id not in state["projects"]:
            raise AppError("CHARACTER_CONFLICT", "角色 ID 或所属项目不匹配。")
        latest = record.latest
        for asset_id in latest.reference_asset_ids:
            asset = state["assets"].get(asset_id)
            if asset is None or asset.get("projectId") != record.project_id:
                raise AppError("CHARACTER_ASSET_MISSING", f"参考素材 {asset_id} 不在本项目的素材库中。")
            if asset.get("kind") != "image":
                raise AppError("CHARACTER_ASSET_MISSING", "角色参考素材必须是素材库中的图像。")
        for link in latest.relationships:
            related = _character_record(state, link.character_id)
            if related.id == record.id:
                raise AppError("CHARACTER_CONFLICT", "角色不能与自己建立关系。")
            if related.project_id != record.project_id:
                raise AppError("CHARACTER_CONFLICT", "关系双方必须属于同一项目。")
        old_raw = previous.get(key)
        if old_raw is None:
            if record.current_version != 1 or record.revision != 0:
                raise AppError("CHARACTER_CONFLICT", "新角色必须先保存第 1 版。")
            continue
        old = Character.model_validate(old_raw)
        if (
            record.project_id != old.project_id
            or record.created_at != old.created_at
            or record.revision < old.revision
            or len(record.versions) < len(old.versions)
            or record.versions[: len(old.versions)] != old.versions
        ):
            raise AppError("CHARACTER_IMMUTABLE", "角色圣经只增不改，旧版本必须保留。")


def validate_style_changes(previous: dict[str, dict], state: dict) -> None:
    """Styles are append-only bibles and never carry M6 subtitle or track fields."""
    styles = state["styles"]
    if previous.keys() - styles.keys():
        raise AppError("STYLE_IMMUTABLE", "已保存的风格不能删除。")
    for key, raw in styles.items():
        record = Style.model_validate(raw)
        if record.id != key or record.project_id not in state["projects"]:
            raise AppError("STYLE_CONFLICT", "风格 ID 或所属项目不匹配。")
        old_raw = previous.get(key)
        if old_raw is None:
            if record.current_version != 1 or record.revision != 0:
                raise AppError("STYLE_CONFLICT", "新风格必须先保存第 1 版。")
            continue
        old = Style.model_validate(old_raw)
        if (
            record.project_id != old.project_id
            or record.created_at != old.created_at
            or record.revision < old.revision
            or len(record.versions) < len(old.versions)
            or record.versions[: len(old.versions)] != old.versions
        ):
            raise AppError("STYLE_IMMUTABLE", "风格圣经只增不改，旧版本必须保留。")
