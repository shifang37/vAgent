"""E3 headless: create a fixed, authorized plan and deliver it without a browser.

The planning phase runs the ordinary Agent runner with a narrowed tool set: the
storyboard tools remain, but no tool can register a Job or assemble a film. The
plan therefore freezes the exact ordered requests, provider profile, capability and
price versions plus every budget bucket. A second command authorizes those exact
limits and only then registers the Jobs; waiting reuses the existing Job/Media
workers and settlement rules, and assembly reuses the local film service. Every
step is recovered from the Run's persisted ``headless`` state, so ``--resume``
never authorizes a different request set or resets a budget.
"""

import asyncio
import json
import time
from decimal import Decimal

from langchain_core.messages import AIMessage, ToolMessage, messages_from_dict
from pydantic import TypeAdapter, ValidationError

from vagent.errors import AppError
from vagent.storage import now
from vagent.video import clients
from vagent.video.contracts import LiveJob, Money, canonical_fingerprint, parse_job
from vagent.video.films import film_media, verify_film_media
from vagent.video.jobs import headless_plan_fingerprint
from vagent.video.media import MediaError
from vagent.video.storyboards import latest_shot_version, storyboard_in_state
from vagent.video.tools import job_settled, jobset_entries, jobset_summary
from vagent.waiting import ToolExecutionContext

FORMAT_VERSION = 1
WAIT_TIMEOUT_SECONDS = 600.0
PLANNING_EXCLUDED_TOOLS = (
    "shot_generate",
    "video_generate",
    "film_assemble",
    "episode_generate",
    "season_generate",
    "shot_redo",
    "audio_generate",
    "film_render",
)
PLANNING_RULES = """本次是 headless 规划阶段：先用 storyboard_save 把方案保存为按顺序的结构化分镜
（每个镜头给出 description、framing、durationSeconds、mode），然后简要说明镜头安排。
本阶段不能登记或提交任何视频任务，也不能生成配音或拼接成片；不要声称已经生成或交付视频。
供应商、模型、规格、能力与价格版本及逐桶预算由服务端据此生成计划，用户随后显式授权。"""

PROVIDER_CURRENCIES = {"wan": "CNY", "kling": "USD", "fal": "USD"}

# Frozen headless exit codes; see M3_CONTRACTS §7.
EXIT_ARGUMENT = 2
EXIT_AUTHORIZATION = 3
EXIT_UPSTREAM = 4
EXIT_WAIT_TIMEOUT = 5
EXIT_RESUME_CHANGED = 6
EXIT_CANCELLED = 130

