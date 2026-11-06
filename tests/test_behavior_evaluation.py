"""H4 offline behavior-evaluation suite: scoring, pass@1 gates, judging, report discipline."""

import json
from argparse import Namespace
from dataclasses import replace
from pathlib import Path

import pytest
from langchain_core.messages import AIMessage

from scripts import evaluate_behavior as evaluation
from vagent.behavior import (
    BehaviorCase,
    ExpectedCall,
    ReplayModel,
    RubricJudge,
    evaluate,
    run_sample,
    score_calls,
    suite_id,
)
from vagent.errors import AppError


def options(tmp_path, *, k=2, only=None, live=False, output="report.json"):
    return Namespace(
        home=tmp_path / "data",
        output=tmp_path / output,
        k=k,
        only=only or [],
        live=live,
    )


def tool(name, args, call_id):
    return AIMessage(content="", tool_calls=[{"name": name, "args": args, "id": call_id}])


def say(text):
    return AIMessage(content=text)


@pytest.fixture(scope="module")
async def accepted_suite(tmp_path_factory):
    args = options(tmp_path_factory.mktemp("behavior-evaluation"))
    report = await evaluation.run_suite(args)
    return args, report


async def test_suite_runs_every_case_offline(accepted_suite):
    args, report = accepted_suite
    assert report["status"] == "passed"
    assert report["mode"] == "offline-replay"
    assert report["schemaVersion"] == 2
    assert report["totals"]["cases"] == 5
    assert report["totals"]["samples"] == 10 and report["totals"]["k"] == 2
    assert report["totals"]["gateCases"] == 2 and report["totals"]["measureCases"] == 3
    assert report["totals"]["pass@1"] == 0.7 and report["totals"]["pass@k"] == 1.0
    assert report["totals"]["gatePass@1"] == 1.0 and report["totals"]["measurePass@1"] == 0.5
    assert report["totals"]["variance"] == 0.15 and report["totals"]["failedGates"] == []
    assert report["totals"]["realProviderCalls"] == 0 and report["totals"]["textModelCalls"] == 0
    assert report["totals"]["redundantCalls"] > 0 and report["totals"]["missingCalls"] > 0
    assert [entry["group"] for entry in report["cases"]].count("gate") == 2
    assert json.loads(args.output.read_text(encoding="utf-8")) == report


async def test_the_report_never_overwrites_an_existing_file(tmp_path):
    args = options(tmp_path)
    await evaluation.run_suite(args)
    first = args.output.read_text(encoding="utf-8")
    with pytest.raises(AppError) as error:
        await evaluation.run_suite(args)
    assert error.value.code == "EVAL_OUTPUT_EXISTS"
    assert args.output.read_text(encoding="utf-8") == first


async def test_live_mode_is_refused_without_touching_the_network(tmp_path):
    with pytest.raises(AppError) as error:
        await evaluation.run_suite(options(tmp_path, live=True))
    assert error.value.code == "EVAL_LIVE_UNAVAILABLE"
    assert not (tmp_path / "report.json").exists()


async def test_identical_inputs_give_an_identical_report(tmp_path):
    first = await evaluate(evaluation.CASES, home=tmp_path / "one", k=2, judge=RubricJudge())
    second = await evaluate(evaluation.CASES, home=tmp_path / "two", k=2, judge=RubricJudge())
    assert json.dumps(first, sort_keys=True) == json.dumps(second, sort_keys=True)


def test_scorer_penalises_missing_and_redundant_calls():
    expected = [ExpectedCall("artifact_save", {"kind": "brief"}), ExpectedCall("project_update")]
    perfect = [
        {"name": "artifact_save", "args": {"kind": "brief", "title": "t"}},
        {"name": "project_update", "args": {}},
    ]
    metrics = score_calls(perfect, expected)
    assert (metrics["precision"], metrics["recall"]) == (1.0, 1.0) and not metrics["redundant"]

    missing = score_calls(perfect[:1], expected)
    assert missing["recall"] == 0.5 and missing["missing"] == [{"name": "project_update", "args": {}}]

    redundant = score_calls([*perfect, {"name": "project_read", "args": {}}], expected)
    assert redundant["precision"] < 1.0 and redundant["redundant"] == [{"name": "project_read", "args": {}}]

    # Wrong arguments do not satisfy a call: the name alone is not enough.
    wrong = score_calls([{"name": "artifact_save", "args": {"kind": "script"}}], expected)
    assert wrong["matched"] == 0 and wrong["recall"] == 0.0

    # No expectation and no call is a clean pass; no call but an expectation is not.
    assert score_calls([], [])["precision"] == score_calls([], [])["recall"] == 1.0
    assert score_calls([], expected)["precision"] == 0.0


