"""M6 E4 offline acceptance: delivered shots -> subtitles/voice -> 60-90 s film.

Default mode is fully offline. The real Store, episode/storyboard/subtitle/audio
tools, the controlled Wan transport, Job/Media Workers, the render service and the
bundled FFmpeg run through production code paths. Submissions go to
``httpx.MockTransport`` and every delivered clip is the committed local MP4 fixture.

``--live`` is deliberately unavailable: a paid Wan/CosyVoice sample needs an explicit
model/count/amount approval and is recorded separately from this suite.
"""

import argparse
import asyncio
import hashlib
import json
import subprocess
import time
import traceback
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from fractions import Fraction
from pathlib import Path
from uuid import uuid4

import httpx

from vagent.config import Config
from vagent.errors import AppError
from vagent.storage import FileStore, RunRecordV1, atomic_write_json, now
from vagent.tools import create_project_tools
from vagent.video.adaptation import plan_episode
from vagent.video.assembler import bundled_engine
from vagent.video.assets import asset_reference_loader
from vagent.video.contracts import ImageInputRule, wan_capabilities
from vagent.video.films import find_episode_film
from vagent.video.jobs import JobService
from vagent.video.media_http import MediaHttpClient
from vagent.video.media_worker import MediaWorker
from vagent.video.providers.mock import MockVideoAdapter
from vagent.video.providers.wan import WanAdapter
from vagent.video.renders import render_sidecar_text
from vagent.video.storyboards import ShotVersion, Storyboard
from vagent.video.subtitle_service import parse_streams
from vagent.video.tools import register_video_tools
from vagent.video.worker import JobWorker
from vagent.waiting import ToolExecutionContext

ROOT = Path(__file__).resolve().parent.parent
FIXTURE = ROOT / "tests" / "fixtures" / "m1c" / "sample-720p.mp4"
TEXTS = ROOT / "tests" / "fixtures" / "m5" / "texts"
WAN_RESULT_HOST = "dashscope-result-sh.oss-accelerate.aliyuncs.com"
PROJECT = "m6-e4"
VIDEO_URL = f"https://{WAN_RESULT_HOST}/m6-e4.mp4?Signature=offline"


@dataclass(frozen=True)
class Case:
    """One offline acceptance scenario."""

    name: str
    kind: str  # episode | failure | refusals
    text: str
    title: str
    scenes: int | None = None
    skip_scene: int | None = None