EXIT_CODES = {
    "HEADLESS_WAIT_TIMEOUT": EXIT_WAIT_TIMEOUT,
    "HEADLESS_PLAN_STALE": EXIT_RESUME_CHANGED,
    "HEADLESS_PLAN_CHANGED": EXIT_RESUME_CHANGED,
    "HEADLESS_PLAN_NOT_FOUND": EXIT_ARGUMENT,
    "HEADLESS_PLAN_INCOMPLETE": EXIT_ARGUMENT,
    "HEADLESS_PLAN_FAILED": EXIT_UPSTREAM,
    "HEADLESS_ALREADY_AUTHORIZED": EXIT_AUTHORIZATION,
    "HEADLESS_CONFIRMATION_REQUIRED": EXIT_AUTHORIZATION,
    "HEADLESS_PLAN_FINGERPRINT_MISMATCH": EXIT_AUTHORIZATION,
    "HEADLESS_AUTHORIZATION_REQUIRED": EXIT_AUTHORIZATION,
    "HEADLESS_AUTHORIZATION_MISMATCH": EXIT_AUTHORIZATION,
    "HEADLESS_BUDGET_NOT_COVERED": EXIT_AUTHORIZATION,
    "HEADLESS_BUDGET_TOO_HIGH": EXIT_AUTHORIZATION,
    "HEADLESS_BUDGET_UNKNOWN": EXIT_AUTHORIZATION,
    "HEADLESS_BUDGET_INVALID": EXIT_AUTHORIZATION,
    "HEADLESS_JOB_FAILED": EXIT_UPSTREAM,
    "CONFIRMATION_REQUIRED": EXIT_AUTHORIZATION,
    "VIDEO_COST_LIMIT": EXIT_AUTHORIZATION,
    "VIDEO_RUN_COST_LIMIT": EXIT_AUTHORIZATION,
    "VIDEO_RUN_UNIT_LIMIT": EXIT_AUTHORIZATION,
    "VIDEO_RUN_JOB_LIMIT": EXIT_AUTHORIZATION,
    "VERSION_CONFLICT": EXIT_RESUME_CHANGED,
    "RESUME_CONFIG_CHANGED": EXIT_RESUME_CHANGED,
    "STALE_RUN": EXIT_RESUME_CHANGED,
    "NO_CHECKPOINT": EXIT_RESUME_CHANGED,
    "VIDEO_MODE_OFF": EXIT_ARGUMENT,
    "INVALID_ARGUMENTS": EXIT_ARGUMENT,
    "INVALID_PROMPT": EXIT_ARGUMENT,
    "MISSING_KEY": EXIT_ARGUMENT,
    "VIDEO_KEY_MISSING": EXIT_ARGUMENT,
    "VIDEO_MODEL_UNAVAILABLE": EXIT_ARGUMENT,
    "VIDEO_UNSUPPORTED_SPEC": EXIT_ARGUMENT,
    "VIDEO_UNSUPPORTED_MODE": EXIT_ARGUMENT,
    "TTS_TEXT_LIMIT": EXIT_ARGUMENT,
    "TTS_PROVIDER_UNKNOWN": EXIT_ARGUMENT,
    "TTS_KEY_MISSING": EXIT_ARGUMENT,
    "VOICE_CONFLICT": EXIT_AUTHORIZATION,
    "TTS_BUDGET_LIMIT": EXIT_AUTHORIZATION,
    "AUDIO_ASSET_MISSING": EXIT_ARGUMENT,
    "AUDIO_FORMAT_UNSUPPORTED": EXIT_ARGUMENT,
    "RENDER_PROFILE_UNSUPPORTED": EXIT_ARGUMENT,
    "RENDER_SOURCE_MISSING": EXIT_ARGUMENT,
    "RENDER_TIMELINE_MISMATCH": EXIT_ARGUMENT,
    "TRANSITION_INVALID": EXIT_ARGUMENT,
    "TITLE_CARD_LIMIT": EXIT_ARGUMENT,
    "RENDER_TIMEOUT": EXIT_UPSTREAM,
    "RENDER_ENCODE_FAILED": EXIT_UPSTREAM,
    "RENDER_VALIDATION_FAILED": EXIT_UPSTREAM,
    "LOUDNESS_OUT_OF_RANGE": EXIT_UPSTREAM,
    "NOT_FOUND": EXIT_ARGUMENT,
    "CANCELLED": EXIT_CANCELLED,
}


class HeadlessCancelled(AppError):
    def __init__(self, run_id: str | None = None):
        super().__init__("CANCELLED", "headless 已中断；计划与已登记任务已保留，可用 --resume 继续。")
        self.run_id = run_id


def exit_code(error: AppError) -> int:
    return EXIT_CODES.get(error.code, EXIT_UPSTREAM)


def blank_result(project_id: str) -> dict:
    return {
        "formatVersion": FORMAT_VERSION,
        "status": "planned",
        "projectId": project_id,
        "sessionId": None,
        "runId": None,
        "planFingerprint": None,
        "storyboardId": None,
        "storyboardVersion": None,
        "storyboardRevision": None,
        "jobIds": [],
        "filmId": None,
        "filmVersion": None,
        "output": None,
        "estimates": [],
        "limits": None,
        "error": None,
    }


