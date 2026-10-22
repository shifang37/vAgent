"""M5 E1/E2: adaptation and consistency tools, 8/8 publication and frozen 7/7 views."""

from pathlib import Path

import pytest
from shot_support import insert_image, mock_tools

from vagent.video import tools as video_tools
from vagent.video.adaptation import plan_episode
from vagent.video.episodes import Episode, Scene
from vagent.video.tools import (
    LIVE_VIDEO_RULES_VERSION,
    LIVE_VIDEO_TOOLS_VERSION,
    VIDEO_RULES_VERSION,
    VIDEO_TOOLS_VERSION,
)

TEXT = (Path(__file__).parent / "fixtures" / "m5" / "texts" / "rainy-cafe.md").read_text(encoding="utf-8")
M5_TOOLS = ("episode_plan", "episode_save", "episode_read", "bible_save", "bible_read")


def call(registry, store, name, args, key):
    result = registry.execute(name, args, store=store, project_id="coffee", operation_key=key)
    return result


def ok(registry, store, name, args, key):
    result = call(registry, store, name, args, key)
    assert result["ok"], result
    return result["data"]


def register_cast(registry, store, plan, *, assets=None):
    """Create one bible entry per recognized speaker, deterministically."""
    cast = {}
    for index, character in enumerate(plan["characters"], start=1):
        args = {
            "kind": "character",
            "name": character["name"],
            "appearance": f"{character['name']}的外观，来自第 {character['firstScene']} 场。",
        }
        if assets:
            args["referenceAssetIds"] = [assets[character["name"]]]
        data = ok(registry, store, "bible_save", args, f"bible-{index}")
        cast[character["name"]] = data["id"]
    return cast


def episode_args(plan, cast, *, title="雨夜咖啡店"):
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
    return {"title": title, "scenes": scenes}


def test_tool_versions_are_eight_and_seven_is_frozen():
    assert (VIDEO_TOOLS_VERSION, VIDEO_RULES_VERSION) == (8, 8)
    assert (LIVE_VIDEO_TOOLS_VERSION, LIVE_VIDEO_RULES_VERSION) == (8, 8)
    assert "episode_plan" in video_tools.M5_TOOL_NAMES
    assert "consistency_review" in video_tools.M5_E2_TOOL_NAMES


def test_new_runs_publish_eight_and_old_snapshots_restore(store):
    registry, service, _adapter, _context = mock_tools(store, clock=None)
    features = registry.features["video"]
    assert (features["toolsVersion"], features["rulesVersion"]) == (8, 8)
    names = {spec["function"]["name"] for spec in registry.specs()}
    assert set(M5_TOOLS) <= names
    assert "consistency_review" in names

    # A recorded 7/7 snapshot restores the frozen view: the E2 review tool vanishes
    # while every M5 E1 tool remains.
    recorded = {"video": {**features, "toolsVersion": 7, "rulesVersion": 7}}
    frozen = registry.for_record_features(recorded)
    assert frozen is not None
    frozen_names = {spec["function"]["name"] for spec in frozen.specs()}
    assert "consistency_review" not in frozen_names
    assert set(M5_TOOLS) <= frozen_names
    assert "storyboard_save" in frozen_names and "film_assemble" in frozen_names
    assert frozen.features == recorded
    # The 7/7 rules text keeps the E1 chapter but never mentions the E2 review tool.
    assert "episode_plan" in frozen.instructions
    assert "consistency_review" not in frozen.instructions
    assert "storyboard_save" in frozen.instructions
    assert service is not None

    # A recorded 6/6 snapshot still hides both M5 chapters.
    six = {"video": {**features, "toolsVersion": 6, "rulesVersion": 6}}
    frozen_six = registry.for_record_features(six)
    assert frozen_six is not None
    assert not (set(M5_TOOLS) & {spec["function"]["name"] for spec in frozen_six.specs()})
    assert "consistency_review" not in {spec["function"]["name"] for spec in frozen_six.specs()}
    assert "episode_plan" not in frozen_six.instructions


