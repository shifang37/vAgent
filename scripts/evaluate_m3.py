"""M3 E4 application acceptance: one prompt to N shots to a locally assembled film.

Default mode is fully offline. The real ApplicationService, Agent Runner, storyboard
tools, provider registration, Job/Media Workers, film tools, headless plan/authorize/
execute flow and the bundled FFmpeg all run through production code paths. Kling, fal
and Wan are served by ``httpx.MockTransport``; every delivered clip is the committed
local MP4 fixture. No DNS/TLS connection, account, credential or paid task is used.

``--live`` is deliberately unavailable in this package: M3 E4 ships no new paid
sample without an explicit model/count/amount approval (M3 contracts decision 3).
The zero-cost real-data assembly is run from the repository outside the wheel smoke
and recorded separately in ``docs/M3_ACCEPTANCE.md``.
"""

import argparse
import asyncio
import copy
import hashlib
import json
import re
import sys
import time
import traceback
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

import httpx
from dotenv import load_dotenv
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

import vagent.headless as headless
from vagent.application import ApplicationService
from vagent.config import load_config
from vagent.errors import AppError, public_error
from vagent.runner import RunPolicy
from vagent.storage import atomic_write_json, now
from vagent.video.contracts import (
    FAL_MODEL,
    KLING_MODEL,
    WAN_MODEL,
)
from vagent.video.jobs import JobService
from vagent.video.media_http import MediaHttpClient
from vagent.video.media_worker import MediaWorker
from vagent.video.providers.fal import FalAdapter
from vagent.video.providers.kling import KlingAdapter
from vagent.video.providers.wan import WanAdapter
from vagent.video.worker import JobWorker
from vagent.waiting import ToolExecutionContext
from vagent.web import create_app

ROOT = Path(__file__).resolve().parent.parent
FIXTURE = ROOT / "tests" / "fixtures" / "m1c" / "sample-720p.mp4"
KLING_RESULT_ORIGIN = "https://cdn.example-provider.test"
FAL_RESULT_ORIGIN = "https://v3.fal.media"
WAN_RESULT_HOST = "dashscope-result-sh.oss-accelerate.aliyuncs.com"
ACTIVE_STATUSES = {"pending_submit", "submitting", "queued", "running", "downloading"}
FILM_TOOL_CHAIN = [
    "video_capabilities",
    "storyboard_save",
    "shot_generate",
    "await_job",
    "storyboard_read",
    "film_assemble",
]
CLIP_SECONDS = 5.021333
TOTAL_SECONDS = 10.021289
PROMPT_MARKER = re.compile(r"【M3 E4 ([a-z-]+)】")
REFUSAL_TOKENS = {
    "FILM_INPUT_MISSING",
    "FILM_INPUT_NOT_READY",
    "FILM_INPUT_INCOMPATIBLE",
    "FILM_MEDIA_INVALID",
    "VERSION_CONFLICT",
}


@dataclass(frozen=True)
class Case:
    """One scripted acceptance scenario."""

    name: str
    kind: str  # conversation | refusals | headless-plan | headless-deliver
    provider: str
    shots: tuple[str, ...]
    redo: bool = False


CASES = (
    Case(
        "kling-film",
        "conversation",
        "kling",
        ("远景：雨夜街道，霓虹与伞影", "中景：推门入店，暖光落在吧台"),
        redo=True,
    ),
    Case(
        "fal-film",
        "conversation",
        "fal",
        ("远景：雨夜街道，镜头缓慢推进", "特写：杯口热气与拉花"),
    ),
    Case(
        "wan-film",
        "conversation",
        "wan",
        ("远景：雨夜街道，霓虹与伞影", "中景：推门入店，暖光落在吧台"),
    ),
    Case("film-refusals", "refusals", "wan", ("镜头一：雨夜街道", "镜头二：推门入店")),
    Case("headless-plan", "headless-plan", "kling", ("镜头一：雨夜街道", "镜头二：推门入店")),
    Case("headless-deliver", "headless-deliver", "kling", ("镜头一：雨夜街道", "镜头二：推门入店")),
)
CASES_BY_NAME = {case.name: case for case in CASES}


def prompt_for(case: Case) -> str:
    """A stable, provider-neutral user request; the offline model derives its script."""
    shots = "；".join(f"镜头{index} {item}" for index, item in enumerate(case.shots, start=1))
    if case.kind == "conversation":
        return (
            f"【M3 E4 {case.name}】这是 {case.provider} 端到端验收。"
            "先核对视频能力，保存一个两镜头分镜（都是 t2v、5 秒）："
            f"{shots}。为两个镜头一次性登记真实任务（显式确认），用一次 await_job 等待交付；"
            "然后读取分镜 revision 并调用 film_assemble 在本地拼成单一成片，"
            "最后如实汇报真实 jobId、逐镜头与合计估算、成片版本、大小与本地路径。"
            "不要重做镜头、不要修改文本产物、不要重复提交、不要编造媒体。"
        )
    if case.kind == "refusals":
        return f"【M3 E4 {case.name}】这是本地拼接拒绝路径验收。保存两镜头分镜并登记交付。"
    if case.kind == "headless-plan":
        return (
            f"【M3 E4 {case.name}】这是 headless 规划验收。"
            f"保存一个两镜头分镜草案：{shots}。只做草案，不登记视频任务。"
        )
    return (
        f"【M3 E4 {case.name}】这是 headless 显式授权验收。"
        f"保存一个两镜头分镜草案：{shots}。只做草案，不登记视频任务。"
    )


def suite_identity(cases) -> str:
    return hashlib.sha256(
        json.dumps(
            {
                "version": 1,
                "cases": [vars(case) | {"prompt": prompt_for(case)} for case in cases],
                "fixtureSha256": hashlib.sha256(fixture_bytes()).hexdigest(),
            },
            sort_keys=True,
            ensure_ascii=False,
        ).encode()
    ).hexdigest()[:16]


_FIXTURE_BYTES = None


def fixture_bytes():
    global _FIXTURE_BYTES
    if _FIXTURE_BYTES is None:
        _FIXTURE_BYTES = FIXTURE.read_bytes()
    return _FIXTURE_BYTES


class SuiteClock:
    def __init__(self):
        self.value = datetime.now(UTC)

    def __call__(self):
        return self.value

    def advance(self, seconds):
        self.value += timedelta(seconds=seconds)


def advance_clock(clock, service):
    """Move the fixture clock to the next persisted deadline (one rate-limit second).

    The live Worker allows one provider call per second per Store, so a clock that
    only jumps to Job deadlines could stall before every Job of a batch is due.
    """
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
        self.path = Path(home) / "m3-fixture.json"
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            self.data = raw if isinstance(raw, dict) else {}
        except (OSError, ValueError):
            self.data = {}

    def case(self, name):
        return self.data.setdefault(
            name,
            {
                "submissions": [],
                "taskIds": [],
                "queryPaths": [],
                "downloads": [],
                "authorization": [],
            },
        )

    def save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_json(self.path, self.data)