# -- plan projection -----------------------------------------------------------


def _bucket_estimates(plan: dict) -> list[dict]:
    return [
        {
            "provider": bucket["provider"],
            "currency": bucket["currency"],
            "amount": bucket["amount"],
            "units": bucket["units"],
            "jobCount": bucket["jobCount"],
            "shots": list(bucket["shots"]),
        }
        for bucket in plan["buckets"]
    ]


def _limits(plan: dict, authorization: dict | None) -> dict:
    return {
        "maxJobs": plan["limits"]["maxJobs"],
        "buckets": [dict(bucket) for bucket in plan["limits"]["buckets"]],
        "authorized": (
            {
                "maxJobs": authorization["maxJobs"],
                "maxCost": [dict(item) for item in authorization["maxCost"]],
                "maxUnits": [dict(item) for item in authorization["maxUnits"]],
                "fingerprint": authorization["fingerprint"],
            }
            if authorization
            else None
        ),
    }


def result_for_run(store, run_id: str, project_id: str | None = None) -> dict:
    run = store.snapshot()["runs"].get(run_id)
    if run is None:
        raise AppError("HEADLESS_PLAN_NOT_FOUND", "没有这个 headless Run。")
    headless = run.get("headless") or {}
    plan = headless.get("plan") or {}
    result = blank_result(project_id or run["sessionId"])
    result.update(
        {
            "sessionId": run["sessionId"],
            "runId": run["id"],
            "status": headless.get("status") or "planned",
            "planFingerprint": headless.get("planFingerprint"),
            "storyboardId": plan.get("storyboardId"),
            "storyboardVersion": plan.get("storyboardVersion"),
            "storyboardRevision": plan.get("storyboardRevision"),
            "estimates": _bucket_estimates(plan) if plan else [],
            "limits": _limits(plan, headless.get("authorization")) if plan else None,
        }
    )
    registration = headless.get("registration") or {}
    result["jobIds"] = list(registration.get("jobIds") or [])
    delivery = headless.get("delivery") or {}
    if delivery:
        result.update(
            {
                "status": "delivered",
                "filmId": delivery["filmId"],
                "filmVersion": delivery["filmVersion"],
            }
        )
    return result


def delivery_output(service, delivery: dict) -> dict:
    """Verify the recorded film file and return its controlled local summary."""
    state = service.store.snapshot()
    media = film_media(state, delivery["mediaId"])
    if media is None:
        raise AppError("FILM_MEDIA_INVALID", "已交付成片的媒体记录不可用，请重新拼接。")
    verify_film_media(state, media)
    if (media.sha256, media.size_bytes) != (delivery["sha256"], delivery["sizeBytes"]):
        raise AppError("FILM_MEDIA_INVALID", "已交付成片的记录与索引不一致。")
    try:
        lease = service.video_jobs.media.files.verified(media.relative_path, media)
    except (MediaError, OSError):
        raise AppError("FILM_MEDIA_INVALID", "已交付成片的本地文件缺失或损坏。") from None
    lease.close()
    path = (service.store.home / media.relative_path).resolve()
    if service.store.home.resolve() not in path.parents:
        raise AppError("FILM_MEDIA_INVALID", "成片路径不在数据目录内。")
    return {
        "mediaId": media.id,
        "path": str(path),
        "sha256": media.sha256,
        "sizeBytes": media.size_bytes,
    }


def authorization_hint(result: dict) -> str:
    parts = [
        f"--max-cost {item['provider']}:{item['currency']}={item['amount']}" for item in result["estimates"]
    ]
    parts += [
        f"--max-units {item['provider']}={item['units']}"
        for item in result["estimates"]
        if item["units"] is not None
    ]
    parts.append(f"--max-jobs {max(1, sum(item['jobCount'] for item in result['estimates']))}")
    return (
        f"vagent headless --plan {result['runId']} --plan-fingerprint {result['planFingerprint']} "
        f"--confirm {' '.join(parts)}"
    )


