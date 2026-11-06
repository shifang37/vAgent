"""H4 offline behavior-evaluation suite: scoring, pass@k, judging, report discipline."""

import json
from argparse import Namespace
from pathlib import Path

import pytest
from langchain_core.messages import AIMessage

from scripts import evaluate_behavior as evaluation
from vagent.behavior import (
    BehaviorCase,
    ExpectedCall,
    RubricJudge,
    evaluate,
    score_calls,
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


@pytest.fixture(scope="module")
async def accepted_suite(tmp_path_factory):
    args = options(tmp_path_factory.mktemp("behavior-evaluation"))
    report = await evaluation.run_suite(args)
    return args, report


async def test_suite_runs_every_case_offline(accepted_suite):
    args, report = accepted_suite
    assert report["status"] == "passed"
    assert report["mode"] == "offline-fixture"
    assert report["totals"]["cases"] == 3
    assert report["totals"]["samples"] == 6 and report["totals"]["k"] == 2
    assert report["totals"]["pass@1"] == 0.5 and report["totals"]["pass@k"] == 1.0
    assert report["totals"]["variance"] == 0.25
    assert report["totals"]["realProviderCalls"] == 0 and report["totals"]["textModelCalls"] == 0
    assert report["totals"]["redundantCalls"] > 0 and report["totals"]["missingCalls"] > 0
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


async def test_pass_at_k_and_variance_come_from_the_samples(tmp_path):
    case = BehaviorCase(
        name="sample-mix",
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
