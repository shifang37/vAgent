"""M5 E4 application acceptance: one script to one episode of scene films.

Default mode is fully offline. The real ApplicationService, Agent Runner, episode
tools, storyboard tools, provider registration, Job/Media Workers, film tools and the
bundled FFmpeg all run through production code paths. Wan is served by
``httpx.MockTransport``; every delivered clip is the committed local MP4 fixture. No
DNS/TLS connection, account, credential or paid task is used.

``--live`` is deliberately unavailable in this package: the M5 E4 real sample needs an
explicit model/count/amount approval and is recorded separately from this suite.
"""

import argparse
import asyncio
import hashlib
import json
import re
import struct
import subprocess
import sys
import time
import traceback
import zlib
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

import httpx
from dotenv import load_dotenv
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from vagent.application import ApplicationService
from vagent.config import load_config
from vagent.errors import AppError, public_error
from vagent.runner import RunPolicy
from vagent.storage import atomic_write_json, now
from vagent.video.adaptation import plan_episode
from vagent.video.assembler import bundled_engine
from vagent.video.contracts import WAN_I2V_MODEL, WAN_MODEL
from vagent.video.jobs import JobService
from vagent.video.media_http import MediaHttpClient
from vagent.video.media_worker import MediaWorker
from vagent.video.providers.wan import WanAdapter
from vagent.video.worker import JobWorker

ROOT = Path(__file__).resolve().parent.parent
FIXTURE = ROOT / "tests" / "fixtures" / "m1c" / "sample-720p.mp4"
TEXTS = ROOT / "tests" / "fixtures" / "m5" / "texts"
WAN_RESULT_HOST = "dashscope-result-sh.oss-accelerate.aliyuncs.com"
ACTIVE_STATUSES = {"pending_submit", "submitting", "queued", "running", "downloading"}
PROMPT_MARKER = re.compile(r"【M5 E4 ([a-z-]+)】")


@dataclass(frozen=True)
class Case:
    """One scripted acceptance scenario."""

    name: str
    kind: str  # episode | refusals | cap | redo
    text: str
    title: str
    style: bool = False
    characters: tuple[tuple[str, str, tuple[int, int, int]], ...] = ()
    review: str | None = None
    scene_index: int = 1


CASES = (
    Case("wan-episode", "episode", "fridge-notes.md", "冰箱上的便条", style=True),
    Case(
        "wan-i2v",
        "episode",
        "rainy-cafe.md",
        "雨夜咖啡店",
        style=True,
        characters=(
            ("陈默", "三十岁上下的咖啡店常客，深色风衣，神情克制", (54, 78, 112)),
            ("林晚", "二十多岁的店员，浅色围裙，笑起来眼睛弯起", (146, 88, 58)),
        ),
        review="陈默",
    ),
    Case("wan-refusals", "refusals", "last-station.md", "末班车"),
    Case("wan-cap", "cap", "last-station.md", "末班车"),
    Case("wan-redo", "redo", "rainy-cafe.md", "雨夜咖啡店", scene_index=2),
)
CASES_BY_NAME = {case.name: case for case in CASES}


def fixture_bytes():
    return FIXTURE.read_bytes()


def case_text(case: Case) -> str:
    return (TEXTS / case.text).read_text(encoding="utf-8")


def prompt_for(case: Case) -> str:
    return (
        f"【M5 E4 {case.name}】这是离线端到端验收：把固定中文文稿改编成一集，"
        "按场记保存分镜并为整集登记任务，一次 await_job 等待全部镜头交付，"
        "再为每个场记在本地拼接成片，最后如实汇报分集、镜头数、估算与逐场成片。"
        "不要重做镜头、不要修改预算、不要重复提交、不要编造媒体。"
    )


def suite_identity(cases) -> str:
    return hashlib.sha256(
        json.dumps(
            {
                "version": 1,
                "cases": [vars(case) | {"prompt": prompt_for(case)} for case in cases],
                "texts": {
                    case.text: hashlib.sha256((TEXTS / case.text).read_bytes()).hexdigest() for case in cases
                },
                "fixtureSha256": hashlib.sha256(fixture_bytes()).hexdigest(),
            },
            sort_keys=True,
            ensure_ascii=False,
        ).encode()
    ).hexdigest()[:16]


def _png_chunk(kind: bytes, data: bytes) -> bytes:
    return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))


def solid_png(width: int, height: int, rgb: tuple[int, int, int]) -> bytes:
    """One deterministic RGB reference frame; no image library is required."""
    rows = b"".join(b"\x00" + bytes(rgb) * width for _ in range(height))
    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + _png_chunk(b"IHDR", header)
        + _png_chunk(b"IDAT", zlib.compress(rows, 9))
        + _png_chunk(b"IEND", b"")
    )


class SuiteClock:
    def __init__(self):
        self.value = datetime.now(UTC)

    def __call__(self):
        return self.value

    def advance(self, seconds):
        self.value += timedelta(seconds=seconds)


def advance_clock(clock, service):
    """Move the fixture clock to the next persisted deadline (one rate-limit second)."""
    jobs = list(service.video_jobs.list())
    deadlines = []
    for job in jobs:
        if job.next_poll_at and not job.query_started_at:
            deadlines.append(datetime.fromisoformat(job.next_poll_at))
        download = getattr(job, "download", None)
        if download is not None and download.next_attempt_at:
            deadlines.append(datetime.fromisoformat(download.next_attempt_at))
    active = [job for job in jobs if job.status in ACTIVE_STATUSES and job.query_state != "paused"]
    if not active:
        return
    target = min(deadlines) if deadlines else clock.value + timedelta(seconds=1)
    if target <= clock.value:
        target = clock.value + timedelta(seconds=1)
    clock.value = target


class Ledger:
    """Controlled-upstream evidence, persisted outside the application state."""

    def __init__(self, home):
        self.path = Path(home) / "m5-fixture.json"
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            self.data = raw if isinstance(raw, dict) else {}
        except (OSError, ValueError):
            self.data = {}

    def case(self, name):
        return self.data.setdefault(
            name,
            {"submissions": [], "taskIds": [], "queryPaths": [], "downloads": [], "authorization": []},
        )

    def save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_json(self.path, self.data)


