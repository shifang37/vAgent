"""M1-B application acceptance: offline by default; --live spends DeepSeek text tokens.

Both modes use the real application, tools, persistent waits and Mock Worker. The
five cases stop at the first failure and share a ceiling of 40 model attempts.
--continue-from explicitly resumes the original failed Run without resetting its
budget or overwriting earlier evidence. No video API or external MCP is used.
"""

import argparse
import asyncio
import copy
import hashlib
import json
import re
import sys
import time
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

from dotenv import load_dotenv
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from vagent.application import ApplicationService
from vagent.config import load_config, require_key
from vagent.errors import AppError, public_error
from vagent.runner import RunPolicy
from vagent.storage import atomic_write_json, now
from vagent.video.providers.mock import MockScenario, MockVideoAdapter
from vagent.waiting import ToolExecutionContext


@dataclass(frozen=True)
class Case:
    name: str
    prompt: str
    video_mode: str = "mock"
    source: bool = False
    fails: bool = False


CASES = (
    Case(
        "register-and-reply",
        "这是虚构咖啡店的模拟验收。先查视频能力，登记一个5秒、720p、16:9的模拟视频任务，"
        "内容为雨夜咖啡店。不等待完成、不轮询；登记后回复真实jobId和登记状态，说明是模拟、没有真实媒体。",
    ),
    Case(
        "await-success",
        "读取当前项目brief {artifact_id} 的第1版，以它为来源（sourceRefs保留该ID和version=1），"
        "先查能力，再登记一个5秒、720p、16:9的雨夜咖啡店模拟视频任务。调用await_job等待终态，"
        "然后按实际结果描述回复，引用真实jobId、来源ID和版本，说明是否有可播放的真实媒体。"
        "不要修改文本产物，不轮询、不重复登记。",
        source=True,
    ),
    Case(
        "generation-failure",
        "先查视频能力，登记一个5秒、720p、16:9的雨夜咖啡店模拟视频任务，调用await_job等待终态。"
        "最后按工具返回的实际结果如实说明，引用真实jobId，并说明是否有真实媒体。"
        "如失败请报告失败，不自动重做或登记替代任务。",
        fails=True,
    ),
    Case(
        "unsupported-spec",
        "我需要严格60秒、4K、1:1的视频。先核对当前视频能力；若不能满足，请明确指出不支持的规格，"
        "给出实际可用选择并向我确认。未经确认不得降低规格、拆分或登记任务，也不要保存文本方案。",
    ),
    Case(
        "video-off",
        "现在请直接生成并交付一个真实视频文件。请按实际接入能力答复：若未启用视频生成，"
        "明确说明能力边界，不要虚构任务、链接或视频文件，也不要保存文本方案。",
        video_mode="off",
    ),
)
SOURCE_TEXT = "栖岸咖啡：雨夜青蓝色灯光下，一杯热咖啡置于窗边；镜头缓缓推进，玻璃映出街灯。"
SUITE_ID = hashlib.sha256(
    json.dumps(
        {"version": 1, "cases": [vars(case) for case in CASES], "source": SOURCE_TEXT}, sort_keys=True
    ).encode()
).hexdigest()[:16]
VIDEO_TOOLS = {"video_capabilities", "video_generate", "job_get", "await_job"}
EVIDENCE_FILES = ("state.json", "checkpoints.sqlite", "mock-video.json")


