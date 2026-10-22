"""M5 E4 acceptance suite: episode adaptation, registration, delivery and scene films."""

import json
from argparse import Namespace
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts import evaluate_m5 as evaluation
from vagent.errors import AppError


def options(tmp_path, *, limit=60, only=None):
    return Namespace(
        home=tmp_path / "data",
        output=tmp_path / "report.json",
        live=False,
        max_model_calls=limit,
        only=only or [],
    )


@pytest.fixture(scope="module")
async def accepted_suite(tmp_path_factory):
    args = options(tmp_path_factory.mktemp("m5-evaluation"))
    report = await evaluation.run_suite(args)
    assert report["status"] == "passed", [
        (item["case"], [key for key, value in item["checks"].items() if not value])
        for item in report["cases"]
        if item.get("checks") and not all(item["checks"].values())
    ]
    return args, report


async def test_m5_evaluation_records_text_to_scene_films(accepted_suite):
    args, report = accepted_suite
    assert report["mode"] == "offline-fixture"
    assert report["totals"]["plannedCases"] == report["totals"]["passedCases"] == 5
    assert report["totals"]["submitCalls"] == 42
    assert report["totals"]["registeredJobs"] == 62
    assert report["totals"]["deliveredClips"] == 41
    assert report["totals"]["filmVersions"] == 18
    assert report["totals"]["redos"] == 2
    assert report["totals"]["realProviderCalls"] == 0
    assert report["totals"]["realModelCalls"] == 0
    assert json.loads(args.output.read_text(encoding="utf-8")) == json.loads(
        json.dumps(report, ensure_ascii=False)
    )
    assert not (args.home / "instance.lock").exists()
    episode, i2v, refusals, cap, redo = report["cases"]
    assert episode["case"] == "wan-episode" and len(episode["scenes"]) == 7
    assert episode["episode"]["episode"]["shotBudget"]["shotCount"] == 18
    assert all(scene["film"] for scene in episode["scenes"])
    assert len(i2v["scenes"]) == 8 and all(scene["film"] for scene in i2v["scenes"])
    assert any(entry["mode"] == "i2v" for entry in i2v["refs"].values())
    assert i2v["episode"]["consistency"]["characters"][0]["consistent"] is True
    assert i2v["episode"]["consistency"]["reviews"][0]["score"] == 5
    assert refusals["shotBudget"] == "EPISODE_SHOT_BUDGET"
    assert refusals["durationLimit"] == "EPISODE_DURATION_LIMIT"
    assert refusals["unboundGenerate"] == "SCENE_CONFLICT"
    assert len(cap["jobs"]) == 20 and all(job["status"] == "pending_submit" for job in cap["jobs"])
    assert len(cap["jobsAfterRedo"]) == 20
    assert cap["redoError"] == "EPISODE_COST_LIMIT"
    assert len(redo["jobs"]) == 5 and redo["filmTwo"]["currentVersion"] == 2


def test_episode_amount_uses_the_frozen_per_second_rate():
    plan = {
        "shots": [
            {"durationSeconds": 5},
            {"durationSeconds": 4},
            {"durationSeconds": 3},
        ]
    }
    assert evaluation.expected_episode_amount(plan, 3) == "7.20"
    assert evaluation.all_checks_pass({"a": True, "b": True})
    assert not evaluation.all_checks_pass({"a": True, "b": False})
    assert not evaluation.all_checks_pass({})


def test_grader_refuses_a_fabricated_episode_film(tmp_path):
    class Store:
        home = tmp_path
        state = {"media": {}}

        def snapshot(self):
            return self.state

    service = SimpleNamespace(store=Store())
    scene = {
        "sceneId": "scene-1",
        "shotCount": 2,
        "film": {
            "currentVersion": 1,
            "latest": {
                "mediaAvailable": True,
                "width": 1280,
                "height": 720,
                "hasAudio": True,
                "inputs": [{"shotId": "a"}],
                "timeline": [{"shotId": "a", "durationSeconds": 5.0}],
                "durationSeconds": 5.0,
                "outputMediaId": "media-1",
                "sizeBytes": 1,
                "sha256": "a" * 64,
            },
        },
    }
    assert evaluation.film_checks(service, scene) is False
    assert evaluation.film_checks(service, {"sceneId": "scene-1", "shotCount": 2, "film": None}) is False


