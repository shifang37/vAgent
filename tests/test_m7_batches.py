"""M7 E2: season batches, per-episode isolation, resume and crash recovery."""

import copy
from uuid import uuid4

import pytest
from conftest import video_run
from shot_support import mock_tools, next_context

from vagent.errors import AppError
from vagent.storage import FileStore
from vagent.video.batches import BATCH_AMOUNT_LIMIT, Batch, BatchEpisode

PROJECT = "coffee"
FRAMINGS = ("wide", "medium", "close", "over_shoulder")
SCENES = ((4, 20), (4, 20), (4, 20))


def tool(registry, store, name, args, context=None, *, key=None):
    return registry.execute(
        name,
        args,
        store=store,
        project_id=PROJECT,
        operation_key=key or (context.operation_key if context is not None else f"m7b-{uuid4()}"),
        context=context,
    )


def ok(registry, store, name, args, context=None, *, key=None):
    result = tool(registry, store, name, args, context, key=key)
    assert result["ok"], result
    return result["data"]


def batch_run(store, mode="mock", *, run_id="m7b-run"):
    context = video_run(store, run_id, project_id=PROJECT)
    features = {
        "video": {
            "mode": mode,
            "toolsVersion": 16,
            "rulesVersion": 16,
            "capabilities": [],
        }
    }

    def upgrade(draft):
        draft["runs"][run_id].update(videoMode=mode, executionVersion=3, toolFeatures=features)

    store.transaction(upgrade)
    return context


def board_shots(count, seconds):
    base, extra = divmod(seconds, count)
    return [
        {
            "description": f"镜头{index + 1}：雨夜咖啡店",
            "framing": FRAMINGS[index % len(FRAMINGS)],
            "durationSeconds": base + (1 if index < extra else 0),
            "mode": "t2v",
        }
        for index in range(count)
    ]


def build_episode(registry, store, context, title, key, *, bound=True, scenes=SCENES):
    saved = ok(
        registry,
        store,
        "episode_save",
        {
            "title": title,
            "scenes": [
                {
                    "heading": f"第{index + 1}场：雨夜咖啡店",
                    "synopsis": f"第{index + 1}场的概要。",
                    "timeOfDay": "night",
                    "shotCount": shots,
                    "durationSeconds": seconds,
                }
                for index, (shots, seconds) in enumerate(scenes)
            ],
        },
        key=f"{key}-episode",
    )
    read = ok(registry, store, "episode_read", {"episodeId": saved["episodeId"]}, key=f"{key}-read")
    if bound:
        for index, (shots, seconds) in enumerate(scenes):
            ok(
                registry,
                store,
                "storyboard_save",
                {
                    "title": f"第{index + 1}场分镜",
                    "sceneId": read["scenes"][index]["sceneId"],
                    "shots": board_shots(shots, seconds),
                },
                key=f"{key}-board-{index}",
            )
    return saved["episodeId"]


def save_season(registry, store, episode_ids, *, title="第一季", key="m7b-season"):
    return ok(
        registry,
        store,
        "season_save",
        {"title": title, "episodeIds": list(episode_ids)},
        key=key,
    )


@pytest.fixture
def batch_world(store):
    registry, service, _adapter, context = mock_tools(store)
    return registry, service, context


