"""M5 E2 character/style consistency: repeated references, prompt template, review record.

The episode bibles (``episodes.py``) own identity and append-only history; this module
owns the three deterministic derivations the E2 work package adds:

* the frozen style prompt template ``style-prompt-zh-v1`` and the per-shot prompt
  composition used by every request path (preview, plan and registration);
* the cross-shot evidence for one character: which shots cite it, which character
  version each shot froze, and whether they all use the same reference image;
* the append-only human consistency review that records a 1-5 score next to that
  evidence. A review never changes a shot, Job or price.

Nothing here contacts a provider; the module only reads Store state.
"""

from pydantic import Field, model_validator

from vagent.contracts import Contract, Identifier, JsonTuple, UtcTimestamp
from vagent.video.storyboards import Storyboard

# The published template id: changing the wording must publish a new id, exactly as
# with text-split-zh-v1, so a saved review stays explainable.
STYLE_PROMPT_CONTRACT = "style-prompt-zh-v1"
CONSISTENCY_REVIEW_LIMIT = 64
CONSISTENCY_SCORE_MIN = 1
CONSISTENCY_SCORE_MAX = 5


class ConsistencyReview(Contract):
    """One human 1-5 score bound to the exact shots and reference images it saw."""

    id: Identifier
    character_id: Identifier
    character_version: int = Field(ge=1)
    episode_version: int = Field(ge=1)
    score: int = Field(ge=CONSISTENCY_SCORE_MIN, le=CONSISTENCY_SCORE_MAX)
    note: str | None = Field(default=None, min_length=1, max_length=500)
    reviewer: str = Field(default="user", min_length=1, max_length=60)
    shot_ids: JsonTuple[Identifier] = Field(default=(), max_length=64)
    storyboard_ids: JsonTuple[Identifier] = Field(default=(), max_length=12)
    asset_ids: JsonTuple[Identifier] = Field(default=(), max_length=4)
    consistent: bool = False
    created_at: UtcTimestamp

    @model_validator(mode="after")
    def frozen_evidence(self):
        for label, values in (
            ("shot", self.shot_ids),
            ("storyboard", self.storyboard_ids),
            ("asset", self.asset_ids),
        ):
            if len(set(values)) != len(values):
                raise ValueError(f"A consistency review cannot repeat the same {label}")
        return self


def style_prompt(version) -> str:
    """The deterministic prompt prefix of one style bible version.

    Only the four frozen style fields contribute, in a fixed order and with fixed
    punctuation, so the same version always produces the same bytes.
    """
    parts = [f"【风格】{version.name}"]
    if version.genre:
        parts.append("题材：" + "、".join(version.genre))
    parts.append("色调：" + version.palette)
    parts.append("镜头语言：" + version.camera_language)
    return "｜".join(parts)


def compose_shot_prompt(description: str, *, style=None, cast=()) -> str:
    """Style template, then the ordered cast names, then the shot's own description."""
    blocks = []
    if style is not None:
        blocks.append(style_prompt(style))
    names = list(dict.fromkeys(name for name in cast if name))
    if names:
        blocks.append("【出场】" + "、".join(names))
    blocks.append(description.strip())
    return "\n".join(blocks)


def style_for_storyboard(state: dict, storyboard_id: str):
    """The style version of the episode that bound this storyboard, if any."""
    from vagent.video.episodes import Episode, Scene, Style

    for raw in state["scenes"].values():
        if raw.get("storyboardId") != storyboard_id:
            continue
        scene = Scene.model_validate(raw)
        episode_raw = state["episodes"].get(scene.episode_id)
        if episode_raw is None:
            return None
        episode = Episode.model_validate(episode_raw)
        if episode.style_id is None:
            return None
        style_raw = state["styles"].get(episode.style_id)
        return Style.model_validate(style_raw).latest if style_raw is not None else None
    return None


def cast_names(state: dict, shot) -> tuple[str, ...]:
    """The frozen names of the characters a shot cites, in first-frame order."""
    from vagent.errors import AppError
    from vagent.video.episodes import Character

    names = []
    for reference in shot.character_refs:
        raw = state["characters"].get(reference.character_id)
        if raw is None:
            raise AppError("CHARACTER_NOT_FOUND", f"角色 {reference.character_id} 不存在，未创建任务。")
        character = Character.model_validate(raw)
        version = next(
            (item for item in character.versions if item.version == reference.character_version), None
        )
        if version is None:
            raise AppError("CHARACTER_NOT_FOUND", "镜头引用的角色版本不存在，未创建任务。")
        names.append(version.name)
    return tuple(dict.fromkeys(names))


def character_evidence(state: dict, episode, character_id: str) -> dict:
    """Every shot of the episode that cites one character, with its frozen reference.

    Only storyboards explicitly bound to the episode's frozen scenes are inspected;
    an unbound scene contributes no evidence instead of guessing a storyboard.
    """
    shots = []
    for reference in episode.latest.scenes:
        raw_scene = state["scenes"].get(reference.scene_id)
        if raw_scene is None:
            continue
        storyboard_raw = state["storyboards"].get(raw_scene.get("storyboardId") or "")
        if storyboard_raw is None:
            continue
        board = Storyboard.model_validate(storyboard_raw)
        for shot in sorted(board.versions[-1].shots, key=lambda item: item.order):
            index = next(
                (
                    position
                    for position, character in enumerate(shot.character_refs)
                    if character.character_id == character_id
                ),
                None,
            )
            if index is None or index >= len(shot.reference_asset_ids):
                continue
            shots.append(
                {
                    "storyboardId": board.id,
                    "storyboardVersion": board.current_version,
                    "shotId": shot.id,
                    "order": shot.order,
                    "characterVersion": shot.character_refs[index].character_version,
                    "assetId": shot.reference_asset_ids[index],
                }
            )
    asset_ids = list(dict.fromkeys(item["assetId"] for item in shots))
    return {
        "characterId": character_id,
        "shotIds": [item["shotId"] for item in shots],
        "storyboardIds": list(dict.fromkeys(item["storyboardId"] for item in shots)),
        "characterVersions": sorted({item["characterVersion"] for item in shots}),
        "assetIds": asset_ids,
        "shotCount": len(shots),
        # One distinct reference image across every shot is the frozen E2 promise;
        # no shot is not "consistent", it is simply unproven.
        "consistent": len(asset_ids) == 1,
        "shots": shots,
    }


def episode_consistency(state: dict, episode) -> dict:
    """Per-character evidence for every character cited by the episode's scenes."""
    from vagent.video.episodes import Character, Scene

    character_ids: list[str] = []
    for reference in episode.latest.scenes:
        raw_scene = state["scenes"].get(reference.scene_id)
        if raw_scene is None:
            continue
        scene = Scene.model_validate(raw_scene)
        frozen = next((item for item in scene.versions if item.version == reference.scene_version), None)
        if frozen is None:
            continue
        for character_id in frozen.character_ids:
            if character_id not in character_ids:
                character_ids.append(character_id)
    characters = []
    for character_id in character_ids:
        raw_character = state["characters"].get(character_id)
        entry = character_evidence(state, episode, character_id)
        entry["name"] = (
            Character.model_validate(raw_character).latest.name if raw_character is not None else None
        )
        entry["reviews"] = [
            item.model_dump(mode="json", by_alias=True)
            for item in episode.consistency_reviews
            if item.character_id == character_id
        ]
        characters.append(entry)
    return {
        "characters": characters,
        "reviews": [item.model_dump(mode="json", by_alias=True) for item in episode.consistency_reviews],
    }