class WanFixture:
    """The frozen Wan protocol, served from the local fixture only."""

    def __init__(self, case, ledger):
        self.case, self.ledger = case, ledger
        self.statuses = {}
        self.durations = {}

    def handle(self, request):
        record = self.ledger.case(self.case.name)
        if request.method == "POST":
            body = json.loads(request.content)
            record["submissions"].append(body)
            task_id = f"m5-wan-{self.case.name}-{len(record['submissions'])}"
            self.statuses[task_id] = (
                "FAILED" if self.case.kind == "redo" and len(record["submissions"]) == 1 else "SUCCEEDED"
            )
            self.durations[task_id] = int((body.get("parameters") or {}).get("duration") or 5)
            record["taskIds"].append(task_id)
            record["authorization"].append(
                "bearer" if request.headers.get("authorization", "").startswith("Bearer ") else "other"
            )
            self.ledger.save()
            return httpx.Response(
                200,
                json={
                    "request_id": f"m5-wan-submit-{len(record['submissions'])}",
                    "output": {"task_id": task_id, "task_status": "PENDING"},
                },
            )
        task_id = request.url.path.rsplit("/", 1)[-1]
        record["queryPaths"].append(request.url.path)
        status = self.statuses.get(task_id, "SUCCEEDED")
        self.ledger.save()
        if status == "FAILED":
            return httpx.Response(
                200,
                json={
                    "request_id": f"m5-wan-query-{len(record['queryPaths'])}",
                    "output": {"task_id": task_id, "task_status": "FAILED", "code": "DataInspectionFailed"},
                },
            )
        duration = self.durations.get(task_id, 5)
        return httpx.Response(
            200,
            json={
                "request_id": f"m5-wan-query-{len(record['queryPaths'])}",
                "output": {
                    "task_id": task_id,
                    "task_status": status,
                    "video_url": (
                        f"https://{WAN_RESULT_HOST}/m5-{task_id}.mp4?Signature=m5-fixture&d={duration}"
                    ),
                },
                "usage": {
                    "duration": duration,
                    "output_video_duration": duration,
                    "input_video_duration": 0,
                    "video_count": 1,
                    "SR": 720,
                    "ratio": "16:9",
                },
            },
        )


class MediaFixture:
    """Serve a clip whose measured duration matches the frozen request."""

    def __init__(self, case, ledger, home):
        self.case, self.ledger = case, ledger
        self.directory = Path(home) / "fixtures" / "media"
        self.directory.mkdir(parents=True, exist_ok=True)
        self.cache = {}

    def variant(self, duration):
        if duration in self.cache:
            return self.cache[duration]
        path = self.directory / f"clip-{duration}s.mp4"
        if not path.is_file():
            engine = bundled_engine()
            result = subprocess.run(
                [
                    str(engine.path),
                    "-hide_banner",
                    "-nostdin",
                    "-loglevel",
                    "error",
                    "-y",
                    "-i",
                    str(FIXTURE),
                    "-t",
                    str(duration),
                    "-c:v",
                    "libx264",
                    "-preset",
                    "veryfast",
                    "-crf",
                    "20",
                    "-pix_fmt",
                    "yuv420p",
                    "-c:a",
                    "aac",
                    "-b:a",
                    "192k",
                    "-ar",
                    "48000",
                    "-ac",
                    "2",
                    str(path),
                ],
                stdin=subprocess.DEVNULL,
                capture_output=True,
                timeout=120,
                check=False,
            )
            if result.returncode != 0 or not path.is_file():
                detail = result.stderr.decode("utf-8", "replace")[-300:]
                raise AppError("EVAL_FIXTURE_MISSING", f"无法生成本地时长夹具：{detail}")
        self.cache[duration] = path.read_bytes()
        return self.cache[duration]

    def handle(self, request):
        record = self.ledger.case(self.case.name)
        record["downloads"].append({"host": request.url.host})
        self.ledger.save()
        query = parse_qs(urlsplit(str(request.url)).query)
        duration = int((query.get("d") or ["5"])[0])
        data = self.variant(duration)
        return httpx.Response(
            200,
            content=data,
            headers={"Content-Type": "video/mp4", "Content-Length": str(len(data))},
        )

    def prewarm(self, durations):
        """Re-encode every needed duration before any Worker starts."""
        for duration in sorted(set(durations)):
            self.variant(duration)


def make_fixtures(case, ledger, clock, home):
    wan = WanFixture(case, ledger)
    media = MediaFixture(case, ledger, home)
    return {
        "wan": wan,
        "media": media,
        "wan_factory": lambda **kwargs: WanAdapter(
            **kwargs, transport=httpx.MockTransport(wan.handle), clock=clock
        ),
    }


class patches:
    """Route the application's adapters, workers and clock through offline fixtures."""

    def __init__(self, fixtures, clock):
        self.entries = [
            patch("vagent.application.WanAdapter", fixtures["wan_factory"]),
            patch(
                "vagent.application.MediaHttpClient",
                lambda: MediaHttpClient(transport=httpx.MockTransport(fixtures["media"].handle)),
            ),
            patch(
                "vagent.application.JobService",
                lambda store, adapters, **kwargs: JobService(store, adapters, clock=clock, **kwargs),
            ),
            patch(
                "vagent.application.JobWorker",
                lambda service: JobWorker(service, idle_interval_seconds=0.01),
            ),
            patch(
                "vagent.application.MediaWorker",
                lambda media, client: MediaWorker(media, client, idle_interval_seconds=0.01),
            ),
        ]

    def __enter__(self):
        for entry in self.entries:
            entry.start()
        return self

    def __exit__(self, *_):
        for entry in reversed(self.entries):
            entry.stop()


def parsed(message):
    try:
        payload = json.loads(message.content)
    except (TypeError, ValueError):
        return {}
    return payload if isinstance(payload, dict) else {}


def episode_scene_args(plan, character_ids, *, scene_indices=None):
    """Scene arguments exactly as episode_plan produced them, plus resolved characters."""
    scenes = []
    for index, scene in enumerate(plan["scenes"], start=1):
        if scene_indices is not None and index not in scene_indices:
            continue
        scenes.append(
            {
                "heading": scene["heading"],
                "location": scene["location"],
                "timeOfDay": scene["timeOfDay"],
                "synopsis": scene["synopsis"],
                "characterIds": [
                    character_ids[name] for name in scene["characterNames"] if name in character_ids
                ],
                "dialogue": [
                    {"text": item["text"], "speaker": item.get("speaker"), "paragraph": item["paragraph"]}
                    for item in scene["dialogue"]
                ],
                "shotCount": scene["shotCount"],
                "durationSeconds": scene["durationSeconds"],
            }
        )
    return scenes


def scene_shot_args(plan, scene_index, character_ids):
    """Storyboard shots for one scene; i2v/flf2v only when the plan names characters."""
    shots = []
    for shot in plan["shots"]:
        if shot["sceneIndex"] != scene_index:
            continue
        names = [name for name in shot["characterNames"] if name in character_ids]
        if not character_ids or not names:
            mode, ids = "t2v", []
        elif len(names) >= 2:
            mode, ids = "flf2v", [character_ids[name] for name in names[:2]]
        else:
            mode, ids = "i2v", [character_ids[names[0]]]
        shots.append(
            {
                "description": shot["description"],
                "framing": shot["framing"],
                "cameraMovement": shot["cameraMovement"],
                "durationSeconds": shot["durationSeconds"],
                "mode": mode,
                "characterIds": ids,
            }
        )
    return shots


