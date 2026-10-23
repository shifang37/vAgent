"""M7 E3 fixtures: an episode with delivered shots, a subtitle track and clips."""

from render_support import renderable_episode  # noqa: F401  (re-exported for tests)

from vagent.video.renders import FilmRenderArgs

PROJECT = "coffee"


def latest_shot_version(state: dict, storyboard_id: str, shot_id: str) -> dict:
    record = state["storyboards"][storyboard_id]
    rows = [item for item in record["shotVersions"] if item["shotId"] == shot_id]
    assert rows, f"shot {shot_id} has no registered version"
    return rows[-1]


def clips_for(state: dict, boards, *, trims=None) -> list[dict]:
    """One full-length clip per delivered shot, in board/shot order."""
    trims = trims or {}
    clips = []
    for board in boards:
        record = state["storyboards"][board["storyboardId"]]
        for shot_id in board["shotIds"]:
            shot = next(item for item in record["versions"][-1]["shots"] if item["id"] == shot_id)
            version = latest_shot_version(state, board["storyboardId"], shot_id)
            trim_in = trims.get(shot_id, 0)
            clips.append(
                {
                    "shotId": shot_id,
                    "shotVersionId": version["id"],
                    "storyboardId": board["storyboardId"],
                    "sceneId": board["sceneId"],
                    "inMs": trim_in,
                    "outMs": shot["durationSeconds"] * 1000,
                }
            )
    return clips


def save(registry, store, fixture, clips, *, key: str = "m7e3-save", **changes) -> dict:
    args = {
        "episodeId": fixture["episode"]["episodeId"],
        "clips": clips,
        "subtitle": {"trackId": fixture["trackId"], "trackVersion": 1},
        **changes,
    }
    result = registry.execute("timeline_save", args, store=store, project_id=PROJECT, operation_key=key)
    assert result["ok"], result
    return result["data"]


def read(registry, store, episode_id: str, *, key: str = "m7e3-read") -> dict:
    result = registry.execute(
        "timeline_read",
        {"episodeId": episode_id},
        store=store,
        project_id=PROJECT,
        operation_key=key,
    )
    assert result["ok"], result
    return result["data"]


def render_args(timeline_id: str, **changes) -> dict:
    return {
        "scope": "timeline",
        "timelineId": timeline_id,
        "confirm": True,
        **changes,
    }


def canonical(args: dict) -> dict:
    return FilmRenderArgs.model_validate(args).model_dump(mode="json", by_alias=True)


async def render(fixture, timeline_id: str, *, key: str = "m7e3-render", **changes) -> dict:
    args = canonical(render_args(timeline_id, **changes))
    result = await fixture["service"].renders.render(args, project_id=PROJECT, operation_key=key, args=args)
    assert result["ok"], result
    return result["data"]
