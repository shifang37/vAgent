"""M7 E4 offline acceptance: season batches, isolation/resume and timeline reassembly.

Default mode is fully offline. The real Store, season/batch/timeline/audio tools, the
controlled Wan transport, Job/Media Workers, the render service and the bundled FFmpeg
run through production code paths. Clip submissions go to ``httpx.MockTransport`` and
every delivered clip is the committed local MP4 fixture; BGM is imported through the
client audio-import path; voice comes from ``local-tts-v1``.

``--live`` is deliberately unavailable: a new paid Wan/CosyVoice sample needs an
explicit model/count/amount approval and is recorded separately from this suite.
"""

import argparse
import asyncio
import hashlib
import io
import json
import shutil
import time
import traceback
import wave
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import httpx

from vagent.config import Config
from vagent.errors import AppError
from vagent.storage import FileStore, RunRecordV1, atomic_write_json, now
from vagent.tools import create_project_tools
from vagent.video.adaptation import plan_episode
from vagent.video.assets import asset_reference_loader
from vagent.video.audio_import import import_audio
from vagent.video.contracts import ImageInputRule, wan_capabilities
from vagent.video.films import RENDER_TIMELINE_ASSEMBLY, Film
from vagent.video.jobs import JobService
from vagent.video.media_http import MediaHttpClient
from vagent.video.media_worker import MediaWorker
from vagent.video.providers.mock import MockVideoAdapter
from vagent.video.providers.wan import WanAdapter
from vagent.video.renders import FilmRenderArgs, rendered_cues
from vagent.video.storyboards import ShotVersion, Storyboard
from vagent.video.tools import VIDEO_RULES_VERSION, VIDEO_TOOLS_VERSION, register_video_tools
from vagent.video.worker import JobWorker
from vagent.waiting import ToolExecutionContext

ROOT = Path(__file__).resolve().parent.parent
FIXTURE = ROOT / "tests" / "fixtures" / "m1c" / "sample-720p.mp4"
TEXTS = ROOT / "tests" / "fixtures" / "m5" / "texts"
WAN_RESULT_HOST = "dashscope-result-sh.oss-accelerate.aliyuncs.com"
PROJECT = "m7-e4"
VIDEO_URL = f"https://{WAN_RESULT_HOST}/m7-e4.mp4?Signature=offline"
TIMESTAMP = "2030-01-01T00:00:00+00:00"

# Batch episodes use four shots of five seconds per scene, matching the local clip.
BATCH_SCENES = ((4, 20), (4, 20), (4, 20))
# Episode shots are written as five-second planned shots; the first six scenes keep
# 14 shots (~67 s), the planned dialogue and a 60–90 s episode duration.
TIMELINE_SCENES = 6


@dataclass(frozen=True)
class Case:
    """One offline acceptance scenario."""

    name: str
    kind: str  # batch | failure | workbench
    text: str
    title: str


CASES = (
    Case("season-batch", "batch", "rainy-cafe.md", "雨夜咖啡店"),
    Case("season-failure", "failure", "rainy-cafe.md", "雨夜咖啡店"),
    Case("timeline-reassemble", "workbench", "rainy-cafe.md", "雨夜咖啡店"),
)
CASES_BY_NAME = {case.name: case for case in CASES}


def fixture_bytes() -> bytes:
    return FIXTURE.read_bytes()


def case_text(case: Case) -> str:
    return (TEXTS / case.text).read_text(encoding="utf-8")


def suite_identity(cases) -> str:
    return hashlib.sha256(
        json.dumps(
            {
                "version": 1,
                "cases": [vars(case) for case in cases],
                "texts": {
                    case.text: hashlib.sha256((TEXTS / case.text).read_bytes()).hexdigest() for case in cases
                },
                "fixtureSha256": hashlib.sha256(fixture_bytes()).hexdigest(),
            },
            sort_keys=True,
            ensure_ascii=False,
        ).encode()
    ).hexdigest()[:16]


class SuiteClock:
    """A deterministic clock shared by the controlled provider and both workers."""

    def __init__(self):
        self.value = datetime(2030, 1, 1, tzinfo=UTC)

    def __call__(self):
        return self.value

    def advance(self, seconds=2):
        self.value += timedelta(seconds=seconds)


class Ledger:
    """Observable facts of the controlled transport; no real connection is made."""

    def __init__(self):
        self.submit_calls = 0
        self.query_calls = 0
        self.download_calls = 0

    def view(self):
        return {
            "submitCalls": self.submit_calls,
            "queryCalls": self.query_calls,
            "downloadCalls": self.download_calls,
        }


def provider_response(status="PENDING", *, task_id="m7-e4-task", request_id="m7-e4-trace", **output):
    return {"request_id": request_id, "output": {"task_id": task_id, "task_status": status, **output}}


def wan_arguments(prompt):
    capability = wan_capabilities()
    return {
        "provider": capability.provider,
        "model": capability.model,
        "capabilitiesVersion": capability.capabilities_version,
        "prompt": prompt,
        "spec": {"durationSeconds": 5, "resolution": "720p", "aspectRatio": "16:9"},
    }


def mode_config(home, mode):
    return Config(
        home=home,
        api_key=None,
        video_mode=mode,
        video_api_key="offline-video-secret" if mode == "live" else None,
        video_workspace_id="offline-workspace" if mode == "live" else None,
        kling_api_key=None,
        fal_api_key=None,
        redis_url=None,
        skills_root=None,
        mcp_config=None,
        mcp_local=False,
    )


