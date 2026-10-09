import argparse
import asyncio
import json
import sys
from contextlib import AsyncExitStack
from dataclasses import asdict, replace
from uuid import uuid4

from dotenv import load_dotenv

from vagent import __version__
from vagent.application import ApplicationService
from vagent.config import assert_id, load_config
from vagent.console import read_prompt
from vagent.errors import AppError, public_error
from vagent.models import DemoModel
from vagent.skills import SkillCatalog
from vagent.storage import FileStore
from vagent.usage import summarize_usage
from vagent.video.jobs import JobService
from vagent.video.views import job_view


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
        print(f"[模拟 Job] {job['jobId']} · {job['status']} / {job['queryState']} · 无真实媒体", flush=True)


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
    if any(job["status"] in {"pending_submit", "submitting", "queued", "running"} for job in service.jobs()):
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
    # Status/retry commands are local only: no adapters, MCP processes, model
    # connections or background workers start merely to read a Job.
    with FileStore.open(config.home) as store:
        service = JobService(store, [])
        if args.action == "list":
            if args.session is not None:
                assert_id(args.session)
            print_json({"jobs": [job_view(job) for job in reversed(service.list(session_id=args.session))]})
        else:
            assert_id(args.job_id)
            job = service.get(args.job_id)
            if args.action == "retry-query":
                job = service.retry_query(job.id, project_id=job.context.project_id)
            print_json(job_view(job))


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
    for command in (run, chat, inspect):
        command.add_argument("-s", "--session", default="default")
    demo.add_argument("-s", "--session", default="demo")
    commands.add_parser("config").add_subparsers(dest="action", required=True).add_parser("show")
    commands.add_parser("skills").add_subparsers(dest="action", required=True).add_parser("list")
    jobs = commands.add_parser("jobs", help="查看本地模拟任务、恢复查询或运行持久队列")
    actions = jobs.add_subparsers(dest="action", required=True)
    listing = actions.add_parser("list", help="列出本地任务，不调用模型或供应商")
    listing.add_argument("-s", "--session")
    actions.add_parser("get", help="查看一个本地任务").add_argument("job_id")
    actions.add_parser("retry-query", help="恢复已暂停的原任务查询，不重新提交").add_argument("job_id")
    actions.add_parser("work", help="持续推进任务和有效等待；Ctrl+C 退出并保留状态")
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
            safe["redis_configured"] = bool(safe.pop("redis_url"))
            print_json({**safe, "api_key_configured": bool(key and key.strip()), "thinking": "disabled"})
        elif args.command == "skills":
            print_json(SkillCatalog.discover(config.skills_root).list())
        elif args.command == "jobs":
            if args.action == "work":
                raise SystemExit(asyncio.run(work_jobs(config)))
            local_jobs(config, args)
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
