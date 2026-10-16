import copy
import hashlib
import json
from argparse import Namespace
from pathlib import Path

import pytest
from langchain_core.messages import ToolMessage

from scripts import evaluate_m1c as evaluation
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
    args = options(tmp_path_factory.mktemp("m1c-evaluation"))
    report = await evaluation.run_suite(args)
    assert report["status"] == "passed"
    return args, report


async def test_m1c_evaluation_records_real_media_delivery_and_independent_ledger(accepted_suite):
    args, report = accepted_suite
    assert report["mode"] == "offline-fixture"
    assert report["totals"]["passedCases"] == report["totals"]["plannedCases"] == 7
    assert report["totals"]["modelCalls"] == 26 and report["totals"]["toolCalls"] == 21
    assert report["totals"]["submitCalls"] == 6 and report["totals"]["queryCalls"] == 5
    assert report["totals"]["downloadCalls"] == 6 and report["totals"]["mediaDelivered"] == 3
    assert report["totals"]["callsWithUnknownUsage"] == 26
    assert not report["totals"]["tokenUsageComplete"]
    assert json.loads(args.output.read_text(encoding="utf-8")) == report
    assert evaluation.evidence_hashes(args.home) == report["evidenceHashes"]
    assert not (args.home / "instance.lock").exists()
    fixture = hashlib.sha256(evaluation.FIXTURE.read_bytes()).hexdigest()
    for item in (report["cases"][0], report["cases"][1], report["cases"][5]):
        assert item["jobs"][0]["status"] == "succeeded"
        assert item["assets"][0]["sha256"] == fixture
        assert item["mediaChecks"]["assetSha256MatchesFile"] is True
        assert item["mediaApi"] and all(item["mediaApi"].values())
        assert item["upstream"]["hasAuthorization"] is True
        assert item["upstream"]["asyncHeader"] == "enable"
        assert item["upstream"]["downloadHosts"] == ["dashscope-result-sh.oss-accelerate.aliyuncs.com"]
        assert "Signature" not in json.dumps(item["jobs"], ensure_ascii=False)
    first = report["cases"][0]
    events = [event for event in first["applicationEvents"] if event["type"] == "job.updated"]
    assert [event["revision"] for event in events] == sorted({event["revision"] for event in events})
    source = report["cases"][1]
    assert source["jobs"][0]["request"]["sourceRefs"] == [{"artifactId": source["sourceId"], "version": 1}]
    assert source["assets"][0]["sourceRefs"] == [{"artifactId": source["sourceId"], "version": 1}]
    assert len(source["waits"]) == 1 and source["waits"][0]["status"] == "delivered"
    assert source["waitingSamples"] and all(sample["unchanged"] for sample in source["waitingSamples"])
    recovered = report["cases"][5]
    assert recovered["downloadFailure"]["download"]["phase"] == "failed"
    assert recovered["retryDownload"]["download"]["generation"] == 2
    assert recovered["jobs"][0]["download"]["attempts"] == 4
    assert report["videoSettings"]["estimate"]["amount"] == "3.00"
    assert report["videoSettings"]["spec"] == {
        "durationSeconds": 5,
        "resolution": "720p",
        "aspectRatio": "16:9",
    }


@pytest.mark.parametrize(
    ("index", "corruption", "failed_check"),
    [
        (0, "extra-submit", "submitted_once"),
        (0, "foreign-task-id", "original_task_identity"),
        (0, "false-completion", "succeeded"),
        (0, "broken-media-file", "media_matches_local_file"),
        (0, "leaked-signature", "private_url_not_exposed"),
        (1, "changed-source-version", "source_version_preserved"),
        (1, "undelivered-job", "succeeded"),
        (1, "media-api-failure", "media_api_ok"),
        (1, "operation-result-mismatch", "durable_deliveries_match"),
        (2, "hidden-failure", "actual_failure_delivered"),
        (2, "wrong-generation-error", "generation_failed"),
        (3, "wrong-submit-stage", "submit_failed"),
        (4, "claimed-task-id", "submission_unknown"),
        (5, "no-manual-retry", "recovered_after_retry"),
        (5, "fabricated-recovery-claim", "failure_reported"),
        (6, "generation-attempted", "no_generation_attempt"),
    ],
)
def test_m1c_grader_rejects_fabricated_or_inconsistent_evidence(
    accepted_suite, index, corruption, failed_check
):
    _, report = accepted_suite
    item = copy.deepcopy(report["cases"][index])
    if corruption == "extra-submit":
        item["upstream"]["submitCalls"] += 1
    elif corruption == "foreign-task-id":
        item["jobs"][0]["providerTaskId"] = "unrelated-task"
    elif corruption == "false-completion":
        item["jobs"][0]["status"] = "downloading"
    elif corruption == "broken-media-file":
        item["mediaChecks"]["assetSha256MatchesFile"] = False
    elif corruption == "leaked-signature":
        item["toolResults"].append(
            {"id": "x", "name": "job_get", "result": {"url": "https://x?Signature=leak"}}
        )
    elif corruption == "changed-source-version":
        item["jobs"][0]["request"]["sourceRefs"][0]["version"] = 2
    elif corruption == "undelivered-job":
        item["jobs"][0]["status"] = "running"
        item["jobs"][0]["download"] = None
        item["assets"] = []
        item["mediaChecks"] = {}
        item["mediaApi"] = None
    elif corruption == "media-api-failure":
        item["mediaApi"]["get200"] = False
    elif corruption == "operation-result-mismatch":
        key = ToolExecutionContext.model_validate(item["waits"][0]["context"]).operation_key
        item["operations"][key]["result"] = {"ok": True, "data": "invented"}
    elif corruption == "hidden-failure":
        next(r for r in item["toolResults"] if r["name"] == "await_job")["result"] = {"ok": True, "data": {}}
    elif corruption == "wrong-generation-error":
        item["jobs"][0]["error"]["code"] = "PROVIDER_CANCELED"
    elif corruption == "wrong-submit-stage":
        item["jobs"][0]["error"]["stage"] = "generate"
    elif corruption == "claimed-task-id":
        item["jobs"][0]["providerTaskId"] = "wan-fixture-submit-unknown"
    elif corruption == "no-manual-retry":
        item["jobs"][0]["download"]["attempts"] = 3
    elif corruption == "fabricated-recovery-claim":
        item["run"]["answer"] = "媒体已可播放，点击下载。"
    elif corruption == "generation-attempted":
        item["run"]["toolTrace"].append(
            {"id": "extra", "name": "video_generate", "arguments": {}, "result": None}
        )
    assert not evaluation.grade(evaluation.CASES[index], item, offline=True)[failed_check]