def test_mock_season_batch_registers_three_episodes_and_reports_progress(store, batch_world):
    registry, service, _context = batch_world
    context = batch_run(store)
    episodes = [
        build_episode(registry, store, context, f"第{index + 1}集", f"m7b-three-{index}")
        for index in range(3)
    ]
    season = save_season(registry, store, episodes)

    call = next_context(context, "season-batch")
    data = ok(
        registry,
        store,
        "season_generate",
        {"seasonId": season["seasonId"], "confirm": True, "episodeIds": episodes},
        call,
    )

    assert data["status"] == "succeeded"
    assert data["episodeIds"] == episodes
    assert data["newJobCount"] == 36
    assert data["simulated"] is True and data["totalEstimate"] is None
    assert [item["status"] for item in data["episodes"]] == ["succeeded"] * 3
    assert [item["shotCount"] for item in data["episodes"]] == [12, 12, 12]
    assert [item["jobCount"] for item in data["episodes"]] == [12, 12, 12]
    assert all(item["errorCode"] is None for item in data["episodes"])

    state = store.snapshot()
    assert len(state["jobs"]) == 36
    assert all(job["mode"] == "mock" for job in state["jobs"].values())
    batch_raw = state["batches"][data["batchId"]]
    stored = Batch.model_validate(batch_raw)
    assert stored.status == "succeeded" and stored.revision == 6
    # One Store transaction per episode phase: pending -> running -> succeeded.
    operations = [key for key in state["operations"] if key.startswith(f"{call.run_id}:1:batch:")]
    assert len(operations) == 3
    # Every Job cites its per-episode batch operation as its registration proof.
    for job in state["jobs"].values():
        assert ":batch:" in job["operationKey"]

    # The same call replays its recorded outcome byte for byte and adds no Job.
    replay = ok(
        registry,
        store,
        "season_generate",
        {"seasonId": season["seasonId"], "confirm": True, "episodeIds": episodes},
        call,
    )
    assert replay == {**data, "replayed": True}
    assert len(store.snapshot()["jobs"]) == 36

    read = ok(registry, store, "season_read", {"seasonId": season["seasonId"]})
    assert read["batch"]["batchId"] == data["batchId"]
    assert [item["title"] for item in read["batch"]["episodes"]] == ["第1集", "第2集", "第3集"]
    assert read["batch"]["episodes"][0]["pendingShots"] == 12
    assert read["batch"]["episodes"][0]["deliveredShots"] == 0
    assert [item["batchId"] for item in read["batches"]] == [data["batchId"]]
    by_batch = ok(registry, store, "season_read", {"batchId": data["batchId"]})
    assert by_batch["seasonId"] == season["seasonId"]
    assert by_batch["batch"]["batchId"] == data["batchId"]


def test_an_empty_episode_fails_alone_and_the_rest_continue(store, batch_world):
    registry, _service, _context = batch_world
    context = batch_run(store)
    first = build_episode(registry, store, context, "第一集", "m7b-partial-0")
    broken = build_episode(registry, store, context, "第二集", "m7b-partial-1", bound=False)
    third = build_episode(registry, store, context, "第三集", "m7b-partial-2")
    season = save_season(registry, store, [first, broken, third])

    data = ok(
        registry,
        store,
        "season_generate",
        {"seasonId": season["seasonId"], "confirm": True},
        next_context(context, "season-partial"),
    )
    assert data["status"] == "partial"
    by_id = {item["episodeId"]: item for item in data["episodes"]}
    assert by_id[first]["status"] == "succeeded"
    assert by_id[broken]["status"] == "failed"
    assert by_id[broken]["errorCode"] == "BATCH_EMPTY"
    assert by_id[third]["status"] == "succeeded"
    assert len(store.snapshot()["jobs"]) == 24
    assert len(store.snapshot()["batches"]) == 1


def test_a_four_member_season_requires_an_explicit_three_episode_selection(store, batch_world):
    registry, _service, _context = batch_world
    context = batch_run(store)
    episodes = [
        build_episode(registry, store, context, f"第{index + 1}集", f"m7b-four-{index}") for index in range(4)
    ]
    season = save_season(registry, store, episodes)

    rejected = tool(
        registry,
        store,
        "season_generate",
        {"seasonId": season["seasonId"], "confirm": True},
        next_context(context, "season-four-default"),
    )
    assert rejected["ok"] is False and rejected["error"]["code"] == "BATCH_LIMIT"
    assert store.snapshot()["batches"] == {} and store.snapshot()["jobs"] == {}

    accepted = ok(
        registry,
        store,
        "season_generate",
        {"seasonId": season["seasonId"], "confirm": True, "episodeIds": episodes[:3]},
        next_context(context, "season-four-explicit"),
    )
    assert accepted["status"] == "succeeded"
    assert len(accepted["episodes"]) == 3


