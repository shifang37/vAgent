"""M5 E2: character references, scene binding, style prompts and human reviews."""

from pathlib import Path

import pytest
from shot_support import insert_image, mock_tools

from vagent.config import Config
from vagent.errors import AppError
from vagent.storage import FileStore
from vagent.video.adaptation import plan_episode
from vagent.video.consistency import cast_names, compose_shot_prompt, style_prompt
from vagent.video.episodes import Character, Episode
from vagent.video.jobs import JobService
from vagent.video.providers.mock import MockVideoAdapter
from vagent.video.storyboards import Storyboard

TEXT = (Path(__file__).parent / "fixtures" / "m5" / "texts" / "rainy-cafe.md").read_text(encoding="utf-8")


def call(registry, store, name, args, key, *, project_id="coffee"):
    return registry.execute(name, args, store=store, project_id=project_id, operation_key=key)


def ok(registry, store, name, args, key, *, project_id="coffee"):
    result = call(registry, store, name, args, key, project_id=project_id)
    assert result["ok"], result
    return result["data"]


def character(registry, store, name, assets=(), *, key="e2-character", project_id="coffee"):
    args = {"kind": "character", "name": name, "appearance": f"{name}的外观。"}
    if assets:
        args["referenceAssetIds"] = list(assets)
    return ok(registry, store, "bible_save", args, key, project_id=project_id)["id"]


def make_style(registry, store, *, key="e2-style"):
    return ok(
        registry,
        store,
        "bible_save",
        {
            "kind": "style",
            "name": "冷调雨夜",
            "genre": ["悬疑", "都市"],
            "palette": "冷蓝灰",
            "cameraLanguage": "手持跟拍，浅景深",
        },
        key,
    )["id"]


def build_episode(registry, store, cast, *, style_id=None, title="E2 一致性样例", key="e2-episode"):
    plan = plan_episode(TEXT)
    scenes = []
    for scene in plan["scenes"]:
        scenes.append(
            {
                "heading": scene["heading"],
                "location": scene["location"],
                "timeOfDay": scene["timeOfDay"],
                "synopsis": scene["synopsis"],
                "characterIds": [cast[name] for name in scene["characterNames"] if name in cast],
                "dialogue": [
                    {"speaker": line["speaker"], "text": line["text"], "paragraph": line["paragraph"]}
                    for line in scene["dialogue"]
                ],
                "shotCount": scene["shotCount"],
                "durationSeconds": scene["durationSeconds"],
            }
        )
    args = {"title": title, "scenes": scenes}
    if style_id is not None:
        args["styleId"] = style_id
    saved = ok(registry, store, "episode_save", args, key)
    saved["scenes"] = ok(registry, store, "episode_read", {}, "e2-read-" + key)["scenes"]
    return saved


def save_board(registry, store, shots, *, scene_id=None, key="e2-board", title="E2 分镜"):
    args = {"title": title, "shots": shots}
    if scene_id is not None:
        args["sceneId"] = scene_id
    return ok(registry, store, "storyboard_save", args, key)


def shot_spec(description, **changes):
    return {"description": description, "framing": "wide", "durationSeconds": 5, "mode": "t2v", **changes}


def board_shots(store, storyboard_id):
    return Storyboard.model_validate(store.snapshot()["storyboards"][storyboard_id]).versions[-1].shots


def scene_ids(saved):
    return [item["sceneId"] for item in saved["scenes"]]


