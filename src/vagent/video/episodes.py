"""M5 episode domain: episodes, scenes and the character/style bibles.

E0 froze the objects that schema v6 keeps; E1 adds the deterministic writing path
(``save_episode``/``read_episode``/``save_bible``/``read_bible``) on top of them,
following the storyboard module's layout: strict contracts, Store-commit
validators and journaled handlers in one place. The text-to-draft algorithm lives
in ``adaptation.py``; tool schemas and registration live in video/tools.py.

A scene owns at most one storyboard (1:1). Shots, reference assets, generation
Jobs and film assembly therefore keep using the frozen M2/M3/M4 machinery keyed
by ``storyboardId``; an episode only orders scenes and freezes their versions.
"""

from datetime import UTC, datetime
from decimal import Decimal
from typing import Literal
from uuid import uuid4

from pydantic import Field, model_validator

from vagent.contracts import Contract, Identifier, JsonTuple, Name, UtcTimestamp
from vagent.errors import AppError
from vagent.video.consistency import (
    CONSISTENCY_REVIEW_LIMIT,
    ConsistencyReview,
    character_evidence,
    episode_consistency,
    style_prompt,
)
from vagent.video.contracts import (
    EPISODE_JOB_CAP,
    SHOT_DURATION_MAX,
    SHOT_DURATION_MIN,
    WAN_PRICE_VERSION,
    ArtifactRef,
    Currency,
    Money,
    parse_job,
)
from vagent.video.storyboards import Storyboard, latest_shot_version, validate_source_refs
from vagent.video.views import delivered_media

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
# keeps constraining 6/6 and older tool surfaces; E3 registers one episode's whole
# batch under the shared ``EPISODE_JOB_CAP`` (imported above) and re-checks it on
# every commit instead of the per-Run 4.
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


class SceneDialogue(Contract):
    """One line of dialogue kept with its scene so later stages never re-guess it.

    E1 adds this field additively: records written by E0 (or without dialogue)
    still validate, and the speaker stays the transcript name until E2 links it
    to a character ID.
    """

    text: str = Field(min_length=1, max_length=300)
    speaker: str | None = Field(default=None, min_length=1, max_length=40)
    paragraph: int = Field(default=0, ge=0, le=100)


class SceneVersion(Contract):
    """One planned beat: heading, cast, shot count and its share of the duration."""

    version: int = Field(ge=1)
    heading: str = Field(min_length=1, max_length=120)
    location: str | None = Field(default=None, min_length=1, max_length=120)
    time_of_day: TimeOfDay = "unknown"
    synopsis: str = Field(min_length=1, max_length=1000)
    character_ids: JsonTuple[Identifier] = Field(default=(), max_length=CHARACTER_RELATION_LIMIT)
    dialogue: JsonTuple[SceneDialogue] = Field(default=(), max_length=40)
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
    # E2 appends human consistency reviews to the record root, exactly like M2
    # appended ShotVersions to a storyboard: no new episode version is written.
    consistency_reviews: JsonTuple[ConsistencyReview] = Field(default=(), max_length=CONSISTENCY_REVIEW_LIMIT)
    revision: int = Field(ge=0)
    created_at: UtcTimestamp

    @model_validator(mode="after")
    def append_only_history(self):
        _require_continuous_versions(self.versions, "Episode")
        if self.current_version != len(self.versions):
            raise ValueError("currentVersion must identify the latest episode version")
        if len({item.id for item in self.consistency_reviews}) != len(self.consistency_reviews):
            raise ValueError("Consistency review IDs must be unique")
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
        for review in record.consistency_reviews:
            character = _character_record(state, review.character_id)
            if character.project_id != record.project_id:
                raise AppError("CHARACTER_CONFLICT", "评分记录的角色必须属于同一项目。")
            if review.episode_version > record.current_version:
                raise AppError("EPISODE_CONFLICT", "评分记录引用了尚未保存的分集版本。")
            if review.character_version > character.current_version:
                raise AppError("CHARACTER_CONFLICT", "评分记录引用了尚未保存的角色版本。")
        old_raw = previous.get(key)
        if old_raw is None:
            if record.current_version != 1 or record.revision != 0:
                raise AppError("EPISODE_CONFLICT", "新分集必须先保存第 1 版。")
            if record.consistency_reviews:
                raise AppError("EPISODE_CONFLICT", "新分集不能预置一致性评分。")
            continue
        old = Episode.model_validate(old_raw)
        if (
            record.project_id != old.project_id
            or record.created_at != old.created_at
            or record.revision < old.revision
            or len(record.versions) < len(old.versions)
            or record.versions[: len(old.versions)] != old.versions
            or (old.style_id is not None and record.style_id != old.style_id)
            or len(record.consistency_reviews) < len(old.consistency_reviews)
            or record.consistency_reviews[: len(old.consistency_reviews)] != old.consistency_reviews
        ):
            raise AppError("EPISODE_IMMUTABLE", "分集版本只增不改，旧版本、预算与评分必须保留。")


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


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