class EpisodeModel:
    """Derive the episode script from the transcript so reruns are identical."""

    name = "offline-m5-episode-fixture"

    def __init__(self, case):
        self.case = case
        self.text = case_text(case)
        self.assets: dict[str, list[str]] = {}
        self.delivery_polls = 0

    async def generate(self, messages, tools):
        start = max(i for i, message in enumerate(messages) if isinstance(message, HumanMessage))
        calls = {
            call["id"]: (call["name"], call.get("args") or {})
            for message in messages[start:]
            if isinstance(message, AIMessage)
            for call in message.tool_calls
        }
        results = [message for message in messages[start:] if isinstance(message, ToolMessage)]
        sequence = [message.name for message in results]

        def calls_message(*items):
            return AIMessage(
                content="",
                tool_calls=[
                    {"id": f"m5-{len(results)}-{index}", "name": name, "args": args}
                    for index, (name, args) in enumerate(items)
                ],
            )

        def data(name):
            for message in reversed(results):
                if message.name == name:
                    return parsed(message).get("data")
            return None

        def args_of(message):
            return calls.get(message.tool_call_id, ("", {}))[1]

        if "episode_plan" not in sequence:
            return calls_message(("episode_plan", {"text": self.text, "title": self.case.title}))
        plan = data("episode_plan")

        characters: dict[str, str] = {}
        style_id = None
        for message in results:
            if message.name != "bible_save" or not parsed(message).get("ok"):
                continue
            options = args_of(message)
            if options.get("kind") == "character":
                characters[options["name"]] = parsed(message)["data"]["id"]
            elif options.get("kind") == "style":
                style_id = parsed(message)["data"]["id"]
        if self.case.style and style_id is None:
            return calls_message(
                (
                    "bible_save",
                    {
                        "kind": "style",
                        "name": "雨夜现实主义",
                        "genre": ["短剧", "都市"],
                        "palette": "冷蓝与暖橙",
                        "cameraLanguage": "克制的手持与缓慢推进",
                    },
                )
            )
        missing = [character for character in self.case.characters if character[0] not in characters]
        if missing:
            name, appearance, _rgb = missing[0]
            return calls_message(
                (
                    "bible_save",
                    {
                        "kind": "character",
                        "name": name,
                        "appearance": appearance,
                        "referenceAssetIds": self.assets.get(name, []),
                    },
                )
            )
        if "episode_save" not in sequence:
            return calls_message(
                (
                    "episode_save",
                    {
                        "title": self.case.title,
                        "styleId": style_id,
                        "scenes": episode_scene_args(plan, characters),
                    },
                )
            )
        episode = data("episode_save")
        attempted = {
            args_of(message).get("sceneId") for message in results if message.name == "storyboard_save"
        }
        boards = {
            parsed(message)["data"]["storyboardId"]: parsed(message)["data"]
            for message in results
            if message.name == "storyboard_save" and parsed(message).get("ok")
        }
        remaining = [scene_id for scene_id in episode["sceneIds"] if scene_id not in attempted]
        if remaining:
            return calls_message(
                *[
                    (
                        "storyboard_save",
                        {
                            "sceneId": scene_id,
                            "title": plan["scenes"][episode["sceneIds"].index(scene_id)]["heading"],
                            "shots": scene_shot_args(
                                plan, episode["sceneIds"].index(scene_id) + 1, characters
                            ),
                        },
                    )
                    for scene_id in remaining
                ]
            )
        if "episode_generate" not in sequence:
            return calls_message(("episode_generate", {"episodeId": episode["episodeId"], "confirm": True}))
        generated = data("episode_generate")
        if "await_job" not in sequence:
            return calls_message(("await_job", {"jobIds": [entry["jobId"] for entry in generated["shots"]]}))
        # The Job set settles before the media Worker commits every local file; a film
        # assembly only sees delivered media, so poll the read view until all are in.
        view = data("episode_read")
        delivered = ((view or {}).get("generation") or {}).get("counts", {}).get("delivered", 0)
        if delivered < len(generated["shots"]) and self.delivery_polls < 40:
            self.delivery_polls += 1
            await asyncio.sleep(0.05)
            return calls_message(("episode_read", {"episodeId": episode["episodeId"]}))
        reads = {
            parsed(message)["data"]["storyboardId"]: parsed(message)["data"]
            for message in results
            if message.name == "storyboard_read" and parsed(message).get("ok")
        }
        pending_reads = [storyboard_id for storyboard_id in boards if storyboard_id not in reads]
        if pending_reads:
            return calls_message(
                *[("storyboard_read", {"storyboardId": storyboard_id}) for storyboard_id in pending_reads]
            )
        attempted_films = [message for message in results if message.name == "film_assemble"]
        if len(attempted_films) < len(boards):
            return calls_message(
                *[
                    (
                        "film_assemble",
                        {"storyboardId": storyboard_id, "expectedRevision": reads[storyboard_id]["revision"]},
                    )
                    for storyboard_id in boards
                ]
            )
        if self.case.review and "consistency_review" not in sequence:
            return calls_message(
                (
                    "consistency_review",
                    {
                        "episodeId": episode["episodeId"],
                        "characterId": characters[self.case.review],
                        "score": 5,
                        "note": "离线夹具人工评分：同一角色跨镜头引用同一素材。",
                    },
                )
            )
        failures = [parsed(message) for message in attempted_films if not parsed(message).get("ok")]
        if failures:
            return AIMessage(
                content=(
                    f"分集 {episode['episodeId']} 共 {episode['shotCount']} 个镜头，"
                    f"逐场成片有 {len(failures)} 个失败：{failures[0]['error']['code']}。"
                    "已交付镜头与历史成片保留，没有自动重试。"
                )
            )
        return AIMessage(
            content=(
                f"分集 {episode['episodeId']} 共 {episode['shotCount']} 个镜头、"
                f"{episode['targetDurationSeconds']} 秒，估算 {generated['totalEstimate']['amount']} CNY；"
                f"全部镜头已交付，{len(boards)} 个场记的成片均已在本地拼接完成。"
            )
        )


def base_config(args, case):
    configured = load_config()
    return replace(
        configured,
        home=args.home,
        api_key="offline-model-placeholder",
        video_mode="live",
        video_provider="wan",
        video_api_key="offline-wan-placeholder",
        video_workspace_id="offline-fixture",
        # One episode's plan, seven storyboards, eighteen jobs and seven films must all
        # remain in the scripted transcript; the production default is tuned for chat.
        context_bytes=2_000_000,
        kling_api_key=None,
        fal_api_key=None,
        redis_url=None,
        skills_root=None,
        mcp_config=None,
        mcp_local=False,
    )


def project_jobs(state, project_id):
    return [job for job in state["jobs"].values() if job["context"]["sessionId"] == project_id]