def test_scene_binding_resolves_one_character_into_one_frozen_reference(store):
    registry, _service, _adapter, _context = mock_tools(store)
    asset = insert_image(store)
    cast = {"陈默": character(registry, store, "陈默", [asset])}
    saved = build_episode(registry, store, cast)
    first_scene, second_scene = scene_ids(saved)[:2]

    board = save_board(
        registry,
        store,
        [
            shot_spec("雨夜街道，霓虹反光"),
            shot_spec("陈默推门进店", mode="i2v", characterIds=[cast["陈默"]]),
        ],
        scene_id=first_scene,
        key="e2-bind",
    )
    assert board["sceneId"] == first_scene
    state = store.snapshot()
    scene = state["scenes"][first_scene]
    assert scene["storyboardId"] == board["storyboardId"] and scene["revision"] == 1
    shots = board_shots(store, board["storyboardId"])
    assert shots[0].character_refs == ()
    assert shots[1].reference_asset_ids == (asset,)
    assert shots[1].character_refs[0].character_id == cast["陈默"]
    assert shots[1].character_refs[0].character_version == 1
    assert shots[1].character_ids == (cast["陈默"],)

    read = ok(registry, store, "storyboard_read", {"storyboardId": board["storyboardId"]}, "e2-board-read")
    assert read["shots"][1]["characterIds"] == [cast["陈默"]]
    assert read["shots"][1]["referenceAssetIds"] == [asset]

    # A scene owns at most one storyboard and may not be rebound.
    other = save_board(registry, store, [shot_spec("另一个镜头")], key="e2-other-board")
    conflict = call(
        registry,
        store,
        "storyboard_save",
        {
            "storyboardId": other["storyboardId"],
            "expectedVersion": 1,
            "sceneId": first_scene,
            "title": "E2 分镜",
            "shots": [shot_spec("另一个镜头")],
        },
        "e2-rebind",
    )
    assert conflict["ok"] is False and conflict["error"]["code"] == "SCENE_CONFLICT"
    # A bound storyboard cannot move to a second scene either.
    move = call(
        registry,
        store,
        "storyboard_save",
        {
            "storyboardId": board["storyboardId"],
            "expectedVersion": 1,
            "sceneId": second_scene,
            "title": "E2 分镜",
            "shots": [shot_spec("陈默推门进店", mode="i2v", characterIds=[cast["陈默"]])],
        },
        "e2-move",
    )
    assert move["ok"] is False and move["error"]["code"] == "SCENE_CONFLICT"


def test_two_characters_fill_first_and_last_frame_in_order(store):
    registry, _service, _adapter, _context = mock_tools(store)
    first, second = insert_image(store), insert_image(store, width=1024, height=576)
    cast = {
        "陈默": character(registry, store, "陈默", [first], key="e2-c1"),
        "林晚": character(registry, store, "林晚", [second], key="e2-c2"),
    }
    board = save_board(
        registry,
        store,
        [shot_spec("陈默与林晚对视", mode="flf2v", characterIds=[cast["陈默"], cast["林晚"]])],
        key="e2-flf2v",
    )
    shot = board_shots(store, board["storyboardId"])[0]
    assert shot.reference_asset_ids == (first, second)
    assert [item.character_id for item in shot.character_refs] == [cast["陈默"], cast["林晚"]]


def test_a_character_without_a_reference_image_rejects_the_batch(store):
    registry, _service, _adapter, _context = mock_tools(store)
    cast = {"陈默": character(registry, store, "陈默")}
    result = call(
        registry,
        store,
        "storyboard_save",
        {
            "title": "缺参考图",
            "shots": [shot_spec("陈默推门进店", mode="i2v", characterIds=[cast["陈默"]])],
        },
        "e2-missing-asset",
    )
    assert result["ok"] is False and result["error"]["code"] == "CHARACTER_ASSET_MISSING"
    assert store.snapshot()["storyboards"] == {}
    assert store.snapshot()["scenes"] == {}