def _episode_record(state: dict, episode_id: str) -> Episode:
    raw = state["episodes"].get(episode_id)
    if raw is None:
        raise AppError("EPISODE_NOT_FOUND", f"分集 {episode_id} 不存在，未保存。")
    return Episode.model_validate(raw)


# --- E3: one episode's shots and its frozen ceiling -------------------------


def _amount(value) -> Decimal:
    return Decimal(str(value))


def _money(value) -> str:
    return format(Decimal(value), ".2f")


def episode_scene_rows(state: dict, episode: Episode) -> tuple[list[dict], list[dict]]:
    """The episode's frozen scenes with the one storyboard that renders each.

    Returns ``(rows, problems)`` instead of raising: the read view has to stay usable
    before every scene is bound, while registration demands a complete plan. Each row
    freezes ``scene``/``frozen``/``board`` and the board's current ordered ``shots``.
    Problems use the frozen M5 error codes so the caller can surface the first one.
    """
    rows, problems = [], []
    for reference in episode.latest.scenes:
        raw = state["scenes"].get(reference.scene_id)
        if raw is None:
            problems.append(
                {
                    "code": "SCENE_NOT_FOUND",
                    "sceneId": reference.scene_id,
                    "message": "分集引用的场记已不存在。",
                }
            )
            continue
        scene = Scene.model_validate(raw)
        frozen = next((item for item in scene.versions if item.version == reference.scene_version), None)
        if frozen is None:
            problems.append(
                {
                    "code": "SCENE_CONFLICT",
                    "sceneId": scene.id,
                    "message": "分集引用的场记版本已不存在。",
                }
            )
            continue
        board = None
        board_raw = state["storyboards"].get(scene.storyboard_id or "")
        if board_raw is not None and board_raw.get("projectId") == episode.project_id:
            board = Storyboard.model_validate(board_raw)
        if board is None:
            problems.append(
                {
                    "code": "SCENE_CONFLICT",
                    "sceneId": scene.id,
                    "message": f"场记「{frozen.heading}」还没有绑定分镜。",
                }
            )
            continue
        shots = sorted(board.versions[-1].shots, key=lambda item: item.order)
        if len(shots) != frozen.shot_count:
            problems.append(
                {
                    "code": "EPISODE_SHOT_BUDGET",
                    "sceneId": scene.id,
                    "message": (
                        f"场记「{frozen.heading}」已绑定的分镜有 {len(shots)} 个镜头，"
                        f"冻结计划是 {frozen.shot_count} 个。"
                    ),
                }
            )
        elif sum(item.duration_seconds for item in shots) != frozen.duration_seconds:
            problems.append(
                {
                    "code": "EPISODE_DURATION_LIMIT",
                    "sceneId": scene.id,
                    "message": (
                        f"场记「{frozen.heading}」已绑定的分镜合计 "
                        f"{sum(item.duration_seconds for item in shots)} 秒，"
                        f"冻结计划是 {frozen.duration_seconds} 秒。"
                    ),
                }
            )
        rows.append({"scene": scene, "frozen": frozen, "board": board, "shots": shots})
    return rows, problems