def scrub_job(raw):
    request = raw.get("request") or {}
    return {
        "id": raw.get("id"),
        "status": raw.get("status"),
        "providerTaskId": raw.get("providerTaskId"),
        "submitAttempts": raw.get("submitAttempts"),
        "queryState": raw.get("queryState"),
        "shotRef": raw.get("shotRef"),
        "mode": raw.get("mode"),
        "mediaAvailable": (raw.get("mediaAvailability") or {}).get("status") == "available",
        "mediaRefs": (raw.get("result") or {}).get("mediaRefs") or [],
        "request": {key: request.get(key) for key in ("provider", "model", "prompt", "spec")},
        "limits": (raw.get("cost") or {}).get("limits"),
        "estimate": (raw.get("cost") or {}).get("estimate"),
    }


def domain_snapshot(state, project_id):
    return {
        collection: {key: raw for key, raw in state[collection].items() if raw["projectId"] == project_id}
        for collection in ("episodes", "scenes", "characters", "styles")
    }


async def settle(service, project_id, clock, *, pause=0.01, timeout=180):
    deadline = time.monotonic() + timeout
    while True:
        worker = getattr(getattr(service, "media_worker", None), "_task", None)
        if worker is not None and worker.done() and not worker.cancelled():
            error = worker.exception()
            if error is not None:
                raise AppError("EVAL_WORKER_DIED", f"媒体 Worker 已停止：{error!r}")
        jobs = list(service.video_jobs.list(session_id=project_id))
        active = [job for job in jobs if job.status in ACTIVE_STATUSES and job.query_state != "paused"]
        # A live Job reports succeeded before its media Worker commits the local file;
        # film assembly needs the committed media reference, so wait for that too.
        pending_media = [
            job
            for job in jobs
            if job.status == "succeeded"
            and getattr(job, "mode", None) == "live"
            and not (getattr(job, "result", None) and job.result.media_refs)
        ]
        if not active and not pending_media:
            return
        if time.monotonic() > deadline:
            stack = []
            if worker is not None and not worker.done():
                stack = [f"{frame.f_code.co_name}:{frame.f_lineno}" for frame in worker.get_stack()]
            pending = [
                (
                    job.id,
                    job.status,
                    getattr(getattr(job, "download", None), "phase", None),
                )
                for job in jobs
                if job.status in ACTIVE_STATUSES
            ]
            raise AppError(
                "EVAL_TIMEOUT",
                f"{project_id} 的任务未在夹具时限内结束；worker={stack}；活跃任务={pending}。",
            )
        advance_clock(clock, service)
        await asyncio.sleep(pause)


async def drive(service, run_id, clock, *, timeout=300):
    waiter = asyncio.create_task(service.wait_for_run(run_id))
    try:
        async with asyncio.timeout(timeout):
            while not waiter.done():
                advance_clock(clock, service)
                await asyncio.sleep(0.01)
            return await waiter
    finally:
        if not waiter.done():
            waiter.cancel()
        await asyncio.gather(waiter, return_exceptions=True)


def tool_results(record):
    return [
        {
            "id": message["data"].get("tool_call_id"),
            "name": message["data"].get("name"),
            "result": message["data"].get("content"),
        }
        for message in record["messages"]
        if message["type"] == "tool"
    ]


def storyboard_refs(state, scenes):
    refs = {}
    for scene in scenes:
        board = state["storyboards"].get(scene["storyboardId"])
        if board is None:
            continue
        for shot in board["versions"][-1]["shots"]:
            refs[shot["id"]] = {
                "mode": shot["mode"],
                "characterRefs": shot.get("characterRefs") or [],
                "referenceAssetIds": shot.get("referenceAssetIds") or [],
            }
    return refs


async def run_episode(base, ledger, clock, service, item):
    case = CASES_BY_NAME[item["case"]]
    service.store.ensure_session(case.name)
    model = service.model
    for name, _appearance, rgb in case.characters:
        path = service.store.home / "fixtures" / f"{case.name}-{name}.png"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(solid_png(1280, 720, rgb))
        asset = service.video_jobs.assets.import_path(path, project_id=case.name)
        model.assets[name] = [asset["assetId"]]
    item["prompt"] = prompt_for(case)
    item["harnessSignature"] = service.runner().context_signature()
    record = await service.start(case.name, item["prompt"], f"m5-{case.name}")
    record = await drive(service, record["id"], clock)
    await settle(service, case.name, clock)
    state = service.store.snapshot()
    item["run"] = {
        "id": record["id"],
        "status": record["status"],
        "answer": record["answer"],
        "errorCode": record.get("errorCode"),
        "modelSteps": record["modelSteps"],
        "toolCalls": record["toolCalls"],
        "modelCalls": record.get("modelCalls") or [],
        "events": [
            {key: event[key] for key in ("type", "name", "runId", "sessionId") if key in event}
            for event in record["events"]
            if event["type"] != "assistant.delta"
        ],
        "policy": record.get("policy"),
        "resumable": bool(record.get("checkpointId")),
    }
    item["toolResults"] = tool_results(record)
    episode = next(raw for raw in state["episodes"].values() if raw["projectId"] == case.name)
    view = service.episode(case.name, episode["id"])
    item["episode"] = view
    item["episodeId"] = episode["id"]
    item["generation"] = view["generation"]
    item["episodeRecord"] = {
        "currentVersion": episode["currentVersion"],
        "revision": episode["revision"],
        "versions": len(episode["versions"]),
    }
    item["jobs"] = [scrub_job(job) for job in project_jobs(state, case.name)]
    item["refs"] = storyboard_refs(state, view["scenes"])
    item["scenes"] = [
        {
            "sceneId": scene["sceneId"],
            "storyboardId": scene["storyboardId"],
            "shotCount": scene["shotCount"],
            "durationSeconds": scene["durationSeconds"],
            "film": service.film(case.name, scene["storyboardId"])["film"] if scene["storyboardId"] else None,
        }
        for scene in view["scenes"]
    ]
    item["upstream"] = upstream_view(ledger, case)


def _error_code(action):
    try:
        result = action()
    except AppError as error:
        return error.code
    if isinstance(result, dict) and not result.get("ok"):
        return (result.get("error") or {}).get("code")
    return None


