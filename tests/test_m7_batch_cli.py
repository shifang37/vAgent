"""M7 E2 CLI: `vagent season generate` registers one season batch without submitting."""

import json
import os
import subprocess
import sys

from shot_support import mock_tools

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

SCENES = ((4, 20), (4, 20), (4, 20))


def command(directory, home, *args):
    wanted = {name.upper() for name in CHILD_ENVIRONMENT}
    env = {key: value for key, value in os.environ.items() if key.upper() in wanted}
    env.update(
        VAGENT_HOME=str(home),
        VAGENT_VIDEO_MODE="mock",
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
        for index in range(2):
            saved = registry.execute(
                "episode_save",
                {
                    "title": f"第{index + 1}集",
                    "scenes": [
                        {
                            "heading": f"第{number + 1}场：雨夜咖啡店",
                            "synopsis": f"第{number + 1}场的概要。",
                            "timeOfDay": "night",
                            "shotCount": shots,
                            "durationSeconds": seconds,
                        }
                        for number, (shots, seconds) in enumerate(SCENES)
                    ],
                },
                store=store,
                project_id="coffee",
                operation_key=f"m7b-cli-episode-{index}",
            )
            assert saved["ok"], saved
            read = registry.execute(
                "episode_read",
                {"episodeId": saved["data"]["episodeId"]},
                store=store,
                project_id="coffee",
                operation_key=f"m7b-cli-read-{index}",
            )
            for number, (shots, seconds) in enumerate(SCENES):
                base, extra = divmod(seconds, shots)
                board = registry.execute(
                    "storyboard_save",
                    {
                        "title": f"第{number + 1}场分镜",
                        "sceneId": read["data"]["scenes"][number]["sceneId"],
                        "shots": [
                            {
                                "description": f"镜头{shot + 1}",
                                "framing": "wide",
                                "durationSeconds": base + (1 if shot < extra else 0),
                                "mode": "t2v",
                            }
                            for shot in range(shots)
                        ],
                    },
                    store=store,
                    project_id="coffee",
                    operation_key=f"m7b-cli-board-{index}-{number}",
                )
                assert board["ok"], board
            episodes.append(saved["data"]["episodeId"])
        season = registry.execute(
            "season_save",
            {"title": "第一季", "episodeIds": episodes},
            store=store,
            project_id="coffee",
            operation_key="m7b-cli-season",
        )
        assert season["ok"], season
        return episodes, season["data"]["seasonId"]


def test_cli_season_generate_registers_and_replays(tmp_path):
    home = tmp_path / "state"
    episodes, season_id = seed(home)
    generated = command(
        tmp_path,
        home,
        "season",
        "generate",
        "-s",
        "coffee",
        "--season",
        season_id,
        "--episode",
        episodes[0],
        "--episode",
        episodes[1],
        "--confirm",
        "--request-id",
        "cli-season-generate",
    )
    assert generated.returncode == 0, generated.stderr
    data = json.loads(generated.stdout)
    assert data["status"] == "succeeded" and data["newJobCount"] == 24
    assert data["replayed"] is False

    again = command(
        tmp_path,
        home,
        "season",
        "generate",
        "-s",
        "coffee",
        "--season",
        season_id,
        "--episode",
        episodes[0],
        "--episode",
        episodes[1],
        "--confirm",
        "--request-id",
        "cli-season-generate",
    )
    assert again.returncode == 0, again.stderr
    replay = json.loads(again.stdout)
    assert replay["batchId"] == data["batchId"] and replay["replayed"] is True

    shown = command(tmp_path, home, "season", "show", "-s", "coffee", "--season", season_id)
    assert shown.returncode == 0, shown.stderr
    view = json.loads(shown.stdout)
    assert view["batch"]["batchId"] == data["batchId"]
    assert [item["batchId"] for item in view["batches"]] == [data["batchId"]]

    with FileStore.open(home) as store:
        state = store.snapshot()
        assert len(state["jobs"]) == 24
        assert all(job["mode"] == "mock" for job in state["jobs"].values())
        assert len(state["batches"]) == 1


def test_cli_season_generate_requires_confirmation(tmp_path):
    home = tmp_path / "state"
    episodes, season_id = seed(home)
    refused = command(
        tmp_path,
        home,
        "season",
        "generate",
        "-s",
        "coffee",
        "--season",
        season_id,
        "--episode",
        episodes[0],
    )
    assert refused.returncode != 0
    assert "CONFIRMATION_REQUIRED" in refused.stderr or "CONFIRMATION_REQUIRED" in refused.stdout
    with FileStore.open(home) as store:
        assert store.snapshot()["jobs"] == {}
