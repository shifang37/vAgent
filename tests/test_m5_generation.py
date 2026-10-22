"""M5 E3: episode batch registration, the episode bucket, redo and set waits.

Every provider call is a controlled ``httpx`` transport and every clip comes from the
local fixture, so the suite never reads a real Key, submits a paid task or calls the
public network. ``submit`` counts are asserted against registered Jobs, never guessed.
"""

import asyncio

import httpx
import pytest
from conftest import video_run
from media_support import SAMPLE
from shot_support import next_context
from wan_support import VIDEO_URL, live_service, provider_response

from vagent.errors import AppError
from vagent.storage import FileStore
from vagent.tools import create_project_tools
from vagent.video.contracts import parse_job
from vagent.video.jobs import JobService
from vagent.video.media_http import MediaHttpClient
from vagent.video.media_worker import MediaWorker
from vagent.video.providers.mock import MockVideoAdapter
from vagent.video.tools import job_settled, register_video_tools, resolve_jobset_wait
from vagent.video.worker import JobWorker
from vagent.waiting import ToolExecutionContext, WaitBindingV3

FRAMINGS = ("wide", "medium", "close", "over_shoulder")
DEFAULT_SCENES = ((4, 20), (4, 20), (4, 20))


def tool(registry, store, name, args, context, *, project_id="coffee"):
    return registry.execute(
        name,
        args,
        store=store,
        project_id=project_id,
        operation_key=context.operation_key,
        context=context,
    )


def ok(registry, store, name, args, context, *, project_id="coffee"):
    result = tool(registry, store, name, args, context, project_id=project_id)
    assert result["ok"], result
    return result["data"]