class KlingFixture:
    """A stateful controlled Kling API: submit, query, exact task binding."""

    def __init__(self, case, ledger):
        self.case, self.ledger = case, ledger
        self.tasks = {}

    def handle(self, request):
        record = self.ledger.case(self.case.name)
        if request.method == "POST":
            body = json.loads(request.content)
            record["submissions"].append(body)
            task_id = f"m3-kling-{self.case.name}-{len(record['submissions'])}"
            self.tasks[task_id] = body["prompt"]
            record["taskIds"].append(task_id)
            record["authorization"].append(
                "bearer" if request.headers.get("authorization", "").startswith("Bearer ") else "other"
            )
            self.ledger.save()
            return httpx.Response(200, json={"code": 0, "data": {"id": task_id}})
        task_id = parse_qs(urlsplit(str(request.url)).query)["task_ids"][0]
        record["queryPaths"].append(request.url.path)
        if task_id not in self.tasks:
            return httpx.Response(200, json={"code": 0, "data": []})
        self.ledger.save()
        return httpx.Response(
            200,
            json={
                "code": 0,
                "data": [
                    {
                        "id": task_id,
                        "status": "succeeded",
                        "outputs": [
                            {"type": "video", "url": f"{KLING_RESULT_ORIGIN}/{task_id}.mp4?sig=private"}
                        ],
                        "usage": {"duration": 5},
                    }
                ],
            },
        )


class FalFixture:
    """A stateful controlled fal queue: the app-root result path is the only read."""

    def __init__(self, case, ledger):
        self.case, self.ledger = case, ledger
        self.tasks = {}

    def handle(self, request):
        record = self.ledger.case(self.case.name)
        path = request.url.path
        if request.method == "POST":
            body = json.loads(request.content)
            record["submissions"].append(body)
            request_id = f"m3-fal-{self.case.name}-{len(record['submissions'])}"
            self.tasks[request_id] = body["prompt"]
            record["taskIds"].append(request_id)
            record["authorization"].append(
                "key" if request.headers.get("authorization", "").startswith("Key ") else "other"
            )
            self.ledger.save()
            return httpx.Response(200, json={"request_id": request_id, "status": "IN_QUEUE"})
        record["queryPaths"].append(path)
        if path.endswith("/status"):
            request_id = path.split("/")[-2]
            self.ledger.save()
            return httpx.Response(200, json={"request_id": request_id, "status": "COMPLETED"})
        request_id = path.rsplit("/", 1)[-1]
        self.ledger.save()
        return httpx.Response(
            200, json={"video": {"url": f"{FAL_RESULT_ORIGIN}/files/{request_id}.mp4?token=private"}}
        )


class WanFixture:
    """The frozen Wan v2 protocol, served from the local fixture only."""

    def __init__(self, case, ledger):
        self.case, self.ledger = case, ledger
        self.statuses = {}

    def handle(self, request):
        record = self.ledger.case(self.case.name)
        if request.method == "POST":
            body = json.loads(request.content)
            record["submissions"].append(body)
            task_id = f"m3-wan-{self.case.name}-{len(record['submissions'])}"
            self.statuses[task_id] = "SUCCEEDED"
            record["taskIds"].append(task_id)
            record["authorization"].append(
                "bearer" if request.headers.get("authorization", "").startswith("Bearer ") else "other"
            )
            self.ledger.save()
            return httpx.Response(
                200,
                json={
                    "request_id": f"m3-wan-submit-{len(record['submissions'])}",
                    "output": {"task_id": task_id, "task_status": "PENDING"},
                },
            )
        task_id = request.url.path.rsplit("/", 1)[-1]
        record["queryPaths"].append(request.url.path)
        status = self.statuses.get(task_id, "SUCCEEDED")
        self.ledger.save()
        return httpx.Response(
            200,
            json={
                "request_id": f"m3-wan-query-{len(record['queryPaths'])}",
                "output": {
                    "task_id": task_id,
                    "task_status": status,
                    "video_url": f"https://{WAN_RESULT_HOST}/m3-{task_id}.mp4?Signature=m3-fixture",
                },
                "usage": {
                    "duration": 5,
                    "output_video_duration": 5,
                    "input_video_duration": 0,
                    "video_count": 1,
                    "SR": 720,
                    "ratio": "16:9",
                },
            },
        )


class MediaFixture:
    """Serves the committed local MP4 to any bound provider origin."""

    def __init__(self, case, ledger):
        self.case, self.ledger = case, ledger
        self.data = fixture_bytes()

    def handle(self, request):
        record = self.ledger.case(self.case.name)
        record["downloads"].append({"host": request.url.host})
        self.ledger.save()
        return httpx.Response(
            200,
            content=self.data,
            headers={"Content-Type": "video/mp4", "Content-Length": str(len(self.data))},
        )


def scripted(results, name):
    """The parsed tool results of one tool inside the current turn, in call order."""
    return [json.loads(message.content) for message in results if message.name == name]


class FixtureModel:
    """Derive the film-delivery script from the transcript, so reruns are identical."""

    name = "offline-m3-fixture"

    async def generate(self, messages, tools):
        start = max(i for i, message in enumerate(messages) if isinstance(message, HumanMessage))
        prompt = messages[start].content
        case = CASES_BY_NAME[PROMPT_MARKER.search(prompt).group(1)]
        results = [message for message in messages[start:] if isinstance(message, ToolMessage)]
        sequence = [message.name for message in results]
        latest = {message.name: json.loads(message.content) for message in results}

        def calls(*items):
            return AIMessage(
                content="",
                tool_calls=[
                    {"id": f"fixture-{len(results)}-{index}", "name": name, "args": args}
                    for index, (name, args) in enumerate(items)
                ],
            )

        if "video_capabilities" not in sequence:
            return calls(("video_capabilities", {}))
        if "storyboard_save" not in sequence:
            return calls(
                (
                    "storyboard_save",
                    {
                        "title": f"{case.name} 分镜",
                        "shots": [
                            {
                                "description": description,
                                "framing": "wide" if index == 1 else "medium",
                                "durationSeconds": 5,
                                "mode": "t2v",
                            }
                            for index, description in enumerate(case.shots, start=1)
                        ],
                    },
                )
            )
        saved = latest["storyboard_save"]["data"]
        if "shot_generate" not in sequence:
            return calls(
                (
                    "shot_generate",
                    {
                        "storyboardId": saved["storyboardId"],
                        "storyboardVersion": saved["version"],
                        "shotIds": saved["shotIds"],
                        "confirm": True,
                    },
                )
            )
        generated = latest["shot_generate"]
        if not generated.get("ok"):
            raise AppError(
                "FIXTURE_TOOL_FAILED",
                f"离线夹具登记失败：{generated['error']['code']}",
            )
        if "await_job" not in sequence:
            return calls(
                (
                    "await_job",
                    {"jobIds": [entry["jobId"] for entry in generated["data"]["shots"]]},
                )
            )
        if "storyboard_read" not in sequence:
            return calls(("storyboard_read", {"storyboardId": saved["storyboardId"]}))
        board = latest["storyboard_read"]["data"]
        if "film_assemble" not in sequence:
            return calls(
                (
                    "film_assemble",
                    {"storyboardId": saved["storyboardId"], "expectedRevision": board["revision"]},
                )
            )
        film = latest["film_assemble"]
        if film.get("ok"):
            data = film["data"]
            latest_version = data["latest"]
            return AIMessage(
                content=(
                    f"两个镜头都已本地交付：{saved['shotIds'][0]}、{saved['shotIds'][1]}；"
                    f"合计估算 {generated['data']['totalEstimate']['amount']} "
                    f"{generated['data']['totalEstimate']['currency']}。"
                    f"已在本地拼接成片 v{data['currentVersion']}，"
                    f"{latest_version['sizeBytes']} 字节，"
                    f"SHA-256 {latest_version['sha256']}，可播放、可拖动、可下载；"
                    f"媒体 {latest_version['outputMediaId']}。"
                )
            )
        return AIMessage(
            content=(
                f"成片拼接被拒绝：{film['error']['code']}——{film['error']['message']}。"
                "已交付镜头与历史成片保持不变，没有自动重试。"
            )
        )