def episode_shot_plan(state: dict, episode: Episode) -> list[dict]:
    """Every registered shot of the episode, ordered by scene then shot.

    Registration is the strict caller: an unbound scene, a storyboard whose shot
    count/duration disagrees with the frozen scene, or a total that disagrees with the
    frozen ``ShotBudget`` rejects the whole batch before anything is written.
    """
    rows, problems = episode_scene_rows(state, episode)
    if problems:
        detail = "；".join(problem["message"] for problem in problems)
        raise AppError(problems[0]["code"], f"{detail} 未登记任何任务。")
    targets = [
        {"scene": row["scene"], "frozen": row["frozen"], "board": row["board"], "shot": shot}
        for row in rows
        for shot in row["shots"]
    ]
    total = len(targets)
    if total != episode.latest.shot_budget.shot_count:
        raise AppError(
            "EPISODE_SHOT_BUDGET",
            f"分集共 {total} 个已绑定镜头，冻结预算是 {episode.latest.shot_budget.shot_count} 个；"
            "未登记任何任务。",
        )
    durations = sum(target["shot"].duration_seconds for target in targets)
    if durations != episode.latest.target_duration_seconds:
        raise AppError(
            "EPISODE_DURATION_LIMIT",
            f"分集镜头合计 {durations} 秒，冻结目标是 {episode.latest.target_duration_seconds} 秒；"
            "未登记任何任务。",
        )
    return targets


def episode_boards(state: dict, episode: Episode) -> set[str]:
    """The storyboard IDs bound to this episode's scenes (tolerant of gaps)."""
    boards = set()
    for reference in episode.latest.scenes:
        raw = state["scenes"].get(reference.scene_id)
        if raw is not None and raw.get("storyboardId"):
            boards.add(raw["storyboardId"])
    return boards


def episode_for_storyboard(state: dict, storyboard_id: str) -> Episode | None:
    """The episode whose scene bound this storyboard, if any."""
    for raw in state["scenes"].values():
        if raw.get("storyboardId") != storyboard_id:
            continue
        episode_raw = state["episodes"].get(raw["episodeId"])
        if episode_raw is None:
            return None
        return Episode.model_validate(episode_raw)
    return None


def episode_budget_usage(state: dict, episode: Episode) -> dict:
    """The episode's frozen ceiling plus what its already-registered Jobs occupy.

    Attribution is provable from the Store: a live Job belongs to the episode when its
    ``shotRef`` names a storyboard bound to one of the episode's scenes. Runs, redoes
    and unknown submissions all stay attributed, so a failure never releases budget.
    Amounts only count the episode's frozen currency; a foreign-currency Job is
    reported separately and never converted.
    """
    budget = episode.latest.shot_budget
    boards = episode_boards(state, episode)
    cap_jobs = min(int(budget.job_cap), EPISODE_JOB_CAP)
    cap_amount = _amount(budget.episode_cap_amount)
    used_jobs, used_amount, foreign = 0, Decimal(0), []
    for raw in state["jobs"].values():
        if raw.get("mode") != "live" or raw["context"]["projectId"] != episode.project_id:
            continue
        shot_ref = raw.get("shotRef")
        if not shot_ref or shot_ref.get("storyboardId") not in boards:
            continue
        used_jobs += 1
        estimate = (raw.get("cost") or {}).get("estimate") or {}
        if estimate.get("currency") == budget.currency and estimate.get("amount") is not None:
            used_amount += _amount(estimate["amount"])
        else:
            foreign.append({"jobId": raw["id"], "currency": estimate.get("currency")})
    return {
        "jobCap": cap_jobs,
        "usedJobs": used_jobs,
        "remainingJobs": max(0, cap_jobs - used_jobs),
        "currency": budget.currency,
        "perShotAmount": budget.per_shot_amount,
        "episodeCapAmount": _money(cap_amount),
        "usedAmount": _money(used_amount),
        "remainingAmount": _money(max(Decimal(0), cap_amount - used_amount)),
        "priceVersion": budget.price_version,
        "shotCount": budget.shot_count,
        "targetDurationSeconds": budget.target_duration_seconds,
        "foreignCurrencyJobs": foreign,
    }


def _shot_status(job, delivered: bool) -> str:
    """One shot's delivery state from its latest Job; never inferred from a request."""
    if job is None:
        return "unregistered"
    if delivered:
        return "delivered"
    if (
        job.status in {"failed", "unknown", "download_failed"}
        or job.query_state == "paused"
        or getattr(job, "runtime_block", None) is not None
    ):
        return "failed"
    return "pending"


