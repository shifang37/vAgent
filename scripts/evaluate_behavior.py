"""Offline behavior evaluation: task success, tool-selection precision, pass@k, judging.

Fully offline and deterministic: every sample replays a scripted reply sequence through
the product ``AgentRunner``, so ``textModelCalls`` and ``realProviderCalls`` stay 0. The
report never overwrites an existing file, and ``--live`` is refused because no paid
sample is authorized for this suite (real samples live in their own milestone evidence).
"""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

from langchain_core.messages import AIMessage

from vagent.behavior import BehaviorCase, ExpectedCall, RubricJudge, evaluate
from vagent.errors import AppError
from vagent.storage import atomic_write_json, now

BRIEF = "栖岸咖啡 30 秒品牌短片：暖色自然光，无旁白，面向城市上班族。"
BAD_REVISION = {"expectedRevision": "0"}


def tool(name: str, args: dict, call_id: str) -> AIMessage:
    return AIMessage(content="", tool_calls=[{"name": name, "args": args, "id": call_id}])


def say(text: str) -> AIMessage:
    return AIMessage(content=text)


CASES = [
    BehaviorCase(
        name="brief-and-memory",
        prompt=(
            "这是虚构项目「栖岸咖啡」。请把创作目标保存到项目记忆，再保存一份 kind=brief 的产物，"
            "最后用一句话报告已保存的 artifactId；不要生成视频。"
        ),
        expected_tools=[ExpectedCall("project_update"), ExpectedCall("artifact_save", {"kind": "brief"})],
        expect_artifacts=1,
        expect_project={"goal": "提升品牌认知和到店意愿"},
        rubric=("artifactId",),
        samples=[
            [
                tool("project_update", {"expectedRevision": 0, "goal": "提升品牌认知和到店意愿"}, "a"),
                tool("artifact_save", {"kind": "brief", "title": "创作方案", "content": BRIEF}, "b"),
                say("已保存 brief，artifactId 见上方工具结果。"),
            ],
            [
                tool("project_read", {}, "a"),
                tool("project_update", {"expectedRevision": 0, "goal": "提升品牌认知和到店意愿"}, "b"),
                tool("project_read", {}, "c"),
                tool("artifact_save", {"kind": "brief", "title": "创作方案", "content": BRIEF}, "d"),
                say("已保存 brief。"),
            ],
        ],
    ),
    BehaviorCase(
        name="answer-without-tools",
        prompt="只回答一个问题：这个项目现在保存了哪些创作目标？不要读写或修改任何状态。",
        expected_tools=[],
        expect_artifacts=0,
        samples=[
            [say("当前没有保存任何创作目标。")],
            [tool("project_read", {}, "a"), say("当前没有保存任何创作目标。")],
        ],
    ),
    BehaviorCase(
        name="recover-from-bad-arguments",
        prompt="把受众更新为「夜班护士」，然后保存一份 kind=brief 的产物并报告 artifactId。",
        expected_tools=[
            ExpectedCall("project_update", {"audience": "夜班护士"}),
            ExpectedCall("artifact_save", {"kind": "brief"}),
        ],
        expect_artifacts=1,
        # One schema-error retry is tolerated; it is not over-calling.
        allowed_redundant=1,
        rubric=("artifactId",),
        samples=[
            [tool("project_update", BAD_REVISION, "a"), tool("project_update", BAD_REVISION, "b")],
            [
                tool("project_update", BAD_REVISION, "a"),
                tool("project_update", {"expectedRevision": 0, "audience": "夜班护士"}, "b"),
                tool("artifact_save", {"kind": "brief", "title": "创作方案", "content": BRIEF}, "c"),
                say("已更新受众并保存 brief，artifactId 见工具结果。"),
            ],
        ],
    ),
]


async def run_suite(args):
    if getattr(args, "live", False):
        raise AppError(
            "EVAL_LIVE_UNAVAILABLE",
            "行为评测未获真实付费样本授权；--live 未执行，未发起任何请求。",
        )
    if args.output.exists():
        raise AppError("EVAL_OUTPUT_EXISTS", "输出文件已存在，请使用新路径保留先前证据。")
    home = args.home.resolve()
    if home.exists() and any(home.iterdir()):
        raise AppError("EVAL_HOME_NOT_EMPTY", "评测需要新的空数据目录，已有数据未修改。")
    selected = [case for case in CASES if not args.only or case.name in args.only]
    if not selected:
        raise AppError("EVAL_NO_CASES", "没有选中的用例。")

    report = await evaluate(selected, home=home, k=args.k, judge=RubricJudge())
    report.update(startedAt=now(), finishedAt=now(), dataDirectory=str(home))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_json(args.output, report)
    print(
        json.dumps(
            {
                "suite": report["suiteId"],
                "mode": report["mode"],
                "status": report["status"],
                "pass@1": report["totals"]["pass@1"],
                "pass@k": report["totals"]["pass@k"],
            },
            ensure_ascii=False,
        ),
        flush=True,
    )
    return report


def parser():
    root = argparse.ArgumentParser(description=__doc__)
    root.add_argument("--home", type=Path, required=True, help="新的空数据目录")
    root.add_argument("--output", type=Path, required=True, help="报告输出路径")
    root.add_argument("--k", type=int, default=2, help="每个用例的采样次数（默认 2）")
    root.add_argument(
        "--only",
        action="append",
        default=[],
        choices=[case.name for case in CASES],
        help="仅运行指定用例（开发调试；默认全部）",
    )
    root.add_argument(
        "--live",
        action="store_true",
        help="本套件未获真实付费样本授权；本参数会拒绝执行且不发起请求",
    )
    return root


def main():
    args = parser().parse_args()
    try:
        report = asyncio.run(run_suite(args))
    except AppError as error:
        print(json.dumps({"status": "error", "code": error.code, "message": str(error)}, ensure_ascii=False))
        raise SystemExit(1) from None
    raise SystemExit(0 if report["status"] == "passed" else 1)


if __name__ == "__main__":
    main()