class PlanModel:
    """One storyboard_save turn, then a final answer; used by the headless cases."""

    name = "offline-m3-headless-fixture"

    def __init__(self, shots):
        self.shots = shots
        self.calls = 0

    async def generate(self, messages, tools):
        self.calls += 1
        if self.calls == 1:
            return AIMessage(
                content="",
                tool_calls=[
                    {
                        "id": "plan-save",
                        "name": "storyboard_save",
                        "args": {
                            "title": "headless 分镜草案",
                            "shots": [
                                {
                                    "description": description,
                                    "framing": "wide" if index == 1 else "medium",
                                    "durationSeconds": 5,
                                    "mode": "t2v",
                                }
                                for index, description in enumerate(self.shots, start=1)
                            ],
                        },
                    }
                ],
            )
        return AIMessage(content="已保存两镜头分镜草案，尚未登记任何视频任务。没有媒体，也没有成片。")


class patches:
    """Route the application's adapters, workers and clock through offline fixtures."""

    def __init__(self, fixtures, clock):
        self.entries = [
            patch("vagent.application.KlingAdapter", fixtures["kling_factory"]),
            patch("vagent.application.FalAdapter", fixtures["fal_factory"]),
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


def scrubbed_job(raw):
    """The raw Job record plus public projection fields, without private URLs."""
    scrubbed = copy.deepcopy(raw)
    output = scrubbed.get("providerOutput")
    if output:
        output["videoUrl"] = "<redacted>"
        output["urlHost"] = urlsplit(output.get("videoUrl") or "").hostname
    scrubbed["simulated"] = scrubbed.get("mode") == "mock"
    scrubbed["mediaAvailable"] = scrubbed.get("mediaAvailability", {}).get("status") == "available"
    scrubbed["mediaRefs"] = (scrubbed.get("result") or {}).get("mediaRefs") or []
    return scrubbed


def job_identity(job):
    download = job.get("download") or {}
    return {
        "revision": job.get("revision"),
        "status": job.get("status"),
        "providerTaskId": job.get("providerTaskId"),
        "submitAttempts": job.get("submitAttempts"),
        "mediaAvailable": job.get("mediaAvailable"),
        "mediaId": download.get("mediaId"),
    }


def media_file_hashes(home, state):
    return {
        media_id: hashlib.sha256((home / raw["relativePath"]).read_bytes()).hexdigest()
        for media_id, raw in state["media"].items()
        if (home / raw["relativePath"]).is_file()
    }


def evidence_hashes(home):
    names = ["state.json", "checkpoints.sqlite", "m3-fixture.json", "mock-video.json"]
    state_path = home / "state.json"
    if state_path.is_file():
        try:
            state = json.loads(state_path.read_text(encoding="utf-8"))
            names.extend(sorted(asset["relativePath"] for asset in state.get("media", {}).values()))
            names.extend(sorted(asset["relativePath"] for asset in state.get("assets", {}).values()))
        except (ValueError, KeyError, TypeError):
            pass
    return {
        name: hashlib.sha256((home / name).read_bytes()).hexdigest() if (home / name).is_file() else None
        for name in names
    }


def tool_results(record):
    current = []
    for message in record["messages"]:
        if message["type"] == "human":
            current = []
        elif message["type"] == "tool":
            data = message["data"]
            current.append(
                {"id": data["tool_call_id"], "name": data.get("name"), "result": json.loads(data["content"])}
            )
    return current


def domain(snapshot):
    return {"projects": snapshot["projects"], "artifacts": snapshot["artifacts"]}


def project_jobs(state, project_id):
    return [scrubbed_job(raw) for raw in state["jobs"].values() if raw["context"]["sessionId"] == project_id]


def project_media(state, project_id):
    return [copy.deepcopy(asset) for asset in state["media"].values() if asset["projectId"] == project_id]


def project_film(state, project_id):
    films = [raw for raw in state["films"].values() if raw["projectId"] == project_id]
    return films[-1] if films else None


def film_checks(service, item):
    """Every film version against its index record, file bytes and copy timeline."""
    film = item.get("film")
    if not film:
        return {}
    state = service.store.snapshot()
    checks = {}
    for version in film["versions"]:
        media_id = version["outputMediaId"]
        raw = state["media"].get(media_id)
        path = service.store.home / raw["relativePath"] if raw else None
        data = path.read_bytes() if path is not None and path.is_file() else b""
        timeline = version["timeline"]
        starts = [entry["startSeconds"] for entry in timeline]
        durations = [entry["durationSeconds"] for entry in timeline]
        contiguous = all(
            abs(starts[index] - (starts[index - 1] + durations[index - 1])) <= 1e-6
            for index in range(1, len(starts))
        )
        checks[str(version["version"])] = {
            "indexRecorded": raw is not None,
            "filmBranch": bool(
                raw
                and raw.get("kind") == "film"
                and raw.get("contractVersion") == 2
                and raw.get("filmId") == film["filmId"]
                and raw.get("filmVersion") == version["version"]
                and raw.get("validationVersion") == "mp4-avc-v1"
            ),
            "fileExists": bool(data),
            "sizeMatches": bool(raw) and raw["sizeBytes"] == len(data) == version["sizeBytes"],
            "sha256Matches": bool(data) and hashlib.sha256(data).hexdigest() == version["sha256"],
            "metadataMatches": bool(raw) and raw["metadata"]["durationSeconds"] == version["durationSeconds"],
            "timelineContiguous": bool(timeline) and starts[0] == 0 and contiguous,
            "durationBound": abs(sum(durations) - version["durationSeconds"]) <= 0.05,
            "assemblyVersion": (version.get("assemblyVersion") if "assemblyVersion" in version else True),
            "engine": version["engine"]["packageVersion"] == "0.6.0"
            and len(version["engine"]["binarySha256"]) == 64,
            "noPart": path is not None and not path.with_name(path.name + ".part").exists(),
        }
    return checks


async def probe_film_api(service, config, item):
    """The played-back film surface: detail, GET, HEAD, Range, 416 and download."""
    film = item.get("film")
    if not film:
        return {}
    app = create_app(config)
    app.state.service = service
    results = {}
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:3210"
    ) as client:
        for version in film["versions"]:
            media_id = version["outputMediaId"]
            size = version["sizeBytes"]
            url = f"/api/media/{media_id}/content"
            detail = await client.get(f"/api/media/{media_id}")
            full = await client.get(url)
            head = await client.head(url)
            ranged = await client.get(url, headers={"Range": "bytes=0-99"})
            bad = await client.get(url, headers={"Range": f"bytes={size + 10}-"})
            attachment = await client.get(url + "?download=1")
            results[str(version["version"])] = {
                "detail200": detail.status_code == 200 and detail.json().get("contentUrl") == url,
                "get200": full.status_code == 200
                and len(full.content) == size
                and full.headers.get("content-type") == "video/mp4",
                "head200": head.status_code == 200
                and not head.content
                and head.headers.get("content-length") == str(size),
                "range206": ranged.status_code == 206
                and len(ranged.content) == 100
                and ranged.headers.get("content-range") == f"bytes 0-99/{size}",
                "unsatisfiable416": bad.status_code == 416
                and bad.headers.get("content-range") == f"bytes */{size}",
                "downloadAttachment": attachment.headers.get("content-disposition")
                == f'attachment; filename="{media_id}.mp4"',
                "etagMatchesSha": full.headers.get("etag", "").strip('"') == version["sha256"],
            }
    return results