# -- planning ------------------------------------------------------------------


def _last_saved_storyboard(record: dict) -> dict | None:
    calls, saved = {}, None
    for message in messages_from_dict(record["messages"]):
        if isinstance(message, AIMessage):
            for call in message.tool_calls:
                calls[call["id"]] = call
        elif isinstance(message, ToolMessage) and message.tool_call_id in calls:
            call = calls[message.tool_call_id]
            if call["name"] != "storyboard_save":
                continue
            try:
                payload = json.loads(message.content)
            except (TypeError, ValueError):
                continue
            if (
                isinstance(payload, dict)
                and payload.get("ok")
                and payload.get("data", {}).get("storyboardId")
            ):
                saved = payload["data"]
    return saved


class _RecordedModel:
    """A name-only stand-in used to rebuild a Run's recorded context signature."""

    def __init__(self, name: str):
        self.name = name


def _check_resume_signature(service, run: dict) -> None:
    """A plan only resumes under the tool set, skills and context it was made with."""
    mode = run.get("videoMode", "off")
    if mode not in {"mock", "live"}:
        raise AppError("RESUME_CONFIG_CHANGED", "headless 计划记录的视频模式不可恢复。")
    runner = service.headless_runner(model=_RecordedModel(run["model"]), video_mode=mode)
    if run.get("contextSignature") != runner.context_signature(run.get("contextVersion", 1)):
        raise AppError(
            "RESUME_CONFIG_CHANGED",
            "headless 计划记录的工具、skills 或上下文配置与当前不一致；请恢复原配置或重新规划。",
        )


def _persist_plan(store, run_id: str, plan: dict, fingerprint: str) -> None:
    timestamp = now()

    def write(draft):
        run = draft["runs"].get(run_id)
        if run is None:
            raise AppError("NOT_FOUND", "没有这个 headless Run。")
        if run.get("headless"):
            raise AppError("HEADLESS_PLAN_CHANGED", "这个 Run 已经有 headless 计划。")
        run["headless"] = {
            "status": "planned",
            "projectId": run["sessionId"],
            "plan": plan,
            "planFingerprint": fingerprint,
            "authorization": None,
            "registration": None,
            "delivery": None,
            "createdAt": timestamp,
        }
        run["updatedAt"] = timestamp

    store.transaction(write)


async def plan(
    service,
    *,
    session_id: str,
    prompt: str,
    request_id: str | None = None,
    model=None,
    on_event=None,
    cancelled=None,
) -> dict:
    """Draft a storyboard with the orchestration model and freeze the request plan."""
    if not prompt.strip() or len(prompt) > 20000:
        raise AppError("INVALID_PROMPT", "需求不能为空，且不能超过 20000 字符。")
    if service.config.video_mode not in {"mock", "live"}:
        raise AppError("VIDEO_MODE_OFF", "headless 需要 mock 或 live 视频模式；当前为 off。")
    service.store.ensure_session(session_id)
    runner = service.headless_runner(model=model, on_event=on_event)
    record = await runner.run(
        session_id,
        prompt,
        request_id=request_id,
        cancelled=cancelled,
        shutdown=service.shutdown,
    )
    if record.get("headless"):
        return result_for_run(service.store, record["id"])
    if record["status"] == "cancelled":
        raise HeadlessCancelled(record["id"])
    if record["status"] != "completed":
        raise AppError(
            "HEADLESS_PLAN_FAILED",
            f"编排未完成（{record.get('errorCode') or record['status']}），未产生计划。",
        )
    saved = _last_saved_storyboard(record)
    if saved is None:
        raise AppError("HEADLESS_PLAN_INCOMPLETE", "编排没有保存结构化分镜，未产生计划。")
    state = service.store.snapshot()
    board = storyboard_in_state(state, session_id, saved["storyboardId"])
    if board.current_version != saved["version"]:
        raise AppError("VERSION_CONFLICT", "分镜版本在规划期间已变化，请重新规划。")
    frozen = service.video_jobs.plan_shots(session_id, board.id, board.current_version)
    if not frozen["newJobCount"]:
        raise AppError("HEADLESS_PLAN_INCOMPLETE", "所有镜头都已有相同请求，没有需要授权的新任务。")
    fingerprint = headless_plan_fingerprint(frozen)
    _persist_plan(service.store, record["id"], frozen, fingerprint)
    return result_for_run(service.store, record["id"])