async def run_refusals(base, ledger, clock, service, item):
    """Every boundary is refused before anything is written or submitted."""
    case = CASES_BY_NAME[item["case"]]
    service.store.ensure_session(case.name)
    tools = service.tools
    item["sourceTooLong"] = _error_code(
        lambda: tools.execute(
            "episode_plan",
            {"text": "字" * 20001},
            store=service.store,
            project_id=case.name,
            operation_key=f"m5-{case.name}-long",
        )
    )
    try:
        plan_episode("字" * 20001)
        item["moduleTooLong"] = None
    except AppError as error:
        item["moduleTooLong"] = error.code
    plan = plan_episode(case_text(case), title=case.title)
    saved = tools.execute(
        "episode_save",
        {"title": case.title, "scenes": episode_scene_args(plan, {})},
        store=service.store,
        project_id=case.name,
        operation_key=f"m5-{case.name}-episode",
    )
    episode_id = saved["data"]["episodeId"]
    scene_ids = saved["data"]["sceneIds"]
    item["episodeId"] = episode_id
    item["unboundGenerate"] = _error_code(
        lambda: service.generate_episode(
            case.name,
            {
                "clientRequestId": f"m5-{case.name}-unbound",
                "episodeId": episode_id,
                "confirm": True,
            },
        )
    )
    item["confirmRequired"] = _error_code(
        lambda: service.generate_episode(
            case.name,
            {
                "clientRequestId": f"m5-{case.name}-no-confirm",
                "episodeId": episode_id,
                "confirm": False,
            },
        )
    )
    # Bind every scene, but make the first scene's storyboard disagree on shot count.
    wrong_count = tools.execute(
        "storyboard_save",
        {
            "sceneId": scene_ids[0],
            "title": "错误镜头数",
            "shots": [
                {"description": "镜头一", "framing": "wide", "durationSeconds": 5, "mode": "t2v"},
                {"description": "镜头二", "framing": "wide", "durationSeconds": 5, "mode": "t2v"},
            ],
        },
        store=service.store,
        project_id=case.name,
        operation_key=f"m5-{case.name}-wrong-count",
    )
    for index, scene_id in enumerate(scene_ids[1:], start=2):
        result = tools.execute(
            "storyboard_save",
            {
                "sceneId": scene_id,
                "title": plan["scenes"][index - 1]["heading"],
                "shots": scene_shot_args(plan, index, {}),
            },
            store=service.store,
            project_id=case.name,
            operation_key=f"m5-{case.name}-board-{index}",
        )
        if not result.get("ok"):
            raise AppError("EVAL_SEED_FAILED", f"第 {index} 个场景的分镜保存失败。")
    item["shotBudget"] = _error_code(
        lambda: service.generate_episode(
            case.name,
            {
                "clientRequestId": f"m5-{case.name}-budget",
                "episodeId": episode_id,
                "confirm": True,
            },
        )
    )
    board = service.store.snapshot()["storyboards"][wrong_count["data"]["storyboardId"]]
    revised = tools.execute(
        "storyboard_save",
        {
            "storyboardId": board["id"],
            "expectedVersion": board["currentVersion"],
            "title": "错误时长",
            "shots": [
                {
                    "id": wrong_count["data"]["shotIds"][0],
                    "description": "镜头一",
                    "framing": "wide",
                    "durationSeconds": 4,
                    "mode": "t2v",
                }
            ],
        },
        store=service.store,
        project_id=case.name,
        operation_key=f"m5-{case.name}-wrong-duration",
    )
    item["durationSaved"] = revised.get("ok", False)
    item["durationLimit"] = _error_code(
        lambda: service.generate_episode(
            case.name,
            {
                "clientRequestId": f"m5-{case.name}-duration",
                "episodeId": episode_id,
                "confirm": True,
            },
        )
    )
    # A second project proves a missing character reference refuses the storyboard save.
    asset_project = f"{case.name}-asset"
    service.store.ensure_session(asset_project)
    asset_plan = plan_episode(case_text(case), title=case.title)
    asset_episode = tools.execute(
        "episode_save",
        {"title": case.title, "scenes": episode_scene_args(asset_plan, {})},
        store=service.store,
        project_id=asset_project,
        operation_key=f"m5-{case.name}-asset-episode",
    )
    character = tools.execute(
        "bible_save",
        {"kind": "character", "name": "无素材角色", "appearance": "只有文字描述，没有参考图"},
        store=service.store,
        project_id=asset_project,
        operation_key=f"m5-{case.name}-character",
    )
    missing = tools.execute(
        "storyboard_save",
        {
            "sceneId": asset_episode["data"]["sceneIds"][0],
            "title": "缺素材",
            "shots": [
                {
                    "description": "镜头一",
                    "framing": "wide",
                    "durationSeconds": 5,
                    "mode": "i2v",
                    "characterIds": [character["data"]["id"]],
                }
            ],
        },
        store=service.store,
        project_id=asset_project,
        operation_key=f"m5-{case.name}-missing",
    )
    item["missingAsset"] = None if missing.get("ok") else missing["error"]["code"]
    state = service.store.snapshot()
    item["jobs"] = project_jobs(state, case.name)
    item["scenes"] = [raw for raw in state["scenes"].values() if raw["projectId"] == case.name]
    item["storyboards"] = [raw for raw in state["storyboards"].values() if raw["projectId"] == case.name]
    item["assetScenes"] = [raw for raw in state["scenes"].values() if raw["projectId"] == asset_project]
    item["assetSceneCount"] = len(asset_plan["scenes"])
    item["assetStoryboards"] = [
        raw for raw in state["storyboards"].values() if raw["projectId"] == asset_project
    ]
    item["upstream"] = upstream_view(ledger, case)


async def run_cap(base, ledger, clock, service, item):
    """Twenty jobs fill the frozen bucket; the next redo is refused as a whole.

    Delivery is deliberately not advanced: the bucket is about registration, and the
    two Workers are stopped first so this case proves exactly 20 registrations with
    zero submissions.
    """
    case = CASES_BY_NAME[item["case"]]
    service.store.ensure_session(case.name)
    await service.job_worker.stop()
    await service.media_worker.stop()
    plan = plan_episode(case_text(case), title=case.title)
    saved = service.tools.execute(
        "episode_save",
        {"title": case.title, "scenes": episode_scene_args(plan, {})},
        store=service.store,
        project_id=case.name,
        operation_key=f"m5-{case.name}-episode",
    )
    episode_id = saved["data"]["episodeId"]
    scene_ids = saved["data"]["sceneIds"]
    boards = {}
    for index, scene_id in enumerate(scene_ids, start=1):
        result = service.tools.execute(
            "storyboard_save",
            {
                "sceneId": scene_id,
                "title": plan["scenes"][index - 1]["heading"],
                "shots": scene_shot_args(plan, index, {}),
            },
            store=service.store,
            project_id=case.name,
            operation_key=f"m5-{case.name}-board-{index}",
        )
        boards[result["data"]["storyboardId"]] = result["data"]
    registered = service.generate_episode(
        case.name,
        {
            "clientRequestId": f"m5-{case.name}-register",
            "episodeId": episode_id,
            "confirm": True,
        },
    )
    item["episodeId"] = episode_id
    item["generation"] = registered
    item["shotCount"] = len(registered["shots"])
    state = service.store.snapshot()
    item["jobs"] = [scrub_job(job) for job in project_jobs(state, case.name)]
    storyboard_id = next(iter(boards))
    board = state["storyboards"][storyboard_id]
    shot_id = board["versions"][-1]["shots"][0]["id"]
    item["redoError"] = _error_code(
        lambda: service.redo_shot(
            case.name,
            shot_id,
            {
                "clientRequestId": f"m5-{case.name}-redo",
                "storyboardId": storyboard_id,
                "storyboardVersion": board["currentVersion"],
                "expectedRevision": None,
            },
        )
    )
    item["jobsAfterRedo"] = project_jobs(service.store.snapshot(), case.name)
    item["episodeView"] = service.episode(case.name, episode_id)
    item["upstream"] = upstream_view(ledger, case)