def upstream_view(ledger, case):
    record = ledger.case(case.name)
    return {
        "source": "fixture",
        "submitCalls": len(record["submissions"]),
        "submitBodies": record["submissions"],
        "taskIds": record["taskIds"],
        "queryPaths": record["queryPaths"],
        "downloadCalls": len(record["downloads"]),
        "downloadHosts": sorted({entry["host"] for entry in record["downloads"]}),
        "authorization": record["authorization"],
    }


def provider_body_frozen(case, upstream):
    bodies = upstream["submitBodies"]
    if len(bodies) != 2:
        return False
    if case.provider == "kling":
        return all(
            set(body) == {"prompt", "settings", "options"}
            and body["settings"] == {"duration": 5, "resolution": "720p", "aspect_ratio": "16:9"}
            and isinstance(body["prompt"], str)
            and body["prompt"].strip()
            and body["options"]["external_task_id"].startswith("vag-")
            for body in bodies
        )
    if case.provider == "fal":
        return all(
            body
            == {
                "prompt": body["prompt"],
                "duration": "5",
                "aspect_ratio": "16:9",
                "generate_audio": False,
                "shot_type": "customize",
                "enable_safety_checker": True,
                "cfg_scale": 0.5,
                "negative_prompt": "blur, distort, and low quality",
            }
            and isinstance(body["prompt"], str)
            and body["prompt"].strip()
            for body in bodies
        )
    parameters = {
        "resolution": "720P",
        "ratio": "16:9",
        "duration": 5,
        "prompt_extend": False,
        "watermark": True,
        "seed": 0,
    }
    return all(
        body["parameters"] == parameters
        and body.get("input", {}).get("media", []) == []
        and isinstance(body["input"]["prompt"], str)
        and body["input"]["prompt"].strip()
        for body in bodies
    )


def provider_cost(case, generation):
    if case.provider == "kling":
        return (
            generation["totalEstimate"] == {"currency": "USD", "amount": "1.12"}
            and all(
                entry["estimate"]["currency"] == "USD"
                and entry["estimate"]["amount"] == "0.56"
                and entry["estimate"]["unitPrice"] == "0.112"
                and entry["estimate"]["priceVersion"] == "kling30-turbo-720p-20261021"
                for entry in generation["shots"]
            )
            and any(
                bucket["provider"] == "kling"
                and bucket["usedAmount"] == "1.12"
                and bucket["remainingAmount"] == "1.28"
                and bucket["usedUnits"] == 8
                and bucket["remainingUnits"] == 8
                for bucket in generation["limits"]["buckets"]
            )
        )
    if case.provider == "fal":
        return (
            generation["totalEstimate"] == {"currency": "USD", "amount": "0.84"}
            and all(
                entry["estimate"]["currency"] == "USD"
                and entry["estimate"]["amount"] == "0.42"
                and entry["estimate"]["unitPrice"] == "0.084"
                and entry["estimate"]["priceVersion"] == "fal-kling30-standard-silent-20261021"
                for entry in generation["shots"]
            )
            and any(
                bucket["provider"] == "fal"
                and bucket["usedAmount"] == "0.84"
                and bucket["remainingAmount"] == "1.16"
                for bucket in generation["limits"]["buckets"]
            )
        )
    return (
        generation["totalEstimate"] == {"currency": "CNY", "amount": "6.00"}
        and all(entry["estimate"]["amount"] == "3.00" for entry in generation["shots"])
        and generation["limits"]["usedNewJobs"] == 2
        and generation["limits"]["usedAmount"] == "6.00"
        and generation["limits"]["remainingAmount"] == "6.00"
        and generation["limits"]["maxRunCost"] == "12.00"
    )


def film_structure(case, item):
    film = item.get("filmInitial") or item.get("film")
    if not film:
        return False
    latest = film["latest"]
    shot_ids = item.get("shotIds") or []
    media_ids = [entry["mediaId"] for entry in latest["inputs"]]
    return (
        film["currentVersion"] == 1
        and film["versionCount"] == 1
        and latest["version"] == 1
        and [entry["shotId"] for entry in latest["inputs"]] == shot_ids
        and media_ids == item.get("jobMediaIds")
        and latest["storyboardRevision"] == item.get("storyboardRevision")
        and [round(entry["startSeconds"], 6) for entry in latest["timeline"]] == [0.0, CLIP_SECONDS]
        and [round(entry["durationSeconds"], 6) for entry in latest["timeline"]]
        == [CLIP_SECONDS, CLIP_SECONDS]
        and latest["width"] == 1280
        and latest["height"] == 720
        and latest["hasAudio"] is True
        and latest["mediaAvailable"] is True
        and latest["mediaUrl"] == f"/api/media/{latest['outputMediaId']}/content"
    )


def all_checks_pass(checks):
    return bool(checks) and all(checks.values())


