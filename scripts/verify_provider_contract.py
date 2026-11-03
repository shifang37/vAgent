"""Replay the vendors' documented payloads through the real adapters, at zero cost.

The built-in mock provider and ``evaluate_m3`` serve Kling and fal from hand-written
fixtures. That proves the Job/Worker/film pipeline, but a field-name drift between an
adapter and the vendor specification stays invisible to both, because the fixture is
written from the adapter's assumptions rather than from the vendor's reference.

This verifier closes that gap without a credential, a DNS lookup or a paid task: each
case feeds the payload copied from the official reference into the production
``KlingAdapter`` / ``FalAdapter`` through ``httpx.MockTransport`` and asserts the parsed
result against the frozen contracts.

  Kling: https://kling.ai/document-api/api/video/3-0-turbo/text-to-video
         https://kling.ai/document-api/api/get-started/error-codes
  fal:   https://queue.fal.run (submit -> status -> app-root result)

Run it after touching a provider adapter or a price/error table:

    .\\.venv\\Scripts\\python.exe scripts\\verify_provider_contract.py

It exits 0 only when every check matches, and it never reaches the network: the adapter
clients are built with a MockTransport, so a wrong host or a leaked proxy cannot hide.
"""

import argparse
import asyncio
import json
import sys

import httpx

from vagent.video.providers.fal import FalAdapter
from vagent.video.providers.kling import KlingAdapter

# The query entry reports create_time/update_time as Unix milliseconds.
KLING_CREATE_TIME = 1781080778802
KLING_UPDATE_TIME = 1781080794151
KLING_SUBMIT_TIME = "2026-06-10T08:39:38.802+00:00"
KLING_END_TIME = "2026-06-10T08:39:54.151+00:00"
KLING_TASK_ID = "893605946402811985"
KLING_VIDEO_URL = "https://p1-kling.klingai.com/kcdn/demo.mp4"
FAL_TASK_ID = "fal-request-1"
FAL_VIDEO_URL = "https://v3.fal.media/files/example/result.mp4"
VENDOR_FAILURE_REASON = "content risk control triggered"

# The specification side of the error table, transcribed from the official reference.
# It is deliberately independent of the adapter's own mapping so a change to one is
# caught by the other.
OFFICIAL_SERVICE_CODES = {
    "1000": "PROVIDER_AUTH_FAILED",
    "1001": "PROVIDER_AUTH_FAILED",
    "1002": "PROVIDER_AUTH_FAILED",
    "1003": "PROVIDER_AUTH_FAILED",
    "1004": "PROVIDER_AUTH_FAILED",
    "1100": "PROVIDER_BILLING_BLOCKED",
    "1101": "PROVIDER_BILLING_BLOCKED",
    "1102": "PROVIDER_BILLING_BLOCKED",
    "1103": "PROVIDER_ACCESS_DENIED",
    "1200": "PROVIDER_INVALID_REQUEST",
    "1201": "PROVIDER_INVALID_REQUEST",
    "1202": "PROVIDER_INVALID_REQUEST",
    "1203": "PROVIDER_MODEL_UNAVAILABLE",
    "1300": "PROVIDER_CONTENT_REJECTED",
    "1301": "PROVIDER_CONTENT_REJECTED",
    "1302": "PROVIDER_RATE_LIMITED",
    "1303": "PROVIDER_RATE_LIMITED",
    "1304": "PROVIDER_ACCESS_DENIED",
}
# The HTTP status each official code arrives with, when the status is part of the spec.
OFFICIAL_CODE_STATUS = {
    "1000": 401,
    "1001": 401,
    "1002": 401,
    "1003": 401,
    "1004": 401,
    "1100": 429,
    "1101": 429,
    "1102": 429,
    "1103": 403,
    "1200": 400,
    "1201": 400,
    "1202": 404,
    "1203": 404,
    "1300": 400,
    "1301": 400,
    "1302": 429,
    "1303": 429,
    "1304": 429,
}


def _record(checks: list, name: str, expected, actual) -> None:
    checks.append({"check": name, "ok": expected == actual, "expected": expected, "actual": actual})


def kling_entry(**overrides) -> dict:
    """One query entry exactly as the Kling reference documents it."""
    entry = {
        "id": KLING_TASK_ID,
        "status": "succeeded",
        "message": "",
        "create_time": KLING_CREATE_TIME,
        "update_time": KLING_UPDATE_TIME,
        "external_id": "vag-abc123",
        "outputs": [
            {
                "type": "video",
                "id": "vid-1",
                "url": KLING_VIDEO_URL,
                "watermark_url": "https://p1-kling.klingai.com/kcdn/demo-wm.mp4",
                "duration": "5",
            }
        ],
        "billing": [{"charge_type": "unit", "amount": "4.0", "package_type": "video"}],
    }
    entry.update(overrides)
    return entry


def kling_transport(entry: dict) -> httpx.MockTransport:
    payload = {"code": 0, "message": "SUCCESS", "request_id": "req-0001", "data": [entry]}

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=payload)

    return httpx.MockTransport(handler)


async def check_kling_success(checks: list) -> None:
    adapter = KlingAdapter(api_key="contract-replay", transport=kling_transport(kling_entry()))
    async with adapter:
        snapshot = await adapter.query(KLING_TASK_ID)
    output = snapshot.output
    _record(checks, "kling.success.status", "succeeded", snapshot.status)
    if output is None:
        _record(checks, "kling.success.output", "present", "missing")
        return
    _record(checks, "kling.success.video_url", KLING_VIDEO_URL, output.video_url)
    _record(
        checks, "kling.success.result_origin", "https://p1-kling.klingai.com:443", output.result_source.origin
    )
    _record(checks, "kling.success.submit_time", KLING_SUBMIT_TIME, output.provider_times.submit_time)
    _record(checks, "kling.success.end_time", KLING_END_TIME, output.provider_times.end_time)
    _record(checks, "kling.success.usage", 5.0, output.usage.duration if output.usage else None)


