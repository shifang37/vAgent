"""M2 application acceptance: offline by default; --live spends real DeepSeek and Wan calls.

Offline mode drives the real application against a controlled upstream: the M2
storyboard tools, batch registration guardrails, the set wait, the local media
pipeline and the explicit per-shot redo all run through the production code
paths, while the Wan protocol is served by ``httpx.MockTransport`` and the
delivered MP4 is the committed local fixture. No paid service and no public
network are used.

``--live`` explicitly uses the configured real DeepSeek model and the frozen
Beijing Wan account with one multi-shot case: a two-shot storyboard, one batch
registration, one set wait and one explicit single-shot redo — at most three
submissions and 9.00 CNY of estimated spend, which needs explicit budget
approval before it runs. i2v/flf2v never run live. Both modes stop at the first
failure and share one persisted suite ceiling; ``--continue-from`` resumes only
the original failed Run without resetting budgets or overwriting earlier
evidence.
"""

import argparse
import asyncio
import copy
import hashlib
import json
import re
import struct
import sys
import time
import traceback
import zlib
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlsplit
from uuid import uuid4

import httpx
from dotenv import load_dotenv
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from vagent.application import ApplicationService
from vagent.config import load_config, require_key
from vagent.errors import AppError, public_error
from vagent.runner import RunPolicy
from vagent.storage import atomic_write_json, now
from vagent.video.contracts import (
    WAN_I2V_MODEL,
    WAN_MODEL,
    WAN_PROMPT_PREFIX,
    WAN_SHOT_CAPABILITIES_VERSION,
    JobV3,
    wan_shot_capabilities,
)
from vagent.video.jobs import JobService
from vagent.video.media_http import MediaHttpClient
from vagent.video.media_worker import MediaWorker
from vagent.video.providers.wan import WanAdapter
from vagent.video.worker import JobWorker
from vagent.waiting import WAIT_EXECUTION_VERSION, ToolExecutionContext
from vagent.web import create_app


@dataclass(frozen=True)
class Case:
    """One scripted acceptance scenario; the offline model derives it from the prompt."""

    name: str
    prompt: str
    expects: str = "batch_delivered"
    shots: tuple = ()  # (description, framing, mode, duration_seconds, frames)
    source: bool = False
    reference: bool = False
    confirm: bool = True
    replay: bool = False
    failure_index: int = 0  # 1-based submission order that the provider fails
    redo_index: int = 0  # 1-based shot index the user redoes after the run
    stale_redo: bool = False


def spec(description, framing="wide", mode="t2v", duration=5, frames=0):
    return (description, framing, mode, duration, frames)


CASES = (
    Case(
        "storyboard-draft",
        "这是 M2 分镜草案验收。读取当前项目 brief {artifact_id} 的第1版，以它为来源（sourceRefs 保留该 ID 与 version=1）。"
        "先核对视频能力，再保存一个三镜头分镜：镜头一 t2v 远景；"
        "镜头二 i2v 中景，参考图 {asset_id}；镜头三 flf2v 特写，参考图依次 {asset_id}、{last_asset_id}。"
        "最后回显每个镜头的模式、时长与逐镜头估算、合计估算。不要登记任务、不要等待、不修改文本产物。",
        expects="draft_only",
        shots=(
            spec("远景：雨夜街道，霓虹与伞影"),
            spec("中景：推门入店，暖光落在吧台", framing="medium", mode="i2v", frames=1),
            spec("特写：杯口热气与拉花", framing="close", mode="flf2v", frames=2),
        ),
        source=True,
        reference=True,
    ),
    Case(
        "batch-delivered",
        "这是 M2 多镜头生成验收。先核对视频能力，再保存一个三镜头分镜：镜头一 t2v 远景；"
        "镜头二 i2v 中景，参考图 {asset_id}；镜头三 t2v 特写。"
        "然后一次性为三个镜头登记真实任务（显式确认），并用一次 await_job 等待集合结果；"
        "登记之后立即用完全相同的参数再登记一次，按工具实际返回处理。"
        "最后按实际结果逐镜头汇报真实 jobId、合计估算与本地交付情况；不要修改文本产物、不重复提交、不编造媒体。",
        expects="batch_delivered",
        shots=(
            spec("远景：雨夜街道，霓虹与伞影"),
            spec("中景：推门入店，暖光落在吧台", framing="medium", mode="i2v", frames=1),
            spec("特写：杯口热气与拉花", framing="close"),
        ),
        reference=True,
        replay=True,
    ),
    Case(
        "partial-failure",
        "这是 M2 部分失败验收。先核对视频能力，再保存一个三镜头分镜（镜头一、镜头二、镜头三，都是 t2v 五秒）。"
        "为三个镜头登记真实任务，并用一次 await_job 等待集合结果。按实际结果逐镜头如实汇报："
        "成功的就是成功，失败的就是失败（引用真实 jobId 与错误码）；"
        "不要自动重做或登记替代任务，不要声称失败镜头有媒体。",
        expects="partial_failure",
        shots=(
            spec("镜头一：远景雨夜街道"),
            spec("镜头二：中景推门入店"),
            spec("镜头三：特写热气与拉花"),
        ),
        failure_index=2,
        redo_index=2,
    ),
    Case(
        "redo-target",
        "这是 M2 逐镜头重做验收。先核对视频能力，再保存一个两镜头分镜（镜头一、镜头二，都是 t2v 五秒），"
        "为两个镜头登记真实任务并用一次 await_job 等待交付，最后逐镜头汇报真实 jobId 与本地交付情况。"
        "不要重做任何镜头：重做只由用户在界面或 CLI 发起。不要修改文本产物。",
        expects="redo_target",
        shots=(spec("镜头一：远景雨夜街道"), spec("镜头二：中景推门入店")),
        redo_index=1,
        stale_redo=True,
    ),
    Case(
        "over-limit",
        "这是 M2 费用护栏验收。先核对视频能力。第一步：保存一个五镜头分镜（每个 5 秒 t2v），为全部五个镜头登记真实任务。"
        "第二步：保存一个单镜头分镜（时长 6 秒 t2v），并登记这个镜头。两次都按工具实际返回处理；"
        "如被拒绝，如实说明拒绝原因与具体上限，不拆分、不降低规格、不重试、不登记替代任务。"
        "最后说明当前真实任务数与剩余额度。",
        expects="over_limit",
        shots=(spec("镜头一：远景雨夜街道"), spec("镜头二：中景推门入店")),
    ),
    Case(
        "unsupported",
        "我需要一条 30 秒、4K、1:1 的真实多镜头视频，并且把上一个镜头延长成续写（镜头延长）。"
        "先核对当前视频能力：如果这些规格或模式不可用，请明确指出不可用的部分和当前已核实的选择，并向我确认。"
        "未经确认不要降低规格、拆分或登记任务，也不要保存或修改文本产物。",
        expects="unsupported",
    ),
    Case(
        "needs-confirmation",
        "这是 M2 确认流程验收。先核对视频能力，保存一个单镜头分镜（镜头一 t2v 五秒），"
        "然后直接登记该镜头，但不要给出显式确认（confirm 留空或设为 false）。"
        "按工具实际返回如实说明是否需要用户确认；不要用确认参数重试，不要声称已登记或有媒体。",
        expects="needs_confirmation",
        shots=(spec("镜头一：远景雨夜街道"),),
        confirm=False,
    ),
)
LIVE_CASES = (
    Case(
        "live-multi-shot",
        "这是 M2 真实多镜头联调。先核对视频能力，保存一个两镜头分镜（镜头一与镜头二，都是 t2v、5 秒），"
        "再一次性为两个镜头登记真实任务（显式确认，回显逐镜头与合计估算以及剩余额度），"
        "然后用一次 await_job 等待集合结果，最后逐镜头汇报真实 jobId、估算与本地交付情况。"
        "不要重做任何镜头，不要修改文本产物，不要重复提交。",
        expects="live_multi_shot",
        shots=(
            spec("镜头一：雨夜街道远景，霓虹与伞影"),
            spec("镜头二：推门入店中景，暖光落在吧台", framing="medium"),
        ),
        redo_index=1,
    ),
)
BRIEF_TEXT = "栖岸咖啡：雨夜青蓝色灯光下，一杯热咖啡置于窗边；镜头缓缓推进，玻璃映出街灯。"
MEDIA_URL = (
    "https://dashscope-result-sh.oss-accelerate.aliyuncs.com/m2-fixture.mp4"
    "?Signature=m2-offline-fixture-signature"
)
FIXTURE = Path(__file__).resolve().parent.parent / "tests" / "fixtures" / "m1c" / "sample-720p.mp4"
M2_TOOLS = {"storyboard_read", "storyboard_save", "shot_generate", "asset_read"}
VIDEO_TOOLS = {"video_capabilities", "video_generate", "job_get", "await_job"}
ACTIVE_STATUSES = {"pending_submit", "submitting", "queued", "running", "downloading"}
MODE_FRAMES = {"t2v": (), "i2v": ("first_frame",), "flf2v": ("first_frame", "last_frame")}
EXPECTED_PRICE_VERSION = {
    "t2v": "wan27-beijing-720p-20261010",
    "i2v": "wan27-i2v-beijing-720p-20261016",
    "flf2v": "wan27-i2v-beijing-720p-20261016",
}
FROZEN_LIMITS = {"maxJobCost": "3.00", "maxNewJobsPerRun": 4, "maxRunCost": "12.00"}


