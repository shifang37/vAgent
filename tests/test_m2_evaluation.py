"""The M2 acceptance suite: offline multi-shot evidence, budgets and honest grading."""

import copy
import hashlib
import json
from argparse import Namespace

import pytest

from scripts import evaluate_m2 as evaluation
from vagent.errors import AppError


def options(tmp_path, *, limit=60):
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
    args = options(tmp_path_factory.mktemp("m2-evaluation"))
    report = await evaluation.run_suite(args)
    assert report["status"] == "passed", [
        (item["case"], [key for key, value in item["checks"].items() if not value])
        for item in report["cases"]
        if item.get("checks") and not all(item["checks"].values())
    ]
    return args, report


async def test_m2_evaluation_records_multishot_delivery_redos_and_refusals(accepted_suite):
    args, report = accepted_suite
    assert report["mode"] == "offline-fixture"
    assert report["totals"]["passedCases"] == report["totals"]["plannedCases"] == 7
    assert report["totals"]["submitCalls"] == 10 and report["totals"]["shotsDelivered"] == 9
    assert report["totals"]["redos"] == 2
    assert report["totals"]["callsWithUnknownUsage"] == report["totals"]["modelCalls"]
    assert not report["totals"]["tokenUsageComplete"]
    assert json.loads(args.output.read_text(encoding="utf-8")) == report
    assert evaluation.evidence_hashes(args.home) == report["evidenceHashes"]
    assert not (args.home / "instance.lock").exists()

    fixture = hashlib.sha256(evaluation.FIXTURE.read_bytes()).hexdigest()
    batch = report["cases"][1]
    assert len(batch["jobs"]) == 3 and all(job["status"] == "succeeded" for job in batch["jobs"])
    assert batch["assets"] and all(asset["sha256"] == fixture for asset in batch["assets"])
    assert all(all(flags.values()) for flags in batch["mediaChecks"].values())
    assert batch["mediaApi"] and all(all(checks.values()) for checks in batch["mediaApi"].values())
    assert batch["upstream"]["hasAuthorization"] is True
    assert batch["upstream"]["asyncHeader"] == "enable"
    assert batch["upstream"]["downloadHosts"] == ["dashscope-result-sh.oss-accelerate.aliyuncs.com"]
    assert "Signature" not in json.dumps(batch["jobs"], ensure_ascii=False)
    assert "workspaceId" not in json.dumps(batch["toolResults"], ensure_ascii=False)
    # The reference image travels as a Base64 data URL and i2v sends no ratio.
    image_bodies = [body for body in batch["upstream"]["submitBodies"] if "media" in body["input"]]
    assert len(image_bodies) == 1
    assert image_bodies[0]["input"]["media"][0]["type"] == "first_frame"
    assert image_bodies[0]["input"]["media"][0]["url"].startswith("data:image/png;base64,")
    assert "ratio" not in image_bodies[0]["parameters"]
    # One durable set wait covers the batch; waiting never consumed run budget.
    assert [binding["resource"]["kind"] for binding in batch["waits"]] == ["jobset"]
    assert batch["waits"][0]["status"] == "delivered"
    assert batch["waitingSamples"] and all(sample["unchanged"] for sample in batch["waitingSamples"])
    assert batch["checks"]["replay_deduplicated"] is True
    assert batch["checks"]["batch_echoed"] is True

    partial = report["cases"][2]
    assert sorted(job["status"] for job in partial["runJobs"]) == ["failed", "succeeded", "succeeded"]
    assert len(partial["jobs"]) == len(partial["runJobs"]) + 1
    assert partial["redo"]["versionCountDelta"][partial["redo"]["shotId"]] == 1
    assert partial["redo"]["delivered"]["status"] == "succeeded"
    assert partial["redo"]["delivered"]["mediaAvailable"] is True

    redo = report["cases"][3]
    assert redo["checks"]["redo_left_other_jobs_untouched"] is True
    assert redo["checks"]["redo_replay_is_idempotent"] is True
    assert redo["checks"]["stale_redo_refused"] is True
    assert len(redo["jobs"]) == len(redo["runJobs"]) + 1

    for item in (report["cases"][4], report["cases"][5], report["cases"][6]):
        assert not item["jobs"] and item["upstream"]["submitCalls"] == 0
        assert not any(item["shotVersions"].values())
    assert report["cases"][4]["checks"]["batch_refused"] is True
    assert report["cases"][6]["checks"]["confirmation_required"] is True
    assert report["videoSettings"]["capabilitiesVersion"] == "wan27-shot-beijing-v2"
    assert report["videoSettings"]["maxNewJobsPerRun"] == 4