async def check_kling_failure(checks: list) -> None:
    entry = kling_entry(status="failed", message=VENDOR_FAILURE_REASON)
    adapter = KlingAdapter(api_key="contract-replay", transport=kling_transport(entry))
    async with adapter:
        snapshot = await adapter.query(KLING_TASK_ID)
    error = snapshot.error
    _record(checks, "kling.failure.status", "failed", snapshot.status)
    _record(checks, "kling.failure.code", "PROVIDER_GENERATION_FAILED", error.code if error else None)
    # provider_code must stay a code-shaped token, so the vendor sentence rides the message.
    _record(checks, "kling.failure.provider_code", None, error.provider_code if error else "missing")
    _record(
        checks,
        "kling.failure.reason_carried",
        True,
        bool(error and VENDOR_FAILURE_REASON in error.message),
    )


async def check_kling_status_mapping(checks: list) -> None:
    """submitted/processing/succeeded/failed are the only documented remote states."""
    for remote, expected in (
        ("submitted", "queued"),
        ("processing", "running"),
        ("succeeded", "succeeded"),
        ("failed", "failed"),
    ):
        entry = kling_entry(status=remote)
        adapter = KlingAdapter(api_key="contract-replay", transport=kling_transport(entry))
        async with adapter:
            _record(
                checks, f"kling.status_map.{remote}", expected, (await adapter.query(KLING_TASK_ID)).status
            )


def check_kling_service_codes(checks: list) -> None:
    adapter = KlingAdapter(api_key="contract-replay")
    for code, expected in OFFICIAL_SERVICE_CODES.items():
        status = OFFICIAL_CODE_STATUS[code]
        _record(
            checks,
            f"kling.service_code.{code}",
            expected,
            adapter._rejection(status, {"code": int(code)}),
        )


def fal_transport(routes: dict[str, httpx.Response]) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        return routes.get(path, httpx.Response(404, json={"detail": f"no replay route for {path}"}))

    return httpx.MockTransport(handler)


def fal_result_routes(*, completed: httpx.Response) -> dict[str, httpx.Response]:
    return {
        f"/fal-ai/kling-video/requests/{FAL_TASK_ID}/status": httpx.Response(
            200, json={"request_id": FAL_TASK_ID, "status": "COMPLETED"}
        ),
        f"/fal-ai/kling-video/requests/{FAL_TASK_ID}": completed,
    }


async def check_fal_success(checks: list) -> None:
    routes = fal_result_routes(
        completed=httpx.Response(200, json={"request_id": FAL_TASK_ID, "video": {"url": FAL_VIDEO_URL}})
    )
    adapter = FalAdapter(api_key="contract-replay", transport=fal_transport(routes))
    async with adapter:
        snapshot = await adapter.query(FAL_TASK_ID)
    output = snapshot.output
    _record(checks, "fal.success.status", "succeeded", snapshot.status)
    _record(checks, "fal.success.video_url", FAL_VIDEO_URL, output.video_url if output else None)


async def check_fal_queue_states(checks: list) -> None:
    for remote, expected in (("IN_QUEUE", "queued"), ("IN_PROGRESS", "running")):
        routes = {
            f"/fal-ai/kling-video/requests/{FAL_TASK_ID}/status": httpx.Response(
                200, json={"request_id": FAL_TASK_ID, "status": remote}
            )
        }
        adapter = FalAdapter(api_key="contract-replay", transport=fal_transport(routes))
        async with adapter:
            _record(checks, f"fal.status_map.{remote}", expected, (await adapter.query(FAL_TASK_ID)).status)


async def check_fal_failure(checks: list) -> None:
    routes = fal_result_routes(
        completed=httpx.Response(200, json={"request_id": FAL_TASK_ID, "error": "content policy"})
    )
    adapter = FalAdapter(api_key="contract-replay", transport=fal_transport(routes))
    async with adapter:
        snapshot = await adapter.query(FAL_TASK_ID)
    _record(checks, "fal.failure.status", "failed", snapshot.status)


async def run() -> dict:
    """Run every replay case and return the machine-readable report."""
    checks: list = []
    await check_kling_success(checks)
    await check_kling_failure(checks)
    await check_kling_status_mapping(checks)
    check_kling_service_codes(checks)
    await check_fal_success(checks)
    await check_fal_queue_states(checks)
    await check_fal_failure(checks)
    failed = [item for item in checks if not item["ok"]]
    return {
        "status": "passed" if not failed else "failed",
        "mode": "offline-replay",
        "networkCalls": 0,
        "failedChecks": [item["check"] for item in failed],
        "totals": {"checks": len(checks), "passed": len(checks) - len(failed), "failed": len(failed)},
        "checks": checks,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=str, default=None, help="可选：把完整报告写成 JSON 文件")
    args = parser.parse_args()
    report = asyncio.run(run())
    if args.output:
        with open(args.output, "w", encoding="utf-8") as handle:
            json.dump(report, handle, ensure_ascii=False, indent=2)
    for item in report["checks"]:
        if not item["ok"]:
            print(
                f"FAILED {item['check']}: expected {item['expected']!r}, got {item['actual']!r}",
                file=sys.stderr,
            )
    print(
        json.dumps(
            {"status": report["status"], "totals": report["totals"], "failedChecks": report["failedChecks"]},
            ensure_ascii=False,
        )
    )
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
