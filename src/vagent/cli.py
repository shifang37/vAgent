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


def print_json(value: object) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2, default=str))


def show_event(event: dict) -> None:
    if event["type"] == "context.prepared" and event["droppedMessages"]:
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
    model = DemoModel() if args.command == "demo" else DeepSeekModel(config.api_key, config.model)
    catalog = SkillCatalog.discover(config.skills_root)
    with FileStore.open(config.home) as store:
        runner = AgentRunner(
            store=store,
            model=model,
            tools=register_skill_tool(create_project_tools(), catalog),
            context=ContextBuilder(config.context_bytes),
            skills=catalog.list(),
            on_event=show_event,
        )
        print(f"模型：{model.name}\n会话：{args.session}\n数据：{config.home}")
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
                if result["status"] == "cancelled":
                    return 130
        prompt = "演示读取项目和保存咖啡店方案。" if args.command == "demo" else args.prompt
        result = await runner.run(args.session, prompt, request_id=getattr(args, "request_id", None))
        print(
            f"\n{result['answer']}\n[{result['status']}] Run {result['id']}；模型 {result['modelSteps']} 步，工具 {result['toolCalls']} 次"
        )
        return 0 if result["status"] == "completed" else 130 if result["status"] == "cancelled" else 1


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(prog="vagent", description="DeepSeek + LangGraph 视频创作 Agent 原型")
    root.add_argument("--version", action="version", version=__version__)
    commands = root.add_subparsers(dest="command", required=True)
    run = commands.add_parser("run", help="执行真实 DeepSeek 任务，会产生 API 费用")
    run.add_argument("prompt")
    run.add_argument("--request-id")
    chat = commands.add_parser("chat", help="持续对话；/exit 退出，Ctrl+C 停止")
    demo = commands.add_parser("demo", help="无需 Key 的离线模拟演示")
    inspect = commands.add_parser("inspect", help="查看本地项目、产物与运行记录")
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
                                )
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