# -- authorization -------------------------------------------------------------


def _parse_cost(values) -> dict[tuple[str, str], str]:
    costs: dict[tuple[str, str], str] = {}
    for raw in values:
        if not isinstance(raw, str) or raw.count("=") != 1:
            raise AppError("HEADLESS_BUDGET_INVALID", "预算必须写成 PROVIDER:CURRENCY=AMOUNT。")
        key, amount = (part.strip() for part in raw.split("=", 1))
        if key.count(":") != 1:
            raise AppError("HEADLESS_BUDGET_INVALID", "预算必须写成 PROVIDER:CURRENCY=AMOUNT。")
        provider, currency = (part.strip().lower() for part in key.split(":", 1))
        expected = PROVIDER_CURRENCIES.get(provider)
        if expected is None or currency.upper() != expected:
            raise AppError("HEADLESS_BUDGET_UNKNOWN", f"未知预算桶 {raw}；只接受已计划的供应商/币种。")
        currency = currency.upper()
        if (provider, currency) in costs:
            raise AppError("HEADLESS_BUDGET_INVALID", "同一预算桶不能重复给出。")
        try:
            costs[(provider, currency)] = TypeAdapter(Money).validate_python(amount)
        except (ValidationError, ValueError):
            raise AppError("HEADLESS_BUDGET_INVALID", "预算金额必须是非负十进制字符串。") from None
    return costs


def _parse_units(values) -> dict[str, int]:
    units: dict[str, int] = {}
    for raw in values:
        if not isinstance(raw, str) or raw.count("=") != 1:
            raise AppError("HEADLESS_BUDGET_INVALID", "资源单位必须写成 PROVIDER=UNITS。")
        provider, value = (part.strip() for part in raw.split("=", 1))
        provider = provider.lower()
        if provider != "kling":
            raise AppError("HEADLESS_BUDGET_UNKNOWN", f"未知资源单位桶 {raw}；只有 kling 使用 VIDEO_UNIT。")
        if provider in units:
            raise AppError("HEADLESS_BUDGET_INVALID", "同一资源单位桶不能重复给出。")
        try:
            number = int(value)
            if number < 0 or str(number) != value:
                raise ValueError
        except ValueError:
            raise AppError("HEADLESS_BUDGET_INVALID", "资源单位必须是非负整数。") from None
        units[provider] = number
    return units