def episode_generation(state: dict, episode: Episode) -> dict:
    """Per-shot registration/delivery evidence and the episode's remaining ceiling.

    This is the read-side projection shared by ``episode_read`` and the acceptance
    tests. It never raises on an incomplete episode: unbound scenes and mismatched
    storyboards become ``problems`` so the caller can fix the plan first.
    """
    rows, problems = episode_scene_rows(state, episode)
    jobs = {job_id: parse_job(raw) for job_id, raw in state["jobs"].items()}
    scenes, shots = [], []
    counts = {
        "planned": episode.latest.shot_budget.shot_count,
        "registered": 0,
        "delivered": 0,
        "failed": 0,
        "pending": 0,
        "unregistered": 0,
    }
    for row in rows:
        board = row["board"]
        entries = []
        for shot in row["shots"]:
            previous = latest_shot_version(board, board.current_version, shot.id)
            entry = {
                "shotId": shot.id,
                "sceneId": row["scene"].id,
                "storyboardId": board.id,
                "storyboardVersion": board.current_version,
                "order": shot.order,
                "mode": shot.mode,
                "durationSeconds": shot.duration_seconds,
                "shotVersionId": None,
                "jobId": None,
                "status": "unregistered",
                "mediaId": None,
                "mediaAvailable": False,
                "estimate": None,
                "error": None,
            }
            if previous is not None:
                job = jobs.get(previous.job_id)
                media_id, available = delivered_media(job)
                entry.update(
                    {
                        "shotVersionId": previous.id,
                        "jobId": previous.job_id,
                        "status": _shot_status(job, available),
                        "mediaId": media_id,
                        "mediaAvailable": available,
                        "estimate": previous.estimate.model_dump(mode="json", by_alias=True)
                        if previous.estimate
                        else None,
                        "error": job.error.public() if job is not None and job.error else None,
                    }
                )
                counts["registered"] += 1
            counts[entry["status"]] += 1
            entries.append(entry)
            shots.append(entry)
        scenes.append(
            {
                "sceneId": row["scene"].id,
                "heading": row["frozen"].heading,
                "storyboardId": board.id,
                "storyboardVersion": board.current_version,
                "shotCount": len(row["shots"]),
                "durationSeconds": row["frozen"].duration_seconds,
                "shotIds": [shot.id for shot in row["shots"]],
            }
        )
    return {
        "ready": not problems,
        "problems": problems,
        "budget": episode_budget_usage(state, episode),
        "scenes": scenes,
        "shots": shots,
        "counts": counts,
    }


def _candidate_scene(item: dict) -> dict:
    """The frozen fields of one requested scene, as it would be stored."""
    dialogue = []
    for line in item.get("dialogue") or ():
        dialogue.append((line["text"], line.get("speaker"), int(line.get("paragraph") or 0)))
    return {
        "heading": item["heading"],
        "location": item.get("location"),
        "time_of_day": item.get("timeOfDay", "unknown"),
        "synopsis": item["synopsis"],
        "character_ids": tuple(item.get("characterIds") or ()),
        "dialogue": tuple(dialogue),
        "shot_count": int(item["shotCount"]),
        "duration_seconds": int(item["durationSeconds"]),
    }


def _stored_scene(version: SceneVersion) -> dict:
    return {
        "heading": version.heading,
        "location": version.location,
        "time_of_day": version.time_of_day,
        "synopsis": version.synopsis,
        "character_ids": tuple(version.character_ids),
        "dialogue": tuple((line.text, line.speaker, line.paragraph) for line in version.dialogue),
        "shot_count": version.shot_count,
        "duration_seconds": version.duration_seconds,
    }