def grade(case, item):
    """One independent check per promised behaviour; no lenient fallbacks."""
    checks = {}
    run = item.get("run") or {}
    results = item.get("toolResults") or []
    sequence = [entry["name"] for entry in results]
    generation = next((entry["result"] for entry in results if entry["name"] == "shot_generate"), {}).get(
        "data"
    )
    final_upstream = item.get("upstream") or {}
    upstream = item.get("runUpstream") or final_upstream
    jobs = item.get("runJobs") or item.get("jobs") or []
    film = item.get("film")

    if case.kind == "conversation":
        reported = item.get("filmInitial") or item.get("film")
        checks["run_completed"] = run.get("status") == "completed" and not run.get("errorCode")
        checks["tool_chain"] = sequence == FILM_TOOL_CHAIN
        checks["film_reported"] = bool(
            reported
            and "成片" in (run.get("answer") or "")
            and f"v{reported['currentVersion']}" in (run.get("answer") or "")
            and reported["latest"]["sha256"] in (run.get("answer") or "")
        )
        checks["jobs_delivered"] = len(jobs) == 2 and all(
            job["status"] == "succeeded"
            and job["mediaAvailable"]
            and job["submitAttempts"] == 1
            and job["mediaRefs"]
            for job in jobs
        )
        checks["provider_body_frozen"] = bool(generation) and provider_body_frozen(case, upstream)
        checks["provider_cost"] = bool(generation) and provider_cost(case, generation)
        if case.provider == "kling":
            checks["provider_hosts"] = upstream["downloadHosts"] == ["cdn.example-provider.test"]
            checks["provider_auth"] = upstream["authorization"] == ["bearer", "bearer"]
            checks["provider_queries"] = set(upstream["queryPaths"]) == {"/tasks"}
        elif case.provider == "fal":
            checks["provider_hosts"] = upstream["downloadHosts"] == ["v3.fal.media"]
            checks["provider_auth"] = upstream["authorization"] == ["key", "key"]
            checks["provider_queries"] = all(
                path.startswith("/fal-ai/kling-video/requests/") for path in upstream["queryPaths"]
            )
        else:
            checks["provider_hosts"] = upstream["downloadHosts"] == [WAN_RESULT_HOST]
            checks["provider_auth"] = upstream["authorization"] == ["bearer", "bearer"]
        checks["submits_accounted"] = final_upstream["submitCalls"] == (3 if case.redo else 2)
        checks["film_structure"] = film_structure(case, item)
        branch_checks = item.get("filmChecks") or {}
        media_checks = item.get("mediaApi") or {}
        read_film = (item.get("filmRead") or {}).get("film") or {}
        checks["film_branch"] = bool(branch_checks) and all(
            all(flags.values()) for flags in branch_checks.values()
        )
        checks["film_playback"] = bool(media_checks) and all(
            all(flags.values()) for flags in media_checks.values()
        )
        checks["film_read"] = bool(read_film.get("latest", {}).get("mediaAvailable"))
        checks["text_side_unchanged"] = item.get("beforeDomain") == item.get("afterDomain")
        if case.redo:
            redo = item.get("redo") or {}
            checks["redo_added_one_shot_version"] = redo.get("versionCountDelta") == {
                redo.get("targetShotId"): 1,
                **{
                    shot_id: 0
                    for shot_id in (item.get("shotIds") or [])
                    if shot_id != redo.get("targetShotId")
                },
            }
            checks["redo_left_other_jobs_untouched"] = bool(
                redo.get("otherJobsUnchanged") and redo.get("otherMediaUnchanged")
            )
            checks["redo_appended_film_version"] = bool(
                redo.get("filmBeforeVersion") == 1 and redo.get("filmAfterVersion") == 2
            )
            checks["redo_kept_old_film"] = bool(
                redo.get("oldFilmExists") and redo.get("oldFilmHashUnchanged")
            )
            checks["redo_uses_new_shot_version"] = bool(redo.get("usesNewShotVersion"))
            checks["redo_replay_is_idempotent"] = bool(redo.get("replaySameVersion"))
            checks["stale_assemble_refused"] = redo.get("staleError") == "VERSION_CONFLICT"
        else:
            replay = item.get("replay") or {}
            checks["assembly_replay_is_idempotent"] = bool(
                replay.get("sameVersion") and replay.get("sameMediaId") and replay.get("filesUnchanged")
            )
        checks["film_assembly_no_submit"] = final_upstream["submitCalls"] == (3 if case.redo else 2)
        return checks

    if case.kind == "refusals":
        refusal = item.get("refusal") or {}
        checks["seed_delivered"] = len(jobs) == 2 and all(
            job["status"] == "succeeded" and job["mediaAvailable"] for job in jobs
        )
        checks["seeded_film_version"] = bool(film and film["currentVersion"] == 1)
        checks["replay_idempotent"] = bool(
            refusal.get("replaySameVersion")
            and refusal.get("replaySameMediaId")
            and refusal.get("replayFilesUnchanged")
        )
        checks["stale_revision_refused"] = refusal.get("staleError") == "VERSION_CONFLICT"
        checks["invalid_media_refused"] = refusal.get("invalidMediaError") == "FILM_MEDIA_INVALID"
        checks["missing_input_refused"] = refusal.get("missingInputError") == "FILM_INPUT_MISSING"
        checks["refusals_known_codes"] = all(
            code in REFUSAL_TOKENS
            for code in (
                refusal.get("staleError"),
                refusal.get("invalidMediaError"),
                refusal.get("missingInputError"),
            )
        )
        checks["refusals_kept_film"] = bool(
            refusal.get("versionCountAfter") == 1
            and refusal.get("filmFilesAfter") == refusal.get("filmFilesBefore")
        )
        checks["refusals_left_no_parts"] = refusal.get("partFiles") == []
        checks["film_assembly_no_submit"] = upstream["submitCalls"] == 2
        return checks

    plan = item.get("headless") or {}
    checks["plan_completed"] = plan.get("status") in {"planned", "authorized", "delivered"}
    checks["plan_fingerprint"] = len(plan.get("planFingerprint") or "") == 64
    checks["plan_fields"] = (
        plan.get("formatVersion") == 1
        and plan.get("projectId") == case.name
        and plan.get("sessionId") == case.name
        and bool(plan.get("storyboardId"))
        and plan.get("storyboardVersion") == 1
    )
    estimates = plan.get("estimates") or []
    checks["plan_estimates"] = bool(
        estimates
        and estimates[0]["provider"] == "kling"
        and estimates[0]["currency"] == "USD"
        and estimates[0]["amount"] == "1.12"
        and estimates[0]["units"] == 8
        and estimates[0]["jobCount"] == 2
    )
    checks["plan_tool_view_narrowed"] = bool(item.get("headlessTools")) and not {
        "shot_generate",
        "video_generate",
        "film_assemble",
    } & set(item.get("headlessTools") or [])
    if case.kind == "headless-plan":
        checks["no_jobs"] = item.get("jobCount") == 0
        checks["no_media"] = item.get("mediaCount") == 0
        checks["no_submits"] = upstream["submitCalls"] == 0
        checks["planned_only"] = (
            plan.get("status") == "planned"
            and plan.get("jobIds") == []
            and plan.get("output") is None
            and plan.get("error") is None
        )
        return checks

    checks["authorized"] = plan.get("status") == "delivered"
    checks["delivered_film"] = bool(plan.get("filmId") and plan.get("filmVersion") == 1)
    checks["delivered_jobs"] = len(plan.get("jobIds") or []) == 2
    checks["delivered_output"] = bool(
        plan.get("output")
        and Path(plan["output"]["path"]).is_file()
        and Path(plan["output"]["path"]).stat().st_size == plan["output"]["sizeBytes"]
        and hashlib.sha256(Path(plan["output"]["path"]).read_bytes()).hexdigest() == plan["output"]["sha256"]
    )
    checks["delivered_film_record"] = bool(
        film
        and film["currentVersion"] == 1
        and len(film["latest"]["inputs"]) == 2
        and film["latest"]["mediaAvailable"]
    )
    checks["delivered_replay"] = bool(
        item.get("replayOutput") == plan.get("output") and item.get("replayFilmId") == plan.get("filmId")
    )
    checks["delivered_no_resubmit"] = upstream["submitCalls"] == 2
    checks["delivered_state_counts"] = item.get("jobCount") == 2 and item.get("mediaCount") == 3
    return checks


async def settle(service, project_id, clock, *, pause=0.01, timeout=180):
    deadline = time.monotonic() + timeout
    while True:
        jobs = list(service.video_jobs.list(session_id=project_id))
        active = [job for job in jobs if job.status in ACTIVE_STATUSES and job.query_state != "paused"]
        if not active:
            return
        if time.monotonic() > deadline:
            raise AppError("EVAL_TIMEOUT", f"{project_id} 的任务未在夹具时限内结束。")
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


async def assemble(service, project_id, storyboard_id, revision, request_id):
    return await service.assemble_film(
        project_id,
        {
            "clientRequestId": request_id,
            "storyboardId": storyboard_id,
            "expectedRevision": revision,
        },
    )