def suite_identity(cases):
    return hashlib.sha256(
        json.dumps(
            {"version": 2, "cases": [vars(case) for case in cases], "brief": BRIEF_TEXT},
            sort_keys=True,
        ).encode()
    ).hexdigest()[:16]


SUITE_ID = suite_identity(CASES)
LIVE_SUITE_ID = suite_identity(LIVE_CASES)

_FIXTURE_BYTES = None


def fixture_bytes():
    global _FIXTURE_BYTES
    if _FIXTURE_BYTES is None:
        _FIXTURE_BYTES = FIXTURE.read_bytes()
    return _FIXTURE_BYTES


def solid_png(width, height, rgb):
    """A real, decodable PNG: the reference loader re-reads the bytes before submitting."""

    def chunk(kind, payload):
        return (
            struct.pack(">I", len(payload))
            + kind
            + payload
            + struct.pack(">I", zlib.crc32(kind + payload) & 0xFFFFFFFF)
        )

    rows = b"".join(b"\x00" + bytes(rgb) * width for _ in range(height))
    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(rows, 6))
        + chunk(b"IEND", b"")
    )


class SuiteClock:
    def __init__(self):
        self.value = datetime.now(UTC)

    def __call__(self):
        return self.value


def advance_clock(clock, service):
    """Advance the fixture clock to the next persisted deadline of an in-flight Job.

    The live Worker rate-limits provider calls to one per second per Store, so a
    clock that only jumps to Job deadlines can stall before every Job of one batch
    has been submitted: the remaining Jobs are due, but the gate has not passed.
    While work is in flight, nudge one second past the current value instead.
    """
    jobs = list(service.video_jobs.list())
    deadlines = []
    for job in jobs:
        if job.next_poll_at and not job.query_started_at:
            deadlines.append(datetime.fromisoformat(job.next_poll_at))
        if isinstance(job, JobV3) and job.download and job.download.next_attempt_at:
            deadlines.append(datetime.fromisoformat(job.download.next_attempt_at))
    active = [job for job in jobs if job.status in ACTIVE_STATUSES and job.query_state != "paused"]
    if not active:
        return
    target = min(deadlines) if deadlines else clock.value + timedelta(seconds=1)
    if target <= clock.value:
        target = clock.value + timedelta(seconds=1)
    clock.value = target


class FixtureLedger:
    """Durable controlled-upstream ledger, independent from the application store."""

    def __init__(self, home):
        self.path = home / "m2-fixture.json"
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            self.data = raw if isinstance(raw, dict) else {}
        except (OSError, ValueError):
            self.data = {}

    def case(self, name):
        return self.data.setdefault(
            name,
            {
                "taskIds": [],
                "statuses": {},
                "submissions": [],
                "queryTaskIds": [],
                "downloads": [],
                "hasAuthorization": False,
                "asyncHeader": None,
            },
        )

    def save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_json(self.path, self.data)


class WanShotFixture:
    """Controlled Wan responses for a shot batch; no credentials and no public network."""

    def __init__(self, case, ledger):
        self.case, self.ledger = case, ledger

    def handle(self, request):
        record = self.ledger.case(self.case.name)
        if request.method == "POST":
            body = json.loads(request.content)
            record["submissions"].append(body)
            index = len(record["submissions"])
            task_id = f"m2-fixture-{self.case.name}-{index}"
            record["taskIds"].append(task_id)
            record["statuses"][task_id] = "FAILED" if index == self.case.failure_index else "SUCCEEDED"
            record["hasAuthorization"] = "authorization" in request.headers
            record["asyncHeader"] = request.headers.get("x-dashscope-async")
            self.ledger.save()
            return httpx.Response(
                200,
                json={
                    "request_id": f"fixture-submit-{index}",
                    "output": {"task_id": task_id, "task_status": "PENDING"},
                },
            )
        task = request.url.path.rsplit("/", 1)[-1]
        record["queryTaskIds"].append(task)
        status = record["statuses"].get(task, "SUCCEEDED")
        output = {"task_id": task, "task_status": status}
        payload = {"request_id": f"fixture-query-{len(record['queryTaskIds'])}", "output": output}
        if status == "SUCCEEDED":
            output["video_url"] = MEDIA_URL
            # Pin the live shape: the real service reports SR numerically and the
            # input duration stays 0 for text-to-video.
            payload["usage"] = {
                "duration": 5,
                "output_video_duration": 5,
                "input_video_duration": 0,
                "video_count": 1,
                "SR": 720,
                "ratio": "16:9",
            }
        else:
            output["code"] = "DataInspectionFailed"
        self.ledger.save()
        return httpx.Response(200, json=payload)


class MediaFixture:
    """Serves the committed local MP4 fixture; optionally fails the first attempts."""

    def __init__(self, case, ledger):
        self.case, self.ledger = case, ledger
        self.data = fixture_bytes()

    def handle(self, request):
        record = self.ledger.case(self.case.name)
        record["downloads"].append({"host": request.url.host, "failed": False})
        self.ledger.save()
        return httpx.Response(
            200,
            content=self.data,
            headers={"Content-Type": "video/mp4", "Content-Length": str(len(self.data))},
        )


def reference_asset_ids(prompt):
    """The seeded reference images the prompt names, in the order the shots cite them."""
    pair = re.search(r"参考图依次 ([0-9a-f-]{36})、([0-9a-f-]{36})", prompt)
    if pair:
        return list(pair.groups())
    single = re.search(r"参考图 ([0-9a-f-]{36})", prompt)
    return [single.group(1)] if single else []