def create_run(store, run_id, *, mode="mock"):
    """A terminal Run whose frozen video mode admits registration and local audio."""
    record = RunRecordV1(
        id=run_id,
        session_id=PROJECT,
        request_id=run_id,
        prompt="offline M7 E4 controlled delivery",
        model="no-model",
        status="completed",
        messages=[],
        model_steps=0,
        tool_calls=0,
        input_tokens=0,
        output_tokens=0,
        answer="",
        created_at=TIMESTAMP,
        updated_at=TIMESTAMP,
    )

    def add(draft):
        draft["runs"][run_id] = record.model_dump(by_alias=True)
        draft["sessions"][PROJECT]["latestRunId"] = run_id
        draft["runs"][run_id].update(
            videoMode=mode,
            executionVersion=3,
            toolFeatures={
                "video": {
                    "mode": mode,
                    "toolsVersion": VIDEO_TOOLS_VERSION,
                    "rulesVersion": VIDEO_RULES_VERSION,
                    "capabilities": [],
                }
            },
        )

    store.transaction(add)
    return ToolExecutionContext(
        project_id=PROJECT,
        session_id=PROJECT,
        run_id=run_id,
        model_step=1,
        tool_call_id="generate",
    )


def next_context(context, call_id, **changes):
    return ToolExecutionContext.model_validate(
        {**context.model_dump(by_alias=True), "toolCallId": call_id, **changes}
    )


def tool(registry, store, name, args, *, context=None, key=None):
    return registry.execute(
        name,
        args,
        store=store,
        project_id=PROJECT,
        operation_key=key or (context.operation_key if context is not None else f"m7e4-{uuid4()}"),
        context=context,
    )


def ok(registry, store, name, args, *, context=None, key=None):
    result = tool(registry, store, name, args, context=context, key=key)
    if not result.get("ok"):
        raise AppError(
            result.get("error", {}).get("code", "EVAL_TOOL_FAILED"),
            json.dumps(result.get("error", result), ensure_ascii=False)[:500],
        )
    return result["data"]


def mock_registry(store, clock):
    config = mode_config(store.home, "mock")
    service = JobService(store, [MockVideoAdapter(store, clock=clock)], clock=clock, config=config)
    return register_video_tools(create_project_tools(), service, mode="mock"), service


def scene_durations(scene):
    count = scene["shotCount"]
    base, extra = divmod(scene["durationSeconds"], count)
    return [base + (1 if number < extra else 0) for number in range(count)]


def board_shots(scene, index):
    return [
        {
            "description": f"{scene['heading']} 第 {number} 镜",
            "framing": "wide",
            "cameraMovement": "static",
            "durationSeconds": duration,
            "mode": "t2v",
        }
        for number, duration in enumerate(scene_durations(scene), start=1)
    ]


def batch_scenes():
    return [
        {
            "heading": f"第{index + 1}场：雨夜咖啡店",
            "synopsis": f"第{index + 1}场的概要。",
            "timeOfDay": "night",
            "shotCount": shots,
            "durationSeconds": seconds,
        }
        for index, (shots, seconds) in enumerate(BATCH_SCENES)
    ]


def scene_args(scene):
    """The saved episode scene: only fields the episode contract accepts."""
    return {
        "heading": scene["heading"],
        "location": scene.get("location"),
        "timeOfDay": scene.get("timeOfDay") or "unknown",
        "synopsis": scene["synopsis"],
        "characterIds": [],
        "dialogue": [
            {"text": line["text"], "speaker": line.get("speaker"), "paragraph": line.get("paragraph", 0)}
            for line in scene.get("dialogue", [])
        ],
        "shotCount": scene["shotCount"],
        "durationSeconds": scene["durationSeconds"],
    }


def build_episode(registry, store, title, key, *, scenes=None, bound=True):
    """Save one episode; optionally bind a planned storyboard to every scene."""
    items = [scene_args(scene) for scene in scenes or batch_scenes()]
    saved = ok(
        registry,
        store,
        "episode_save",
        {
            "title": title,
            "targetDurationSeconds": sum(item["durationSeconds"] for item in items),
            "scenes": items,
        },
        key=f"{key}-episode",
    )
    read = ok(registry, store, "episode_read", {"episodeId": saved["episodeId"]}, key=f"{key}-read")
    boards = []
    if bound:
        for index, scene in enumerate(read["scenes"]):
            board = ok(
                registry,
                store,
                "storyboard_save",
                {
                    "title": f"第{index + 1}场分镜",
                    "sceneId": scene["sceneId"],
                    "shots": board_shots(scene, index),
                },
                key=f"{key}-board-{index}",
            )
            boards.append(board)
    return {"episode": saved, "scenes": read["scenes"], "boards": boards}


def save_season(registry, store, episode_ids, *, title="雨夜咖啡店 第一季", key="m7e4-season"):
    return ok(
        registry,
        store,
        "season_save",
        {"title": title, "episodeIds": list(episode_ids)},
        key=key,
    )