def test_batch_aggregate_limit_rejects_before_any_registration(store, batch_world, monkeypatch):
    from vagent.video import jobs as job_module

    monkeypatch.setattr(job_module, "BATCH_JOB_LIMIT", 10)
    registry, _service, _context = batch_world
    context = batch_run(store)
    episodes = [
        build_episode(registry, store, context, f"第{index + 1}集", f"m7b-limit-{index}")
        for index in range(2)
    ]
    season = save_season(registry, store, episodes)
    rejected = tool(
        registry,
        store,
        "season_generate",
        {"seasonId": season["seasonId"], "confirm": True},
        next_context(context, "season-limit"),
    )
    assert rejected["ok"] is False and rejected["error"]["code"] == "BATCH_LIMIT"
    state = store.snapshot()
    assert state["batches"] == {} and state["jobs"] == {}
    assert BATCH_AMOUNT_LIMIT == "180.00"


def test_resume_creates_a_new_batch_for_only_the_failed_episodes(store, batch_world):
    registry, _service, _context = batch_world
    context = batch_run(store)
    first = build_episode(registry, store, context, "第一集", "m7b-resume-0")
    broken = build_episode(registry, store, context, "第二集", "m7b-resume-1", bound=False)
    season = save_season(registry, store, [first, broken])
    original = ok(
        registry,
        store,
        "season_generate",
        {"seasonId": season["seasonId"], "confirm": True},
        next_context(context, "season-resume-first"),
    )
    assert original["status"] == "partial"
    frozen = copy.deepcopy(store.snapshot()["batches"][original["batchId"]])

    # Fix the broken episode: append EpisodeVersion 2, then bind its storyboards.
    read = ok(registry, store, "episode_read", {"episodeId": broken})
    ok(
        registry,
        store,
        "episode_save",
        {
            "episodeId": broken,
            "expectedRevision": 0,
            "title": "第二集",
            "scenes": [
                {
                    "id": read["scenes"][index]["sceneId"],
                    "heading": f"第{index + 1}场：雨夜咖啡店",
                    "synopsis": f"第{index + 1}场的概要。",
                    "timeOfDay": "night",
                    "shotCount": shots,
                    "durationSeconds": seconds,
                }
                for index, (shots, seconds) in enumerate(SCENES)
            ],
        },
        key="m7b-resume-episode-v2",
    )
    read = ok(registry, store, "episode_read", {"episodeId": broken})
    for index, (shots, seconds) in enumerate(SCENES):
        ok(
            registry,
            store,
            "storyboard_save",
            {
                "title": f"第{index + 1}场分镜",
                "sceneId": read["scenes"][index]["sceneId"],
                "shots": board_shots(shots, seconds),
            },
            key=f"m7b-resume-fix-{index}",
        )

    resumed = ok(
        registry,
        store,
        "season_generate",
        {"seasonId": season["seasonId"], "resumeBatchId": original["batchId"], "confirm": True},
        next_context(context, "season-resume-second"),
    )
    assert resumed["batchId"] != original["batchId"]
    assert resumed["resumeOf"] == original["batchId"]
    assert resumed["episodeIds"] == [broken]
    assert resumed["status"] == "succeeded"
    assert resumed["episodes"][0]["episodeVersion"] == 2
    assert len(store.snapshot()["jobs"]) == 24

    # The old batch is byte-for-byte unchanged and stays terminal.
    assert store.snapshot()["batches"][original["batchId"]] == frozen
    assert frozen["status"] == "partial"
    # A resume of a fully succeeded batch is refused.
    refused = tool(
        registry,
        store,
        "season_generate",
        {"seasonId": season["seasonId"], "resumeBatchId": resumed["batchId"], "confirm": True},
        next_context(context, "season-resume-third"),
    )
    assert refused["ok"] is False and refused["error"]["code"] == "BATCH_CONFLICT"