def _authorization(service, plan: dict, fingerprint: str, max_cost, max_units, max_jobs) -> dict:
    planned = {(bucket["provider"], bucket["currency"]): bucket for bucket in plan["buckets"]}
    costs = _parse_cost(max_cost)
    units = _parse_units(max_units)
    for key in costs:
        if key not in planned:
            raise AppError("HEADLESS_BUDGET_UNKNOWN", "授权包含计划未使用的金额桶，未授权任何任务。")
    for key, bucket in planned.items():
        if key not in costs:
            raise AppError(
                "HEADLESS_BUDGET_NOT_COVERED", f"缺少 {key[0]}:{key[1]} 的授权金额，未授权任何任务。"
            )
        if Decimal(costs[key]) < Decimal(bucket["amount"]):
            raise AppError(
                "HEADLESS_BUDGET_NOT_COVERED",
                f"{key[0]}:{key[1]} 授权金额低于计划估算，未授权任何任务。",
            )
        _, run_amount, _, run_units = service.video_jobs.budget_ceiling(bucket["provider"])
        if Decimal(costs[key]) > Decimal(run_amount):
            raise AppError("HEADLESS_BUDGET_TOO_HIGH", "授权金额超过服务端上限，未授权任何任务。")
        if bucket["units"] is None:
            continue
        provider = bucket["provider"]
        if provider not in units:
            raise AppError("HEADLESS_BUDGET_NOT_COVERED", "缺少资源单位授权，未授权任何任务。")
        if units[provider] < bucket["units"]:
            raise AppError("HEADLESS_BUDGET_NOT_COVERED", "资源单位授权低于计划用量，未授权任何任务。")
        if run_units is None or units[provider] > run_units:
            raise AppError("HEADLESS_BUDGET_TOO_HIGH", "资源单位授权超过服务端上限，未授权任何任务。")
    for provider in units:
        if provider not in {bucket["provider"] for bucket in plan["buckets"]}:
            raise AppError("HEADLESS_BUDGET_UNKNOWN", "授权包含计划未使用的资源单位桶。")
    ceiling = int(plan["limits"]["maxJobs"])
    if plan["newJobCount"] > ceiling:
        raise AppError("HEADLESS_BUDGET_TOO_HIGH", "计划本身超过共享新任务上限。")
    jobs = ceiling if max_jobs is None else max_jobs
    if type(jobs) is not int or not 1 <= jobs <= ceiling:
        raise AppError("HEADLESS_BUDGET_TOO_HIGH", "授权任务数必须在 1 到共享上限之间。")
    if jobs < plan["newJobCount"]:
        raise AppError("HEADLESS_BUDGET_NOT_COVERED", "授权任务数低于计划新任务数，未授权任何任务。")
    cost_list = [
        {"provider": provider, "currency": currency, "amount": amount}
        for (provider, currency), amount in sorted(costs.items())
    ]
    unit_list = [{"provider": provider, "units": value} for provider, value in sorted(units.items())]
    authorization = {
        "planFingerprint": fingerprint,
        "maxCost": cost_list,
        "maxUnits": unit_list,
        "maxJobs": jobs,
        "createdAt": now(),
    }
    authorization["fingerprint"] = canonical_fingerprint(
        {
            "planFingerprint": fingerprint,
            "maxCost": cost_list,
            "maxUnits": unit_list,
            "maxJobs": jobs,
        }
    )
    return authorization


