"""M8 E2: `vagent usage bill record|show` is local and never contacts a provider.

The CLI writes the ``billing.json`` sidecar only, requires an explicit ``--confirm``,
and leaves ``state.json`` byte-identical (a reported bill never rewrites a Job).
"""

import json
import os
import subprocess
import sys
from datetime import UTC, datetime

import httpx
from m3_support import kling_service
from wan_support import live_run

from vagent.storage import FileStore
from vagent.video.contracts import KLING_MODEL
from vagent.video.worker import JobWorker

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

SOURCE = "Kling 控制台 2026-10-25 账单"


class Clock:
    def __init__(self):
        self.value = datetime(2030, 1, 1, tzinfo=UTC)

    def __call__(self):
        return self.value


def command(directory, home, *args):
    wanted = {name.upper() for name in CHILD_ENVIRONMENT}
    env = {key: value for key, value in os.environ.items() if key.upper() in wanted}
    env.update(
        VAGENT_HOME=str(home),
        VAGENT_VIDEO_MODE="off",
        VAGENT_DASHSCOPE_KEY="",
        VAGENT_DASHSCOPE_WORKSPACE_ID="",
        VAGENT_KLING_API_KEY="",
        FAL_KEY="",
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


async def seed_terminal_job(home):
    clock = Clock()

    def handler(request):
        return httpx.Response(401, json={"code": 1001, "message": "invalid key"})

    with FileStore.open(home) as store:
        async with kling_service(store, clock, handler) as (service, _):
            request = {
                "provider": "kling",
                "model": KLING_MODEL,
                "capabilitiesVersion": "multi-provider-v3",
                "prompt": "雨夜咖啡店",
                "spec": {"durationSeconds": 5, "resolution": "720p", "aspectRatio": "16:9"},
            }
            registered = service.generate(request, context=live_run(store, "cli-bill-run"))
            assert registered["ok"], registered
            job = await JobWorker(service).run_once()
            assert job.status == "failed"
            return job.id


def bill_args(job_id, *extra):
    return (
        "usage",
        "bill",
        "record",
        "--job",
        job_id,
        "--amount",
        "0.56",
        "--currency",
        "USD",
        "--source",
        SOURCE,
        "--invoiced-on",
        "2026-10-25",
        *extra,
    )


async def test_cli_record_requires_confirm_and_never_writes_the_state_file(tmp_path):
    home = tmp_path / "state"
    job_id = await seed_terminal_job(home)
    before = (home / "state.json").read_bytes()

    refused = command(tmp_path, home, *bill_args(job_id))
    assert refused.returncode == 1
    assert "CONFIRMATION_REQUIRED" in refused.stderr
    assert not (home / "billing.json").exists()

    recorded = command(tmp_path, home, *bill_args(job_id, "--confirm"))
    assert recorded.returncode == 0, recorded.stderr
    view = json.loads(recorded.stdout)
    assert view["recorded"] is True and view["replayed"] is False
    assert view["record"]["amount"] == "0.56" and view["record"]["currency"] == "USD"
    assert view["record"]["invoicedOn"] == "2026-10-25"
    assert view["record"]["revision"] == 1

    ledger = json.loads((home / "billing.json").read_bytes())
    assert ledger["billingVersion"] == 1 and list(ledger["records"]) == [job_id]
    assert (home / "state.json").read_bytes() == before

    replay = command(tmp_path, home, *bill_args(job_id, "--confirm"))
    assert replay.returncode == 0, replay.stderr
    assert json.loads(replay.stdout)["replayed"] is True


async def test_cli_show_reads_back_the_ledger_additively(tmp_path):
    home = tmp_path / "state"
    job_id = await seed_terminal_job(home)
    empty = command(tmp_path, home, "usage", "bill", "show")
    assert empty.returncode == 0, empty.stderr
    assert json.loads(empty.stdout)["ledgerStatus"] == "missing"
    assert json.loads(empty.stdout)["records"] == []

    assert command(tmp_path, home, *bill_args(job_id, "--confirm")).returncode == 0
    shown = command(tmp_path, home, "usage", "bill", "show")
    assert shown.returncode == 0, shown.stderr
    view = json.loads(shown.stdout)
    assert view["ledgerStatus"] == "ok"
    assert [item["jobId"] for item in view["records"]] == [job_id]
    usd = next(item for item in view["billing"]["currencies"] if item["currency"] == "USD")
    assert (usd["reported"], usd["estimate"]) == ("0.56", "0.56")
    assert any("不换汇" in note for note in view["notes"])

    filtered = command(tmp_path, home, "usage", "bill", "show", "--currency", "CNY")
    assert filtered.returncode == 0, filtered.stderr
    assert json.loads(filtered.stdout)["records"] == []


async def test_cli_conflicts_and_invalid_bills_keep_the_original_record(tmp_path):
    home = tmp_path / "state"
    job_id = await seed_terminal_job(home)
    assert command(tmp_path, home, *bill_args(job_id, "--confirm")).returncode == 0

    conflict = command(tmp_path, home, *bill_args(job_id, "--amount", "0.60", "--confirm"))
    assert conflict.returncode == 1
    assert "BILL_CONFLICT" in conflict.stderr
    ledger = json.loads((home / "billing.json").read_bytes())
    assert ledger["records"][job_id]["amount"] == "0.56"

    replaced = command(
        tmp_path,
        home,
        *bill_args(job_id, "--amount", "0.60", "--replace", "--expected-revision", "1", "--confirm"),
    )
    assert replaced.returncode == 0, replaced.stderr
    assert json.loads(replaced.stdout)["record"]["revision"] == 2

    wrong_currency = command(
        tmp_path,
        home,
        *bill_args(job_id, "--amount", "3.00", "--currency", "CNY", "--confirm"),
    )
    assert wrong_currency.returncode == 1
    assert "BILL_RECORD_INVALID" in wrong_currency.stderr

    missing = command(tmp_path, home, *bill_args("no-such-job", "--confirm"))
    assert missing.returncode == 1 and "BILL_RECORD_INVALID" in missing.stderr


async def test_cli_usage_reports_the_billing_block_and_non_durable_runtime(tmp_path):
    home = tmp_path / "state"
    job_id = await seed_terminal_job(home)
    assert command(tmp_path, home, *bill_args(job_id, "--confirm")).returncode == 0
    shown = command(tmp_path, home, "usage", "--project", "all")
    assert shown.returncode == 0, shown.stderr
    view = json.loads(shown.stdout)
    assert view["billing"]["ledgerStatus"] == "ok"
    usd = next(item for item in view["billing"]["currencies"] if item["currency"] == "USD")
    assert usd["reported"] == "0.56"
    # A fresh CLI process has no live Worker, so the in-memory counters are empty
    # but the block must still be honest about being non-durable.
    assert view["runtime"]["durable"] is False
    assert view["runtime"]["backoffCount"] == 0
