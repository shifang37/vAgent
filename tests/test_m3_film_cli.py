"""E1 CLI: `vagent film show|assemble` are local, keyless and never submit a video Job."""

import json
import os
import subprocess
import sys

import pytest
from film_support import delivered_pair

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
    except Exception:
        return False


async def seed(home):
    with FileStore.open(home) as store:
        return await delivered_pair(store)


async def test_cli_film_show_and_assemble_without_keys(tmp_path):
    if not engine_available():
        pytest.skip("bundled FFmpeg is unavailable on this platform")
    home = tmp_path / "state"
    fixture = await seed(home)
    before = json.loads((home / "state.json").read_bytes())
    shown = command(tmp_path, home, "film", "show", "-s", "coffee", "--storyboard", fixture["storyboardId"])
    assert shown.returncode == 0, shown.stderr
    assert json.loads(shown.stdout) == {"storyboardId": fixture["storyboardId"], "film": None}

    conflict = command(
        tmp_path,
        home,
        "film",
        "assemble",
        "-s",
        "coffee",
        "--storyboard",
        fixture["storyboardId"],
        "--expected-revision",
        "99",
    )
    assert conflict.returncode == 1 and "VERSION_CONFLICT" in conflict.stderr

    assembled = command(
        tmp_path,
        home,
        "film",
        "assemble",
        "-s",
        "coffee",
        "--storyboard",
        fixture["storyboardId"],
        "--expected-revision",
        str(fixture["revision"]),
        "--request-id",
        "cli-assemble-1",
    )
    assert assembled.returncode == 0, assembled.stderr
    result = json.loads(assembled.stdout)
    assert result["currentVersion"] == 1 and result["latest"]["mediaAvailable"] is True
    assert result["runId"] and result["mode"] == "live"
    again = command(
        tmp_path,
        home,
        "film",
        "assemble",
        "-s",
        "coffee",
        "--storyboard",
        fixture["storyboardId"],
        "--expected-revision",
        str(fixture["revision"]),
        "--request-id",
        "cli-assemble-1",
    )
    assert again.returncode == 0, again.stderr
    assert json.loads(again.stdout)["latest"]["outputMediaId"] == result["latest"]["outputMediaId"]
    state = json.loads((home / "state.json").read_bytes())
    assert len(state["films"]) == 1 and len(state["films"][result["filmId"]]["versions"]) == 1
    assert len(state["jobs"]) == len(before["jobs"])  # no generation Job was registered
    assert state["schemaVersion"] == 8
    reread = command(tmp_path, home, "film", "show", "-s", "coffee")
    assert reread.returncode == 0, reread.stderr
    assert json.loads(reread.stdout)["film"]["currentVersion"] == 1