def authorize(
    service,
    *,
    run_id: str,
    plan_fingerprint: str,
    confirm: bool,
    max_cost=(),
    max_units=(),
    max_jobs=None,
) -> dict:
    """Bind this exact plan and these exact limits; register nothing yet."""
    state = service.store.snapshot()
    run = state["runs"].get(run_id)
    if run is None or not run.get("headless"):
        raise AppError("HEADLESS_PLAN_NOT_FOUND", "没有这个 headless 计划；请先运行 vagent headless PROMPT。")
    headless = run["headless"]
    if headless.get("planFingerprint") != plan_fingerprint:
        raise AppError("HEADLESS_PLAN_FINGERPRINT_MISMATCH", "计划指纹不匹配，未授权任何任务。")
    if not confirm:
        raise AppError("HEADLESS_CONFIRMATION_REQUIRED", "需要 --confirm 明确确认这次固定请求集合。")
    if headless.get("status") != "planned":
        raise AppError("HEADLESS_ALREADY_AUTHORIZED", "这个计划已经授权；继续执行请使用 --resume。")
    _check_resume_signature(service, run)
    plan = headless["plan"]
    if headless_plan_fingerprint(plan) != plan_fingerprint:
        raise AppError("HEADLESS_PLAN_CHANGED", "计划内容与已保存指纹不一致，未授权任何任务。")
    board = storyboard_in_state(state, run["sessionId"], plan["storyboardId"])
    if board.current_version != plan["storyboardVersion"] or board.revision != plan["storyboardRevision"]:
        raise AppError("HEADLESS_PLAN_STALE", "分镜在计划后已变化，请重新规划并确认。")
    current = service.video_jobs.plan_shots(run["sessionId"], board.id, board.current_version)
    if headless_plan_fingerprint(current) != plan_fingerprint:
        raise AppError("HEADLESS_PLAN_STALE", "能力、价格或请求在计划后已变化，请重新规划并确认。")
    authorization = _authorization(service, plan, plan_fingerprint, max_cost, max_units, max_jobs)

    def write(draft):
        run = draft["runs"][run_id]
        current_headless = run.get("headless") or {}
        if current_headless.get("status") != "planned" or current_headless.get("planFingerprint") != (
            plan_fingerprint
        ):
            raise AppError("HEADLESS_PLAN_CHANGED", "计划状态在授权期间已变化，未授权任何任务。")
        current_board = draft["storyboards"][plan["storyboardId"]]
        if current_board["revision"] != plan["storyboardRevision"]:
            raise AppError("HEADLESS_PLAN_STALE", "分镜在授权期间已变化，未授权任何任务。")
        current_headless["status"] = "authorized"
        current_headless["authorization"] = authorization
        current_headless["authorizedAt"] = now()
        run["headless"] = current_headless
        run["updatedAt"] = now()

    service.store.transaction(write)
    return result_for_run(service.store, run_id)


# -- registration, waiting and delivery ---------------------------------------


def _register(service, run_id: str, plan: dict, authorization: dict) -> None:
    run = service.store.snapshot()["runs"][run_id]
    context = ToolExecutionContext(
        project_id=run["sessionId"],
        session_id=run["sessionId"],
        run_id=run_id,
        model_step=1,
        tool_call_id="headless_generate",
    )
    args = {
        "storyboardId": plan["storyboardId"],
        "storyboardVersion": plan["storyboardVersion"],
        "shotIds": [row["shotId"] for row in plan["shots"]],
        "confirm": True,
    }
    result = service.video_jobs.shot_generate(args, context=context, authorization=authorization)
    if not result.get("ok"):
        raise AppError(result["error"]["code"], result["error"]["message"])


def _job_summary(service, job_ids):
    state = service.store.snapshot()
    items = []
    for job_id in job_ids:
        raw = state["jobs"].get(job_id)
        if raw is None:
            items.append((job_id, None))
            continue
        job = parse_job(raw)
        if isinstance(job, LiveJob) and job.status == "succeeded":
            job = service.video_jobs.media.refresh(job)
        items.append((job_id, job))
    entries = jobset_entries(items)
    settled = all(job is not None and job_settled(job) for _, job in items)
    return {**jobset_summary(entries), "allSettled": settled}


async def _wait(service, session_id: str, job_ids, timeout: float, cancelled, on_event) -> dict:
    queue = service.subscribe(session_id)
    deadline = time.monotonic() + timeout
    try:
        while True:
            summary = _job_summary(service, job_ids)
            if summary["allSettled"]:
                return summary
            if cancelled is not None and cancelled.is_set():
                raise HeadlessCancelled()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise AppError(
                    "HEADLESS_WAIT_TIMEOUT",
                    f"等待 {len(job_ids)} 个镜头任务超时；任务保留，可用 --resume 继续。",
                )
            try:
                async with asyncio.timeout(min(remaining, 0.5)):
                    await queue.get()
            except TimeoutError:
                pass
    finally:
        service.subscribers.pop(queue, None)


async def _assemble(service, run_id: str, plan: dict, revision: int) -> dict:
    return await clients.assemble_film(
        service.video_jobs,
        session_id=service.store.snapshot()["runs"][run_id]["sessionId"],
        client_request_id=f"headless-film-{run_id}",
        storyboard_id=plan["storyboardId"],
        expected_revision=revision,
    )


