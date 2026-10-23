"""M6 E1 fixtures: an episode, a bound scene, delivered clips and an assembled film.

The subtitle aligner has two inputs only: the frozen scene dialogue and an exact shot
timeline. These helpers build both from the real M5/M3 code paths, so the tests never
invent a timeline or a dialogue record that the product could not produce.
"""

import json
from pathlib import Path

from film_support import delivered_clip, link_shot_versions
from shot_support import mock_tools, shot

from vagent.video.adaptation import plan_episode
from vagent.video.film_service import FilmService
from vagent.video.jobs import JobService
from vagent.video.providers.mock import MockVideoAdapter

TEXTS = Path(__file__).parent / "fixtures" / "m5" / "texts"
TEXT = (TEXTS / "rainy-cafe.md").read_text(encoding="utf-8")
EPISODE_TITLE = "雨夜咖啡店"
SHOTS_PER_SCENE = 2


def plan() -> dict:
    return plan_episode(TEXT, title=EPISODE_TITLE)


def episode_args(plan_value: dict) -> dict:
    """The saved episode: every scene keeps its planned dialogue verbatim."""
    return {
        "title": EPISODE_TITLE,
        "scenes": [
            {
                "heading": scene["heading"],
                "location": scene["location"],
                "timeOfDay": scene["timeOfDay"],
                "synopsis": scene["synopsis"],
                "characterIds": [],
                "dialogue": [
                    {"text": line["text"], "speaker": line["speaker"], "paragraph": line["paragraph"]}
                    for line in scene["dialogue"]
                ],
                "shotCount": scene["shotCount"],
                "durationSeconds": scene["durationSeconds"],
            }
            for scene in plan_value["scenes"]
        ],
    }


def save_episode(registry, store, value: dict | None = None) -> dict:
    value = value or plan()
    result = registry.execute(
        "episode_save",
        episode_args(value),
        store=store,
        project_id="coffee",
        operation_key="m6-episode-save",
    )
    assert result["ok"], result
    return result["data"]


def scene_with_dialogue(value: dict) -> int:
    """The first planned scene that actually has frozen dialogue."""
    return next(index for index, scene in enumerate(value["scenes"]) if scene["dialogue"])


def save_bound_storyboard(registry, store, scene_id: str, shots, *, key: str):
    result = registry.execute(
        "storyboard_save",
        {"title": "雨夜咖啡店", "sceneId": scene_id, "shots": shots},
        store=store,
        project_id="coffee",
        operation_key=key,
    )
    assert result["ok"], result
    return result["data"]


async def film_with_dialogue(store, clock=None):
    """Two delivered clips, one bound scene and a two-shot assembled film."""
    from conftest import VideoClock

    clock = clock or VideoClock()
    registry, _service, _adapter, _context = mock_tools(store, clock=clock)
    round_value = plan()
    episode = save_episode(registry, store, round_value)
    scene_index = scene_with_dialogue(round_value)
    scene_id = episode["sceneIds"][scene_index]
    saved = save_bound_storyboard(
        registry,
        store,
        scene_id,
        [shot("雨夜街道"), shot("推门入店")],
        key="m6-storyboard",
    )
    first = await delivered_clip(store, clock, run_id="m6-run-1", prompt="雨夜街道")
    second = await delivered_clip(store, clock, run_id="m6-run-2", prompt="推门入店")
    revision = link_shot_versions(
        store,
        storyboard_id=saved["storyboardId"],
        storyboard_version=1,
        entries=[
            (
                shot_id,
                job.id,
                job.result.media_refs[0].media_id,
                job.request.model_dump(mode="json", by_alias=True),
            )
            for shot_id, job in zip(saved["shotIds"], (first, second), strict=True)
        ],
    )
    service = FilmService(JobService(store, [MockVideoAdapter(store)]))
    result = await service.assemble(
        project_id="coffee",
        storyboard_id=saved["storyboardId"],
        expected_revision=revision,
        operation_key="m6-film-op",
        args={"storyboardId": saved["storyboardId"], "expectedRevision": revision},
    )
    assert result["ok"], result
    return {
        "storyboardId": saved["storyboardId"],
        "sceneId": scene_id,
        "episodeId": episode["episodeId"],
        "shotIds": saved["shotIds"],
        "revision": revision,
        "film": result["data"],
        "dialogue": round_value["scenes"][scene_index]["dialogue"],
        "registry": registry,
        "plan": round_value,
    }


def all_scenes_bound(store, registry) -> dict:
    """Bind every planned scene to a storyboard of its planned shot count."""
    value = plan()
    episode = save_episode(registry, store, value)
    for index, scene in enumerate(value["scenes"]):
        scene_id = episode["sceneIds"][index]
        save_bound_storyboard(
            registry,
            store,
            scene_id,
            [shot(f"{scene['heading']} 第 {number} 镜") for number in range(1, scene["shotCount"] + 1)],
            key=f"m6-episode-storyboard-{index}",
        )
    return {"episodeId": episode["episodeId"], "sceneIds": episode["sceneIds"], "plan": value}


def dump_v6_state(state: dict) -> str:
    """A real schema-6 envelope: the v7 state without its two new collections."""
    legacy = json.loads(json.dumps(state))
    legacy["schemaVersion"] = 6
    legacy.pop("audio", None)
    legacy.pop("subtitleTracks", None)
    legacy.pop("renders", None)
    return json.dumps(legacy, ensure_ascii=False, indent=2, allow_nan=False)