class FixtureModel:
    """Derive scripted M2 decisions from the transcript so explicit resume is repeatable."""

    name = "offline-m2-fixture"

    async def generate_stream(self, messages, tools, delta):
        reply = await self.generate(messages, tools)
        for offset in range(0, len(reply.content), 16):
            delta(reply.content[offset : offset + 16])
        return reply

    async def generate(self, messages, tools):
        start = max(i for i, message in enumerate(messages) if isinstance(message, HumanMessage))
        prompt = messages[start].content
        case = next(case for case in CASES if prompt.startswith(case.prompt.split("{")[0]))
        results = [message for message in messages[start:] if isinstance(message, ToolMessage)]
        sequence = [message.name for message in results]
        latest = {}
        for message in results:
            latest[message.name] = json.loads(message.content)
        asset_ids = reference_asset_ids(prompt)
        source_id = re.search(r"brief ([a-z0-9-]+)", prompt).group(1) if case.source else None

        def calls(*items):
            return AIMessage(
                content="",
                tool_calls=[
                    {"id": f"fixture-{len(results)}-{index}", "name": name, "args": args}
                    for index, (name, args) in enumerate(items)
                ],
            )

        def shot_arguments(items):
            rows = []
            for description, framing, mode, duration, frames in items:
                row = {
                    "description": description,
                    "framing": framing,
                    "mode": mode,
                    "durationSeconds": duration,
                }
                if frames:
                    row["referenceAssetIds"] = asset_ids[:frames]
                rows.append(row)
            return {
                "title": f"{case.name} 分镜",
                "shots": rows,
                **({"sourceRefs": [{"artifactId": source_id, "version": 1}]} if case.source else {}),
            }

        if "video_capabilities" not in sequence:
            reads = [("video_capabilities", {})]
            if case.source:
                reads.append(("artifact_read", {"artifactId": source_id, "version": 1}))
            if case.reference:
                reads.append(("asset_read", {}))
            return calls(*reads)
        if case.expects == "unsupported":
            return AIMessage(
                content="当前真实视频能力只有 t2v 与 i2v/flf2v、2–15 整数秒、720P，图片模式画幅由输入图决定；"
                "不支持 30 秒、4K、1:1，也没有镜头延长（续写）条目。请确认是否采用 5 秒 720P 的多镜头方案？"
                "尚未登记任何任务，也没有媒体。",
            )
        if case.expects == "over_limit":
            saves = [entry["data"] for entry in scripted(results, "storyboard_save")]
            generates = scripted(results, "shot_generate")
            if not saves:
                five = [spec(f"镜头{index}：雨夜街道") for index in range(1, 6)]
                return calls(("storyboard_save", shot_arguments(five)))
            if not generates:
                return calls(("shot_generate", generation_call(saves[0], confirm=True)))
            if len(saves) == 1:
                return calls(("storyboard_save", shot_arguments((spec("六秒镜头：雨夜街道", duration=6),))))
            if len(generates) == 1:
                return calls(("shot_generate", generation_call(saves[1], confirm=True)))
            first, second = generates
            return AIMessage(
                content=f"两次登记都被服务端整批拒绝：五个镜头是 {first['error']['code']}，"
                f"六秒镜头是 {second['error']['code']}。上限是每 Job 3.00 CNY、每 Run 4 个新任务、"
                "每 Run 累计 12.00 CNY。当前真实任务 0 个，没有媒体；我没有降低规格、拆分或重试。"
            )
        if "storyboard_save" not in sequence:
            return calls(("storyboard_save", shot_arguments(case.shots)))
        if case.expects == "draft_only":
            rows = [
                f"{description}（{mode} {duration} 秒）" for description, _, mode, duration, _ in case.shots
            ]
            total = sum(Decimal("0.60") * row[3] for row in case.shots)
            return AIMessage(
                content="分镜草案已保存，未登记任务。"
                + "；".join(rows)
                + f"；逐镜头估算各 3.00 CNY，合计 {total:.2f} CNY。没有任务也没有媒体。"
            )
        generates = scripted(results, "shot_generate")
        if not generates:
            return calls(
                ("shot_generate", generation_call(latest["storyboard_save"]["data"], confirm=case.confirm))
            )
        if case.expects == "needs_confirmation":
            refusal = generates[0]
            if refusal.get("ok"):
                # An unconfirmed batch must never register: report it as it happened.
                registered = "、".join(entry["jobId"] for entry in refusal["data"]["shots"])
                return AIMessage(content=f"未确认的登记被接受，已登记任务：{registered}；媒体尚未交付。")
            return AIMessage(
                content=f"登记未执行：{refusal['error']['code']}——{refusal['error']['message']}"
                "需要用户显式确认后才登记。当前没有任务，也没有媒体。"
            )
        if not generates[0]["ok"]:
            raise AppError("FIXTURE_TOOL_FAILED", f"离线夹具登记失败：{generates[0]['error']['code']}")
        if case.replay and len(generates) == 1:
            return calls(("shot_generate", generation_call(latest["storyboard_save"]["data"], confirm=True)))
        if "await_job" not in sequence:
            job_ids = sorted(entry["jobId"] for entry in generates[-1]["data"]["shots"])
            return calls(("await_job", {"jobIds": job_ids}))
        summary = latest["await_job"]["data"]
        delivered = [entry for entry in summary["jobs"] if entry["ok"]]
        failed = [entry for entry in summary["jobs"] if not entry["ok"]]
        ids = "、".join(entry["jobId"] for entry in delivered)
        text = f"集合等待结束：{len(delivered)} 个镜头已本地交付（可播放可下载）：{ids}；{len(failed)} 个未完成。"
        if failed:
            codes = "、".join(f"{entry['jobId']}（{entry['error']['code']}）" for entry in failed)
            text += f"失败镜头：{codes}；不自动重做，保留原任务可单独重做。"
        text += "本次合计估算 " + generates[-1]["data"]["totalEstimate"]["amount"] + " CNY。"
        return AIMessage(content=text)


def scripted(results, name):
    """The parsed tool results of one tool inside the current turn, in call order."""
    return [json.loads(message.content) for message in results if message.name == name]


def generation_call(saved, *, confirm):
    """The public shot_generate arguments of one saved storyboard version."""
    return {
        "storyboardId": saved["storyboardId"],
        "storyboardVersion": saved["version"],
        "shotIds": saved["shotIds"],
        "confirm": confirm,
    }


def artifact_id_from(prompt):
    return re.search(r"brief ([a-z0-9-]+)", prompt).group(1)


def domain(snapshot):
    """The text-side state an M2 generation Run must never touch: project memory and artifacts."""
    return {"projects": snapshot["projects"], "artifacts": snapshot["artifacts"]}


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


def scrubbed_job(raw):
    """Raw Job record plus public projection fields, without private provider data."""
    scrubbed = copy.deepcopy(raw)
    if scrubbed["request"].pop("workspaceId", None) is not None:
        scrubbed["request"]["workspaceScopeRecorded"] = True
    output = scrubbed.get("providerOutput")
    if output:
        host = urlsplit(output.get("videoUrl") or "").hostname
        output["videoUrl"] = "<redacted>"
        output["urlHost"] = host
    scrubbed["simulated"] = scrubbed.get("mode") == "mock"
    scrubbed["mediaAvailable"] = scrubbed.get("mediaAvailability", {}).get("status") == "available"
    scrubbed["mediaRefs"] = (scrubbed.get("result") or {}).get("mediaRefs") or []
    return scrubbed


def job_identity(job):
    """Everything a redo of another shot must leave untouched."""
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
    names = ["state.json", "checkpoints.sqlite", "m2-fixture.json", "mock-video.json"]
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


def media_checks(service, item, facts):
    """Every delivered shot's local MP4 against the fixture and the MP4 contract."""
    checks = {}
    for asset in item["assets"]:
        path = service.store.home / asset["relativePath"]
        data = path.read_bytes() if path.is_file() else b""
        metadata = asset["metadata"]
        flags = {
            "sha256": asset["sha256"],
            "sizeBytes": asset["sizeBytes"],
            "fileBytes": len(data),
            "assetSha256MatchesFile": bool(data) and hashlib.sha256(data).hexdigest() == asset["sha256"],
            "h264": metadata["videoCodec"] == "h264",
            "spec720p5s": metadata["width"] == 1280
            and metadata["height"] == 720
            and abs(metadata["durationSeconds"] - 5) <= 0.1000001,
            "hasAudio": metadata["hasAudio"],
        }
        if facts is not None:
            flags["matchesCommittedFixture"] = (
                asset["sha256"] == facts["sha256"] and asset["sizeBytes"] == facts["sizeBytes"]
            )
        checks[asset["id"]] = flags
    return checks


async def probe_media_api(service, config, item):
    """The played-back media surface: detail, GET, HEAD, one Range, 416 and download."""
    delivered = [
        job
        for job in item["jobs"]
        if job["status"] == "succeeded" and job.get("mediaAvailable") and job.get("mediaRefs")
    ]
    if not delivered:
        return None
    by_media = {raw["id"]: raw for raw in item["assets"]}
    app = create_app(config)
    app.state.service = service
    results = {}
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:3210"
    ) as client:
        for job in delivered:
            media_id = job["mediaRefs"][0]["mediaId"]
            asset = by_media.get(media_id)
            if asset is None:
                results[media_id] = {"mediaRecorded": False}
                continue
            size = asset["sizeBytes"]
            url = f"/api/media/{media_id}/content"
            detail = await client.get(f"/api/media/{media_id}")
            full = await client.get(url)
            head = await client.head(url)
            ranged = await client.get(url, headers={"Range": "bytes=0-99"})
            bad = await client.get(url, headers={"Range": f"bytes={size + 10}-"})
            attachment = await client.get(url + "?download=1")
            results[media_id] = {
                "mediaRecorded": True,
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
                "etagMatchesSha": full.headers.get("etag", "").strip('"') == asset["sha256"],
            }
    return results