async def run_conversation(case, base, ledger, clock, service, item):
    """Start one Agent Run from the prompt and capture the complete film evidence."""
    service.store.ensure_session(case.name)
    item["prompt"] = prompt_for(case)
    item["beforeDomain"] = domain(service.store.snapshot())
    item["harnessSignature"] = service.runner().context_signature()
    record = await service.start(case.name, item["prompt"], f"m3-{case.name}")
    record = await drive(service, record["id"], clock)
    await settle(service, case.name, clock)
    state = service.store.snapshot()
    board = next(raw for raw in state["storyboards"].values() if raw["projectId"] == case.name)
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
        "toolTrace": [
            {"id": entry["id"], "name": entry["name"], "arguments": None, "result": entry["result"]}
            for entry in tool_results(record)
        ],
        "policy": record.get("policy"),
        "resumable": bool(record.get("checkpointId")),
    }
    item["toolResults"] = tool_results(record)
    saved = next(
        entry["result"]["data"] for entry in item["toolResults"] if entry["name"] == "storyboard_save"
    )
    item["storyboardId"] = saved["storyboardId"]
    item["shotIds"] = saved["shotIds"]
    item["storyboardRevision"] = board["revision"]
    item["jobs"] = project_jobs(state, case.name)
    item["jobMediaIds"] = [job["mediaRefs"][0]["mediaId"] for job in item["jobs"]]
    item["upstream"] = upstream_view(ledger, case)
    item["film"] = service.film(case.name, saved["storyboardId"])["film"]
    item["filmInitial"] = copy.deepcopy(item["film"])
    item["filmChecks"] = film_checks(service, item)
    item["mediaApi"] = await probe_film_api(service, base, item)
    context = ToolExecutionContext(
        project_id=case.name,
        session_id=case.name,
        run_id=record["id"],
        model_step=record["modelSteps"],
        tool_call_id="film-read",
    )
    item["filmRead"] = await service.tools.aexecute(
        "film_read",
        {"storyboardId": saved["storyboardId"]},
        store=service.store,
        project_id=case.name,
        operation_key=context.operation_key,
        context=context,
    )
    item["afterDomain"] = domain(service.store.snapshot())
    item["runJobs"] = copy.deepcopy(item["jobs"])
    item["runUpstream"] = copy.deepcopy(item["upstream"])

    if case.redo and item["film"]:
        item["redo"] = await redo_and_reassemble(case, service, clock, item, state, board, ledger)
    elif item["film"]:
        item["replay"] = await replay_assembly(service, case, item, board)


async def replay_assembly(service, case, item, board):
    files_before = {path.name for path in (service.store.home / "media" / case.name).iterdir()}
    first = item["film"]["latest"]
    again = await assemble(service, case.name, board["id"], board["revision"], f"m3-{case.name}-replay")
    files_after = {path.name for path in (service.store.home / "media" / case.name).iterdir()}
    return {
        "sameVersion": again["currentVersion"] == item["film"]["currentVersion"],
        "sameMediaId": again["latest"]["outputMediaId"] == first["outputMediaId"],
        "filesUnchanged": files_before == files_after,
    }


async def redo_and_reassemble(case, service, clock, item, state, board, ledger):
    """One explicit user redo, then an explicit reassembly; the old film stays intact."""
    target = board["versions"][-1]["shots"][0]["id"]
    other = board["versions"][-1]["shots"][1]["id"]
    before_jobs = {job["id"]: job_identity(job) for job in project_jobs(state, case.name)}
    film_media_before = copy.deepcopy(state["media"][item["film"]["latest"]["outputMediaId"]])
    old_path = service.store.home / film_media_before["relativePath"]
    old_hash = hashlib.sha256(old_path.read_bytes()).hexdigest()
    other_entry = next(entry for entry in board["shotVersions"] if entry["shotId"] == other)
    other_shot_version = other_entry["id"]
    other_media = next(
        raw["id"] for raw in state["media"].values() if raw.get("jobId") == other_entry["jobId"]
    )

    body = service.redo_shot(
        case.name,
        target,
        {
            "clientRequestId": f"m3-{case.name}-redo",
            "storyboardId": board["id"],
            "storyboardVersion": board["currentVersion"],
            "expectedRevision": board["revision"],
        },
    )
    await settle(service, case.name, clock)
    state_after = service.store.snapshot()
    board_after = state_after["storyboards"][board["id"]]
    after_jobs = {entry["id"]: job_identity(entry) for entry in project_jobs(state_after, case.name)}
    new_job = body["shots"][0]["jobId"]
    other_jobs_unchanged = all(after_jobs.get(job_id) == identity for job_id, identity in before_jobs.items())
    hashes_before = media_file_hashes(service.store.home, state)
    hashes_after = media_file_hashes(service.store.home, state_after)
    other_media_unchanged = hashes_after.get(other_media) == hashes_before.get(other_media)
    film_before = item["film"]
    film_after = await assemble(
        service,
        case.name,
        board_after["id"],
        board_after["revision"],
        f"m3-{case.name}-assemble-redo",
    )
    replay = await assemble(
        service,
        case.name,
        board_after["id"],
        board_after["revision"],
        f"m3-{case.name}-assemble-redo-replay",
    )
    new_inputs = film_after["latest"]["inputs"]
    new_shot_version = [entry["id"] for entry in board_after["shotVersions"] if entry["shotId"] == target][-1]
    try:
        await assemble(
            service,
            case.name,
            board_after["id"],
            board_after["revision"] + 5,
            f"m3-{case.name}-assemble-stale",
        )
        stale_error = None
    except AppError as error:
        stale_error = error.code
    version_counts = {
        shot_id: len([entry for entry in board_after["shotVersions"] if entry["shotId"] == shot_id])
        for shot_id in item["shotIds"]
    }
    before_counts = {
        shot_id: len([entry for entry in board["shotVersions"] if entry["shotId"] == shot_id])
        for shot_id in item["shotIds"]
    }
    item["film"] = film_after
    item["filmChecks"] = film_checks(service, item)
    item["mediaApi"] = await probe_film_api(service, service.config, item)
    item["upstream"] = upstream_view(ledger, case)
    item["jobs"] = project_jobs(state_after, case.name)
    return {
        "targetShotId": target,
        "newJobId": new_job,
        "body": body,
        "before": before_jobs,
        "after": after_jobs,
        "otherJobsUnchanged": other_jobs_unchanged,
        "otherMediaUnchanged": other_media_unchanged,
        "versionCountDelta": {
            shot_id: version_counts[shot_id] - before_counts[shot_id] for shot_id in item["shotIds"]
        },
        "filmBeforeVersion": film_before["currentVersion"],
        "filmAfterVersion": film_after["currentVersion"],
        "oldFilmExists": old_path.is_file(),
        "oldFilmHashUnchanged": old_path.is_file()
        and hashlib.sha256(old_path.read_bytes()).hexdigest() == old_hash,
        "usesNewShotVersion": new_shot_version in [entry["shotVersionId"] for entry in new_inputs],
        "keptOtherShotVersion": other_shot_version in [entry["shotVersionId"] for entry in new_inputs],
        "replaySameVersion": replay["currentVersion"] == film_after["currentVersion"]
        and replay["latest"]["outputMediaId"] == film_after["latest"]["outputMediaId"],
        "staleError": stale_error,
        "oldFilmMediaUnchanged": state_after["media"].get(film_media_before["id"]) == film_media_before,
    }


