"""M8 E2: the ``billing-v1`` sidecar ledger.

The ledger records a hand-verified provider charge: it never scrapes a provider,
never treats a ``usage`` field as money, never converts a currency and never rewrites
a Job. ``billing.json`` is a sidecar, so ``state.json`` and the 168 frozen model
hashes are untouched by every case here.
"""

import json
import os
from contextlib import asynccontextmanager
from datetime import UTC, datetime

import httpx
import pytest
from m3_support import kling_service
from wan_support import live_arguments, live_run, live_service

from vagent.errors import AppError
from vagent.video import billing
from vagent.video.contracts import KLING_MODEL, parse_job
from vagent.video.usage import summarize_records
from vagent.video.worker import JobWorker

CLOCK = datetime(2026, 10, 25, 9, 0, tzinfo=UTC)
SOURCE = "Kling 控制台 2026-10-25 账单"


def kling_request():
    return {
        "provider": "kling",
        "model": KLING_MODEL,
        "capabilitiesVersion": "multi-provider-v3",
        "prompt": "雨夜咖啡店",
        "spec": {"durationSeconds": 5, "resolution": "720p", "aspectRatio": "16:9"},
    }


@asynccontextmanager
async def failed_kling_job(store, clock, run_id="bill-kling", **config_changes):
    def handler(request):
        return httpx.Response(401, json={"code": 1001, "message": "invalid key"})

    async with kling_service(store, clock, handler, **config_changes) as (service, _):
        result = service.generate(kling_request(), context=live_run(store, run_id))
        assert result["ok"], result
        job = await JobWorker(service).run_once()
        assert job.status == "failed" and job.cost.estimate.currency == "USD"
        yield job.id


@asynccontextmanager
async def failed_wan_job(store, clock, run_id="bill-wan", **config_changes):
    def handler(request):
        return httpx.Response(401, json={"code": "InvalidApiKey", "message": "invalid key"})

    async with live_service(store, clock, handler, **config_changes) as (service, _):
        result = service.generate(live_arguments(), context=live_run(store, run_id))
        assert result["ok"], result
        job = await JobWorker(service).run_once()
        assert job.status == "failed" and job.cost.estimate.currency == "CNY"
        yield job.id


def record(store, job_id, **changes):
    payload = {
        "job_id": job_id,
        "amount": "0.56",
        "currency": "USD",
        "source": SOURCE,
        "clock": lambda: CLOCK,
        "store": store,
    }
    payload.update(changes)
    return billing.record_bill(store.snapshot(), store.home, **payload)


def billing_file(store):
    return store.home / billing.BILLING_FILE


async def test_a_reported_charge_is_written_once_and_replayed_idempotently(store, video_clock):
    async with failed_kling_job(store, video_clock) as job_id:
        pass
    state_file = store.home / "state.json"
    before = state_file.read_bytes()

    first = record(store, job_id)
    assert first["recorded"] is True and first["replayed"] is False
    assert first["ledgerStatus"] == "ok"
    assert first["record"]["revision"] == 1
    assert first["record"]["currency"] == "USD" and first["record"]["amount"] == "0.56"
    assert first["record"]["invoicedOn"] == "2026-10-25"
    assert first["record"]["recordedAt"] == CLOCK.isoformat()
    assert first["record"]["provider"] == "kling"
    assert first["record"]["projectId"] == "coffee"

    ledger = json.loads(billing_file(store).read_bytes())
    assert ledger["billingVersion"] == 1 and list(ledger["records"]) == [job_id]
    written = billing_file(store).read_bytes()
    # The ledger is a sidecar: state.json is byte-identical after a recorded charge.
    assert state_file.read_bytes() == before

    later = datetime(2026, 10, 26, 18, 30, tzinfo=UTC)
    replay = record(store, job_id, clock=lambda: later, invoiced_on="2026-10-25")
    assert replay["recorded"] is False and replay["replayed"] is True
    assert replay["record"]["revision"] == 1
    # A different timestamp never turns the same bill into a conflict or a rewrite.
    assert replay["record"]["recordedAt"] == CLOCK.isoformat()
    assert billing_file(store).read_bytes() == written


