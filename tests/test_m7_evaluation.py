"""M7 E4 offline acceptance suite: batches, isolation/resume and timeline v1→v2."""

import json
from argparse import Namespace

import pytest

from scripts import evaluate_m7 as evaluation
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
    args = options(tmp_path_factory.mktemp("m7-evaluation"))
    report = await evaluation.run_suite(args)
    assert report["status"] == "passed", [
        (item["case"], [key for key, value in item["checks"].items() if not value])
        for item in report["cases"]
        if item.get("checks") and not all(item["checks"].values())
    ]
    return args, report


async def test_m7_evaluation_records_batches_and_a_reassembled_timeline(accepted_suite):
    args, report = accepted_suite
    assert report["mode"] == "offline-fixture"
    assert report["totals"]["plannedCases"] == report["totals"]["passedCases"] == 3
    assert report["totals"]["mockJobs"] == 96
    assert report["totals"]["batches"] == 4
    assert report["totals"]["batchResumes"] == 1
    assert report["totals"]["controlledSubmits"] == 14
    assert report["totals"]["deliveredClips"] == 14
    assert report["totals"]["timelineVersions"] == 2
    assert report["totals"]["filmVersions"] == 2
    assert report["totals"]["subtitleCues"] == 22
    assert report["totals"]["realProviderCalls"] == 0
    assert report["totals"]["textModelCalls"] == 0
    assert report["totals"]["ttsRealRequests"] == 0
    assert json.loads(args.output.read_text(encoding="utf-8")) == json.loads(
        json.dumps(report, ensure_ascii=False)
    )
    assert not (args.home / "instance.lock").exists()

    batch, failure, timeline = report["cases"]

    assert batch["case"] == "season-batch"
    assert batch["batch"]["status"] == "succeeded"
    assert batch["batch"]["jobCounts"] == [12, 12, 12]
    assert batch["batch"]["revision"] == 6
    assert batch["batch"]["replayed"] is True
    assert batch["mockJobs"] == 36
    assert batch["controlledSubmits"] == 0

    assert failure["case"] == "season-failure"
    assert failure["original"]["status"] == "partial"
    assert failure["original"]["episodeStatuses"] == ["succeeded", "failed", "succeeded"]
    assert failure["original"]["errorCodes"] == [None, "BATCH_EMPTY", None]
    assert failure["resumed"]["episodeIds"] == [failure["original"]["episodeIds"][1]]
    assert failure["resumed"]["episodeVersions"] == [2]
    assert failure["mockJobs"] == 36

    assert timeline["case"] == "timeline-reassemble"
    assert timeline["timeline"]["effectiveDurationMs"] == [67_000, 65_600]
    assert timeline["timeline"]["clippedShots"] == 14
    assert timeline["timeline"]["transitions"] == 1
    assert timeline["timeline"]["subtitleShiftMs"] == 1400
    assert timeline["timeline"]["titleCards"] == ["雨夜咖啡店", "未完待续"]
    assert timeline["films"]["assemblyVersion"] == "ffmpeg-render-timeline-v1"
    assert timeline["films"]["versions"] == [1, 2]
    assert timeline["films"]["timelineVersion"] == 2
    assert timeline["films"]["audioTracks"] >= 2
    assert timeline["refusals"] == {
        "duplicate": "TIMELINE_INVALID",
        "shortTrim": "TIMELINE_TRIM_INVALID",
        "transitionAfterLast": "TIMELINE_INVALID",
    }
    assert timeline["staleCode"] == "TIMELINE_SOURCE_MISSING"
    assert timeline["deliveredClips"] == 14
    assert timeline["controlledSubmits"] == 14


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
    changed = (evaluation.Case("other", "batch", "rainy-cafe.md", "另一个标题"),)
    assert evaluation.suite_identity(changed) != first
    assert evaluation.all_checks_pass({"a": True, "b": True})
    assert not evaluation.all_checks_pass({"a": True, "b": False})
    assert not evaluation.all_checks_pass({})