async def run_redo(base, ledger, clock, service, item):
    """One upstream failure is isolated; two redos append film versions v1 and v2."""
    case = CASES_BY_NAME[item["case"]]
    service.store.ensure_session(case.name)
    plan = plan_episode(case_text(case), title=case.title)
    saved = service.tools.execute(
        "episode_save",
        {"title": case.title, "scenes": episode_scene_args(plan, {})},
        store=service.store,
        project_id=case.name,
        operation_key=f"m5-{case.name}-episode",
    )
    episode_id = saved["data"]["episodeId"]
    scene_id = saved["data"]["sceneIds"][case.scene_index - 1]
    board = service.tools.execute(
        "storyboard_save",
        {
            "sceneId": scene_id,
            "title": plan["scenes"][case.scene_index - 1]["heading"],
            "shots": scene_shot_args(plan, case.scene_index, {}),
        },
        store=service.store,
        project_id=case.name,
        operation_key=f"m5-{case.name}-board",
    )
    shot_ids = board["data"]["shotIds"]
    registered = service.generate_shots(
        case.name,
        {
            "clientRequestId": f"m5-{case.name}-generate",
            "storyboardId": board["data"]["storyboardId"],
            "storyboardVersion": board["data"]["version"],
            "shotIds": shot_ids,
            "confirm": True,
        },
    )
    item["episodeId"] = episode_id
    item["generation"] = registered
    await settle(service, case.name, clock)
    first_jobs = [scrub_job(job) for job in project_jobs(service.store.snapshot(), case.name)]
    item["firstJobs"] = first_jobs
    failed = [job for job in first_jobs if job["status"] == "failed"]
    failed_shot = failed[0]["shotRef"]["shotId"] if failed else shot_ids[0]
    board_now = service.store.snapshot()["storyboards"][board["data"]["storyboardId"]]
    service.redo_shot(
        case.name,
        failed_shot,
        {
            "clientRequestId": f"m5-{case.name}-redo-failed",
            "storyboardId": board_now["id"],
            "storyboardVersion": board_now["currentVersion"],
            "expectedRevision": None,
        },
    )
    await settle(service, case.name, clock)
    after_fix = service.store.snapshot()
    board_now = after_fix["storyboards"][board["data"]["storyboardId"]]
    film_one = await service.assemble_film(
        case.name,
        {
            "clientRequestId": f"m5-{case.name}-film-1",
            "storyboardId": board_now["id"],
            "expectedRevision": board_now["revision"],
        },
    )
    other_shot = next(shot_id for shot_id in shot_ids if shot_id != failed_shot)
    service.redo_shot(
        case.name,
        other_shot,
        {
            "clientRequestId": f"m5-{case.name}-redo-other",
            "storyboardId": board_now["id"],
            "storyboardVersion": board_now["currentVersion"],
            "expectedRevision": None,
        },
    )
    await settle(service, case.name, clock)
    after_second = service.store.snapshot()
    board_final = after_second["storyboards"][board["data"]["storyboardId"]]
    film_two = await service.assemble_film(
        case.name,
        {
            "clientRequestId": f"m5-{case.name}-film-2",
            "storyboardId": board_final["id"],
            "expectedRevision": board_final["revision"],
        },
    )
    item["filmOne"] = film_one
    item["filmTwo"] = film_two
    item["failedShot"] = failed_shot
    item["otherShot"] = other_shot
    item["jobs"] = [scrub_job(job) for job in project_jobs(after_second, case.name)]
    item["shotVersions"] = board_final["shotVersions"]
    item["episodeView"] = service.episode(case.name, episode_id)
    item["upstream"] = upstream_view(ledger, case)


def upstream_view(ledger, case):
    record = ledger.case(case.name)
    return {
        "submitCalls": len(record["submissions"]),
        "downloadCalls": len(record["downloads"]),
        "taskIds": record["taskIds"],
        "queryPaths": record["queryPaths"],
        "downloadHosts": sorted({entry["host"] for entry in record["downloads"]}),
        "authorization": record["authorization"],
        "submissions": record["submissions"],
    }


def film_checks(service, scene):
    film = scene["film"]
    if not film:
        return False
    latest = film["latest"]
    raw = service.store.snapshot()["media"].get(latest["outputMediaId"])
    path = service.store.home / raw["relativePath"] if raw else None
    return bool(
        film["currentVersion"] == 1
        and latest["mediaAvailable"]
        and latest["width"] == 1280
        and latest["height"] == 720
        and latest["hasAudio"] is True
        and len(latest["inputs"]) == scene["shotCount"]
        and len(latest["timeline"]) == scene["shotCount"]
        and abs(latest["durationSeconds"] - sum(entry["durationSeconds"] for entry in latest["timeline"]))
        < 0.2
        and path is not None
        and path.is_file()
        and path.stat().st_size == latest["sizeBytes"]
        and hashlib.sha256(path.read_bytes()).hexdigest() == latest["sha256"]
    )


def expected_episode_amount(plan, shot_count):
    total = sum(shot["durationSeconds"] for shot in plan["shots"])
    return f"{total * 0.60:.2f}"


def all_checks_pass(checks):
    return bool(checks) and all(checks.values())


