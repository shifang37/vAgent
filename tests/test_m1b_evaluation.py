import copy
import json
from argparse import Namespace
from pathlib import Path

import pytest
from langchain_core.messages import HumanMessage, ToolMessage

from scripts import evaluate_m1b as evaluation
from vagent.errors import AppError
from vagent.waiting import ToolExecutionContext


def options(tmp_path, *, limit=40):
    return Namespace(
        home=tmp_path / "data",
        output=tmp_path / "report.json",
        live=False,
        context_bytes=65536,
        max_model_calls=limit,
        continue_from=None,
    )


@pytest.fixture(scope="module")
async def accepted_suite(tmp_path_factory):
    args = options(tmp_path_factory.mktemp("m1b-evaluation"))
    report = await evaluation.run_suite(args)
    assert report["status"] == "passed"
    return args, report


async def test_m1b_evaluation_records_real_application_state_and_independent_ledger(accepted_suite):
    args, report = accepted_suite
    assert report["mode"] == "offline-fixture"
    assert report["totals"]["passedCases"] == report["totals"]["plannedCases"] == 5
    assert report["totals"]["modelCalls"] == report["totals"]["callsWithUnknownUsage"] == 14
    assert not report["totals"]["tokenUsageComplete"]
    assert report["totals"]["observedInputTokens"] == report["totals"]["observedOutputTokens"] == 0
    assert report["totals"]["toolCalls"] == 10
    assert report["totals"]["submitCalls"] == 3 and report["totals"]["queryCalls"] == 6
    assert report["totals"]["automaticResumes"] == 2
    assert report["totals"]["externalWaitSeconds"] > 0
    assert json.loads(args.output.read_text(encoding="utf-8")) == report
    assert evaluation.evidence_hashes(args.home) == report["evidenceHashes"]
    assert not (args.home / "instance.lock").exists()
    for item in report["cases"][:3]:
        events = [event for event in item["applicationEvents"] if event["type"] == "job.updated"]
        assert {event["runId"] for event in events} == {item["run"]["id"]}
        assert [event["revision"] for event in events] == sorted({event["revision"] for event in events})
        assert events[-1]["status"] == item["jobs"][0]["status"]
    source = report["cases"][1]
    assert source["jobs"][0]["request"]["sourceRefs"] == [{"artifactId": source["sourceId"], "version": 1}]
    assert len(source["waits"]) == 1 and source["waits"][0]["status"] == "delivered"
    assert source["waitingSamples"] and all(sample["unchanged"] for sample in source["waitingSamples"])


@pytest.mark.parametrize(
    ("index", "corruption", "failed_check"),
    [
        (0, "duplicate-job", "unique_job"),
        (0, "extra-submit", "submitted_once"),
        (0, "foreign-provider-id", "submission_provenance"),
        (1, "duplicate-result", "one_result_per_original_call"),
        (1, "changed-source-version", "source_version_preserved"),
        (1, "made-up-job-id", "real_job_reference"),
        (1, "false-completion", "succeeded"),
        (1, "operation-result-mismatch", "durable_deliveries_match"),
        (2, "hidden-failure", "actual_failure_delivered"),
        (3, "unauthorized-write", "no_unrequested_text_changes"),
        (4, "visible-video-tools", "video_tools_hidden"),
    ],
)
def test_m1b_grader_rejects_fabricated_or_inconsistent_evidence(
    accepted_suite, index, corruption, failed_check
):
    _, report = accepted_suite
    item = copy.deepcopy(report["cases"][index])
    if corruption == "duplicate-job":
        item["jobs"].append(copy.deepcopy(item["jobs"][0]))
    elif corruption == "extra-submit":
        item["upstream"]["submitCalls"] += 1
    elif corruption == "foreign-provider-id":
        item["upstream"]["submissions"][0]["taskId"] = "unrelated-task"
    elif corruption == "duplicate-result":
        item["toolResults"].append(copy.deepcopy(item["toolResults"][-1]))
    elif corruption == "changed-source-version":
        item["jobs"][0]["request"]["sourceRefs"][0]["version"] = 2
    elif corruption == "made-up-job-id":
        item["run"]["answer"] = "模拟任务 imaginary-job 已完成，没有真实媒体。"
    elif corruption == "false-completion":
        item["jobs"][0]["status"] = "running"
    elif corruption == "operation-result-mismatch":
        key = ToolExecutionContext.model_validate(item["waits"][0]["context"]).operation_key
        item["operations"][key]["result"] = {"ok": True, "data": "invented"}
    elif corruption == "hidden-failure":
        next(result for result in item["toolResults"] if result["name"] == "await_job")["result"] = {
            "ok": True,
            "data": {},
        }
    elif corruption == "unauthorized-write":
        item["after"]["project"]["audience"] = "changed without permission"
    elif corruption == "visible-video-tools":
        item["toolInventory"].append("video_generate")
    assert not evaluation.grade(evaluation.CASES[index], item, offline=True)[failed_check]


