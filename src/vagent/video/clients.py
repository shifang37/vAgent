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
    "episode_generate": "本地界面发起的分集整批登记（不调用模型）。",
    "subtitle_save": "本地界面发起的字幕保存（不调用模型）。",
    "subtitle_export": "本地界面发起的字幕导出（不调用模型）。",
    "film_render": "本地界面发起的整片渲染（不调用模型）。",
    "season_save": "本地界面发起的季成员保存（不调用模型）。",
}
CLIENT_GROUPS = (
    "shot_generate",
    "shot_redo",
    "film_assemble",
    "episode_generate",
    "subtitle_save",
    "subtitle_export",
    "film_render",
    "season_save",
)


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


def client_context(
    service,
    *,
    session_id: str,
    client_request_id: str,
    group: str,
    require_mode_config=True,
    metadata_only=False,
):
    """Create or reuse the Run that owns one explicit local action.

    ``metadata_only`` is for actions that never touch a video provider (season
    membership). Such a Run keeps video mode off and records no video tool
    features, so it works in text-only projects and can never be mistaken for a
    generation Run.
    """
    if group not in CLIENT_GROUPS:
        raise AppError("INVALID_ARGUMENTS", "未知的本地动作。")
    mode = "off" if metadata_only else service.generation_mode()
    if mode == "live" and require_mode_config:
        # The two-Key setup needs only the Key of the provider the user selected.
        service.check_live_configuration(service.config.video_provider)
    store = service.store
    run_id = client_run_id(session_id, client_request_id)
    existing = store.snapshot()["runs"].get(run_id)
    if existing is not None:
        if existing["sessionId"] != session_id or existing.get("videoMode") != mode:
            raise AppError("OPERATION_CONFLICT", "这个请求 ID 已用于其他项目或视频模式，请换一个再试。")
    else:
        features = {} if metadata_only else client_features(service, mode)
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
    return _result(service, result["data"], run_id, result["data"].get("storyboardId"))


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


def generate_episode(
    service,
    *,
    session_id: str,
    client_request_id: str,
    episode_id: str,
    confirm: bool = True,
) -> dict:
    """Register every shot of one episode as an explicit local user action."""
    args = {"episodeId": episode_id, "confirm": confirm}
    replayed = _replay_or_conflict(service, session_id, client_request_id, "episode_generate", args)
    if replayed is not None:
        return replayed
    raw = service.store.snapshot()["episodes"].get(episode_id)
    if raw is None or raw["projectId"] != session_id:
        raise AppError("EPISODE_NOT_FOUND", "当前项目中没有这个分集。")
    context = client_context(
        service, session_id=session_id, client_request_id=client_request_id, group="episode_generate"
    )
    result = service.episode_generate(args, context=context)
    if not result.get("ok"):
        raise AppError(result["error"]["code"], result["error"]["message"])
    return _result(service, result["data"], context.run_id)


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


def _result(service, data: dict, run_id: str, storyboard_id: str | None = None) -> dict:
    try:
        mode = service.generation_mode()
    except AppError:
        # A metadata-only local action (season membership) is allowed with video off.
        mode = getattr(service.config, "video_mode", "off")
    result = {
        **data,
        "runId": run_id,
        "mode": mode,
    }
    if storyboard_id is not None:
        raw = service.store.snapshot()["storyboards"].get(storyboard_id) or {}
        result["storyboardRevision"] = raw.get("revision")
    return result


def save_subtitles(
    service,
    *,
    session_id: str,
    client_request_id: str,
    scope: str,
    storyboard_id: str | None = None,
    episode_id: str | None = None,
    cues=None,
    track_id: str | None = None,
    expected_revision: int | None = None,
) -> dict:
    """Save or correct a subtitle track as one explicit local action."""
    from vagent.video.subtitles import save_subtitles

    args = {
        "scope": scope,
        **({"storyboardId": storyboard_id} if storyboard_id is not None else {}),
        **({"episodeId": episode_id} if episode_id is not None else {}),
        **({"cues": cues} if cues is not None else {}),
        **({"trackId": track_id} if track_id is not None else {}),
        **({"expectedRevision": expected_revision} if expected_revision is not None else {}),
    }
    replayed = _replay_or_conflict(service, session_id, client_request_id, "subtitle_save", args)
    if replayed is not None:
        return replayed
    if scope == "storyboard":
        if storyboard_id is None:
            raise AppError("INVALID_ARGUMENTS", "保存分镜字幕需要 storyboardId。")
        target = service.store.snapshot()["storyboards"].get(storyboard_id)
        if target is None or target["projectId"] != session_id:
            raise AppError("NOT_FOUND", "当前项目中没有这个分镜。")
    elif scope == "episode":
        if episode_id is None:
            raise AppError("INVALID_ARGUMENTS", "保存整集字幕需要 episodeId。")
        target = service.store.snapshot()["episodes"].get(episode_id)
        if target is None or target["projectId"] != session_id:
            raise AppError("EPISODE_NOT_FOUND", "当前项目中没有这个分集。")
    else:
        raise AppError("INVALID_ARGUMENTS", "scope 必须是 storyboard 或 episode。")
    context = client_context(
        service,
        session_id=session_id,
        client_request_id=client_request_id,
        group="subtitle_save",
        require_mode_config=False,
    )
    result = service.store.operation(
        _operation_key(context.run_id, "subtitle_save"),
        "subtitle_save",
        args,
        lambda draft: save_subtitles(args, draft, session_id),
    )
    if not result.get("ok"):
        raise AppError(result["error"]["code"], result["error"]["message"])
    data = result["data"]
    return _result(service, data, context.run_id, data.get("storyboardId"))