@pytest.mark.parametrize(
    ("index", "corruption", "failed_check"),
    [
        (0, "estimate-not-echoed", "estimate_echoed"),
        (0, "generation-attempted", "no_generation_attempt"),
        (1, "extra-submit", "submit_accounted"),
        (1, "foreign-task-id", "original_task_identity"),
        (1, "false-completion", "all_delivered"),
        (1, "broken-media-file", "media_matches_local_file"),
        (1, "leaked-signature", "private_url_not_exposed"),
        (1, "replay-registered-again", "replay_deduplicated"),
        (1, "wrong-quota", "batch_echoed"),
        (1, "wrong-price-version", "cost_recorded"),
        (2, "hidden-failure", "partial_summary_delivered"),
        (2, "redone-job-moved-others", "redo_left_other_jobs_untouched"),
        (3, "redo-moved-another-shot", "redo_added_one_shot_version"),
        (3, "redone-rewrote-other-media", "redo_left_other_media_untouched"),
        (4, "limit-not-enforced", "batch_refused"),
        (5, "generation-attempted", "no_generation_attempt"),
        (6, "confirmation-given", "confirmation_required"),
    ],
)
def test_m2_grader_rejects_fabricated_or_inconsistent_evidence(
    accepted_suite, index, corruption, failed_check
):
    _, report = accepted_suite
    item = copy.deepcopy(report["cases"][index])
    if corruption == "estimate-not-echoed":
        item["run"]["answer"] = "分镜草案已保存，未登记任务。"
    elif corruption == "generation-attempted":
        item["run"]["toolTrace"].append(
            {"id": "extra", "name": "shot_generate", "arguments": {}, "result": None}
        )
    elif corruption == "extra-submit":
        item["runUpstream"]["submitCalls"] += 1
    elif corruption == "foreign-task-id":
        item["runJobs"][0]["providerTaskId"] = "unrelated-task"
    elif corruption == "false-completion":
        item["runJobs"][0]["status"] = "downloading"
        item["runJobs"][0]["mediaAvailable"] = False
    elif corruption == "broken-media-file":
        next(iter(item["mediaChecks"].values()))["assetSha256MatchesFile"] = False
    elif corruption == "leaked-signature":
        item["toolResults"].append(
            {"id": "x", "name": "job_get", "result": {"url": "https://x?Signature=leak"}}
        )
    elif corruption == "replay-registered-again":
        generates = [result for result in item["toolResults"] if result["name"] == "shot_generate"]
        for shot in generates[-1]["result"]["data"]["shots"]:
            shot["reused"] = False
    elif corruption == "wrong-quota":
        generate = next(result for result in item["toolResults"] if result["name"] == "shot_generate")
        generate["result"]["data"]["limits"]["usedNewJobs"] = 4
    elif corruption == "wrong-price-version":
        item["runJobs"][0]["cost"]["estimate"]["priceVersion"] = "wan27-t2v-beijing-v1"
    elif corruption == "hidden-failure":
        next(result for result in item["toolResults"] if result["name"] == "await_job")["result"] = {
            "ok": True,
            "data": {
                "jobIds": [],
                "jobs": [],
                "succeeded": 3,
                "failed": 0,
                "summary": "共 3 个镜头任务：3 个可交付，0 个未完成。",
            },
        }
    elif corruption == "redone-job-moved-others":
        target = item["redo"]["shotId"]
        new_job = item["redo"]["body"]["shots"][0]["jobId"]
        other = next(job_id for job_id in item["redo"]["after"] if job_id != new_job)
        item["redo"]["after"][other]["identity"]["status"] = "running"
        assert other != target
    elif corruption == "redo-moved-another-shot":
        delta = item["redo"]["versionCountDelta"]
        other = next(shot_id for shot_id in delta if shot_id != item["redo"]["shotId"])
        delta[other] = 1
    elif corruption == "redone-rewrote-other-media":
        hashes = item["redo"]["mediaHashes"]
        other = next(iter(hashes["before"]))
        hashes["after"][other] = "0" * 64
    elif corruption == "limit-not-enforced":
        refusals = [result for result in item["toolResults"] if result["name"] == "shot_generate"]
        refusals[-1]["result"]["error"]["code"] = "VIDEO_RUN_COST_LIMIT"
    elif corruption == "confirmation-given":
        for call in item["run"]["toolTrace"]:
            if call["name"] == "shot_generate":
                call["arguments"]["confirm"] = True
    assert not evaluation.grade(evaluation.CASES[index], item, offline=True)[failed_check]