def upstream_view(item, *, offline, ledger, case):
    if offline:
        record = ledger.case(case.name)
        return {
            "source": "fixture",
            "submitCalls": len(record["submissions"]),
            "queryCalls": len(record["queryTaskIds"]),
            "submitBodies": record["submissions"],
            "taskIds": record["taskIds"],
            "statuses": record["statuses"],
            "hasAuthorization": record["hasAuthorization"],
            "asyncHeader": record["asyncHeader"],
            "queryTaskIds": record["queryTaskIds"],
            "downloadCalls": len(record["downloads"]),
            "downloadHosts": sorted({entry["host"] for entry in record["downloads"]}),
        }
    jobs = item["jobs"]
    return {
        "source": "durable",
        "submitCalls": sum(job["submitAttempts"] for job in jobs),
        "queryCalls": sum(job["queryAttempts"] for job in jobs),
        "downloadCalls": sum((job["download"] or {}).get("attempts", 0) for job in jobs),
        "taskIds": [job["providerTaskId"] for job in jobs],
    }


def capture_state(service, item, case, *, offline, ledger, full):
    state = service.store.snapshot()
    run_id = state["sessions"][case.name].get("latestRunId")
    if not run_id:
        return
    record = state["runs"][run_id]
    item["run"] = {
        **service.run_view(record),
        "modelCalls": record["modelCalls"],
        "executionVersion": record["executionVersion"],
        "contextSignature": record["contextSignature"],
    }
    item["toolResults"] = tool_results(record)
    item["jobs"] = [
        scrubbed_job(raw) for raw in state["jobs"].values() if raw["context"]["sessionId"] == case.name
    ]
    item["assets"] = [
        copy.deepcopy(asset) for asset in state["media"].values() if asset["projectId"] == case.name
    ]
    item["libraryAssets"] = [
        copy.deepcopy(asset) for asset in state["assets"].values() if asset["projectId"] == case.name
    ]
    item["waits"] = [binding for binding in state["waits"].values() if binding["context"]["runId"] == run_id]
    item["operations"] = {key: op for key, op in state["operations"].items() if key.startswith(f"{run_id}:")}
    item["upstream"] = upstream_view(item, offline=offline, ledger=ledger, case=case)
    if full:
        item["after"] = domain(state)
        try:
            item["storyboard"] = service.storyboard(case.name)
        except AppError:
            item["storyboard"] = None
        item["shotVersions"] = {
            shot_id: len(
                [
                    entry
                    for entry in state["storyboards"][item["storyboard"]["storyboardId"]]["shotVersions"]
                    if entry["shotId"] == shot_id
                ]
            )
            for shot_id in (
                [shot["id"] for shot in item["storyboard"]["shots"]] if item["storyboard"] else []
            )
        }


async def capture_full(service, item, case, *, offline, ledger, config, facts):
    capture_state(service, item, case, offline=offline, ledger=ledger, full=True)
    item["mediaChecks"] = media_checks(service, item, facts)
    item["mediaApi"] = await probe_media_api(service, config, item) if item["mediaChecks"] else None
    item["mediaFileHashes"] = media_file_hashes(service.store.home, service.store.snapshot())


def all_media_flags_ok(checks, *, offline):
    if not checks:
        return False
    keys = ["assetSha256MatchesFile", "h264", "spec720p5s"]
    if offline:
        keys.append("matchesCommittedFixture")
    return all(all(flags.get(key) is True for key in keys) for flags in checks.values())


def media_api_ok(probes):
    return bool(probes) and all(all(value.values()) for value in probes.values())


def no_media_claim(answer):
    no_media = bool(
        re.search(
            r"(?:没有|无法|不能|未|无|不(?:会)?(?:能|支持|生成|交付|提供|产生|包含)|不具备|不存在)"
            r"[^。；\n]{0,50}(?:真实|可播放|媒体|视频)",
            answer,
        )
    )
    fabricated = re.search(r"https?://|(?:已生成|已交付)(?:了)?真实视频|点击.{0,12}(?:播放|下载)", answer)
    return no_media and not fabricated


def shots_by_order(item):
    view = item.get("storyboard") or {}
    return {shot["id"]: shot for shot in view.get("shots", [])}


