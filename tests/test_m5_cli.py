"""M5 E4 CLI: plan, save, show and whole-episode registration are local and keyless."""

import json
import os
import subprocess
import sys
from pathlib import Path

from vagent.storage import FileStore
from vagent.tools import create_project_tools
from vagent.video.adaptation import plan_episode
from vagent.video.jobs import JobService
from vagent.video.providers.mock import MockVideoAdapter
from vagent.video.tools import register_video_tools

ROOT = Path(__file__).resolve().parent.parent
TEXT = ROOT / "tests" / "fixtures" / "m5" / "texts" / "fridge-notes.md"
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


def command(directory, home, *args, mode="mock"):
    wanted = {name.upper() for name in CHILD_ENVIRONMENT}
    env = {key: value for key, value in os.environ.items() if key.upper() in wanted}
    env.update(
        VAGENT_HOME=str(home),
        VAGENT_VIDEO_MODE=mode,
        VAGENT_DASHSCOPE_KEY="",
        VAGENT_DASHSCOPE_WORKSPACE_ID="",
        VAGENT_DEEPSEEK_KEY="",
        DEEPSEEK_API_KEY="",
        VAGENT_MCP_CONFIG=str(directory / "missing-mcp.json"),
        PYTHONIOENCODING="utf-8",
    )
    return subprocess.run(
        [sys.executable, "-m", "vagent", *args],
        cwd=directory,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=180,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
    )


def seed_episode(home, *, bind=True):
    """Create one real episode-shaped record and optionally bind every scene."""
    plan = plan_episode(TEXT.read_text(encoding="utf-8"), title="冰箱上的便条")
    with FileStore.open(home) as store:
        store.ensure_session("probe")
        registry = register_video_tools(
            create_project_tools(), JobService(store, [MockVideoAdapter(store)]), mode="mock"
        )
        saved = registry.execute(
            "episode_save",
            {
                "title": "冰箱上的便条",
                "scenes": [
                    {
                        "heading": scene["heading"],
                        "location": scene["location"],
                        "timeOfDay": scene["timeOfDay"],
                        "synopsis": scene["synopsis"],
                        "dialogue": [
                            {
                                "text": line["text"],
                                "speaker": line.get("speaker"),
                                "paragraph": line["paragraph"],
                            }
                            for line in scene["dialogue"]
                        ],
                        "shotCount": scene["shotCount"],
                        "durationSeconds": scene["durationSeconds"],
                    }
                    for scene in plan["scenes"]
                ],
            },
            store=store,
            project_id="probe",
            operation_key="cli-seed-episode",
        )
        if bind:
            for index, scene_id in enumerate(saved["data"]["sceneIds"], start=1):
                shots = [
                    {
                        "description": shot["description"],
                        "framing": shot["framing"],
                        "cameraMovement": shot["cameraMovement"],
                        "durationSeconds": shot["durationSeconds"],
                        "mode": "t2v",
                    }
                    for shot in plan["shots"]
                    if shot["sceneIndex"] == index
                ]
                result = registry.execute(
                    "storyboard_save",
                    {"sceneId": scene_id, "title": plan["scenes"][index - 1]["heading"], "shots": shots},
                    store=store,
                    project_id="probe",
                    operation_key=f"cli-seed-board-{index}",
                )
                assert result["ok"], result
        return saved["data"]


def test_cli_episode_plan_is_keyless_and_does_not_write(tmp_path):
    home = tmp_path / "state"
    result = command(tmp_path, home, "episode", "plan", "--text", str(TEXT), "--title", "冰箱上的便条")
    assert result.returncode == 0, result.stderr
    plan = json.loads(result.stdout)
    assert plan["shotBudget"]["shotCount"] == 18
    assert plan["shotBudget"]["targetDurationSeconds"] == 90
    assert len(plan["scenes"]) == 7
    assert plan["textSplitContract"] == "text-split-zh-v1"
    assert not (home / "state.json").exists()