def test_maximum_matching_counts_both_expected_calls():
    # Greedy would spend the generic expectation on the specific call and score 1/2.
    expected = [ExpectedCall("project_update"), ExpectedCall("project_update", {"audience": "夜班护士"})]
    executed = [
        {"name": "project_update", "args": {"audience": "夜班护士"}},
        {"name": "project_update", "args": {"goal": "g"}},
    ]
    metrics = score_calls(executed, expected)
    assert metrics["matched"] == 2
    assert (metrics["precision"], metrics["recall"]) == (1.0, 1.0)
    assert metrics["missing"] == [] and metrics["redundant"] == []


def test_suite_id_changes_when_a_script_changes():
    base = BehaviorCase(name="identity", prompt="保存一份 brief。", samples=[[say("已保存。")]])
    assert suite_id([base]) == suite_id([base])
    assert suite_id([base]) != suite_id([replace(base, prompt="保存两份 brief。")])
    assert suite_id([base]) != suite_id([replace(base, samples=[[say("已保存两份。")]])])
    assert suite_id([base]) != suite_id([replace(base, samples=[[tool("project_read", {"offset": 1}, "a")]])])
    assert suite_id([base]) != suite_id([replace(base, group="measure")])


async def test_k_larger_than_samples_is_rejected(tmp_path):
    case = BehaviorCase(
        name="two-samples",
        prompt="只回答。",
        samples=[[say("一")], [say("二")]],
    )
    with pytest.raises(AppError) as error:
        await evaluate([case], home=tmp_path / "data", k=3)
    assert error.value.code == "EVAL_K_TOO_LARGE"
    assert not (tmp_path / "data").exists()

    with pytest.raises(AppError) as error:
        await evaluate([case], home=tmp_path / "data", k=0)
    assert error.value.code == "EVAL_K_INVALID"

    with pytest.raises(AppError) as error:
        await evaluation.run_suite(options(tmp_path, k=3))
    assert error.value.code == "EVAL_K_TOO_LARGE"
    assert not (tmp_path / "data").exists() and not (tmp_path / "report.json").exists()


async def test_pass_at_1_decides_the_gate(tmp_path):
    passing = BehaviorCase(
        name="gate-passing",
        group="gate",
        prompt="只回答。",
        expected_tools=[],
        expect_artifacts=0,
        samples=[[say("好")], [say("好")]],
    )
    mixed = BehaviorCase(
        name="measure-mixed",
        group="measure",
        prompt="只回答。",
        expected_tools=[],
        expect_artifacts=0,
        samples=[[say("好")], [tool("project_read", {}, "a"), say("好")]],
    )

    report = await evaluate([passing, mixed], home=tmp_path / "one", k=2)
    assert report["status"] == "passed"
    assert report["cases"][1]["pass@1"] == 0.5
    assert report["totals"]["measurePass@1"] == 0.5 and report["totals"]["failedGates"] == []

    # A gate that passes every scripted sample passes at k=1 and k=2: the old suite
    # flipped its status when k changed because it padded with the last script.
    assert (await evaluate([passing], home=tmp_path / "two", k=1))["status"] == "passed"
    assert (await evaluate([passing], home=tmp_path / "three", k=2))["status"] == "passed"

    # A 50% gate fails, and a failed gate is named in the report.
    failing = replace(passing, name="gate-mixed", samples=mixed.samples)
    failed = await evaluate([failing], home=tmp_path / "four", k=2)
    assert failed["status"] == "failed" and failed["totals"]["failedGates"] == ["gate-mixed"]

    # A measure-only run is not a release gate and cannot report "passed".
    measure_only = await evaluate([mixed], home=tmp_path / "five", k=2)
    assert measure_only["status"] == "failed" and measure_only["totals"]["gateCases"] == 0