def test_character_reference_shape_must_match_the_mode(store):
    registry, _service, _adapter, _context = mock_tools(store)
    cast = {"陈默": character(registry, store, "陈默")}
    cases = [
        {"mode": "i2v", "characterIds": [cast["陈默"], cast["陈默"]]},
        {"mode": "t2v", "characterIds": [cast["陈默"]]},
        {"mode": "flf2v", "characterIds": [cast["陈默"]]},
        {
            "mode": "i2v",
            "characterIds": [cast["陈默"]],
            "referenceAssetIds": ["9cba9c02-05b1-5404-9c75-55d89a7de300"],
        },
    ]
    for index, changes in enumerate(cases):
        result = call(
            registry,
            store,
            "storyboard_save",
            {"title": "坏引用", "shots": [shot_spec("镜头", **changes)]},
            f"e2-shape-{index}",
        )
        assert result["ok"] is False, (index, result)
        assert result["error"]["code"] == "INVALID_ARGUMENTS"


def test_tampered_character_reference_is_rejected_on_commit(store):
    registry, _service, _adapter, _context = mock_tools(store)
    asset, other = insert_image(store), insert_image(store, width=1024, height=576)
    cast = {"陈默": character(registry, store, "陈默", [asset])}
    board = save_board(
        registry,
        store,
        [shot_spec("陈默推门进店", mode="i2v", characterIds=[cast["陈默"]])],
        key="e2-tamper",
    )

    def swap_asset(draft):
        draft["storyboards"][board["storyboardId"]]["versions"][-1]["shots"][0]["referenceAssetIds"] = [other]

    before = store.snapshot()
    with pytest.raises(AppError) as error:
        store.transaction(swap_asset)
    assert error.value.code == "CHARACTER_ASSET_MISSING"
    assert store.snapshot() == before

    def bump_version(draft):
        draft["storyboards"][board["storyboardId"]]["versions"][-1]["shots"][0]["characterRefs"][0][
            "characterVersion"
        ] = 2

    with pytest.raises(AppError) as error:
        store.transaction(bump_version)
    assert error.value.code == "CHARACTER_NOT_FOUND"
    assert store.snapshot() == before


def test_a_character_from_another_project_is_rejected(store):
    registry, _service, _adapter, _context = mock_tools(store)
    other_asset = insert_image(store, project_id="other")
    foreign = character(registry, store, "外来角色", [other_asset], key="e2-foreign", project_id="other")
    result = call(
        registry,
        store,
        "storyboard_save",
        {
            "title": "跨项目",
            "shots": [shot_spec("外来角色出现", mode="i2v", characterIds=[foreign])],
        },
        "e2-cross-project",
    )
    assert result["ok"] is False and result["error"]["code"] == "CHARACTER_CONFLICT"


def test_episode_read_reports_cross_shot_evidence_and_the_style_prompt(store):
    registry, _service, _adapter, _context = mock_tools(store)
    planner = JobService(
        store,
        [MockVideoAdapter(store)],
        config=Config(home=store.home, api_key=None, video_mode="mock"),
    )
    asset = insert_image(store)
    cast = {"陈默": character(registry, store, "陈默", [asset])}
    chosen_style = make_style(registry, store)
    saved = build_episode(registry, store, cast, style_id=chosen_style)
    first_scene, second_scene = scene_ids(saved)[:2]

    save_board(
        registry,
        store,
        [shot_spec("陈默推门进店", mode="i2v", characterIds=[cast["陈默"]])],
        scene_id=first_scene,
        key="e2-evidence-board",
    )
    styled = save_board(
        registry,
        store,
        [shot_spec("雨夜街道，霓虹反光")],
        scene_id=second_scene,
        key="e2-style-board",
    )
    read = ok(registry, store, "episode_read", {}, "e2-episode-read")
    assert read["style"]["stylePrompt"] == (
        "【风格】冷调雨夜｜题材：悬疑、都市｜色调：冷蓝灰｜镜头语言：手持跟拍，浅景深"
    )
    entry = next(item for item in read["consistency"]["characters"] if item["name"] == "陈默")
    assert entry["shotCount"] == 1 and entry["assetIds"] == [asset]
    assert entry["consistent"] is True and entry["characterVersions"] == [1]
    assert read["scenes"][0]["storyboardId"] is not None

    # The same frozen template is prepended to every derived request prompt, so the
    # preview/plan/registration paths all see identical bytes.
    plan = planner.plan_shots("coffee", styled["storyboardId"], styled["version"])
    assert plan["shots"][0]["request"]["prompt"].startswith("【风格】冷调雨夜")
    again = planner.plan_shots("coffee", styled["storyboardId"], styled["version"])
    assert again["shots"] == plan["shots"]