def test_cli_episode_save_show_and_registration_refusal(tmp_path):
    home = tmp_path / "state"
    saved = command(
        tmp_path,
        home,
        "episode",
        "save",
        "--text",
        str(TEXT),
        "--title",
        "冰箱上的便条",
        "-s",
        "probe",
        "--request-id",
        "cli-save-1",
    )
    assert saved.returncode == 0, saved.stderr
    data = json.loads(saved.stdout)
    assert data["shotCount"] == 18 and data["targetDurationSeconds"] == 90
    reshown = command(
        tmp_path,
        home,
        "episode",
        "save",
        "--text",
        str(TEXT),
        "--title",
        "冰箱上的便条",
        "-s",
        "probe",
        "--request-id",
        "cli-save-1",
    )
    assert reshown.returncode == 0 and json.loads(reshown.stdout)["episodeId"] == data["episodeId"]
    view = command(tmp_path, home, "episode", "show", "-s", "probe")
    assert view.returncode == 0, view.stderr
    shown = json.loads(view.stdout)
    assert shown["episodeId"] == data["episodeId"]
    assert len(shown["scenes"]) == 7
    assert all(scene["film"] is None for scene in shown["scenes"])

    no_confirm = command(tmp_path, home, "episode", "generate", "-s", "probe", "--episode", data["episodeId"])
    assert no_confirm.returncode == 1 and "CONFIRMATION_REQUIRED" in no_confirm.stderr
    unbound = command(
        tmp_path,
        home,
        "episode",
        "generate",
        "-s",
        "probe",
        "--episode",
        data["episodeId"],
        "--confirm",
        "--request-id",
        "cli-generate-1",
    )
    assert unbound.returncode == 1 and "SCENE_CONFLICT" in unbound.stderr
    state = json.loads((home / "state.json").read_bytes())
    assert len(state["jobs"]) == 0


def test_cli_episode_generate_registers_mock_jobs_without_submit(tmp_path):
    home = tmp_path / "state"
    data = seed_episode(home)
    generated = command(
        tmp_path,
        home,
        "episode",
        "generate",
        "-s",
        "probe",
        "--episode",
        data["episodeId"],
        "--confirm",
        "--request-id",
        "cli-generate-2",
    )
    assert generated.returncode == 0, generated.stderr
    result = json.loads(generated.stdout)
    assert len(result["shots"]) == 18 and result["runId"] and result["mode"] == "mock"
    replay = command(
        tmp_path,
        home,
        "episode",
        "generate",
        "-s",
        "probe",
        "--episode",
        data["episodeId"],
        "--confirm",
        "--request-id",
        "cli-generate-2",
    )
    assert replay.returncode == 0
    assert json.loads(replay.stdout)["shots"] == result["shots"]
    state = json.loads((home / "state.json").read_bytes())
    assert len(state["jobs"]) == 18
    assert all(job["mode"] == "mock" for job in state["jobs"].values())
    assert all(job["status"] != "failed" for job in state["jobs"].values())


def test_cli_episode_save_binds_existing_bible_characters(tmp_path):
    home = tmp_path / "state"
    with FileStore.open(home) as store:
        store.ensure_session("probe")
        registry = register_video_tools(
            create_project_tools(), JobService(store, [MockVideoAdapter(store)]), mode="mock"
        )
        character = registry.execute(
            "bible_save",
            {"kind": "character", "name": "母亲", "appearance": "围着旧围裙的中年女性"},
            store=store,
            project_id="probe",
            operation_key="cli-seed-character",
        )
    assert character["ok"], character
    saved = command(
        tmp_path,
        home,
        "episode",
        "save",
        "--text",
        str(TEXT),
        "--title",
        "冰箱上的便条",
        "-s",
        "probe",
        "--request-id",
        "cli-save-2",
    )
    assert saved.returncode == 0, saved.stderr
    view = json.loads(command(tmp_path, home, "episode", "show", "-s", "probe").stdout)
    assert any(character["data"]["id"] in scene["characterIds"] for scene in view["scenes"])


def test_cli_episode_plan_missing_file_is_reported(tmp_path):
    result = command(tmp_path, tmp_path / "state", "episode", "plan", "--text", str(tmp_path / "nope.md"))
    assert result.returncode == 1 and "NOT_FOUND" in result.stderr