CASES = (
    Case("episode-both", "episode", "rainy-cafe.md", "雨夜咖啡店"),
    Case("episode-failure", "failure", "rainy-cafe.md", "雨夜咖啡店", skip_scene=2),
    Case("episode-refusals", "refusals", "rainy-cafe.md", "雨夜咖啡店"),
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

    def due(self, job):
        self.value = max(self.value, datetime.fromisoformat(job.next_poll_at))


class Ledger:
    """Observable facts of the controlled transport; no real connection is made."""

    def __init__(self):
        self.submit_calls = 0
        self.query_calls = 0
        self.download_calls = 0
        self.submit_attempts = 0

    def view(self):
        return {
            "submitCalls": self.submit_calls,
            "queryCalls": self.query_calls,
            "downloadCalls": self.download_calls,
            "submitAttempts": self.submit_attempts,
        }


def provider_response(status="PENDING", *, task_id="m6-e4-task", request_id="m6-e4-trace", **output):
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


def live_config(home):
    return Config(
        home=home,
        api_key=None,
        video_mode="live",
        video_api_key="offline-video-secret",
        video_workspace_id="offline-workspace",
        kling_api_key=None,
        fal_api_key=None,
        redis_url=None,
        skills_root=None,
        mcp_config=None,
        mcp_local=False,
    )


def live_run(store, run_id, project_id=PROJECT):
    record = RunRecordV1(
        id=run_id,
        session_id=project_id,
        request_id=run_id,
        prompt="offline M6 E4 controlled delivery",
        model="no-model",
        status="completed",
        messages=[],
        model_steps=0,
        tool_calls=0,
        input_tokens=0,
        output_tokens=0,
        answer="",
        created_at="2030-01-01T00:00:00+00:00",
        updated_at="2030-01-01T00:00:00+00:00",
    )

    def add(draft):
        draft["runs"][run_id] = record.model_dump(by_alias=True)
        draft["sessions"][project_id]["latestRunId"] = run_id
        draft["runs"][run_id].update(videoMode="live", executionVersion=2)

    store.transaction(add)
    return ToolExecutionContext(
        project_id=project_id,
        session_id=project_id,
        run_id=run_id,
        model_step=1,
        tool_call_id="generate",
    )


@asynccontextmanager
async def live_service(store, clock, ledger):
    data = fixture_bytes()

    def upstream(request):
        if request.method == "POST":
            ledger.submit_calls += 1
            ledger.submit_attempts += 1
            return httpx.Response(
                200,
                json=provider_response("PENDING", task_id="m6-e4-task", request_id="m6-e4-submit"),
            )
        ledger.query_calls += 1
        return httpx.Response(
            200,
            json=provider_response(
                "SUCCEEDED",
                task_id="m6-e4-task",
                request_id="m6-e4-query",
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

    config = live_config(store.home)
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
    run_id = f"m6-e4-clip-{index}"
    context = live_run(store, run_id)
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


def scene_durations(scene):
    count = scene["shotCount"]
    base, extra = divmod(scene["durationSeconds"], count)
    return [base + (1 if number < extra else 0) for number in range(count)]


def bind_storyboard(registry, store, scene_id, index, scene, key):
    result = registry.execute(
        "storyboard_save",
        {
            "title": f"{scene['heading']} {index}",
            "sceneId": scene_id,
            "shots": [
                {
                    "description": f"{scene['heading']} 第 {number} 镜",
                    "framing": "wide",
                    "cameraMovement": "static",
                    "durationSeconds": duration,
                    "mode": "t2v",
                }
                for number, duration in enumerate(scene_durations(scene), start=1)
            ],
        },
        store=store,
        project_id=PROJECT,
        operation_key=key,
    )
    if not result.get("ok"):
        raise AppError("EVAL_STORYBOARD_FAILED", json.dumps(result, ensure_ascii=False)[:500])
    return result["data"]


def link_shot_versions(store, *, storyboard_id, storyboard_version, entries):
    timestamp = "2030-01-01T00:00:00+00:00"

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
                created_at=timestamp,
            )
            for shot_id, job_id, media_id, request in entries
        ]
        updated = record.model_copy(
            update={"shot_versions": (*record.shot_versions, *created), "revision": record.revision + 1}
        )
        draft["storyboards"][storyboard_id] = updated.model_dump(mode="json", by_alias=True)

    store.transaction(link)
    return store.snapshot()["storyboards"][storyboard_id]["revision"]


def audio_lines(plan):
    speakers = sorted({(line["speaker"] or "画外") for scene in plan["scenes"] for line in scene["dialogue"]})
    return [{"speaker": speaker, "voiceId": "local-narrator"} for speaker in speakers]


async def build_fixture(store, case, ledger, clock):
    registry = register_video_tools(
        create_project_tools(), JobService(store, [MockVideoAdapter(store, clock=clock)]), mode="mock"
    )
    plan = plan_episode(case_text(case), title=case.title)
    if case.scenes is not None:
        plan = {**plan, "scenes": plan["scenes"][: case.scenes]}
        plan["shots"] = [shot for shot in plan["shots"] if shot["sceneIndex"] <= case.scenes]
        plan["shotBudget"] = {
            **plan["shotBudget"],
            "shotCount": sum(scene["shotCount"] for scene in plan["scenes"]),
        }
    scenes = [
        {
            "heading": scene["heading"],
            "location": scene["location"],
            "timeOfDay": scene["timeOfDay"],
            "synopsis": scene["synopsis"],
            "dialogue": [
                {"text": line["text"], "speaker": line.get("speaker"), "paragraph": line["paragraph"]}
                for line in scene["dialogue"]
            ],
            "shotCount": scene["shotCount"],
            "durationSeconds": scene["durationSeconds"],
        }
        for scene in plan["scenes"]
    ]
    saved = registry.execute(
        "episode_save",
        {"title": case.title, "scenes": scenes},
        store=store,
        project_id=PROJECT,
        operation_key=f"m6-e4-{case.name}-episode",
    )
    if not saved.get("ok"):
        raise AppError("EVAL_EPISODE_FAILED", json.dumps(saved, ensure_ascii=False)[:500])
    episode = saved["data"]
    boards = []
    delivered = 0
    for index, scene in enumerate(plan["scenes"]):
        board = bind_storyboard(
            registry,
            store,
            episode["sceneIds"][index],
            index,
            scene,
            f"m6-e4-{case.name}-board-{index}",
        )
        entries = []
        if index != case.skip_scene:
            for number, shot_id in enumerate(board["shotIds"], start=1):
                delivered += 1
                job = await deliver_clip(
                    store,
                    clock,
                    ledger,
                    index=f"{case.name}-{index}-{number}",
                    prompt=f"{scene['heading']} 第 {number} 镜",
                )
                entries.append(
                    (
                        shot_id,
                        job.id,
                        job.result.media_refs[0].media_id,
                        job.request.model_dump(mode="json", by_alias=True),
                    )
                )
        revision = (
            link_shot_versions(
                store,
                storyboard_id=board["storyboardId"],
                storyboard_version=board["version"],
                entries=entries,
            )
            if entries
            else 0
        )
        boards.append({**board, "revision": revision, "delivered": bool(entries)})
    subtitle = registry.execute(
        "subtitle_save",
        {"scope": "episode", "episodeId": episode["episodeId"]},
        store=store,
        project_id=PROJECT,
        operation_key=f"m6-e4-{case.name}-subtitle",
    )
    if not subtitle.get("ok"):
        raise AppError("EVAL_SUBTITLE_FAILED", json.dumps(subtitle, ensure_ascii=False)[:500])
    run_id = _tool_run(store, f"m6-e4-{case.name}-tool-run")
    audio_context = ToolExecutionContext(
        project_id=PROJECT,
        session_id=PROJECT,
        run_id=run_id,
        model_step=1,
        tool_call_id="audio",
    )
    audio = await registry.aexecute(
        "audio_generate",
        {
            "episodeId": episode["episodeId"],
            "voices": audio_lines(plan),
            "confirm": True,
        },
        store=store,
        project_id=PROJECT,
        operation_key=audio_context.operation_key,
        context=audio_context,
    )
    if not audio.get("ok"):
        raise AppError("EVAL_AUDIO_FAILED", json.dumps(audio, ensure_ascii=False)[:500])
    return {
        "registry": registry,
        "plan": plan,
        "episode": episode,
        "boards": boards,
        "trackId": subtitle["data"]["trackId"],
        "track": subtitle["data"],
        "audio": audio["data"],
        "jobs": delivered,
    }


def _tool_run(store, run_id):
    record = RunRecordV1(
        id=run_id,
        session_id=PROJECT,
        request_id=run_id,
        prompt="offline M6 E4 tool run",
        model="no-model",
        status="completed",
        messages=[],
        model_steps=0,
        tool_calls=0,
        input_tokens=0,
        output_tokens=0,
        answer="",
        created_at="2030-01-01T00:00:00+00:00",
        updated_at="2030-01-01T00:00:00+00:00",
    )
    store.transaction(lambda draft: draft["runs"].__setitem__(run_id, record.model_dump(by_alias=True)))
    return run_id


def render_args(case, fixture):
    last_shot = fixture["boards"][0]["shotIds"][-1]
    transitions = []
    if case.kind == "episode":
        transitions = [
            {"afterShotId": last_shot, "type": "crossfade", "durationMs": 400},
            {
                "afterShotId": fixture["boards"][3]["shotIds"][-1],
                "type": "fade",
                "durationMs": 300,
            },
        ]
    return {
        "scope": "episode",
        "episodeId": fixture["episode"]["episodeId"],
        "trackId": fixture["trackId"],
        "profile": "720p-h264-aac-v1",
        "subtitleMode": "both" if case.kind == "episode" else "soft",
        "transitions": transitions,
        "titleCards": (
            [
                {"text": case.title, "startMs": 100, "durationMs": 1200},
                {"text": "未完待续", "startMs": 86_000, "durationMs": 1500},
            ]
            if case.kind == "episode"
            else []
        ),
        "confirm": True,
    }


def stream_facts(path):
    probe = subprocess.run(
        [str(bundled_engine().path), "-hide_banner", "-nostdin", "-i", str(path)],
        capture_output=True,
        check=False,
    )
    streams = parse_streams(probe.stderr.decode("utf-8", "replace"))
    return {
        "duration": streams.duration_seconds,
        "video": streams.video,
        "audio": streams.audio,
        "subtitles": streams.subtitles,
    }


def black_frames(path):
    probe = subprocess.run(
        [
            str(bundled_engine().path),
            "-hide_banner",
            "-nostdin",
            "-i",
            str(path),
            "-vf",
            "blackdetect=d=0.04:pix_th=0.08",
            "-an",
            "-f",
            "null",
            "-",
        ],
        capture_output=True,
        check=False,
    )
    text = probe.stderr.decode("utf-8", "replace")
    return text.count("black_start")


def parse_srt_cues(text):
    cues = []
    for block in text.replace("\r\n", "\n").strip().split("\n\n"):
        lines = block.splitlines()
        timing = next((line for line in lines if "-->" in line), None)
        if timing is None:
            continue
        body = "\n".join(lines[lines.index(timing) + 1 :]).strip()
        if body:
            cues.append(body)
    return cues


async def run_episode(case, base):
    fixture = await build_fixture(base["store"], case, base["ledger"], base["clock"])
    args = render_args(case, fixture)
    service = JobService(base["store"], [MockVideoAdapter(base["store"], clock=base["clock"])])
    first = await service.renders.render(
        args,
        project_id=PROJECT,
        operation_key=f"m6-e4-{case.name}-render-1",
        args=args,
    )
    if not first.get("ok"):
        raise AppError(first["error"]["code"], first["error"]["message"])
    replay = await service.renders.render(
        args,
        project_id=PROJECT,
        operation_key=f"m6-e4-{case.name}-render-2",
        args=args,
    )
    data = first["data"]
    latest = data["film"]["latest"]
    film = find_episode_film(base["store"].snapshot(), PROJECT, fixture["episode"]["episodeId"])
    version = film.versions[-1]
    state = base["store"].snapshot()
    media = state["media"][latest["outputMediaId"]]
    path = base["store"].home / media["relativePath"]
    sidecar, sidecar_name = render_sidecar_text(state, film, version)
    cues = parse_srt_cues(sidecar)
    facts = stream_facts(path)
    track = state["subtitleTracks"][fixture["trackId"]]
    frozen = track["versions"][-1]
    checks = {
        "allScenesSucceeded": all(scene["status"] == "succeeded" for scene in data["scenes"]),
        "durationInRange": 60 <= float(latest["durationSeconds"]) <= 90,
        "durationMatchesTimeline": abs(float(facts["duration"]) - float(latest["durationSeconds"])) <= 0.05,
        "videoIs720pH264": facts["video"]["codec"] == "h264"
        and facts["video"]["width"] == 1280
        and facts["video"]["height"] == 720,
        "audioIs48kStereo": facts["audio"]["codec"] == "aac"
        and facts["audio"]["sample_rate"] == 48000
        and facts["audio"]["channels"] == 2,
        "embeddedSubtitles": any(item["codec"] == "mov_text" for item in facts["subtitles"]),
        "sidecarHasCues": len(cues) > 0,
        "sidecarName": sidecar_name.endswith(".srt"),
        "loudnessInTolerance": abs(
            float(version.render.audio.loudness.output["verification"]["integratedLufs"]) + 16
        )
        <= 0.5,
        "truePeakInTolerance": float(version.render.audio.loudness.output["verification"]["truePeakDb"])
        <= -1,
        "audioTracksMatchLines": len(version.render.audio.tracks)
        == sum(len(scene["dialogue"]) for scene in fixture["plan"]["scenes"]),
        "timelineContiguous": all(
            Fraction(entry["start"]["numerator"], entry["start"]["denominator"])
            == sum(
                (
                    Fraction(previous["duration"]["numerator"], previous["duration"]["denominator"])
                    for previous in frozen["timeline"][:index]
                ),
                Fraction(0),
            )
            for index, entry in enumerate(frozen["timeline"])
        ),
        "noBlackFrames": black_frames(path) == 0,
        "replayHitsTheSameVersion": replay.get("data", {}).get("replay") is True
        and replay.get("data", {}).get("filmVersion") == data["filmVersion"],
        "singleFilmVersion": len(film.versions) == 1,
        "allJobsSucceeded": all(raw["status"] == "succeeded" for raw in state["jobs"].values()),
    }
    return {
        "case": case.name,
        "checks": checks,
        "episodeId": fixture["episode"]["episodeId"],
        "film": {
            "filmId": film.id,
            "filmVersion": latest["version"],
            "durationSeconds": latest["durationSeconds"],
            "sizeBytes": latest["sizeBytes"],
            "sha256": latest["sha256"],
            "width": latest["width"],
            "height": latest["height"],
            "loudness": version.render.audio.loudness.output["verification"],
            "transitions": len(version.render.transitions),
            "titleCards": [card.text for card in version.render.title_cards],
            "audioTracks": len(version.render.audio.tracks),
            "subtitleCues": len(cues),
            "sidecarSha256": version.render.subtitle.sidecar_sha256,
        },
        "streams": facts,
        "sceneStatuses": [scene["status"] for scene in data["scenes"]],
        "replay": replay.get("data", {}).get("replay") is True,
        "deliveredClips": fixture["jobs"],
    }


async def run_failure(case, base):
    fixture = await build_fixture(base["store"], case, base["ledger"], base["clock"])
    args = render_args(case, fixture)
    service = JobService(base["store"], [MockVideoAdapter(base["store"], clock=base["clock"])])
    error = None
    try:
        await service.renders.render(
            args,
            project_id=PROJECT,
            operation_key=f"m6-e4-{case.name}-render",
            args=args,
        )
    except AppError as caught:
        error = caught
    state = base["store"].snapshot()
    read = service.renders.read({"episodeId": fixture["episode"]["episodeId"]}, PROJECT)
    scenes = read["render"]["scenes"] if read.get("render") else []
    checks = {
        "errorCode": error is not None and error.code == "RENDER_SOURCE_MISSING",
        "noFilmCommitted": find_episode_film(state, PROJECT, fixture["episode"]["episodeId"]) is None,
        "failedSceneIsolated": len(scenes) == len(fixture["plan"]["scenes"])
        and scenes[case.skip_scene]["status"] == "failed"
        and scenes[case.skip_scene]["failureStage"] == "source",
        "otherScenesSucceeded": sum(scene["status"] == "succeeded" for scene in scenes)
        == len(fixture["plan"]["scenes"]) - 1,
        "deliveredClipsPreserved": len(state["jobs"]) == fixture["jobs"],
        "allJobsSucceeded": all(raw["status"] == "succeeded" for raw in state["jobs"].values()),
    }
    return {
        "case": case.name,
        "checks": checks,
        "episodeId": fixture["episode"]["episodeId"],
        "renderStatus": read.get("render", {}).get("status"),
        "failedScene": case.skip_scene,
        "sceneStatuses": [scene["status"] for scene in scenes],
        "deliveredClips": fixture["jobs"],
    }


async def run_refusals(case, base):
    fixture = await build_fixture(base["store"], case, base["ledger"], base["clock"])
    service = JobService(base["store"], [MockVideoAdapter(base["store"], clock=base["clock"])])

    async def attempt(args, key):
        try:
            await service.renders.render(args, project_id=PROJECT, operation_key=key, args=args)
        except AppError as caught:
            return caught.code
        return None

    base_args = {
        "scope": "episode",
        "episodeId": fixture["episode"]["episodeId"],
        "profile": "720p-h264-aac-v1",
        "subtitleMode": "soft",
        "confirm": True,
    }
    codes = {
        "confirmation": await attempt({**base_args, "confirm": False}, "m6-e4-refuse-confirm"),
        "transition": await attempt(
            {
                **base_args,
                "transitions": [
                    {
                        "afterShotId": fixture["boards"][-1]["shotIds"][-1],
                        "type": "crossfade",
                        "durationMs": 500,
                    }
                ],
            },
            "m6-e4-refuse-transition",
        ),
        "missingTrack": await attempt(
            {**base_args, "trackId": str(uuid4()), "subtitleMode": "burn"},
            "m6-e4-refuse-track",
        ),
    }
    state = base["store"].snapshot()
    checks = {
        "confirmationRequired": codes["confirmation"] == "CONFIRMATION_REQUIRED",
        "invalidTransition": codes["transition"] == "TRANSITION_INVALID",
        "missingTrack": codes["missingTrack"] == "SUBTITLE_TRACK_MISSING",
        "noFilmCommitted": find_episode_film(state, PROJECT, fixture["episode"]["episodeId"]) is None,
        "noRenderTaskCommitted": not state["renders"],
    }
    return {
        "case": case.name,
        "checks": checks,
        "episodeId": fixture["episode"]["episodeId"],
        "codes": codes,
        "deliveredClips": fixture["jobs"],
    }


async def run_case(case, base):
    if case.kind == "episode":
        return await run_episode(case, base)
    if case.kind == "failure":
        return await run_failure(case, base)
    return await run_refusals(case, base)


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
        "submitCalls": sum((item.get("upstream") or {}).get("submitCalls") or 0 for item in cases),
        "deliveredClips": sum((item.get("upstream") or {}).get("deliveredClips") or 0 for item in cases),
        "filmVersions": sum(1 for item in cases if item.get("film")),
        "renderReplays": sum(1 for item in cases if item.get("replay")),
        "subtitleCues": sum((item.get("film") or {}).get("subtitleCues") or 0 for item in cases),
    }


async def run_suite(args):
    if getattr(args, "live", False):
        raise AppError(
            "EVAL_LIVE_UNAVAILABLE",
            "M6 E4 未获真实付费样本的模型、次数与金额授权；--live 未执行，未发起任何请求。",
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
            "受控 Wan 传输与本地测试 MP4 的固定脚本用例：证明字幕/配音/转场/整集渲染的生产代码路径、"
            "失败隔离与拒绝边界，不证明真实账号、真实 CosyVoice 声音或账单。真实付费样本未执行。"
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
                item["upstream"] = {
                    **ledger.view(),
                    "deliveredClips": item.get("deliveredClips", 0),
                }
            report["cases"].append(item)
            persist()
            print(
                json.dumps(
                    {
                        "case": case.name,
                        "passed": all_checks_pass(item["checks"]),
                        "failedChecks": [key for key, value in item["checks"].items() if not value],
                        "film": (item.get("film") or {}).get("durationSeconds"),
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
        help="M6 E4 未获真实付费样本授权；本参数会拒绝执行且不发起请求",
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