def _scene_version(version: int, fields: dict, timestamp: str) -> SceneVersion:
    if not 1 <= fields["shot_count"] <= SCENE_MAX_SHOTS:
        raise AppError("EPISODE_SHOT_BUDGET", f"单个场记的镜头数必须在 1–{SCENE_MAX_SHOTS} 之间。")
    low = SHOT_DURATION_MIN * fields["shot_count"]
    high = SHOT_DURATION_MAX * fields["shot_count"]
    if not low <= fields["duration_seconds"] <= high:
        raise AppError("EPISODE_DURATION_LIMIT", "场记时长必须容得下它自己的镜头数。")
    try:
        return SceneVersion(
            version=version,
            heading=fields["heading"],
            location=fields["location"],
            time_of_day=fields["time_of_day"],
            synopsis=fields["synopsis"],
            character_ids=fields["character_ids"],
            dialogue=tuple(
                SceneDialogue(text=text, speaker=speaker, paragraph=paragraph)
                for text, speaker, paragraph in fields["dialogue"]
            ),
            shot_count=fields["shot_count"],
            duration_seconds=fields["duration_seconds"],
            created_at=timestamp,
        )
    except ValueError:
        raise AppError("INVALID_ARGUMENTS", "场记字段不符合要求（镜头数或时长越界），未保存分集。") from None


def _episode_target(args: dict, shots: int) -> int:
    requested = args.get("targetDurationSeconds")
    if requested is None:
        # Five seconds per shot keeps the plan inside 60..90 for 12..18 shots.
        return max(EPISODE_MIN_SECONDS, min(EPISODE_MAX_SECONDS, shots * 5))
    if not isinstance(requested, int) or not EPISODE_MIN_SECONDS <= requested <= EPISODE_MAX_SECONDS:
        raise AppError("EPISODE_DURATION_LIMIT", "分集时长必须在冻结的 60–90 秒之间。")
    return requested


def save_episode(args: dict, state: dict, project_id: str) -> dict:
    """Create or append one EpisodeVersion, creating or editing its scenes atomically."""
    episode_id = args.get("episodeId")
    raw = state["episodes"].get(episode_id) if episode_id else None
    if episode_id and (raw is None or raw["projectId"] != project_id):
        raise AppError("EPISODE_NOT_FOUND", "当前项目中没有这个分集。")
    existing = Episode.model_validate(raw) if raw is not None else None
    if existing is not None and existing.revision != args.get("expectedRevision", -1):
        raise AppError("VERSION_CONFLICT", "分集记录已变化，请重新读取后再修改。")
    if existing is None:
        episode_id = str(uuid4())
    style_id = args.get("styleId", existing.style_id if existing else None)
    if style_id is not None:
        style = _style_record(state, style_id)
        if style.project_id != project_id:
            raise AppError("STYLE_CONFLICT", "风格圣经必须属于同一项目。")

    timestamp = utc_now()
    scenes: list[Scene] = []
    for item in args["scenes"]:
        scene_id = item.get("id")
        if scene_id:
            if existing is None:
                raise AppError("SCENE_NOT_FOUND", "新建分集不能引用尚未保存的场记。")
            scene_raw = state["scenes"].get(scene_id)
            if scene_raw is None or scene_raw["projectId"] != project_id:
                raise AppError("SCENE_NOT_FOUND", "当前项目中没有这个场记。")
            scene = Scene.model_validate(scene_raw)
            if scene.episode_id != episode_id:
                raise AppError("SCENE_CONFLICT", "场记不属于这个分集。")
            fields = _candidate_scene(item)
            if _stored_scene(scene.latest) == fields:
                scenes.append(scene)
                continue
            version = _scene_version(scene.current_version + 1, fields, timestamp)
            scenes.append(
                scene.model_copy(
                    update={
                        "versions": (*scene.versions, version),
                        "current_version": version.version,
                        "revision": scene.revision + 1,
                    }
                )
            )
            continue
        fields = _candidate_scene(item)
        scenes.append(
            Scene(
                id=str(uuid4()),
                project_id=project_id,
                episode_id=episode_id,
                current_version=1,
                versions=(_scene_version(1, fields, timestamp),),
                revision=0,
                created_at=timestamp,
            )
        )

    shots = sum(scene.latest.shot_count for scene in scenes)
    if not EPISODE_MIN_SHOTS <= shots <= EPISODE_MAX_SHOTS:
        raise AppError(
            "EPISODE_SHOT_BUDGET",
            f"分集共 {shots} 个镜头，必须在 {EPISODE_MIN_SHOTS}–{EPISODE_MAX_SHOTS} 之间。",
        )
    target = _episode_target(args, shots)
    budget = ShotBudget(shot_count=shots, target_duration_seconds=target)
    if "sourceRefs" in args:
        sources = args["sourceRefs"]
    elif existing is not None:
        sources = [item.model_dump(mode="json", by_alias=True) for item in existing.latest.sources]
    else:
        sources = []
    refs = tuple(ArtifactRef.model_validate(item) for item in sources)
    validate_source_refs(state, project_id, refs)
    version = EpisodeVersion(
        version=(existing.current_version + 1) if existing is not None else 1,
        target_duration_seconds=target,
        shot_budget=budget,
        scenes=tuple(SceneRef(scene_id=scene.id, scene_version=scene.current_version) for scene in scenes),
        sources=refs,
        created_at=timestamp,
    )
    if existing is None:
        record = Episode(
            id=episode_id,
            project_id=project_id,
            title=args["title"],
            style_id=style_id,
            current_version=1,
            versions=(version,),
            revision=0,
            created_at=timestamp,
        )
    else:
        record = existing.model_copy(
            update={
                "title": args["title"],
                "style_id": style_id,
                "current_version": version.version,
                "versions": (*existing.versions, version),
                "revision": existing.revision + 1,
            }
        )
    for scene in scenes:
        state["scenes"][scene.id] = scene.model_dump(mode="json", by_alias=True)
    state["episodes"][record.id] = record.model_dump(mode="json", by_alias=True)
    return {
        "episodeId": record.id,
        "version": record.current_version,
        "revision": record.revision,
        "sceneIds": [scene.id for scene in scenes],
        "shotCount": shots,
        "targetDurationSeconds": target,
        "textSplitContract": version.text_split_contract,
        "shotBudget": budget.model_dump(mode="json", by_alias=True),
    }


