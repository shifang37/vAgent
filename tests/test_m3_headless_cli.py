"""E3 CLI: the real `vagent headless` entry with controlled adapters and no network.

The child process (`headless_cli_process.py`) replaces only the model and provider
transport; argument parsing, JSON output, exit codes, persistence and resume are the
production code paths.
"""

import json
import os
import subprocess
import sys
from pathlib import Path
from uuid import uuid4

CHILD_ENVIRONMENT = (
    "PATH",
    "PATHEXT",
    "SYSTEMROOT",
    "SYSTEMDRIVE",
    "WINDIR",
    "COMSPEC",
    "TEMP",
    "TMP",
    "USERPROFILE",
    "HOMEDRIVE",
    "HOMEPATH",
    "LOCALAPPDATA",
    "APPDATA",
    "PROGRAMDATA",
    "PROGRAMFILES",
    "PROGRAMFILES(X86)",
    "NUMBER_OF_PROCESSORS",
    "PROCESSOR_ARCHITECTURE",
    "LANG",
    "LC_ALL",
)
CONTROL = "control.json"
SUBMISSIONS = "submissions.jsonl"
FROZEN_FIELDS = {
    "formatVersion",
    "status",
    "projectId",
    "sessionId",
    "runId",
    "planFingerprint",
    "storyboardId",
    "storyboardVersion",
    "storyboardRevision",
    "jobIds",
    "filmId",
    "filmVersion",
    "output",
    "estimates",
    "limits",
    "error",
}
COST = ["--max-cost", "kling:USD=1.12"]
UNITS = ["--max-units", "kling=8"]


def command(tmp_path, home, *args, **changes):
    wanted = {name.upper() for name in CHILD_ENVIRONMENT}
    env = {key: value for key, value in os.environ.items() if key.upper() in wanted}
    env.update(
        VAGENT_HOME=str(home),
        VAGENT_VIDEO_MODE="live",
        VAGENT_VIDEO_PROVIDER="kling",
        VAGENT_KLING_API_KEY="kling-test-secret",
        VAGENT_FAL_API_KEY="fal-test-secret",
        VAGENT_DASHSCOPE_KEY="",
        VAGENT_DASHSCOPE_WORKSPACE_ID="",
        VAGENT_DEEPSEEK_KEY="deepseek-test-placeholder",
        PYTHONIOENCODING="utf-8",
        HEADLESS_CONTROL=str(tmp_path / CONTROL),
        HEADLESS_ARGS=json.dumps(list(args)),
    )
    env.update({key: str(value) for key, value in changes.items()})
    return subprocess.run(
        [sys.executable, str(Path(__file__).parent / "headless_cli_process.py")],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=180,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
    )


def payload(result):
    assert result.stdout.strip(), result.stderr
    body = json.loads(result.stdout)
    assert set(body) == FROZEN_FIELDS
    return body


def state_of(home):
    return json.loads((home / "state.json").read_text(encoding="utf-8"))


def submissions(tmp_path):
    path = tmp_path / SUBMISSIONS
    if not path.exists():
        return []
    return path.read_text(encoding="utf-8").splitlines()


def plan(tmp_path, home):
    (tmp_path / CONTROL).write_text('{"status": "succeeded"}', encoding="utf-8")
    result = command(tmp_path, home, "做一个雨夜咖啡店短片", "--json", "-s", "coffee")
    assert result.returncode == 0, result.stderr
    return payload(result)


def test_cli_headless_plan_authorize_and_resume_deliver(tmp_path):
    home = tmp_path / "state"
    planned = plan(tmp_path, home)
    assert planned["status"] == "planned" and planned["runId"]
    assert planned["estimates"][0]["amount"] == "1.12"
    assert planned["estimates"][0]["units"] == 8
    assert planned["jobIds"] == [] and planned["output"] is None
    assert not submissions(tmp_path)
    assert state_of(home)["jobs"] == {}

    authorized = command(
        tmp_path,
        home,
        "--plan",
        planned["runId"],
        "--plan-fingerprint",
        planned["planFingerprint"],
        "--confirm",
        *COST,
        *UNITS,
        "--max-jobs",
        "2",
        "--json",
        "-s",
        "coffee",
    )
    assert authorized.returncode == 0, authorized.stderr
    delivered = payload(authorized)
    assert delivered["status"] == "delivered" and delivered["filmVersion"] == 1
    assert len(delivered["jobIds"]) == 2
    assert delivered["limits"]["authorized"]["maxJobs"] == 2
    output = Path(delivered["output"]["path"])
    assert output.is_file() and output.stat().st_size == delivered["output"]["sizeBytes"]
    assert len(submissions(tmp_path)) == 2

    resumed = command(tmp_path, home, "--resume", planned["runId"], "--json", "-s", "coffee")
    assert resumed.returncode == 0, resumed.stderr
    replay = payload(resumed)
    assert replay["status"] == "delivered" and replay["output"] == delivered["output"]
    assert len(submissions(tmp_path)) == 2
    assert len(state_of(home)["jobs"]) == 2
    assert not (home / "instance.lock").exists()


