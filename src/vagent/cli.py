import argparse
import asyncio
import json
import re
import sys
from contextlib import AsyncExitStack
from dataclasses import asdict, replace
from pathlib import Path
from uuid import uuid4

from dotenv import load_dotenv

from vagent import __version__
from vagent import headless as headless_service
from vagent.application import ApplicationService
from vagent.config import assert_id, load_config
from vagent.console import read_prompt
from vagent.errors import AppError, public_error
from vagent.models import DemoModel
from vagent.skills import discover_for
from vagent.storage import FileStore
from vagent.tools import create_project_tools
from vagent.usage import summarize_usage
from vagent.video import clients
from vagent.video.adaptation import plan_episode
from vagent.video.assets import asset_reference_loader
from vagent.video.contracts import ImageInputRule
from vagent.video.episodes import read_episode
from vagent.video.film_service import FilmService
from vagent.video.jobs import JobService
from vagent.video.media import DownloadRetry
from vagent.video.providers.fal import FalAdapter
from vagent.video.providers.kling import KlingAdapter
from vagent.video.providers.mock import MockVideoAdapter
from vagent.video.providers.wan import WanAdapter
from vagent.video.storyboards import find_shot, storyboard_in_state
from vagent.video.tools import register_video_tools
from vagent.video.usage import summarize_records
from vagent.video.views import cli_job_view, current_storyboard_view, storyboard_view


def print_json(value: object) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2, default=str))


def show_event(event: dict) -> None:
    if event["type"] in {"run.started", "run.resumed"}:
        print(f"[{'恢复' if event['type'] == 'run.resumed' else '运行'}] {event['runId']}", flush=True)
    elif event["type"] == "context.prepared" and event["droppedMessages"]:
        print(
            f"[上下文] 裁剪 {event['droppedMessages']} 条历史消息，输入 {event['inputBytes']} 字节；原始记录仍保留",
            flush=True,
        )
    elif event["type"] == "model.started":
        print(f"[模型步骤 {event['step']}]", flush=True)
    elif event["type"] == "answer_cache":
        print(f"[Redis 回答缓存] {event['status']}", flush=True)
    elif event["type"] == "tool.started":
        print(f"[工具] {event['name']}", flush=True)
    elif event["type"] == "tool.completed":
        print(f"[工具结果] {event['name']}: {'成功' if event['ok'] else '失败'}", flush=True)
    elif event["type"] == "run.waiting":
        print("[等待] Agent 已挂起，正在等待外部结果；Ctrl+C 停止 Agent，Job 保留。", flush=True)
    elif event["type"] == "run.resume_blocked":
        print(f"[等待恢复受阻] {event['error']['code']} · {event['error']['message']}", flush=True)
    elif event["type"] == "job.updated":
        job = event["job"]
        if job["mode"] == "live":
            availability = "本地媒体可用" if job["mediaAvailable"] else "尚无本地媒体"
            print(
                f"[真实 Job] {job['jobId']} · {job['status']} / {job['queryState']} · {availability}",
                flush=True,
            )
            if job.get("runtimeBlock"):
                print(
                    f"[任务受阻] {job['runtimeBlock']['code']} · {job['runtimeBlock']['message']}", flush=True
                )
        else:
            print(
                f"[模拟 Job] {job['jobId']} · {job['status']} / {job['queryState']} · 无真实媒体", flush=True
            )


class EventDisplay:
    def __init__(self):
        self.text = ""
        self.final_text = None
        self.pending_events = []

    def __call__(self, event):
        kind = event["type"]
        if kind == "job.updated" and self.text:
            self.pending_events.append(event)
            return
        if kind == "assistant.delta":
            if not self.text:
                print("\nAgent > ", end="", flush=True)
            self.text += event["text"]
            print(event["text"], end="", flush=True)
            return
        if kind in {"model.completed", "assistant.discarded", "run.completed"} and self.text:
            print(flush=True)
            if kind == "assistant.discarded":
                print("[草稿未完成，已丢弃]", flush=True)
            self.final_text = self.text if kind == "model.completed" and event.get("final") else None
            self.text = ""
            for pending in self.pending_events:
                show_event(pending)
            self.pending_events.clear()
        show_event(event)

    def finish(self, result, *, chat=False):
        if self.final_text != result["answer"]:
            print(f"\n{'Agent > ' if chat else ''}{result['answer']}")
        suffix = (
            ""
            if chat
            else f" Run {result['id']}；模型 {result['modelSteps']} 步，工具 {result['toolCalls']} 次"
        )
        print(f"[{result['status']}]{suffix}")


async def run_agent(args: argparse.Namespace) -> int:
    config = load_config()
    async with AsyncExitStack() as stack:
        store = stack.enter_context(FileStore.open(config.home))
        saved = None
        if args.command == "resume":
            saved = store.snapshot()["runs"].get(args.run_id)
            if saved is None:
                raise AppError("NOT_FOUND", "没有这个 Run，请使用 inspect 查看运行 ID。")
        offline = args.command == "demo" or (saved and saved["model"] == DemoModel.name)
        model = DemoModel() if offline else None
        read_only = saved.get("readOnly", False) if saved else getattr(args, "read_only", False)
        runtime_config = replace(config, redis_url=None) if offline else config
        service = await stack.enter_async_context(
            ApplicationService.open(runtime_config, model=model, existing_store=store)
        )
        session_id = saved["sessionId"] if saved else args.session
        print(f"模型：{model.name if model else config.model}\n会话：{session_id}\n数据：{config.home}")
        try:
            if args.command == "chat":
                if not sys.stdin.isatty():
                    raise AppError("TTY_REQUIRED", "交互模式需要终端；自动化调用请使用 run。")
                while True:
                    try:
                        prompt = await read_prompt("\n你 > ")
                    except EOFError:
                        return 0
                    except asyncio.CancelledError:
                        for record in service.store.snapshot()["runs"].values():
                            if record["status"] in {"running", "waiting_external"}:
                                service.stop(record["id"])
                        return 130
                    if prompt.strip() == "/exit":
                        return 0
                    if not prompt.strip():
                        continue
                    display = service.on_event = EventDisplay()
                    result = await execute_cli(service, session_id, prompt=prompt, read_only=read_only)
                    display.finish(result, chat=True)
                    show_usage(result)
                    show_resume_hint(result)
                    if result["status"] == "cancelled":
                        return 130
                    if result.get("waitResumeError"):
                        return 1
            display = service.on_event = EventDisplay()
            if args.command == "resume":
                result = await execute_cli(service, session_id, resume_id=args.run_id)
            else:
                prompt = "演示读取项目和保存咖啡店方案。" if args.command == "demo" else args.prompt
                result = await execute_cli(
                    service,
                    session_id,
                    prompt=prompt,
                    request_id=getattr(args, "request_id", None),
                    read_only=read_only,
                )
            display.finish(result)
            show_usage(result)
            show_resume_hint(result)
            return 0 if result["status"] == "completed" else 130 if result["status"] == "cancelled" else 1
        finally:
            show_job_exit_hint(service)