# --- controlled delivery of the committed local clip -------------------------
@asynccontextmanager
async def live_service(store, clock, ledger):
    data = fixture_bytes()

    def upstream(request):
        if request.method == "POST":
            ledger.submit_calls += 1
            return httpx.Response(
                200,
                json=provider_response("PENDING", task_id="m7-e4-task", request_id="m7-e4-submit"),
            )
        ledger.query_calls += 1
        return httpx.Response(
            200,
            json=provider_response(
                "SUCCEEDED",
                task_id="m7-e4-task",
                request_id="m7-e4-query",
                video_url=VIDEO_URL,
            ),
        )

    def media(_request):
        ledger.download_calls += 1
        return httpx.Response(
            200,
            content=data,
            headers={"Content-Type": "video/mp4", "Content-Length": str(len(data))},
        )

    config = mode_config(store.home, "live")
    async with WanAdapter(
        api_key=config.video_api_key,
        workspace_id=config.video_workspace_id,
        transport=httpx.MockTransport(upstream),
        clock=clock,
        reference_loader=asset_reference_loader(store, ImageInputRule()),
    ) as provider:
        service = JobService(
            store, [MockVideoAdapter(store, clock=clock), provider], clock=clock, config=config
        )
        yield service, provider, media


async def deliver_clip(store, clock, ledger, *, index, prompt):
    clock.advance(5)
    run_id = f"m7-e4-clip-{index}"
    context = create_run(store, run_id, mode="live")
    async with live_service(store, clock, ledger) as (service, _provider, media_handler):
        service.generate(wan_arguments(prompt), context=context)
        worker = JobWorker(service)
        job = None
        for _ in range(8):
            job = await worker.run_once()
            if job is not None and job.status in {"downloading", "succeeded"}:
                break
            clock.advance(10)
        async with MediaHttpClient(transport=httpx.MockTransport(media_handler)) as client:
            delivered = await MediaWorker(service.media, client).run_once()
    if delivered is None or delivered.status != "succeeded":
        raise AppError("EVAL_DELIVERY_FAILED", f"受控镜头 {index} 未交付。")
    return delivered


def link_shot_versions(store, *, storyboard_id, storyboard_version, entries):
    def link(draft):
        record = Storyboard.model_validate(draft["storyboards"][storyboard_id])
        created = [
            ShotVersion(
                id=str(uuid4()),
                shot_id=shot_id,
                storyboard_version=storyboard_version,
                job_id=job_id,
                request=request,
                media_id=media_id,
                created_at=TIMESTAMP,
            )
            for shot_id, job_id, media_id, request in entries
        ]
        updated = record.model_copy(
            update={"shot_versions": (*record.shot_versions, *created), "revision": record.revision + 1}
        )
        draft["storyboards"][storyboard_id] = updated.model_dump(mode="json", by_alias=True)

    store.transaction(link)
    return store.snapshot()["storyboards"][storyboard_id]["revision"]


def wav_bytes(seconds=1.0, sample_rate=48000):
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(sample_rate)
        handle.writeframes(b"\x00\x00" * int(sample_rate * seconds))
    return buffer.getvalue()


# --- cases -------------------------------------------------------------------
async def run_season_batch(case, base):
    store, clock, ledger = base["store"], base["clock"], base["ledger"]
    registry, service = mock_registry(store, clock)
    context = create_run(store, "m7-e4-batch-run")
    episodes = [
        build_episode(registry, store, f"第{index + 1}集", f"m7e4-batch-{index}")["episode"]
        for index in range(3)
    ]
    episode_ids = [item["episodeId"] for item in episodes]
    season = save_season(registry, store, episode_ids)
    preview = service.preview_season(PROJECT, {"seasonId": season["seasonId"], "episodeIds": episode_ids})
    call = next_context(context, "season-batch")
    data = ok(
        registry,
        store,
        "season_generate",
        {"seasonId": season["seasonId"], "confirm": True, "episodeIds": episode_ids},
        context=call,
    )
    replay = ok(
        registry,
        store,
        "season_generate",
        {"seasonId": season["seasonId"], "confirm": True, "episodeIds": episode_ids},
        context=call,
    )
    read = ok(registry, store, "season_read", {"seasonId": season["seasonId"]})
    state = store.snapshot()
    batch = state["batches"][data["batchId"]]
    checks = {
        "previewReportsThreeEpisodes": preview["totalNewJobs"] == 36
        and [item["shotCount"] for item in preview["episodes"]] == [12, 12, 12]
        and preview["overLimit"] is False,
        "batchSucceeded": data["status"] == "succeeded",
        "threeMembersInOrder": data["episodeIds"] == episode_ids and len(data["episodes"]) == 3,
        "perEpisodeTwelveJobs": [item["jobCount"] for item in data["episodes"]] == [12, 12, 12]
        and [item["shotCount"] for item in data["episodes"]] == [12, 12, 12],
        "thirtySixJobs": len(state["jobs"]) == 36,
        "mockOnlyJobs": all(raw["mode"] == "mock" for raw in state["jobs"].values()),
        "noProviderSubmits": ledger.submit_calls == 0,
        "replayReturnsTheSameBatch": replay["batchId"] == data["batchId"]
        and replay["replayed"] is True
        and len(store.snapshot()["jobs"]) == 36,
        "batchRevisionIsSix": batch["status"] == "succeeded" and batch["revision"] == 6,
        "singleBatchRecord": len(state["batches"]) == 1,
        "readEchoesRegistration": read["batch"]["batchId"] == data["batchId"]
        and [item["pendingShots"] for item in read["batch"]["episodes"]] == [12, 12, 12]
        and [item["deliveredShots"] for item in read["batch"]["episodes"]] == [0, 0, 0],
    }
    return {
        "case": case.name,
        "checks": checks,
        "seasonId": season["seasonId"],
        "batch": {
            "batchId": data["batchId"],
            "status": data["status"],
            "episodeIds": data["episodeIds"],
            "jobCounts": [item["jobCount"] for item in data["episodes"]],
            "revision": batch["revision"],
            "replayed": replay["replayed"],
        },
        "mockJobs": len(state["jobs"]),
        "batchCount": len(state["batches"]),
        "controlledSubmits": ledger.submit_calls,
    }