async def run_refusals(case, base, ledger, clock, service, item):
    """Deliver two clips, then prove the refusal, replay and version boundaries."""
    service.store.ensure_session(case.name)
    saved = service.tools.execute(
        "storyboard_save",
        {
            "title": f"{case.name} 分镜",
            "shots": [
                {"description": description, "framing": "wide", "durationSeconds": 5}
                for description in case.shots
            ],
        },
        store=service.store,
        project_id=case.name,
        operation_key=f"m3-{case.name}-seed-storyboard",
    )
    item["storyboardId"] = saved["data"]["storyboardId"]
    item["shotIds"] = saved["data"]["shotIds"]
    registered = service.generate_shots(
        case.name,
        {
            "clientRequestId": f"m3-{case.name}-seed",
            "storyboardId": saved["data"]["storyboardId"],
            "storyboardVersion": saved["data"]["version"],
            "shotIds": saved["data"]["shotIds"],
            "confirm": True,
        },
    )
    if not all(entry["jobId"] for entry in registered["shots"]):
        raise AppError("EVAL_SEED_FAILED", "拒绝路径的种子登记失败。")
    await settle(service, case.name, clock)
    state = service.store.snapshot()
    board = state["storyboards"][saved["data"]["storyboardId"]]
    item["storyboardRevision"] = board["revision"]
    item["jobs"] = project_jobs(state, case.name)
    item["jobMediaIds"] = [job["mediaRefs"][0]["mediaId"] for job in item["jobs"]]
    item["film"] = await assemble(
        service,
        case.name,
        saved["data"]["storyboardId"],
        board["revision"],
        f"m3-{case.name}-seed-film",
    )
    item["film"] = service.film(case.name, saved["data"]["storyboardId"])["film"]
    version = item["film"]["latest"]
    item["filmChecks"] = film_checks(service, item)
    files_before = {path.name for path in (service.store.home / "media" / case.name).iterdir()}
    replay = await assemble(
        service,
        case.name,
        saved["data"]["storyboardId"],
        board["revision"],
        f"m3-{case.name}-seed-film-replay",
    )
    files_after = {path.name for path in (service.store.home / "media" / case.name).iterdir()}

    async def refusal(revision, request_id):
        try:
            await assemble(service, case.name, saved["data"]["storyboardId"], revision, request_id)
            return None
        except AppError as error:
            return error.code

    stale = await refusal(board["revision"] + 5, f"m3-{case.name}-stale")
    first_input = version["inputs"][0]
    raw = state["media"][first_input["mediaId"]]
    path = service.store.home / raw["relativePath"]
    original = path.read_bytes()
    path.write_bytes(original[:-16])
    try:
        invalid_media = await refusal(board["revision"], f"m3-{case.name}-invalid")
    finally:
        path.write_bytes(original)
        path.touch()
    revised = service.tools.execute(
        "storyboard_save",
        {
            "storyboardId": saved["data"]["storyboardId"],
            "expectedVersion": board["currentVersion"],
            "title": f"{case.name} 分镜 v2",
            "shots": [
                {"id": shot_id, "description": description, "framing": "wide", "durationSeconds": 5}
                for shot_id, description in zip(item["shotIds"], case.shots, strict=True)
            ]
            + [{"description": "镜头三：收尾", "framing": "wide", "durationSeconds": 5}],
        },
        store=service.store,
        project_id=case.name,
        operation_key=f"m3-{case.name}-revise",
    )
    revised_board = service.store.snapshot()["storyboards"][saved["data"]["storyboardId"]]
    missing = await refusal(revised_board["revision"], f"m3-{case.name}-missing")
    part_files = sorted(path.name for path in (service.store.home / "media" / case.name).glob("*.part"))
    item["film"] = service.film(case.name, saved["data"]["storyboardId"])["film"]
    item["filmChecks"] = film_checks(service, item)
    item["mediaApi"] = await probe_film_api(service, base, item)
    item["upstream"] = upstream_view(ledger, case)
    item["refusal"] = {
        "replaySameVersion": replay["currentVersion"] == item["film"]["currentVersion"],
        "replaySameMediaId": replay["latest"]["outputMediaId"] == version["outputMediaId"],
        "replayFilesUnchanged": files_before == files_after,
        "staleError": stale,
        "invalidMediaError": invalid_media,
        "missingInputError": missing,
        "versionCountAfter": len(item["film"]["versions"]),
        "filmFilesBefore": sorted(files_before),
        "filmFilesAfter": sorted(path.name for path in (service.store.home / "media" / case.name).iterdir()),
        "partFiles": part_files,
        "revisedVersion": revised["data"]["version"],
    }


async def run_headless(case, base, ledger, clock, service, item):
    """Plan first; only the explicit authorization path registers and delivers."""
    model = PlanModel(case.shots)
    planned = await headless.plan(
        service,
        session_id=case.name,
        prompt=prompt_for(case),
        model=model,
    )
    item["headless"] = planned
    item["headlessTools"] = [
        spec["function"]["name"]
        for spec in service.headless_runner(model=PlanModel(case.shots)).tools.specs()
    ]
    item["upstream"] = upstream_view(ledger, case)
    state = service.store.snapshot()
    item["jobCount"] = len(
        [job for job in state["jobs"].values() if job["context"]["sessionId"] == case.name]
    )
    item["mediaCount"] = len([media for media in state["media"].values() if media["projectId"] == case.name])
    if case.kind == "headless-plan":
        item["film"] = None
        return

    authorized = headless.authorize(
        service,
        run_id=planned["runId"],
        plan_fingerprint=planned["planFingerprint"],
        confirm=True,
        max_cost=["kling:USD=1.12"],
        max_units=["kling=8"],
        max_jobs=2,
    )
    item["authorization"] = authorized
    task = asyncio.create_task(headless.execute(service, run_id=planned["runId"], wait_timeout=120))
    while not task.done():
        clock.advance(20)
        await asyncio.sleep(0.01)
    delivered = await task
    item["headless"] = delivered
    item["film"] = service.film(case.name, delivered["storyboardId"])["film"]
    item["storyboardId"] = delivered["storyboardId"]
    item["shotIds"] = [entry["shotId"] for entry in item["film"]["latest"]["inputs"]]
    item["filmChecks"] = film_checks(service, item)
    item["mediaApi"] = await probe_film_api(service, base, item)
    item["jobs"] = project_jobs(service.store.snapshot(), case.name)
    item["upstream"] = upstream_view(ledger, case)
    state = service.store.snapshot()
    item["jobCount"] = len(
        [job for job in state["jobs"].values() if job["context"]["sessionId"] == case.name]
    )
    item["mediaCount"] = len([media for media in state["media"].values() if media["projectId"] == case.name])
    replay = await headless.execute(service, run_id=planned["runId"], wait_timeout=120)
    item["replayOutput"] = replay["output"]
    item["replayFilmId"] = replay["filmId"]