def _persist_delivery(store, run_id: str, film: dict) -> None:
    timestamp = now()
    latest = film["latest"]

    def write(draft):
        run = draft["runs"][run_id]
        headless = run["headless"]
        headless["status"] = "delivered"
        headless["delivery"] = {
            "filmId": film["filmId"],
            "filmVersion": film["currentVersion"],
            "mediaId": latest["outputMediaId"],
            "sha256": latest["sha256"],
            "sizeBytes": latest["sizeBytes"],
            "deliveredAt": timestamp,
        }
        headless["deliveredAt"] = timestamp
        run["headless"] = headless
        run["updatedAt"] = timestamp

    store.transaction(write)


async def execute(
    service,
    *,
    run_id: str,
    wait_timeout: float | None = None,
    cancelled=None,
    on_event=None,
) -> dict:
    """Register (first time only), wait for every shot and assemble the film."""
    state = service.store.snapshot()
    run = state["runs"].get(run_id)
    if run is None or not run.get("headless"):
        raise AppError("HEADLESS_PLAN_NOT_FOUND", "没有这个 headless 计划。")
    headless = run["headless"]
    if headless.get("status") == "delivered":
        result = result_for_run(service.store, run_id)
        result["output"] = delivery_output(service, headless["delivery"])
        return result
    if headless.get("status") != "authorized":
        raise AppError(
            "HEADLESS_AUTHORIZATION_REQUIRED", "计划尚未授权；先运行带 --confirm 与预算的授权命令。"
        )
    _check_resume_signature(service, run)
    plan, authorization = headless["plan"], headless["authorization"]
    if headless_plan_fingerprint(plan) != headless.get("planFingerprint"):
        raise AppError("HEADLESS_PLAN_CHANGED", "计划内容与已保存指纹不一致，未继续执行。")
    board = storyboard_in_state(state, run["sessionId"], plan["storyboardId"])
    if board.current_version != plan["storyboardVersion"]:
        raise AppError("HEADLESS_PLAN_STALE", "分镜在计划后已变化，请重新规划并确认。")
    if headless.get("registration") is None:
        if plan["newJobCount"]:
            _register(service, run_id, plan, authorization)
        else:
            raise AppError("HEADLESS_PLAN_STALE", "计划没有可登记的新任务，请重新规划。")
        state = service.store.snapshot()
        run = state["runs"][run_id]
        headless = run["headless"]
    registration = headless["registration"]
    board = storyboard_in_state(state, run["sessionId"], plan["storyboardId"])
    if board.revision != registration["revision"]:
        raise AppError("HEADLESS_PLAN_STALE", "分镜在登记后已变化，未继续等待或拼接。")
    for item in registration["shots"]:
        latest = latest_shot_version(board, plan["storyboardVersion"], item["shotId"])
        if latest is None or latest.id != item["shotVersionId"]:
            raise AppError("HEADLESS_PLAN_STALE", "镜头版本在登记后已变化，未继续等待或拼接。")
    if on_event:
        on_event({"type": "headless.registered", "jobIds": list(registration["jobIds"])})
    summary = await _wait(
        service,
        run["sessionId"],
        registration["jobIds"],
        WAIT_TIMEOUT_SECONDS if wait_timeout is None else wait_timeout,
        cancelled,
        on_event,
    )
    failed = [item for item in summary["jobs"] if not item["ok"]]
    if failed:
        raise AppError(
            "HEADLESS_JOB_FAILED",
            f"{len(failed)} 个镜头任务未交付（{failed[0]['error']['code']}）；保留原任务，可 --resume。",
        )
    film = await _assemble(service, run_id, plan, registration["revision"])
    _persist_delivery(service.store, run_id, film)
    result = result_for_run(service.store, run_id)
    result["output"] = delivery_output(
        service, service.store.snapshot()["runs"][run_id]["headless"]["delivery"]
    )
    return result
