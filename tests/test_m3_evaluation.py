"""M3 E4 acceptance suite: offline film delivery across providers, refusals and headless."""

import copy
import hashlib
import json
from argparse import Namespace
from pathlib import Path

import pytest

from scripts import evaluate_m3 as evaluation
from vagent.errors import AppError


def options(tmp_path, *, limit=60):
    return Namespace(
        home=tmp_path / "data",
        output=tmp_path / "report.json",
        live=False,
        max_model_calls=limit,
    )


@pytest.fixture(scope="module")
async def accepted_suite(tmp_path_factory):
    args = options(tmp_path_factory.mktemp("m3-evaluation"))
    report = await evaluation.run_suite(args)
    assert report["status"] == "passed", [
        (item["case"], [key for key, value in item["checks"].items() if not value])
        for item in report["cases"]
        if item.get("checks") and not all(item["checks"].values())
    ]
    return args, report


async def test_m3_evaluation_records_prompt_to_film_across_providers(accepted_suite):
    args, report = accepted_suite
    assert report["mode"] == "offline-fixture"
    assert report["totals"]["plannedCases"] == report["totals"]["passedCases"] == 6
    assert report["totals"]["modelCalls"] == 21
    assert report["totals"]["submitCalls"] == 11
    assert report["totals"]["deliveredClips"] == 11
    assert report["totals"]["filmVersions"] == 6
    assert report["totals"]["redos"] == 1
    assert report["totals"]["realProviderCalls"] == 0
    assert report["totals"]["callsWithUnknownUsage"] == report["totals"]["modelCalls"]
    assert not json.loads(args.output.read_text(encoding="utf-8"))["totals"]["tokenUsageComplete"]
    assert json.loads(args.output.read_text(encoding="utf-8")) == json.loads(
        json.dumps(report, ensure_ascii=False)
    )
    assert evaluation.evidence_hashes(args.home) == report["evidenceHashes"]
    assert not (args.home / "instance.lock").exists()

    kling, fal, wan, refusals, planned, delivered = report["cases"]
    # One Run per prompt: capabilities, storyboard, batch, set wait, read, film.
    assert [entry["name"] for entry in kling["toolResults"]] == evaluation.FILM_TOOL_CHAIN
    assert kling["filmInitial"]["currentVersion"] == 1
    assert kling["film"]["currentVersion"] == 2 and len(kling["film"]["versions"]) == 2
    assert kling["redo"]["versionCountDelta"][kling["redo"]["targetShotId"]] == 1
    assert kling["redo"]["usesNewShotVersion"] is True
    assert kling["redo"]["oldFilmExists"] is True
    assert kling["redo"]["staleError"] == "VERSION_CONFLICT"
    assert len(kling["runJobs"]) == 2 and len(kling["jobs"]) == 3
    assert all(flags["sha256Matches"] for flags in kling["filmChecks"].values())
    assert kling["film"]["latest"]["width"] == 1280 and kling["film"]["latest"]["hasAudio"] is True
    assert all(all(checks.values()) for checks in kling["mediaApi"].values())
    assert kling["filmRead"]["film"]["latest"]["mediaAvailable"] is True

    # fal is the silent relay: no audio switch omission, the app-root result path.
    assert fal["runUpstream"]["submitBodies"][0]["generate_audio"] is False
    assert fal["runUpstream"]["submitBodies"][0]["duration"] == "5"
    assert fal["runUpstream"]["downloadHosts"] == ["v3.fal.media"]
    assert fal["film"]["latest"]["hasAudio"] is True  # the local fixture carries audio

    # The pre-M3 Wan path still produces the same local film branch.
    assert wan["runUpstream"]["submitBodies"][0]["parameters"]["resolution"] == "720P"
    assert wan["upstream"]["downloadHosts"] == [evaluation.WAN_RESULT_HOST]
    assert wan["film"]["currentVersion"] == 1

    assert refusals["refusal"]["staleError"] == "VERSION_CONFLICT"
    assert refusals["refusal"]["invalidMediaError"] == "FILM_MEDIA_INVALID"
    assert refusals["refusal"]["missingInputError"] == "FILM_INPUT_MISSING"
    assert refusals["refusal"]["versionCountAfter"] == 1
    assert refusals["refusal"]["partFiles"] == []

    assert planned["headless"]["status"] == "planned"
    assert planned["jobCount"] == 0 and planned["mediaCount"] == 0
    assert planned["upstream"]["submitCalls"] == 0
    assert not {"shot_generate", "film_assemble"} & set(planned["headlessTools"])

    assert delivered["headless"]["status"] == "delivered"
    assert len(delivered["headless"]["jobIds"]) == 2
    assert delivered["jobCount"] == 2 and delivered["mediaCount"] == 3
    assert delivered["replayOutput"] == delivered["headless"]["output"]
    assert delivered["upstream"]["submitCalls"] == 2
    assert (
        hashlib.sha256(Path(delivered["headless"]["output"]["path"]).read_bytes()).hexdigest()
        == delivered["headless"]["output"]["sha256"]
    )


