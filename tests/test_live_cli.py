import json
import os
import subprocess
import sys
from datetime import UTC, datetime

import httpx
from wan_support import VIDEO_URL, live_arguments, live_run, live_service, provider_response

from vagent.storage import FileStore
from vagent.video.worker import JobWorker


def command(directory, home, *args, **video_env):
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith("VAGENT_") and key != "DEEPSEEK_API_KEY"
    }
    env.update(
        VAGENT_HOME=str(home),
        VAGENT_VIDEO_MODE="off",
        PYTHONIOENCODING="utf-8",
        VAGENT_DASHSCOPE_KEY="",
        VAGENT_DASHSCOPE_WORKSPACE_ID="",
        VAGENT_DEEPSEEK_KEY="",
        DEEPSEEK_API_KEY="",
        VAGENT_MCP_CONFIG=str(directory / "missing-mcp.json"),
    )
    env.update(video_env)
    return subprocess.run(
        [sys.executable, "-m", "vagent", "jobs", *args],
        cwd=directory,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=15,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
    )


async def test_live_cli_inspection_redacts_private_output_and_starts_no_services(tmp_path, video_clock):
    home = tmp_path / "state"
    with FileStore.open(home) as store:
        context = live_run(store)
        async with live_service(
            store,
            video_clock,
            lambda _: httpx.Response(200, json=provider_response("SUCCEEDED", video_url=VIDEO_URL)),
        ) as (service, _):
            job_id = service.generate(live_arguments(), context=context)["data"]["jobId"]
            job = await JobWorker(service).run_once()
            assert job.status == "downloading"
            original = store.snapshot()
    for args in (("list", "--session", "coffee"), ("get", job_id)):
        result = command(tmp_path, home, *args)
        assert result.returncode == 0, result.stderr
        data = json.loads(result.stdout)
        view = data["jobs"][0] if "jobs" in data else data
        assert view["status"] == "downloading" and not view["simulated"] and not view["mediaAvailable"]
        assert view["cost"]["actual"]["amount"] is None
        assert "private-signature" not in result.stdout and "workspaceId" not in result.stdout
    assert json.loads((home / "state.json").read_bytes()) == original
    assert not (home / "instance.lock").exists()


async def test_live_cli_retry_checks_original_scope_and_never_calls_http(tmp_path, video_clock):
    home = tmp_path / "state"
    video_clock.value = datetime.now(UTC)

    def handler(request):
        return httpx.Response(
            200, json=provider_response("RUNNING" if request.method == "POST" else "UNKNOWN")
        )

    with FileStore.open(home) as store:
        context = live_run(store)
        async with live_service(store, video_clock, handler) as (service, _):
            job_id = service.generate(live_arguments(), context=context)["data"]["jobId"]
            worker = JobWorker(service)
            video_clock.due(await worker.run_once())
            paused = await worker.run_once()
            assert paused.query_state == "paused" and paused.query_attempts == 1
            original = store.snapshot()
    missing = command(tmp_path, home, "retry-query", job_id)
    assert missing.returncode == 1 and "VIDEO_KEY_MISSING" in missing.stderr
    wrong = command(
        tmp_path,
        home,
        "retry-query",
        job_id,
        VAGENT_DASHSCOPE_KEY="test-secret",
        VAGENT_DASHSCOPE_WORKSPACE_ID="wrong-space",
    )
    assert wrong.returncode == 1 and "VIDEO_PROVIDER_UNAVAILABLE" in wrong.stderr
    assert json.loads((home / "state.json").read_bytes()) == original
    resumed = command(
        tmp_path,
        home,
        "retry-query",
        job_id,
        VAGENT_DASHSCOPE_KEY="test-secret",
        VAGENT_DASHSCOPE_WORKSPACE_ID="test-workspace",
        VAGENT_VIDEO_MAX_JOB_COST="0",
    )
    assert resumed.returncode == 0, resumed.stderr
    job = json.loads(resumed.stdout)
    assert job["providerTaskId"] == "wan-original" and job["queryState"] == "polling"
    assert job["submitAttempts"] == 1 and job["queryAttempts"] == 1
    assert job["queryPauseReason"] is None and "test-secret" not in resumed.stdout
    after = json.loads((home / "state.json").read_bytes())
    assert after["jobs"][job_id]["consecutiveQueryErrors"] == 0
    assert after["operations"] == original["operations"]