def generation_run(store, mode, *, run_id="e3-run", tools_version=9, read_only=False):
    """A Run recorded by one tool surface; E3 only takes the episode path at 9/9."""
    context = video_run(store, run_id, read_only=read_only)
    features = {
        "video": {
            "mode": mode,
            "toolsVersion": tools_version,
            "rulesVersion": tools_version,
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


def episode_spec(scenes, *, title="E3 分集", target=None):
    args = {
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
    }
    if target is not None:
        args["targetDurationSeconds"] = target
    return args


def build_episode(registry, store, context, *, scenes=DEFAULT_SCENES, target=None, key="e3-episode"):
    """Save the episode, then one storyboard per scene bound 1:1 to it."""
    saved = ok(registry, store, "episode_save", episode_spec(scenes, target=target), context)
    read = ok(registry, store, "episode_read", {}, next_context(context, key + "-read"))
    boards = []
    for index, (shots, seconds) in enumerate(scenes):
        board = ok(
            registry,
            store,
            "storyboard_save",
            {
                "title": f"第{index + 1}场分镜",
                "sceneId": read["scenes"][index]["sceneId"],
                "shots": board_shots(shots, seconds),
            },
            next_context(context, f"{key}-board-{index}"),
        )
        boards.append(board)
    return {"episodeId": saved["episodeId"], "scenes": read["scenes"], "boards": boards}


def generation_context(store, mode, *, run_id="e3-run"):
    return generation_run(store, mode, run_id=run_id)


def mock_registry(store, clock):
    store.ensure_session("coffee")
    adapter = MockVideoAdapter(store, clock=clock)
    service = JobService(store, [adapter], clock=clock)
    return register_video_tools(create_project_tools(), service), service, adapter


def live_registry(service):
    return register_video_tools(create_project_tools(), service)


class WanUpstream:
    """A controlled Wan upstream: one task per submission, scripted per task."""

    def __init__(self, failures=()):
        self.calls = []
        self.status = {}
        self.submits = 0
        self.failures = set(failures)

    def handler(self, request):
        self.calls.append(request)
        if request.method == "POST":
            self.submits += 1
            task_id = f"wan-{self.submits}"
            self.status[task_id] = "FAILED" if self.submits in self.failures else "SUCCEEDED"
            return httpx.Response(200, json=provider_response("PENDING", task_id=task_id))
        task_id = request.url.path.rsplit("/", 1)[-1]
        status = self.status.get(task_id, "PENDING")
        if status == "SUCCEEDED":
            return httpx.Response(
                200,
                json={
                    **provider_response("SUCCEEDED", task_id=task_id, video_url=VIDEO_URL),
                    "usage": {"duration": 5},
                },
            )
        return httpx.Response(200, json=provider_response(status, task_id=task_id))

    @property
    def submitted(self):
        return [call for call in self.calls if call.method == "POST"]


def clip_response(_request):
    return httpx.Response(200, content=SAMPLE.read_bytes(), headers={"Content-Type": "video/mp4"})


async def drain(service, clock, *, rounds=200):
    """Advance the frozen clock and drain both Workers until every Job is settled."""
    worker = JobWorker(service)
    async with MediaHttpClient(transport=httpx.MockTransport(clip_response)) as client:
        media = MediaWorker(service.media, client)
        for _ in range(rounds):
            jobs = [parse_job(raw) for raw in service.store.snapshot()["jobs"].values()]
            if jobs and all(job_settled(job) for job in jobs):
                break
            clock.advance(30)
            # One live call per frozen second is the Worker's shared throttle, so a
            # round makes at most one provider call; media downloads are not throttled.
            if await worker.run_once() is not None:
                await media.run_once()
    jobs = [parse_job(raw) for raw in service.store.snapshot()["jobs"].values()]
    assert jobs and all(job_settled(job) for job in jobs), [
        (job.id, job.status, job.query_state) for job in jobs
    ]
    return jobs


def set_wait(job_ids, *, deadline="2030-12-31T00:00:00+00:00"):
    return WaitBindingV3(
        id="e3-set-wait",
        context=ToolExecutionContext(
            project_id="coffee",
            session_id="coffee",
            run_id="e3-wait-run",
            model_step=1,
            tool_call_id="wait",
        ),
        resource={"kind": "jobset", "id": "e3-set-1"},
        jobIds=tuple(sorted(job_ids)),
        startedAt="2030-01-01T00:00:00+00:00",
        deadlineAt=deadline,
    )


def test_mock_episode_batch_registers_every_bound_shot_in_scene_order(store, video_clock):
    registry, service, adapter = mock_registry(store, video_clock)
    context = generation_context(store, "mock")
    episode = build_episode(registry, store, context)
    batch = next_context(context, "episode-batch")
    data = ok(
        registry, store, "episode_generate", {"episodeId": episode["episodeId"], "confirm": True}, batch
    )

    assert data["episodeId"] == episode["episodeId"]
    assert data["simulated"] is True and data["mediaAvailable"] is False
    assert data["totalEstimate"] is None and data["limits"] is None
    expected = [shot_id for board in episode["boards"] for shot_id in board["shotIds"]]
    assert [item["shotId"] for item in data["shots"]] == expected
    assert [item["sceneId"] for item in data["shots"]] == [
        scene["sceneId"] for scene in episode["scenes"] for _ in range(4)
    ]
    assert all(item["reused"] is False and item["jobId"] for item in data["shots"])
    state = store.snapshot()
    assert len(state["jobs"]) == 12 and all(job["mode"] == "mock" for job in state["jobs"].values())
    for board in episode["boards"]:
        record = state["storyboards"][board["storyboardId"]]
        assert record["revision"] == 1
        assert len(record["shotVersions"]) == 4
    assert adapter.ledger_snapshot()["submitCalls"] == 0

    # The same call replays; a new call reuses every frozen request without a Job.
    assert tool(
        registry, store, "episode_generate", {"episodeId": episode["episodeId"], "confirm": True}, batch
    ) == {
        "ok": True,
        "data": data,
    }
    again = ok(
        registry,
        store,
        "episode_generate",
        {"episodeId": episode["episodeId"], "confirm": True},
        next_context(context, "episode-batch-2"),
    )
    assert all(item["reused"] is True for item in again["shots"])
    assert [item["jobId"] for item in again["shots"]] == [item["jobId"] for item in data["shots"]]
    assert len(store.snapshot()["jobs"]) == 12
    assert all(record["revision"] == 1 for record in store.snapshot()["storyboards"].values())

    read = ok(registry, store, "episode_read", {}, next_context(context, "episode-read"))
    generation = read["generation"]
    assert generation["ready"] is True and generation["problems"] == []
    assert generation["counts"] == {
        "planned": 12,
        "registered": 12,
        "delivered": 0,
        "failed": 0,
        "pending": 12,
        "unregistered": 0,
    }
    assert [shot["shotId"] for shot in generation["shots"]] == expected
    assert all(shot["status"] == "pending" for shot in generation["shots"])


def test_episode_batch_requires_confirmation_and_rejects_an_incomplete_plan(store, video_clock):
    registry, _service, _adapter = mock_registry(store, video_clock)
    context = generation_context(store, "mock")
    episode = build_episode(registry, store, context)

    unconfirmed = tool(
        registry,
        store,
        "episode_generate",
        {"episodeId": episode["episodeId"], "confirm": False},
        next_context(context, "unconfirmed"),
    )
    assert unconfirmed["ok"] is False and unconfirmed["error"]["code"] == "CONFIRMATION_REQUIRED"
    assert store.snapshot()["jobs"] == {}

    # A scene without a storyboard rejects the whole batch.
    unbound = ok(
        registry,
        store,
        "episode_save",
        episode_spec(DEFAULT_SCENES, title="未绑定的分集"),
        next_context(context, "unbound-save"),
    )
    result = tool(
        registry,
        store,
        "episode_generate",
        {"episodeId": unbound["episodeId"], "confirm": True},
        next_context(context, "unbound"),
    )
    assert result["ok"] is False and result["error"]["code"] == "SCENE_CONFLICT"
    assert "还没有绑定分镜" in result["error"]["message"]
    assert store.snapshot()["jobs"] == {}

    # A bound storyboard must match the frozen scene: first its shot count, then its
    # duration. Both reject the batch before any Job or ShotVersion is written.
    first_board = episode["boards"][0]
    wrong_count = ok(
        registry,
        store,
        "storyboard_save",
        {
            "storyboardId": first_board["storyboardId"],
            "expectedVersion": first_board["version"],
            "title": "缺一个镜头",
            "shots": board_shots(3, 20),
        },
        next_context(context, "wrong-count"),
    )
    count_result = tool(
        registry,
        store,
        "episode_generate",
        {"episodeId": episode["episodeId"], "confirm": True},
        next_context(context, "wrong-count-batch"),
    )
    assert count_result["ok"] is False and count_result["error"]["code"] == "EPISODE_SHOT_BUDGET"
    assert len(wrong_count["shotIds"]) == 3 and store.snapshot()["jobs"] == {}

    wrong_length = ok(
        registry,
        store,
        "storyboard_save",
        {
            "storyboardId": wrong_count["storyboardId"],
            "expectedVersion": wrong_count["version"],
            "title": "少四秒",
            "shots": board_shots(4, 16),
        },
        next_context(context, "wrong-length"),
    )
    length_result = tool(
        registry,
        store,
        "episode_generate",
        {"episodeId": episode["episodeId"], "confirm": True},
        next_context(context, "wrong-length-batch"),
    )
    assert length_result["ok"] is False and length_result["error"]["code"] == "EPISODE_DURATION_LIMIT"
    assert wrong_length["version"] == 3 and store.snapshot()["jobs"] == {}

    read = ok(
        registry, store, "episode_read", {"episodeId": episode["episodeId"]}, next_context(context, "read")
    )
    assert read["generation"]["ready"] is False
    assert read["generation"]["problems"]
    assert read["generation"]["counts"]["registered"] == 0


def test_read_only_and_foreign_episodes_are_refused(store, video_clock):
    registry, _service, _adapter = mock_registry(store, video_clock)
    context = generation_context(store, "mock")
    episode = build_episode(registry, store, context)
    read_only = generation_run(store, "mock", run_id="e3-read-only", read_only=True)
    refused = tool(
        registry,
        store,
        "episode_generate",
        {"episodeId": episode["episodeId"], "confirm": True},
        next_context(read_only, "read-only"),
    )
    assert refused["ok"] is False and refused["error"]["code"] == "READ_ONLY"

    missing = tool(
        registry,
        store,
        "episode_generate",
        {"episodeId": "0f0f0f0f-0000-4000-8000-000000000000", "confirm": True},
        next_context(context, "missing"),
    )
    assert missing["ok"] is False and missing["error"]["code"] == "EPISODE_NOT_FOUND"
    assert store.snapshot()["jobs"] == {}


async def test_live_episode_batch_freezes_the_episode_bucket(store, video_clock):
    upstream = WanUpstream()
    async with live_service(store, video_clock, upstream.handler) as (service, _provider):
        registry = live_registry(service)
        context = generation_context(store, "live")
        episode = build_episode(registry, store, context)
        board = episode["boards"][0]
        preview = service.preview_shots(
            "coffee",
            {
                "storyboardId": board["storyboardId"],
                "storyboardVersion": board["version"],
                "shotIds": board["shotIds"],
                "confirm": False,
            },
        )
        assert preview["limits"]["scope"] == "episode"
        assert preview["limits"]["usedNewJobs"] == 4
        assert preview["limits"]["usedAmount"] == "12.00"
        data = ok(
            registry,
            store,
            "episode_generate",
            {"episodeId": episode["episodeId"], "confirm": True},
            next_context(context, "live-batch"),
        )
        assert data["simulated"] is False and data["mediaAvailable"] is False
        assert data["totalEstimate"] == {"currency": "CNY", "amount": "36.00"}
        assert data["limits"] == {
            "maxJobCost": "3.00",
            "maxNewJobsPerRun": 20,
            "maxRunCost": "60.00",
            "usedNewJobs": 12,
            "usedAmount": "36.00",
            "remainingAmount": "24.00",
            "scope": "episode",
            "episodeId": episode["episodeId"],
        }
        assert [item["estimate"]["amount"] for item in data["shots"]] == ["3.00"] * 12
        assert len(store.snapshot()["jobs"]) == 12
        assert all(job["status"] == "pending_submit" for job in store.snapshot()["jobs"].values())
        assert upstream.submitted == []  # registration never submits

        replay_preview = service.preview_shots(
            "coffee",
            {
                "storyboardId": board["storyboardId"],
                "storyboardVersion": board["version"],
                "shotIds": board["shotIds"],
                "confirm": False,
            },
        )
        assert replay_preview["limits"]["usedNewJobs"] == 12
        assert all(row["reused"] for row in replay_preview["shots"])

        generation = ok(registry, store, "episode_read", {}, next_context(context, "live-read"))["generation"]
        assert generation["budget"] == {
            "jobCap": 20,
            "usedJobs": 12,
            "remainingJobs": 8,
            "currency": "CNY",
            "perShotAmount": "3.00",
            "episodeCapAmount": "60.00",
            "usedAmount": "36.00",
            "remainingAmount": "24.00",
            "priceVersion": "wan27-beijing-720p-20261010",
            "shotCount": 12,
            "targetDurationSeconds": 60,
            "foreignCurrencyJobs": [],
        }


async def test_the_episode_job_cap_rejects_a_redo_across_runs(store, video_clock):
    upstream = WanUpstream()
    scenes = ((4, 16), (4, 16), (4, 16), (4, 16), (4, 16))
    async with live_service(store, video_clock, upstream.handler) as (service, _provider):
        registry = live_registry(service)
        context = generation_context(store, "live")
        episode = build_episode(registry, store, context, scenes=scenes, target=80)
        batch = ok(
            registry,
            store,
            "episode_generate",
            {"episodeId": episode["episodeId"], "confirm": True},
            next_context(context, "full-batch"),
        )
        assert len(batch["shots"]) == 20
        assert batch["limits"]["usedNewJobs"] == 20
        assert batch["limits"]["usedAmount"] == "48.00"
        board = episode["boards"][0]
        revision = store.snapshot()["storyboards"][board["storyboardId"]]["revision"]

        # A redo in a different Run still spends the same episode bucket.
        other = generation_context(store, "live", run_id="e3-redo-run")
        rejected = tool(
            registry,
            store,
            "shot_redo",
            {
                "storyboardId": board["storyboardId"],
                "storyboardVersion": board["version"],
                "shotId": board["shotIds"][0],
                "expectedRevision": revision,
            },
            next_context(other, "redo"),
        )
        assert rejected["ok"] is False and rejected["error"]["code"] == "EPISODE_COST_LIMIT"
        assert "jobCap" in rejected["error"]["message"]
        state = store.snapshot()
        assert len(state["jobs"]) == 20
        assert state["storyboards"][board["storyboardId"]]["revision"] == revision
        assert upstream.submitted == []

        # Replaying the whole batch finds every request already frozen: no new Job.
        replay = ok(
            registry,
            store,
            "episode_generate",
            {"episodeId": episode["episodeId"], "confirm": True},
            next_context(other, "replay"),
        )
        assert all(item["reused"] is True for item in replay["shots"])
        assert len(store.snapshot()["jobs"]) == 20


async def test_the_episode_budget_counts_a_storyboard_batch(store, video_clock):
    upstream = WanUpstream()
    async with live_service(store, video_clock, upstream.handler, video_max_job_cost="15.00") as (
        service,
        _provider,
    ):
        registry = live_registry(service)
        context = generation_context(store, "live")
        episode = build_episode(registry, store, context)
        ok(
            registry,
            store,
            "episode_generate",
            {"episodeId": episode["episodeId"], "confirm": True},
            next_context(context, "budget-batch"),
        )
        # Re-version one bound storyboard with six long shots, then register them: the
        # 9/9 surface counts this storyboard batch against the same episode bucket.
        board = episode["boards"][0]
        revised = ok(
            registry,
            store,
            "storyboard_save",
            {
                "storyboardId": board["storyboardId"],
                "expectedVersion": board["version"],
                "title": "拉长后的第一场",
                "shots": board_shots(6, 90),
            },
            next_context(context, "long-board"),
        )
        over = tool(
            registry,
            store,
            "shot_generate",
            {
                "storyboardId": revised["storyboardId"],
                "storyboardVersion": revised["version"],
                "shotIds": revised["shotIds"],
                "confirm": True,
            },
            next_context(context, "long-batch"),
        )
        assert over["ok"] is False and over["error"]["code"] == "EPISODE_COST_LIMIT"
        assert "集级成本额度不足" in over["error"]["message"]
        assert len(store.snapshot()["jobs"]) == 12
        assert upstream.submitted == []


async def test_a_redo_only_changes_the_target_shot(store, video_clock):
    upstream = WanUpstream()
    async with live_service(store, video_clock, upstream.handler) as (service, _provider):
        registry = live_registry(service)
        context = generation_context(store, "live")
        episode = build_episode(registry, store, context)
        ok(
            registry,
            store,
            "episode_generate",
            {"episodeId": episode["episodeId"], "confirm": True},
            next_context(context, "redo-batch"),
        )
        target = episode["boards"][1]
        state = store.snapshot()
        revisions = {item["id"]: item["revision"] for item in state["storyboards"].values()}
        versions = {
            (board_id, item["shotId"]): item["id"]
            for board_id, record in state["storyboards"].items()
            for item in record["shotVersions"]
        }
        jobs_before = {raw["id"]: raw for raw in state["jobs"].values()}

        redone = ok(
            registry,
            store,
            "shot_redo",
            {
                "storyboardId": target["storyboardId"],
                "storyboardVersion": target["version"],
                "shotId": target["shotIds"][1],
                "expectedRevision": revisions[target["storyboardId"]],
            },
            next_context(context, "redo"),
        )
        assert len(redone["shots"]) == 1
        entry = redone["shots"][0]
        assert entry["shotId"] == target["shotIds"][1] and entry["reused"] is False
        assert redone["limits"]["scope"] == "episode" and redone["limits"]["usedNewJobs"] == 13
        assert redone["limits"]["usedAmount"] == "39.00"

        after = store.snapshot()
        assert (
            after["storyboards"][target["storyboardId"]]["revision"] == revisions[target["storyboardId"]] + 1
        )
        for board_id, revision in revisions.items():
            if board_id != target["storyboardId"]:
                assert after["storyboards"][board_id]["revision"] == revision
        for board_id, record in after["storyboards"].items():
            for item in record["shotVersions"]:
                key = (board_id, item["shotId"])
                if (
                    key == (target["storyboardId"], target["shotIds"][1])
                    and item["id"] == entry["shotVersionId"]
                ):
                    continue
                assert versions[key] == item["id"]
        assert len(after["jobs"]) == 13
        assert all(
            raw == jobs_before[raw["id"]] for raw in after["jobs"].values() if raw["id"] in jobs_before
        )
        assert upstream.submitted == []

        # A stale revision read is refused before anything is written.
        stale = tool(
            registry,
            store,
            "shot_redo",
            {
                "storyboardId": target["storyboardId"],
                "storyboardVersion": target["version"],
                "shotId": target["shotIds"][1],
                "expectedRevision": revisions[target["storyboardId"]],
            },
            next_context(context, "stale-redo"),
        )
        assert stale["ok"] is False and stale["error"]["code"] == "VERSION_CONFLICT"
        assert len(store.snapshot()["jobs"]) == 13


async def test_the_set_wait_covers_an_episode_and_isolates_a_failed_shot(store, video_clock):
    upstream = WanUpstream(failures={1})
    async with live_service(store, video_clock, upstream.handler) as (service, _provider):
        registry = live_registry(service)
        context = generation_context(store, "live")
        episode = build_episode(registry, store, context)
        batch = ok(
            registry,
            store,
            "episode_generate",
            {"episodeId": episode["episodeId"], "confirm": True},
            next_context(context, "wait-batch"),
        )
        job_ids = [item["jobId"] for item in batch["shots"]]
        binding = set_wait(job_ids)
        assert resolve_jobset_wait(store, binding, service=service) is None
        jobs = await drain(service, video_clock)
        assert len(jobs) == 12
        settled = resolve_jobset_wait(store, binding, service=service)
        summary = settled["data"]
        assert summary["failed"] == 1 and summary["succeeded"] == 11
        failed = next(item for item in summary["jobs"] if not item["ok"])
        assert failed["error"]["code"] == "JOB_FAILED"
        assert all(item["mediaAvailable"] for item in summary["jobs"] if item["ok"])
        # One collection wait via the tool returns the same per-shot evidence.
        waited = ok(
            registry,
            store,
            "await_job",
            {"jobIds": job_ids},
            next_context(context, "wait-call"),
        )
        assert waited["failed"] == 1 and waited["succeeded"] == 11
        assert len(upstream.submitted) == 12
        assert all(parse_job(raw).submit_attempts == 1 for raw in store.snapshot()["jobs"].values())

        read = ok(registry, store, "episode_read", {}, next_context(context, "wait-read"))["generation"]
        assert read["counts"]["delivered"] == 11 and read["counts"]["failed"] == 1
        assert read["budget"]["usedJobs"] == 12 and read["budget"]["usedAmount"] == "36.00"
        assert read["budget"]["remainingJobs"] == 8

        # The failure is isolated: redoing only that shot registers one new Job.
        failed_shot = next(item for item in read["shots"] if item["status"] == "failed")
        board_id = failed_shot["storyboardId"]
        revision = store.snapshot()["storyboards"][board_id]["revision"]
        redone = ok(
            registry,
            store,
            "shot_redo",
            {
                "storyboardId": board_id,
                "storyboardVersion": failed_shot["storyboardVersion"],
                "shotId": failed_shot["shotId"],
                "expectedRevision": revision,
            },
            next_context(context, "redo-failed"),
        )
        assert len(redone["shots"]) == 1
        await drain(service, video_clock)
        assert len(upstream.submitted) == 13
        repaired = ok(registry, store, "episode_read", {}, next_context(context, "repair-read"))["generation"]
        assert repaired["counts"]["delivered"] == 12 and repaired["counts"]["failed"] == 0
        assert repaired["budget"]["usedJobs"] == 13 and repaired["budget"]["usedAmount"] == "39.00"


async def test_episode_scope_snapshots_survive_a_reopen(store, video_clock):
    upstream = WanUpstream()
    home = store.home
    async with live_service(store, video_clock, upstream.handler) as (service, _provider):
        registry = live_registry(service)
        context = generation_context(store, "live")
        episode = build_episode(registry, store, context)
        ok(
            registry,
            store,
            "episode_generate",
            {"episodeId": episode["episodeId"], "confirm": True},
            next_context(context, "reopen-batch"),
        )
    store.close()
    with FileStore.open(home) as reopened:
        raw = next(iter(reopened.snapshot()["jobs"].values()))
        assert raw["cost"]["limits"]["scope"] == "episode"
        assert raw["cost"]["limits"]["episodeId"] == episode["episodeId"]
        assert raw["cost"]["limits"]["maxRunCost"] == "60.00"


def test_tampered_episode_snapshot_is_rejected_on_commit(store, video_clock):
    upstream = WanUpstream()

    async def scenario():
        async with live_service(store, video_clock, upstream.handler) as (service, _provider):
            registry = live_registry(service)
            context = generation_context(store, "live")
            episode = build_episode(registry, store, context)
            ok(
                registry,
                store,
                "episode_generate",
                {"episodeId": episode["episodeId"], "confirm": True},
                next_context(context, "tamper-batch"),
            )

    asyncio.run(scenario())
    job_id = next(iter(store.snapshot()["jobs"]))
    before = store.snapshot()

    def widen(draft):
        # Internally consistent, but no longer the episode's frozen jobCap.
        job = draft["jobs"][job_id]
        job["cost"]["limits"]["maxNewJobsPerRun"] = 19
        job["revision"] += 1

    with pytest.raises(AppError) as error:
        store.transaction(widen)
    assert error.value.code == "JOB_CONFLICT"
    assert store.snapshot() == before


def test_a_whole_episode_fits_one_collection_wait():
    members = [f"0f0f0f0f-0000-4000-8000-{index:012d}" for index in range(20)]
    binding = set_wait(members)
    assert len(binding.job_ids) == 20
    with pytest.raises(ValueError):
        set_wait(members + ["0f0f0f0f-0000-4000-8000-999999999999"])


def test_episode_generation_keeps_the_frozen_episode_record(store, video_clock):
    registry, _service, _adapter = mock_registry(store, video_clock)
    context = generation_context(store, "mock")
    episode = build_episode(registry, store, context)
    before = store.snapshot()["episodes"]
    ok(
        registry,
        store,
        "episode_generate",
        {"episodeId": episode["episodeId"], "confirm": True},
        next_context(context, "record-batch"),
    )
    assert store.snapshot()["episodes"] == before