async def run_season_failure(case, base):
    store, clock, ledger = base["store"], base["clock"], base["ledger"]
    registry, _service = mock_registry(store, clock)
    context = create_run(store, "m7-e4-failure-run")
    first = build_episode(registry, store, "第一集", "m7e4-failure-0")
    broken = build_episode(registry, store, "第二集", "m7e4-failure-1", bound=False)
    third = build_episode(registry, store, "第三集", "m7e4-failure-2")
    episode_ids = [
        first["episode"]["episodeId"],
        broken["episode"]["episodeId"],
        third["episode"]["episodeId"],
    ]
    season = save_season(registry, store, episode_ids)
    original = ok(
        registry,
        store,
        "season_generate",
        {"seasonId": season["seasonId"], "confirm": True},
        context=next_context(context, "season-failure-first"),
    )
    frozen = json.loads(json.dumps(store.snapshot()["batches"][original["batchId"]]))
    by_episode = {item["episodeId"]: item for item in original["episodes"]}
    jobs_after_first = len(store.snapshot()["jobs"])

    # Fix only the failed member: EpisodeVersion 2 plus its planned storyboards.
    frozen_episode = store.snapshot()["episodes"][broken["episode"]["episodeId"]]
    updated = ok(
        registry,
        store,
        "episode_save",
        {
            "episodeId": frozen_episode["id"],
            "expectedRevision": frozen_episode["revision"],
            "title": "第二集",
            "scenes": batch_scenes(),
        },
        key="m7e4-failure-episode-v2",
    )
    for index, scene_id in enumerate(updated["sceneIds"]):
        ok(
            registry,
            store,
            "storyboard_save",
            {
                "title": f"第{index + 1}场分镜",
                "sceneId": scene_id,
                "shots": board_shots(batch_scenes()[index], index),
            },
            key=f"m7e4-failure-fix-{index}",
        )

    resumed = ok(
        registry,
        store,
        "season_generate",
        {"seasonId": season["seasonId"], "resumeBatchId": original["batchId"], "confirm": True},
        context=next_context(context, "season-failure-resume"),
    )
    after_resume = store.snapshot()
    resumed_ids = [item["episodeId"] for item in resumed["episodes"]]
    refused = tool(
        registry,
        store,
        "season_generate",
        {"seasonId": season["seasonId"], "resumeBatchId": resumed["batchId"], "confirm": True},
        context=next_context(context, "season-failure-refused"),
    )
    checks = {
        "partialBatch": original["status"] == "partial",
        "failedMemberIsolated": by_episode[episode_ids[1]]["status"] == "failed"
        and by_episode[episode_ids[1]]["errorCode"] == "BATCH_EMPTY"
        and by_episode[episode_ids[0]]["status"] == "succeeded"
        and by_episode[episode_ids[2]]["status"] == "succeeded",
        "onlyTwoMembersRegistered": jobs_after_first == 24,
        "resumeTargetsFailedEpisodeOnly": resumed_ids == [episode_ids[1]]
        and resumed["resumeOf"] == original["batchId"]
        and resumed["status"] == "succeeded",
        "resumeFreezesTheNewVersion": resumed["episodes"][0]["episodeVersion"] == 2,
        "noDuplicateRegistration": len(after_resume["jobs"]) == jobs_after_first + 12,
        "oldBatchIsByteFrozen": after_resume["batches"][original["batchId"]] == frozen,
        "resumeOfSucceededIsRefused": refused.get("ok") is False
        and refused["error"]["code"] == "BATCH_CONFLICT",
        "noProviderSubmits": ledger.submit_calls == 0,
    }
    return {
        "case": case.name,
        "checks": checks,
        "seasonId": season["seasonId"],
        "original": {
            "batchId": original["batchId"],
            "status": original["status"],
            "episodeIds": original["episodeIds"],
            "episodeStatuses": [item["status"] for item in original["episodes"]],
            "errorCodes": [item["errorCode"] for item in original["episodes"]],
        },
        "resumed": {
            "batchId": resumed["batchId"],
            "resumeOf": resumed["resumeOf"],
            "episodeIds": resumed_ids,
            "episodeVersions": [item["episodeVersion"] for item in resumed["episodes"]],
        },
        "mockJobs": len(after_resume["jobs"]),
        "batchCount": len(after_resume["batches"]),
        "controlledSubmits": ledger.submit_calls,
    }


def timeline_clips(state, boards):
    """One full-length clip per delivered shot, in board/shot order."""
    clips = []
    for board in boards:
        record = state["storyboards"][board["storyboardId"]]
        shots = {item["id"]: item for item in record["versions"][-1]["shots"]}
        for shot_id in board["shotIds"]:
            version = next(item for item in reversed(record["shotVersions"]) if item["shotId"] == shot_id)
            clips.append(
                {
                    "shotId": shot_id,
                    "shotVersionId": version["id"],
                    "storyboardId": board["storyboardId"],
                    "sceneId": board["sceneId"],
                    "inMs": 0,
                    "outMs": shots[shot_id]["durationSeconds"] * 1000,
                }
            )
    return clips