def _character_summary(record: Character) -> dict:
    latest = record.latest
    return {
        "characterId": record.id,
        "name": latest.name,
        "aliases": list(latest.aliases),
        "appearance": latest.appearance,
        "wardrobe": latest.wardrobe,
        "voice": latest.voice.model_dump(mode="json", by_alias=True) if latest.voice else None,
        "relationships": [link.model_dump(mode="json", by_alias=True) for link in latest.relationships],
        "referenceAssetIds": list(latest.reference_asset_ids),
        "currentVersion": record.current_version,
        "revision": record.revision,
    }


def _style_summary(record: Style) -> dict:
    latest = record.latest
    return {
        "styleId": record.id,
        "name": latest.name,
        "genre": list(latest.genre),
        "palette": latest.palette,
        "cameraLanguage": latest.camera_language,
        "currentVersion": record.current_version,
        "revision": record.revision,
    }


def read_episode(args: dict, state: dict, project_id: str) -> dict:
    """The current episode with its frozen scenes and re-derivable shot drafts."""
    from vagent.video.adaptation import draft_stored_scene

    summaries = []
    for raw in state["episodes"].values():
        if raw["projectId"] != project_id:
            continue
        episode = Episode.model_validate(raw)
        summaries.append(
            {
                "episodeId": episode.id,
                "title": episode.title,
                "currentVersion": episode.current_version,
                "revision": episode.revision,
                "sceneCount": len(episode.latest.scenes),
                "shotCount": episode.latest.shot_budget.shot_count,
                "targetDurationSeconds": episode.latest.target_duration_seconds,
            }
        )
    summaries.sort(key=lambda item: (item["title"], item["episodeId"]))
    episode_id = args.get("episodeId")
    if episode_id:
        raw = state["episodes"].get(episode_id)
        if raw is None or raw["projectId"] != project_id:
            raise AppError("EPISODE_NOT_FOUND", "当前项目中没有这个分集。")
        episode = Episode.model_validate(raw)
    elif not summaries:
        return {
            "episodeId": None,
            "episode": None,
            "episodes": [],
            "scenes": [],
            "style": None,
            "consistency": None,
            "generation": None,
        }
    else:
        episode = Episode.model_validate(state["episodes"][summaries[0]["episodeId"]])

    style = None
    if episode.style_id is not None:
        style_record = _style_record(state, episode.style_id)
        style = {**_style_summary(style_record), "stylePrompt": style_prompt(style_record.latest)}

    scenes = []
    for reference in episode.latest.scenes:
        scene = _scene_record(state, reference.scene_id)
        if scene.episode_id != episode.id:
            continue
        frozen = next(item for item in scene.versions if item.version == reference.scene_version)
        stored = frozen.model_dump(mode="json", by_alias=True)
        scenes.append(
            {
                "sceneId": scene.id,
                "storyboardId": scene.storyboard_id,
                "revision": scene.revision,
                "currentVersion": scene.current_version,
                "frozenVersion": reference.scene_version,
                "heading": frozen.heading,
                "location": frozen.location,
                "timeOfDay": frozen.time_of_day,
                "synopsis": frozen.synopsis,
                "characterIds": list(frozen.character_ids),
                "dialogue": stored["dialogue"],
                "shotCount": frozen.shot_count,
                "durationSeconds": frozen.duration_seconds,
                "shots": draft_stored_scene(stored),
            }
        )
    characters = []
    seen = set()
    for scene in scenes:
        for character_id in scene["characterIds"]:
            if character_id in seen:
                continue
            character = _character_record(state, character_id)
            characters.append(_character_summary(character))
            seen.add(character_id)
    return {
        "episodeId": episode.id,
        "episode": {
            "title": episode.title,
            "currentVersion": episode.current_version,
            "revision": episode.revision,
            "styleId": episode.style_id,
            "createdAt": episode.created_at,
            "textSplitContract": episode.latest.text_split_contract,
            "targetDurationSeconds": episode.latest.target_duration_seconds,
            "sourceRefs": [item.model_dump(mode="json", by_alias=True) for item in episode.latest.sources],
            "shotBudget": episode.latest.shot_budget.model_dump(mode="json", by_alias=True),
        },
        "scenes": scenes,
        "characters": characters,
        "episodes": summaries,
        "style": style,
        "consistency": episode_consistency(state, episode),
        "generation": episode_generation(state, episode),
    }


