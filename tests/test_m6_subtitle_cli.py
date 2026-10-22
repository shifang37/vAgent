"""M6 E1 CLI: `vagent subtitle show|save|export` are local, keyless and Job-free."""

import json
import os
import subprocess
import sys

import pytest
from subtitle_support import film_with_dialogue

from vagent.errors import AppError
from vagent.storage import FileStore
from vagent.video.assembler import bundled_engine

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


def command(directory, home, *args):
    wanted = {name.upper() for name in CHILD_ENVIRONMENT}
    env = {key: value for key, value in os.environ.items() if key.upper() in wanted}
    env.update(
        VAGENT_HOME=str(home),
        VAGENT_VIDEO_MODE="live",
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


def engine_available():
    try:
        bundled_engine()
        return True
    except AppError:
        return False


async def seed(home):
    with FileStore.open(home) as store:
        return await film_with_dialogue(store)


def test_cli_subtitle_show_save_export(tmp_path):
    if not engine_available():
        pytest.skip("bundled FFmpeg is unavailable on this platform")
    home = tmp_path / "state"
    fixture = None
    import asyncio

    fixture = asyncio.run(seed(home))
    storyboard_id = fixture["storyboardId"]
    before = json.loads((home / "state.json").read_bytes())
    shown = command(tmp_path, home, "subtitle", "show", "-s", "coffee", "--storyboard", storyboard_id)
    assert shown.returncode == 0, shown.stderr
    assert json.loads(shown.stdout) == {
        "storyboardId": storyboard_id,
        "episodeId": None,
        "track": None,
    }
    saved = command(
        tmp_path,
        home,
        "subtitle",
        "save",
        "-s",
        "coffee",
        "--storyboard",
        storyboard_id,
        "--request-id",
        "cli-sub-1",
    )
    assert saved.returncode == 0, saved.stderr
    track = json.loads(saved.stdout)
    assert track["currentVersion"] == 1 and track["latest"]["cueCount"] > 0
    track_id = track["trackId"]
    state = json.loads((home / "state.json").read_bytes())
    assert state["schemaVersion"] == 7
    assert len(state["subtitleTracks"]) == 1
    assert len(state["jobs"]) == len(before["jobs"])  # no video Job was registered
    reread = command(tmp_path, home, "subtitle", "show", "-s", "coffee", "--storyboard", storyboard_id)
    assert reread.returncode == 0, reread.stderr
    assert json.loads(reread.stdout)["track"]["trackId"] == track_id
    exported = command(
        tmp_path,
        home,
        "subtitle",
        "export",
        "-s",
        "coffee",
        "--track",
        track_id,
        "--mode",
        "soft",
        "--request-id",
        "cli-export-1",
    )
    assert exported.returncode == 0, exported.stderr
    film = json.loads(exported.stdout)
    assert film["currentVersion"] == 2
    assert film["latest"]["render"]["subtitle"]["mode"] == "soft"
    assert film["latest"]["render"]["subtitle"]["sidecarSha256"]
    again = command(
        tmp_path,
        home,
        "subtitle",
        "export",
        "-s",
        "coffee",
        "--track",
        track_id,
        "--mode",
        "soft",
        "--request-id",
        "cli-export-1",
    )
    assert again.returncode == 0, again.stderr
    assert json.loads(again.stdout)["latest"]["outputMediaId"] == film["latest"]["outputMediaId"]
    # Manual correction from a JSON cue file appends a version and can re-render.
    cues = [
        {
            "startMs": cue["startMs"],
            "endMs": cue["endMs"],
            "text": cue["text"],
            "speaker": cue.get("speaker"),
        }
        for cue in track["latest"]["cues"]
    ]
    cues[0]["text"] = "改过的第一句字幕。"
    cue_file = tmp_path / "cues.json"
    cue_file.write_text(json.dumps(cues, ensure_ascii=False), encoding="utf-8")
    corrected = command(
        tmp_path,
        home,
        "subtitle",
        "save",
        "-s",
        "coffee",
        "--storyboard",
        storyboard_id,
        "--track",
        track_id,
        "--expected-revision",
        "0",
        "--cues",
        str(cue_file),
        "--request-id",
        "cli-sub-2",
    )
    assert corrected.returncode == 0, corrected.stderr
    second = json.loads(corrected.stdout)
    assert second["currentVersion"] == 2 and second["latest"]["cues"][0]["text"] == "改过的第一句字幕。"
    burned = command(
        tmp_path,
        home,
        "subtitle",
        "export",
        "-s",
        "coffee",
        "--track",
        track_id,
        "--mode",
        "burn",
        "--request-id",
        "cli-export-2",
    )
    assert burned.returncode == 0, burned.stderr
    burned_film = json.loads(burned.stdout)
    assert burned_film["currentVersion"] == 3
    assert burned_film["latest"]["render"]["subtitle"]["mode"] == "burn"
    # A stale subtitle revision is refused rather than overwritten.
    stale = command(
        tmp_path,
        home,
        "subtitle",
        "save",
        "-s",
        "coffee",
        "--storyboard",
        storyboard_id,
        "--track",
        track_id,
        "--expected-revision",
        "0",
        "--cues",
        str(cue_file),
        "--request-id",
        "cli-sub-3",
    )
    assert stale.returncode == 1 and "VERSION_CONFLICT" in stale.stderr