async def run_workbench(case, base):
    store, clock, ledger = base["store"], base["clock"], base["ledger"]
    registry, _service = mock_registry(store, clock)
    plan = plan_episode(case_text(case), title=case.title)
    scenes = plan["scenes"][:TIMELINE_SCENES]
    episode = build_episode(registry, store, case.title, "m7e4-timeline", scenes=scenes)

    delivered = []
    for index, board in enumerate(episode["boards"]):
        entries = []
        for number, shot_id in enumerate(board["shotIds"], start=1):
            job = await deliver_clip(
                store,
                clock,
                ledger,
                index=f"timeline-{index}-{number}",
                prompt=f"{episode['scenes'][index]['heading']} 第 {number} 镜",
            )
            delivered.append(job.id)
            entries.append(
                (
                    shot_id,
                    job.id,
                    job.result.media_refs[0].media_id,
                    job.request.model_dump(mode="json", by_alias=True),
                )
            )
        link_shot_versions(
            store,
            storyboard_id=board["storyboardId"],
            storyboard_version=board["version"],
            entries=entries,
        )

    # A season of the two supporting episodes exercises the batch/workbench surface.
    supporting = [
        build_episode(registry, store, f"第{index + 2}集", f"m7e4-support-{index}", scenes=batch_scenes())
        for index in range(2)
    ]
    season = save_season(
        registry,
        store,
        [item["episode"]["episodeId"] for item in supporting],
        title="雨夜咖啡店 支线",
    )
    batch = ok(
        registry,
        store,
        "season_generate",
        {"seasonId": season["seasonId"], "confirm": True},
        context=next_context(create_run(store, "m7e4-support-run"), "support-batch"),
    )

    subtitle = ok(
        registry,
        store,
        "subtitle_save",
        {"scope": "episode", "episodeId": episode["episode"]["episodeId"]},
        key="m7e4-timeline-subtitle",
    )
    speakers = sorted({line["speaker"] or "画外" for scene in scenes for line in scene["dialogue"]})
    audio_context = create_run(store, "m7e4-audio-run")
    audio = await registry.aexecute(
        "audio_generate",
        {
            "episodeId": episode["episode"]["episodeId"],
            "voices": [{"speaker": speaker, "voiceId": "local-narrator"} for speaker in speakers],
            "confirm": True,
        },
        store=store,
        project_id=PROJECT,
        operation_key=audio_context.operation_key,
        context=audio_context,
    )
    if not audio.get("ok"):
        raise AppError(audio["error"]["code"], audio["error"]["message"])
    bgm = await import_audio(
        _service,
        project_id=PROJECT,
        data=wav_bytes(),
        kind="bgm",
        title="雨声",
        source_url="https://example.test/rain.wav",
        license="CC0",
    )

    clips = timeline_clips(store.snapshot(), episode["boards"])
    voice_tracks = [
        {"audioId": asset["audioId"], "audioVersion": asset["audioVersion"], "kind": "voice"}
        for asset in audio["data"]["assets"]
    ]
    audio_tracks = [
        {
            "audioId": bgm["audioId"],
            "audioVersion": bgm["audioVersion"],
            "kind": "bgm",
            "gainDb": -12,
            "startMs": 500,
        },
        *voice_tracks,
    ]
    title_cards = [
        {"text": "雨夜咖啡店", "startMs": 1000, "durationMs": 2000},
        {"text": "未完待续", "startMs": 25_000, "durationMs": 3000},
    ]
    v1 = ok(
        registry,
        store,
        "timeline_save",
        {
            "episodeId": episode["episode"]["episodeId"],
            "clips": clips,
            "subtitle": {"trackId": subtitle["trackId"], "trackVersion": subtitle["latest"]["version"]},
            "audio": audio_tracks,
            "titleCards": title_cards,
        },
        key="m7e4-timeline-v1",
    )
    replay = ok(
        registry,
        store,
        "timeline_save",
        {
            "episodeId": episode["episode"]["episodeId"],
            "clips": clips,
            "subtitle": {"trackId": subtitle["trackId"], "trackVersion": subtitle["latest"]["version"]},
            "audio": audio_tracks,
            "titleCards": title_cards,
        },
        key="m7e4-timeline-replay",
    )
    frozen_v1 = json.loads(json.dumps(store.snapshot()["timelines"][v1["timelineId"]]["versions"][0]))
    first = await render_timeline(store, v1["timelineId"], key="m7e4-render-v1")
    film = film_record(store, first["filmId"])
    first_sha = film["versions"][0]["sha256"]
    first_version = Film.model_validate(film).versions[0]

    # Reorder the first two shots, trim the new head by one second and add a crossfade.
    edited = [dict(item) for item in clips]
    edited[0], edited[1] = edited[1], edited[0]
    edited[0]["inMs"] = 1000
    transitions = [{"afterShotId": edited[0]["shotId"], "type": "crossfade", "durationMs": 400}]
    v2 = ok(
        registry,
        store,
        "timeline_save",
        {
            "episodeId": episode["episode"]["episodeId"],
            "timelineId": v1["timelineId"],
            "expectedRevision": v1["revision"],
            "clips": edited,
            "transitions": transitions,
            "subtitle": {"trackId": subtitle["trackId"], "trackVersion": subtitle["latest"]["version"]},
            "audio": audio_tracks,
            "titleCards": title_cards,
        },
        key="m7e4-timeline-v2",
    )
    second = await render_timeline(store, v1["timelineId"], key="m7e4-render-v2")
    old = await render_timeline(store, v1["timelineId"], key="m7e4-render-v1-again", timeline_version=1)
    film_v2 = Film.model_validate(film_record(store, first["filmId"]))

    conflicts = {
        "duplicate": timeline_refusal(
            registry,
            store,
            {
                "episodeId": episode["episode"]["episodeId"],
                "timelineId": v1["timelineId"],
                "expectedRevision": v2["revision"],
                "clips": [*edited, edited[0]],
            },
        ),
        "shortTrim": timeline_refusal(
            registry,
            store,
            {
                "episodeId": episode["episode"]["episodeId"],
                "timelineId": v1["timelineId"],
                "expectedRevision": v2["revision"],
                "clips": [dict(edited[0], inMs=edited[0]["outMs"] - 500), *edited[1:]],
            },
        ),
        "transitionAfterLast": timeline_refusal(
            registry,
            store,
            {
                "episodeId": episode["episode"]["episodeId"],
                "timelineId": v1["timelineId"],
                "expectedRevision": v2["revision"],
                "clips": edited,
                "transitions": [{"afterShotId": edited[-1]["shotId"], "type": "fade", "durationMs": 300}],
            },
        ),
    }

    cues_v1 = rendered_cues(store.snapshot(), Film.model_validate(film), first_version)
    cues_v2 = rendered_cues(store.snapshot(), film_v2, film_v2.versions[-1])
    effective_v1 = v1["latest"]["effectiveDurationMs"]
    effective_v2 = v2["latest"]["effectiveDurationMs"]

    # The stale contract is proved on a byte-for-byte copy so the workbench home
    # keeps a live (non-stale) timeline for the browser and installed-wheel checks.
    stale_home = store.home.parent / f"{store.home.name}-stale"
    if stale_home.exists():
        shutil.rmtree(stale_home)
    shutil.copytree(store.home, stale_home)
    (stale_home / "instance.lock").unlink(missing_ok=True)
    with FileStore.open(stale_home) as stale_store:
        stale_registry, _stale_service = mock_registry(stale_store, clock)
        edited_episode = stale_store.snapshot()["episodes"][episode["episode"]["episodeId"]]
        current_scenes = ok(
            stale_registry,
            stale_store,
            "episode_read",
            {"episodeId": edited_episode["id"]},
            key="m7e4-episode-read-v1",
        )["scenes"]
        ok(
            stale_registry,
            stale_store,
            "episode_save",
            {
                "episodeId": edited_episode["id"],
                "expectedRevision": edited_episode["revision"],
                "title": f"{case.title}（第二版）",
                "targetDurationSeconds": sum(scene["durationSeconds"] for scene in scenes),
                "scenes": [
                    {
                        **scene_args(scene),
                        # Keep the same scenes so their storyboards stay bound; only the
                        # episode version advances and the timeline becomes stale.
                        "id": current_scenes[index]["sceneId"],
                    }
                    for index, scene in enumerate(scenes)
                ],
            },
            key="m7e4-episode-v2",
        )
        stale_read = ok(
            stale_registry,
            stale_store,
            "timeline_read",
            {"episodeId": episode["episode"]["episodeId"]},
            key="m7e4-timeline-stale",
        )
        before_stale = stale_store.snapshot()
        stale_code = await render_failure(stale_store, v1["timelineId"], key="m7e4-render-stale")
        after_stale = stale_store.snapshot()
        film_after_stale = Film.model_validate(film_record(stale_store, first["filmId"]))

    assembly = film_v2.versions[-1].assembly.assembly_version
    sources = film_v2.versions[-1].render.sources.timeline
    checks = {
        "seasonBatchRegistered": batch["status"] == "succeeded"
        and [item["jobCount"] for item in batch["episodes"]] == [12, 12],
        "timelineV1Frozen": v1["currentVersion"] == 1
        and v1["latest"]["clipCount"] == len(clips)
        and effective_v1 == sum(item["outMs"] - item["inMs"] for item in clips)
        and v1["replayed"] is False,
        "timelineReplayHitsTheSameVersion": replay["replayed"] is True
        and replay["currentVersion"] == 1
        and replay["timelineId"] == v1["timelineId"],
        "filmV1IsATimelineRender": first["filmVersion"] == 1
        and first_version.assembly.assembly_version == RENDER_TIMELINE_ASSEMBLY
        and first_version.render.sources.timeline is not None
        and first_version.render.sources.timeline.timeline_version == 1
        and first_version.render.subtitle is not None
        and first_version.render.subtitle.retimed is True
        and abs(first_version.metadata.duration_seconds - effective_v1 / 1000) <= 1 / 30 + 0.05,
        "timelineV2Appended": v2["currentVersion"] == 2
        and v2["revision"] == v1["revision"] + 1
        and effective_v2 == effective_v1 - 1_400
        and store.snapshot()["timelines"][v1["timelineId"]]["versions"][0] == frozen_v1
        and len(store.snapshot()["timelines"][v1["timelineId"]]["versions"]) == 2,
        "filmV2ReassemblesTheEdit": second["filmId"] == first["filmId"]
        and second["filmVersion"] == 2
        and assembly == RENDER_TIMELINE_ASSEMBLY
        and sources is not None
        and sources.timeline_version == 2
        and abs(film_v2.versions[-1].metadata.duration_seconds - effective_v2 / 1000) <= 1 / 30 + 0.05,
        "oldFilmVersionIsByteFrozen": film_v2.versions[0].sha256 == first_sha,
        "oldTimelineVersionStillRenders": old["replay"] is True and old["filmVersion"] == 1,
        "subtitleRemapIsExactAndOrdered": bool(cues_v1)
        and len(cues_v2) == len(cues_v1)
        and abs((cues_v2[0].start.total_microseconds() - cues_v1[0].start.total_microseconds()) + 1_400_000)
        <= 1
        and all(
            0 <= cue.start.total_microseconds() < effective_v2 * 1000
            and 0 < cue.end.total_microseconds() <= effective_v2 * 1000
            for cue in cues_v2
        )
        and all(
            before.end.total_microseconds() <= after.start.total_microseconds()
            for before, after in zip(cues_v2, cues_v2[1:], strict=False)
        ),
        "timelineAudioTracksFrozen": {track.kind for track in film_v2.versions[-1].render.audio.tracks}
        == {"voice", "bgm"}
        and any(
            track.kind == "bgm" and track.gain_db == -12 and track.start.total_microseconds() == 500_000
            for track in film_v2.versions[-1].render.audio.tracks
        ),
        "invalidEditsAreRefused": conflicts
        == {
            "duplicate": "TIMELINE_INVALID",
            "shortTrim": "TIMELINE_TRIM_INVALID",
            "transitionAfterLast": "TIMELINE_INVALID",
        },
        "staleTimelineRefusesBeforeEncoding": stale_read["latest"]["stale"] is True
        and stale_code == "TIMELINE_SOURCE_MISSING"
        and after_stale["films"] == before_stale["films"]
        and after_stale["media"] == before_stale["media"]
        and after_stale["renders"] == before_stale["renders"],
        "filmHistoryKeepsBothVersions": [item.version for item in film_after_stale.versions] == [1, 2],
    }
    return {
        "case": case.name,
        "checks": checks,
        "seasonId": season["seasonId"],
        "episodeId": episode["episode"]["episodeId"],
        "timeline": {
            "timelineId": v1["timelineId"],
            "versions": [v1["currentVersion"], v2["currentVersion"]],
            "effectiveDurationMs": [effective_v1, effective_v2],
            "clippedShots": len(clips),
            "transitions": len(transitions),
            "titleCards": [card["text"] for card in title_cards],
            "audioKinds": sorted({item["kind"] for item in audio_tracks}),
            "subtitleCuesV1": len(cues_v1),
            "subtitleCuesV2": len(cues_v2),
            "subtitleShiftMs": (cues_v1[0].start.total_microseconds() - cues_v2[0].start.total_microseconds())
            / 1000
            if cues_v1 and cues_v2
            else None,
        },
        "films": {
            "filmId": first["filmId"],
            "versions": [item.version for item in film_after_stale.versions],
            "assemblyVersion": assembly,
            "timelineVersion": sources.timeline_version if sources is not None else None,
            "durationSeconds": film_v2.versions[-1].metadata.duration_seconds,
            "sizeBytes": film_v2.versions[-1].size_bytes,
            "sha256": film_v2.versions[-1].sha256,
            "subtitleSidecarSha256": film_v2.versions[-1].render.subtitle.sidecar_sha256,
            "audioTracks": len(film_v2.versions[-1].render.audio.tracks),
            "loudness": film_v2.versions[-1].render.audio.loudness.output["verification"],
        },
        "batch": {
            "batchId": batch["batchId"],
            "status": batch["status"],
            "jobCounts": [item["jobCount"] for item in batch["episodes"]],
        },
        "refusals": conflicts,
        "staleCode": stale_code,
        "deliveredClips": len(delivered),
        "mockJobs": len([raw for raw in store.snapshot()["jobs"].values() if raw["mode"] == "mock"]),
        "batchCount": len(store.snapshot()["batches"]),
        "controlledSubmits": ledger.submit_calls,
    }


