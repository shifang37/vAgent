"""M2 D3 CLI: storyboard show, shots redo, assets list|import and usage --project.

Every command here is local: no model call, no provider submit, no Worker and no MCP
server, so the suite runs with empty credentials and no network.
"""

import json
import os
import subprocess
import sys

from shot_support import (
    generation_args,
    m2_run,
    mock_tools,
    png_bytes,
    save_storyboard,
    shot,
)

from vagent.storage import FileStore
from vagent.video import clients
from vagent.waiting import ToolExecutionContext

# A child process only needs the system variables; the harness's own environment can
# push the whole Windows environment block past its 32K limit.
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


def command(directory, home, *args, **changes):
    wanted = {name.upper() for name in CHILD_ENVIRONMENT}
    env = {key: value for key, value in os.environ.items() if key.upper() in wanted}
    env.update(
        VAGENT_HOME=str(home),
        VAGENT_VIDEO_MODE="mock",
        PYTHONIOENCODING="utf-8",
        VAGENT_DASHSCOPE_KEY="",
        VAGENT_DASHSCOPE_WORKSPACE_ID="",
        VAGENT_DEEPSEEK_KEY="",
        DEEPSEEK_API_KEY="",
        VAGENT_MCP_CONFIG=str(directory / "missing-mcp.json"),
    )
    env.update(changes)
    return subprocess.run(
        [sys.executable, "-m", "vagent", *args],
        cwd=directory,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=60,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
    )


def seed(home):
    """One project with a saved storyboard and a registered batch, written offline."""
    with FileStore.open(home) as store:
        registry, _, _, _ = mock_tools(store)
        saved = save_storyboard(registry, store, [shot("镜头一"), shot("镜头二")])
        m2_run(store, "seed-run", mode="mock")
        context = ToolExecutionContext(
            project_id="coffee",
            session_id="coffee",
            run_id="seed-run",
            model_step=1,
            tool_call_id="batch",
        )
        registered = registry.execute(
            "shot_generate",
            generation_args(saved),
            store=store,
            project_id="coffee",
            operation_key=context.operation_key,
            context=context,
        )
        assert registered["ok"], registered
        return saved, [item["jobId"] for item in registered["data"]["shots"]]


def test_cli_storyboard_show_reads_local_state_without_keys(tmp_path):
    home = tmp_path / "state"
    saved, job_ids = seed(home)
    before = json.loads((home / "state.json").read_bytes())
    result = command(tmp_path, home, "storyboard", "show", "-s", "coffee")
    assert result.returncode == 0, result.stderr
    view = json.loads(result.stdout)
    assert view["storyboardId"] == saved["storyboardId"] and view["shotCount"] == 2
    assert [item["id"] for item in view["shots"]] == saved["shotIds"]
    assert [item["latest"]["jobId"] for item in view["shots"]] == job_ids
    assert all(item["latest"]["status"] == "pending_submit" for item in view["shots"])
    explicit = command(
        tmp_path, home, "storyboard", "show", "-s", "coffee", "--storyboard", saved["storyboardId"]
    )
    assert json.loads(explicit.stdout) == view
    absent = command(tmp_path, home, "storyboard", "show", "-s", "other")
    assert absent.returncode == 1 and "NOT_FOUND" in absent.stderr
    assert json.loads((home / "state.json").read_bytes()) == before
    assert not (home / "instance.lock").exists()


