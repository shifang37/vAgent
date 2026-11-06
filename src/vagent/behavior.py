"""Offline behavior evaluation: success, tool-selection precision, pass@k, judging.

Everything here is deterministic and offline. Each sample replays a scripted reply
sequence, so a sample is reproducible and costs nothing: ``textModelCalls`` and
``realProviderCalls`` are always 0. Scoring is separate from the cases so it can be
mutation-tested, and an optional judge is recorded but never gates a sample.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path

from langchain_core.messages import AIMessage, messages_from_dict

from vagent.errors import AppError
from vagent.runner import AgentRunner, RunPolicy
from vagent.storage import FileStore
from vagent.tools import create_project_tools

SUITE_VERSION = 1
CASE_SESSION = "behavior"
LIMITATIONS = (
    "离线脚本模型回放：证明评分器、pass@k 与报告口径，不证明真实模型质量或生产成功率。"
    "真实供应商调用与文本模型调用均为 0；judge 为离线确定性打分，不是人工评审。"
)


@dataclass(frozen=True)
class ExpectedCall:
    """One required tool call: the name plus a subset of arguments that must match."""

    name: str
    args: dict = field(default_factory=dict)

    def matches(self, call: dict) -> bool:
        if call.get("name") != self.name:
            return False
        actual = call.get("args") or {}
        return all(actual.get(key) == value for key, value in self.args.items())


@dataclass(frozen=True)
class BehaviorCase:
    """A golden case: a prompt, k scripted samples, the expected calls and end state."""

    name: str
    prompt: str
    samples: list[list[AIMessage]]
    expected_tools: list[ExpectedCall] = field(default_factory=list)
    expect_artifacts: int | None = None
    expect_project: dict | None = None
    # A tolerated number of extra calls (e.g. one schema-error retry is not over-calling).
    allowed_redundant: int = 0
    rubric: tuple[str, ...] = ()


def score_calls(executed: list[dict], expected: list[ExpectedCall]) -> dict:
    """One-to-one greedy match: extra calls lower precision, absent calls lower recall."""
    remaining = list(expected)
    matched = 0
    redundant: list[dict] = []
    for call in executed:
        for index, want in enumerate(remaining):
            if want.matches(call):
                matched += 1
                remaining.pop(index)
                break
        else:
            redundant.append({"name": call.get("name"), "args": call.get("args")})
    missing = [{"name": want.name, "args": want.args} for want in remaining]
    if executed:
        precision = matched / len(executed)
    else:
        precision = 1.0 if not expected else 0.0
    recall = matched / len(expected) if expected else 1.0
    return {
        "matched": matched,
        "expected": len(expected),
        "executed": len(executed),
        "redundant": redundant,
        "missing": missing,
        "precision": precision,
        "recall": recall,
    }


class RubricJudge:
    """A deterministic offline judge: the fraction of rubric phrases present."""

    name = "offline-rubric-v1"

    def score(self, answer: str, rubric: tuple[str, ...]) -> dict:
        text = answer or ""
        hits = [phrase for phrase in rubric if phrase in text]
        return {
            "judge": self.name,
            "score": len(hits) / len(rubric) if rubric else 1.0,
            "hits": hits,
            "rubric": list(rubric),
        }


class ReplayModel:
    """A deterministic stand-in that replays one sample's scripted replies step by step."""

    def __init__(self, script: list[AIMessage], name: str = "offline-replay"):
        self.name = name
        self.script = list(script)
        self.calls = 0

    async def generate(self, messages, tools):
        step = self.calls
        self.calls += 1
        if step >= len(self.script):
            raise AppError("REPLAY_EXHAUSTED", "评测脚本的回复步数已用尽，本次样本未完成。")
        return self.script[step]


def executed_calls(run: dict) -> list[dict]:
    calls = []
    for message in messages_from_dict(run["messages"]):
        if isinstance(message, AIMessage):
            calls.extend({"name": call["name"], "args": call["args"]} for call in message.tool_calls)
    return calls