def timeline_refusal(registry, store, args):
    result = tool(registry, store, "timeline_save", args, key=f"m7e4-refuse-{uuid4()}")
    return result.get("error", {}).get("code") if not result.get("ok") else "ACCEPTED"


def film_record(store, film_id):
    return store.snapshot()["films"][film_id]


def render_args(timeline_id, *, timeline_version=None):
    args = {
        "scope": "timeline",
        "timelineId": timeline_id,
        "profile": "720p-h264-aac-v1",
        "subtitleMode": "both",
        "confirm": True,
    }
    if timeline_version is not None:
        args["timelineVersion"] = timeline_version
    return FilmRenderArgs.model_validate(args).model_dump(mode="json", by_alias=True)


async def render_timeline(store, timeline_id, *, key, timeline_version=None):
    config = mode_config(store.home, "mock")
    service = JobService(store, [MockVideoAdapter(store)], config=config)
    args = render_args(timeline_id, timeline_version=timeline_version)
    result = await service.renders.render(args, project_id=PROJECT, operation_key=key, args=args)
    if not result.get("ok"):
        raise AppError(result["error"]["code"], result["error"]["message"])
    return result["data"]


async def render_failure(store, timeline_id, *, key):
    config = mode_config(store.home, "mock")
    service = JobService(store, [MockVideoAdapter(store)], config=config)
    args = render_args(timeline_id)
    try:
        await service.renders.render(args, project_id=PROJECT, operation_key=key, args=args)
    except AppError as error:
        return error.code
    return "ACCEPTED"


