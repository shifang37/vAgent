"""M6 E4 offline acceptance suite: subtitles, voice, 89 s episode and failures."""

import json
from argparse import Namespace

import pytest

from scripts import evaluate_m6 as evaluation
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
    args = options(tmp_path_factory.mktemp("m6-evaluation"))
    report = await evaluation.run_suite(args)
    assert report["status"] == "passed", [
        (item["case"], [key for key, value in item["checks"].items() if not value])
        for item in report["cases"]
        if item.get("checks") and not all(item["checks"].values())
    ]
    return args, report


async def test_m6_evaluation_records_a_whole_episode_film(accepted_suite):
    args, report = accepted_suite
    assert report["mode"] == "offline-fixture"
    assert report["totals"]["plannedCases"] == report["totals"]["passedCases"] == 3
    assert report["totals"]["submitCalls"] == 56
    assert report["totals"]["deliveredClips"] == 56
    assert report["totals"]["filmVersions"] == 1
    assert report["totals"]["renderReplays"] == 1
    assert report["totals"]["subtitleCues"] == 27
    assert report["totals"]["realProviderCalls"] == 0
    assert report["totals"]["textModelCalls"] == 0
    assert report["totals"]["ttsRealRequests"] == 0
    assert json.loads(args.output.read_text(encoding="utf-8")) == json.loads(
        json.dumps(report, ensure_ascii=False)
    )
    assert not (args.home / "instance.lock").exists()

    episode, failure, refusals = report["cases"]
    assert episode["case"] == "episode-both"
    assert episode["film"]["durationSeconds"] == 89.3
    assert episode["film"]["width"] == 1280 and episode["film"]["height"] == 720
    assert episode["film"]["audioTracks"] == 19
    assert episode["film"]["subtitleCues"] == 27
    assert episode["film"]["transitions"] == 2
    assert episode["film"]["titleCards"] == ["雨夜咖啡店", "未完待续"]
    assert episode["sceneStatuses"] == ["succeeded"] * 8
    assert abs(episode["film"]["loudness"]["integratedLufs"] + 16) <= 0.5
    assert episode["film"]["loudness"]["truePeakDb"] <= -1
    assert episode["streams"]["subtitles"][0]["codec"] == "mov_text"
    assert episode["replay"] is True

    assert failure["case"] == "episode-failure"
    assert failure["renderStatus"] == "failed"
    assert failure["failedScene"] == 2
    assert failure["sceneStatuses"][2] == "failed"
    assert failure["sceneStatuses"].count("succeeded") == 7

    assert refusals["case"] == "episode-refusals"
    assert refusals["codes"] == {
        "confirmation": "CONFIRMATION_REQUIRED",
        "transition": "TRANSITION_INVALID",
        "missingTrack": "SUBTITLE_TRACK_MISSING",
    }


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
    changed = (evaluation.Case("other", "episode", "rainy-cafe.md", "另一个标题"),)
    assert evaluation.suite_identity(changed) != first
    assert evaluation.all_checks_pass({"a": True, "b": True})
    assert not evaluation.all_checks_pass({"a": True, "b": False})
    assert not evaluation.all_checks_pass({})