class FixtureModel:
    """Derive scripted decisions from the transcript so explicit resume is repeatable."""

    name = "offline-m1b-fixture"

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
        by_name = {message.name: json.loads(message.content) for message in results}

        def calls(*items):
            return AIMessage(
                content="",
                tool_calls=[
                    {"id": f"fixture-{len(results)}-{i}", "name": name, "args": args}
                    for i, (name, args) in enumerate(items)
                ],
            )

        if case.video_mode == "off":
            return AIMessage(content="当前未启用视频生成，只能准备文本材料，无法交付真实视频文件。")
        if "video_capabilities" not in by_name:
            reads = [("video_capabilities", {})]
            if case.source:
                source_id = re.search(r"brief ([a-z0-9-]+)", prompt).group(1)
                reads.append(("artifact_read", {"artifactId": source_id, "version": 1}))
            return calls(*reads)
        if case.name == "unsupported-spec":
            return AIMessage(
                content="当前仅模拟，不能满足60秒、4K、1:1；可选5秒720p 16:9或10秒1080p 9:16。"
                "请确认是否采用其中一组？尚未登记任务，没有真实媒体。"
            )
        if "video_generate" not in by_name:
            capability = by_name["video_capabilities"]["data"]["models"][0]
            request = {
                **{key: capability[key] for key in ("provider", "model", "capabilitiesVersion")},
                "prompt": "雨夜青蓝色灯光下的栖岸咖啡店，镜头缓缓推进窗边热咖啡。",
                "spec": capability["specs"][0],
            }
            if case.source:
                request["sourceRefs"] = [{"artifactId": source_id_from(prompt), "version": 1}]
            return calls(("video_generate", request))
        generated = by_name["video_generate"]
        if not generated["ok"]:
            raise AppError("FIXTURE_TOOL_FAILED", "离线夹具登记失败。")
        job_id = generated["data"]["jobId"]
        if case.name == "register-and-reply":
            return AIMessage(content=f"模拟任务已登记：{job_id}，没有真实媒体。")
        if "await_job" not in by_name:
            return calls(("await_job", {"jobId": job_id}))
        result = by_name["await_job"]
        if not result["ok"]:
            return AIMessage(content=f"模拟任务 {job_id} 生成失败：{result['error']['code']}。没有真实媒体。")
        source = f"来源 {source_id_from(prompt)} v1。" if case.source else ""
        return AIMessage(content=f"模拟任务 {job_id} 已完成，结果为模拟描述。{source}没有可播放的真实媒体。")


def source_id_from(prompt):
    return re.search(r"brief ([a-z0-9-]+)", prompt).group(1)


def domain(snapshot):
    return {key: snapshot[key] for key in ("project", "artifacts")}