def grade(case, item, service):
    """One independent check per promised behaviour; no lenient fallbacks."""
    checks = {}
    run = item.get("run") or {}
    results = item.get("toolResults") or []
    names = [entry["name"] for entry in results]
    upstream = item.get("upstream") or {}
    jobs = item.get("jobs") or []
    episode = item.get("episode") or {}
    scenes = item.get("scenes") or []
    if case.kind == "episode":
        generated = next(
            (json.loads(entry["result"])["data"] for entry in results if entry["name"] == "episode_generate"),
            None,
        )
        plan = next(
            (json.loads(entry["result"])["data"] for entry in results if entry["name"] == "episode_plan"),
            None,
        )
        checks["run_completed"] = run.get("status") == "completed" and not run.get("errorCode")
        checks["tool_chain"] = bool(
            plan
            and generated
            and names[:1] == ["episode_plan"]
            and names.count("storyboard_save") == len(plan["scenes"])
            and names.count("storyboard_read") == len(plan["scenes"])
            and names.count("film_assemble") == len(plan["scenes"])
            and "episode_save" in names
            and "episode_generate" in names
            and "await_job" in names
        )
        checks["plan_budget"] = bool(
            plan
            and plan["shotBudget"]["shotCount"] == episode["episode"]["shotBudget"]["shotCount"]
            and plan["shotBudget"]["targetDurationSeconds"] == episode["episode"]["targetDurationSeconds"]
            and 10 <= plan["shotBudget"]["shotCount"] <= 20
            and 60 <= plan["shotBudget"]["targetDurationSeconds"] <= 90
            and sum(scene["shotCount"] for scene in plan["scenes"]) == plan["shotBudget"]["shotCount"]
            and sum(scene["durationSeconds"] for scene in plan["scenes"])
            == plan["shotBudget"]["targetDurationSeconds"]
        )
        checks["scenes_bound_one_to_one"] = bool(
            len(episode.get("scenes") or []) == len(plan["scenes"])
            and all(scene["storyboardId"] for scene in episode["scenes"])
            and len({scene["storyboardId"] for scene in episode["scenes"]}) == len(plan["scenes"])
        )
        expected_amount = expected_episode_amount(plan, plan["shotBudget"]["shotCount"])
        checks["generation_complete"] = bool(
            generated
            and len(generated["shots"]) == plan["shotBudget"]["shotCount"]
            and all(entry["jobId"] for entry in generated["shots"])
            and generated["totalEstimate"]["amount"] == expected_amount
            and generated["limits"]["usedAmount"] == expected_amount
            and generated["limits"]["scope"] == "episode"
            and generated["limits"]["maxNewJobsPerRun"] == 20
            and generated["limits"]["maxRunCost"] == "60.00"
            and generated["limits"]["usedNewJobs"] == plan["shotBudget"]["shotCount"]
        )
        checks["jobs_delivered"] = bool(
            len(jobs) == plan["shotBudget"]["shotCount"]
            and all(
                job["status"] == "succeeded" and job["mediaAvailable"] and job["submitAttempts"] == 1
                for job in jobs
            )
        )
        checks["provider_contract"] = bool(
            upstream.get("submitCalls") == plan["shotBudget"]["shotCount"]
            and upstream.get("downloadHosts") == [WAN_RESULT_HOST]
            and set(upstream.get("authorization") or []) == {"bearer"}
            and all(
                entry["model"] in {WAN_MODEL, WAN_I2V_MODEL} for entry in upstream.get("submissions") or []
            )
        )
        checks["scene_films"] = bool(
            len(scenes) == len(plan["scenes"]) and all(film_checks(service, scene) for scene in scenes)
        )
        checks["episode_not_rewritten"] = bool(
            item.get("episodeRecord", {}).get("currentVersion") == 1
            and item.get("episodeRecord", {}).get("revision") == (1 if case.review else 0)
            and item.get("episodeRecord", {}).get("versions") == 1
        )
        if case.characters:
            consistency = episode.get("consistency") or {}
            refs = item.get("refs") or {}
            checks["character_references_frozen"] = bool(
                any(entry["mode"] == "i2v" for entry in refs.values())
                and all(
                    entry["characterRefs"] and entry["referenceAssetIds"]
                    for entry in refs.values()
                    if entry["mode"] in {"i2v", "flf2v"}
                )
            )
            checks["consistency_evidence"] = bool(
                consistency
                and any(
                    entry["name"] == case.review
                    and entry["consistent"]
                    and entry["shotCount"] >= 3
                    and len(entry["assetIds"]) == 1
                    for entry in consistency["characters"]
                )
            )
            checks["review_recorded"] = bool(
                consistency
                and any(
                    review["score"] == 5 and review["consistent"] is True for review in consistency["reviews"]
                )
            )
        return checks
    if case.kind == "refusals":
        checks["source_too_long"] = item.get("sourceTooLong") in {
            "INVALID_ARGUMENTS",
            "EPISODE_SOURCE_TOO_LONG",
        }
        checks["module_source_too_long"] = item.get("moduleTooLong") == "EPISODE_SOURCE_TOO_LONG"
        checks["unbound_scene"] = item.get("unboundGenerate") == "SCENE_CONFLICT"
        checks["confirmation_required"] = item.get("confirmRequired") == "CONFIRMATION_REQUIRED"
        checks["wrong_shot_count"] = item.get("shotBudget") == "EPISODE_SHOT_BUDGET"
        checks["wrong_duration"] = item.get("durationLimit") == "EPISODE_DURATION_LIMIT"
        checks["missing_character_asset"] = item.get("missingAsset") in {
            "CHARACTER_ASSET_MISSING",
            "CHARACTER_NOT_FOUND",
        }
        checks["no_jobs"] = not item.get("jobs")
        checks["bound_boards_kept"] = bool(
            len(item.get("scenes") or []) == 9 and len(item.get("storyboards") or []) == 9
        )
        checks["asset_project_clean"] = bool(
            len(item.get("assetScenes") or []) == item.get("assetSceneCount")
            and not item.get("assetStoryboards")
        )
        checks["nothing_submitted"] = upstream.get("submitCalls") == 0
        return checks
    if case.kind == "cap":
        checks["twenty_jobs_registered"] = bool(
            item.get("shotCount") == 20
            and len(item.get("jobs") or []) == 20
            and len({job["id"] for job in item.get("jobs") or []}) == 20
        )
        checks["episode_bucket_full"] = bool(
            item.get("generation")
            and item["generation"]["limits"]["usedNewJobs"] == 20
            and item["generation"]["limits"]["remainingAmount"] == "6.00"
            and item["generation"]["limits"]["maxNewJobsPerRun"] == 20
        )
        checks["redo_refused_by_bucket"] = item.get("redoError") == "EPISODE_COST_LIMIT"
        checks["no_job_after_refusal"] = len(item.get("jobsAfterRedo") or []) == 20
        checks["nothing_submitted"] = upstream.get("submitCalls") == 0
        checks["twenty_pending"] = bool(
            len(item.get("jobs") or []) == 20
            and all(job["status"] == "pending_submit" for job in item["jobs"])
        )
        return checks
    checks["failure_isolated"] = bool(
        len(item.get("firstJobs") or []) == len(item.get("generation", {}).get("shots") or [])
        and len([job for job in item.get("firstJobs") or [] if job["status"] == "failed"]) == 1
        and len([job for job in item.get("firstJobs") or [] if job["status"] == "succeeded"])
        == len(item.get("generation", {}).get("shots") or []) - 1
    )
    checks["first_film_v1"] = bool(
        item.get("filmOne")
        and item["filmOne"]["currentVersion"] == 1
        and item["filmOne"]["latest"]["mediaAvailable"]
        and len(item["filmOne"]["latest"]["inputs"]) == len(item.get("generation", {}).get("shots") or [])
    )
    film_two = item.get("filmTwo") or {}
    checks["second_film_v2"] = bool(
        film_two
        and film_two["currentVersion"] == 2
        and film_two["latest"]["mediaAvailable"]
        and len(film_two["latest"]["inputs"]) == len(item.get("generation", {}).get("shots") or [])
    )
    versions = item.get("shotVersions") or []
    checks["redo_only_target_shots"] = bool(
        len([entry for entry in versions if entry["shotId"] == item.get("failedShot")]) == 2
        and len([entry for entry in versions if entry["shotId"] == item.get("otherShot")]) == 2
    )
    latest_by_shot = {}
    for entry in versions:
        latest_by_shot[entry["shotId"]] = entry["id"]
    checks["film_two_uses_latest_versions"] = bool(
        film_two
        and all(
            entry["shotVersionId"] == latest_by_shot[entry["shotId"]]
            for entry in film_two["latest"]["inputs"]
        )
    )
    checks["submits_accounted"] = upstream.get("submitCalls") == len(item.get("jobs") or [])
    return checks