NEGATIVE_CASES = {
    "completed": BehaviorCase(
        name="negative-completed",
        prompt="保存一份 brief。",
        expected_tools=[ExpectedCall("artifact_save", {"kind": "brief"})],
        expect_artifacts=1,
        samples=[[tool("artifact_save", {"kind": "brief", "title": "创作方案", "content": "正文"}, "a")]],
    ),
    "tools": BehaviorCase(
        name="negative-tools",
        prompt="只回答，不要读取任何状态。",
        expected_tools=[],
        expect_artifacts=0,
        samples=[[tool("project_read", {}, "a"), say("没有可读内容。")]],
    ),
    "artifacts": BehaviorCase(
        name="negative-artifacts",
        prompt="只回答，不要保存产物。",
        expected_tools=[],
        expect_artifacts=0,
        allowed_redundant=1,
        samples=[
            [
                tool("artifact_save", {"kind": "brief", "title": "创作方案", "content": "正文"}, "a"),
                say("已保存。"),
            ]
        ],
    ),
    "project": BehaviorCase(
        name="negative-project",
        prompt="把创作目标更新为「另一个目标」。",
        expected_tools=[ExpectedCall("project_update", {"goal": "另一个目标"})],
        expect_project={"goal": "期望目标"},
        samples=[
            [tool("project_update", {"expectedRevision": 0, "goal": "另一个目标"}, "a"), say("已更新。")]
        ],
    ),
}


@pytest.mark.parametrize("condition", ["completed", "artifacts", "project", "tools"])
async def test_scorer_check_is_required(tmp_path, condition):
    case = NEGATIVE_CASES[condition]
    sample = await run_sample(case, case.samples[0], home=tmp_path / condition, index=0, policy=None)
    assert sample["checks"][condition] is False
    assert [key for key, value in sample["checks"].items() if not value] == [condition]
    assert sample["passed"] is False


async def test_replay_model_requires_tools_for_tool_scripts():
    scripted_tool_call = ReplayModel([tool("project_read", {}, "a")])
    with pytest.raises(AppError) as error:
        await scripted_tool_call.generate([], [])
    assert error.value.code == "REPLAY_SCRIPT_NEEDS_TOOLS"

    plain = ReplayModel([say("只回答。")])
    assert (await plain.generate([], [])).content == "只回答。"


async def test_pass_at_k_and_variance_come_from_the_samples(tmp_path):
    case = BehaviorCase(
        name="sample-mix",
        group="measure",
        prompt="保存一份 brief。",
        expected_tools=[ExpectedCall("artifact_save", {"kind": "brief"})],
        expect_artifacts=1,
        samples=[
            [tool("project_read", {}, "a"), AIMessage(content="我不保存。")],
            [
                tool("artifact_save", {"kind": "brief", "title": "t", "content": "c"}, "a"),
                AIMessage(content="已保存。"),
            ],
        ],
    )
    report = await evaluate([case], home=tmp_path / "mix", k=2)
    entry = report["cases"][0]
    assert entry["pass@1"] == 0.5 and entry["pass@k"] == 1.0 and entry["variance"] == 0.25
    assert [sample["passed"] for sample in entry["samples"]] == [False, True]
    assert entry["samples"][0]["metrics"]["redundant"] and entry["samples"][0]["metrics"]["missing"]


def test_rubric_judge_is_deterministic_and_never_gates():
    judge = RubricJudge()
    scored = judge.score("已保存，artifactId 见工具结果。", ("artifactId", "版本号"))
    assert scored == {
        "judge": "offline-rubric-v1",
        "score": 0.5,
        "hits": ["artifactId"],
        "rubric": ["artifactId", "版本号"],
    }
    assert judge.score("x", ())["score"] == 1.0
    assert judge.score("", ("a",))["score"] == 0.0


async def test_judge_scores_are_recorded_but_do_not_change_pass(tmp_path):
    with_judge = await evaluate(evaluation.CASES, home=tmp_path / "j1", k=2, judge=RubricJudge())
    without = await evaluate(evaluation.CASES, home=tmp_path / "j2", k=2)
    assert with_judge["totals"]["judge"]["mode"] == "offline-rubric-v1"
    assert [sample["passed"] for entry in with_judge["cases"] for sample in entry["samples"]] == [
        sample["passed"] for entry in without["cases"] for sample in entry["samples"]
    ]
    assert "judge" not in without["cases"][0]


def test_evaluation_module_is_offline_and_deterministic_by_construction():
    source = Path(__file__).resolve().parents[1] / "src" / "vagent" / "behavior.py"
    text = source.read_text(encoding="utf-8")
    for forbidden in ("httpx", "requests", "socket", "DeepSeekModel", "random"):
        assert forbidden not in text