async def execute_cli(service, session_id, *, prompt=None, request_id=None, read_only=False, resume_id=None):
    run_id = resume_id
    try:
        result = (
            await service.resume(resume_id)
            if resume_id
            else await service.start(session_id, prompt, request_id or str(uuid4()), read_only=read_only)
        )
        run_id = result["id"]
        return await service.wait_for_run(run_id)
    except asyncio.CancelledError:
        # Ctrl+C is a user stop for run/chat/resume, including a persisted wait.
        # Normal application closure (and jobs work) preserves auto-resume intent.
        run_id = run_id or service.active_run_id
        if run_id is None:
            raise
        service.stop(run_id)
        if service.task and not service.task.done():
            await asyncio.shield(service.task)
        return service.run_record(run_id)


def show_job_exit_hint(service):
    if any(
        job["status"] in {"pending_submit", "submitting", "queued", "running", "downloading"}
        or (job.get("download") or {}).get("phase") in {"pending", "writing", "prepared"}
        for job in service.jobs()
    ):
        print("[Job] 未结束的任务已保存。CLI 退出后停止推进；使用 vagent web 或 vagent jobs work 继续。")


async def work_jobs(config):
    async with ApplicationService.open(config, on_event=EventDisplay()) as service:
        print(
            f"Job 工作循环已启动 · 数据：{config.home}\nCtrl+C 退出；保留 Job 和 Agent 的等待意图。",
            flush=True,
        )
        try:
            await service.work()
        except asyncio.CancelledError:
            return 130
        finally:
            show_job_exit_hint(service)


def local_jobs(config, args):
    # Status/retry commands never make HTTP calls or start background workers.
    # Live retry validates the original adapter/configuration without querying it.
    with FileStore.open(config.home) as store:
        service = JobService(store, [])
        if args.action == "list":
            if args.session is not None:
                assert_id(args.session)
            print_json(
                {
                    "jobs": [
                        cli_job_view(service.media.refresh(job))
                        for job in reversed(service.list(session_id=args.session))
                    ]
                }
            )
        else:
            assert_id(args.job_id)
            job = service.media.refresh(service.get(args.job_id))
            if args.action == "retry-query":
                job = (
                    asyncio.run(retry_live_query(config, store, job))
                    if job.mode == "live"
                    else service.retry_query(job.id, project_id=job.context.project_id)
                )
            if args.action == "retry-download":
                service.media.retry_download(
                    job.id,
                    DownloadRetry(
                        client_request_id=str(uuid4()),
                        expected_revision=job.revision,
                    ),
                    project_id=job.context.project_id,
                )
                job = service.get(job.id)
            print_json(cli_job_view(job))


async def live_adapters(config, store, stack):
    """Mock plus every published live provider; missing Keys stay unusable.

    Inspection and retry commands must find the original adapter even when the
    current mode is off, so the missing-Key/configuration diagnostics stay exact.
    """
    adapters = [MockVideoAdapter(store)]
    adapters.append(
        await stack.enter_async_context(
            WanAdapter(
                api_key=config.video_api_key,
                workspace_id=config.video_workspace_id,
                model=config.video_model,
                region=config.video_region,
                reference_loader=asset_reference_loader(store, ImageInputRule()),
            )
        )
    )
    adapters.append(await stack.enter_async_context(KlingAdapter(api_key=config.kling_api_key)))
    adapters.append(await stack.enter_async_context(FalAdapter(api_key=config.fal_api_key)))
    return adapters


async def retry_live_query(config, store, job):
    async with AsyncExitStack() as stack:
        service = JobService(store, await live_adapters(config, store, stack), config=config)
        return service.retry_query(job.id, project_id=job.context.project_id)


def show_resume_hint(result: dict) -> None:
    if result.get("waitResumeError"):
        error = result["waitResumeError"]
        print(f"[恢复受阻] {error['code']} · {error['message']}")
    if result.get("resumable"):
        print(f"可在剩余预算内继续：vagent resume {result['id']}")