def make_fixtures(case, ledger, clock):
    kling = KlingFixture(case, ledger)
    fal = FalFixture(case, ledger)
    wan = WanFixture(case, ledger)
    return {
        "kling": kling,
        "fal": fal,
        "wan": wan,
        "media": MediaFixture(case, ledger),
        "kling_factory": lambda api_key: KlingAdapter(
            api_key=api_key, transport=httpx.MockTransport(kling.handle), clock=clock
        ),
        "fal_factory": lambda api_key: FalAdapter(
            api_key=api_key, transport=httpx.MockTransport(fal.handle), clock=clock
        ),
        "wan_factory": lambda **kwargs: WanAdapter(
            **kwargs, transport=httpx.MockTransport(wan.handle), clock=clock
        ),
    }


def base_config(args, case):
    configured = load_config()
    return replace(
        configured,
        home=args.home,
        api_key="offline-model-placeholder",
        video_mode="live",
        video_provider=case.provider,
        video_api_key="offline-wan-placeholder",
        video_workspace_id="offline-fixture",
        kling_api_key="offline-kling-placeholder",
        fal_api_key="offline-fal-placeholder",
        context_bytes=98304,
        redis_url=None,
        skills_root=None,
        mcp_config=None,
        mcp_local=False,
    )


async def run_case(args, case, ledger, remaining):
    clock = SuiteClock()
    fixtures = make_fixtures(case, ledger, clock)
    base = base_config(args, case)
    item = {"case": case.name, "kind": case.kind, "provider": case.provider}
    model = FixtureModel() if case.kind == "conversation" else None
    with patches(fixtures, clock):
        async with ApplicationService.open(
            base,
            model=model,
            policy=RunPolicy(max_steps=min(8, remaining), max_tool_calls=12, timeout_seconds=180),
        ) as service:
            if case.kind == "conversation":
                await run_conversation(case, base, ledger, clock, service, item)
            elif case.kind == "refusals":
                await run_refusals(case, base, ledger, clock, service, item)
            else:
                await run_headless(case, base, ledger, clock, service, item)
            item["filmCheckSummary"] = item.get("filmChecks")
            item["checks"] = grade(case, item)
            if item.get("film"):
                item["film"] = copy.deepcopy(item["film"])
    return item


def totals(report):
    cases = report.get("cases") or []
    calls = [call for item in cases for call in ((item.get("run") or {}).get("modelCalls") or [])]
    known = [
        call for call in calls if call.get("inputTokens") is not None and call.get("outputTokens") is not None
    ]
    jobs = [job for item in cases for job in item.get("jobs") or []]
    return {
        "plannedCases": len(CASES),
        "finishedCases": len(cases),
        "passedCases": len([item for item in cases if all_checks_pass(item.get("checks"))]),
        "modelCalls": len(calls),
        "toolCalls": sum((item.get("run") or {}).get("toolCalls") or 0 for item in cases),
        "submitCalls": sum((item.get("upstream") or {}).get("submitCalls") or 0 for item in cases),
        "downloadCalls": sum((item.get("upstream") or {}).get("downloadCalls") or 0 for item in cases),
        "deliveredClips": len(
            [job for job in jobs if job.get("status") == "succeeded" and job.get("mediaAvailable")]
        ),
        "filmVersions": sum(len((item.get("film") or {}).get("versions") or []) for item in cases),
        "redos": len([item for item in cases if item.get("redo")]),
        "callsWithUnknownUsage": len(calls) - len(known),
        "tokenUsageComplete": bool(calls) and len(known) == len(calls),
        "realProviderCalls": 0,
    }


async def run_suite(args):
    if getattr(args, "live", False):
        raise AppError(
            "EVAL_LIVE_UNAVAILABLE",
            "M3 E4 未获新增付费样本的模型、次数与金额授权；--live 未执行，未发起任何请求。",
        )
    if args.max_model_calls is None:
        args.max_model_calls = 40
    if not 1 <= args.max_model_calls <= 60:
        raise AppError("EVAL_BUDGET_INVALID", "模型调用上限须为1～60。")
    if args.output.exists():
        raise AppError("EVAL_OUTPUT_EXISTS", "输出文件已存在，请使用新路径保留先前证据。")
    home = args.home.resolve()
    if home.exists() and any(home.iterdir()):
        raise AppError("EVAL_HOME_NOT_EMPTY", "评测需要新的空数据目录，已有数据未修改。")
    if not FIXTURE.is_file():
        raise AppError("EVAL_FIXTURE_MISSING", "缺少本地测试媒体夹具，离线评测未启动。")
    ledger = Ledger(home)
    report = {
        "schemaVersion": 1,
        "suiteId": suite_identity(CASES),
        "mode": "offline-fixture",
        "status": "incomplete",
        "startedAt": now(),
        "dataDirectory": str(home),
        "maxModelCalls": args.max_model_calls,
        "budget": {"maxModelCalls": 40, "maxSubmits": None, "maxEstimatedAmount": None},
        "videoSettings": {
            "mode": "live",
            "capabilitiesVersion": "multi-provider-v3",
            "providers": {
                "kling": {
                    "model": KLING_MODEL,
                    "endpointProfile": "kling-singapore-v1",
                    "priceVersion": "kling30-turbo-720p-20261021",
                    "sourcePolicyVersion": "kling-auth-result-origin-v1",
                },
                "fal": {
                    "model": FAL_MODEL,
                    "endpointProfile": "fal-queue-v1",
                    "priceVersion": "fal-kling30-standard-silent-20261021",
                    "sourcePolicyVersion": "fal-auth-result-origin-v1",
                },
                "wan": {"model": WAN_MODEL},
            },
        },
        "cases": [],
        "limitations": (
            "受控 HTTP 与本地测试 MP4 的固定脚本用例：证明应用代码路径、拼接与拒绝边界，"
            "不证明真实账号、真实 CDN、真实编码或账单。真实付费样本未执行。"
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
        for case in CASES:
            remaining = args.max_model_calls - totals(report)["modelCalls"]
            if remaining <= 0:
                report["status"] = "budget-exhausted"
                break
            item = await run_case(args, case, ledger, remaining)
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
                        "filmVersions": len((item.get("film") or {}).get("versions") or []),
                    },
                    ensure_ascii=False,
                ),
                flush=True,
            )
            if not all_checks_pass(item["checks"]):
                report["status"] = "failed"
                break
        else:
            report["status"] = "passed"
    except asyncio.CancelledError:
        report["status"] = "interrupted"
        raise
    except Exception as error:
        traceback.print_exc()
        safe = public_error(error)
        report.update(status="error", error={"code": safe.code, "message": str(safe)})
    finally:
        report["finishedAt"] = now()
        report["evidenceHashes"] = evidence_hashes(home)
        persist()
    return report


def parser():
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument(
        "--live",
        action="store_true",
        help="M3 E4 未获新增付费样本授权；本参数会拒绝执行且不发起请求",
    )
    result.add_argument("--home", type=Path, default=Path(".vagent/evaluations/m3") / str(uuid4()))
    result.add_argument("--output", type=Path)
    result.add_argument("--max-model-calls", type=int, default=None, help="套件模型调用上限，默认40，最高60")
    return result


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    load_dotenv(".env", override=False)
    args = parser().parse_args()
    args.output = args.output or Path(f"output/m3-offline-{uuid4().hex[:8]}.json")
    try:
        report = asyncio.run(run_suite(args))
    except AppError as error:
        print(f"{error.code}: {error}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130
    print(
        json.dumps(
            {"status": report["status"], "output": str(args.output), "totals": report["totals"]},
            ensure_ascii=False,
        )
    )
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