def grade(case, item, *, offline):
    run, jobs, upstream = item["run"], item["jobs"], item["upstream"]
    answer = re.sub(r"[*`]", "", run["answer"])
    trace = run["toolTrace"]
    results = item["toolResults"]
    names = [call["name"] for call in trace]
    result_ids = [result["id"] for result in results]
    successful = {result["name"] for result in results if result["result"].get("ok")}
    checks = {
        "run_completed": run["status"] == "completed",
        "within_original_budget": run["modelSteps"] <= run["policy"]["maxSteps"] <= 8
        and run["toolCalls"] <= run["policy"]["maxToolCalls"] <= 12
        and run["activeSeconds"] <= run["policy"]["timeoutSeconds"] <= 180,
        "model_attempts_accounted": len(run["modelCalls"]) == run["modelSteps"],
        "no_unrequested_text_changes": item["before"] == item["after"],
        "one_result_per_original_call": len(result_ids) == len(set(result_ids)) == len(trace)
        and set(result_ids) == {call["id"] for call in trace},
        "capability_read": "video_capabilities" in successful,
        "m2_tools_visible": M2_TOOLS.issubset(set(item["toolInventory"])),
        "video_tools_visible": VIDEO_TOOLS.issubset(set(item["toolInventory"])),
        "saved_mode_live": run["videoMode"] == "live" and run["executionVersion"] == WAIT_EXECUTION_VERSION,
        "private_url_not_exposed": "Signature" not in json.dumps(item["toolResults"], ensure_ascii=False)
        and "Signature" not in answer
        and "Signature" not in json.dumps(jobs, ensure_ascii=False)
        and "workspaceId" not in json.dumps(item["toolResults"], ensure_ascii=False)
        and "relativePath" not in json.dumps(item["toolResults"], ensure_ascii=False),
    }
    if case.expects == "unsupported":
        checks.update(
            no_jobs_or_submissions=not jobs and upstream["submitCalls"] == 0 and not upstream["taskIds"],
            no_generation_attempt="shot_generate" not in names,
            unsupported_explained=bool(re.search(r"不支持|不可用|不能|无法|没有", answer))
            and "30" in answer
            and "4k" in answer.lower()
            and "延长" in answer,
            asks_for_confirmation=bool(re.search(r"确认|选择|是否|哪[种个]|[？?]", answer)),
            no_fabricated_media_claim=no_media_claim(answer),
        )
        return checks
    if case.expects == "needs_confirmation":
        attempts = [result for result in results if result["name"] == "shot_generate"]
        checks.update(
            confirmation_required=bool(attempts)
            and all(
                not result["result"].get("ok")
                and result["result"]["error"]["code"] == "CONFIRMATION_REQUIRED"
                for result in attempts
            )
            and all(
                call["arguments"].get("confirm") is not True
                for call in trace
                if call["name"] == "shot_generate"
            ),
            registered_nothing=not jobs
            and upstream["submitCalls"] == 0
            and not any(item["shotVersions"].values()),
            confirmation_explained="CONFIRMATION_REQUIRED" in answer or "确认" in answer,
            no_fabricated_media_claim=no_media_claim(answer),
        )
        return checks
    if case.expects == "over_limit":
        refused = [result["result"] for result in results if result["name"] == "shot_generate"]
        codes = [entry.get("error", {}).get("code") for entry in refused]
        checks.update(
            batch_refused=len(refused) == 2 and codes == ["VIDEO_RUN_JOB_LIMIT", "VIDEO_COST_LIMIT"],
            registered_nothing=not jobs
            and upstream["submitCalls"] == 0
            and not upstream["taskIds"]
            and not any(item["shotVersions"].values()),
            limits_explained="4" in answer and "3.00" in answer and "12.00" in answer,
            no_retry_or_split=names.count("shot_generate") == 2,
            no_fabricated_media_claim=no_media_claim(answer),
        )
        return checks
    if case.expects == "draft_only":
        shots = (item.get("storyboard") or {}).get("shots", [])
        checks.update(
            storyboard_saved=(item.get("storyboard") or {}).get("currentVersion") == 1
            and len(shots) == len(case.shots),
            shots_ordered=[shot["order"] for shot in shots] == list(range(1, len(case.shots) + 1)),
            shot_modes=[shot["mode"] for shot in shots] == [row[2] for row in case.shots],
            shot_identities=len({shot["id"] for shot in shots}) == len(case.shots),
            reference_frames_bound=all(
                len(shot["referenceAssetIds"]) == MODE_FRAMES[shot["mode"]].__len__()
                and all(
                    any(asset["id"] == asset_id for asset in item["libraryAssets"])
                    for asset_id in shot["referenceAssetIds"]
                )
                for shot in shots
            ),
            source_version_preserved=(item.get("storyboard") or {}).get("sourceRefs")
            == [{"artifactId": item["sourceId"], "version": 1}],
            source_was_read="artifact_read" in successful and "asset_read" in successful,
            registered_nothing=not jobs
            and upstream["submitCalls"] == 0
            and not any(item["shotVersions"].values()),
            no_generation_attempt="shot_generate" not in names,
            estimate_echoed="9.00" in answer and "3.00" in answer,
            modes_echoed=all(row[2] in answer for row in case.shots),
            no_fabricated_media_claim=no_media_claim(answer),
        )
        return checks
    run_jobs = item.get("runJobs", jobs)
    run_upstream = item.get("runUpstream", upstream)
    redo_job = (item.get("redo") or {}).get("body", {}).get("shots", [{}])[0].get("jobId")
    checks["shot_count"] = len(run_jobs) == len(case.shots)
    if not checks["shot_count"]:
        return checks
    checks.update(
        capability_before_generation="video_capabilities" in names
        and "shot_generate" in names
        and names.index("video_capabilities") < names.index("shot_generate"),
        every_job_is_a_shot=all("shotRef" in job for job in jobs),
        shot_refs_match_storyboard=sorted(job["shotRef"]["shotId"] for job in run_jobs if "shotRef" in job)
        == sorted(shot["id"] for shot in (item.get("storyboard") or {}).get("shots", [])),
        submit_accounted=run_upstream["submitCalls"] == len(run_jobs)
        and all(job["submitAttempts"] == 1 for job in run_jobs),
        cost_recorded=all(
            job["cost"]["estimate"]["amount"] == "3.00"
            and job["cost"]["estimate"]["currency"] == "CNY"
            and job["cost"]["estimate"]["priceVersion"]
            == EXPECTED_PRICE_VERSION[job["request"]["spec"]["mode"]]
            and job["cost"]["actual"]["status"] == "unknown"
            for job in run_jobs
        ),
        quota_snapshot=all(job["cost"]["limits"]["maxNewJobsPerRun"] == 4 for job in run_jobs),
    )
    requests = [job["request"] for job in run_jobs]
    checks["frozen_requests"] = all(
        request["providerPrompt"] == WAN_PROMPT_PREFIX + request["prompt"]
        and request["region"] == "cn-beijing"
        and request["adapterVersion"] == "wan-http-v2"
        and request["capabilitiesVersion"] == WAN_SHOT_CAPABILITIES_VERSION
        and request["parameters"]["duration"] == request["spec"]["durationSeconds"]
        and request["parameters"]["prompt_extend"] is False
        and request["parameters"]["watermark"] is True
        and request["parameters"]["seed"] == 0
        and request.get("workspaceScopeRecorded") is True
        for request in requests
    )
    checks["frozen_modes"] = all(
        (
            request["model"] == WAN_MODEL
            and request["spec"]["mode"] == "t2v"
            and request["parameters"]["ratio"] == "16:9"
            and not request["media"]
        )
        or (
            request["model"] == WAN_I2V_MODEL
            and request["spec"]["mode"] in {"i2v", "flf2v"}
            and request["parameters"].get("ratio") is None
            and [item["frame"] for item in request["media"]] == list(MODE_FRAMES[request["spec"]["mode"]])
            and all(item["sha256"] for item in request["media"])
        )
        for request in requests
    )
    if offline:
        by_prompt = {request["providerPrompt"]: request for request in requests}
        bodies = run_upstream["submitBodies"]
        frozen_bodies = []
        for body in bodies:
            request = by_prompt.get(body["input"]["prompt"])
            if request is None:
                frozen_bodies.append(False)
                continue
            media = body["input"].get("media")
            if request["spec"]["mode"] == "t2v":
                frozen_bodies.append(
                    body["model"] == request["model"]
                    and body["parameters"]["resolution"] == "720P"
                    and body["parameters"]["ratio"] == "16:9"
                    and "media" not in body["input"]
                )
            else:
                frozen_bodies.append(
                    body["model"] == request["model"]
                    and body["parameters"]["resolution"] == "720P"
                    and "ratio" not in body["parameters"]
                    and bool(media)
                    and [entry["type"] for entry in media] == list(MODE_FRAMES[request["spec"]["mode"]])
                    and all(entry["url"].startswith("data:image/png;base64,") for entry in media)
                )
        checks["frozen_submit_bodies"] = run_upstream["submitCalls"] == len(requests) == len(bodies) and all(
            frozen_bodies
        )
        checks["submit_headers"] = run_upstream["hasAuthorization"] is True
        checks["async_header"] = run_upstream["asyncHeader"] == "enable"
        submitted = set(run_upstream["taskIds"])
        checks["original_task_identity"] = (
            len({job["providerTaskId"] for job in run_jobs}) == len(run_jobs)
            and all(job["providerTaskId"] in submitted for job in run_jobs)
            and submitted.issubset(set(run_upstream["queryTaskIds"]))
        )
    else:
        checks["original_task_identity"] = all(job["providerTaskId"] for job in run_jobs)
    failed = [job for job in run_jobs if job["status"] == "failed"]
    delivered = [job for job in run_jobs if job["status"] == "succeeded"]
    if case.expects in {"batch_delivered", "redo_target", "live_multi_shot"}:
        checks.update(
            all_delivered=len(delivered) == len(run_jobs)
            and len(jobs) == len(run_jobs) + (1 if redo_job else 0),
            media_delivered=all(
                job["mediaAvailable"] is True
                and job["mediaAvailability"]["status"] == "available"
                and len(job["mediaRefs"]) == 1
                and job["result"]["mediaAvailable"] is True
                for job in delivered
            ),
            media_matches_local_file=all_media_flags_ok(item["mediaChecks"], offline=offline),
            download_accounted=all(
                (job["download"] or {}).get("phase") == "committed"
                for job in jobs
                if job["status"] == "succeeded"
            ),
            media_api_ok=media_api_ok(item["mediaApi"]),
            answer_reports_delivery=bool(re.search(r"交付|播放|下载|媒体", answer)),
            answer_cites_jobs=all(job["id"] in answer for job in delivered),
            no_fabricated_media_claim=not re.search(r"https?://", answer),
        )
    if case.expects == "batch_delivered":
        generates = [result for result in results if result["name"] == "shot_generate"]
        checks.update(
            batch_echoed=bool(generates)
            and generates[0]["result"]["data"]["totalEstimate"] == {"currency": "CNY", "amount": "9.00"}
            and generates[0]["result"]["data"]["limits"]
            == {
                **FROZEN_LIMITS,
                "usedNewJobs": 3,
                "usedAmount": "9.00",
                "remainingAmount": "3.00",
                # E3 added the snapshot scope; this Run stays on the frozen per-Run one.
                "scope": "run",
                "episodeId": None,
            },
            replay_deduplicated=len(generates) >= 2
            and generates[1]["result"].get("ok")
            and all(row["reused"] is True for row in generates[1]["result"]["data"]["shots"])
            and len(jobs) == 3,
        )
    if case.expects in {"partial_failure", "live_multi_shot"}:
        attempts = [result for result in results if result["name"] == "await_job"]
        checks.update(
            set_wait_registered=bool(item["waits"])
            and all(binding["resource"]["kind"] == "jobset" for binding in item["waits"]),
            set_wait_delivered=bool(attempts)
            and all(
                binding["status"] == "delivered"
                and next(
                    result["result"]
                    for result in attempts
                    if result["id"] == binding["context"]["toolCallId"]
                )
                == item["operations"]
                .get(ToolExecutionContext.model_validate(binding["context"]).operation_key, {})
                .get("result")
                == binding["result"]
                for binding in item["waits"]
            ),
            waiting_does_not_call_model=all(sample["unchanged"] for sample in item["waitingSamples"]),
        )
    if case.expects in {"partial_failure", "redo_target", "live_multi_shot"}:
        checks.update(**redo_checks(case, item))
    if case.expects == "partial_failure":
        summary = None
        for result in results:
            if result["name"] == "await_job":
                summary = result["result"]["data"]
        checks.update(
            one_provider_failure=len(failed) == 1
            and failed[0]["error"]["stage"] == "generate"
            and failed[0]["error"]["code"] == "PROVIDER_GENERATION_FAILED"
            and failed[0]["providerTaskId"] is not None,
            partial_summary_delivered=bool(summary)
            and summary["failed"] == 1
            and summary["succeeded"] == 2
            and all(
                item_entry["error"]["code"] == "JOB_FAILED"
                for item_entry in summary["jobs"]
                if not item_entry["ok"]
            ),
            others_still_delivered=len(delivered) == 2 and all(job["mediaAvailable"] for job in delivered),
            media_matches_local_file=all_media_flags_ok(item["mediaChecks"], offline=offline),
            media_api_ok=media_api_ok(item["mediaApi"]),
            failure_reported=failed[0]["id"] in answer
            and bool(re.search(r"失败|PROVIDER_GENERATION_FAILED|JOB_FAILED", answer)),
        )
    return checks