def show_usage(result: dict) -> None:
    usage = summarize_usage(result)
    cache = "缓存用量未知"
    if usage["cacheHitTokens"] is not None or usage["cacheMissTokens"] is not None:
        rate = f"{usage['cacheHitRate']:.1%}" if usage["cacheHitRate"] is not None else "不适用"
        hits = usage["cacheHitTokens"] if usage["cacheHitTokens"] is not None else "未知"
        misses = usage["cacheMissTokens"] if usage["cacheMissTokens"] is not None else "未知"
        cache = f"已知缓存命中 {hits}、未命中 {misses}，完整记录命中率 {rate}"
    suffix = ""
    if not usage["tokenUsageComplete"] or not usage["cacheUsageComplete"]:
        suffix = "；部分请求用量未知"
    if usage["untrackedModelSteps"]:
        suffix += f"；另有 {usage['untrackedModelSteps']} 个旧模型步无调用明细"
    print(
        f"[用量] 已记录模型调用 {usage['recordedCallCount']} 次；"
        f"已知输入 {usage['observedInputTokens']}、输出 {usage['observedOutputTokens']} Token；{cache}{suffix}"
    )
    if usage["answerCache"]:
        print(f"[Redis 回答缓存] {json.dumps(usage['answerCache'], ensure_ascii=False)}")


def local_storyboard(state, args) -> dict:
    """Read one project's storyboard without starting a model, Worker or MCP server."""
    assert_id(args.project)
    if args.storyboard_id:
        assert_id(args.storyboard_id)
        view = storyboard_view(state, storyboard_in_state(state, args.project, args.storyboard_id))
    else:
        view = current_storyboard_view(state, args.project)
        if view is None:
            raise AppError("NOT_FOUND", "当前项目还没有结构化分镜，请先保存。")
    from vagent.video.subtitles import subtitle_for

    view["subtitle"] = subtitle_for(
        state, args.project, storyboard_id=view["storyboardId"], include_cues=False, include_digests=False
    )
    return view


def _episode_text(path: str) -> str:
    """Read one plain-text script; the tool contract owns length and splitting rules."""
    try:
        return Path(path).read_text(encoding="utf-8")
    except OSError:
        raise AppError("NOT_FOUND", "没有找到这个剧本文本文件。") from None


def _bible_character_ids(state, project_id: str, names: list[str], overrides: dict[str, str]) -> list[str]:
    """Resolve plan character names to this project's character bible, in plan order."""
    resolved = []
    for name in names:
        if name in overrides:
            character_id = overrides[name]
        else:
            matches = [
                raw["id"]
                for raw in state["characters"].values()
                if raw["projectId"] == project_id
                and (
                    raw["versions"][-1]["name"] == name or name in (raw["versions"][-1].get("aliases") or [])
                )
            ]
            if len(matches) != 1:
                continue
            character_id = matches[0]
        if character_id not in resolved:
            resolved.append(character_id)
    return resolved


def _episode_scenes(plan: dict, state, project_id: str, overrides: dict[str, str]) -> list[dict]:
    scenes = []
    for scene in plan["scenes"]:
        scenes.append(
            {
                "heading": scene["heading"],
                "location": scene["location"],
                "timeOfDay": scene["timeOfDay"],
                "synopsis": scene["synopsis"],
                "characterIds": _bible_character_ids(state, project_id, scene["characterNames"], overrides),
                "dialogue": [
                    {"text": item["text"], "speaker": item.get("speaker"), "paragraph": item["paragraph"]}
                    for item in scene["dialogue"]
                ],
                "shotCount": scene["shotCount"],
                "durationSeconds": scene["durationSeconds"],
            }
        )
    return scenes


def local_episode_read(state, args, films=None) -> dict:
    """Read the episode view without a model, Worker or provider call."""
    assert_id(args.project)
    if args.episode_id:
        assert_id(args.episode_id)
    view = read_episode({"episodeId": args.episode_id}, state, args.project)
    if films is not None:
        for scene in view["scenes"]:
            storyboard_id = scene.get("storyboardId")
            scene["film"] = films.view(args.project, storyboard_id)["film"] if storyboard_id else None
    if view.get("episodeId"):
        from vagent.video.subtitles import subtitle_for

        view["subtitle"] = subtitle_for(
            state,
            args.project,
            episode_id=view["episodeId"],
            include_cues=False,
            include_digests=False,
        )
    return view


def local_episode_plan(args) -> dict:
    """Deterministic text adaptation only: no Store write, no Job, no model call."""
    plan = plan_episode(_episode_text(args.text), title=args.title)
    return plan


def local_episode_save(config, store, args) -> int:
    """Save one episode draft as an explicit local action; registers nothing."""
    assert_id(args.project)
    store.ensure_session(args.project)
    overrides = {}
    for value in args.character:
        name, separator, character_id = value.partition("=")
        if not separator or not name or not character_id:
            raise AppError("INVALID_ARGUMENTS", "--character 需要“姓名=角色ID”的形式。")
        assert_id(character_id)
        overrides[name] = character_id
    plan = plan_episode(_episode_text(args.text), title=args.title)
    episode_args = {
        "title": args.title or plan["title"],
        "scenes": _episode_scenes(plan, store.snapshot(), args.project, overrides),
    }
    if args.style:
        assert_id(args.style)
        episode_args["styleId"] = args.style
    if args.request_id is None:
        args.request_id = str(uuid4())
    registry = register_video_tools(
        create_project_tools(), JobService(store, [MockVideoAdapter(store)], config=config)
    )
    result = registry.execute(
        "episode_save",
        episode_args,
        store=store,
        project_id=args.project,
        operation_key=f"cli-episode-save:{args.request_id}",
    )
    if not result.get("ok"):
        raise AppError(result["error"]["code"], result["error"]["message"])
    print_json(result["data"])
    return 0


async def local_episode_generate(config, store, args) -> int:
    """Register one whole episode as an explicit local action; submits nothing."""
    assert_id(args.project)
    assert_id(args.episode_id)
    if not args.confirm:
        raise AppError("CONFIRMATION_REQUIRED", "整集登记需要显式 --confirm。")
    async with AsyncExitStack() as stack:
        adapters = await live_adapters(config, store, stack)
        service = JobService(store, adapters, config=config)
        result = clients.generate_episode(
            service,
            session_id=args.project,
            client_request_id=args.request_id or str(uuid4()),
            episode_id=args.episode_id,
            confirm=True,
        )
    print_json(result)
    return 0


