"""Client-initiated Runs and storyboard actions for the local CLI and Web.

Redo and batch generation are explicit user actions, not model turns. Each action
gets its own durable Run record, so its Jobs keep a real execution context and a
fresh per-Run quota, while the session's latest Run stays the conversation — an
interrupted conversation Run therefore keeps its resume path. Nothing here calls
a model, and a client Run never runs the execution graph.

The Run ID is derived from (session, clientRequestId), so retrying the same
request replays the original registration instead of registering new Jobs.
"""

from uuid import UUID, uuid5

from vagent.context import CONTEXT_VERSION
from vagent.errors import AppError
from vagent.runner import RunPolicy
from vagent.storage import RunRecord, now
from vagent.video.storyboards import shot_of, storyboard_in_state
from vagent.video.tools import LIVE_VIDEO_RULES_VERSION, LIVE_VIDEO_TOOLS_VERSION
from vagent.video.tools import VIDEO_RULES_VERSION as MOCK_VIDEO_RULES_VERSION
from vagent.video.tools import VIDEO_TOOLS_VERSION as MOCK_VIDEO_TOOLS_VERSION
from vagent.waiting import WAIT_EXECUTION_VERSION, ToolExecutionContext

CLIENT_RUN_NAMESPACE = UUID("6d1a4f2e-6f2b-4a4d-9a71-3f5d9c0b7e41")
CLIENT_RUN_MODEL = "local-client"
CLIENT_PROMPTS = {
    "shot_generate": "本地界面发起的镜头批量登记（不调用模型）。",
    "shot_redo": "本地界面发起的镜头重做（不调用模型）。",
    "film_assemble": "本地界面发起的成片拼接（不调用模型）。",
}
CLIENT_GROUPS = ("shot_generate", "shot_redo", "film_assemble")


def client_run_id(session_id: str, client_request_id: str) -> str:
    """One deterministic Run per (session, client request), so retries replay."""
    return str(uuid5(CLIENT_RUN_NAMESPACE, f"vagent-client-run:{session_id}:{client_request_id}"))


def client_features(service, mode: str) -> dict:
    """The tool features a client Run records, exactly as an Agent Run would."""
    return {
        "video": {
            "mode": mode,
            "toolsVersion": LIVE_VIDEO_TOOLS_VERSION if mode == "live" else MOCK_VIDEO_TOOLS_VERSION,
            "rulesVersion": LIVE_VIDEO_RULES_VERSION if mode == "live" else MOCK_VIDEO_RULES_VERSION,
            "capabilities": [
                item.model_dump(mode="json", by_alias=True) for item in service.capabilities(mode)
            ],
        }
    }


def client_context(service, *, session_id: str, client_request_id: str, group: str, require_mode_config=True):
    """Create or reuse the Run that owns one explicit local action."""
    if group not in CLIENT_GROUPS:
        raise AppError("INVALID_ARGUMENTS", "未知的本地动作。")
    mode = service.generation_mode()
    if mode == "live" and require_mode_config:
        service.check_live_configuration()
    store = service.store
    run_id = client_run_id(session_id, client_request_id)
    existing = store.snapshot()["runs"].get(run_id)
    if existing is not None:
        if existing["sessionId"] != session_id or existing.get("videoMode") != mode:
            raise AppError("OPERATION_CONFLICT", "这个请求 ID 已用于其他项目或视频模式，请换一个再试。")
    else:
        features = client_features(service, mode)
        policy = RunPolicy()

        def create(draft):
            if session_id not in draft["projects"] or session_id not in draft["sessions"]:
                raise AppError("NOT_FOUND", "当前数据目录中没有这个项目，未登记任务。")
            if run_id in draft["runs"]:
                return
            timestamp = now()
            # A client Run is immediately terminal: it records provenance and the
            # per-Run quota, and deliberately does not become the session's latest
            # Run, so an unfinished conversation Run stays resumable.
            draft["runs"][run_id] = RunRecord(
                id=run_id,
                session_id=session_id,
                request_id=client_request_id,
                prompt=CLIENT_PROMPTS[group],
                model=CLIENT_RUN_MODEL,
                status="completed",
                messages=[],
                model_steps=0,
                tool_calls=0,
                input_tokens=0,
                output_tokens=0,
                answer="本地界面动作已记录；未调用模型。",
                created_at=timestamp,
                updated_at=timestamp,
                execution_version=WAIT_EXECUTION_VERSION,
                context_version=CONTEXT_VERSION,
                policy={
                    "maxSteps": policy.max_steps,
                    "maxToolCalls": policy.max_tool_calls,
                    "timeoutSeconds": policy.timeout_seconds,
                },
                resumable=False,
                usage_start_step=1,
                model_calls=[],
                video_mode=mode,
                tool_features=features,
                active_wait_id=None,
                external_wait_seconds=0,
                external_wait_started_at=None,
                wait_resume_error=None,
            ).model_dump(mode="json", by_alias=True)

        store.transaction(create)
    return ToolExecutionContext(
        project_id=session_id,
        session_id=session_id,
        run_id=run_id,
        model_step=1,
        tool_call_id=group,
    )


def _read_storyboard(store, project_id: str, storyboard_id: str, storyboard_version: int, shot_id=None):
    """Pre-read the scope so a doomed action never leaves an empty client Run."""
    record = storyboard_in_state(store.snapshot(), project_id, storyboard_id)
    if record.current_version != storyboard_version:
        raise AppError("VERSION_CONFLICT", "分镜版本已变化，请重新读取后再生成。")
    if shot_id is not None:
        shot_of(record, storyboard_version, shot_id)
    return record