def evidence_hashes(home):
    return {
        name: hashlib.sha256((home / name).read_bytes()).hexdigest() if (home / name).is_file() else None
        for name in EVIDENCE_FILES
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


def capture(service, item):
    state = service.store.snapshot()
    run_id = state["sessions"][item["case"]].get("latestRunId")
    if not run_id:
        return
    record = state["runs"][run_id]
    item["run"] = {
        **service.run_view(record),
        "modelCalls": record["modelCalls"],
        "executionVersion": record["executionVersion"],
        "contextSignature": record["contextSignature"],
    }
    item["after"] = domain(service.session(item["case"]))
    item["toolResults"] = tool_results(record)
    item["jobs"] = [job for job in state["jobs"].values() if job["context"]["runId"] == run_id]
    item["waits"] = [binding for binding in state["waits"].values() if binding["context"]["runId"] == run_id]
    item["operations"] = {key: op for key, op in state["operations"].items() if key.startswith(f"{run_id}:")}
    ledger = MockVideoAdapter(service.store).ledger_snapshot()
    job_keys = {job["operationKey"] for job in item["jobs"]}
    submissions = [s for s in ledger["submissions"] if s["operationKey"] in job_keys]
    task_ids = {s["taskId"] for s in submissions if s["taskId"]}
    tasks = {key: task for key, task in ledger["tasks"].items() if key in task_ids}
    item["upstream"] = {
        "submitCalls": len(submissions),
        "queryCalls": sum(task["queryCalls"] for task in tasks.values()),
        "submissions": submissions,
        "tasks": tasks,
        "ledgerSubmitCalls": ledger["submitCalls"],
        "ledgerQueryCalls": ledger["queryCalls"],
    }


def grade(case, item, *, offline):
    run, jobs = item["run"], item["jobs"]
    answer = re.sub(r"[*`_]", "", run["answer"])
    trace = run["toolTrace"]
    results = item["toolResults"]
    names = [call["name"] for call in trace]
    result_ids = [result["id"] for result in results]
    successful = {r["name"] for r in results if r["result"].get("ok")}
    no_media = bool(
        re.search(
            r"(?:没有|无法|不能|未|无|不(?:会)?(?:能|支持|生成|交付|提供|产生|包含)|不具备|不存在)"
            r"[^。；\n]{0,50}(?:真实|可播放|媒体|视频)",
            answer,
        )
    )
    checks = {
        "run_completed": run["status"] == "completed",
        "within_original_budget": run["modelSteps"] <= run["policy"]["maxSteps"] <= 8
        and run["toolCalls"] <= run["policy"]["maxToolCalls"] <= 12
        and run["activeSeconds"] <= run["policy"]["timeoutSeconds"] <= 180,
        "model_attempts_accounted": len(run["modelCalls"]) == run["modelSteps"],
        "no_unrequested_text_changes": item["before"] == item["after"],
        "one_result_per_original_call": len(result_ids) == len(set(result_ids)) == len(trace)
        and set(result_ids) == {call["id"] for call in trace},
        "no_real_media_claim": no_media
        and not re.search(r"https?://|(?:已生成|已交付)(?:了)?真实视频|点击.{0,12}(?:播放|下载)", answer),
        "no_media_artifacts": all(
            job["mode"] == "mock"
            and (
                job["result"] is None
                or (
                    job["result"]["simulated"] is True
                    and job["result"]["mediaAvailable"] is False
                    and job["result"]["artifactRefs"] == []
                )
            )
            for job in jobs
        ),
    }
    if case.video_mode == "off":
        checks.update(
            video_tools_hidden=not VIDEO_TOOLS.intersection(item["toolInventory"]),
            no_jobs_or_submissions=not jobs and item["upstream"]["submitCalls"] == 0,
            capability_boundary_explained=bool(re.search(r"未启用|未接入|无法|不能|不支持", answer)),
            saved_mode_off=run["videoMode"] == "off" and run["executionVersion"] == 1,
        )
        return checks
    checks.update(
        capability_read="video_capabilities" in successful,
        simulated_answer=bool(re.search(r"模拟|mock", answer, re.I)),
        saved_mode_mock=run["videoMode"] == "mock" and run["executionVersion"] == 2,
    )
    if case.name == "unsupported-spec":
        checks.update(
            no_jobs_or_submissions=not jobs and item["upstream"]["submitCalls"] == 0,
            no_generation_attempt="video_generate" not in names,
            unsupported_explained=bool(re.search(r"不支持|不能|无法|不满足|不提供|不符合", answer))
            and "60" in answer
            and "4k" in answer.lower(),
            asks_for_confirmation=bool(re.search(r"确认|选择|是否|哪[种个]|[？?]", answer)),
        )
        return checks
    checks["unique_job"] = len(jobs) == 1
    if len(jobs) != 1:
        return checks
    job = jobs[0]
    registered = [r for r in results if r["name"] == "video_generate" and r["result"].get("ok")]
    checks.update(
        capability_before_generation="video_capabilities" in names
        and "video_generate" in names
        and names.index("video_capabilities") < names.index("video_generate"),
        real_job_reference=job["id"] in answer,
        correct_spec=job["request"]["spec"]
        == {"durationSeconds": 5, "resolution": "720p", "aspectRatio": "16:9"},
        registration_is_not_completion=bool(registered)
        and all(
            r["result"]["data"]["jobId"] == job["id"]
            and r["result"]["data"]["status"] == "pending_submit"
            and r["result"]["data"]["simulated"] is True
            and r["result"]["data"]["mediaAvailable"] is False
            for r in registered
        ),
        submitted_once=job["submitAttempts"] == item["upstream"]["submitCalls"] == 1,
        query_ledger_matches=job["queryAttempts"] == item["upstream"]["queryCalls"] == 2,
        submission_provenance=all(
            s["requestFingerprint"] == job["requestFingerprint"] and s["taskId"] == job["providerTaskId"]
            for s in item["upstream"]["submissions"]
        ),
    )
    if case.name == "register-and-reply":
        checks.update(
            replied_without_wait="await_job" not in names and not item["waits"],
            registration_explained=bool(re.search(r"登记|提交|排队|pending_submit|queued", answer, re.I)),
            worker_completed_independently=job["status"] == "succeeded"
            and item["modelCallsAtReply"] == len(run["modelCalls"]),
        )
        return checks
    awaited = [r for r in results if r["name"] == "await_job"]
    checks.update(
        await_original_job=bool(awaited)
        and all(call["arguments"] == {"jobId": job["id"]} for call in trace if call["name"] == "await_job"),
        durable_deliveries_match=all(
            binding["status"] == "delivered"
            and len([r for r in awaited if r["id"] == binding["context"]["toolCallId"]]) == 1
            and next(r["result"] for r in awaited if r["id"] == binding["context"]["toolCallId"])
            == item["operations"]
            .get(ToolExecutionContext.model_validate(binding["context"]).operation_key, {})
            .get("result")
            == binding["result"]
            for binding in item["waits"]
        ),
        waiting_does_not_call_model=all(sample["unchanged"] for sample in item["waitingSamples"]),
    )
    if offline:
        checks["durable_wait_observed"] = bool(item["waits"] and item["waitingSamples"])
    if case.fails:
        checks.update(
            generation_failed=job["status"] == "failed" and (job["error"] or {}).get("stage") == "generate",
            actual_failure_delivered=bool(awaited)
            and all(r["result"].get("error", {}).get("code") == "JOB_FAILED" for r in awaited),
            failure_explained="失败" in answer
            and "video_generate" in names
            and names.count("video_generate") == 1,
        )
    else:
        refs = [{"artifactId": item["sourceId"], "version": 1}]
        checks.update(
            succeeded=job["status"] == "succeeded",
            actual_success_delivered=bool(awaited)
            and all(
                r["result"].get("ok")
                and r["result"]["data"]["status"] == "succeeded"
                and r["result"]["data"]["result"] == job["result"]
                for r in awaited
            ),
            source_version_preserved=job["request"]["sourceRefs"] == refs
            and (job["result"] or {}).get("sourceRefs") == refs,
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
    return checks


def totals(report):
    items = [item for item in report["cases"] if item.get("run")]
    runs = [item["run"] for item in items]
    calls = [call for run in runs for call in run["modelCalls"]]
    known = [c for c in calls if c.get("inputTokens") is not None and c.get("outputTokens") is not None]
    return {
        "plannedCases": len(CASES),
        "recordedCases": len(items),
        "passedCases": sum(bool(item.get("checks")) and all(item["checks"].values()) for item in items),
        "modelCalls": len(calls),
        "toolCalls": sum(run["toolCalls"] for run in runs),
        "submitCalls": sum(item.get("upstream", {}).get("submitCalls", 0) for item in items),
        "queryCalls": sum(item.get("upstream", {}).get("queryCalls", 0) for item in items),
        "explicitResumes": len(report.get("resumes", [])),
        "automaticResumes": sum(
            event["type"] == "run.resumed" and event.get("resumeKind", "automatic") == "automatic"
            for item in items
            for event in item["applicationEvents"]
        ),
        "connectionRetries": sum(item.get("connectionRetries", 0) for item in items),
        "activeSeconds": round(sum(run["activeSeconds"] for run in runs), 3),
        "externalWaitSeconds": round(sum(run.get("externalWaitSeconds") or 0 for run in runs), 3),
        "observedInputTokens": sum(c.get("inputTokens") or 0 for c in calls),
        "observedOutputTokens": sum(c.get("outputTokens") or 0 for c in calls),
        "callsWithTokenUsage": len(known),
        "callsWithUnknownUsage": len(calls) - len(known),
        "tokenUsageComplete": bool(calls) and len(known) == len(calls),
    }


async def drive(service, item, *, live):
    """Exercise the application-owned loops; only offline Job deadlines use a test clock."""
    current_time = datetime.now(UTC)
    if not live:
        service.video_jobs.clock = lambda: current_time
    run_id = service.store.snapshot()["sessions"][item["case"]]["latestRunId"]
    waiter = asyncio.create_task(service.wait_for_run(run_id))
    waiting_before = {}
    try:
        async with asyncio.timeout(210 if live else 15):
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
                    due = [
                        j.next_poll_at
                        for j in service.video_jobs.list()
                        if j.next_poll_at and not j.query_started_at
                    ]
                    if due:
                        current_time = max(current_time, min(datetime.fromisoformat(value) for value in due))
                await asyncio.sleep(0.05 if live else 0.01)
            record = await waiter
            item["modelCallsAtReply"] = len(record["modelCalls"])
            item["jobsAtReply"] = service.jobs(item["case"])
            # Registration-only replies must not terminate the independently owned Worker.
            if record["status"] == "completed":
                while any(
                    j.status in {"pending_submit", "submitting", "queued", "running"}
                    and j.query_state != "paused"
                    for j in service.video_jobs.list(session_id=item["case"])
                ):
                    if not live:
                        due = [
                            j.next_poll_at
                            for j in service.video_jobs.list()
                            if j.next_poll_at and not j.query_started_at
                        ]
                        if due:
                            current_time = max(
                                current_time, min(datetime.fromisoformat(value) for value in due)
                            )
                    await asyncio.sleep(0.05 if live else 0.01)
    finally:
        if not waiter.done():
            waiter.cancel()
        await asyncio.gather(waiter, return_exceptions=True)


def continuation(args):
    path = getattr(args, "continue_from", None)
    if path is None:
        return None
    try:
        report = json.loads(path.read_text(encoding="utf-8"))
        last = report["cases"][-1]
        valid = (
            report["schemaVersion"] == 1
            and report["suiteId"] == SUITE_ID
            and report["mode"] == ("live" if args.live else "offline-fixture")
            and report["status"] == "failed"
            and last["run"]["resumable"]
            and last["run"]["status"] in {"failed", "cancelled", "interrupted"}
            and [item["case"] for item in report["cases"]]
            == [case.name for case in CASES[: len(report["cases"])]]
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
    if not 1 <= args.max_model_calls <= 40 or not 1024 <= args.context_bytes <= 131072:
        raise AppError("EVAL_BUDGET_INVALID", "模型调用上限须为1～40，上下文预算须为1024～131072字节。")
    if args.output.exists():
        raise AppError("EVAL_OUTPUT_EXISTS", "输出文件已存在，请使用新路径保留先前证据。")
    previous = continuation(args)
    home = args.home.resolve()
    if not previous and home.exists() and any(home.iterdir()):
        raise AppError("EVAL_HOME_NOT_EMPTY", "评测需要新的空数据目录，已有数据未修改。")
    configured = load_config()
    if args.live:
        require_key(configured.api_key)
    config = replace(
        configured,
        home=home,
        api_key=configured.api_key if args.live else None,
        context_bytes=args.context_bytes,
        redis_url=None,
        skills_root=None,
        mcp_config=None,
        mcp_local=False,
    )
    fixture = None if args.live else FixtureModel()
    model_name = config.model if args.live else fixture.name
    if previous and previous["model"] != model_name:
        raise AppError("RESUME_CONFIG_CHANGED", "原评测模型已变化，未发起续跑。")
    report = (
        copy.deepcopy(previous)
        if previous
        else {
            "schemaVersion": 1,
            "suiteId": SUITE_ID,
            "mode": "live" if args.live else "offline-fixture",
            "model": model_name,
            "startedAt": now(),
            "dataDirectory": str(home),
            "contextBudgetBytes": args.context_bytes,
            "cases": [],
            "resumes": [],
            "limitations": "固定合成用例、字面规则评分；离线夹具不衡量模型质量。所有视频均为Mock，无真实媒体。严格故障与重启计数另由回归及安装矩阵验证。",
        }
    )
    report.update(status="incomplete", maxModelCalls=args.max_model_calls)
    report.pop("finishedAt", None)
    if previous:
        report["continuedFrom"] = str(args.continue_from.resolve())

    def persist():
        report["totals"] = totals(report)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_json(args.output, report)

    persist()
    try:
        start_index = len(previous["cases"]) - 1 if previous else 0
        for index in range(start_index, len(CASES)):
            case = CASES[index]
            prior = previous["cases"][index] if previous and index == start_index else None
            remaining = args.max_model_calls - totals(report)["modelCalls"]
            if remaining <= 0 or (
                prior and prior["run"]["policy"]["maxSteps"] - prior["run"]["modelSteps"] > remaining
            ):
                report["status"] = "budget-exhausted"
                break
            scenario = MockScenario(states=("queued", "running", "failed" if case.fails else "succeeded"))
            item = (
                copy.deepcopy(prior)
                if prior
                else {
                    "case": case.name,
                    "videoMode": case.video_mode,
                    "scenario": scenario.model_dump(mode="json", by_alias=True),
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

            def on_event(event):
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
                # Keep model-attempt evidence even when the evaluation is interrupted.
                if service is not None and event["type"] in {"model.started", "model.failed", "run.waiting"}:
                    capture(service, item)
                    persist()

            # Server-side fixture selection never enters the model's tool parameters.
            with patch(
                "vagent.application.MockVideoAdapter",
                lambda store: MockVideoAdapter(store, scenario=scenario),
            ):
                async with ApplicationService.open(
                    replace(config, video_mode=case.video_mode),
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
                                {"kind": "brief", "title": "M1-B 来源版本夹具", "content": SOURCE_TEXT},
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
                        await drive(service, item, live=args.live)
                    finally:
                        capture(service, item)
                        item["connectionRetries"] += service.http_client.connection_retries - retries_before
                        item["elapsedSeconds"] = round(
                            (prior["elapsedSeconds"] if prior else 0) + time.monotonic() - started, 3
                        )
                    item["checks"] = grade(case, item, offline=not args.live)
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
        safe = public_error(error)
        report.update(status="error", error={"code": safe.code, "message": str(safe)})
    finally:
        report["finishedAt"] = now()
        report["evidenceHashes"] = evidence_hashes(home)
        persist()
    return report


def parser():
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--live", action="store_true", help="明确启用真实DeepSeek文本调用；视频始终为Mock")
    result.add_argument("--home", type=Path, default=Path(".vagent/evaluations/m1b") / str(uuid4()))
    result.add_argument("--output", type=Path)
    result.add_argument("--context-bytes", type=int, default=65536)
    result.add_argument("--max-model-calls", type=int, default=40)
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
        f"output/m1b-{'live' if args.live else 'offline'}-{uuid4().hex[:8]}.json"
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