def test_plan_returns_a_draft_without_touching_the_store(store):
    registry, _service, _adapter, _context = mock_tools(store)
    before = store.snapshot()
    data = ok(registry, store, "episode_plan", {"text": TEXT}, "plan-1")
    assert data["textSplitContract"] == "text-split-zh-v1"
    assert data["shots"] and data["scenes"] and data["characters"]
    after = store.snapshot()
    for collection in ("episodes", "scenes", "characters", "styles", "jobs", "storyboards"):
        assert after[collection] == before[collection]


def test_plan_replays_the_same_operation_without_recomputing(store):
    registry, _service, _adapter, _context = mock_tools(store)
    first = ok(registry, store, "episode_plan", {"text": TEXT}, "plan-replay")
    second = ok(registry, store, "episode_plan", {"text": TEXT}, "plan-replay")
    assert first == second
    conflict = call(registry, store, "episode_plan", {"text": TEXT + "\n再次。"}, "plan-replay")
    assert conflict["ok"] is False and conflict["error"]["code"] == "OPERATION_CONFLICT"


def test_episode_save_persists_scenes_dialogue_and_budget(store):
    registry, _service, _adapter, _context = mock_tools(store)
    plan = plan_episode(TEXT)
    cast = register_cast(registry, store, plan)
    saved = ok(registry, store, "episode_save", episode_args(plan, cast), "episode-save-1")
    assert saved["version"] == 1 and saved["revision"] == 0
    assert saved["shotCount"] == len(plan["shots"])
    assert saved["targetDurationSeconds"] == plan["shotBudget"]["targetDurationSeconds"]
    assert len(saved["sceneIds"]) == len(plan["scenes"])

    state = store.snapshot()
    episode = Episode.model_validate(state["episodes"][saved["episodeId"]])
    assert episode.title == "雨夜咖啡店"
    assert [ref.scene_id for ref in episode.latest.scenes] == saved["sceneIds"]
    stored_dialogue = sum(
        len(Scene.model_validate(state["scenes"][scene_id]).latest.dialogue) for scene_id in saved["sceneIds"]
    )
    assert stored_dialogue == plan["dialogueCount"] > 0
    first = Scene.model_validate(state["scenes"][saved["sceneIds"][0]])
    assert first.latest.synopsis != first.latest.heading
    # Saving an episode never registers a Job or creates a storyboard.
    assert state["jobs"] == {} and state["storyboards"] == {}

    view = ok(registry, store, "episode_read", {}, "episode-read-1")
    assert view["episodeId"] == saved["episodeId"]
    assert len(view["scenes"]) == len(plan["scenes"])
    assert view["scenes"][0]["shots"][0]["description"]
    assert view["episodes"][0]["shotCount"] == saved["shotCount"]
    assert {item["name"] for item in view["characters"]} == {item["name"] for item in plan["characters"]}


def test_episode_edit_appends_versions_and_keeps_untouched_scenes(store):
    registry, _service, _adapter, _context = mock_tools(store)
    plan = plan_episode(TEXT)
    cast = register_cast(registry, store, plan)
    args = episode_args(plan, cast)
    saved = ok(registry, store, "episode_save", args, "episode-save-1")
    revision = store.snapshot()["episodes"][saved["episodeId"]]["revision"]

    edited = episode_args(plan, cast, title="雨夜咖啡店（第二稿）")
    edited["episodeId"] = saved["episodeId"]
    edited["expectedRevision"] = revision
    edited["scenes"][0]["id"] = saved["sceneIds"][0]
    edited["scenes"][0]["synopsis"] = "改过的第一场概要。"
    # +1 shot keeps the episode inside the frozen 10..20 ceiling; the five seconds
    # come from the last scene so the episode total stays exactly 90.
    edited["scenes"][0]["shotCount"] = 3
    edited["scenes"][0]["durationSeconds"] = 15
    edited["scenes"][-1]["durationSeconds"] -= 5
    second = ok(registry, store, "episode_save", edited, "episode-save-2")
    assert second["version"] == 2 and second["revision"] == revision + 1
    assert len(second["sceneIds"]) == len(plan["scenes"])
    assert second["shotCount"] == len(plan["shots"]) + 1
    assert second["targetDurationSeconds"] == 90

    state = store.snapshot()
    episode = Episode.model_validate(state["episodes"][saved["episodeId"]])
    assert len(episode.versions) == 2
    first_scene = Scene.model_validate(state["scenes"][saved["sceneIds"][0]])
    assert first_scene.current_version == 2 and len(first_scene.versions) == 2
    # The untouched scene is not rewritten.
    untouched = Scene.model_validate(state["scenes"][saved["sceneIds"][1]])
    assert untouched.current_version == 1 and untouched.revision == 0