async def test_a_different_amount_is_a_conflict_until_an_explicit_replacement(store, video_clock):
    async with failed_kling_job(store, video_clock) as job_id:
        pass
    record(store, job_id)

    with pytest.raises(AppError) as raised:
        record(store, job_id, amount="0.60")
    assert raised.value.code == "BILL_CONFLICT"
    assert json.loads(billing_file(store).read_bytes())["records"][job_id]["amount"] == "0.56"

    with pytest.raises(AppError) as raised:
        record(store, job_id, amount="0.60", replace=True)
    assert raised.value.code == "BILL_RECORD_INVALID"

    with pytest.raises(AppError) as raised:
        record(store, job_id, amount="0.60", replace=True, expected_revision=7)
    assert raised.value.code == "BILL_CONFLICT"

    replaced = record(store, job_id, amount="0.60", replace=True, expected_revision=1)
    assert replaced["recorded"] is True
    assert replaced["record"]["revision"] == 2 and replaced["record"]["amount"] == "0.60"
    ledger = json.loads(billing_file(store).read_bytes())
    assert ledger["records"][job_id]["revision"] == 2


async def test_the_currency_must_equal_the_frozen_quote(store, video_clock):
    async with failed_kling_job(store, video_clock) as job_id:
        pass
    with pytest.raises(AppError) as raised:
        record(store, job_id, amount="3.00", currency="CNY")
    assert raised.value.code == "BILL_RECORD_INVALID"
    assert "不换汇" in str(raised.value)
    assert not billing_file(store).exists()


async def test_only_a_terminal_quoted_job_can_be_billed(store, video_clock):
    from conftest import video_request, video_run

    from vagent.config import Config
    from vagent.video.jobs import JobService
    from vagent.video.providers.mock import MockVideoAdapter

    adapter = MockVideoAdapter(store, clock=video_clock)
    service = JobService(
        store, [adapter], clock=video_clock, config=Config(home=store.home, api_key=None, video_mode="mock")
    )
    context = video_run(store, "mock-run")
    mock_job = service.generate(video_request(adapter), context=context)["data"]["jobId"]

    # Still pending: an in-flight Job is adjusted by the provider, so it is refused.
    with pytest.raises(AppError) as raised:
        record(store, mock_job, amount="3.00", currency="CNY")
    assert raised.value.code == "BILL_RECORD_INVALID"

    # Unknown Job ID: refused, nothing written.
    with pytest.raises(AppError) as raised:
        record(store, "no-such-job")
    assert raised.value.code == "BILL_RECORD_INVALID"

    # Terminal but simulated: no frozen quote, so no provider money can be attached.
    # The record is only projected in memory; the stored Job stays pending_submit.
    terminal_mock = json.loads(json.dumps(store.snapshot()["jobs"][mock_job]))
    terminal_mock.update(
        status="failed",
        submitAttempts=1,
        error={"stage": "submit", "code": "SUBMISSION_UNKNOWN", "message": "模拟任务不提交上游。"},
    )
    with pytest.raises(AppError) as raised:
        billing.record_bill(
            {"jobs": {mock_job: terminal_mock}},
            store.home,
            job_id=mock_job,
            amount="3.00",
            currency="CNY",
            source=SOURCE,
            clock=lambda: CLOCK,
        )
    assert raised.value.code == "BILL_RECORD_INVALID"
    assert not billing_file(store).exists()
    assert parse_job(store.snapshot()["jobs"][mock_job]).status == "pending_submit"


async def test_invalid_fields_are_rejected_without_writing(store, video_clock):
    async with failed_kling_job(store, video_clock) as job_id:
        pass
    cases = (
        {"amount": "not-money"},
        {"amount": "1.234"},
        {"amount": "-1.00"},
        {"amount": "100000.00"},
        {"source": ""},
        {"source": "   "},
        {"invoiced_on": "2026-13-01"},
        {"invoiced_on": "2026-10-32"},
        {"note": "x" * 501},
    )
    for changes in cases:
        with pytest.raises(AppError) as raised:
            record(store, job_id, **changes)
        assert raised.value.code == "BILL_RECORD_INVALID", changes
    assert not billing_file(store).exists()


async def test_a_damaged_ledger_is_reported_and_never_overwritten(store, video_clock):
    async with failed_kling_job(store, video_clock) as job_id:
        pass
    billing_file(store).write_text('{"billingVersion": 1, "records": {', encoding="utf-8")
    damaged = billing_file(store).read_bytes()
    assert billing.read_ledger(store.home)[1] == "invalid"

    with pytest.raises(AppError) as raised:
        record(store, job_id)
    assert raised.value.code == "INVALID_STORE"
    assert billing_file(store).read_bytes() == damaged

    report = summarize_records(store.snapshot(), home=store.home)
    assert report["billing"]["ledgerStatus"] == "invalid"
    assert report["billing"]["currencies"][0]["reported"] is None
    view = billing.ledger_view(store.snapshot(), store.home)
    assert view["ledgerStatus"] == "invalid" and view["records"] == []