async def test_m1c_offline_ignores_paid_models_external_services_and_user_video_mode(tmp_path, monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Offline evaluation must not instantiate a paid model or connect Redis")

    monkeypatch.setenv("DEEPSEEK_API_KEY", "must-not-appear-in-evidence")
    monkeypatch.setenv("VAGENT_VIDEO_MODE", "mock")
    monkeypatch.setenv("VAGENT_MCP_CONFIG", str(tmp_path / "nonexistent.json"))
    monkeypatch.setenv("VAGENT_REDIS_URL", "redis://127.0.0.1:1")
    monkeypatch.setattr("vagent.application.DeepSeekModel", forbidden)
    monkeypatch.setattr("vagent.application.AnswerCache.connect", forbidden)
    args = options(tmp_path, limit=4)
    report = await evaluation.run_suite(args)
    assert report["status"] == "budget-exhausted" and report["totals"]["modelCalls"] == 4
    assert report["totals"]["passedCases"] == 1 and len(report["cases"]) == 1
    assert "must-not-appear-in-evidence" not in args.output.read_text(encoding="utf-8")


async def test_m1c_stops_on_first_failure_with_attempts_accounted(tmp_path):
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
        if not self.failed:
            delivered = [
                message
                for message in messages
                if isinstance(message, ToolMessage) and message.name == "await_job"
            ]
            if delivered and json.loads(delivered[-1].content).get("ok"):
                self.failed = True
                raise AppError("EXECUTION_ERROR", "Injected response loss after durable delivery")
        return await super().generate(messages, tools)


async def test_m1c_explicit_continuation_retains_run_failure_evidence_and_suite_budget(tmp_path, monkeypatch):
    model = FailsAfterDelivery()
    monkeypatch.setattr(evaluation, "FixtureModel", lambda: model)
    args = options(tmp_path, limit=40)
    first = await evaluation.run_suite(args)
    assert first["status"] == "failed" and len(first["cases"]) == 2
    assert first["totals"]["modelCalls"] == 8
    failed = first["cases"][-1]
    assert failed["run"]["resumable"] and failed["waits"][0]["status"] == "delivered"
    original_bytes = args.output.read_bytes()

    restricted = options(tmp_path / "limited", limit=8)
    restricted.continue_from = args.output
    stopped = await evaluation.run_suite(restricted)
    assert stopped["status"] == "budget-exhausted" and stopped["totals"]["modelCalls"] == 8
    assert not stopped["resumes"]

    resumed_args = options(tmp_path / "resumed", limit=40)
    resumed_args.continue_from = args.output
    final = await evaluation.run_suite(resumed_args)
    assert final["status"] == "passed" and final["totals"]["modelCalls"] == 27
    assert final["maxModelCalls"] == 40 and final["totals"]["explicitResumes"] == 1
    assert args.output.read_bytes() == original_bytes
    resumed = final["cases"][1]
    assert resumed["run"]["id"] == failed["run"]["id"]
    assert resumed["run"]["policy"] == failed["run"]["policy"]
    assert resumed["jobs"] == failed["jobs"] and resumed["waits"] == failed["waits"]
    assert resumed["run"]["modelCalls"][:4] == failed["run"]["modelCalls"]
    assert resumed["upstream"]["submitCalls"] == 1
    assert any(event["type"] == "model.failed" for event in resumed["run"]["events"])
    assert final["resumes"][0]["suiteCallsBefore"] == 8


async def test_m1c_continuation_requires_matching_mode_and_evidence(tmp_path, monkeypatch):
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
    ledger = args.home / "m1c-fixture.json"
    ledger.write_bytes(ledger.read_bytes() + b"\n")
    with pytest.raises(AppError) as error:
        await evaluation.run_suite(resumed_args)
    assert error.value.code == "EVAL_STATE_CHANGED"
    assert not resumed_args.output.exists()


@pytest.mark.parametrize("limit", [0, 41, 64])
async def test_m1c_cannot_raise_suite_ceiling(tmp_path, limit):
    with pytest.raises(AppError) as error:
        await evaluation.run_suite(options(tmp_path, limit=limit))
    assert error.value.code == "EVAL_BUDGET_INVALID"


async def test_m1c_does_not_overwrite_reports_or_reuse_existing_data(tmp_path, accepted_suite):
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
