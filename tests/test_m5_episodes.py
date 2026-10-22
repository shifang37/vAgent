"""M5 E0: the frozen episode/scene/bible contracts and their commit validators."""

import pytest
from shot_support import insert_image

from vagent.errors import AppError
from vagent.storage import FileStore
from vagent.video.episodes import (
    EPISODE_COST_CAP,
    SCENE_MAX_SHOTS,
    Character,
    CharacterLink,
    CharacterVersion,
    Episode,
    EpisodeVersion,
    Scene,
    SceneRef,
    SceneVersion,
    ShotBudget,
    Style,
    StyleVersion,
    VoiceProfile,
)

TS = "2030-01-01T00:00:00+00:00"


def style_record(style_id="style-1", *, project_id="coffee", version=1, name="冷色调"):
    entry = StyleVersion(
        version=version,
        name=name,
        genre=("悬疑",),
        palette="冷蓝灰",
        camera_language="手持跟拍",
        created_at=TS,
    )
    record = Style(
        id=style_id,
        project_id=project_id,
        current_version=version,
        versions=(entry,),
        revision=version - 1,
        created_at=TS,
    )
    return record.model_dump(mode="json", by_alias=True)


def character_record(
    character_id="character-a",
    *,
    project_id="coffee",
    assets=(),
    relationships=(),
    version=1,
    name="林晚",
):
    entry = CharacterVersion(
        version=version,
        name=name,
        aliases=("晚晚",),
        appearance="黑色短发，米色风衣",
        wardrobe="米色风衣",
        voice=VoiceProfile(description="低沉的女性声音"),
        relationships=tuple(CharacterLink(character_id=item, relation="同事") for item in relationships),
        reference_asset_ids=tuple(assets),
        created_at=TS,
    )
    record = Character(
        id=character_id,
        project_id=project_id,
        current_version=version,
        versions=(entry,),
        revision=version - 1,
        created_at=TS,
    )
    return record.model_dump(mode="json", by_alias=True)


def scene_record(
    scene_id,
    episode_id,
    *,
    project_id="coffee",
    shot_count=6,
    duration=45,
    character_ids=(),
    storyboard_id=None,
):
    entry = SceneVersion(
        version=1,
        heading=f"场次 {scene_id}",
        location="雨夜咖啡店门口",
        time_of_day="night",
        synopsis="她把伞收起来，推门进店。",
        character_ids=tuple(character_ids),
        shot_count=shot_count,
        duration_seconds=duration,
        created_at=TS,
    )
    record = Scene(
        id=scene_id,
        project_id=project_id,
        episode_id=episode_id,
        storyboard_id=storyboard_id,
        current_version=1,
        versions=(entry,),
        revision=0,
        created_at=TS,
    )
    return record.model_dump(mode="json", by_alias=True)


def episode_record(episode_id, refs, *, project_id="coffee", style_id=None, target=90, shots=12, version=1):
    budget = ShotBudget(shot_count=shots, target_duration_seconds=target)
    entry = EpisodeVersion(
        version=version,
        target_duration_seconds=target,
        shot_budget=budget,
        scenes=tuple(
            SceneRef(scene_id=scene_id, scene_version=scene_version) for scene_id, scene_version in refs
        ),
        created_at=TS,
    )
    record = Episode(
        id=episode_id,
        project_id=project_id,
        title="第一集 雨夜",
        style_id=style_id,
        current_version=version,
        versions=(entry,),
        revision=version - 1,
        created_at=TS,
    )
    return record.model_dump(mode="json", by_alias=True)


def insert(store, **collections):
    store.transaction(lambda draft: [draft[name].update(value) for name, value in collections.items()])


def seed_episode(store, *, asset_id=None, **overrides):
    """One atomic commit: bibles, scenes and the episode that freezes them."""
    characters = {"character-a": character_record(assets=tuple([asset_id] if asset_id else []))}
    characters["character-b"] = character_record("character-b", name="陈默", relationships=("character-a",))
    scenes = {
        "scene-1": scene_record(
            "scene-1", "episode-1", shot_count=6, duration=45, character_ids=("character-a",)
        ),
        "scene-2": scene_record(
            "scene-2", "episode-1", shot_count=6, duration=45, character_ids=("character-a", "character-b")
        ),
    }
    episode = episode_record(
        "episode-1",
        [("scene-1", 1), ("scene-2", 1)],
        style_id="style-1",
        **overrides,
    )
    insert(
        store,
        characters=characters,
        styles={"style-1": style_record()},
        scenes=scenes,
        episodes={"episode-1": episode},
    )
    return store.snapshot()