def test_character_names_join_the_prompt_in_frame_order(store):
    registry, _service, _adapter, _context = mock_tools(store)
    first, second = insert_image(store), insert_image(store, width=1024, height=576)
    cast = {
        "陈默": character(registry, store, "陈默", [first], key="e2-p1"),
        "林晚": character(registry, store, "林晚", [second], key="e2-p2"),
    }
    board = save_board(
        registry,
        store,
        [shot_spec("两人对视", mode="flf2v", characterIds=[cast["陈默"], cast["林晚"]])],
        key="e2-prompt-board",
    )
    shot = board_shots(store, board["storyboardId"])[0]
    assert cast_names(store.snapshot(), shot) == ("陈默", "林晚")


def test_style_prompt_is_deterministic_and_bounded():
    from vagent.video.episodes import StyleVersion

    version = StyleVersion(
        version=1,
        name="冷调雨夜",
        genre=("悬疑",),
        palette="冷蓝灰",
        camera_language="手持跟拍",
        created_at="2030-01-01T00:00:00+00:00",
    )
    assert style_prompt(version) == "【风格】冷调雨夜｜题材：悬疑｜色调：冷蓝灰｜镜头语言：手持跟拍"
    assert compose_shot_prompt(" 镜头 ", style=version, cast=("陈默",)) == (
        "【风格】冷调雨夜｜题材：悬疑｜色调：冷蓝灰｜镜头语言：手持跟拍\n【出场】陈默\n镜头"
    )
    assert compose_shot_prompt("镜头") == "镜头"


def test_a_new_character_version_makes_the_evidence_inconsistent(store):
    registry, _service, _adapter, _context = mock_tools(store)
    first, second = insert_image(store), insert_image(store, width=1024, height=576)
    cast = {"陈默": character(registry, store, "陈默", [first], key="e2-v1")}
    saved = build_episode(registry, store, cast)
    first_scene, second_scene = scene_ids(saved)[:2]

    save_board(
        registry,
        store,
        [shot_spec("陈默推门", mode="i2v", characterIds=[cast["陈默"]])],
        scene_id=first_scene,
        key="e2-v-board-1",
    )
    # A second bible version with a different image is a deliberate redesign.
    updated = ok(
        registry,
        store,
        "bible_save",
        {
            "kind": "character",
            "id": cast["陈默"],
            "expectedRevision": 0,
            "name": "陈默",
            "appearance": "陈默换了大衣。",
            "referenceAssetIds": [second],
        },
        "e2-v2",
    )
    assert updated["version"] == 2
    save_board(
        registry,
        store,
        [shot_spec("陈默回头", mode="i2v", characterIds=[cast["陈默"]])],
        scene_id=second_scene,
        key="e2-v-board-2",
    )
    read = ok(registry, store, "episode_read", {}, "e2-v-read")
    entry = next(item for item in read["consistency"]["characters"] if item["name"] == "陈默")
    assert entry["consistent"] is False
    assert entry["assetIds"] == [first, second]
    assert entry["characterVersions"] == [1, 2]
    assert entry["shotCount"] == 2


