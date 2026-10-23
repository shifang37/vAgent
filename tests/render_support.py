"""M6 E3 fixtures: renderable storyboards and whole episodes from real deliveries."""

import hashlib

from conftest import VideoClock
from film_support import delivered_clip, link_shot_versions
from media_support import SAMPLE
from shot_support import mock_tools, shot
from subtitle_support import plan, save_episode

from vagent.video.jobs import JobService
from vagent.video.providers.mock import MockVideoAdapter

PROJECT = "coffee"


def scene_durations(scene: dict) -> list[int]:
    """Split the planned scene duration into integer shot durations (exact sum)."""
    count = scene["shotCount"]
    base, extra = divmod(scene["durationSeconds"], count)
    return [base + (1 if number < extra else 0) for number in range(count)]


def exact_shots(scene: dict, index: int) -> list[dict]:
    return [
        shot(f"场景 {index} 镜头 {number}", durationSeconds=duration)
        for number, duration in enumerate(scene_durations(scene), start=1)
    ]


def bind_storyboard(registry, store, scene_id: str, index: int, scene: dict) -> dict:
    result = registry.execute(
        "storyboard_save",
        {"title": f"E3 场景 {index}", "sceneId": scene_id, "shots": exact_shots(scene, index)},
        store=store,
        project_id=PROJECT,
        operation_key=f"e3-board-{index}",
    )
    assert result["ok"], result
    return result["data"]


async def deliver_storyboard(store, clock, saved: dict, *, prefix: str, count: int) -> int:
    jobs = []
    for number in range(count):
        clock.advance(6)
        jobs.append(
            await delivered_clip(store, clock, run_id=f"{prefix}-{number}", prompt=f"{prefix} {number}")
        )
    version = store.snapshot()["storyboards"][saved["storyboardId"]]["currentVersion"]
    return link_shot_versions(
        store,
        storyboard_id=saved["storyboardId"],
        storyboard_version=version,
        entries=[
            (
                shot_id,
                job.id,
                job.result.media_refs[0].media_id,
                job.request.model_dump(mode="json", by_alias=True),
            )
            for shot_id, job in zip(saved["shotIds"], jobs, strict=True)
        ],
    )


async def renderable_storyboard(store, clock=None, *, count: int = 4):
    """One bound scene plus `count` delivered clips and a saved subtitle track."""
    clock = clock or VideoClock()
    registry, _service, _adapter, context = mock_tools(store, clock=clock)
    plan_value = plan()
    episode = save_episode(registry, store, plan_value)
    index = next(number for number, scene in enumerate(plan_value["scenes"]) if scene["dialogue"])
    scene = plan_value["scenes"][index]
    saved = bind_storyboard(registry, store, episode["sceneIds"][index], index, scene)
    # A render may use more or fewer shots than the plan; keep the timeline exact.
    durations = [5] * count
    saved["shotIds"] = saved["shotIds"][:count]
    if len(saved["shotIds"]) != count:
        result = registry.execute(
            "storyboard_save",
            {
                "title": "E3 渲染分镜",
                "storyboardId": saved["storyboardId"],
                "expectedVersion": 1,
                "shots": [
                    shot(f"E3 镜头 {number}", durationSeconds=durations[number]) for number in range(count)
                ],
            },
            store=store,
            project_id=PROJECT,
            operation_key="e3-board-resize",
        )
        assert result["ok"], result
        saved = result["data"]
    revision = await deliver_storyboard(store, clock, saved, prefix="e3-storyboard", count=count)
    subtitle = registry.execute(
        "subtitle_save",
        {"scope": "storyboard", "storyboardId": saved["storyboardId"]},
        store=store,
        project_id=PROJECT,
        operation_key="e3-storyboard-subtitle",
    )
    assert subtitle["ok"], subtitle
    audio = await registry.aexecute(
        "audio_generate",
        {
            "episodeId": episode["episodeId"],
            "voices": [
                {"speaker": speaker, "voiceId": "local-narrator"}
                for speaker in sorted(
                    {
                        line["speaker"] or "画外"
                        for scene_value in plan_value["scenes"]
                        for line in scene_value["dialogue"]
                    }
                )
            ],
            "confirm": True,
        },
        store=store,
        project_id=PROJECT,
        operation_key=context.operation_key,
        context=context,
    )
    assert audio["ok"], audio
    return {
        "registry": registry,
        "context": context,
        "clock": clock,
        "plan": plan_value,
        "episode": episode,
        "sceneIndex": index,
        "storyboardId": saved["storyboardId"],
        "shotIds": saved["shotIds"],
        "revision": revision,
        "trackId": subtitle["data"]["trackId"],
        "audio": audio["data"],
        "service": JobService(store, [MockVideoAdapter(store)]),
        "sha256": hashlib.sha256(SAMPLE.read_bytes()).hexdigest(),
    }


async def renderable_episode(store, clock=None, *, skip_scene: int | None = None):
    """Every planned scene bound, delivered and subtitled; optionally one undelivered."""
    clock = clock or VideoClock()
    registry, _service, _adapter, context = mock_tools(store, clock=clock)
    plan_value = plan()
    episode = save_episode(registry, store, plan_value)
    boards = []
    for index, scene in enumerate(plan_value["scenes"]):
        scene_id = episode["sceneIds"][index]
        saved = bind_storyboard(registry, store, scene_id, index, scene)
        if index == skip_scene:
            boards.append({"sceneId": scene_id, **saved, "revision": 0, "delivered": False})
            continue
        revision = await deliver_storyboard(
            store,
            clock,
            saved,
            prefix=f"e3-episode-{index}",
            count=scene["shotCount"],
        )
        boards.append({"sceneId": scene_id, **saved, "revision": revision, "delivered": True})
    subtitle = registry.execute(
        "subtitle_save",
        {"scope": "episode", "episodeId": episode["episodeId"]},
        store=store,
        project_id=PROJECT,
        operation_key="e3-episode-subtitle",
    )
    assert subtitle["ok"], subtitle
    audio = await registry.aexecute(
        "audio_generate",
        {
            "episodeId": episode["episodeId"],
            "voices": [
                {"speaker": speaker, "voiceId": "local-narrator"}
                for speaker in sorted(
                    {
                        line["speaker"] or "画外"
                        for scene_value in plan_value["scenes"]
                        for line in scene_value["dialogue"]
                    }
                )
            ],
            "confirm": True,
        },
        store=store,
        project_id=PROJECT,
        operation_key=context.operation_key,
        context=context,
    )
    assert audio["ok"], audio
    return {
        "registry": registry,
        "context": context,
        "clock": clock,
        "plan": plan_value,
        "episode": episode,
        "boards": boards,
        "trackId": subtitle["data"]["trackId"],
        "track": subtitle["data"],
        "audio": audio["data"],
        "service": JobService(store, [MockVideoAdapter(store)]),
    }