def record_consistency_review(args: dict, state: dict, project_id: str) -> dict:
    """Record one human 1-5 score next to the exact cross-shot reference evidence.

    The evidence is recomputed inside the Store transaction: the review cites the
    shots and frozen reference images that exist right now, so a later redo cannot
    silently inherit an old score.
    """
    episode = _episode_record(state, args["episodeId"])
    if episode.project_id != project_id:
        raise AppError("EPISODE_NOT_FOUND", "当前项目中没有这个分集。")
    character = _character_record(state, args["characterId"])
    if character.project_id != project_id:
        raise AppError("CHARACTER_NOT_FOUND", "当前项目中没有这个角色。")
    evidence = character_evidence(state, episode, character.id)
    if not evidence["shotIds"]:
        raise AppError(
            "CONSISTENCY_EVIDENCE_MISSING",
            "这个分集已绑定的分镜中还没有引用该角色的镜头，未记录评分。",
        )
    timestamp = utc_now()
    review = ConsistencyReview(
        id=str(uuid4()),
        character_id=character.id,
        character_version=character.current_version,
        episode_version=episode.current_version,
        score=args["score"],
        note=args.get("note"),
        reviewer=args.get("reviewer") or "user",
        shot_ids=tuple(evidence["shotIds"]),
        storyboard_ids=tuple(evidence["storyboardIds"]),
        asset_ids=tuple(evidence["assetIds"]),
        consistent=evidence["consistent"],
        created_at=timestamp,
    )
    record = episode.model_copy(
        update={
            "consistency_reviews": (*episode.consistency_reviews, review),
            "revision": episode.revision + 1,
        }
    )
    state["episodes"][record.id] = record.model_dump(mode="json", by_alias=True)
    return {
        "reviewId": review.id,
        "episodeId": record.id,
        "revision": record.revision,
        "characterId": character.id,
        "score": review.score,
        "consistent": review.consistent,
        "shotIds": list(review.shot_ids),
        "assetIds": list(review.asset_ids),
        "createdAt": review.created_at,
    }


