"""M7 E1 CLI: `vagent season show|save` are local, keyless and Job-free."""

import json
import os
import subprocess
import sys

from shot_support import mock_tools
from subtitle_support import episode_args, plan

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


def command(directory, home, *args):
    wanted = {name.upper() for name in CHILD_ENVIRONMENT}
    env = {key: value for key, value in os.environ.items() if key.upper() in wanted}
    env.update(
        VAGENT_HOME=str(home),
        VAGENT_VIDEO_MODE="off",
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


def seed(home):
    with FileStore.open(home) as store:
        store.ensure_session("coffee")
        registry, _service, _adapter, _context = mock_tools(store)
        episodes = []
        for index in range(3):
            args = episode_args(plan())
            args["title"] = f"第{index + 1}集"
            result = registry.execute(
                "episode_save",
                args,
                store=store,
                project_id="coffee",
                operation_key=f"m7-cli-episode-{index}",
            )
            assert result["ok"], result
            episodes.append(result["data"]["episodeId"])
        saved = registry.execute(
            "season_save",
            {"title": "第一季", "episodeIds": episodes[:2]},
            store=store,
            project_id="coffee",
            operation_key="m7-cli-season",
        )
        assert saved["ok"], saved
        return {"episodes": episodes, "season": saved["data"]["seasonId"]}


def test_cli_season_show_and_save(tmp_path):
    home = tmp_path / "state"
    fixture = seed(home)
    shown = command(tmp_path, home, "season", "show", "-s", "coffee")
    assert shown.returncode == 0, shown.stderr
    view = json.loads(shown.stdout)
    assert view["seasonId"] == fixture["season"]
    assert view["season"]["title"] == "第一季"
    assert [item["episodeId"] for item in view["episodes"]] == fixture["episodes"][:2]
    assert view["batch"] is None
    assert len(view["seasons"]) == 1

    created = command(
        tmp_path,
        home,
        "season",
        "save",
        "-s",
        "coffee",
        "--title",
        "第二季",
        "--episode",
        fixture["episodes"][2],
        "--request-id",
        "cli-season-create",
    )
    assert created.returncode == 0, created.stderr
    second = json.loads(created.stdout)
    assert second["title"] == "第二季" and second["version"] == 1
    assert second["seasonId"] != fixture["season"]

    # Replaying the same request id returns the same season version.
    again = command(
        tmp_path,
        home,
        "season",
        "save",
        "-s",
        "coffee",
        "--title",
        "第二季",
        "--episode",
        fixture["episodes"][2],
        "--request-id",
        "cli-season-create",
    )
    assert again.returncode == 0, again.stderr
    assert json.loads(again.stdout)["seasonId"] == second["seasonId"]

    updated = command(
        tmp_path,
        home,
        "season",
        "save",
        "-s",
        "coffee",
        "--season",
        fixture["season"],
        "--title",
        "第一季",
        "--episode",
        fixture["episodes"][1],
        "--episode",
        fixture["episodes"][0],
        "--expected-revision",
        "0",
        "--request-id",
        "cli-season-update",
    )
    assert updated.returncode == 0, updated.stderr
    changed = json.loads(updated.stdout)
    assert changed["seasonId"] == fixture["season"]
    assert (changed["version"], changed["revision"]) == (2, 1)
    assert changed["episodeIds"] == [fixture["episodes"][1], fixture["episodes"][0]]

    with FileStore.open(home) as store:
        state = store.snapshot()
        assert state["schemaVersion"] == 9
        assert state["jobs"] == {}
        assert len(state["seasons"][fixture["season"]]["versions"]) == 2
        season_run = next(
            run for run in state["runs"].values() if run["prompt"].startswith("本地界面发起的季成员保存")
        )
        assert season_run["videoMode"] == "off"
        assert season_run["toolFeatures"] == {}


def test_cli_season_save_requires_an_episode_and_reports_ownership(tmp_path):
    home = tmp_path / "state"
    fixture = seed(home)
    missing = command(tmp_path, home, "season", "save", "-s", "coffee", "--title", "第二季")
    assert missing.returncode != 0

    clash = command(
        tmp_path,
        home,
        "season",
        "save",
        "-s",
        "coffee",
        "--title",
        "第二季",
        "--episode",
        fixture["episodes"][0],
        "--request-id",
        "cli-season-clash",
    )
    assert clash.returncode != 0
    assert "SEASON_EPISODE_CONFLICT" in clash.stderr or "SEASON_EPISODE_CONFLICT" in clash.stdout

    show_missing = command(tmp_path, home, "season", "show", "-s", "coffee", "--season", "not-a-uuid")
    assert show_missing.returncode != 0