def redo_checks(case, item):
    """The explicit user redo: one new shot version and Job, nothing else moves."""
    redo = item.get("redo") or {}
    replay, stale = redo.get("replay") or {}, redo.get("stale") or {}
    target = redo.get("shotId")
    before, after = redo.get("before") or {}, redo.get("after") or {}
    delta = redo.get("versionCountDelta") or {}
    hashes = redo.get("mediaHashes") or {}
    untouched_bytes = hashes.get("before") or {}
    checks = {
        "redo_registered_one_job": bool(redo.get("body"))
        and len(redo["body"].get("shots", [])) == 1
        and redo["body"]["shots"][0]["shotId"] == target,
        "redo_added_one_shot_version": delta.get(target) == 1
        and all(count == 0 for shot_id, count in delta.items() if shot_id != target),
        "redo_left_other_jobs_untouched": bool(before)
        and all(before[job_id] == after.get(job_id, {}).get("identity") for job_id in before),
        "redo_left_other_media_untouched": bool(untouched_bytes)
        and all(
            (hashes.get("after") or {}).get(media_id) == digest
            for media_id, digest in untouched_bytes.items()
        ),
        "redo_replay_is_idempotent": replay.get("body") == redo.get("body")
        and replay.get("jobs") == replay.get("jobsAfterReplay"),
        "redo_new_job_delivered": bool(redo.get("delivered"))
        and redo["delivered"]["status"] == "succeeded"
        and redo["delivered"]["mediaAvailable"] is True,
    }
    if case.stale_redo:
        checks["stale_redo_refused"] = stale.get("code") == "VERSION_CONFLICT" and stale.get("jobsUnchanged")
    return checks


def totals(report):
    cases = LIVE_CASES if report["mode"] == "live" else CASES
    items = [item for item in report["cases"] if item.get("run")]
    runs = [item["run"] for item in items]
    calls = [call for run in runs for call in run["modelCalls"]]
    known = [
        call for call in calls if call.get("inputTokens") is not None and call.get("outputTokens") is not None
    ]
    return {
        "plannedCases": len(cases),
        "recordedCases": len(items),
        "passedCases": sum(bool(item.get("checks")) and all(item["checks"].values()) for item in items),
        "modelCalls": len(calls),
        "toolCalls": sum(run["toolCalls"] for run in runs),
        "submitCalls": sum(item.get("upstream", {}).get("submitCalls", 0) for item in items),
        "queryCalls": sum(item.get("upstream", {}).get("queryCalls", 0) for item in items),
        "downloadCalls": sum(item.get("upstream", {}).get("downloadCalls", 0) for item in items),
        "shotsRegistered": sum(len(item.get("jobs", [])) for item in items),
        "shotsDelivered": sum(
            1
            for item in items
            for job in item.get("jobs", [])
            if job["status"] == "succeeded" and job.get("mediaAvailable")
        ),
        "redos": sum(1 for item in items if (item.get("redo") or {}).get("body")),
        "explicitResumes": len(report.get("resumes", [])),
        "automaticResumes": sum(
            event["type"] == "run.resumed" and event.get("resumeKind", "automatic") == "automatic"
            for item in items
            for event in item["applicationEvents"]
        ),
        "connectionRetries": sum(item.get("connectionRetries", 0) for item in items),
        "activeSeconds": round(sum(run["activeSeconds"] for run in runs), 3),
        "externalWaitSeconds": round(sum(run.get("externalWaitSeconds") or 0 for run in runs), 3),
        "estimatedAmount": str(
            sum(
                (
                    Decimal(job["cost"]["estimate"]["amount"])
                    for item in items
                    for job in item.get("jobs", [])
                ),
                Decimal(0),
            )
        ),
        "observedInputTokens": sum(call.get("inputTokens") or 0 for call in calls),
        "observedOutputTokens": sum(call.get("outputTokens") or 0 for call in calls),
        "callsWithTokenUsage": len(known),
        "callsWithUnknownUsage": len(calls) - len(known),
        "tokenUsageComplete": bool(calls) and len(known) == len(calls),
    }


class patches:
    """Route the application's provider and media construction through offline fixtures."""

    def __init__(self, wan_fixture, media_fixture, clock, *, live):
        self.entries = []
        if not live:
            self.entries = [
                patch(
                    "vagent.application.WanAdapter",
                    lambda **kwargs: WanAdapter(**kwargs, transport=httpx.MockTransport(wan_fixture.handle)),
                ),
                patch(
                    "vagent.application.MediaHttpClient",
                    lambda: MediaHttpClient(transport=httpx.MockTransport(media_fixture.handle)),
                ),
                patch(
                    "vagent.application.JobService",
                    lambda *args, **kwargs: JobService(*args, **kwargs, clock=clock),
                ),
                patch(
                    "vagent.application.JobWorker",
                    lambda service: JobWorker(service, idle_interval_seconds=0.05),
                ),
                patch(
                    "vagent.application.MediaWorker",
                    lambda media, client: MediaWorker(media, client, idle_interval_seconds=0.05),
                ),
            ]

    def __enter__(self):
        for entry in self.entries:
            entry.start()
        return self

    def __exit__(self, *_):
        for entry in reversed(self.entries):
            entry.stop()


async def settle(service, case, *, live, clock):
    pause = 0.5 if live else 0.01
    while True:
        jobs = list(service.video_jobs.list(session_id=case.name))
        active = [job for job in jobs if job.status in ACTIVE_STATUSES and job.query_state != "paused"]
        if not active:
            return
        if not live:
            advance_clock(clock, service)
        await asyncio.sleep(pause)


