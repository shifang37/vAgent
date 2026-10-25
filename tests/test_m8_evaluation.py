"""M8 E4 offline acceptance suite: route matrix, refusals, billing and replay."""

import json
from argparse import Namespace

import pytest

from scripts import evaluate_m8 as evaluation
from vagent.errors import AppError


def options(tmp_path, *, live=False, only=None):
    return Namespace(
        home=tmp_path / "data",
        output=tmp_path / "report.json",
        live=live,
        only=only or [],
    )


@pytest.fixture(scope="module")
async def accepted_suite(tmp_path_factory):
    args = options(tmp_path_factory.mktemp("m8-evaluation"))
    report = await evaluation.run_suite(args)
    assert report["status"] == "passed", [
        (item["case"], [key for key, value in item["checks"].items() if not value])
        for item in report["cases"]
        if item.get("checks") and not all(item["checks"].values())
    ]
    return args, report


async def test_m8_evaluation_records_the_route_billing_and_replay_facts(accepted_suite):
    args, report = accepted_suite
    assert report["mode"] == "offline-fixture"
    assert report["totals"]["plannedCases"] == report["totals"]["passedCases"] == 4
    assert report["totals"]["frozenProbeRows"] == 4
    assert report["totals"]["matrixRows"] == 18
    assert report["totals"]["previews"] == 38
    assert report["totals"]["controlledSubmits"] == 6
    assert report["totals"]["jobs"] == 7
    assert report["totals"]["billWrites"] == 3
    assert report["totals"]["billReplays"] == 3
    assert report["totals"]["ledgerRecords"] == 1
    assert report["totals"]["realProviderCalls"] == 0
    assert report["totals"]["textModelCalls"] == 0
    assert report["totals"]["ttsRealRequests"] == 0
    assert json.loads(args.output.read_text(encoding="utf-8")) == json.loads(
        json.dumps(report, ensure_ascii=False)
    )
    for name in ("route-matrix", "refusal-matrix", "billing-errors", "replay-no-new"):
        assert not (args.home / name / "instance.lock").exists()

    route, refusal, billing, replay = report["cases"]

    assert route["case"] == "route-matrix"
    assert route["rowsSha256"] == evaluation.load_baseline()["routeProbe"]["rowsSha256"]
    assert route["frozenProbeRows"] == 4
    assert route["matrixRows"] == 18
    assert route["jobs"] == 0
    reference = next(
        row
        for row in route["matrix"]
        if row["scope"] == "shot" and row["mode"] == "t2v" and row["durationSeconds"] == 5
    )
    assert [(item["provider"], item["amount"], item["currency"]) for item in reference["candidates"]] == [
        ("wan", "3.00", "CNY"),
        ("fal", "0.42", "USD"),
        ("kling", "0.56", "USD"),
    ]
    assert reference["recommended"] == "wan"

    assert refusal["case"] == "refusal-matrix"
    assert refusal["refusals"]["tenSeconds"] == [
        "VIDEO_COST_LIMIT",
        "VIDEO_UNSUPPORTED_SPEC",
        "VIDEO_UNSUPPORTED_SPEC",
    ]
    assert refusal["refusals"]["episode"] == [None, "EPISODE_COST_LIMIT", "EPISODE_COST_LIMIT"]
    assert refusal["refusals"]["unpublishedProvider"] == "VIDEO_PROVIDER_UNAVAILABLE"
    assert refusal["refusals"]["unknownStoryboard"] == "ROUTE_UNAVAILABLE"
    assert refusal["refusals"]["requireAvailable"] == "ROUTE_UNAVAILABLE"
    assert all(item["code"] == item["expected"] for item in refusal["malformedRequests"])
    assert refusal["jobs"] == 0

    assert billing["case"] == "billing-errors"
    assert billing["errorMapping"] == {
        "auth": "PROVIDER_AUTH_FAILED",
        "timeout": "SUBMISSION_UNKNOWN",
        "rateLimited": "PROVIDER_RATE_LIMITED",
        "blocked": "PROVIDER_BACKOFF",
        "wanAuth": "PROVIDER_AUTH_FAILED",
    }
    assert billing["throttle"]["gateAllowed"] is False and billing["throttle"]["gateReason"] == "cooldown"
    assert billing["throttle"]["blockedStatus"] == "pending_submit"
    assert billing["throttle"]["blockedAttempts"] == 0
    assert billing["throttle"]["blockedCode"] == "PROVIDER_BACKOFF"
    currencies = {item["currency"]: item for item in billing["billing"]["currencies"]}
    assert currencies["CNY"]["reported"] is None and currencies["CNY"]["unknownJobs"] == 1
    assert (currencies["USD"]["reported"], currencies["USD"]["delta"]) == ("0.60", "-1.64")
    assert billing["billing"]["revisionAfterReplace"] == 2
    assert billing["controlledSubmits"] == 5

    assert replay["case"] == "replay-no-new"
    assert replay["ledgerRecords"] == 1
    assert replay["billReplays"] == 2
    assert replay["controlledSubmits"] == 1


async def test_live_mode_is_refused_before_any_request(tmp_path):
    with pytest.raises(AppError) as error:
        await evaluation.run_suite(options(tmp_path, live=True))
    assert error.value.code == "EVAL_LIVE_UNAVAILABLE"
    assert not (tmp_path / "data").exists()


async def test_existing_report_or_non_empty_home_is_not_reused(tmp_path):
    args = options(tmp_path)
    args.output.write_text("previous", encoding="utf-8")
    with pytest.raises(AppError) as error:
        await evaluation.run_suite(args)
    assert error.value.code == "EVAL_OUTPUT_EXISTS"

    args = options(tmp_path / "again")
    args.home.mkdir(parents=True)
    (args.home / "state.json").write_text("{}", encoding="utf-8")
    with pytest.raises(AppError) as error:
        await evaluation.run_suite(args)
    assert error.value.code == "EVAL_HOME_NOT_EMPTY"


def test_suite_identity_and_check_helpers_are_pure():
    first = evaluation.suite_identity(evaluation.CASES)
    assert first == evaluation.suite_identity(evaluation.CASES)
    changed = (evaluation.Case("other", "route", "另一个用例"),)
    assert evaluation.suite_identity(changed) != first
    assert evaluation.all_checks_pass({"a": True, "b": True})
    assert not evaluation.all_checks_pass({"a": True, "b": False})
    assert not evaluation.all_checks_pass({})