def save_season(
    service,
    *,
    session_id: str,
    client_request_id: str,
    title: str,
    episode_ids,
    season_id: str | None = None,
    expected_revision: int | None = None,
) -> dict:
    """Save one season's ordered members as an explicit local action.

    Seasons never register Jobs, so this is a pure metadata transaction: it does
    not need a video mode, a provider Key or a model, and replaying one request ID
    returns the recorded season version instead of appending another.
    """
    from vagent.video.seasons import save_season

    args = {
        "title": title,
        "episodeIds": list(episode_ids),
        **({"seasonId": season_id} if season_id is not None else {}),
        **({"expectedRevision": expected_revision} if expected_revision is not None else {}),
    }
    replayed = _replay_or_conflict(service, session_id, client_request_id, "season_save", args)
    if replayed is not None:
        return replayed
    context = client_context(
        service,
        session_id=session_id,
        client_request_id=client_request_id,
        group="season_save",
        require_mode_config=False,
        metadata_only=True,
    )
    result = service.store.operation(
        _operation_key(context.run_id, "season_save"),
        "season_save",
        args,
        lambda draft: save_season(args, draft, session_id),
    )
    if not result.get("ok"):
        raise AppError(result["error"]["code"], result["error"]["message"])
    data = result["data"]
    return _result(service, data, context.run_id)


async def film_render(
    service,
    *,
    session_id: str,
    client_request_id: str,
    request: dict,
) -> dict:
    """Render delivered shots as one explicit local action; no provider or Job."""
    from vagent.video.renders import FilmRenderArgs

    args = {key: value for key, value in request.items() if key != "clientRequestId"}
    args["confirm"] = True
    model = FilmRenderArgs.model_validate(args)
    args = model.model_dump(mode="json", by_alias=True)
    replayed = _replay_or_conflict(service, session_id, client_request_id, "film_render", args)
    if replayed is not None:
        return replayed
    if model.scope == "storyboard":
        target = service.store.snapshot()["storyboards"].get(model.storyboard_id)
        if target is None or target["projectId"] != session_id:
            raise AppError("NOT_FOUND", "当前项目中没有这个分镜。")
    else:
        target = service.store.snapshot()["episodes"].get(model.episode_id)
        if target is None or target["projectId"] != session_id:
            raise AppError("EPISODE_NOT_FOUND", "当前项目中没有这个分集。")
    context = client_context(
        service,
        session_id=session_id,
        client_request_id=client_request_id,
        group="film_render",
        require_mode_config=False,
    )
    result = await service.renders.render(
        args,
        project_id=session_id,
        operation_key=_operation_key(context.run_id, "film_render"),
        args=args,
    )
    if not result.get("ok"):
        raise AppError(result["error"]["code"], result["error"]["message"])
    data = result["data"]
    return _result(service, data, context.run_id, data.get("storyboardId"))


async def export_subtitles(
    service,
    *,
    session_id: str,
    client_request_id: str,
    track_id: str,
    mode: str,
    profile: str = "720p-h264-aac-v1",
    track_version: int | None = None,
) -> dict:
    """Render soft/burn subtitles onto the current film as one local action."""
    args = {
        "trackId": track_id,
        "mode": mode,
        "profile": profile,
        **({"trackVersion": track_version} if track_version is not None else {}),
    }
    replayed = _replay_or_conflict(service, session_id, client_request_id, "subtitle_export", args)
    if replayed is not None:
        return replayed
    raw = service.store.snapshot()["subtitleTracks"].get(track_id)
    if raw is None or raw["projectId"] != session_id:
        raise AppError("SUBTITLE_TRACK_MISSING", "当前项目中没有这个字幕轨。")
    context = client_context(
        service,
        session_id=session_id,
        client_request_id=client_request_id,
        group="subtitle_export",
        require_mode_config=False,
    )
    result = await service.subtitles.export(
        project_id=session_id,
        track_id=track_id,
        mode=mode,
        profile_name=profile,
        track_version=track_version,
        operation_key=_operation_key(context.run_id, "subtitle_export"),
        args=args,
    )
    if not result.get("ok"):
        raise AppError(result["error"]["code"], result["error"]["message"])
    return _result(service, result["data"], context.run_id, result["data"].get("storyboardId"))