async def drive(service, item, case, *, live, clock):
    run_id = service.store.snapshot()["sessions"][case.name]["latestRunId"]
    waiter = asyncio.create_task(service.wait_for_run(run_id))
    waiting_before = {}
    pause = 0.5 if live else 0.01
    try:
        async with asyncio.timeout(3600 if live else 60):
            while not waiter.done():
                run = service.run_record(run_id)
                if run["status"] == "waiting_external":
                    wait_id = run["activeWaitId"]
                    counters = {key: run[key] for key in ("modelSteps", "toolCalls", "activeSeconds")}
                    if wait_id in waiting_before:
                        item["waitingSamples"].append(
                            {"waitId": wait_id, "before": waiting_before.pop(wait_id), "after": counters}
                        )
                        item["waitingSamples"][-1]["unchanged"] = (
                            item["waitingSamples"][-1]["before"] == counters
                        )
                    elif not any(sample["waitId"] == wait_id for sample in item["waitingSamples"]):
                        waiting_before[wait_id] = counters
                if not live and run["status"] != "running" and not waiting_before:
                    advance_clock(clock, service)
                await asyncio.sleep(pause)
            record = await waiter
            item["modelCallsAtReply"] = len(record["modelCalls"])
            await settle(service, case, live=live, clock=clock)
    finally:
        if not waiter.done():
            waiter.cancel()
        await asyncio.gather(waiter, return_exceptions=True)


async def redo_shot(service, item, case, *, live, clock):
    """One explicit user redo, exactly as the CLI or the Web panel performs it."""
    view = item["storyboard"]
    target = view["shots"][case.redo_index - 1]["id"]
    before_jobs = {job["id"]: job_identity(job) for job in item["jobs"]}
    versions_before = dict(item["shotVersions"])
    hashes_before = media_file_hashes(service.store.home, service.store.snapshot())
    request = {
        "clientRequestId": f"eval-{case.name}-redo",
        "storyboardId": view["storyboardId"],
        "storyboardVersion": view["currentVersion"],
        "expectedRevision": view["revision"],
    }
    body = service.redo_shot(case.name, target, request)
    await settle(service, case, live=live, clock=clock)
    jobs_after = {job["jobId"]: job_identity(job) for job in service.jobs(case.name)}
    state = service.store.snapshot()
    record = state["storyboards"][view["storyboardId"]]
    hashes_after = media_file_hashes(service.store.home, state)
    after_versions = {
        shot_id: len([entry for entry in record["shotVersions"] if entry["shotId"] == shot_id])
        for shot_id in versions_before
    }
    new_job = body["shots"][0]["jobId"]
    redo = {
        "shotId": target,
        "body": body,
        "before": before_jobs,
        "after": {
            job_id: {"identity": identity, "isRedo": job_id == new_job}
            for job_id, identity in jobs_after.items()
        },
        "versionCountDelta": {
            shot_id: after_versions[shot_id] - versions_before[shot_id] for shot_id in versions_before
        },
        "delivered": next((job for job in service.jobs(case.name) if job["jobId"] == new_job), None),
        "mediaHashes": {"before": hashes_before, "after": hashes_after},
    }
    replay = service.redo_shot(case.name, target, request)
    redo["replay"] = {
        "body": replay,
        "jobs": sorted(jobs_after),
        "jobsAfterReplay": sorted(job["jobId"] for job in service.jobs(case.name)),
    }
    if case.stale_redo:
        stale_request = {**request, "clientRequestId": f"eval-{case.name}-redo-stale", "expectedRevision": 0}
        try:
            service.redo_shot(case.name, target, stale_request)
            redo["stale"] = {"code": None, "jobsUnchanged": False}
        except AppError as error:
            redo["stale"] = {
                "code": error.code,
                "jobsUnchanged": sorted(job["jobId"] for job in service.jobs(case.name))
                == redo["replay"]["jobs"],
            }
    item["redo"] = redo
    item["jobs"] = [
        scrubbed_job(raw) for raw in state["jobs"].values() if raw["context"]["sessionId"] == case.name
    ]
    item["assets"] = [
        copy.deepcopy(asset) for asset in state["media"].values() if asset["projectId"] == case.name
    ]
    item["shotVersions"] = after_versions


def continuation(args, *, live):
    path = getattr(args, "continue_from", None)
    if path is None:
        return None
    cases = LIVE_CASES if live else CASES
    suite = LIVE_SUITE_ID if live else SUITE_ID
    try:
        report = json.loads(path.read_text(encoding="utf-8"))
        last = report["cases"][-1]
        valid = (
            report["schemaVersion"] == 2
            and report["suiteId"] == suite
            and report["mode"] == ("live" if live else "offline-fixture")
            and report["status"] == "failed"
            and last["run"]["resumable"]
            and last["run"]["status"] in {"failed", "cancelled", "interrupted"}
            and [item["case"] for item in report["cases"]]
            == [case.name for case in cases[: len(report["cases"])]]
            and all(all(item["checks"].values()) for item in report["cases"][:-1])
            and 1 <= report["maxModelCalls"] <= 60
        )
        if not valid:
            raise ValueError
        args.home = Path(report["dataDirectory"])
        args.context_bytes = report["contextBudgetBytes"]
        args.max_model_calls = min(args.max_model_calls, report["maxModelCalls"])
    except (OSError, ValueError, KeyError, TypeError, IndexError):
        raise AppError(
            "EVAL_CONTINUE_INVALID", "需使用同一模式、用例集的可恢复失败报告和新的输出文件。"
        ) from None
    if evidence_hashes(args.home) != report.get("evidenceHashes"):
        raise AppError("EVAL_STATE_CHANGED", "原评测数据或检查点已变化；未重发请求，也未重置预算。")
    return report