def test_stale_revision_is_rejected(store):
    registry, _service, _adapter, _context = mock_tools(store)
    plan = plan_episode(TEXT)
    cast = register_cast(registry, store, plan)
    saved = ok(registry, store, "episode_save", episode_args(plan, cast), "episode-save-1")
    stale = episode_args(plan, cast, title="过期修改")
    stale["episodeId"] = saved["episodeId"]
    stale["expectedRevision"] = 7
    result = call(registry, store, "episode_save", stale, "episode-save-stale")
    assert result["ok"] is False and result["error"]["code"] == "VERSION_CONFLICT"


def _repeat(args):
    """Only scenes left, each capped at six shots: 12 shots are unreachable."""
    args["scenes"] = args["scenes"][:12]
    for scene in args["scenes"]:
        scene["shotCount"] = 6
        scene["durationSeconds"] = 30
    return args


@pytest.mark.parametrize(
    ("mutate", "code"),
    [
        (lambda args: args.update(scenes=args["scenes"][:1]), "EPISODE_SHOT_BUDGET"),
        (_repeat, "EPISODE_SHOT_BUDGET"),
        (lambda args: args["scenes"][0].update(shotCount=7), "EPISODE_SHOT_BUDGET"),
        (lambda args: args["scenes"][0].update(durationSeconds=81), "EPISODE_DURATION_LIMIT"),
        (lambda args: args.update(targetDurationSeconds=91), "EPISODE_DURATION_LIMIT"),
    ],
)
def test_budget_violations_reject_the_whole_save(store, mutate, code):
    registry, _service, _adapter, _context = mock_tools(store)
    plan = plan_episode(TEXT)
    cast = register_cast(registry, store, plan)
    args = episode_args(plan, cast)
    mutate(args)
    result = call(registry, store, "episode_save", args, "episode-save-bad")
    assert result["ok"] is False
    assert result["error"]["code"] in {code, "INVALID_ARGUMENTS"}
    assert store.snapshot()["episodes"] == {} and store.snapshot()["scenes"] == {}


def test_unknown_character_is_rejected(store):
    registry, _service, _adapter, _context = mock_tools(store)
    plan = plan_episode(TEXT)
    args = episode_args(plan, {})
    args["scenes"][0]["characterIds"] = ["0f0f0f0f-0000-4000-8000-000000000000"]
    result = call(registry, store, "episode_save", args, "episode-save-cast")
    assert result["ok"] is False
    assert result["error"]["code"] in {"CHARACTER_NOT_FOUND", "EPISODE_SHOT_BUDGET"}


def test_character_reference_asset_must_be_imported(store):
    registry, _service, _adapter, _context = mock_tools(store)
    result = call(
        registry,
        store,
        "bible_save",
        {
            "kind": "character",
            "name": "林晚",
            "appearance": "黑色短发",
            "referenceAssetIds": ["9cba9c02-05b1-5404-9c75-55d89a7de300"],
        },
        "bible-missing-asset",
    )
    assert result["ok"] is False and result["error"]["code"] == "CHARACTER_ASSET_MISSING"