def local_assets(config, store, args) -> int:
    """Library access never needs an adapter, a model or a provider call."""
    service = JobService(store, [], config=config)
    if args.action == "list":
        if args.project is not None:
            assert_id(args.project)
        print_json({"assets": service.assets.list(project_id=args.project)})
        return 0
    assert_id(args.project)
    store.ensure_session(args.project)
    imported, failed = [], []
    for path in args.paths:
        try:
            imported.append(service.assets.import_path(path, project_id=args.project))
        except AppError as error:
            failed.append({"path": path, "error": {"code": error.code, "message": str(error)}})
    print_json({"projectId": args.project, "imported": imported, "failed": failed})
    return 1 if failed else 0


def local_usage(state, selector: str) -> dict:
    if selector == "all":
        return summarize_records(state)
    assert_id(selector)
    if selector not in state["projects"]:
        raise AppError("NOT_FOUND", "当前数据目录中没有这个项目。")
    return summarize_records(state, project_id=selector)


async def local_redo(config, store, args) -> int:
    """Register one new ShotVersion and Job for a named shot; submits nothing."""
    async with AsyncExitStack() as stack:
        adapters = await live_adapters(config, store, stack)
        service = JobService(store, adapters, config=config)
        record, shot = find_shot(store.snapshot(), args.shot_id)
        if args.storyboard_id and record.id != args.storyboard_id:
            raise AppError("NOT_FOUND", "这个镜头不属于指定的分镜。")
        if args.project and record.project_id != args.project:
            raise AppError("NOT_FOUND", "这个镜头不属于指定的项目。")
        result = clients.redo_shot(
            service,
            session_id=record.project_id,
            client_request_id=args.request_id or str(uuid4()),
            storyboard_id=record.id,
            storyboard_version=record.current_version,
            shot_id=shot.id,
            expected_revision=None,
        )
    print_json(result)
    return 0


class LocalActionHost:
    """The ApplicationService surface a local client action needs, without Workers."""

    def __init__(self, store, films, config, subtitles=None):
        self.store, self.films, self.config = store, films, config
        self.subtitles = subtitles

    def generation_mode(self):
        return self.config.video_mode

    def capabilities(self, mode):
        if mode not in {"mock", "live"}:
            return []
        try:
            return self.films.jobs.capabilities(mode)
        except AppError:
            # A purely local film action records provenance without a provider.
            return []


async def local_film(config, store, args) -> int:
    """Local film read/assembly; never starts a model, Worker or provider submission."""
    assert_id(args.project)
    service = FilmService(JobService(store, [MockVideoAdapter(store)], config=config))
    storyboard_id = args.storyboard_id
    if args.action == "show":
        if storyboard_id is None:
            view = current_storyboard_view(store.snapshot(), args.project)
            if view is None:
                raise AppError("NOT_FOUND", "当前项目还没有结构化分镜，请先保存。")
            storyboard_id = view["storyboardId"]
        assert_id(storyboard_id)
        print_json(service.view(args.project, storyboard_id))
        return 0
    if not storyboard_id:
        raise AppError("INVALID_ARGUMENTS", "缺少 --storyboard，未拼接成片。")
    assert_id(storyboard_id)
    if args.expected_revision < 0:
        raise AppError("INVALID_ARGUMENTS", "--expected-revision 必须是不小于 0 的整数。")
    result = await clients.assemble_film(
        LocalActionHost(store, service, config),
        session_id=args.project,
        client_request_id=args.request_id or str(uuid4()),
        storyboard_id=storyboard_id,
        expected_revision=args.expected_revision,
    )
    print_json(result)
    return 0


async def local_subtitle(config, store, args) -> int:
    """Local subtitle generate/save/export; zero provider calls and no Job."""
    assert_id(args.project)
    films = FilmService(JobService(store, [MockVideoAdapter(store)], config=config))
    host = LocalActionHost(store, films, config, films.jobs.subtitles)
    if args.action == "show":
        for name in ("storyboard_id", "episode_id", "track_id"):
            value = getattr(args, name, None)
            if value:
                assert_id(value)
        print_json(
            host.subtitles.view(
                args.project,
                storyboard_id=args.storyboard_id,
                episode_id=args.episode_id,
                track_id=args.track_id,
            )
        )
        return 0
    request_id = args.request_id or str(uuid4())
    if args.action == "save":
        cues = None
        if args.cues:
            path = Path(args.cues)
            try:
                cues = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                raise AppError("INVALID_ARGUMENTS", "字幕修正文件必须是 UTF-8 JSON 数组。") from None
            if not isinstance(cues, list):
                raise AppError("INVALID_ARGUMENTS", "字幕修正文件必须是 JSON 数组。")
        scope = "storyboard" if args.storyboard_id else "episode"
        result = clients.save_subtitles(
            host,
            session_id=args.project,
            client_request_id=request_id,
            scope=scope,
            storyboard_id=args.storyboard_id,
            episode_id=args.episode_id,
            cues=cues,
            track_id=args.track_id,
            expected_revision=args.expected_revision,
        )
        print_json(result)
        return 0
    assert_id(args.track_id)
    result = await clients.export_subtitles(
        host,
        session_id=args.project,
        client_request_id=request_id,
        track_id=args.track_id,
        mode=args.mode,
        profile=args.profile,
        track_version=args.version,
    )
    print_json(result)
    return 0


