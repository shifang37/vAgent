"""M1-C application acceptance: offline by default; --live spends real DeepSeek and Wan calls.

Offline mode drives the real application, live tools, background workers, persistent
waits and the local media pipeline against controlled HTTP fixtures: no paid
services and no public network. --live explicitly uses the configured real
DeepSeek model and the frozen Beijing Wan account with a single video case: at
most one Job and one submission. Both modes stop at the first failure and share
a persisted suite ceiling; --continue-from resumes only the original failed Run
without resetting budgets or overwriting earlier evidence.
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
from datetime import UTC, datetime
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
    WAN_CAPABILITIES_VERSION,
    WAN_MODEL,
    WAN_PROMPT_PREFIX,
    JobV2,
    wan_capabilities,
)
from vagent.video.jobs import JobService
from vagent.video.media import DownloadRetry
from vagent.video.media_http import MediaHttpClient
from vagent.video.media_worker import MediaWorker
from vagent.video.providers.wan import WanAdapter
from vagent.video.worker import JobWorker
from vagent.waiting import WAIT_EXECUTION_VERSION, ToolExecutionContext
from vagent.web import create_app


@dataclass(frozen=True)
class Case:
    name: str
    prompt: str
    expects: str = "success"  # success | unsupported | submit_failed | submit_unknown | generate_failed | download_recovered
    submission: str = "accepted"  # accepted | rejected | timeout
    initial: str = "PENDING"  # PENDING | RUNNING | SUCCEEDED
    states: tuple = ("RUNNING", "SUCCEEDED")  # provider status per query
    download_failures: int = 0
    source: bool = False
    replay: bool = False
    recover_download: bool = False


CASES = (
    Case(
        "register-and-reply",
        "这是虚构咖啡店的登记验收。先查视频能力，登记一个5秒、720p、16:9的真实视频任务，内容为雨夜咖啡店；"
        "登记后立即用完全相同参数再登记一次，再用不同提示词再登记一次，都按工具实际返回处理。"
        "不要等待、不轮询。最后回复真实jobId和登记状态，并说明云端生成和本地媒体尚未完成时不能声称有可播放视频。",
        initial="SUCCEEDED",
        states=(),
        replay=True,
    ),
    Case(
        "await-success",
        "读取当前项目brief {artifact_id} 的第1版，以它为来源（sourceRefs保留该ID和version=1）。"
        "先查视频能力，再登记一个5秒、720p、16:9的雨夜咖啡店真实视频任务，然后调用await_job等待终态。"
        "按实际结果回复，引用真实jobId、来源ID和版本，说明是否有可播放媒体、本地是否已交付。"
        "不要修改文本产物，不轮询、不重复登记。",
        source=True,
    ),
    Case(
        "generation-failure",
        "这是生成失败场景的验收。先查视频能力，登记一个5秒、720p、16:9的雨夜咖啡店真实视频任务，"
        "调用await_job等待终态。最后按工具返回的实际结果如实说明，引用真实jobId和错误信息；"
        "如失败请报告失败，不自动重做或登记替代任务，不声称有媒体。",
        states=("RUNNING", "FAILED"),
        expects="generate_failed",
    ),
    Case(
        "submit-rejected",
        "这是提交被拒场景的验收。先查视频能力，登记一个5秒、720p、16:9的雨夜咖啡店真实视频任务，"
        "调用await_job等待终态。按实际结果如实说明jobId和失败原因；不要重试提交、不登记替代任务，不声称有媒体。",
        submission="rejected",
        expects="submit_failed",
    ),
    Case(
        "submit-unknown",
        "这是提交不确定场景的验收。先查视频能力，登记一个5秒、720p、16:9的雨夜咖啡店真实视频任务，"
        "调用await_job等待终态。如实说明提交结果不确定、没有确认的上游任务ID、不会自动重提；"
        "不登记替代任务，不声称有媒体。",
        submission="timeout",
        expects="submit_unknown",
    ),
    Case(
        "download-recovery",
        "这是下载恢复场景的验收。先查视频能力，登记一个5秒、720p、16:9的雨夜咖啡店真实视频任务，"
        "调用await_job等待终态。按实际结果如实说明jobId与下载状态；如下载失败，说明保留原任务、可显式重试下载，"
        "不要声称已有可播放媒体，不重新生成。",
        states=("SUCCEEDED",),
        download_failures=3,
        recover_download=True,
        expects="download_recovered",
    ),
    Case(
        "unsupported-spec",
        "我需要严格60秒、4K、1:1的真实视频。先核对当前视频能力；若不能满足，请明确指出不支持的规格，"
        "给出实际可用选择并向我确认。未经确认不得降低规格、拆分或登记任务，也不要保存文本方案。",
        expects="unsupported",
    ),
)
LIVE_CASES = (CASES[1],)
SOURCE_TEXT = "栖岸咖啡：雨夜青蓝色灯光下，一杯热咖啡置于窗边；镜头缓缓推进，玻璃映出街灯。"
VIDEO_PROMPT = "雨夜青蓝色灯光下的栖岸咖啡店，镜头缓缓推近窗边热咖啡。"
MEDIA_URL = (
    "https://dashscope-result-sh.oss-accelerate.aliyuncs.com/offline-fixture/wan-fixture.mp4"
    "?Signature=offline-fixture-signature"
)
FIXTURE = Path(__file__).resolve().parent.parent / "tests" / "fixtures" / "m1c" / "sample-720p.mp4"
VIDEO_TOOLS = {"video_capabilities", "video_generate", "job_get", "await_job"}
ACTIVE_STATUSES = {"pending_submit", "submitting", "queued", "running", "downloading"}
EXPECTED_PARAMETERS = {
    "resolution": "720P",
    "ratio": "16:9",
    "duration": 5,
    "prompt_extend": False,
    "watermark": True,
    "seed": 0,
}


def suite_identity(cases):
    return hashlib.sha256(
        json.dumps(
            {"version": 2, "cases": [vars(case) for case in cases], "source": SOURCE_TEXT},
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


class SuiteClock:
    def __init__(self):
        self.value = datetime.now(UTC)

    def __call__(self):
        return self.value


def advance_clock(clock, service):
    deadlines = []
    for job in service.video_jobs.list():
        if job.next_poll_at and not job.query_started_at:
            deadlines.append(job.next_poll_at)
        if isinstance(job, JobV2) and job.download and job.download.next_attempt_at:
            deadlines.append(job.download.next_attempt_at)
    if deadlines:
        clock.value = max(clock.value, min(datetime.fromisoformat(value) for value in deadlines))


class FixtureLedger:
    """Durable controlled-upstream ledger, independent from the application store."""

    def __init__(self, home):
        self.path = home / "m1c-fixture.json"
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            self.data = raw if isinstance(raw, dict) else {}
        except (OSError, ValueError):
            self.data = {}

    def case(self, name):
        return self.data.setdefault(
            name,
            {
                "taskId": None,
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


class WanFixture:
    """Controlled Wan HTTP responses; no credentials and no public network."""

    def __init__(self, case, ledger):
        self.case, self.ledger = case, ledger
        self.task_id = f"wan-fixture-{case.name}"

    def handle(self, request):
        record = self.ledger.case(self.case.name)
        record["taskId"] = self.task_id
        if request.method == "POST":
            record["submissions"].append(json.loads(request.content))
            record["hasAuthorization"] = "authorization" in request.headers
            record["asyncHeader"] = request.headers.get("x-dashscope-async")
            self.ledger.save()
            if self.case.submission == "timeout":
                raise httpx.ReadTimeout("fixture submit timeout", request=request)
            if self.case.submission == "rejected":
                return httpx.Response(
                    400,
                    json={
                        "code": "InvalidParameter",
                        "message": "fixture rejection",
                        "request_id": "fixture-trace",
                    },
                )
            output = {"task_id": self.task_id, "task_status": self.case.initial}
            if self.case.initial == "SUCCEEDED":
                output["video_url"] = MEDIA_URL
            return httpx.Response(200, json={"request_id": "fixture-submit-trace", "output": output})
        task = request.url.path.rsplit("/", 1)[-1]
        record["queryTaskIds"].append(task)
        self.ledger.save()
        index = min(len(record["queryTaskIds"]) - 1, len(self.case.states) - 1)
        status = self.case.states[index] if self.case.states else "SUCCEEDED"
        output = {"task_id": task, "task_status": status}
        if status == "SUCCEEDED":
            output["video_url"] = MEDIA_URL
        if status == "FAILED":
            output["code"] = "DataInspectionFailed"
        payload = {"request_id": f"fixture-query-{len(record['queryTaskIds'])}", "output": output}
        if status == "SUCCEEDED":
            # Pin the live response shape: the real service reports SR numerically.
            payload["usage"] = {
                "duration": 5,
                "output_video_duration": 5,
                "input_video_duration": 0,
                "video_count": 1,
                "SR": 720,
                "ratio": "16:9",
            }
        return httpx.Response(200, json=payload)


class MediaFixture:
    """Serves the committed local MP4 fixture; optionally fails the first attempts."""

    def __init__(self, case, ledger):
        self.case, self.ledger = case, ledger
        self.data = fixture_bytes()

    def handle(self, request):
        record = self.ledger.case(self.case.name)
        failed_before = sum(1 for entry in record["downloads"] if entry.get("failed"))
        will_fail = failed_before < self.case.download_failures
        record["downloads"].append({"host": request.url.host, "failed": will_fail})
        self.ledger.save()
        if will_fail:
            return httpx.Response(503, content=b"fixture temporarily unavailable")
        return httpx.Response(
            200,
            content=self.data,
            headers={"Content-Type": "video/mp4", "Content-Length": str(len(self.data))},
        )


class FixtureModel:
    """Derive scripted decisions from the transcript so explicit resume is repeatable."""

    name = "offline-m1c-fixture"

    async def generate_stream(self, messages, tools, delta):
        reply = await self.generate(messages, tools)
        for offset in range(0, len(reply.content), 16):
            delta(reply.content[offset : offset + 16])
        return reply

    async def generate(self, messages, tools):
        start = max(i for i, message in enumerate(messages) if isinstance(message, HumanMessage))
        prompt = messages[start].content
        case = next(case for case in CASES if prompt.startswith(case.prompt.split("{artifact_id}")[0]))
        results = [message for message in messages[start:] if isinstance(message, ToolMessage)]
        sequence = [message.name for message in results]
        latest = {}
        for message in results:
            latest[message.name] = json.loads(message.content)
        generates = [json.loads(message.content) for message in results if message.name == "video_generate"]

        def calls(*items):
            return AIMessage(
                content="",
                tool_calls=[
                    {"id": f"fixture-{len(results)}-{index}", "name": name, "args": args}
                    for index, (name, args) in enumerate(items)
                ],
            )

        if "video_capabilities" not in sequence:
            reads = [("video_capabilities", {})]
            if case.source:
                reads.append(("artifact_read", {"artifactId": source_id_from(prompt), "version": 1}))
            return calls(*reads)
        if case.expects == "unsupported":
            return AIMessage(
                content="当前真实视频能力只有5秒、720p、16:9，不支持60秒、4K、1:1。"
                "请确认是否采用该组合？尚未登记任务，没有媒体。"
            )
        request = {
            "provider": "wan",
            "model": WAN_MODEL,
            "capabilitiesVersion": WAN_CAPABILITIES_VERSION,
            "prompt": VIDEO_PROMPT,
            "spec": {"durationSeconds": 5, "resolution": "720p", "aspectRatio": "16:9"},
        }
        if case.source:
            request["sourceRefs"] = [{"artifactId": source_id_from(prompt), "version": 1}]
        if not generates:
            return calls(("video_generate", request))
        if not generates[0]["ok"]:
            raise AppError("FIXTURE_TOOL_FAILED", "离线夹具登记失败。")
        job_id = generates[0]["data"]["jobId"]
        if case.replay:
            if len(generates) == 1:
                different = {**request, "prompt": request["prompt"] + "（改为晴朗白昼。）"}
                return calls(("video_generate", request), ("video_generate", different))
            return AIMessage(
                content=f"真实任务已登记：{job_id}；云端生成与本地媒体尚未完成，不能声称已有可播放视频。"
            )
        if "await_job" not in sequence:
            return calls(("await_job", {"jobId": job_id}))
        result = latest["await_job"]
        source = f"来源 {source_id_from(prompt)} v1。" if case.source else ""
        if result.get("ok"):
            delivered = result["data"].get("mediaAvailable") and result["data"].get("mediaRefs")
            text = (
                f"真实任务 {job_id} 已本地交付，媒体可播放可下载。"
                if delivered
                else f"真实任务 {job_id} 尚未交付本地媒体。"
            )
            return AIMessage(content=f"{text}{source}")
        code = result["error"]["code"]
        message = result["error"]["message"]
        if code == "JOB_SUBMISSION_UNKNOWN":
            return AIMessage(
                content=f"任务 {job_id} 提交结果不确定（{code}）；没有确认的上游ID，不会自动重提。没有可播放媒体。"
            )
        if code == "JOB_DOWNLOAD_FAILED":
            return AIMessage(
                content=f"任务 {job_id} 已生成但本地下载失败（{code}）；保留原任务，可显式重试下载，没有可播放媒体。"
            )
        return AIMessage(content=f"任务 {job_id} 失败：{message}（{code}）。没有可播放媒体。")


def source_id_from(prompt):
    return re.search(r"brief ([a-z0-9-]+)", prompt).group(1)


def domain(snapshot):
    return {key: snapshot[key] for key in ("project", "artifacts")}


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
    """Raw Job record plus the public projection fields, without private provider data."""
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


def evidence_hashes(home):
    names = ["state.json", "checkpoints.sqlite", "m1c-fixture.json"]
    state_path = home / "state.json"
    if state_path.is_file():
        try:
            state = json.loads(state_path.read_text(encoding="utf-8"))
            names.extend(sorted(asset["relativePath"] for asset in state.get("media", {}).values()))
        except (ValueError, KeyError, TypeError):
            pass
    return {
        name: hashlib.sha256((home / name).read_bytes()).hexdigest() if (home / name).is_file() else None
        for name in names
    }


def media_checks(service, item, facts):
    if not item["assets"]:
        return {}
    asset = item["assets"][0]
    path = service.store.home / asset["relativePath"]
    data = path.read_bytes() if path.is_file() else b""
    metadata = asset["metadata"]
    checks = {
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
        checks["matchesCommittedFixture"] = (
            asset["sha256"] == facts["sha256"] and asset["sizeBytes"] == facts["sizeBytes"]
        )
    return checks


async def probe_media_api(service, config, item):
    delivered = [job for job in item["jobs"] if job["status"] == "succeeded" and job.get("mediaAvailable")]
    if not delivered or not item["assets"]:
        return None
    asset = item["assets"][0]
    media_id, size = asset["id"], asset["sizeBytes"]
    app = create_app(config)
    app.state.service = service
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:3210"
    ) as client:
        detail = await client.get(f"/api/media/{media_id}")
        url = f"/api/media/{media_id}/content"
        full = await client.get(url)
        head = await client.head(url)
        ranged = await client.get(url, headers={"Range": "bytes=0-99"})
        bad = await client.get(url, headers={"Range": f"bytes={size + 10}-"})
        attachment = await client.get(url + "?download=1")
    return {
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
        "unsatisfiable416": bad.status_code == 416 and bad.headers.get("content-range") == f"bytes */{size}",
        "downloadAttachment": attachment.headers.get("content-disposition")
        == f'attachment; filename="{media_id}.mp4"',
        "etagMatchesSha": full.headers.get("etag", "").strip('"') == asset["sha256"],
    }


def upstream_view(item, case, *, offline, ledger):
    if offline:
        record = ledger.case(case.name)
        return {
            "source": "fixture",
            "taskId": record["taskId"],
            "submitCalls": len(record["submissions"]),
            "queryCalls": len(record["queryTaskIds"]),
            "submitBodies": record["submissions"],
            "hasAuthorization": record["hasAuthorization"],
            "asyncHeader": record["asyncHeader"],
            "queryTaskIds": record["queryTaskIds"],
            "downloadCalls": len(record["downloads"]),
            "downloadHosts": sorted({entry["host"] for entry in record["downloads"]}),
        }
    job = item["jobs"][0] if item["jobs"] else None
    return {
        "source": "durable",
        "submitCalls": job["submitAttempts"] if job else 0,
        "queryCalls": job["queryAttempts"] if job else 0,
        "downloadCalls": (job["download"] or {}).get("attempts", 0) if job else 0,
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
    item["waits"] = [binding for binding in state["waits"].values() if binding["context"]["runId"] == run_id]
    item["operations"] = {key: op for key, op in state["operations"].items() if key.startswith(f"{run_id}:")}
    item["upstream"] = upstream_view(item, case, offline=offline, ledger=ledger)
    if full:
        item["after"] = domain(service.session(case.name))


async def capture_full(service, item, case, *, offline, ledger, config, facts):
    capture_state(service, item, case, offline=offline, ledger=ledger, full=True)
    item["mediaChecks"] = media_checks(service, item, facts)
    item["mediaApi"] = await probe_media_api(service, config, item) if item["mediaChecks"] else None


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


def media_flags_ok(flags, *, offline):
    keys = ["assetSha256MatchesFile", "h264", "spec720p5s"]
    if offline:
        keys.append("matchesCommittedFixture")
    return bool(flags) and all(flags.get(key) is True for key in keys)


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
        "video_tools_visible": VIDEO_TOOLS.issubset(set(item["toolInventory"])),
        "saved_mode_live": run["videoMode"] == "live" and run["executionVersion"] == WAIT_EXECUTION_VERSION,
        "private_url_not_exposed": "Signature" not in json.dumps(item["toolResults"], ensure_ascii=False)
        and "Signature" not in answer
        and "Signature" not in json.dumps(jobs, ensure_ascii=False),
    }
    if case.expects == "unsupported":
        checks.update(
            no_jobs_or_submissions=not jobs and upstream["submitCalls"] == 0,
            no_generation_attempt="video_generate" not in names,
            unsupported_explained=bool(re.search(r"不支持|不能|无法|不满足|不提供|不符合", answer))
            and "60" in answer
            and "4k" in answer.lower(),
            asks_for_confirmation=bool(re.search(r"确认|选择|是否|哪[种个]|[？?]", answer)),
            no_fabricated_media_claim=no_media_claim(answer),
        )
        return checks
    checks["unique_job"] = len(jobs) == 1
    if len(jobs) != 1:
        return checks
    job = jobs[0]
    checks.update(
        capability_before_generation="video_capabilities" in names
        and "video_generate" in names
        and names.index("video_capabilities") < names.index("video_generate"),
        submitted_once=job["submitAttempts"] == upstream["submitCalls"] == 1,
        frozen_parameters=job["request"]["parameters"] == EXPECTED_PARAMETERS
        and job["request"]["providerPrompt"] == WAN_PROMPT_PREFIX + job["request"]["prompt"]
        and job["request"]["region"] == "cn-beijing"
        and job["request"].get("workspaceScopeRecorded") is True,
        cost_recorded=job["cost"]["estimate"]["amount"] == "3.00"
        and job["cost"]["estimate"]["currency"] == "CNY"
        and job["cost"]["actual"]["status"] == "unknown",
    )
    if offline:
        expected_queries = len(case.states)
        checks.update(
            frozen_submit_body=upstream["submitCalls"] == 1
            and upstream["submitBodies"]
            == [
                {
                    "model": WAN_MODEL,
                    "input": {"prompt": WAN_PROMPT_PREFIX + VIDEO_PROMPT},
                    "parameters": EXPECTED_PARAMETERS,
                }
            ],
            submit_headers=upstream["hasAuthorization"] is True and upstream["asyncHeader"] == "enable",
        )
        if job["providerTaskId"] is None:
            checks["original_task_identity"] = not upstream["queryTaskIds"]
        else:
            checks["original_task_identity"] = (
                job["providerTaskId"] == upstream["taskId"]
                and len(upstream["queryTaskIds"]) == expected_queries
                and all(task == upstream["taskId"] for task in upstream["queryTaskIds"])
            )
    else:
        checks["original_task_identity"] = bool(job["providerTaskId"])
    if case.expects == "success":
        download = job["download"] or {}
        checks.update(
            succeeded=job["status"] == "succeeded" and job["simulated"] is False,
            media_delivered=job["mediaAvailable"] is True
            and job["mediaAvailability"]["status"] == "available"
            and len(job["mediaRefs"]) == 1
            and job["result"]["mediaAvailable"] is True,
            media_matches_local_file=media_flags_ok(item["mediaChecks"], offline=offline),
            download_accounted=download.get("attempts") == upstream["downloadCalls"]
            and download.get("phase") == "committed",
            media_api_ok=bool(item["mediaApi"]) and all(item["mediaApi"].values()),
            answer_references_job=job["id"] in answer,
            no_fabricated_media_claim=not re.search(r"https?://", answer),
        )
        if case.source:
            refs = [{"artifactId": item["sourceId"], "version": 1}]
            checks.update(
                source_version_preserved=job["request"]["sourceRefs"] == refs
                and bool(item["assets"])
                and item["assets"][0]["sourceRefs"] == refs,
                source_was_read="artifact_read" in successful
                and any(
                    call["name"] == "artifact_read"
                    and call["arguments"].get("artifactId") == item["sourceId"]
                    and call["arguments"].get("version") == 1
                    for call in trace
                ),
                source_version_cited=item["sourceId"] in answer
                and bool(re.search(r"v\s*1|第\s*1\s*版|版本\s*[:：]?\s*1", answer, re.I)),
            )
        if case.replay:
            generates_list = [result for result in results if result["name"] == "video_generate"]
            checks.update(
                replied_without_wait="await_job" not in names and not item["waits"],
                registration_explained=bool(re.search(r"登记|提交|排队|下载|媒体", answer, re.I)),
                replay_deduplicated=len(generates_list) == 3
                and generates_list[0]["result"].get("ok")
                and generates_list[0]["result"]["data"]["status"] == "pending_submit"
                and generates_list[0]["result"]["data"]["simulated"] is False
                and generates_list[0]["result"]["data"]["mediaAvailable"] is False
                and generates_list[1]["result"].get("ok")
                and generates_list[0]["result"]["data"]["jobId"] == job["id"]
                and generates_list[1]["result"]["data"]["jobId"] == job["id"]
                and not generates_list[2]["result"].get("ok")
                and generates_list[2]["result"]["error"]["code"] == "JOB_ALREADY_EXISTS",
                worker_completed_independently=item["modelCallsAtReply"] == len(run["modelCalls"]),
            )
        else:
            checks["answer_reports_delivery"] = bool(re.search(r"媒体|播放|下载|交付", answer))
    if case.expects in {"submit_failed", "submit_unknown", "generate_failed", "download_recovered"}:
        awaited = [result for result in results if result["name"] == "await_job"]
        expected_wait_error = {
            "submit_failed": "JOB_FAILED",
            "submit_unknown": "JOB_SUBMISSION_UNKNOWN",
            "generate_failed": "JOB_FAILED",
            "download_recovered": "JOB_DOWNLOAD_FAILED",
        }[case.expects]
        checks.update(
            single_generation_attempt="video_generate" in names and names.count("video_generate") == 1,
            actual_failure_delivered=bool(awaited)
            and all(
                result["result"].get("error", {}).get("code") == expected_wait_error for result in awaited
            ),
        )
        if case.expects in {"submit_failed", "submit_unknown", "generate_failed"}:
            checks["no_media"] = (
                job["status"] != "succeeded"
                and job["mediaAvailable"] is False
                and not job["mediaRefs"]
                and not item["assets"]
            )
        if case.expects == "submit_failed":
            checks.update(
                submit_failed=job["status"] == "failed"
                and job["error"]["stage"] == "submit"
                and job["error"]["code"] == "PROVIDER_INVALID_REQUEST"
                and job["providerTaskId"] is None,
                failure_explained="失败" in answer and "PROVIDER_INVALID_REQUEST" in answer,
                no_fabricated_media_claim=no_media_claim(answer),
            )
        elif case.expects == "submit_unknown":
            checks.update(
                submission_unknown=job["status"] == "unknown"
                and job["error"]["stage"] == "submit"
                and job["error"]["code"] == "SUBMISSION_UNKNOWN"
                and job["providerTaskId"] is None,
                uncertainty_explained=bool(re.search(r"不确定|无法确认|未知", answer)),
                no_fabricated_media_claim=no_media_claim(answer),
            )
        elif case.expects == "generate_failed":
            checks.update(
                generation_failed=job["status"] == "failed"
                and job["error"]["stage"] == "generate"
                and job["error"]["code"] == "PROVIDER_GENERATION_FAILED"
                and bool(job["providerTaskId"]),
                failure_explained="失败" in answer and "PROVIDER_GENERATION_FAILED" in answer,
                no_fabricated_media_claim=no_media_claim(answer),
            )
        elif case.expects == "download_recovered":
            failure = item.get("downloadFailure") or {}
            retry = item.get("retryDownload") or {}
            checks.update(
                initial_download_failure=failure.get("status") == "download_failed"
                and (failure.get("error") or {}).get("stage") == "download"
                and (failure.get("download") or {}).get("phase") == "failed",
                failure_reported=bool(re.search(r"失败|重试", answer)) and no_media_claim(answer),
                manual_retry_new_window=(retry.get("download") or {}).get("generation") == 2
                and (retry.get("download") or {}).get("repair") is False,
                recovered_after_retry=job["status"] == "succeeded"
                and job["mediaAvailable"] is True
                and job["download"]["attempts"] == upstream["downloadCalls"] == 4
                and job["download"]["windowAttempts"] == 1
                and job["submitAttempts"] == 1,
                media_matches_local_file=media_flags_ok(item["mediaChecks"], offline=offline),
                media_api_ok=bool(item["mediaApi"]) and all(item["mediaApi"].values()),
            )
    awaited = [result for result in results if result["name"] == "await_job"]
    if (
        case.expects in {"success", "generate_failed", "submit_unknown", "download_recovered"}
        and not case.replay
    ):
        checks.update(
            await_original_job=bool(awaited)
            and all(
                call["arguments"] == {"jobId": job["id"]} for call in trace if call["name"] == "await_job"
            ),
            durable_deliveries_match=all(
                binding["status"] == "delivered"
                and len([result for result in awaited if result["id"] == binding["context"]["toolCallId"]])
                == 1
                and next(
                    result["result"] for result in awaited if result["id"] == binding["context"]["toolCallId"]
                )
                == item["operations"]
                .get(ToolExecutionContext.model_validate(binding["context"]).operation_key, {})
                .get("result")
                == binding["result"]
                for binding in item["waits"]
            ),
            waiting_does_not_call_model=all(sample["unchanged"] for sample in item["waitingSamples"]),
        )
        if offline and case.name in {"await-success", "download-recovery"}:
            checks["durable_wait_observed"] = bool(item["waits"] and item["waitingSamples"])
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
        "mediaDelivered": sum(
            1
            for item in items
            if item.get("jobs")
            and item["jobs"][0]["status"] == "succeeded"
            and item["jobs"][0].get("mediaAvailable")
        ),
        "explicitResumes": len(report.get("resumes", [])),
        "automaticResumes": sum(
            event["type"] == "run.resumed" and event.get("resumeKind", "automatic") == "automatic"
            for item in items
            for event in item["applicationEvents"]
        ),
        "connectionRetries": sum(item.get("connectionRetries", 0) for item in items),
        "activeSeconds": round(sum(run["activeSeconds"] for run in runs), 3),
        "externalWaitSeconds": round(sum(run.get("externalWaitSeconds") or 0 for run in runs), 3),
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
        async with asyncio.timeout(900 if live else 30):
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
            item["jobsAtReply"] = service.jobs(case.name)
            await settle(service, case, live=live, clock=clock)
            if case.recover_download:
                jobs = service.video_jobs.list(session_id=case.name)
                job = jobs[0] if jobs else None
                if job is not None and job.status == "download_failed":
                    item["downloadFailure"] = {
                        "status": job.status,
                        "error": job.error.model_dump(mode="json", by_alias=True) if job.error else None,
                        "download": job.download.model_dump(mode="json", by_alias=True),
                    }
                    request = DownloadRetry(
                        client_request_id=f"eval-{case.name}", expected_revision=job.revision
                    )
                    item["retryDownload"] = service.retry_download(job.id, request)
                    await settle(service, case, live=live, clock=clock)
    finally:
        if not waiter.done():
            waiter.cancel()
        await asyncio.gather(waiter, return_exceptions=True)


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
            report["schemaVersion"] == 1
            and report["suiteId"] == suite
            and report["mode"] == ("live" if live else "offline-fixture")
            and report["status"] == "failed"
            and last["run"]["resumable"]
            and last["run"]["status"] in {"failed", "cancelled", "interrupted"}
            and [item["case"] for item in report["cases"]]
            == [case.name for case in cases[: len(report["cases"])]]
            and all(all(item["checks"].values()) for item in report["cases"][:-1])
            and 1 <= report["maxModelCalls"] <= 40
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
        args.max_model_calls = 16 if live else 40
    if not 1 <= args.max_model_calls <= 40 or not 1024 <= args.context_bytes <= 131072:
        raise AppError("EVAL_BUDGET_INVALID", "模型调用上限须为1～40，上下文预算须为1024～131072字节。")
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
    capability = wan_capabilities()
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
        base = replace(
            base,
            api_key=None,
            video_api_key="offline-video-fixture-key",
            video_workspace_id="offline-fixture",
            video_max_job_cost="3.00",
        )
    video_settings = {
        "mode": "live",
        "provider": capability.provider,
        "model": capability.model,
        "region": capability.region,
        "capabilitiesVersion": capability.capabilities_version,
        "spec": {"durationSeconds": 5, "resolution": "720p", "aspectRatio": "16:9"},
        "estimate": capability.price.model_dump(mode="json", by_alias=True),
        "maxJobCost": base.video_max_job_cost,
    }
    fixture = None if live else FixtureModel()
    model_name = base.model if live else fixture.name
    if previous and previous["model"] != model_name:
        raise AppError("RESUME_CONFIG_CHANGED", "原评测模型已变化，未发起续跑。")
    report = (
        copy.deepcopy(previous)
        if previous
        else {
            "schemaVersion": 1,
            "suiteId": LIVE_SUITE_ID if live else SUITE_ID,
            "mode": "live" if live else "offline-fixture",
            "model": model_name,
            "startedAt": now(),
            "dataDirectory": str(home),
            "contextBudgetBytes": args.context_bytes,
            "videoSettings": video_settings,
            "cases": [],
            "resumes": [],
            "limitations": (
                "固定合成用例与字面规则评分；离线夹具不衡量模型质量，也不代表真实账号、CDN 或账单。媒体为本地测试夹具。"
                if not live
                else "单视频 Job、单次提交的真实联调；账号权限、真实 CDN、编码与账单以本次实际取得的证据为准，未取得项保持未知。"
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
            wan_fixture = None if live else WanFixture(case, ledger)
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
            service = None
            explicit_resume_pending = bool(prior)

            def on_event(event, case=case, item=item):
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
                if service is not None and event["type"] in {"model.started", "model.failed", "run.waiting"}:
                    capture_state(service, item, case, offline=not live, ledger=ledger, full=False)
                    persist()

            with patches(wan_fixture, media_fixture, clock, live=live):
                async with ApplicationService.open(
                    base,
                    model=fixture,
                    policy=RunPolicy(max_steps=min(8, remaining), max_tool_calls=12, timeout_seconds=180),
                    on_event=on_event,
                ) as service:
                    signature = service.runner().context_signature()
                    if prior and signature != prior["harnessSignature"]:
                        raise AppError("RESUME_CONFIG_CHANGED", "原评测的工具、Skills或上下文配置已变化。")
                    service.store.ensure_session(case.name)
                    if not prior:
                        item["sourceId"] = None
                        if case.source:
                            seeded = service.tools.execute(
                                "artifact_save",
                                {"kind": "brief", "title": "M1-C 来源版本夹具", "content": SOURCE_TEXT},
                                store=service.store,
                                project_id=case.name,
                                operation_key=f"eval-seed-{case.name}",
                            )
                            if not seeded["ok"]:
                                raise AppError("EVAL_SEED_FAILED", "来源文本夹具保存失败。")
                            item["sourceId"] = seeded["data"]["artifactId"]
                        item["prompt"] = case.prompt.format(artifact_id=item["sourceId"])
                        item["before"] = domain(service.session(case.name))
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
                        "jobStatus": item["jobs"][0]["status"] if item["jobs"] else None,
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
        "--live", action="store_true", help="显式启用真实 DeepSeek 与万相调用；单用例、单 Job、单次提交"
    )
    result.add_argument("--home", type=Path, default=Path(".vagent/evaluations/m1c") / str(uuid4()))
    result.add_argument("--output", type=Path)
    result.add_argument("--context-bytes", type=int, default=65536)
    result.add_argument(
        "--max-model-calls", type=int, default=None, help="套件模型调用上限；离线默认40，真实默认16"
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
        f"output/m1c-{'live' if args.live else 'offline'}-{uuid4().hex[:8]}.json"
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