def _mean(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def _variance(values: list[float]) -> float:
    if not values:
        return 0.0
    mean = _mean(values)
    return sum((value - mean) ** 2 for value in values) / len(values)


def suite_id(cases: list[BehaviorCase]) -> str:
    payload = [
        {
            "name": case.name,
            "prompt": case.prompt,
            "expected": [{"name": want.name, "args": want.args} for want in case.expected_tools],
            "expectArtifacts": case.expect_artifacts,
            "expectProject": case.expect_project,
            "allowedRedundant": case.allowed_redundant,
            "samples": len(case.samples),
            "rubric": list(case.rubric),
        }
        for case in cases
    ]
    canonical = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


async def run_sample(case: BehaviorCase, script: list[AIMessage], *, home: Path, index: int, policy) -> dict:
    home = Path(home) / f"{case.name}-{index}"
    model = ReplayModel(script)
    with FileStore.open(home) as store:
        runner = AgentRunner(
            store=store,
            model=model,
            tools=create_project_tools(),
            policy=policy or RunPolicy(),
        )
        run = await runner.run(CASE_SESSION, case.prompt, request_id=f"{case.name}-{index}")
        snapshot = store.snapshot()
    calls = executed_calls(run)
    metrics = score_calls(calls, case.expected_tools)
    project = snapshot["projects"].get(CASE_SESSION, {})
    checks = {
        "completed": run["status"] == "completed",
        "tools": metrics["recall"] == 1.0 and len(metrics["redundant"]) <= case.allowed_redundant,
        "artifacts": case.expect_artifacts is None or len(snapshot["artifacts"]) == case.expect_artifacts,
        "project": case.expect_project is None
        or all(project.get(key) == value for key, value in case.expect_project.items()),
    }
    return {
        "index": index,
        "status": run["status"],
        "errorCode": run["errorCode"],
        "modelSteps": run["modelSteps"],
        "toolCalls": calls,
        "answer": run["answer"],
        "metrics": metrics,
        "checks": checks,
        "passed": all(checks.values()),
    }


def summarize_case(case: BehaviorCase, samples: list[dict], judge) -> dict:
    passed = [1.0 if sample["passed"] else 0.0 for sample in samples]
    entry = {
        "name": case.name,
        "pass@1": _mean(passed),
        "pass@k": 1.0 if any(sample["passed"] for sample in samples) else 0.0,
        "variance": _variance(passed),
        "meanPrecision": _mean([sample["metrics"]["precision"] for sample in samples]),
        "meanRecall": _mean([sample["metrics"]["recall"] for sample in samples]),
        "samples": samples,
    }
    if judge is not None:
        entry["judge"] = [judge.score(sample["answer"], case.rubric) for sample in samples]
    return entry


def build_report(cases: list[BehaviorCase], entries: list[dict], *, k: int, judge) -> dict:
    samples = [sample for entry in entries for sample in entry["samples"]]
    passed = [1.0 if sample["passed"] else 0.0 for sample in samples]
    totals = {
        "cases": len(cases),
        "samples": len(samples),
        "k": k,
        "passedSamples": int(sum(passed)),
        "successRate": _mean(passed),
        "pass@1": _mean([entry["pass@1"] for entry in entries]),
        "pass@k": _mean([entry["pass@k"] for entry in entries]),
        "variance": _mean([entry["variance"] for entry in entries]),
        "meanToolPrecision": _mean([entry["meanPrecision"] for entry in entries]),
        "meanToolRecall": _mean([entry["meanRecall"] for entry in entries]),
        "redundantCalls": sum(len(sample["metrics"]["redundant"]) for sample in samples),
        "missingCalls": sum(len(sample["metrics"]["missing"]) for sample in samples),
        "realProviderCalls": 0,
        "textModelCalls": 0,
    }
    if judge is not None:
        scores = [score["score"] for entry in entries for score in entry.get("judge", [])]
        totals["judge"] = {"mode": judge.name, "meanScore": _mean(scores), "scored": len(scores)}
    return {
        "schemaVersion": SUITE_VERSION,
        "suiteId": suite_id(cases),
        "mode": "offline-fixture",
        "k": k,
        "status": "passed" if all(entry["pass@k"] == 1.0 for entry in entries) else "failed",
        "cases": entries,
        "totals": totals,
        "limitations": LIMITATIONS,
    }


async def evaluate(cases: list[BehaviorCase], *, home, k: int = 1, judge=None, policy=None) -> dict:
    """Run every case ``k`` times and score it; identical inputs give an identical report."""
    entries = []
    for case in cases:
        samples = []
        for index in range(k):
            script = case.samples[index] if index < len(case.samples) else case.samples[-1]
            samples.append(await run_sample(case, script, home=home, index=index, policy=policy))
        entries.append(summarize_case(case, samples, judge))
    return build_report(cases, entries, k=k, judge=judge)
