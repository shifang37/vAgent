"""M8 E2: the local HTTP faces of the billing ledger.

``POST /api/usage/bill`` is a trusted local write next to ``/api/audio/import`` — it
needs a CSRF token and an explicit ``confirm``, and it never contacts a provider.
``GET /api/usage/bill`` is read-only and ``GET /api/usage`` appends the ``billing``
block (plus the non-durable ``runtime`` block when asked for).
"""

from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta

import httpx
from m3_support import kling_service
from wan_support import live_run

from vagent.config import Config
from vagent.storage import FileStore
from vagent.video.contracts import KLING_MODEL
from vagent.video.worker import JobWorker
from vagent.web import create_app

CLOCK = datetime(2030, 1, 1, tzinfo=UTC)


class Clock:
    def __init__(self):
        self.value = CLOCK

    def __call__(self):
        return self.value

    def advance(self, seconds=2):
        self.value += timedelta(seconds=seconds)


def kling_request():
    return {
        "provider": "kling",
        "model": KLING_MODEL,
        "capabilitiesVersion": "multi-provider-v3",
        "prompt": "雨夜咖啡店",
        "spec": {"durationSeconds": 5, "resolution": "720p", "aspectRatio": "16:9"},
    }


async def seed_terminal_job(home):
    """Register one Kling Job and fail it against a controlled 401, then close."""
    clock = Clock()

    def handler(request):
        return httpx.Response(401, json={"code": 1001, "message": "invalid key"})

    with FileStore.open(home) as store:
        async with kling_service(store, clock, handler) as (service, _):
            result = service.generate(kling_request(), context=live_run(store, "api-bill-run"))
            assert result["ok"], result
            job = await JobWorker(service).run_once()
            assert job.status == "failed"
            return job.id


@asynccontextmanager
async def client_for(home):
    config = Config(home=home, api_key=None, video_mode="mock")
    app = create_app(config)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:3210"
        ) as client:
            client.headers["X-CSRF-Token"] = (await client.get("/api/session-token")).json()["token"]
            yield client, app.state.service


def body(job_id, **changes):
    payload = {
        "jobId": job_id,
        "amount": "0.56",
        "currency": "USD",
        "source": "Kling 控制台 2026-10-25 账单",
        "invoicedOn": "2026-10-25",
        "confirm": True,
    }
    payload.update(changes)
    return payload


async def test_the_bill_state_module_is_served_to_the_panel(tmp_path):
    home = tmp_path / "home"
    with FileStore.open(home):
        pass
    async with client_for(home) as (client, _):
        page = await client.get("/")
        assert page.status_code == 200
        for name in ("app.js", "bill-state.js"):
            asset = await client.get(f"/{name}")
            assert asset.status_code == 200, name
            assert "javascript" in asset.headers["content-type"]
        script = (await client.get("/bill-state.js")).text
        assert "billing-v1" in script or "BILL_LEDGER_VERSION" in script
        assert "billRecordBody" in script and "billingCurrencyRows" in script
        # M8 E2 §5.5: a throttled submit is rendered as normal waiting.
        app_script = (await client.get("/app.js")).text
        assert "等待节流" in app_script and "PROVIDER_BACKOFF" in app_script
        assert "record-bill" in app_script and "billLedgerPath" in app_script


async def test_the_ledger_starts_empty_and_stays_a_sidecar(tmp_path):
    home = tmp_path / "home"
    await seed_terminal_job(home)
    state_before = (home / "state.json").read_bytes()
    async with client_for(home) as (client, _):
        view = (await client.get("/api/usage/bill")).json()
        assert view["ledgerVersion"] == 1
        assert view["ledgerFile"] == "billing.json"
        assert view["ledgerStatus"] == "missing" and view["records"] == []
        assert view["billing"]["reportedJobs"] == 0
        usage = (await client.get("/api/usage")).json()
        assert usage["billing"]["ledgerStatus"] == "missing"
        assert usage["billing"]["currencies"][0]["reported"] is None
        assert any("不换汇" in note for note in usage["billing"]["notes"])
        assert not (home / "billing.json").exists()
    assert (home / "state.json").read_bytes() == state_before


