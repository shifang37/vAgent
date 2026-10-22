"""M5 E1: the deterministic ``text-split-zh-v1`` adaptation on three frozen samples."""

import hashlib
import json
from pathlib import Path

import pytest

from vagent.errors import AppError
from vagent.video.adaptation import (
    draft_stored_scene,
    extract_dialogue,
    normalize,
    plan_episode,
    split_chapters,
)
from vagent.video.contracts import SHOT_DURATION_MAX, SHOT_DURATION_MIN
from vagent.video.episodes import (
    EPISODE_MAX_SECONDS,
    EPISODE_MAX_SHOTS,
    EPISODE_MIN_SECONDS,
    EPISODE_MIN_SHOTS,
    SCENE_LIMIT,
    SCENE_MAX_SHOTS,
    TEXT_SPLIT_CONTRACT,
)

TEXTS = Path(__file__).parent / "fixtures" / "m5" / "texts"
SAMPLES = sorted(TEXTS.glob("*.md"))


def load(path):
    return path.read_text(encoding="utf-8")


def plan_of(path):
    return plan_episode(load(path), title=None)


def test_three_frozen_chinese_samples_exist():
    assert [path.name for path in SAMPLES] == [
        "fridge-notes.md",
        "last-station.md",
        "rainy-cafe.md",
    ]
    for path in SAMPLES:
        text = normalize(load(path))
        # M5 plans a 1000-2000 character story: the samples stay inside that band.
        assert 900 <= len(text) <= 2000, (path.name, len(text))


