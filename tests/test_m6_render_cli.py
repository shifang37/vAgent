"""M6 E3 CLI: `vagent film render|renders` are local, keyless and Job-free."""

import asyncio
import json
import os
import subprocess
import sys

import pytest
from render_support import renderable_storyboard

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
        timeout=300,
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
        return await renderable_storyboard(store)


def test_cli_film_render_and_renders(tmp_path):
    if not engine_available():
        pytest.skip("bundled FFmpeg is unavailable on this platform")
    home = tmp_path / "state"
    fixture = asyncio.run(seed(home))
    storyboard_id = fixture["storyboardId"]
    before = json.loads((home / "state.json").read_bytes())
    rendered = command(
        tmp_path,
        home,
        "film",
        "render",
        "-s",
        "coffee",
        "--storyboard",
        storyboard_id,
        "--track",
        fixture["trackId"],
        "--profile",
        "720p-h264-aac-v1",
        "--mode",
        "soft",
        "--confirm",
        "--request-id",
        "cli-render-1",
    )
    assert rendered.returncode == 0, rendered.stderr
    data = json.loads(rendered.stdout)
    assert data["filmVersion"] == 1 and data["status"] == "succeeded"
    assert data["film"]["latest"]["render"]["subtitle"]["mode"] == "soft"
    state = json.loads((home / "state.json").read_bytes())
    assert state["schemaVersion"] == 9
    assert len(state["films"]) == 1 and len(state["renders"]) == 1
    # A local render never registers a Job.
    assert len(state["jobs"]) == len(before["jobs"])
    read = command(tmp_path, home, "film", "renders", "-s", "coffee", "--storyboard", storyboard_id)
    assert read.returncode == 0, read.stderr
    view = json.loads(read.stdout)
    assert view["render"]["status"] == "succeeded"
    assert view["render"]["renderId"] == data["renderId"]
    assert view["render"]["scenes"][0]["shotIds"] == fixture["shotIds"]
    # The same request ID replays instead of rendering a second version.
    again = command(
        tmp_path,
        home,
        "film",
        "render",
        "-s",
        "coffee",
        "--storyboard",
        storyboard_id,
        "--track",
        fixture["trackId"],
        "--profile",
        "720p-h264-aac-v1",
        "--mode",
        "soft",
        "--request-id",
        "cli-render-1",
    )
    assert again.returncode == 0, again.stderr
    assert json.loads(again.stdout)["filmVersion"] == 1
    # A malformed transition file is rejected before any encoding.
    bad = tmp_path / "transitions.json"
    bad.write_text(
        json.dumps([{"afterShotId": fixture["shotIds"][-1], "type": "crossfade", "durationMs": 500}]),
        encoding="utf-8",
    )
    refused = command(
        tmp_path,
        home,
        "film",
        "render",
        "-s",
        "coffee",
        "--storyboard",
        storyboard_id,
        "--track",
        fixture["trackId"],
        "--transitions",
        str(bad),
        "--request-id",
        "cli-render-2",
    )
    assert refused.returncode == 1 and "TRANSITION_INVALID" in refused.stderr