def headless_mode(args) -> str:
    """Validate the frozen headless argument combinations before opening services."""
    plan_mode = args.plan_run is not None or args.plan_fingerprint is not None
    resume_mode = args.resume_run is not None
    if plan_mode and resume_mode:
        raise AppError("INVALID_ARGUMENTS", "--plan 与 --resume 不能同时使用。")
    if plan_mode:
        if not args.plan_run or not args.plan_fingerprint:
            raise AppError("INVALID_ARGUMENTS", "授权需要同时提供 --plan RUN_ID 与 --plan-fingerprint SHA。")
        if args.prompt is not None or args.request_id is not None:
            raise AppError("INVALID_ARGUMENTS", "授权模式不接受新的需求或请求 ID。")
        if not re.fullmatch(r"[0-9a-f]{64}", args.plan_fingerprint):
            raise AppError("INVALID_ARGUMENTS", "计划指纹必须是 64 位小写十六进制字符串。")
        return "plan"
    if resume_mode:
        if (
            args.prompt is not None
            or args.confirm
            or args.max_cost
            or args.max_units
            or args.max_jobs is not None
        ):
            raise AppError("INVALID_ARGUMENTS", "--resume 不能同时给出需求或新的授权参数。")
        return "resume"
    if not args.prompt or not args.prompt.strip():
        raise AppError("INVALID_ARGUMENTS", "缺少需求；headless 需要 PROMPT、--plan 或 --resume。")
    if args.confirm or args.max_cost or args.max_units or args.max_jobs is not None:
        raise AppError(
            "HEADLESS_AUTHORIZATION_REQUIRED",
            "不能在尚未生成计划的 PROMPT 上预先授权；先运行 vagent headless PROMPT。",
        )
    return "draft"


def headless_events(json_mode: bool):
    """Progress goes to stderr in JSON mode so stdout stays one object."""
    if not json_mode:
        return EventDisplay()

    def publish(event):
        if event["type"] in {"run.started", "run.resumed"}:
            print(f"[运行] {event['runId']}", file=sys.stderr, flush=True)
        elif event["type"] == "tool.completed":
            status = "成功" if event["ok"] else f"失败 {event.get('errorCode') or ''}".strip()
            print(f"[工具] {event['name']}: {status}", file=sys.stderr, flush=True)
        elif event["type"] == "run.waiting":
            print("[等待] Agent 已挂起，等待外部结果。", file=sys.stderr, flush=True)
        elif event["type"] == "headless.registered":
            print(f"[登记] {len(event['jobIds'])} 个镜头任务", file=sys.stderr, flush=True)
        elif event["type"] == "headless.waiting":
            print("[等待] 正在等待镜头任务与本地媒体交付。", file=sys.stderr, flush=True)
        elif event["type"] == "run.completed":
            print(f"[结束] {event['status']}", file=sys.stderr, flush=True)

    return publish


def emit_headless(result: dict, json_mode: bool) -> None:
    if json_mode:
        print(json.dumps(result, ensure_ascii=False, default=str))
        return
    error = result.get("error")
    if error:
        print(f"[{error['code']}] {error['message']}", file=sys.stderr)
    status = result["status"]
    if status == "planned":
        print(
            f"[计划] Run {result['runId']} · 分镜 {result['storyboardId']} "
            f"v{result['storyboardVersion']} · revision {result['storyboardRevision']}"
        )
        for item in result["estimates"]:
            units = f" · {item['units']} VIDEO_UNIT" if item["units"] is not None else ""
            print(
                f"  预计 {item['provider']} {item['currency']} {item['amount']} · "
                f"{item['jobCount']} 个镜头{units}"
            )
        print(f"[指纹] {result['planFingerprint']}")
        print("[下一步] 核对镜头与预算后显式授权：")
        print(headless_service.authorization_hint(result))
    elif status == "delivered" and result["output"]:
        print(f"[已交付] 成片 v{result['filmVersion']} · {result['output']['sizeBytes']} 字节")
        print(f"[SHA-256] {result['output']['sha256']}")
        print(f"[文件] {result['output']['path']}")
    elif status in {"waiting", "failed", "authorized"}:
        if result["jobIds"]:
            print(f"[任务] {len(result['jobIds'])} 个已登记：" + "、".join(result["jobIds"]))
        if status == "waiting":
            print(f"[等待中] Run {result['runId']} · {len(result['jobIds'])} 个任务已保留")
        if result["runId"]:
            print(f"[继续] vagent headless --resume {result['runId']}")
    elif status == "cancelled":
        if result["runId"]:
            print(f"[继续] vagent headless --resume {result['runId']}")


def headless_failure(store, project: str, run_id: str | None, error: AppError) -> dict:
    result = headless_service.blank_result(project)
    if run_id and store is not None:
        try:
            result = headless_service.result_for_run(store, run_id, project)
        except AppError:
            pass
    result["status"] = (
        "cancelled"
        if error.code == "CANCELLED"
        else "waiting"
        if error.code == "HEADLESS_WAIT_TIMEOUT"
        else "failed"
    )
    result["error"] = {"code": error.code, "message": str(error)}
    return result


async def headless_command(args) -> int:
    project = args.project
    run_id = args.plan_run or args.resume_run
    result = headless_service.blank_result(project)
    try:
        mode = headless_mode(args)
        config = load_config()
        async with ApplicationService.open(config) as service:
            try:
                if mode == "draft":
                    result = await headless_service.plan(
                        service,
                        session_id=project,
                        prompt=args.prompt,
                        request_id=args.request_id,
                        on_event=headless_events(args.json),
                    )
                else:
                    run_id = args.plan_run or args.resume_run
                    if mode == "plan":
                        result = headless_service.authorize(
                            service,
                            run_id=run_id,
                            plan_fingerprint=args.plan_fingerprint,
                            confirm=args.confirm,
                            max_cost=args.max_cost,
                            max_units=args.max_units,
                            max_jobs=args.max_jobs,
                        )
                    result = await headless_service.execute(
                        service, run_id=run_id, on_event=headless_events(args.json)
                    )
                code = 0
            except (AppError, asyncio.CancelledError) as error:
                if isinstance(error, asyncio.CancelledError):
                    error = AppError(
                        "CANCELLED", "headless 已中断；计划与已登记任务已保留，可用 --resume 继续。"
                    )
                result = headless_failure(service.store, project, run_id, error)
                code = headless_service.exit_code(error)
    except AppError as error:
        result = headless_failure(None, project, run_id, error)
        result["status"] = "failed" if error.code != "CANCELLED" else "cancelled"
        code = headless_service.exit_code(error)
    emit_headless(result, args.json)
    return code