def test_a_second_running_batch_is_refused(store, batch_world):
    registry, _service, _context = batch_world
    context = batch_run(store)
    first = build_episode(registry, store, context, "第一集", "m7b-running-0")
    season = save_season(registry, store, [first])
    running = Batch(
        id=str(uuid4()),
        project_id=PROJECT,
        season_id=season["seasonId"],
        season_version=1,
        episode_ids=(first,),
        status="running",
        episodes=(BatchEpisode(episode_id=first, episode_version=1, status="pending", shot_count=12),),
        fingerprint="0" * 64,
        created_at="2030-01-01T00:00:00+00:00",
        updated_at="2030-01-01T00:00:00+00:00",
        revision=0,
    )
    store.transaction(
        lambda draft: draft["batches"].__setitem__(running.id, running.model_dump(mode="json", by_alias=True))
    )
    blocked = tool(
        registry,
        store,
        "season_generate",
        {"seasonId": season["seasonId"], "confirm": True},
        next_context(context, "season-running"),
    )
    assert blocked["ok"] is False and blocked["error"]["code"] == "BATCH_CONFLICT"
    assert store.snapshot()["jobs"] == {}


def test_an_unrelated_commit_while_a_batch_runs_does_not_advance_it(store, batch_world):
    registry, _service, _context = batch_world
    context = batch_run(store)
    first = build_episode(registry, store, context, "第一集", "m7b-unrelated-0")
    season = save_season(registry, store, [first])
    running = Batch(
        id=str(uuid4()),
        project_id=PROJECT,
        season_id=season["seasonId"],
        season_version=1,
        episode_ids=(first,),
        status="running",
        episodes=(BatchEpisode(episode_id=first, episode_version=1, status="pending", shot_count=12),),
        fingerprint="2" * 64,
        created_at="2030-01-01T00:00:00+00:00",
        updated_at="2030-01-01T00:00:00+00:00",
        revision=0,
    )
    store.transaction(
        lambda draft: draft["batches"].__setitem__(running.id, running.model_dump(mode="json", by_alias=True))
    )
    # A Job poll, episode save or render commit must not be mistaken for a batch advance.
    store.transaction(lambda draft: draft["projects"][PROJECT].__setitem__("goal", "unrelated"))
    assert store.snapshot()["projects"][PROJECT]["goal"] == "unrelated"
    assert store.snapshot()["batches"][running.id]["revision"] == 0


def test_store_recovers_a_running_batch_as_interrupted_on_open(tmp_path):
    home = tmp_path / "state"
    with FileStore.open(home) as store:
        store.ensure_session(PROJECT)
        registry, _service, _adapter, _context = mock_tools(store)
        context = batch_run(store)
        first = build_episode(registry, store, context, "第一集", "m7b-crash-0")
        second = build_episode(registry, store, context, "第二集", "m7b-crash-1")
        season = save_season(registry, store, [first, second])
        done = ok(
            registry,
            store,
            "season_generate",
            {"seasonId": season["seasonId"], "confirm": True, "episodeIds": [first]},
            next_context(context, "season-crash-done"),
        )
        succeeded_member = Batch.model_validate(store.snapshot()["batches"][done["batchId"]]).episodes[0]
        running = Batch(
            id=str(uuid4()),
            project_id=PROJECT,
            season_id=season["seasonId"],
            season_version=1,
            episode_ids=(first, second),
            status="running",
            episodes=(
                succeeded_member,
                BatchEpisode(episode_id=second, episode_version=1, status="pending", shot_count=12),
            ),
            fingerprint="1" * 64,
            created_at="2020-01-02T00:00:00+00:00",
            updated_at="2020-01-02T00:00:00+00:00",
            revision=0,
        )
        store.transaction(
            lambda draft: draft["batches"].__setitem__(
                running.id, running.model_dump(mode="json", by_alias=True)
            )
        )
    with FileStore.open(home) as reopened:
        state = reopened.snapshot()
        recovered = Batch.model_validate(state["batches"][running.id])
    assert recovered.status == "partial"
    assert recovered.revision == 1
    members = {member.episode_id: member for member in recovered.episodes}
    assert members[first].status == "succeeded"
    assert members[second].status == "failed" and members[second].error_code == "BATCH_INTERRUPTED"