@pytest.mark.parametrize("path", SAMPLES, ids=lambda p: p.name)
def test_plan_is_deterministic_and_reproducible(path):
    first = plan_of(path)
    second = plan_of(path)
    assert first == second
    # The draft carries no time, ID or path that could make a re-plan diverge.
    assert first["textSplitContract"] == TEXT_SPLIT_CONTRACT
    assert "createdAt" not in json.dumps(first)
    assert hashlib.sha256(json.dumps(first, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


@pytest.mark.parametrize("path", SAMPLES, ids=lambda p: p.name)
def test_dialogue_is_never_dropped(path):
    text = normalize(load(path))
    plan = plan_of(path)
    quotes = [line.strip() for line in _quoted_lines(text)]
    extracted = [line["text"] for scene in plan["scenes"] for line in scene["dialogue"]]
    assert plan["dialogueCount"] == len(quotes) == len(extracted)
    assert sorted(extracted) == sorted(quotes)
    assert plan["dialogueCount"] > 0


def _quoted_lines(text):
    import re

    found = []
    for pattern in (re.compile(r"「([^」]{1,300})」"), re.compile(r"“([^”]{1,300})”")):
        found.extend(match.group(1) for match in pattern.finditer(text) if match.group(1).strip())
    return found


@pytest.mark.parametrize("path", SAMPLES, ids=lambda p: p.name)
def test_budget_respects_every_frozen_limit(path):
    plan = plan_of(path)
    shots = plan["shots"]
    scenes = plan["scenes"]
    assert EPISODE_MIN_SHOTS <= len(shots) <= EPISODE_MAX_SHOTS
    assert len(scenes) <= SCENE_LIMIT
    assert sum(scene["shotCount"] for scene in scenes) == len(shots)
    assert sum(scene["durationSeconds"] for scene in scenes) == plan["shotBudget"]["targetDurationSeconds"]
    assert EPISODE_MIN_SECONDS <= plan["shotBudget"]["targetDurationSeconds"] <= EPISODE_MAX_SECONDS
    for scene in scenes:
        assert 1 <= scene["shotCount"] <= SCENE_MAX_SHOTS
        assert scene["shotCount"] * SHOT_DURATION_MIN <= scene["durationSeconds"]
        assert scene["durationSeconds"] <= scene["shotCount"] * SHOT_DURATION_MAX
    for shot in shots:
        assert SHOT_DURATION_MIN <= shot["durationSeconds"] <= SHOT_DURATION_MAX
        assert shot["framing"] and shot["cameraMovement"] and shot["description"]
    per_scene = {}
    for shot in shots:
        per_scene[shot["sceneIndex"]] = per_scene.get(shot["sceneIndex"], 0) + shot["durationSeconds"]
    assert per_scene == {scene["index"]: scene["durationSeconds"] for scene in scenes}


@pytest.mark.parametrize("path", SAMPLES, ids=lambda p: p.name)
def test_estimate_matches_the_frozen_per_shot_price(path):
    plan = plan_of(path)
    budget = plan["shotBudget"]
    assert budget["perShotAmount"] == "3.00"
    assert budget["currency"] == "CNY"
    assert budget["episodeCapAmount"] == "60.00"
    assert budget["priceVersion"]
    assert plan["estimate"]["amount"] == f"{3 * len(plan['shots'])}.00"


def test_characters_are_recognized_from_attributed_dialogue():
    plan = plan_of(TEXTS / "rainy-cafe.md")
    names = [item["name"] for item in plan["characters"]]
    # First attribution order, not narration order: 陈默 speaks with a verb, then 林晚.
    assert names == ["陈默", "林晚"]
    assert all(item["dialogueCount"] > 0 for item in plan["characters"])
    assert all(item["firstScene"] >= 1 for item in plan["characters"])


def test_chapter_markers_split_without_creating_empty_sections():
    text = "# 第一幕\n\n内景 房间 夜\n\n门开着。\n\n## 第二幕\n\n外景 街道 清晨\n\n下雪了。\n"
    chapters = split_chapters(normalize(text))
    assert [title for title, _ in chapters] == ["第一幕", "第二幕"]
    plan = plan_episode(text)
    assert len(plan["chapters"]) == 2
    assert [scene["timeOfDay"] for scene in plan["scenes"]] == ["night", "dawn"]


def test_dialogue_attribution_prefers_an_explicit_speaker_prefix():
    lines = extract_dialogue(["林晚：「你来了。」", "陈默没有说话，只说：“坐吧。”"])
    assert [(line.speaker, line.text) for line in lines] == [("林晚", "你来了。"), ("陈默", "坐吧。")]


def test_source_text_longer_than_one_episode_is_rejected():
    with pytest.raises(AppError) as error:
        plan_episode("雨" * 20001)
    assert error.value.code == "EPISODE_SOURCE_TOO_LONG"


def test_empty_text_is_rejected():
    for text in ("", "   \n\n  \t "):
        with pytest.raises(AppError) as error:
            plan_episode(text)
        assert error.value.code == "TEXT_SPLIT_FAILED"


def test_a_single_short_scene_cannot_reach_the_frozen_minimum():
    with pytest.raises(AppError) as error:
        plan_episode("内景 房间 夜\n\n门开着，风从窗缝里进来。")
    assert error.value.code == "EPISODE_SHOT_BUDGET"


def test_too_many_scenes_is_refused_instead_of_merging_silently():
    blocks = "\n\n".join(f"内景 房间 夜\n\n第{i}次推门。门开着。" for i in range(1, SCENE_LIMIT + 2))
    with pytest.raises(AppError) as error:
        plan_episode(blocks)
    assert error.value.code == "EPISODE_SHOT_BUDGET"


@pytest.mark.parametrize("path", SAMPLES, ids=lambda p: p.name)
def test_stored_scene_rederives_the_same_shot_drafts(path):
    plan = plan_of(path)
    for scene in plan["scenes"]:
        stored = {
            key: scene[key]
            for key in (
                "index",
                "heading",
                "location",
                "timeOfDay",
                "synopsis",
                "dialogue",
                "shotCount",
                "durationSeconds",
            )
        }
        planned = [
            {key: value for key, value in shot.items() if key != "sceneIndex"}
            for shot in plan["shots"]
            if shot["sceneIndex"] == scene["index"]
        ]
        assert draft_stored_scene(stored) == planned