def test_cli_shots_redo_registers_one_job_and_replays_the_same_request(tmp_path):
    home = tmp_path / "state"
    saved, job_ids = seed(home)
    target = saved["shotIds"][1]
    result = command(tmp_path, home, "shots", "redo", target, "--request-id", "cli-redo-1")
    assert result.returncode == 0, result.stderr
    body = json.loads(result.stdout)
    assert body["shots"][0]["shotId"] == target and body["mode"] == "mock"
    assert body["shots"][0]["jobId"] not in job_ids
    assert body["runId"] == clients.client_run_id("coffee", "cli-redo-1")
    assert body["storyboardRevision"] == 2
    replay = command(tmp_path, home, "shots", "redo", target, "--request-id", "cli-redo-1")
    assert replay.returncode == 0 and json.loads(replay.stdout) == body
    state = json.loads((home / "state.json").read_bytes())
    assert len(state["jobs"]) == 3
    assert len(state["storyboards"][saved["storyboardId"]]["shotVersions"]) == 3
    # The local action owns its own Run; the conversation Run stays the session's latest.
    assert state["sessions"]["coffee"]["latestRunId"] == "seed-run"
    assert clients.client_run_id("coffee", "cli-redo-1") in state["runs"]
    checked = command(tmp_path, home, "shots", "redo", target, "-s", "other", "--request-id", "cli-redo-3")
    assert checked.returncode == 1 and "NOT_FOUND" in checked.stderr
    unknown = command(
        tmp_path, home, "shots", "redo", "44444444-4444-4444-4444-444444444444", "--request-id", "cli-redo-2"
    )
    assert unknown.returncode == 1 and "SHOT_NOT_FOUND" in unknown.stderr
    assert len(json.loads((home / "state.json").read_bytes())["jobs"]) == 3
    assert not (home / "instance.lock").exists()


def test_cli_assets_import_and_list_validate_without_a_model(tmp_path):
    home = tmp_path / "state"
    seed(home)
    source = tmp_path / "reference.png"
    source.write_bytes(png_bytes(1280, 720))
    imported = command(tmp_path, home, "assets", "import", str(source), "-s", "coffee")
    assert imported.returncode == 0, imported.stderr
    body = json.loads(imported.stdout)
    assert body["failed"] == [] and body["projectId"] == "coffee"
    asset = body["imported"][0]
    assert asset["mimeType"] == "image/png" and asset["deduplicated"] is False
    assert "relativePath" not in imported.stdout and str(home) not in imported.stdout
    again = command(tmp_path, home, "assets", "import", str(source), "-s", "coffee")
    assert json.loads(again.stdout)["imported"][0]["deduplicated"] is True
    listing = command(tmp_path, home, "assets", "list")
    assert [item["assetId"] for item in json.loads(listing.stdout)["assets"]] == [asset["assetId"]]
    assert json.loads(command(tmp_path, home, "assets", "list", "-s", "other").stdout)["assets"] == []
    notes = tmp_path / "notes.txt"
    notes.write_text("not an image", encoding="utf-8")
    refused = command(tmp_path, home, "assets", "import", str(notes))
    assert refused.returncode == 1
    failure = json.loads(refused.stdout)
    assert failure["imported"] == [] and failure["failed"][0]["error"]["code"] == ("IMAGE_FORMAT_UNSUPPORTED")
    missing = command(tmp_path, home, "assets", "import", str(tmp_path / "absent.png"))
    assert missing.returncode == 1
    assert json.loads(missing.stdout)["failed"][0]["error"]["code"] == "ASSET_IMPORT_UNREADABLE"
    assert len(json.loads((home / "state.json").read_bytes())["assets"]) == 1
    assert not (home / "instance.lock").exists()


def test_cli_usage_project_aggregates_jobs_cost_and_library(tmp_path):
    home = tmp_path / "state"
    saved, _ = seed(home)
    source = tmp_path / "reference.png"
    source.write_bytes(png_bytes(1280, 720))
    assert command(tmp_path, home, "assets", "import", str(source), "-s", "coffee").returncode == 0
    result = command(tmp_path, home, "usage", "--project", "coffee")
    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    assert report["projectId"] == "coffee"
    assert report["jobs"]["total"] == 2 and report["jobs"]["mock"] == 2
    assert report["cost"]["estimateAmount"] == "0.00"
    assert report["cost"]["actual"]["unknownJobs"] == 0
    assert report["assets"] == {"total": 1, "libraryTotal": 1}
    assert report["storyboards"]["total"] == 1 and report["storyboards"]["shotVersions"] == 2
    assert report["runs"]["runs"] == 2 and report["runs"]["recordedCallCount"] == 0
    everything = json.loads(command(tmp_path, home, "usage", "--project").stdout)
    assert everything["projectId"] is None and everything["jobs"]["total"] == 2
    absent = command(tmp_path, home, "usage", "--project", "other")
    assert absent.returncode == 1 and "NOT_FOUND" in absent.stderr
    assert saved["storyboardId"]
    assert not (home / "instance.lock").exists()