def test_cli_headless_rejects_unauthorized_or_uncovered_plans(tmp_path):
    home = tmp_path / "state"
    planned = plan(tmp_path, home)
    common = ("--plan", planned["runId"], "--plan-fingerprint", planned["planFingerprint"])

    unconfirmed = command(tmp_path, home, *common, *COST, *UNITS, "--json", "-s", "coffee")
    assert unconfirmed.returncode == 3
    assert payload(unconfirmed)["error"]["code"] == "HEADLESS_CONFIRMATION_REQUIRED"

    wrong = command(
        tmp_path,
        home,
        "--plan",
        planned["runId"],
        "--plan-fingerprint",
        "0" * 64,
        "--confirm",
        *COST,
        *UNITS,
        "--json",
        "-s",
        "coffee",
    )
    assert wrong.returncode == 3
    assert payload(wrong)["error"]["code"] == "HEADLESS_PLAN_FINGERPRINT_MISMATCH"

    short = command(
        tmp_path,
        home,
        *common,
        "--confirm",
        "--max-cost",
        "kling:USD=0.10",
        *UNITS,
        "--json",
        "-s",
        "coffee",
    )
    assert short.returncode == 3
    assert payload(short)["error"]["code"] == "HEADLESS_BUDGET_NOT_COVERED"
    assert not submissions(tmp_path)
    assert state_of(home)["jobs"] == {}

    preauthorized = command(tmp_path, home, "短片", "--confirm", *COST, "--json")
    assert preauthorized.returncode == 3
    assert payload(preauthorized)["error"]["code"] == "HEADLESS_AUTHORIZATION_REQUIRED"

    missing = command(
        tmp_path,
        home,
        "--plan",
        str(uuid4()),
        "--plan-fingerprint",
        "0" * 64,
        "--confirm",
        "--json",
    )
    assert missing.returncode == 2
    assert payload(missing)["error"]["code"] == "HEADLESS_PLAN_NOT_FOUND"

    bad_jobs = command(tmp_path, home, *common, "--confirm", *COST, *UNITS, "--max-jobs", "x")
    assert bad_jobs.returncode == 2

    off = command(tmp_path, home, "短片", "--json", VAGENT_VIDEO_MODE="off")
    assert off.returncode == 2
    assert payload(off)["error"]["code"] == "VIDEO_MODE_OFF"


def test_cli_headless_wait_timeout_keeps_ids_and_resume_delivers(tmp_path):
    home = tmp_path / "state"
    planned = plan(tmp_path, home)
    (tmp_path / CONTROL).write_text('{"status": "processing"}', encoding="utf-8")
    timed = command(
        tmp_path,
        home,
        "--plan",
        planned["runId"],
        "--plan-fingerprint",
        planned["planFingerprint"],
        "--confirm",
        *COST,
        *UNITS,
        "--json",
        "-s",
        "coffee",
        HEADLESS_WAIT_TIMEOUT="0.3",
    )
    assert timed.returncode == 5, timed.stderr
    waiting = payload(timed)
    assert waiting["status"] == "waiting" and len(waiting["jobIds"]) == 2
    assert waiting["output"] is None and waiting["error"]["code"] == "HEADLESS_WAIT_TIMEOUT"
    assert len(submissions(tmp_path)) == 2

    (tmp_path / CONTROL).write_text('{"status": "succeeded"}', encoding="utf-8")
    resumed = command(
        tmp_path, home, "--resume", planned["runId"], "--json", "-s", "coffee", HEADLESS_WAIT_TIMEOUT="60"
    )
    assert resumed.returncode == 0, resumed.stderr
    delivered = payload(resumed)
    assert delivered["status"] == "delivered" and delivered["jobIds"] == waiting["jobIds"]
    assert len(submissions(tmp_path)) == 2  # resume never submitted again