async def run_case(case, base):
    if case.kind == "batch":
        return await run_season_batch(case, base)
    if case.kind == "failure":
        return await run_season_failure(case, base)
    return await run_workbench(case, base)


def all_checks_pass(checks):
    return bool(checks) and all(checks.values())


def totals(report):
    cases = report.get("cases") or []
    return {
        "plannedCases": report.get("plannedCases", len(CASES)),
        "finishedCases": len(cases),
        "passedCases": len([item for item in cases if all_checks_pass(item.get("checks"))]),
        "modelCalls": 0,
        "textModelCalls": 0,
        "ttsRealRequests": 0,
        "realProviderCalls": 0,
        "controlledSubmits": sum(item.get("controlledSubmits") or 0 for item in cases),
        "deliveredClips": sum(item.get("deliveredClips") or 0 for item in cases),
        "mockJobs": sum(item.get("mockJobs") or 0 for item in cases),
        "batches": sum(item.get("batchCount") or 0 for item in cases),
        "batchResumes": sum(1 for item in cases if item.get("resumed")),
        "timelineVersions": sum(len(item["timeline"]["versions"]) for item in cases if item.get("timeline")),
        "filmVersions": sum(len(item["films"]["versions"]) for item in cases if item.get("films")),
        "renderReplays": sum(1 for item in cases if item.get("films")),
        "subtitleCues": sum(
            (item["timeline"].get("subtitleCuesV2") or 0) for item in cases if item.get("timeline")
        ),
    }