async def test_a_bill_requires_an_explicit_confirmation_and_a_csrf_token(tmp_path):
    home = tmp_path / "home"
    job_id = await seed_terminal_job(home)
    async with client_for(home) as (client, _):
        refused = await client.post("/api/usage/bill", json=body(job_id, confirm=False))
        assert refused.status_code == 400
        assert refused.json()["error"]["code"] == "CONFIRMATION_REQUIRED"
        assert not (home / "billing.json").exists()

        tokenless = await client.post(
            "/api/usage/bill",
            json=body(job_id),
            headers={"X-CSRF-Token": "wrong-token"},
        )
        assert tokenless.status_code == 403
        assert tokenless.json()["error"]["code"] == "CSRF_DENIED"
        assert not (home / "billing.json").exists()


async def test_recording_a_bill_shows_up_in_usage_immediately(tmp_path):
    home = tmp_path / "home"
    job_id = await seed_terminal_job(home)
    state_before = (home / "state.json").read_bytes()
    async with client_for(home) as (client, _):
        recorded = await client.post("/api/usage/bill", json=body(job_id))
        assert recorded.status_code == 201, recorded.text
        assert recorded.json()["recorded"] is True and recorded.json()["replayed"] is False
        assert recorded.json()["record"]["currency"] == "USD"
        assert recorded.json()["record"]["revision"] == 1

        replay = await client.post("/api/usage/bill", json=body(job_id))
        assert replay.status_code == 201 and replay.json()["replayed"] is True

        view = (await client.get("/api/usage/bill")).json()
        assert view["ledgerStatus"] == "ok"
        assert [item["jobId"] for item in view["records"]] == [job_id]

        usage = (await client.get("/api/usage")).json()
        assert usage["billing"]["ledgerStatus"] == "ok"
        usd = next(item for item in usage["billing"]["currencies"] if item["currency"] == "USD")
        assert (usd["reported"], usd["estimate"], usd["delta"]) == ("0.56", "0.56", "0.00")
        assert usd["reportedJobs"] == 1 and usd["unknownJobs"] == 0
        # The frozen Job record is untouched: bills live in the sidecar only.
        assert usage["jobs"]["total"] == 1
    assert (home / "state.json").read_bytes() == state_before


async def test_conflicts_and_invalid_bills_use_the_frozen_codes(tmp_path):
    home = tmp_path / "home"
    job_id = await seed_terminal_job(home)
    async with client_for(home) as (client, _):
        assert (await client.post("/api/usage/bill", json=body(job_id))).status_code == 201

        conflict = await client.post("/api/usage/bill", json=body(job_id, amount="0.60"))
        assert conflict.status_code == 409
        assert conflict.json()["error"]["code"] == "BILL_CONFLICT"

        wrong_currency = await client.post("/api/usage/bill", json=body("no-such-job", currency="CNY"))
        assert wrong_currency.status_code == 400
        assert wrong_currency.json()["error"]["code"] == "BILL_RECORD_INVALID"

        mismatch = await client.post(
            "/api/usage/bill",
            json=body(job_id, amount="3.00", currency="CNY", replace=True, expectedRevision=1),
        )
        assert mismatch.status_code == 400
        assert mismatch.json()["error"]["code"] == "BILL_RECORD_INVALID"

        replaced = await client.post(
            "/api/usage/bill",
            json=body(job_id, amount="0.60", replace=True, expectedRevision=1),
        )
        assert replaced.status_code == 201
        assert replaced.json()["record"]["revision"] == 2
        stale = await client.post(
            "/api/usage/bill",
            json=body(job_id, amount="0.70", replace=True, expectedRevision=1),
        )
        assert stale.status_code == 409 and stale.json()["error"]["code"] == "BILL_CONFLICT"


async def test_usage_runtime_is_opt_in_and_labelled_not_durable(tmp_path):
    home = tmp_path / "home"
    await seed_terminal_job(home)
    async with client_for(home) as (client, _):
        assert "runtime" not in (await client.get("/api/usage")).json()
        with_runtime = (await client.get("/api/usage?runtime=true")).json()
        assert with_runtime["runtime"]["durable"] is False
        assert with_runtime["runtime"]["window"] == 64
        assert "重启归零" in with_runtime["runtime"]["note"]