@pytest.mark.parametrize(
    ("index", "corruption", "failed_check"),
    [
        (0, "answer-without-film", "film_reported"),
        (0, "extra-final-submit", "film_assembly_no_submit"),
        (0, "wrong-kling-settings", "provider_body_frozen"),
        (0, "kling-body-not-echoed", "provider_body_frozen"),
        (0, "wrong-provider-auth", "provider_auth"),
        (0, "film-sha-mismatch", "film_branch"),
        (0, "film-range-broken", "film_playback"),
        (0, "text-side-touched", "text_side_unchanged"),
        (0, "redo-moved-other-jobs", "redo_left_other_jobs_untouched"),
        (0, "redo-overwrote-old-film", "redo_kept_old_film"),
        (0, "stale-assembly-accepted", "stale_assemble_refused"),
        (1, "fal-audio-omitted", "provider_body_frozen"),
        (1, "fal-duration-numeric", "provider_body_frozen"),
        (2, "wan-duration-changed", "provider_body_frozen"),
        (2, "wan-cost-not-recorded", "film_assembly_no_submit"),
        (3, "invalid-media-accepted", "invalid_media_refused"),
        (3, "refusal-added-version", "refusals_kept_film"),
        (3, "refusal-left-part", "refusals_left_no_parts"),
        (4, "plan-submitted", "no_submits"),
        (4, "plan-registered-job", "no_jobs"),
        (5, "headless-film-missing", "delivered_film"),
        (5, "headless-resubmitted", "delivered_no_resubmit"),
    ],
)
def test_m3_grader_rejects_fabricated_or_inconsistent_evidence(
    accepted_suite, index, corruption, failed_check
):
    _, report = accepted_suite
    item = copy.deepcopy(report["cases"][index])
    if corruption == "answer-without-film":
        item["run"]["answer"] = "两个镜头都已交付。"
    elif corruption == "extra-final-submit":
        item["upstream"]["submitCalls"] += 1
    elif corruption == "wrong-kling-settings":
        item["runUpstream"]["submitBodies"][0]["settings"]["resolution"] = "1080p"
    elif corruption == "kling-body-not-echoed":
        item["runUpstream"]["submitBodies"][0].pop("options")
    elif corruption == "wrong-provider-auth":
        item["runUpstream"]["authorization"] = ["key", "key"]
    elif corruption == "film-sha-mismatch":
        next(iter(item["filmChecks"].values()))["sha256Matches"] = False
    elif corruption == "film-range-broken":
        next(iter(item["mediaApi"].values()))["range206"] = False
    elif corruption == "text-side-touched":
        item["afterDomain"] = {"projects": {}, "artifacts": {}}
    elif corruption == "redo-moved-other-jobs":
        item["redo"]["otherJobsUnchanged"] = False
    elif corruption == "redo-overwrote-old-film":
        item["redo"]["oldFilmHashUnchanged"] = False
    elif corruption == "stale-assembly-accepted":
        item["redo"]["staleError"] = None
    elif corruption == "fal-audio-omitted":
        item["runUpstream"]["submitBodies"][0].pop("generate_audio")
    elif corruption == "fal-duration-numeric":
        item["runUpstream"]["submitBodies"][0]["duration"] = 5
    elif corruption == "wan-duration-changed":
        item["runUpstream"]["submitBodies"][0]["parameters"]["duration"] = 6
    elif corruption == "wan-cost-not-recorded":
        item["upstream"]["submitCalls"] = 0
    elif corruption == "invalid-media-accepted":
        item["refusal"]["invalidMediaError"] = None
    elif corruption == "refusal-added-version":
        item["refusal"]["versionCountAfter"] = 2
    elif corruption == "refusal-left-part":
        item["refusal"]["partFiles"] = ["x.mp4.part"]
    elif corruption == "plan-submitted":
        item["upstream"]["submitCalls"] = 1
    elif corruption == "plan-registered-job":
        item["jobCount"] = 1
    elif corruption == "headless-film-missing":
        item["headless"]["filmId"] = None
    elif corruption == "headless-resubmitted":
        item["upstream"]["submitCalls"] = 3
    assert not evaluation.grade(evaluation.CASES[index], item)[failed_check]