async def run_case(args, case, ledger):
    clock = SuiteClock()
    case_home = args.home / case.name
    fixtures = make_fixtures(case, ledger, clock, case_home)
    plan = plan_episode(case_text(case), title=case.title)
    fixtures["media"].prewarm([shot["durationSeconds"] for shot in plan["shots"]])
    base = replace(base_config(args, case), home=case_home)
    item = {"case": case.name, "kind": case.kind}
    model = EpisodeModel(case) if case.kind == "episode" else None
    with patches(fixtures, clock):
        async with ApplicationService.open(
            base,
            model=model,
            policy=RunPolicy(max_steps=80, max_tool_calls=400, timeout_seconds=300),
        ) as service:
            if case.kind == "episode":
                await run_episode(base, ledger, clock, service, item)
            elif case.kind == "refusals":
                await run_refusals(base, ledger, clock, service, item)
            elif case.kind == "cap":
                await run_cap(base, ledger, clock, service, item)
            else:
                await run_redo(base, ledger, clock, service, item)
            item["checks"] = grade(case, item, service)
            item["upstream"] = upstream_view(ledger, case)
    return item


def totals(report):
    cases = report.get("cases") or []
    planned = report.get("plannedCases", len(CASES))
    calls = [call for item in cases for call in ((item.get("run") or {}).get("modelCalls") or [])]
    jobs = [job for item in cases for job in item.get("jobs") or []]
    films = [scene["film"] for item in cases for scene in item.get("scenes") or [] if scene.get("film")]
    films += [film for item in cases for film in (item.get("filmOne"), item.get("filmTwo")) if film]
    return {
        "plannedCases": planned,
        "finishedCases": len(cases),
        "passedCases": len([item for item in cases if all_checks_pass(item.get("checks"))]),
        "modelCalls": len(calls),
        "toolCalls": sum((item.get("run") or {}).get("toolCalls") or 0 for item in cases),
        "submitCalls": sum((item.get("upstream") or {}).get("submitCalls") or 0 for item in cases),
        "downloadCalls": sum((item.get("upstream") or {}).get("downloadCalls") or 0 for item in cases),
        "registeredJobs": len(jobs),
        "deliveredClips": len(
            [job for job in jobs if job.get("status") == "succeeded" and job.get("mediaAvailable")]
        ),
        "filmVersions": sum(len(film.get("versions") or []) for film in films),
        "redos": sum(
            max(
                0,
                len(item.get("jobs") or []) - len((item.get("generation") or {}).get("shots") or []),
            )
            for item in cases
            if item.get("kind") in {"episode", "redo"}
        ),
        "realProviderCalls": 0,
        "realModelCalls": 0,
    }


async def run_suite(args):
    if getattr(args, "live", False):
        raise AppError(
            "EVAL_LIVE_UNAVAILABLE",
            "M5 E4 未获真实付费样本的模型、次数与金额授权；--live 未执行，未发起任何请求。",
        )
    if args.max_model_calls is None:
        args.max_model_calls = 60
    if not 1 <= args.max_model_calls <= 100:
        raise AppError("EVAL_BUDGET_INVALID", "模型调用上限须为1～100。")
    if args.output.exists():
        raise AppError("EVAL_OUTPUT_EXISTS", "输出文件已存在，请使用新路径保留先前证据。")
    home = args.home.resolve()
    if home.exists() and any(home.iterdir()):
        raise AppError("EVAL_HOME_NOT_EMPTY", "评测需要新的空数据目录，已有数据未修改。")
    if not FIXTURE.is_file():
        raise AppError("EVAL_FIXTURE_MISSING", "缺少本地测试媒体夹具，离线评测未启动。")
    ledger = Ledger(home)
    started = time.monotonic()
    selected = [case for case in CASES if not args.only or case.name in args.only]
    report = {
        "schemaVersion": 1,
        "suiteId": suite_identity(CASES),
        "mode": "offline-fixture",
        "status": "incomplete",
        "startedAt": now(),
        "dataDirectory": str(home),
        "maxModelCalls": args.max_model_calls,
        "plannedCases": len(selected),
        "cases": [],
        "limitations": (
            "受控 HTTP 与本地测试 MP4 的固定脚本用例：证明分集/场记/分镜/批量登记/等待/逐场成片的"
            "应用代码路径与拒绝边界，不证明真实账号、真实 CDN、真实编码或账单。真实付费样本未执行。"
        ),
    }

    def persist():
        report["totals"] = totals(report)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_json(args.output, report)

    persist()
    print(
        json.dumps(
            {"suite": report["suiteId"], "mode": report["mode"], "dataDirectory": str(home)},
            ensure_ascii=False,
        ),
        flush=True,
    )
    try:
        for case in selected:
            if totals(report)["modelCalls"] >= args.max_model_calls:
                report["status"] = "budget-exhausted"
                break
            item = await run_case(args, case, ledger)
            report["cases"].append(item)
            persist()
            print(
                json.dumps(
                    {
                        "case": case.name,
                        "passed": all_checks_pass(item["checks"]),
                        "failedChecks": [key for key, value in item["checks"].items() if not value],
                        "modelSteps": (item.get("run") or {}).get("modelSteps"),
                        "toolCalls": (item.get("run") or {}).get("toolCalls"),
                        "jobs": len(item.get("jobs") or []),
                        "films": len([scene for scene in item.get("scenes") or [] if scene.get("film")]),
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
    root.add_argument("--max-model-calls", type=int, default=60)
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
        help="M5 E4 未获真实付费样本授权；本参数会拒绝执行且不发起请求",
    )
    return root


def main():
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    load_dotenv(".env", override=False)
    args = parser().parse_args()
    try:
        report = asyncio.run(run_suite(args))
    except Exception as error:
        safe = public_error(error)
        print(f"[{safe.code}] {safe}", file=sys.stderr)
        raise SystemExit(1) from None
    raise SystemExit(0 if report["status"] == "passed" else 1)


if __name__ == "__main__":
    main()
