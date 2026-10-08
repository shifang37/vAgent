import copy
import json
from argparse import Namespace

import pytest
from langchain_core.messages import HumanMessage

from scripts.evaluate_m1a import CASES, FixtureModel, compare_reports, grade, run_suite
from vagent.errors import AppError


def options(tmp_path, *, limit=32):
    return Namespace(
        home=tmp_path / "data",
        output=tmp_path / "report.json",
        live=False,
        context_bytes=65536,
        context_version=2,
        max_model_calls=limit,
        compare=None,
    )


async def test_evaluation_uses_real_tools_and_grades_outputs_independently(tmp_path):
    args = options(tmp_path)
    report = await run_suite(args)
    assert report["status"] == "passed" and len(report["cases"]) == 9
    assert report["mode"] == "offline-fixture" and report["totals"]["modelCalls"] == 20
    assert not report["totals"]["tokenUsageComplete"]
    assert report["totals"]["cacheHitTokens"] is None
    assert json.loads(args.output.read_text(encoding="utf-8")) == report
    records = {item["case"]: item for item in report["cases"]}

    def grade_changed(name, mutate):
        item = copy.deepcopy(records[name])
        mutate(item)
        case = next(case for case in CASES if case.name == name)
        return grade(case, item["before"], item["after"], item["run"])

    # A model saying "completed" never substitutes for independently checking state.
    def stale_memory(item):
        item["after"]["project"]["goal"] += "暖色自然光"

    assert not grade_changed("revision-and-mcp", stale_memory)["memory_consistent"]

    def overwritten(item):
        item["after"]["artifacts"][0]["versions"][0]["content"] = "原版被覆盖"

    assert not grade_changed("revision-and-mcp", overwritten)["original_versions_preserved"]

    def overlong(item):
        item["after"]["artifacts"][0]["versions"][-1]["content"] = "字" * 121

    checked = grade_changed("length-revision", overlong)
    assert not checked["new_120_limit_enforced"] and not checked["saved_counts_and_limits"]

    def unauthorized(item):
        item["after"]["project"]["audience"] = "儿童"

    assert not grade_changed("read-only-write-attempt", unauthorized)["domain_unchanged"]

    def fake_reference(item):
        item["run"]["answer"] = "已保存 imaginary-id v1"

    assert not grade_changed("brief-and-memory", fake_reference)["real_artifact_references"]
    comparison = compare_reports(report, copy.deepcopy(report))
    assert comparison["sameSuiteModeAndModel"]
    assert all(item["inputTokensDelta"] is None for item in comparison["cases"])
    with pytest.raises(AppError) as caught:
        await run_suite(args)
    assert caught.value.code == "EVAL_HOME_NOT_EMPTY"


async def test_evaluation_enforces_suite_call_budget_and_does_not_claim_full_pass(tmp_path):
    report = await run_suite(options(tmp_path, limit=1))
    assert report["status"] == "budget-exhausted"
    assert report["totals"]["modelCalls"] == 1
    assert report["totals"]["completedCases"] == 1 and report["totals"]["plannedCases"] == 9


def test_report_comparison_only_uses_complete_live_usage_for_same_model():
    def report(tokens, *, mode="live", complete=True):
        return {
            "suiteId": "test",
            "mode": mode,
            "model": "test-model",
            "cases": [
                {
                    "case": "test",
                    "checks": {"valid": True},
                    "elapsedSeconds": 1,
                    "run": {
                        "contextBytes": 100,
                        "usage": {
                            "tokenUsageComplete": complete,
                            "observedInputTokens": tokens,
                            "observedOutputTokens": 2,
                        },
                    },
                }
            ],
        }

    result = compare_reports(report(10), report(8))
    assert result["cases"][0]["inputTokensDelta"] == -2
    assert compare_reports(report(10), report(8, complete=False))["cases"][0]["inputTokensDelta"] is None
    assert (
        compare_reports(report(10), report(8, mode="offline-fixture"))["cases"][0]["inputTokensDelta"] is None
    )
    changed_model = report(8)
    changed_model["model"] = "different-model"
    assert not compare_reports(report(10), changed_model)["sameSuiteModeAndModel"]


async def test_explicit_evaluation_resume_keeps_evidence_budget_and_saved_artifacts(tmp_path, monkeypatch):
    class FaultFixture(FixtureModel):
        def __init__(self):
            self.case = None
            self.failed = False

        async def generate_stream(self, messages, tools, delta):
            prompt = next(
                message.content for message in reversed(messages) if isinstance(message, HumanMessage)
            )
            name = next(case.name for case in CASES if case.prompt == prompt)
            if name != self.case:
                self.begin(name)
            if name == "brief-and-memory" and self.step == 3 and not self.failed:
                self.failed = True
                raise AppError("EXECUTION_ERROR", "模拟连接故障")
            return await super().generate_stream(messages, tools, delta)

    model = FaultFixture()
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")
    monkeypatch.setattr("vagent.application.DeepSeekModel", lambda *_args, **_kwargs: model)
    args = options(tmp_path)
    args.live = True  # The adapter is replaced by a local fixture; no provider request.
    first = await run_suite(args)
    assert first["status"] == "failed" and first["totals"]["modelCalls"] == 5
    original = args.output.read_bytes()
    continuation = options(tmp_path / "continued", limit=64)
    continuation.continue_from = args.output
    with pytest.raises(AppError) as caught:
        await run_suite(continuation)
    assert caught.value.code == "EVAL_CONTINUE_INVALID"  # No implicit paid continuation.
    continuation.live = True
    final = await run_suite(continuation)
    assert final["status"] == "passed" and final["totals"]["modelCalls"] == 21
    assert final["maxModelCalls"] == 32 and args.output.read_bytes() == original
    assert final["cases"][1]["run"]["id"] == first["cases"][1]["run"]["id"]
    assert final["cases"][1]["after"]["artifacts"] == first["cases"][1]["after"]["artifacts"]
    assert final["resumes"][0]["callsBefore"] == 4
    assert any(event["type"] == "model.failed" for event in final["cases"][1]["run"]["events"])