def test_grader_requires_the_whole_refusal_matrix():
    case = evaluation.CASES_BY_NAME["wan-refusals"]
    item = {
        "kind": "refusals",
        "sourceTooLong": "INVALID_ARGUMENTS",
        "moduleTooLong": "EPISODE_SOURCE_TOO_LONG",
        "unboundGenerate": "SCENE_CONFLICT",
        "confirmRequired": "CONFIRMATION_REQUIRED",
        "shotBudget": "EPISODE_SHOT_BUDGET",
        "durationLimit": "EPISODE_DURATION_LIMIT",
        "missingAsset": "CHARACTER_ASSET_MISSING",
        "jobs": [],
        "scenes": [{}] * 9,
        "storyboards": [{}] * 9,
        "assetScenes": [{}] * 9,
        "assetSceneCount": 9,
        "assetStoryboards": [],
        "upstream": {"submitCalls": 0},
    }
    checks = evaluation.grade(case, item, SimpleNamespace())
    assert checks["source_too_long"] and checks["unbound_scene"]
    bad = {**item, "missingAsset": None}
    assert evaluation.grade(case, bad, SimpleNamespace())["missing_character_asset"] is False


def test_cap_requires_a_full_bucket_and_no_extra_job():
    case = evaluation.CASES_BY_NAME["wan-cap"]
    item = {
        "kind": "cap",
        "shotCount": 20,
        "jobs": [
            {"id": f"job-{index}", "status": "pending_submit", "mediaAvailable": False} for index in range(20)
        ],
        "jobsAfterRedo": [{"id": f"job-{index}"} for index in range(20)],
        "generation": {"limits": {"usedNewJobs": 20, "remainingAmount": "6.00", "maxNewJobsPerRun": 20}},
        "redoError": "EPISODE_COST_LIMIT",
        "upstream": {"submitCalls": 0},
    }
    checks = evaluation.grade(case, item, SimpleNamespace())
    assert checks["twenty_jobs_registered"] and checks["redo_refused_by_bucket"]
    assert checks["nothing_submitted"] and checks["twenty_pending"]
    short = {**item, "shotCount": 19, "jobs": item["jobs"][:19], "jobsAfterRedo": item["jobsAfterRedo"][:19]}
    assert evaluation.grade(case, short, SimpleNamespace())["twenty_jobs_registered"] is False


async def test_live_mode_never_spends_without_an_explicit_sample_approval(tmp_path):
    args = options(tmp_path)
    args.live = True
    with pytest.raises(AppError) as caught:
        await evaluation.run_suite(args)
    assert caught.value.code == "EVAL_LIVE_UNAVAILABLE"
    assert not args.home.exists()


@pytest.mark.parametrize("limit", [0, 101])
async def test_cannot_raise_the_suite_ceiling(tmp_path, limit):
    with pytest.raises(AppError) as caught:
        await evaluation.run_suite(options(tmp_path, limit=limit))
    assert caught.value.code == "EVAL_BUDGET_INVALID"


async def test_does_not_overwrite_reports_or_reuse_existing_data(accepted_suite, tmp_path):
    args, _report = accepted_suite
    with pytest.raises(AppError) as caught:
        await evaluation.run_suite(args)
    assert caught.value.code == "EVAL_OUTPUT_EXISTS"
    other = options(tmp_path)
    other.output = tmp_path / "fresh.json"
    (other.home).mkdir(parents=True)
    (other.home / "state.json").write_text("{}", encoding="utf-8")
    with pytest.raises(AppError) as caught:
        await evaluation.run_suite(other)
    assert caught.value.code == "EVAL_HOME_NOT_EMPTY"


def test_suite_identity_is_stable_and_text_dependent():
    first = evaluation.suite_identity(evaluation.CASES)
    second = evaluation.suite_identity(evaluation.CASES)
    assert first == second and len(first) == 16
    assert evaluation.solid_png(4, 4, (1, 2, 3)).startswith(b"\x89PNG\r\n\x1a\n")
    assert evaluation._error_code(lambda: (_ for _ in ()).throw(AppError("X", "x"))) == "X"
    assert evaluation._error_code(lambda: {"ok": False, "error": {"code": "Y"}}) == "Y"
    assert evaluation._error_code(lambda: {"ok": True}) is None
    assert Path(evaluation.TEXTS / evaluation.CASES[0].text).is_file()