async def run_suite(args):
    if getattr(args, "live", False):
        raise AppError(
            "EVAL_LIVE_UNAVAILABLE",
            "M7 E4 未获真实付费样本的模型、次数与金额授权；--live 未执行，未发起任何请求。",
        )
    if args.output.exists():
        raise AppError("EVAL_OUTPUT_EXISTS", "输出文件已存在，请使用新路径保留先前证据。")
    home = args.home.resolve()
    if home.exists() and any(home.iterdir()):
        raise AppError("EVAL_HOME_NOT_EMPTY", "评测需要新的空数据目录，已有数据未修改。")
    if not FIXTURE.is_file():
        raise AppError("EVAL_FIXTURE_MISSING", "缺少本地测试媒体夹具，离线评测未启动。")
    selected = [case for case in CASES if not args.only or case.name in args.only]
    report = {
        "schemaVersion": 1,
        "suiteId": suite_identity(CASES),
        "mode": "offline-fixture",
        "status": "incomplete",
        "startedAt": now(),
        "dataDirectory": str(home),
        "plannedCases": len(selected),
        "cases": [],
        "limitations": (
            "受控 Wan 传输、本地测试 MP4 与 local-tts-v1 的固定脚本用例：证明季批量、逐集隔离、显式恢复、"
            "时间线 v1→v2 重拼、字幕重映射与本地渲染的生产代码路径，不证明真实账号、真实 CosyVoice 声音、"
            "真实 BGM 或账单。真实付费样本未执行。"
        ),
    }

    def persist():
        report["totals"] = totals(report)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_json(args.output, report)

    persist()
    started = time.monotonic()
    print(
        json.dumps(
            {"suite": report["suiteId"], "mode": report["mode"], "dataDirectory": str(home)},
            ensure_ascii=False,
        ),
        flush=True,
    )
    try:
        for case in selected:
            case_home = home / case.name
            with FileStore.open(case_home) as store:
                store.ensure_session(PROJECT)
                ledger = Ledger()
                clock = SuiteClock()
                item = await run_case(case, {"store": store, "ledger": ledger, "clock": clock})
                item["upstream"] = ledger.view()
            report["cases"].append(item)
            persist()
            print(
                json.dumps(
                    {
                        "case": case.name,
                        "passed": all_checks_pass(item["checks"]),
                        "failedChecks": [key for key, value in item["checks"].items() if not value],
                        "timeline": (item.get("timeline") or {}).get("effectiveDurationMs"),
                    },
                    ensure_ascii=False,
                ),
                flush=True,
            )
        report["status"] = (
            "passed"
            if len(report["cases"]) == len(selected)
            and all(all_checks_pass(item.get("checks")) for item in report["cases"])
            else "failed"
        )
    except (Exception, KeyboardInterrupt):
        report["status"] = "error"
        report["error"] = traceback.format_exc(limit=8)
        raise
    finally:
        report["finishedAt"] = now()
        report["durationSeconds"] = round(time.monotonic() - started, 3)
        persist()
        print(
            json.dumps(
                {
                    "status": report["status"],
                    "totals": report.get("totals"),
                    "durationSeconds": report.get("durationSeconds"),
                },
                ensure_ascii=False,
            ),
            flush=True,
        )
    return report


def parser():
    root = argparse.ArgumentParser(description=__doc__)
    root.add_argument("--home", type=Path, required=True, help="新的空数据目录")
    root.add_argument("--output", type=Path, required=True, help="报告输出路径")
    root.add_argument(
        "--only",
        action="append",
        default=[],
        choices=[case.name for case in CASES],
        help="仅运行指定用例（开发调试；默认全部）",
    )
    root.add_argument(
        "--live",
        action="store_true",
        help="M7 E4 未获真实付费样本授权；本参数会拒绝执行且不发起请求",
    )
    return root


def main():
    args = parser().parse_args()
    try:
        report = asyncio.run(run_suite(args))
    except AppError as error:
        print(json.dumps({"status": "error", "code": error.code, "message": str(error)}, ensure_ascii=False))
        raise SystemExit(1) from None
    raise SystemExit(0 if report["status"] == "passed" else 1)


if __name__ == "__main__":
    main()