async def test_summary_keeps_currencies_separate_and_unknown_is_not_zero(store, video_clock):
    async with failed_kling_job(store, video_clock) as usd_job:
        pass
    video_clock.advance(5)
    async with failed_wan_job(store, video_clock) as cny_job:
        pass
    record(store, usd_job)

    report = summarize_records(store.snapshot(), home=store.home)["billing"]
    assert report["ledgerVersion"] == 1 and report["ledgerStatus"] == "ok"
    assert [item["currency"] for item in report["currencies"]] == ["CNY", "USD"]
    by_currency = {item["currency"]: item for item in report["currencies"]}
    assert by_currency["CNY"] == {
        "currency": "CNY",
        "estimate": "3.00",
        "reported": None,
        "reportedJobs": 0,
        "unknownJobs": 1,
        "delta": None,
    }
    assert by_currency["USD"] == {
        "currency": "USD",
        "estimate": "0.56",
        "reported": "0.56",
        "reportedJobs": 1,
        "unknownJobs": 0,
        "delta": "0.00",
    }
    assert (report["reportedJobs"], report["unknownJobs"]) == (1, 1)
    # No cross-currency total exists anywhere in the block.
    assert not {"total", "grandTotal", "amount", "estimateAmount"} & set(report)
    assert any("不换汇" in note for note in report["notes"])

    filtered = summarize_records(store.snapshot(), home=store.home, project_id="coffee")["billing"]
    assert filtered == report
    assert (
        summarize_records(store.snapshot(), home=store.home, project_id="other")["billing"]["currencies"]
        == []
    )
    # The bill never rewrites the Job: its frozen actual cost stays unknown.
    stored = store.snapshot()["jobs"][cny_job]
    assert stored["cost"]["actual"] == {
        "status": "unknown",
        "currency": "CNY",
        "amount": None,
        "source": None,
    }
    assert "billing" not in json.dumps(stored)


async def test_the_record_cap_and_the_ledger_size_cap_are_enforced(store, video_clock, monkeypatch):
    async with failed_kling_job(store, video_clock, "cap-run-1") as first:
        pass
    video_clock.advance(5)
    async with failed_kling_job(store, video_clock, "cap-run-2") as second:
        pass
    monkeypatch.setattr(billing, "MAX_RECORDS_PER_PROJECT", 1)
    record(store, first)
    with pytest.raises(AppError) as raised:
        record(store, second)
    assert raised.value.code == "BILL_RECORD_INVALID"


async def test_the_ledger_size_cap_refuses_to_write(store, video_clock, monkeypatch):
    async with failed_kling_job(store, video_clock) as job_id:
        pass
    monkeypatch.setattr(billing, "MAX_LEDGER_BYTES", 64)
    with pytest.raises(AppError) as raised:
        record(store, job_id)
    assert raised.value.code == "BILL_RECORD_INVALID"
    assert not billing_file(store).exists()


@pytest.mark.skipif(os.name != "posix", reason="POSIX file modes do not exist on Windows")
async def test_the_ledger_file_is_owner_only(store, video_clock):
    async with failed_kling_job(store, video_clock) as job_id:
        pass
    record(store, job_id)
    assert billing_file(store).stat().st_mode & 0o777 == 0o600


async def test_the_read_only_view_filters_by_project_and_currency(store, video_clock):
    async with failed_kling_job(store, video_clock) as job_id:
        pass
    record(store, job_id)
    view = billing.ledger_view(store.snapshot(), store.home)
    assert view["ledgerVersion"] == 1 and view["ledgerFile"] == "billing.json"
    assert [item["jobId"] for item in view["records"]] == [job_id]
    assert view["billing"]["currencies"][0]["reported"] == "0.56"

    assert billing.ledger_view(store.snapshot(), store.home, project_id="other")["records"] == []
    assert billing.ledger_view(store.snapshot(), store.home, currency="CNY")["records"] == []
    with pytest.raises(AppError) as raised:
        billing.ledger_view(store.snapshot(), store.home, currency="EUR")
    assert raised.value.code == "BILL_RECORD_INVALID"
