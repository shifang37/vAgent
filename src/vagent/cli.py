import argparse
import asyncio
import json
import sys
from dataclasses import asdict

from dotenv import load_dotenv

from vagent import __version__
from vagent.config import load_config
from vagent.context import ContextBuilder
from vagent.errors import AppError, public_error
from vagent.models import DeepSeekModel, DemoModel
from vagent.runner import AgentRunner
from vagent.skills import SkillCatalog, register_skill_tool
from vagent.storage import FileStore
from vagent.tools import create_project_tools
from vagent.usage import summarize_usage


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
    elif event["type"] == "tool.started":
        print(f"[工具] {event['name']}", flush=True)
    elif event["type"] == "tool.completed":
        print(f"[工具结果] {event['name']}: {'成功' if event['ok'] else '失败'}", flush=True)


async def run_agent(args: argparse.Namespace) -> int:
    config = load_config()
    catalog = SkillCatalog.discover(config.skills_root)
    with FileStore.open(config.home) as store:
        saved = None
        if args.command == "resume":
            saved = store.snapshot()["runs"].get(args.run_id)
            if saved is None:
                raise AppError("NOT_FOUND", "没有这个 Run，请使用 inspect 查看运行 ID。")
        offline = args.command == "demo" or (saved and saved["model"] == DemoModel.name)
        model = DemoModel() if offline else DeepSeekModel(config.api_key, config.model)
        runner = AgentRunner(
            store=store,
            model=model,
            tools=register_skill_tool(create_project_tools(), catalog),
            context=ContextBuilder(config.context_bytes),
            skills=catalog.list(),
            on_event=show_event,
        )
        session_id = saved["sessionId"] if saved else args.session
        print(f"模型：{model.name}\n会话：{session_id}\n数据：{config.home}")
        if args.command == "chat":
            if not sys.stdin.isatty():
                raise AppError("TTY_REQUIRED", "交互模式需要终端；自动化调用请使用 run。")
            while True:
                # Read locally between runs; no background input thread remains after Ctrl+C.
                try:
                    prompt = input("\n你 > ")
                except EOFError:
                    return 0
                if prompt.strip() == "/exit":
                    return 0
                if not prompt.strip():
                    continue
                result = await runner.run(args.session, prompt)
                print(f"\nAgent > {result['answer']}\n[{result['status']}]")
                show_usage(result)
                show_resume_hint(result)
                if result["status"] == "cancelled":
                    return 130
        if args.command == "resume":
            result = await runner.resume(args.run_id)
        else:
            prompt = "演示读取项目和保存咖啡店方案。" if args.command == "demo" else args.prompt
            result = await runner.run(args.session, prompt, request_id=getattr(args, "request_id", None))
        print(
            f"\n{result['answer']}\n[{result['status']}] Run {result['id']}；模型 {result['modelSteps']} 步，工具 {result['toolCalls']} 次"
        )
        show_usage(result)
        show_resume_hint(result)
        return 0 if result["status"] == "completed" else 130 if result["status"] == "cancelled" else 1


def show_resume_hint(result: dict) -> None:
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
        if args.command == "config":
            safe = asdict(config)
            key = safe.pop("api_key")
            print_json({**safe, "api_key_configured": bool(key and key.strip()), "thinking": "disabled"})
        elif args.command == "skills":
            print_json(SkillCatalog.discover(config.skills_root).list())
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
                                        "policy",
                                    )
                                },
                                "usage": summarize_usage(run),
                                "contextVersion": run.get("contextVersion", 1),
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