async def run_suite(args):
    live = bool(args.live)
    cases = LIVE_CASES if live else CASES
    if args.max_model_calls is None:
        args.max_model_calls = 16 if live else 60
    if not 1 <= args.max_model_calls <= 60 or not 1024 <= args.context_bytes <= 131072:
        raise AppError("EVAL_BUDGET_INVALID", "模型调用上限须为1～60，上下文预算须为1024～131072字节。")
    if args.output.exists():
        raise AppError("EVAL_OUTPUT_EXISTS", "输出文件已存在，请使用新路径保留先前证据。")
    facts = None
    if not live:
        if not FIXTURE.is_file():
            raise AppError("EVAL_FIXTURE_MISSING", "缺少本地测试媒体夹具，离线评测未启动。")
        data = fixture_bytes()
        facts = {"sha256": hashlib.sha256(data).hexdigest(), "sizeBytes": len(data)}
    previous = continuation(args, live=live)
    home = args.home.resolve()
    if not previous and home.exists() and any(home.iterdir()):
        raise AppError("EVAL_HOME_NOT_EMPTY", "评测需要新的空数据目录，已有数据未修改。")
    configured = load_config()
    if live:
        require_key(configured.api_key)
        if not (configured.video_api_key and configured.video_workspace_id):
            raise AppError(
                "EVAL_LIVE_CONFIG_MISSING",
                "真实联调需要 VAGENT_DASHSCOPE_KEY 与 VAGENT_DASHSCOPE_WORKSPACE_ID；未发起任何请求。",
            )
    capabilities = [item.model_dump(mode="json", by_alias=True) for item in wan_shot_capabilities()]
    video_settings = {
        "mode": "live",
        "capabilitiesVersion": WAN_SHOT_CAPABILITIES_VERSION,
        "capabilities": capabilities,
        "maxJobCost": configured.video_max_job_cost,
        "maxNewJobsPerRun": configured.video_max_new_jobs_per_run,
        "maxRunCost": configured.video_max_run_cost,
    }
    base = replace(
        configured,
        home=home,
        context_bytes=args.context_bytes,
        redis_url=None,
        skills_root=None,
        mcp_config=None,
        mcp_local=False,
        video_mode="live",
    )
    if not live:
        base = replace(base, video_api_key="offline-video-fixture-key", video_workspace_id="offline-fixture")
    fixture = None if live else FixtureModel()
    model_name = base.model if live else fixture.name
    if previous and previous["model"] != model_name:
        raise AppError("RESUME_CONFIG_CHANGED", "原评测模型已变化，未发起续跑。")
    report = (
        copy.deepcopy(previous)
        if previous
        else {
            "schemaVersion": 2,
            "suiteId": LIVE_SUITE_ID if live else SUITE_ID,
            "mode": "live" if live else "offline-fixture",
            "model": model_name,
            "startedAt": now(),
            "dataDirectory": str(home),
            "contextBudgetBytes": args.context_bytes,
            "videoSettings": video_settings,
            "budget": {
                "maxModelCalls": 16 if live else 60,
                "maxSubmits": 3 if live else None,
                "maxEstimatedAmount": "9.00" if live else None,
            },
            "cases": [],
            "resumes": [],
            "limitations": (
                "固定合成用例与字面规则评分；离线夹具不衡量模型质量，也不代表真实账号、CDN 或账单。"
                "媒体为本地测试夹具，上游为受控 HTTP。"
                if not live
                else "双镜头加一次单镜头重做的真实联调（submit≤3、估算≤9.00 CNY）。"
                "账号权限、真实 CDN、编码与账单以本次实际取得的证据为准，未取得项保持未知；"
                "i2v/首尾帧与镜头延长未做真实样本。"
            ),
        }
    )
    report.update(status="incomplete", maxModelCalls=args.max_model_calls)
    report.pop("finishedAt", None)
    if previous:
        report["continuedFrom"] = str(args.continue_from.resolve())
    ledger = None if live else FixtureLedger(home)

    def persist():
        report["totals"] = totals(report)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_json(args.output, report)

    persist()
    print(
        json.dumps(
            {
                "suite": report["suiteId"],
                "mode": report["mode"],
                "dataDirectory": str(home),
                "maxModelCalls": args.max_model_calls,
            },
            ensure_ascii=False,
        ),
        flush=True,
    )
    try:
        start_index = len(previous["cases"]) - 1 if previous else 0
        for index in range(start_index, len(cases)):
            case = cases[index]
            prior = previous["cases"][index] if previous and index == start_index else None
            remaining = args.max_model_calls - totals(report)["modelCalls"]
            if remaining <= 0 or (
                prior and prior["run"]["policy"]["maxSteps"] - prior["run"]["modelSteps"] > remaining
            ):
                report["status"] = "budget-exhausted"
                break
            clock = SuiteClock()
            wan_fixture = None if live else WanShotFixture(case, ledger)
            media_fixture = None if live else MediaFixture(case, ledger)
            item = (
                copy.deepcopy(prior)
                if prior
                else {
                    "case": case.name,
                    "expects": case.expects,
                    "run": None,
                    "waitingSamples": [],
                    "applicationEvents": [],
                    "connectionRetries": 0,
                }
            )
            if prior:
                report["cases"][index] = item
            else:
                report["cases"].append(item)
            started = time.monotonic()
            service_ref = {"service": None}
            explicit_resume_pending = bool(prior)

            def on_event(event, case=case, item=item, service_ref=service_ref):
                nonlocal explicit_resume_pending
                if event.get("sessionId") != case.name or event["type"] == "assistant.delta":
                    return
                entry = {key: event[key] for key in ("type", "runId", "sessionId") if key in event}
                if event["type"] == "run.resumed":
                    entry["resumeKind"] = "explicit" if explicit_resume_pending else "automatic"
                    explicit_resume_pending = False
                if event["type"] == "job.updated":
                    entry["jobId"] = event["job"]["jobId"]
                    entry["status"] = event["job"]["status"]
                    entry["revision"] = event["job"]["revision"]
                item["applicationEvents"].append(entry)
                if service_ref["service"] is not None and event["type"] in {
                    "model.started",
                    "model.failed",
                    "run.waiting",
                }:
                    capture_state(
                        service_ref["service"], item, case, offline=not live, ledger=ledger, full=False
                    )
                    persist()

            with patches(wan_fixture, media_fixture, clock, live=live):
                async with ApplicationService.open(
                    base,
                    model=fixture,
                    policy=RunPolicy(max_steps=min(8, remaining), max_tool_calls=12, timeout_seconds=180),
                    on_event=on_event,
                ) as service:
                    service_ref["service"] = service
                    signature = service.runner().context_signature()
                    if prior and signature != prior["harnessSignature"]:
                        raise AppError("RESUME_CONFIG_CHANGED", "原评测的工具、Skills或上下文配置已变化。")
                    service.store.ensure_session(case.name)
                    if not prior:
                        item["sourceId"] = None
                        item["assetIds"] = []
                        if case.source:
                            seeded = service.tools.execute(
                                "artifact_save",
                                {"kind": "brief", "title": "M2 来源版本夹具", "content": BRIEF_TEXT},
                                store=service.store,
                                project_id=case.name,
                                operation_key=f"eval-seed-{case.name}",
                            )
                            if not seeded["ok"]:
                                raise AppError("EVAL_SEED_FAILED", "来源文本夹具保存失败。")
                            item["sourceId"] = seeded["data"]["artifactId"]
                        if case.reference:
                            for index, rgb in enumerate([(32, 48, 72), (188, 140, 96)], start=1):
                                imported = service.import_asset(
                                    case.name,
                                    solid_png(1280, 720, rgb),
                                    filename=f"reference-{index}.png",
                                )
                                item["assetIds"].append(imported["assetId"])
                        prompt = case.prompt.format(
                            artifact_id=item["sourceId"],
                            asset_id=(item["assetIds"] or [""])[0],
                            last_asset_id=(item["assetIds"] or ["", ""])[-1],
                        )
                        item["prompt"] = prompt
                        item["before"] = domain(service.store.snapshot())
                        item["harnessSignature"] = signature
                        item["toolInventory"] = [tool["name"] for tool in service.tools.inventory()]
                    retries_before = service.http_client.connection_retries
                    try:
                        if prior:
                            report["resumes"].append(
                                {
                                    "case": case.name,
                                    "runId": prior["run"]["id"],
                                    "callsBefore": len(prior["run"]["modelCalls"]),
                                    "suiteCallsBefore": totals(report)["modelCalls"],
                                    "errorCode": prior["run"]["errorCode"],
                                    "at": now(),
                                }
                            )
                            await service.resume(prior["run"]["id"])
                        else:
                            await service.start(case.name, item["prompt"], case.name)
                        await drive(service, item, case, live=live, clock=clock)
                        capture_state(service, item, case, offline=not live, ledger=ledger, full=True)
                        item["runJobs"] = copy.deepcopy(item["jobs"])
                        item["runUpstream"] = copy.deepcopy(item["upstream"])
                        if case.redo_index:
                            await redo_shot(service, item, case, live=live, clock=clock)
                            await settle(service, case, live=live, clock=clock)
                    finally:
                        await capture_full(
                            service,
                            item,
                            case,
                            offline=not live,
                            ledger=ledger,
                            config=base,
                            facts=facts,
                        )
                        item["connectionRetries"] += service.http_client.connection_retries - retries_before
                        item["elapsedSeconds"] = round(
                            (prior["elapsedSeconds"] if prior else 0) + time.monotonic() - started, 3
                        )
            item["checks"] = grade(case, item, offline=not live)
            persist()
            print(
                json.dumps(
                    {
                        "case": case.name,
                        "passed": all(item["checks"].values()),
                        "failedChecks": [key for key, value in item["checks"].items() if not value],
                        "runId": item["run"]["id"],
                        "modelSteps": item["run"]["modelSteps"],
                        "toolCalls": item["run"]["toolCalls"],
                        "errorCode": item["run"]["errorCode"],
                        "jobs": len(item["jobs"]),
                    },
                    ensure_ascii=False,
                ),
                flush=True,
            )
            if not all(item["checks"].values()):
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
        help="显式启用真实 DeepSeek 与万相调用；双镜头加一次重做，submit≤3、估算≤9.00 CNY",
    )
    result.add_argument("--home", type=Path, default=Path(".vagent/evaluations/m2") / str(uuid4()))
    result.add_argument("--output", type=Path)
    result.add_argument("--context-bytes", type=int, default=65536)
    result.add_argument(
        "--max-model-calls", type=int, default=None, help="套件模型调用上限；离线默认60，真实默认16"
    )
    result.add_argument(
        "--continue-from", type=Path, help="显式续跑原失败Run，沿用原数据和累计预算；需新的输出文件"
    )
    return result


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    load_dotenv(".env", override=False)
    args = parser().parse_args()
    args.output = args.output or Path(
        f"output/m2-{'live' if args.live else 'offline'}-{uuid4().hex[:8]}.json"
    )
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