def test_episode_scenes_and_bibles_round_trip_through_the_store(store):
    store.ensure_session("coffee")
    asset_id = insert_image(store)
    state = seed_episode(store, asset_id=asset_id)
    assert set(state["episodes"]) == {"episode-1"}
    assert set(state["scenes"]) == {"scene-1", "scene-2"}
    episode = Episode.model_validate(state["episodes"]["episode-1"])
    assert episode.latest.shot_budget.shot_count == 12
    assert episode.latest.text_split_contract == "text-split-zh-v1"
    assert [item.scene_id for item in episode.latest.scenes] == ["scene-1", "scene-2"]
    assert episode.latest.shot_budget.per_shot_amount == "3.00"
    assert episode.latest.shot_budget.episode_cap_amount == EPISODE_COST_CAP
    scene = Scene.model_validate(state["scenes"]["scene-1"])
    assert scene.latest.duration_seconds == 45
    assert scene.storyboard_id is None
    # Reopening validates the same records without rewriting them.
    store.close()
    with FileStore.open(store.home) as reopened:
        assert reopened.snapshot()["episodes"] == state["episodes"]


def test_scene_binds_at_most_one_storyboard(store):
    store.ensure_session("coffee")
    seed_episode(store)

    def bind(draft):
        draft["scenes"]["scene-1"]["storyboardId"] = "board-a"

    with pytest.raises(AppError) as error:
        store.transaction(bind)
    assert error.value.code == "SCENE_CONFLICT"

    def rebind(draft):
        draft["scenes"]["scene-1"]["storyboardId"] = "board-a"

    scenes = store.snapshot()["scenes"]
    scenes["scene-1"] = {**scenes["scene-1"], "storyboardId": None}
    insert(store, scenes=scenes)
    with pytest.raises(AppError) as error:
        store.transaction(rebind)
    assert error.value.code == "SCENE_CONFLICT"


@pytest.mark.parametrize(
    ("change", "code"),
    [
        ({"shots": 13}, "EPISODE_SHOT_BUDGET"),
        ({"target": 80}, "EPISODE_DURATION_LIMIT"),
    ],
)
def test_episode_budget_must_match_its_frozen_scenes(store, change, code):
    store.ensure_session("coffee")
    seed_episode(store)
    with pytest.raises(AppError) as error:
        store.transaction(
            lambda draft: draft["episodes"].__setitem__(
                "episode-1", episode_record("episode-1", [("scene-1", 1), ("scene-2", 1)], **change)
            )
        )
    assert error.value.code == code


def test_saved_episode_history_is_append_only(store):
    store.ensure_session("coffee")
    seed_episode(store)
    with pytest.raises(AppError) as error:
        store.transaction(lambda draft: draft["episodes"].pop("episode-1"))
    assert error.value.code == "EPISODE_IMMUTABLE"

    def rewrite_frozen_version(draft):
        """Model-valid append that still rewrites the frozen first version."""
        current = draft["episodes"]["episode-1"]
        frozen = current["versions"][0]
        tampered = {**frozen, "createdAt": "2030-01-03T00:00:00+00:00"}
        appended = {**frozen, "version": 2, "createdAt": "2030-01-02T00:00:00+00:00"}
        current.update(versions=[tampered, appended], currentVersion=2, revision=1)

    with pytest.raises(AppError) as error:
        store.transaction(rewrite_frozen_version)
    assert error.value.code == "EPISODE_IMMUTABLE"
    assert store.snapshot()["episodes"]["episode-1"]["currentVersion"] == 1


def test_character_reference_assets_must_exist_in_the_project(store):
    store.ensure_session("coffee")
    with pytest.raises(AppError) as error:
        seed_episode(store, asset_id="9cba9c02-05b1-5404-9c75-55d89a7de300")
    assert error.value.code == "CHARACTER_ASSET_MISSING"
    assert store.snapshot()["episodes"] == {}


def test_character_relationships_stay_inside_one_project(store):
    store.ensure_session("coffee")

    def insert_lonely(draft):
        draft["characters"]["character-a"] = character_record(relationships=("character-missing",))

    with pytest.raises(AppError) as error:
        store.transaction(insert_lonely)
    assert error.value.code == "CHARACTER_NOT_FOUND"


def test_bibles_are_append_only(store):
    store.ensure_session("coffee")
    insert(store, characters={"character-a": character_record()}, styles={"style-1": style_record()})
    with pytest.raises(AppError) as error:
        store.transaction(lambda draft: draft["characters"].pop("character-a"))
    assert error.value.code == "CHARACTER_IMMUTABLE"
    with pytest.raises(AppError) as error:
        store.transaction(lambda draft: draft["styles"].pop("style-1"))
    assert error.value.code == "STYLE_IMMUTABLE"


def test_shot_budget_refuses_an_episode_above_the_cost_cap():
    with pytest.raises(ValueError):
        ShotBudget(shot_count=20, target_duration_seconds=90, episode_cap_amount="30.00")
    with pytest.raises(ValueError):
        ShotBudget(shot_count=21, target_duration_seconds=90)
    budget = ShotBudget(shot_count=20, target_duration_seconds=90)
    assert budget.per_shot_amount == "3.00"
    assert budget.episode_cap_amount == EPISODE_COST_CAP


def test_scene_duration_must_fit_its_own_shots():
    with pytest.raises(ValueError):
        SceneVersion(
            version=1,
            heading="过长的一场",
            synopsis="",
            shot_count=1,
            duration_seconds=30,
            created_at=TS,
        )
    assert SCENE_MAX_SHOTS == 6