async def test_m3_offline_ignores_paid_models_external_services_and_user_video_mode(tmp_path, monkeypatch):
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
    assert report["status"] == "failed" and report["totals"]["modelCalls"] == 3
    assert len(report["cases"]) == 1 and report["totals"]["passedCases"] == 0
    assert "must-not-appear-in-evidence" not in args.output.read_text(encoding="utf-8")
    assert report["totals"]["realProviderCalls"] == 0


async def test_m3_stops_on_first_failure_with_attempts_accounted(tmp_path):
    report = await evaluation.run_suite(options(tmp_path, limit=2))
    assert report["status"] == "failed" and len(report["cases"]) == 1
    assert report["totals"]["modelCalls"] == 2
    assert report["cases"][0]["run"]["errorCode"] == "STEP_LIMIT"
    assert report["cases"][0]["run"]["policy"]["maxSteps"] == 2


@pytest.mark.parametrize("limit", [0, 61, 128])
async def test_m3_cannot_raise_suite_ceiling(tmp_path, limit):
    with pytest.raises(AppError) as error:
        await evaluation.run_suite(options(tmp_path, limit=limit))
    assert error.value.code == "EVAL_BUDGET_INVALID"


async def test_m3_live_never_spends_without_an_explicit_sample_approval(tmp_path):
    args = options(tmp_path)
    args.live = True
    with pytest.raises(AppError) as error:
        await evaluation.run_suite(args)
    assert error.value.code == "EVAL_LIVE_UNAVAILABLE"
    assert not args.home.exists() and not args.output.exists()


async def test_m3_does_not_overwrite_reports_or_reuse_existing_data(tmp_path, accepted_suite):
    args, _ = accepted_suite
    original = args.output.read_bytes()
    with pytest.raises(AppError) as error:
        await evaluation.run_suite(args)
    assert error.value.code == "EVAL_OUTPUT_EXISTS" and args.output.read_bytes() == original
    other = options(tmp_path)
    other.home = args.home
    with pytest.raises(AppError) as error:
        await evaluation.run_suite(other)
    assert error.value.code == "EVAL_HOME_NOT_EMPTY" and not other.output.exists()


def test_film_tools_declare_a_server_context_for_the_runner(store, video_service):
    """Async local writes are outside the Store lock but still need the Run scope."""
    from vagent.tools import create_project_tools
    from vagent.video.tools import register_video_tools

    registry = register_video_tools(create_project_tools(), video_service, mode="mock")
    assert registry.requires_context("film_assemble") is True
    assert registry.requires_context("film_read") is True
