"""M8 E1: `vagent route preview` is local, read-only and key-aware.

The CLI face never registers a Job, never contacts a provider and never changes
state.json; it shares the one pure preview function with HTTP and the Web panel.
"""

import json
import os
import subprocess
import sys

from shot_support import mock_tools, save_storyboard, shot

from vagent.storage import FileStore

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


def command(directory, home, *args, keys=True):
    wanted = {name.upper() for name in CHILD_ENVIRONMENT}
    env = {key: value for key, value in os.environ.items() if key.upper() in wanted}
    env.update(
        VAGENT_HOME=str(home),
        VAGENT_VIDEO_MODE="live",
        VAGENT_DASHSCOPE_KEY="wan-test-secret" if keys else "",
        VAGENT_DASHSCOPE_WORKSPACE_ID="",
        VAGENT_KLING_API_KEY="kling-test-secret" if keys else "",
        FAL_KEY="fal-test-secret" if keys else "",
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


def seed(home):
    with FileStore.open(home) as store:
        registry, _, _, _ = mock_tools(store)
        return save_storyboard(registry, store, [shot("镜头一"), shot("镜头二")])


def test_cli_route_preview_by_spec_and_scope(tmp_path):
    home = tmp_path / "state"
    with FileStore.open(home):
        pass
    before = json.loads((home / "state.json").read_bytes())
    shown = command(tmp_path, home, "route", "preview", "--mode", "t2v", "--duration", "5")
    assert shown.returncode == 0, shown.stderr
    view = json.loads(shown.stdout)
    assert view["contract"] == "provider-route-v1"
    assert [item["provider"] for item in view["candidates"]] == ["wan", "fal", "kling"]
    assert [item["amount"] for item in view["candidates"]] == ["3.00", "0.42", "0.56"]
    assert view["recommended"] == "wan"

    episode = command(
        tmp_path, home, "route", "preview", "--mode", "t2v", "--duration", "5", "--scope", "episode"
    )
    assert episode.returncode == 0, episode.stderr
    reasons = {item["provider"]: item["reason"] for item in json.loads(episode.stdout)["candidates"]}
    assert reasons == {"wan": None, "kling": "EPISODE_COST_LIMIT", "fal": "EPISODE_COST_LIMIT"}

    unavailable = command(
        tmp_path,
        home,
        "route",
        "preview",
        "--mode",
        "t2v",
        "--duration",
        "10",
        "--require-available",
    )
    assert unavailable.returncode == 1 and "ROUTE_UNAVAILABLE" in unavailable.stderr

    conflict = command(tmp_path, home, "route", "preview", "--mode", "t2v", "--storyboard", "board")
    assert conflict.returncode == 1 and "INVALID_ARGUMENTS" in conflict.stderr
    after = json.loads((home / "state.json").read_bytes())
    assert after["jobs"] == before["jobs"] == {}
    assert after["schemaVersion"] == 9


def test_cli_route_preview_reads_a_storyboard_and_missing_keys(tmp_path):
    home = tmp_path / "state"
    saved = seed(home)
    before = json.loads((home / "state.json").read_bytes())
    shown = command(
        tmp_path, home, "route", "preview", "--storyboard", saved["storyboardId"], "--version", "1"
    )
    assert shown.returncode == 0, shown.stderr
    view = json.loads(shown.stdout)
    assert view["request"]["storyboardId"] == saved["storyboardId"]
    assert view["request"]["shotIds"] == saved["shotIds"]
    wan = next(item for item in view["candidates"] if item["provider"] == "wan")
    assert (wan["amount"], wan["quantity"]) == ("6.00", "10")

    keyless = command(
        tmp_path,
        home,
        "route",
        "preview",
        "--mode",
        "t2v",
        "--duration",
        "5",
        keys=False,
    )
    assert keyless.returncode == 0, keyless.stderr
    assert {item["reason"] for item in json.loads(keyless.stdout)["candidates"]} == {"VIDEO_KEY_MISSING"}
    assert json.loads((home / "state.json").read_bytes()) == before