async def test_m2_offline_ignores_paid_models_external_services_and_user_video_mode(tmp_path, monkeypatch):
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


async def test_m2_stops_on_first_failure_with_attempts_accounted(tmp_path):
    report = await evaluation.run_suite(options(tmp_path, limit=1))
    assert report["status"] == "failed" and len(report["cases"]) == 1
    assert report["totals"]["modelCalls"] == 1 and report["totals"]["passedCases"] == 0
    assert report["cases"][0]["run"]["errorCode"] == "STEP_LIMIT"
    assert report["cases"][0]["run"]["policy"]["maxSteps"] == 1


class FailsAfterDelivery(evaluation.FixtureModel):
    """Lose the response only after the durable set wait was already delivered."""

    def __init__(self):
        self.failed = False

    async def generate(self, messages, tools):
        if not self.failed:
            delivered = [
                message
                for message in messages
                if isinstance(message, evaluation.ToolMessage) and message.name == "await_job"
            ]
            if delivered and json.loads(delivered[-1].content).get("ok"):
                self.failed = True
                raise AppError("EXECUTION_ERROR", "Injected response loss after durable delivery")
        return await super().generate(messages, tools)


async def test_m2_explicit_continuation_retains_run_failure_evidence_and_suite_budget(tmp_path, monkeypatch):
    model = FailsAfterDelivery()
    monkeypatch.setattr(evaluation, "FixtureModel", lambda: model)
    args = options(tmp_path)
    first = await evaluation.run_suite(args)
    assert first["status"] == "failed" and len(first["cases"]) == 2
    failed = first["cases"][-1]
    assert failed["run"]["resumable"] and failed["waits"][0]["status"] == "delivered"
    assert len(failed["jobs"]) == 3
    original_bytes = args.output.read_bytes()

    restricted = options(tmp_path / "limited", limit=8)
    restricted.continue_from = args.output
    stopped = await evaluation.run_suite(restricted)
    assert stopped["status"] == "budget-exhausted" and not stopped["resumes"]

    resumed_args = options(tmp_path / "resumed")
    resumed_args.continue_from = args.output
    final = await evaluation.run_suite(resumed_args)
    assert final["status"] == "passed" and final["totals"]["explicitResumes"] == 1
    assert args.output.read_bytes() == original_bytes
    resumed = final["cases"][1]
    assert resumed["run"]["id"] == failed["run"]["id"]
    assert resumed["run"]["policy"] == failed["run"]["policy"]
    assert resumed["run"]["modelCalls"][: len(failed["run"]["modelCalls"])] == failed["run"]["modelCalls"]
    assert resumed["runJobs"] == failed["runJobs"] and resumed["waits"] == failed["waits"]
    assert resumed["runUpstream"]["submitCalls"] == 3
    assert any(event["type"] == "model.failed" for event in resumed["run"]["events"])


async def test_m2_continuation_requires_matching_mode_and_evidence(tmp_path, monkeypatch):
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
    ledger = args.home / "m2-fixture.json"
    ledger.write_bytes(ledger.read_bytes() + b"\n")
    with pytest.raises(AppError) as error:
        await evaluation.run_suite(resumed_args)
    assert error.value.code == "EVAL_STATE_CHANGED"
    assert not resumed_args.output.exists()


@pytest.mark.parametrize("limit", [0, 61, 128])
async def test_m2_cannot_raise_suite_ceiling(tmp_path, limit):
    with pytest.raises(AppError) as error:
        await evaluation.run_suite(options(tmp_path, limit=limit))
    assert error.value.code == "EVAL_BUDGET_INVALID"


async def test_m2_does_not_overwrite_reports_or_reuse_existing_data(tmp_path, accepted_suite):
    args, report = accepted_suite
    original = args.output.read_bytes()
    with pytest.raises(AppError) as error:
        await evaluation.run_suite(args)
    assert error.value.code == "EVAL_OUTPUT_EXISTS" and args.output.read_bytes() == original
    other = options(tmp_path)
    other.home = args.home
    with pytest.raises(AppError) as error:
        await evaluation.run_suite(other)
    assert error.value.code == "EVAL_HOME_NOT_EMPTY" and not other.output.exists()