def headless_main(args) -> int:
    try:
        return asyncio.run(headless_command(args))
    except (KeyboardInterrupt, asyncio.CancelledError):
        result = headless_service.blank_result(args.project)
        result["status"] = "cancelled"
        result["error"] = {"code": "CANCELLED", "message": "headless 已中断，未完成的动作未标记成功。"}
        emit_headless(result, args.json)
        return 130


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(prog="vagent", description="DeepSeek + LangGraph 视频创作 Agent 原型")
    root.add_argument("--version", action="version", version=__version__)
    commands = root.add_subparsers(dest="command", required=True)
    run = commands.add_parser("run", help="执行真实 DeepSeek 任务，会产生 API 费用")
    run.add_argument("prompt")
    run.add_argument("--request-id")
    resume = commands.add_parser("resume", help="从持久检查点继续原 Run，沿用剩余预算")
    resume.add_argument("run_id")
    chat = commands.add_parser("chat", help="持续对话；/exit 退出，Ctrl+C 停止")
    for command in (run, chat):
        command.add_argument(
            "--read-only", action="store_true", help="仅提供读取工具；配置 Redis 后启用回答缓存"
        )
    demo = commands.add_parser("demo", help="无需 Key 的离线模拟演示")
    inspect = commands.add_parser("inspect", help="查看本地项目、产物与运行记录")
    usage = commands.add_parser("usage", help="查看模型调用和缓存 Token 用量，不调用模型")
    selection = usage.add_mutually_exclusive_group()
    selection.add_argument("-s", "--session")
    selection.add_argument("--run", dest="run_id")
    selection.add_argument(
        "--project",
        nargs="?",
        const="all",
        default=None,
        metavar="PROJECT",
        help="按项目（省略取值时汇总整个数据目录）聚合 Job、估算与 Token 用量",
    )
    for command in (run, chat, inspect):
        command.add_argument("-s", "--session", default="default")
    demo.add_argument("-s", "--session", default="demo")
    commands.add_parser("config").add_subparsers(dest="action", required=True).add_parser("show")
    commands.add_parser("skills").add_subparsers(dest="action", required=True).add_parser("list")
    jobs = commands.add_parser("jobs", help="查看本地视频任务、恢复查询或运行持久队列")
    actions = jobs.add_subparsers(dest="action", required=True)
    listing = actions.add_parser("list", help="列出本地任务，不调用模型或供应商")
    listing.add_argument("-s", "--session")
    actions.add_parser("get", help="查看一个本地任务").add_argument("job_id")
    actions.add_parser("retry-query", help="恢复已暂停的原任务查询，不重新提交").add_argument("job_id")
    actions.add_parser("retry-download", help="恢复原媒体下载，复用任务和媒体 ID").add_argument("job_id")
    actions.add_parser("work", help="持续推进任务和有效等待；Ctrl+C 退出并保留状态")
    storyboard = commands.add_parser("storyboard", help="查看结构化分镜，不调用模型")
    storyboard_actions = storyboard.add_subparsers(dest="action", required=True)
    show = storyboard_actions.add_parser("show", help="读取当前项目的分镜、镜头与各镜头版本摘要")
    show.add_argument("-s", "--project", default="default")
    show.add_argument("--storyboard", dest="storyboard_id", help="指定分镜；省略时读取最新分镜")
    shots = commands.add_parser("shots", help="逐镜头操作；只登记本地任务，不提交上游")
    shot_actions = shots.add_subparsers(dest="action", required=True)
    redo = shot_actions.add_parser("redo", help="重做一个镜头：新增 ShotVersion 并登记一个新任务")
    redo.add_argument("shot_id")
    redo.add_argument("-s", "--project", help="核对镜头所属项目；省略时按镜头 ID 解析")
    redo.add_argument("--storyboard", dest="storyboard_id", help="核对镜头所属分镜")
    redo.add_argument("--request-id", dest="request_id", help="同一请求 ID 重放原结果，不重复登记")
    assets = commands.add_parser("assets", help="素材库：列出或导入参考图，不调用模型")
    asset_actions = assets.add_subparsers(dest="action", required=True)
    asset_listing = asset_actions.add_parser("list", help="列出素材元数据；省略项目时列出整个素材库")
    asset_listing.add_argument("-s", "--project")
    importer = asset_actions.add_parser("import", help="导入一张或多张本地图片，按哈希去重")
    importer.add_argument("paths", nargs="+")
    importer.add_argument("-s", "--project", default="default")
    film = commands.add_parser("film", help="本地拼接成片：组装已交付镜头，不调用供应商、不产生费用")
    film_actions = film.add_subparsers(dest="action", required=True)
    film_assemble = film_actions.add_parser("assemble", help="按分镜顺序本地拼接当前版本的成片")
    film_assemble.add_argument("-s", "--project", default="default")
    film_assemble.add_argument("--storyboard", dest="storyboard_id", required=True)
    film_assemble.add_argument(
        "--expected-revision", dest="expected_revision", type=int, required=True, help="分镜的当前 revision"
    )
    film_assemble.add_argument("--request-id", dest="request_id", help="同一请求 ID 重放原结果，不重复拼接")
    film_show = film_actions.add_parser("show", help="读取当前成片、历史版本与受控媒体地址")
    film_show.add_argument("-s", "--project", default="default")
    film_show.add_argument("--storyboard", dest="storyboard_id", help="指定分镜；省略时读取最新分镜")
    episode = commands.add_parser("episode", help="分集：离线改编、保存、查看与整批登记；不调用模型")
    episode_actions = episode.add_subparsers(dest="action", required=True)
    episode_plan = episode_actions.add_parser("plan", help="离线切分剧本并返回分集草案；不写存储、不登记任务")
    episode_plan.add_argument("--text", required=True, help="纯文本剧本文件（txt/markdown）")
    episode_plan.add_argument("--title", help="分集标题；省略时使用文件名")
    episode_save = episode_actions.add_parser("save", help="保存改编出的分集；不创建分镜、不登记任务")
    episode_save.add_argument("--text", required=True, help="纯文本剧本文件（txt/markdown）")
    episode_save.add_argument("-s", "--project", default="default")
    episode_save.add_argument("--title", help="分集标题；省略时使用文件名")
    episode_save.add_argument("--style", help="绑定同项目的风格圣经 ID")
    episode_save.add_argument(
        "--character",
        action="append",
        default=[],
        metavar="NAME=ID",
        help="把草案中的角色姓名显式绑定到角色圣经 ID；可重复",
    )
    episode_save.add_argument("--request-id", dest="request_id", help="同一请求 ID 重放原结果")
    episode_show = episode_actions.add_parser("show", help="读取当前分集、场记、对白、生成进度与逐场成片")
    episode_show.add_argument("-s", "--project", default="default")
    episode_show.add_argument("--episode", dest="episode_id", help="指定分集；省略时读取本项目最新分集")
    episode_generate = episode_actions.add_parser(
        "generate", help="为整集已绑定分镜登记任务；只登记，不提交上游"
    )
    episode_generate.add_argument("-s", "--project", default="default")
    episode_generate.add_argument("--episode", dest="episode_id", required=True)
    episode_generate.add_argument("--confirm", action="store_true", help="显式确认整批镜头与集级预算")
    episode_generate.add_argument("--request-id", dest="request_id", help="同一请求 ID 重放原结果")
    subtitle = commands.add_parser("subtitle", help="字幕：确定性对齐、保存修正与本地软/烧录导出")
    subtitle_actions = subtitle.add_subparsers(dest="action", required=True)
    subtitle_show = subtitle_actions.add_parser("show", help="读取当前字幕轨、版本与逐条 cue")
    subtitle_show.add_argument("-s", "--project", default="default")
    subtitle_show.add_argument("--storyboard", dest="storyboard_id", help="指定分镜；省略时读取最新分镜")
    subtitle_show.add_argument("--episode", dest="episode_id", help="改为读取整集字幕轨")
    subtitle_show.add_argument("--track", dest="track_id", help="直接按字幕轨 ID 读取")
    subtitle_save = subtitle_actions.add_parser(
        "save", help="按冻结台词与时间线生成字幕，或用 --cues 提交人工修正"
    )
    subtitle_save.add_argument("-s", "--project", default="default")
    subtitle_target = subtitle_save.add_mutually_exclusive_group(required=True)
    subtitle_target.add_argument("--storyboard", dest="storyboard_id")
    subtitle_target.add_argument("--episode", dest="episode_id")
    subtitle_save.add_argument("--cues", help="人工修正 JSON 数组文件（startMs/endMs/text/speaker）")
    subtitle_save.add_argument("--track", dest="track_id", help="给已有字幕轨追加新版本")
    subtitle_save.add_argument("--expected-revision", dest="expected_revision", type=int)
    subtitle_save.add_argument("--request-id", dest="request_id", help="同一请求 ID 重放原结果")
    subtitle_export = subtitle_actions.add_parser(
        "export", help="把字幕软封装或烧录到当前成片，追加一个新成片版本"
    )
    subtitle_export.add_argument("-s", "--project", default="default")
    subtitle_export.add_argument("--track", dest="track_id", required=True)
    subtitle_export.add_argument("--mode", choices=("soft", "burn", "both"), default="soft")
    subtitle_export.add_argument("--profile", choices=("720p", "1080p"), default="720p")
    subtitle_export.add_argument("--version", type=int, help="指定字幕版本；省略时用当前版本")
    subtitle_export.add_argument("--request-id", dest="request_id", help="同一请求 ID 重放原结果")
    headless = commands.add_parser("headless", help="无 Web 的计划-授权-交付流程；默认只规划，不登记视频任务")
    headless.add_argument("prompt", nargs="?", help="创作需求；只用于第一次规划")
    headless.add_argument("-s", "--project", default="default")
    headless.add_argument("--request-id", dest="request_id", help="同一请求 ID 重放原规划 Run")
    headless.add_argument("--json", action="store_true", help="stdout 只输出一个 JSON 对象")
    headless.add_argument("--plan", dest="plan_run", metavar="RUN_ID", help="授权并执行这个已生成的计划")
    headless.add_argument(
        "--plan-fingerprint", dest="plan_fingerprint", metavar="SHA", help="规划返回的 canonical 计划指纹"
    )
    headless.add_argument("--confirm", action="store_true", help="显式确认这次固定请求集合与各桶预算")
    headless.add_argument(
        "--max-cost",
        dest="max_cost",
        action="append",
        default=[],
        metavar="PROVIDER:CURRENCY=AMOUNT",
        help="每个拟用金额桶的授权上限；可重复",
    )
    headless.add_argument(
        "--max-units",
        dest="max_units",
        action="append",
        default=[],
        metavar="PROVIDER=UNITS",
        help="Kling VIDEO_UNIT 授权上限；可重复",
    )
    headless.add_argument("--max-jobs", dest="max_jobs", type=int, help="收紧本次 Run 的共享新任务上限")
    headless.add_argument("--resume", dest="resume_run", metavar="RUN_ID", help="继续已授权或等待中的 Run")
    web = commands.add_parser("web", help="启动本地 Web 与真实 Agent 编排")
    web.add_argument("--port", type=int, default=3210)
    web.add_argument("--no-open", action="store_true")
    web.add_argument("--mcp-local", action="store_true", help="启用内置只读视频规划 MCP 服务")
    return root