def save_bible(args: dict, state: dict, project_id: str) -> dict:
    """Create or append one character/style bible version; history is append-only."""
    kind = args.get("kind")
    if kind not in BIBLE_KINDS:
        raise AppError("INVALID_ARGUMENTS", "kind 只能是 character 或 style。")
    record_id = args.get("id")
    collection = state["characters"] if kind == "character" else state["styles"]
    raw = collection.get(record_id) if record_id else None
    if record_id and (raw is None or raw["projectId"] != project_id):
        raise AppError(
            "CHARACTER_NOT_FOUND" if kind == "character" else "STYLE_NOT_FOUND",
            "当前项目中没有这条圣经记录。",
        )
    timestamp = utc_now()
    if kind == "character":
        existing = Character.model_validate(raw) if raw is not None else None
        if existing is not None and existing.revision != args.get("expectedRevision", -1):
            raise AppError("VERSION_CONFLICT", "角色记录已变化，请重新读取后再修改。")
        entry = CharacterVersion(
            version=(existing.current_version + 1) if existing is not None else 1,
            name=args["name"],
            aliases=tuple(args.get("aliases") or ()),
            appearance=args["appearance"],
            wardrobe=args.get("wardrobe"),
            voice=args.get("voice"),
            relationships=tuple(args.get("relationships") or ()),
            reference_asset_ids=tuple(args.get("referenceAssetIds") or ()),
            created_at=timestamp,
        )
        if existing is None:
            record = Character(
                id=record_id or str(uuid4()),
                project_id=project_id,
                current_version=1,
                versions=(entry,),
                revision=0,
                created_at=timestamp,
            )
        else:
            record = existing.model_copy(
                update={
                    "versions": (*existing.versions, entry),
                    "current_version": entry.version,
                    "revision": existing.revision + 1,
                }
            )
        state["characters"][record.id] = record.model_dump(mode="json", by_alias=True)
        return {
            "kind": "character",
            "id": record.id,
            "version": record.current_version,
            "revision": record.revision,
        }
    existing_style = Style.model_validate(raw) if raw is not None else None
    if existing_style is not None and existing_style.revision != args.get("expectedRevision", -1):
        raise AppError("VERSION_CONFLICT", "风格记录已变化，请重新读取后再修改。")
    entry = StyleVersion(
        version=(existing_style.current_version + 1) if existing_style is not None else 1,
        name=args["name"],
        genre=tuple(args.get("genre") or ()),
        palette=args["palette"],
        camera_language=args["cameraLanguage"],
        created_at=timestamp,
    )
    if existing_style is None:
        record = Style(
            id=record_id or str(uuid4()),
            project_id=project_id,
            current_version=1,
            versions=(entry,),
            revision=0,
            created_at=timestamp,
        )
    else:
        record = existing_style.model_copy(
            update={
                "versions": (*existing_style.versions, entry),
                "current_version": entry.version,
                "revision": existing_style.revision + 1,
            }
        )
    state["styles"][record.id] = record.model_dump(mode="json", by_alias=True)
    return {
        "kind": "style",
        "id": record.id,
        "version": record.current_version,
        "revision": record.revision,
    }


def read_bible(args: dict, state: dict, project_id: str) -> dict:
    """Read one or all bible entries of a kind; metadata only, no private paths."""
    kind = args.get("kind")
    record_id = args.get("id")
    if kind == "character":
        records = [Character.model_validate(raw) for raw in state["characters"].values()]
        summaries = [_character_summary(record) for record in records if record.project_id == project_id]
        key = "characterId"
    elif kind == "style":
        records = [Style.model_validate(raw) for raw in state["styles"].values()]
        summaries = [_style_summary(record) for record in records if record.project_id == project_id]
        key = "styleId"
    else:
        raise AppError("INVALID_ARGUMENTS", "kind 只能是 character 或 style。")
    summaries.sort(key=lambda item: (item["name"], item[key]))
    if record_id:
        found = next((item for item in summaries if item[key] == record_id), None)
        if found is None:
            raise AppError(
                "CHARACTER_NOT_FOUND" if kind == "character" else "STYLE_NOT_FOUND",
                "当前项目中没有这条圣经记录。",
            )
        return {"kind": kind, "entry": found, "entries": summaries}
    return {"kind": kind, "entry": None, "entries": summaries}