def test_character_with_imported_asset_round_trips(store):
    registry, _service, _adapter, _context = mock_tools(store)
    asset_id = insert_image(store)
    saved = ok(
        registry,
        store,
        "bible_save",
        {
            "kind": "character",
            "name": "林晚",
            "aliases": ["晚晚"],
            "appearance": "黑色短发，米色风衣",
            "wardrobe": "米色风衣",
            "voice": {"description": "低沉的女性声音", "pace": "slow"},
            "referenceAssetIds": [asset_id],
        },
        "bible-character",
    )
    assert saved["version"] == 1
    read = ok(registry, store, "bible_read", {"kind": "character"}, "bible-read")
    assert read["entries"][0]["characterId"] == saved["id"]
    assert read["entries"][0]["referenceAssetIds"] == [asset_id]
    assert read["entries"][0]["voice"]["pace"] == "slow"
    # The same id with a new version is append-only.
    updated = ok(
        registry,
        store,
        "bible_save",
        {
            "kind": "character",
            "id": saved["id"],
            "expectedRevision": 0,
            "name": "林晚",
            "appearance": "黑色短发，深灰风衣",
            "referenceAssetIds": [asset_id],
        },
        "bible-character-2",
    )
    assert updated["version"] == 2 and updated["revision"] == 1
    entry = store.snapshot()["characters"][saved["id"]]
    assert len(entry["versions"]) == 2


def test_style_bible_round_trip(store):
    registry, _service, _adapter, _context = mock_tools(store)
    saved = ok(
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
        "style-save",
    )
    read = ok(registry, store, "bible_read", {"kind": "style", "id": saved["id"]}, "style-read")
    assert read["entry"]["name"] == "冷调雨夜"
    assert read["entry"]["genre"] == ["悬疑", "都市"]


def test_kind_specific_fields_are_enforced(store):
    registry, _service, _adapter, _context = mock_tools(store)
    missing = call(
        registry,
        store,
        "bible_save",
        {"kind": "character", "name": "林晚"},
        "bible-no-appearance",
    )
    assert missing["ok"] is False and missing["error"]["code"] == "INVALID_ARGUMENTS"
    mixed = call(
        registry,
        store,
        "bible_save",
        {
            "kind": "style",
            "name": "冷调",
            "palette": "冷蓝",
            "cameraLanguage": "手持",
            "appearance": "不该出现",
        },
        "bible-mixed",
    )
    assert mixed["ok"] is False and mixed["error"]["code"] == "INVALID_ARGUMENTS"


def test_read_only_runs_get_a_registry_without_writers(store):
    registry, _service, _adapter, _context = mock_tools(store)
    read_only = registry.read_only()
    names = {spec["function"]["name"] for spec in read_only.specs()}
    assert {"episode_plan", "episode_read", "bible_read"} <= names
    assert not {"episode_save", "bible_save", "storyboard_save"} & names
    # A read tool still works under that view and never mutates the episode domain.
    plan = ok(read_only, store, "episode_plan", {"text": TEXT}, "read-only-plan")
    assert plan["shots"]
    assert store.snapshot()["episodes"] == {} and store.snapshot()["characters"] == {}


def test_writes_are_journaled_and_replay_without_a_second_version(store):
    registry, _service, _adapter, _context = mock_tools(store)
    plan = plan_episode(TEXT)
    cast = register_cast(registry, store, plan)
    args = episode_args(plan, cast)
    first = ok(registry, store, "episode_save", args, "episode-replay")
    again = ok(registry, store, "episode_save", args, "episode-replay")
    assert first == again
    episode = store.snapshot()["episodes"][first["episodeId"]]
    assert len(episode["versions"]) == 1
    changed = episode_args(plan, cast, title="另一个标题")
    conflict = call(registry, store, "episode_save", changed, "episode-replay")
    assert conflict["ok"] is False and conflict["error"]["code"] == "OPERATION_CONFLICT"


def test_bible_read_requires_a_kind(store):
    registry, _service, _adapter, _context = mock_tools(store)
    result = call(registry, store, "bible_read", {}, "bible-no-kind")
    assert result["ok"] is False and result["error"]["code"] == "INVALID_ARGUMENTS"


def test_episode_read_without_an_episode_is_empty(store):
    registry, _service, _adapter, _context = mock_tools(store)
    data = ok(registry, store, "episode_read", {}, "episode-read-empty")
    assert data == {
        "episodeId": None,
        "episode": None,
        "episodes": [],
        "scenes": [],
        "style": None,
        "consistency": None,
    }
