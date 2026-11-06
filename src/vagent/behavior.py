"""Offline behavior evaluation: replay scoring, pass@1 gates, maximum matching.

Everything here is deterministic and offline. Each sample replays a scripted reply
sequence through the product runner, so ``textModelCalls`` and ``realProviderCalls``
stay 0. The replay model deliberately ignores the prompt and the tool list: an offline
sample proves the scorer, the gate and the report shape, never real model behavior.
Scoring is separate from the cases so it can be mutation-tested, and an optional judge
is recorded but never gates a sample.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from langchain_core.messages import AIMessage, messages_from_dict

from vagent.errors import AppError
from vagent.runner import AgentRunner, RunPolicy
from vagent.storage import FileStore
from vagent.tools import create_project_tools

SUITE_VERSION = 2
CASE_SESSION = "behavior"
GATE = "gate"
MEASURE = "measure"
LIMITATIONS = (
    "离线脚本回放：ReplayModel 不读取提示词与工具，回放结果与输入无关，"
    "因此不证明真实模型行为，也不覆盖提示词或上下文回归；只证明评分器、pass@1 门禁与报告口径。"
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
    """A golden case: a prompt, scripted samples, the expected calls and end state.

    ``gate`` cases must pass every evaluated sample; ``measure`` cases only report
    metrics. The default is ``gate`` so a new case cannot silently stop gating.
    """

    name: str
    prompt: str
    samples: list[list[AIMessage]]
    expected_tools: list[ExpectedCall] = field(default_factory=list)
    expect_artifacts: int | None = None
    expect_project: dict | None = None
    # A tolerated number of extra calls (e.g. one schema-error retry is not over-calling).
    allowed_redundant: int = 0
    rubric: tuple[str, ...] = ()
    group: Literal["gate", "measure"] = GATE

    def __post_init__(self) -> None:
        if self.group not in {GATE, MEASURE}:
            raise ValueError("BehaviorCase.group 只支持 gate 或 measure。")
        if not self.samples:
            raise ValueError("BehaviorCase 至少需要一个样本。")


def _maximum_matching(executed: list[dict], expected: list[ExpectedCall]) -> list[int]:
    """Kuhn's augmenting paths: owner[executed_index] = expected_index or -1.

    A greedy pass can consume a generic expectation with a specific call and then
    miss a later match; augmenting paths find the maximum one-to-one matching, so
    precision/recall never undercount because of script order.
    """
    edges = [[index for index, call in enumerate(executed) if want.matches(call)] for want in expected]
    owner: list[int] = [-1] * len(executed)

    def augment(want: int, seen: set[int]) -> bool:
        for call_index in edges[want]:
            if call_index in seen:
                continue
            seen.add(call_index)
            if owner[call_index] == -1 or augment(owner[call_index], seen):
                owner[call_index] = want
                return True
        return False

    for want in range(len(expected)):
        augment(want, set())
    return owner


def score_calls(executed: list[dict], expected: list[ExpectedCall]) -> dict:
    """Maximum one-to-one matching: extra calls lower precision, absent calls lower recall."""
    owner = _maximum_matching(executed, expected)
    matched_wants = {want for want in owner if want != -1}
    matched = len(matched_wants)
    redundant = [
        {"name": call.get("name"), "args": call.get("args")}
        for index, call in enumerate(executed)
        if owner[index] == -1
    ]
    missing = [
        {"name": want.name, "args": want.args}
        for index, want in enumerate(expected)
        if index not in matched_wants
    ]
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
    """A deterministic stand-in that replays one sample's scripted replies step by step.

    It intentionally never looks at ``messages`` or ``tools``: that is the honest
    boundary of an offline replay. The only input assertion is structural, so a
    scripted tool call in a run that has no tools fails loudly instead of pretending
    the script adapted to its input.
    """

    def __init__(self, script: list[AIMessage], name: str = "offline-replay"):
        self.name = name
        self.script = list(script)
        self.calls = 0

    async def generate(self, messages, tools):
        step = self.calls
        self.calls += 1
        if step >= len(self.script):
            raise AppError("REPLAY_EXHAUSTED", "评测脚本的回复步数已用尽，本次样本未完成。")
        reply = self.script[step]
        if not tools and getattr(reply, "tool_calls", None):
            raise AppError(
                "REPLAY_SCRIPT_NEEDS_TOOLS",
                "评测脚本请求了工具，但本次调用没有提供工具列表；脚本并不依赖输入。",
            )
        return reply


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


def _script_payload(script: list[AIMessage]) -> list[dict]:
    """The complete script text and tool calls; a prompt-only change must change the id."""
    payload = []
    for message in script:
        payload.append(
            {
                "type": getattr(message, "type", ""),
                "content": message.content,
                "toolCalls": [
                    {"name": call.get("name"), "args": call.get("args"), "id": call.get("id")}
                    for call in getattr(message, "tool_calls", None) or []
                ],
            }
        )
    return payload


def suite_id(cases: list[BehaviorCase]) -> str:
    payload = [
        {
            "name": case.name,
            "prompt": case.prompt,
            "group": case.group,
            "expected": [{"name": want.name, "args": want.args} for want in case.expected_tools],
            "expectArtifacts": case.expect_artifacts,
            "expectProject": case.expect_project,
            "allowedRedundant": case.allowed_redundant,
            "samples": [_script_payload(script) for script in case.samples],
            "rubric": list(case.rubric),
        }
        for case in cases
    ]
    canonical = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def validate_k(cases: list[BehaviorCase], k: int) -> None:
    """Reject a request for more samples than a case actually scripts."""
    if type(k) is not int or k < 1:
        raise AppError("EVAL_K_INVALID", "k 必须是至少 1 的整数。")
    for case in cases:
        if k > len(case.samples):
            raise AppError(
                "EVAL_K_TOO_LARGE",
                f"用例 {case.name} 只有 {len(case.samples)} 个样本，k={k} 超过样本数；不会重复最后一个脚本。",
            )


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
        "group": case.group,
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
    gate_entries = [entry for entry in entries if entry["group"] == GATE]
    measure_entries = [entry for entry in entries if entry["group"] == MEASURE]
    failed_gates = [entry["name"] for entry in gate_entries if entry["pass@1"] != 1.0]
    totals = {
        "cases": len(cases),
        "gateCases": len(gate_entries),
        "measureCases": len(measure_entries),
        "samples": len(samples),
        "k": k,
        "passedSamples": int(sum(passed)),
        "successRate": _mean(passed),
        "pass@1": _mean([entry["pass@1"] for entry in entries]),
        "pass@k": _mean([entry["pass@k"] for entry in entries]),
        "gatePass@1": _mean([entry["pass@1"] for entry in gate_entries]),
        "measurePass@1": _mean([entry["pass@1"] for entry in measure_entries]),
        "failedGates": failed_gates,
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
        "mode": "offline-replay",
        "k": k,
        # Only gate cases decide the exit status; a measure-only run is not a gate.
        "status": "passed" if gate_entries and not failed_gates else "failed",
        "cases": entries,
        "totals": totals,
        "limitations": LIMITATIONS,
    }


async def evaluate(cases: list[BehaviorCase], *, home, k: int = 1, judge=None, policy=None) -> dict:
    """Run every case ``k`` times and score it; identical inputs give an identical report."""
    if not cases:
        raise AppError("EVAL_NO_CASES", "没有选中的用例。")
    validate_k(cases, k)
    entries = []
    for case in cases:
        samples = []
        for index in range(k):
            samples.append(await run_sample(case, case.samples[index], home=home, index=index, policy=policy))
        entries.append(summarize_case(case, samples, judge))
    return build_report(cases, entries, k=k, judge=judge)