def test_consistency_review_records_evidence_and_is_append_only(store):
    registry, _service, _adapter, _context = mock_tools(store)
    asset = insert_image(store)
    cast = {"陈默": character(registry, store, "陈默", [asset])}
    saved = build_episode(registry, store, cast)
    scenes = scene_ids(saved)
    for index in range(3):
        save_board(
            registry,
            store,
            [shot_spec(f"陈默第 {index + 1} 次出现", mode="i2v", characterIds=[cast["陈默"]])],
            scene_id=scenes[index],
            key=f"e2-review-board-{index}",
        )

    args = {
        "episodeId": saved["episodeId"],
        "characterId": cast["陈默"],
        "score": 5,
        "note": "三处外观一致。",
        "reviewer": "reviewer-a",
    }
    reviewed = ok(registry, store, "consistency_review", args, "e2-review")
    assert reviewed["consistent"] is True and reviewed["score"] == 5
    assert len(reviewed["shotIds"]) == 3 and reviewed["assetIds"] == [asset]
    episode_record = Episode.model_validate(store.snapshot()["episodes"][saved["episodeId"]])
    assert episode_record.revision == reviewed["revision"] == 1
    assert len(episode_record.consistency_reviews) == 1

    replay = ok(registry, store, "consistency_review", args, "e2-review")
    assert replay == reviewed
    assert (
        len(Episode.model_validate(store.snapshot()["episodes"][saved["episodeId"]]).consistency_reviews) == 1
    )

    read = ok(registry, store, "episode_read", {}, "e2-review-read")
    entry = next(item for item in read["consistency"]["characters"] if item["name"] == "陈默")
    assert entry["reviews"][0]["score"] == 5 and entry["reviews"][0]["consistent"] is True

    def rewrite_score(draft):
        draft["episodes"][saved["episodeId"]]["consistencyReviews"][0]["score"] = 1

    with pytest.raises(AppError) as error:
        store.transaction(rewrite_score)
    assert error.value.code == "EPISODE_IMMUTABLE"

    def drop_review(draft):
        draft["episodes"][saved["episodeId"]]["consistencyReviews"] = []

    with pytest.raises(AppError) as error:
        store.transaction(drop_review)
    assert error.value.code == "EPISODE_IMMUTABLE"


def test_a_review_without_shot_evidence_is_rejected(store):
    registry, _service, _adapter, _context = mock_tools(store)
    cast = {"陈默": character(registry, store, "陈默", key="e2-no-evidence")}
    saved = build_episode(registry, store, cast)
    result = call(
        registry,
        store,
        "consistency_review",
        {"episodeId": saved["episodeId"], "characterId": cast["陈默"], "score": 4},
        "e2-review-empty",
    )
    assert result["ok"] is False and result["error"]["code"] == "CONSISTENCY_EVIDENCE_MISSING"
    record = Episode.model_validate(store.snapshot()["episodes"][saved["episodeId"]])
    assert record.consistency_reviews == () and record.revision == 0


def test_reviews_survive_a_store_reopen(store):
    registry, _service, _adapter, _context = mock_tools(store)
    asset = insert_image(store)
    cast = {"陈默": character(registry, store, "陈默", [asset])}
    saved = build_episode(registry, store, cast)
    save_board(
        registry,
        store,
        [shot_spec("陈默推门", mode="i2v", characterIds=[cast["陈默"]])],
        scene_id=scene_ids(saved)[0],
        key="e2-reopen-board",
    )
    ok(
        registry,
        store,
        "consistency_review",
        {"episodeId": saved["episodeId"], "characterId": cast["陈默"], "score": 3},
        "e2-reopen-review",
    )
    home = store.home
    store.close()
    with FileStore.open(home) as reopened:
        record = Episode.model_validate(reopened.snapshot()["episodes"][saved["episodeId"]])
        assert len(record.consistency_reviews) == 1
        assert record.consistency_reviews[0].score == 3


def test_character_bible_without_a_reference_is_still_a_valid_record(store):
    registry, _service, _adapter, _context = mock_tools(store)
    cast = {"陈默": character(registry, store, "陈默")}
    entry = Character.model_validate(store.snapshot()["characters"][cast["陈默"]])
    assert entry.latest.reference_asset_ids == ()