def _operation_key(run_id: str, group: str) -> str:
    """The one operation key a client Run uses for its single explicit action."""
    return f"{run_id}:1:{group}"


def _replay_or_conflict(service, session_id: str, client_request_id: str, group: str, args: dict | None):
    """Replay this request's recorded outcome, or refuse a different request under it.

    One request ID owns one outcome: a retry must behave exactly like the first
    attempt even after the storyboard revision moved on, so the journal is consulted
    before any scope check. ``args`` are the arguments the caller restates (the Web
    sends back the body it rendered); a caller that cannot restate them (the CLI
    names only a shot) passes None and the request ID alone decides.
    """
    run_id = client_run_id(session_id, client_request_id)
    state = service.store.snapshot()
    if run_id not in state["runs"]:
        return None
    entry = state["operations"].get(_operation_key(run_id, group))
    if entry is None:
        return None
    if args is not None and service.store.operation_fingerprint(group, args) != entry["fingerprint"]:
        raise AppError("OPERATION_CONFLICT", "同一请求 ID 不能用于另一个镜头操作，请换一个请求 ID。")
    result = entry["result"]
    if not result.get("ok"):
        raise AppError(result["error"]["code"], result["error"]["message"])
    return _result(service, result["data"], run_id, result["data"]["storyboardId"])


def generate_shots(
    service,
    *,
    session_id: str,
    client_request_id: str,
    storyboard_id: str,
    storyboard_version: int,
    shot_ids,
    confirm: bool = True,
) -> dict:
    """Register one Job per confirmed shot as an explicit local user action."""
    args = {
        "storyboardId": storyboard_id,
        "storyboardVersion": storyboard_version,
        "shotIds": list(shot_ids),
        "confirm": confirm,
    }
    replayed = _replay_or_conflict(service, session_id, client_request_id, "shot_generate", args)
    if replayed is not None:
        return replayed
    _read_storyboard(service.store, session_id, storyboard_id, storyboard_version)
    context = client_context(
        service, session_id=session_id, client_request_id=client_request_id, group="shot_generate"
    )
    result = service.shot_generate(args, context=context)
    if not result.get("ok"):
        raise AppError(result["error"]["code"], result["error"]["message"])
    return _result(service, result["data"], context.run_id, storyboard_id)


def redo_shot(
    service,
    *,
    session_id: str,
    client_request_id: str,
    storyboard_id: str,
    storyboard_version: int,
    shot_id: str,
    expected_revision: int | None = None,
) -> dict:
    """Register a new ShotVersion and one new Job for the named shot only.

    ``expected_revision`` is the revision the caller rendered, for the Web and for
    callers that can restate their request. ``None`` means the local caller has no
    rendered revision (the CLI): the request ID alone decides, so a retry replays
    the recorded outcome and a first attempt uses the revision read right now.
    """
    stated = None
    if expected_revision is not None:
        stated = {
            "storyboardId": storyboard_id,
            "storyboardVersion": storyboard_version,
            "shotId": shot_id,
            "expectedRevision": expected_revision,
            "clientRequestId": client_request_id,
        }
    replayed = _replay_or_conflict(service, session_id, client_request_id, "shot_redo", stated)
    if replayed is not None:
        return replayed
    record = _read_storyboard(service.store, session_id, storyboard_id, storyboard_version, shot_id)
    if expected_revision is None:
        expected_revision = record.revision
    elif record.revision != expected_revision:
        raise AppError("VERSION_CONFLICT", "分镜记录已变化，请重新读取后再重做。")
    args = {
        "storyboardId": storyboard_id,
        "storyboardVersion": storyboard_version,
        "shotId": shot_id,
        "expectedRevision": expected_revision,
        "clientRequestId": client_request_id,
    }
    context = client_context(
        service, session_id=session_id, client_request_id=client_request_id, group="shot_redo"
    )
    result = service.redo_shot(args, context=context)
    if not result.get("ok"):
        raise AppError(result["error"]["code"], result["error"]["message"])
    return _result(service, result["data"], context.run_id, storyboard_id)


async def assemble_film(
    service,
    *,
    session_id: str,
    client_request_id: str,
    storyboard_id: str,
    expected_revision: int,
) -> dict:
    """Concatenate the current delivered clips as an explicit local user action."""
    args = {"storyboardId": storyboard_id, "expectedRevision": expected_revision}
    replayed = _replay_or_conflict(service, session_id, client_request_id, "film_assemble", args)
    if replayed is not None:
        return replayed
    record = storyboard_in_state(service.store.snapshot(), session_id, storyboard_id)
    if record.revision != expected_revision:
        raise AppError("VERSION_CONFLICT", "分镜记录已变化，请重新读取后再拼接。")
    context = client_context(
        service,
        session_id=session_id,
        client_request_id=client_request_id,
        group="film_assemble",
        require_mode_config=False,
    )
    result = await service.films.assemble(
        project_id=session_id,
        storyboard_id=storyboard_id,
        expected_revision=expected_revision,
        operation_key=_operation_key(context.run_id, "film_assemble"),
        args=args,
    )
    if not result.get("ok"):
        raise AppError(result["error"]["code"], result["error"]["message"])
    return _result(service, result["data"], context.run_id, storyboard_id)


def _result(service, data: dict, run_id: str, storyboard_id: str) -> dict:
    raw = service.store.snapshot()["storyboards"].get(storyboard_id) or {}
    return {
        **data,
        "runId": run_id,
        "mode": service.generation_mode(),
        "storyboardRevision": raw.get("revision"),
    }