async def test_m1b_offline_ignores_paid_model_external_services_and_user_video_mode(tmp_path, monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Offline evaluation must not instantiate a paid model or connect Redis")

    monkeypatch.setenv("DEEPSEEK_API_KEY", "must-not-appear-in-evidence")
    monkeypatch.setenv("VAGENT_VIDEO_MODE", "mock")
    monkeypatch.setenv("VAGENT_MCP_CONFIG", str(tmp_path / "nonexistent.json"))
    monkeypatch.setenv("VAGENT_REDIS_URL", "redis://127.0.0.1:1")
    monkeypatch.setattr("vagent.application.DeepSeekModel", forbidden)
    monkeypatch.setattr("vagent.application.AnswerCache.connect", forbidden)
    args = options(tmp_path, limit=3)
    report = await evaluation.run_suite(args)
    assert report["status"] == "budget-exhausted" and report["totals"]["modelCalls"] == 3
    assert report["totals"]["passedCases"] == 1 and len(report["cases"]) == 1
    assert "must-not-appear-in-evidence" not in args.output.read_text(encoding="utf-8")


async def test_m1b_stops_on_first_failure_with_attempts_accounted(tmp_path):
    report = await evaluation.run_suite(options(tmp_path, limit=1))
    assert report["status"] == "failed" and len(report["cases"]) == 1
    assert report["totals"]["modelCalls"] == 1
    assert report["totals"]["passedCases"] == 0
    assert report["cases"][0]["run"]["errorCode"] == "STEP_LIMIT"
    assert report["cases"][0]["run"]["policy"]["maxSteps"] == 1


class FailsAfterDelivery(evaluation.FixtureModel):
    def __init__(self):
        self.failed = False

    async def generate(self, messages, tools):
        prompt = next(message.content for message in reversed(messages) if isinstance(message, HumanMessage))
        if prompt.startswith("读取当前项目brief") and not self.failed:
            if any(isinstance(message, ToolMessage) and message.name == "await_job" for message in messages):
                self.failed = True
                raise AppError("EXECUTION_ERROR", "Injected response loss after durable delivery")
        return await super().generate(messages, tools)


async def test_m1b_explicit_continuation_retains_run_failure_evidence_and_suite_budget(tmp_path, monkeypatch):
    model = FailsAfterDelivery()
    monkeypatch.setattr(evaluation, "FixtureModel", lambda: model)
    args = options(tmp_path, limit=16)
    first = await evaluation.run_suite(args)
    assert first["status"] == "failed" and len(first["cases"]) == 2
    assert first["totals"]["modelCalls"] == 7
    failed = first["cases"][-1]
    assert failed["run"]["resumable"] and failed["waits"][0]["status"] == "delivered"
    original_bytes = args.output.read_bytes()

    restricted = options(tmp_path / "limited", limit=7)
    restricted.continue_from = args.output
    stopped = await evaluation.run_suite(restricted)
    assert stopped["status"] == "budget-exhausted" and stopped["totals"]["modelCalls"] == 7
    assert not stopped["resumes"]

    resumed_args = options(tmp_path / "resumed", limit=40)
    resumed_args.continue_from = args.output
    final = await evaluation.run_suite(resumed_args)
    assert final["status"] == "passed" and final["totals"]["modelCalls"] == 15
    assert final["maxModelCalls"] == 16 and final["totals"]["explicitResumes"] == 1
    assert final["totals"]["automaticResumes"] == 2
    assert args.output.read_bytes() == original_bytes
    resumed = final["cases"][1]
    assert resumed["run"]["id"] == failed["run"]["id"]
    assert resumed["run"]["policy"] == failed["run"]["policy"]
    assert resumed["jobs"] == failed["jobs"] and resumed["waits"] == failed["waits"]
    assert resumed["run"]["modelCalls"][:4] == failed["run"]["modelCalls"]
    assert resumed["upstream"]["submitCalls"] == 1
    assert resumed["upstream"]["queryCalls"] == 2
    assert final["resumes"][0]["suiteCallsBefore"] == 7
    assert len(json.loads((args.home / "state.json").read_text(encoding="utf-8"))["runs"]) == 5
    assert any(event["type"] == "model.failed" for event in resumed["run"]["events"])

    stale_args = options(tmp_path / "stale")
    stale_args.continue_from = args.output
    with pytest.raises(AppError, match="原评测数据") as error:
        await evaluation.run_suite(stale_args)
    assert error.value.code == "EVAL_STATE_CHANGED"
    assert not stale_args.output.exists()


async def test_m1b_continuation_requires_explicit_live_and_matching_evidence(tmp_path, monkeypatch):
    model = FailsAfterDelivery()
    monkeypatch.setattr(evaluation, "FixtureModel", lambda: model)
    args = options(tmp_path)
    failed = await evaluation.run_suite(args)
    changed = copy.deepcopy(failed)
    changed["mode"] = "live"
    live_report = tmp_path / "live.json"
    live_report.write_text(json.dumps(changed), encoding="utf-8")
    resumed_args = options(tmp_path / "resume")
    resumed_args.continue_from = live_report
    with pytest.raises(AppError) as error:
        await evaluation.run_suite(resumed_args)
    assert error.value.code == "EVAL_CONTINUE_INVALID"

    resumed_args.continue_from = args.output
    ledger = args.home / "mock-video.json"
    ledger.write_bytes(ledger.read_bytes() + b"\n")
    with pytest.raises(AppError) as error:
        await evaluation.run_suite(resumed_args)
    assert error.value.code == "EVAL_STATE_CHANGED"
    assert not resumed_args.output.exists()


@pytest.mark.parametrize("limit", [0, 41, 64])
async def test_m1b_cannot_raise_suite_ceiling(tmp_path, limit):
    with pytest.raises(AppError) as error:
        await evaluation.run_suite(options(tmp_path, limit=limit))
    assert error.value.code == "EVAL_BUDGET_INVALID"


async def test_m1b_does_not_overwrite_reports_or_reuse_existing_data(tmp_path, accepted_suite):
    args, report = accepted_suite
    original = args.output.read_bytes()
    with pytest.raises(AppError) as error:
        await evaluation.run_suite(args)
    assert error.value.code == "EVAL_OUTPUT_EXISTS" and args.output.read_bytes() == original
    other = options(tmp_path)
    other.home = Path(report["dataDirectory"])
    with pytest.raises(AppError) as error:
        await evaluation.run_suite(other)
    assert error.value.code == "EVAL_HOME_NOT_EMPTY" and not other.output.exists()