def test_terminal_batch_is_immutable(store, batch_world):
    registry, _service, _context = batch_world
    context = batch_run(store)
    episode = build_episode(registry, store, context, "第一集", "m7b-frozen")
    season = save_season(registry, store, [episode])
    done = ok(
        registry,
        store,
        "season_generate",
        {"seasonId": season["seasonId"], "confirm": True},
        next_context(context, "season-frozen"),
    )
    frozen = copy.deepcopy(store.snapshot()["batches"][done["batchId"]])

    def rewrite(draft):
        draft["batches"][done["batchId"]]["status"] = "partial"

    with pytest.raises(AppError) as error:
        store.transaction(rewrite)
    assert error.value.code == "BATCH_CONFLICT"
    assert store.snapshot()["batches"][done["batchId"]] == frozen

    def delete(draft):
        draft["batches"].pop(done["batchId"])

    with pytest.raises(AppError) as error:
        store.transaction(delete)
    assert error.value.code == "BATCH_CONFLICT"


def test_preview_reports_estimates_and_limits_without_writing(store, batch_world):
    registry, service, _context = batch_world
    from vagent.config import Config

    service.config = Config(home=store.home, api_key=None, video_mode="mock")
    context = batch_run(store)
    episodes = [
        build_episode(registry, store, context, f"第{index + 1}集", f"m7b-preview-{index}")
        for index in range(3)
    ]
    season = save_season(registry, store, episodes)
    before = store.snapshot()
    preview = service.preview_season(PROJECT, {"seasonId": season["seasonId"], "episodeIds": episodes})
    assert preview["totalNewJobs"] == 36
    assert preview["totalEstimate"] is None and preview["simulated"] is True
    assert preview["limits"]["maxNewJobs"] == 60
    assert preview["overLimit"] is False
    assert [item["shotCount"] for item in preview["episodes"]] == [12, 12, 12]
    assert [len(item["shots"]) for item in preview["episodes"]] == [12, 12, 12]
    assert all(shot["reused"] is False for shot in preview["episodes"][0]["shots"])
    after = store.snapshot()
    assert after["jobs"] == {} and after["batches"] == {}
    assert after["operations"] == before["operations"]


def test_a_later_shot_redo_only_adds_one_job_and_keeps_the_batch_record(store, batch_world):
    registry, _service, _context = batch_world
    context = batch_run(store)
    episodes = [
        build_episode(registry, store, context, f"第{index + 1}集", f"m7b-redo-{index}") for index in range(2)
    ]
    season = save_season(registry, store, episodes)
    done = ok(
        registry,
        store,
        "season_generate",
        {"seasonId": season["seasonId"], "confirm": True},
        next_context(context, "season-redo-batch"),
    )
    frozen = copy.deepcopy(store.snapshot()["batches"][done["batchId"]])
    read = ok(registry, store, "episode_read", {"episodeId": episodes[0]})
    board_id = read["scenes"][0]["storyboardId"]
    board = store.snapshot()["storyboards"][board_id]
    shot_id = board["versions"][-1]["shots"][0]["id"]
    redo = ok(
        registry,
        store,
        "shot_redo",
        {
            "storyboardId": board_id,
            "storyboardVersion": board["currentVersion"],
            "shotId": shot_id,
            "expectedRevision": board["revision"],
        },
        next_context(context, "season-redo-shot"),
    )
    assert redo["shots"][0]["shotId"] == shot_id and redo["shots"][0]["reused"] is False
    state = store.snapshot()
    assert len(state["jobs"]) == 25
    # The terminal batch records only registration facts; a later redo cannot rewrite it.
    assert state["batches"][done["batchId"]] == frozen
    assert state["storyboards"][board_id]["revision"] == board["revision"] + 1