def main() -> None:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    # Explicit cwd avoids dotenv discovering an unrelated installed package's .env.
    load_dotenv(".env", override=False)
    args = parser().parse_args()
    if args.command == "headless":
        raise SystemExit(headless_main(args))
    try:
        config = load_config()
        if args.command == "web":
            import webbrowser

            import uvicorn

            from vagent.web import create_app

            if not 1 <= args.port <= 65535:
                raise AppError("INVALID_PORT", "端口必须为 1～65535。")
            config = replace(config, mcp_local=config.mcp_local or args.mcp_local)
            address = f"http://127.0.0.1:{args.port}"
            print(f"vagent Web: {address}", flush=True)
            if not args.no_open:
                webbrowser.open(address)
            uvicorn.run(
                create_app(config, port=args.port),
                host="127.0.0.1",
                port=args.port,
                access_log=False,
                log_level="warning",
            )
        elif args.command == "config":
            safe = asdict(config)
            key = safe.pop("api_key")
            video_key = safe.pop("video_api_key")
            kling_key = safe.pop("kling_api_key")
            fal_key = safe.pop("fal_api_key")
            safe["video_api_key_configured"] = bool(video_key)
            safe["kling_api_key_configured"] = bool(kling_key)
            safe["fal_api_key_configured"] = bool(fal_key)
            safe["video_permission_status"] = "unverified"
            safe["redis_configured"] = bool(safe.pop("redis_url"))
            print_json({**safe, "api_key_configured": bool(key and key.strip()), "thinking": "disabled"})
        elif args.command == "skills":
            catalog = discover_for(config)
            print_json({"skills": catalog.list(), "diagnostics": catalog.diagnostics})
        elif args.command == "jobs":
            if args.action == "work":
                raise SystemExit(asyncio.run(work_jobs(config)))
            local_jobs(config, args)
        elif args.command == "storyboard":
            with FileStore.open(config.home) as store:
                print_json(local_storyboard(store.snapshot(), args))
        elif args.command == "shots":
            with FileStore.open(config.home) as store:
                raise SystemExit(asyncio.run(local_redo(config, store, args)))
        elif args.command == "assets":
            with FileStore.open(config.home) as store:
                raise SystemExit(local_assets(config, store, args))
        elif args.command == "film":
            with FileStore.open(config.home) as store:
                raise SystemExit(asyncio.run(local_film(config, store, args)))
        elif args.command == "subtitle":
            if args.action == "export" and args.profile == "1080p":
                args.profile = "1080p-h264-aac-v1"
            elif args.action == "export":
                args.profile = "720p-h264-aac-v1"
            with FileStore.open(config.home) as store:
                raise SystemExit(asyncio.run(local_subtitle(config, store, args)))
        elif args.command == "episode" and args.action == "plan":
            print_json(local_episode_plan(args))
        elif args.command == "episode":
            with FileStore.open(config.home) as store:
                if args.action == "show":
                    films = FilmService(JobService(store, [MockVideoAdapter(store)], config=config))
                    print_json(local_episode_read(store.snapshot(), args, films))
                elif args.action == "save":
                    raise SystemExit(local_episode_save(config, store, args))
                else:
                    raise SystemExit(asyncio.run(local_episode_generate(config, store, args)))
        elif args.command == "usage" and args.project is not None:
            with FileStore.open(config.home) as store:
                print_json(local_usage(store.snapshot(), args.project))
        elif args.command == "usage":
            with FileStore.open(config.home) as store:
                runs = store.snapshot()["runs"]
                if args.run_id:
                    if args.run_id not in runs:
                        raise AppError("NOT_FOUND", "没有这个 Run，请使用 inspect 查看运行 ID。")
                    selected = [runs[args.run_id]]
                else:
                    selected = [
                        run for run in runs.values() if run["sessionId"] == (args.session or "default")
                    ]
                print_json(
                    {
                        "runs": [
                            {
                                "id": run["id"],
                                "sessionId": run["sessionId"],
                                "model": run["model"],
                                "status": run["status"],
                                "contextVersion": run.get("contextVersion", 1),
                                "readOnly": run.get("readOnly", False),
                                "usage": summarize_usage(run),
                                "modelCalls": run.get("modelCalls", []),
                            }
                            for run in selected
                        ]
                    }
                )
        elif args.command == "inspect":
            with FileStore.open(config.home) as store:
                state = store.snapshot()
                print_json(
                    {
                        "project": state["projects"].get(args.session),
                        "artifacts": [
                            a for a in state["artifacts"].values() if a["projectId"] == args.session
                        ],
                        "runs": [
                            {
                                **{
                                    k: run.get(k)
                                    for k in (
                                        "id",
                                        "status",
                                        "modelSteps",
                                        "toolCalls",
                                        "inputTokens",
                                        "outputTokens",
                                        "contextBytes",
                                        "droppedMessages",
                                        "errorCode",
                                        "resumable",
                                        "activeSeconds",
                                        "externalWaitSeconds",
                                        "activeWaitId",
                                        "waitResumeError",
                                        "policy",
                                        "videoMode",
                                    )
                                },
                                "usage": summarize_usage(run),
                                "contextVersion": run.get("contextVersion", 1),
                                "readOnly": run.get("readOnly", False),
                            }
                            for run in state["runs"].values()
                            if run["sessionId"] == args.session
                        ],
                    }
                )
        else:
            raise SystemExit(asyncio.run(run_agent(args)))
    except KeyboardInterrupt:
        print("\n执行已停止。", file=sys.stderr)
        raise SystemExit(130) from None
    except Exception as error:
        safe = public_error(error)
        print(f"[{safe.code}] {safe}", file=sys.stderr)
        raise SystemExit(1) from None
