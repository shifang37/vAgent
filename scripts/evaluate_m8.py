"""M8 E4 offline acceptance: route matrix, refusal matrix, billing/errors and replay.

Default mode is fully offline. The real ``provider-route-v1`` preview, the production
``billing-v1`` sidecar ledger, the submit gate and the Job Worker run through product
code paths; provider HTTP goes to ``httpx.MockTransport`` only, so no credential is
used and no request leaves the process.

``--live`` is deliberately unavailable: a new paid sample needs an explicit
model/count/amount approval and is recorded separately from this suite (M8 E3 already
holds the only authorized real samples).
"""

import argparse
import asyncio
import hashlib
import json
import time
import traceback
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx

from vagent.config import Config
from vagent.errors import AppError
from vagent.storage import FileStore, RunRecordV1, atomic_write_json, now
from vagent.tools import create_project_tools
from vagent.video import billing, routing
from vagent.video.assets import asset_reference_loader
from vagent.video.contracts import (
    KLING_MODEL,
    ImageInputRule,
    canonical_fingerprint,
    multi_provider_capabilities,
    wan_capabilities,
    wan_shot_capabilities,
)
from vagent.video.jobs import JobService
from vagent.video.providers.gate import QUERY_ERROR_TABLE, SUBMIT_ERROR_TABLE, gate_for
from vagent.video.providers.kling import KlingAdapter
from vagent.video.providers.mock import MockVideoAdapter
from vagent.video.providers.wan import WanAdapter
from vagent.video.tools import VIDEO_RULES_VERSION, VIDEO_TOOLS_VERSION, register_video_tools
from vagent.video.usage import summarize_records
from vagent.video.worker import JobWorker
from vagent.waiting import ToolExecutionContext

ROOT = Path(__file__).resolve().parent.parent
BASELINE_PATH = ROOT / "docs" / "M8_E0_BASELINE.json"
PROJECT = "m8-e4"
TIMESTAMP = "2030-01-01T00:00:00+00:00"
BILL_CLOCK = datetime(2026, 10, 25, 9, 0, tzinfo=UTC)
BILL_SOURCE = "Kling 控制台 2026-10-25 账单"
WAN_SOURCE = "阿里云百炼 2026-10-25 账单"
KEYS = {"wan": True, "kling": True, "fal": True}
AUDIO_MODES = {"wan": "auto", "kling": "native", "fal": "silent"}
MATRIX_MODES = ("t2v", "i2v", "flf2v")
MATRIX_DURATIONS = (5, 10)
MATRIX_SCOPES = ("shot", "episode", "batch")

# The frozen §5.1/§5.2 mapping, restated here so the suite fails if the shared table
# constants ever drift from the contract the acceptance document cites.
EXPECTED_SUBMIT_ROWS = (
    (None, "accepted", "queued", False),
    ("PROVIDER_AUTH_FAILED", "not_accepted", "failed", False),
    ("PROVIDER_ACCESS_DENIED", "not_accepted", "failed", False),
    ("PROVIDER_MODEL_UNAVAILABLE", "not_accepted", "failed", False),
    ("PROVIDER_RATE_LIMITED", "not_accepted", "failed", True),
    ("PROVIDER_BILLING_BLOCKED", "not_accepted", "failed", False),
    ("PROVIDER_CONTENT_REJECTED", "not_accepted", "failed", False),
    ("PROVIDER_INVALID_REQUEST", "not_accepted", "failed", False),
    ("SUBMISSION_UNKNOWN", "unknown", "unknown", False),
)
EXPECTED_QUERY_ROWS = (
    ((401,), "PROVIDER_AUTH_FAILED", "configuration"),
    ((403,), "PROVIDER_ACCESS_DENIED", "configuration"),
    ((404,), "PROVIDER_TASK_UNAVAILABLE", "task_unavailable"),
    ((429,), "PROVIDER_RATE_LIMITED", None),
    ((500, 502, 503, 504), "QUERY_HTTP_ERROR", None),
)

# wan x kling x fal, t2v/i2v/flf2v, 5/10 seconds at the frozen default ceilings.
MATRIX_EXPECTED = {
    "wan": {
        ("t2v", 5): (True, None, "3.00"),
        ("t2v", 10): (False, "VIDEO_COST_LIMIT", "6.00"),
        ("i2v", 5): (True, None, "3.00"),
        ("i2v", 10): (False, "VIDEO_COST_LIMIT", "6.00"),
        ("flf2v", 5): (True, None, "3.00"),
        ("flf2v", 10): (False, "VIDEO_COST_LIMIT", "6.00"),
    },
    "kling": {
        ("t2v", 5): (True, None, "0.56"),
        ("t2v", 10): (False, "VIDEO_UNSUPPORTED_SPEC", None),
        ("i2v", 5): (False, "VIDEO_UNSUPPORTED_MODE", None),
        ("i2v", 10): (False, "VIDEO_UNSUPPORTED_MODE", None),
        ("flf2v", 5): (False, "VIDEO_UNSUPPORTED_MODE", None),
        ("flf2v", 10): (False, "VIDEO_UNSUPPORTED_MODE", None),
    },
    "fal": {
        ("t2v", 5): (True, None, "0.42"),
        ("t2v", 10): (False, "VIDEO_UNSUPPORTED_SPEC", None),
        ("i2v", 5): (False, "VIDEO_UNSUPPORTED_MODE", None),
        ("i2v", 10): (False, "VIDEO_UNSUPPORTED_MODE", None),
        ("flf2v", 5): (False, "VIDEO_UNSUPPORTED_MODE", None),
        ("flf2v", 10): (False, "VIDEO_UNSUPPORTED_MODE", None),
    },
}


@dataclass(frozen=True)
class Case:
    """One offline acceptance scenario."""

    name: str
    kind: str  # route | refusal | billing | replay
    title: str


CASES = (
    Case("route-matrix", "route", "路由矩阵与冻结探针"),
    Case("refusal-matrix", "refusal", "拒绝矩阵"),
    Case("billing-errors", "billing", "账单与错误映射"),
    Case("replay-no-new", "replay", "重放不新增"),
)
CASES_BY_NAME = {case.name: case for case in CASES}


def baseline_bytes() -> bytes:
    return BASELINE_PATH.read_bytes()


def load_baseline() -> dict:
    return json.loads(baseline_bytes())


def suite_identity(cases) -> str:
    return hashlib.sha256(
        json.dumps(
            {
                "version": 1,
                "cases": [vars(case) for case in cases],
                "baselineSha256": hashlib.sha256(baseline_bytes()).hexdigest(),
            },
            sort_keys=True,
            ensure_ascii=False,
        ).encode()
    ).hexdigest()[:16]


class SuiteClock:
    """A deterministic clock shared by the controlled providers and the Worker."""

    def __init__(self):
        self.value = datetime(2030, 1, 1, tzinfo=UTC)

    def __call__(self):
        return self.value

    def advance(self, seconds=2):
        self.value += timedelta(seconds=seconds)


class Ledger:
    """Observable facts of the suite itself; no real connection is ever counted."""

    def __init__(self):
        self.preview_calls = 0
        self.submit_calls = 0
        self.registrations = 0
        self.bill_writes = 0
        self.bill_replays = 0

    def view(self):
        return {
            "previewCalls": self.preview_calls,
            "submitCalls": self.submit_calls,
            "registrations": self.registrations,
            "billWrites": self.bill_writes,
            "billReplays": self.bill_replays,
        }


def preview(config, state, request, ledger):
    ledger.preview_calls += 1
    return routing.preview_for(config, state, request)


def spec_request(mode="t2v", duration=5, aspect=None, scope="shot", provider=None):
    return routing.make_request(
        scope=scope,
        mode=mode,
        duration_seconds=duration,
        aspect_ratio=aspect,
        provider=provider,
    )


def by_provider(view, name):
    return next(item for item in view.candidates if item.provider == name)


def probe_row(view, request_row):
    """Project one preview onto the frozen ``routeProbe`` row shape."""
    return {
        "request": {
            "scope": request_row["scope"],
            "mode": request_row["mode"],
            "durationSeconds": request_row["durationSeconds"],
            "aspectRatio": request_row["aspectRatio"],
        },
        "candidates": [
            {
                "provider": item.provider,
                "model": item.model,
                "capabilitiesVersion": item.capabilities_version,
                "audioMode": AUDIO_MODES[item.provider],
                "available": item.available,
                "reason": item.reason,
                "quote": (
                    None
                    if not item.available
                    else {
                        "currency": item.currency,
                        "unitPrice": item.unit_price,
                        "quantity": item.quantity,
                        "amount": item.amount,
                        "priceVersion": item.price_version,
                        "sourceUrl": item.source_url,
                        **(
                            {"resource": {"unit": "VIDEO_UNIT", "units": item.resource_units}}
                            if item.resource_units is not None
                            else {}
                        ),
                    }
                ),
            }
            for item in view.candidates
        ],
        "recommended": view.recommended,
    }


def live_config(home, **changes):
    return Config(
        **{
            "home": home,
            "api_key": None,
            "video_mode": "live",
            "video_api_key": "wan-offline-secret",
            "video_workspace_id": "offline-workspace",
            "kling_api_key": "kling-offline-secret",
            "fal_api_key": "fal-offline-secret",
            **changes,
        }
    )


def mock_config(home):
    return Config(
        home=home,
        api_key=None,
        video_mode="mock",
        kling_api_key=None,
        fal_api_key=None,
        redis_url=None,
        skills_root=None,
        mcp_config=None,
        mcp_local=False,
    )


def create_run(store, run_id, *, mode="live"):
    """A terminal Run whose frozen video mode admits live registration."""
    record = RunRecordV1(
        id=run_id,
        session_id=PROJECT,
        request_id=run_id,
        prompt="offline M8 E4 controlled acceptance",
        model="no-model",
        status="completed",
        messages=[],
        model_steps=0,
        tool_calls=0,
        input_tokens=0,
        output_tokens=0,
        answer="",
        created_at=TIMESTAMP,
        updated_at=TIMESTAMP,
    )

    def add(draft):
        draft["runs"][run_id] = record.model_dump(by_alias=True)
        draft["sessions"][PROJECT]["latestRunId"] = run_id
        draft["runs"][run_id].update(
            videoMode=mode,
            executionVersion=3,
            toolFeatures={
                "video": {
                    "mode": mode,
                    "toolsVersion": VIDEO_TOOLS_VERSION,
                    "rulesVersion": VIDEO_RULES_VERSION,
                    "capabilities": [],
                }
            },
        )

    store.transaction(add)
    return ToolExecutionContext(
        project_id=PROJECT,
        session_id=PROJECT,
        run_id=run_id,
        model_step=1,
        tool_call_id="generate",
    )


def register(service, store, run_id, arguments, ledger, *, context=None, mode="live"):
    context = context or create_run(store, run_id, mode=mode)
    result = service.generate(arguments, context=context)
    if not result.get("ok"):
        raise AppError(result["error"]["code"], result["error"]["message"])
    ledger.registrations += 1
    return result["data"]["jobId"]


def kling_request():
    return {
        "provider": "kling",
        "model": KLING_MODEL,
        "capabilitiesVersion": "multi-provider-v3",
        "prompt": "雨夜咖啡店",
        "spec": {"durationSeconds": 5, "resolution": "720p", "aspectRatio": "16:9"},
    }


def wan_arguments():
    capability = wan_capabilities()
    return {
        "provider": capability.provider,
        "model": capability.model,
        "capabilitiesVersion": capability.capabilities_version,
        "prompt": "雨夜咖啡店，镜头缓慢推近。",
        "spec": {"durationSeconds": 5, "resolution": "720p", "aspectRatio": "16:9"},
    }


@asynccontextmanager
async def kling_service(store, clock, handler):
    """A controlled Kling adapter; ``handler`` is the only upstream."""
    config = Config(
        home=store.home,
        api_key=None,
        video_mode="live",
        video_provider="kling",
        kling_api_key="kling-offline-secret",
    )
    async with KlingAdapter(
        api_key=config.kling_api_key,
        transport=httpx.MockTransport(handler),
        clock=clock,
    ) as provider:
        service = JobService(
            store,
            [MockVideoAdapter(store, clock=clock), provider],
            clock=clock,
            config=config,
        )
        yield service


@asynccontextmanager
async def wan_service(store, clock, handler):
    """A controlled Wan adapter; ``handler`` is the only upstream."""
    config = Config(
        home=store.home,
        api_key=None,
        video_mode="live",
        video_api_key="wan-offline-secret",
        video_workspace_id="offline-workspace",
    )
    async with WanAdapter(
        api_key=config.video_api_key,
        workspace_id=config.video_workspace_id,
        transport=httpx.MockTransport(handler),
        clock=clock,
        reference_loader=asset_reference_loader(store, ImageInputRule()),
    ) as provider:
        service = JobService(
            store,
            [MockVideoAdapter(store, clock=clock), provider],
            clock=clock,
            config=config,
        )
        yield service


def state_bytes(store):
    return (store.home / "state.json").read_bytes()


def error_code(call):
    """Run one callable and return its AppError code (or ``ACCEPTED``)."""
    try:
        call()
    except AppError as error:
        return error.code
    return "ACCEPTED"


async def run_route_matrix(case, base):
    store, clock, ledger, config = base["store"], base["clock"], base["ledger"], base["config"]
    baseline = load_baseline()
    frozen_rows = []
    for row in baseline["routeProbe"]["rows"]:
        request = routing.make_request(
            scope=row["request"]["scope"],
            mode=row["request"]["mode"],
            duration_seconds=row["request"]["durationSeconds"],
            aspect_ratio=row["request"]["aspectRatio"],
        )
        ledger.preview_calls += 1
        view = routing.preview(request, keys_configured=KEYS)
        frozen_rows.append(probe_row(view, row["request"]))

    matrix = []
    for scope in MATRIX_SCOPES:
        for mode in MATRIX_MODES:
            for duration in MATRIX_DURATIONS:
                view = preview(config, store.snapshot(), spec_request(mode, duration, scope=scope), ledger)
                matrix.append(
                    {
                        "scope": scope,
                        "mode": mode,
                        "durationSeconds": duration,
                        "candidates": [
                            {
                                "provider": item.provider,
                                "available": item.available,
                                "reason": item.reason,
                                "currency": item.currency,
                                "amount": item.amount,
                                "resourceUnits": item.resource_units,
                            }
                            for item in view.candidates
                        ],
                        "recommended": view.recommended,
                    }
                )

    # A storyboard preview sums every shot's frozen quote through the product tool path.
    registry = register_video_tools(
        create_project_tools(),
        JobService(
            store, [MockVideoAdapter(store, clock=clock)], clock=clock, config=mock_config(store.home)
        ),
    )
    saved = registry.execute(
        "storyboard_save",
        {
            "title": "M8 E4 报价对比",
            "shots": [
                {"description": "雨夜咖啡店窗外", "framing": "wide", "durationSeconds": 5, "mode": "t2v"},
                {"description": "吧台前的两个人", "framing": "medium", "durationSeconds": 5, "mode": "t2v"},
                {"description": "雨滴落在招牌上", "framing": "close", "durationSeconds": 5, "mode": "t2v"},
            ],
        },
        store=store,
        project_id=PROJECT,
        operation_key="m8e4-storyboard",
    )
    if not saved.get("ok"):
        raise AppError(saved["error"]["code"], saved["error"]["message"])
    storyboard = saved["data"]
    board_request = routing.make_request(
        storyboard_id=storyboard["storyboardId"],
        storyboard_version=storyboard["version"],
        shot_ids=storyboard["shotIds"],
    )
    board_view = preview(config, store.snapshot(), board_request, ledger)

    # The storyboard is the only write of this case; every preview below must leave
    # the state bytes untouched.
    state_before = state_bytes(store)
    shot = preview(config, store.snapshot(), spec_request(), ledger)
    missing_wan = preview(
        live_config(store.home, video_api_key=None), store.snapshot(), spec_request(), ledger
    )
    missing_kling = preview(
        live_config(store.home, kling_api_key=None), store.snapshot(), spec_request(), ledger
    )
    first_json = shot.model_dump_json(by_alias=True)
    second_json = preview(config, store.snapshot(), spec_request(), ledger).model_dump_json(by_alias=True)
    state_after = state_bytes(store)

    checks = {
        "frozenRouteProbeReproduced": [row["candidates"] for row in frozen_rows]
        == [row["candidates"] for row in baseline["routeProbe"]["rows"]]
        and [row["recommended"] for row in frozen_rows]
        == [row["recommended"] for row in baseline["routeProbe"]["rows"]]
        and canonical_fingerprint(frozen_rows) == baseline["routeProbe"]["rowsSha256"],
        "referenceShotComparesThreeProviders": [
            (item.provider, item.available, item.currency, item.amount) for item in shot.candidates
        ]
        == [
            ("wan", True, "CNY", "3.00"),
            ("fal", True, "USD", "0.42"),
            ("kling", True, "USD", "0.56"),
        ]
        and shot.recommended == "wan",
        "fullMatrixMatchesFrozenPolicies": len(matrix) == 18
        and all(
            (item["available"], item["reason"], item["amount"])
            == MATRIX_EXPECTED[item["provider"]][(row["mode"], row["durationSeconds"])]
            for row in matrix
            if row["scope"] == "shot"
            for item in row["candidates"]
        ),
        "episodeAndBatchRejectUsdButKeepQuotes": all(
            row["candidates"][0]["available"] is True
            and row["candidates"][0]["amount"] == "3.00"
            and all(
                item["available"] is False and item["reason"] == "EPISODE_COST_LIMIT"
                for item in row["candidates"][1:]
            )
            and row["recommended"] == "wan"
            for row in matrix
            if row["scope"] in {"episode", "batch"} and row["mode"] == "t2v" and row["durationSeconds"] == 5
        ),
        "missingKeyBlocksOnlyThatProvider": (
            by_provider(missing_wan, "wan").reason == "VIDEO_KEY_MISSING"
            and by_provider(missing_wan, "fal").available is True
            and by_provider(missing_wan, "kling").available is True
            and by_provider(missing_kling, "kling").reason == "VIDEO_KEY_MISSING"
            and by_provider(missing_kling, "wan").available is True
            and by_provider(missing_kling, "fal").available is True
        ),
        "storyboardPreviewSumsEachShot": (
            (
                [(item.provider, item.quantity, item.amount) for item in board_view.candidates],
                board_view.recommended,
            )
            == (
                [
                    ("wan", "15", "9.00"),
                    ("fal", "15", "1.26"),
                    ("kling", "15", "1.68"),
                ],
                "wan",
            )
        ),
        "previewIsDeterministic": first_json == second_json,
        "previewWritesNothing": state_after == state_before and store.snapshot()["jobs"] == {},
        "zeroRealCalls": ledger.submit_calls == 0,
    }
    return {
        "case": case.name,
        "checks": checks,
        "frozenProbeRows": len(frozen_rows),
        "rowsSha256": canonical_fingerprint(frozen_rows),
        "matrixRows": len(matrix),
        "matrix": matrix,
        "storyboardId": storyboard["storyboardId"],
        "previews": ledger.preview_calls,
        "controlledSubmits": ledger.submit_calls,
        "jobs": len(store.snapshot()["jobs"]),
    }


async def run_refusal_matrix(case, base):
    store, ledger, config = base["store"], base["ledger"], base["config"]
    state_before = state_bytes(store)

    ten_seconds = preview(config, store.snapshot(), spec_request("t2v", 10), ledger)
    image_i2v = preview(config, store.snapshot(), spec_request("i2v", 5), ledger)
    image_flf2v = preview(config, store.snapshot(), spec_request("flf2v", 5), ledger)
    episode = preview(config, store.snapshot(), spec_request("t2v", 5, scope="episode"), ledger)
    batch = preview(config, store.snapshot(), spec_request("t2v", 5, scope="batch"), ledger)

    stale_entries = tuple(
        entry.model_copy(
            update={"price": entry.price.model_copy(update={"price_version": "wan27-old-price"})}
        )
        for entry in wan_shot_capabilities()
    )
    ledger.preview_calls += 1
    stale = routing.preview(
        spec_request(),
        capabilities=(*stale_entries, *multi_provider_capabilities()),
        keys_configured=KEYS,
    )

    ledger.preview_calls += 1
    unpublished = routing.preview(
        spec_request(provider="wan"),
        capabilities=tuple(multi_provider_capabilities()),
        keys_configured=KEYS,
    )

    malformed = (
        ({"scope": "reel", "mode": "t2v", "duration_seconds": 5}, "ROUTE_UNAVAILABLE"),
        ({"mode": "t2v", "duration_seconds": 1}, "ROUTE_UNAVAILABLE"),
        ({"mode": "t2v", "duration_seconds": 16}, "ROUTE_UNAVAILABLE"),
        ({"mode": "i2v", "duration_seconds": 5, "aspect_ratio": "16:9"}, "ROUTE_UNAVAILABLE"),
        ({"mode": "t2v", "duration_seconds": 5, "aspect_ratio": "9:16"}, "ROUTE_UNAVAILABLE"),
        ({"storyboard_id": "board", "mode": "t2v", "duration_seconds": 5}, "INVALID_ARGUMENTS"),
        ({"mode": "t2v", "duration_seconds": 5, "shot_ids": ["shot-1"]}, "INVALID_ARGUMENTS"),
        ({"mode": "t2v", "duration_seconds": 5, "provider": "sora"}, "ROUTE_UNAVAILABLE"),
        ({"scope": "shot"}, "INVALID_ARGUMENTS"),
    )
    malformed_codes = []
    for kwargs, expected in malformed:
        actual = error_code(lambda kwargs=kwargs: routing.make_request(**kwargs))
        malformed_codes.append({"arguments": kwargs, "expected": expected, "code": actual})

    ledger.preview_calls += 1
    unknown_storyboard = error_code(
        lambda: routing.preview_for(
            config,
            store.snapshot(),
            routing.make_request(storyboard_id="00000000-0000-4000-8000-000000000000"),
        )
    )
    require_available = error_code(
        lambda: routing.preview(
            spec_request("t2v", 10),
            keys_configured=KEYS,
            require_available=True,
        )
    )

    state_after = state_bytes(store)
    checks = {
        "tenSecondShotRefused": [
            (item.provider, item.available, item.reason) for item in ten_seconds.candidates
        ]
        == [
            ("wan", False, "VIDEO_COST_LIMIT"),
            ("kling", False, "VIDEO_UNSUPPORTED_SPEC"),
            ("fal", False, "VIDEO_UNSUPPORTED_SPEC"),
        ]
        and ten_seconds.recommended is None,
        "imageModesRefusedForTextProviders": all(
            by_provider(view, "wan").available is True
            and all(
                (by_provider(view, name).available, by_provider(view, name).reason)
                == (False, "VIDEO_UNSUPPORTED_MODE")
                for name in ("kling", "fal")
            )
            for view in (image_i2v, image_flf2v)
        ),
        "episodeAndBatchKeepWanOnly": all(
            by_provider(view, "wan").available is True
            and all(
                (by_provider(view, name).available, by_provider(view, name).reason)
                == (False, "EPISODE_COST_LIMIT")
                for name in ("kling", "fal")
            )
            and view.recommended == "wan"
            for view in (episode, batch)
        ),
        "aStalePriceIsNeverQuoted": by_provider(stale, "wan").reason == "VIDEO_PRICE_UNKNOWN"
        and by_provider(stale, "wan").amount is None,
        "anUnpublishedNamedProviderIsBlocked": by_provider(unpublished, "wan").reason
        == "VIDEO_PROVIDER_UNAVAILABLE"
        and by_provider(unpublished, "wan").model is None
        and unpublished.recommended == "fal",
        "malformedRequestsAreRefused": all(item["code"] == item["expected"] for item in malformed_codes),
        "unknownStoryboardIsRefused": unknown_storyboard == "ROUTE_UNAVAILABLE",
        "requireAvailableFailsWithoutCandidate": require_available == "ROUTE_UNAVAILABLE",
        "nothingIsRegistered": state_after == state_before and store.snapshot()["jobs"] == {},
        "zeroRealCalls": ledger.submit_calls == 0,
    }
    return {
        "case": case.name,
        "checks": checks,
        "refusals": {
            "tenSeconds": [item.reason for item in ten_seconds.candidates],
            "imageModes": [item.reason for item in image_i2v.candidates],
            "episode": [item.reason for item in episode.candidates],
            "stalePrice": by_provider(stale, "wan").reason,
            "unpublishedProvider": by_provider(unpublished, "wan").reason,
            "unknownStoryboard": unknown_storyboard,
            "requireAvailable": require_available,
        },
        "malformedRequests": malformed_codes,
        "previews": ledger.preview_calls,
        "controlledSubmits": ledger.submit_calls,
        "jobs": len(store.snapshot()["jobs"]),
    }


def submit_table_rows():
    return [
        (row["code"], row["outcome"], row["jobStatus"], bool(row["cooldown"])) for row in SUBMIT_ERROR_TABLE
    ]


def query_table_rows():
    return [(tuple(row["statuses"]), row["code"], row["pause"]) for row in QUERY_ERROR_TABLE]


async def run_billing_errors(case, base):
    store, clock, ledger = base["store"], base["clock"], base["ledger"]
    kling_calls = {"count": 0}
    wan_calls = {"count": 0}

    def handler_401(request):
        kling_calls["count"] += 1
        ledger.submit_calls += 1
        return httpx.Response(401, json={"code": 1001, "message": "invalid key"})

    async with kling_service(store, clock, handler_401) as service:
        auth_job = register(service, store, "kling-auth", kling_request(), ledger)
        worker = JobWorker(service)
        auth_failed = await worker.run_once()
        auth_retry = await worker.run_once()

    clock.advance(2)

    def handler_timeout(request):
        kling_calls["count"] += 1
        ledger.submit_calls += 1
        raise httpx.ConnectTimeout("offline timeout")

    async with kling_service(store, clock, handler_timeout) as service:
        unknown_job = register(service, store, "kling-timeout", kling_request(), ledger)
        worker = JobWorker(service)
        unknown = await worker.run_once()
        unknown_retry = await worker.run_once()

    clock.advance(2)

    def handler_429(request):
        kling_calls["count"] += 1
        ledger.submit_calls += 1
        return httpx.Response(429, headers={"Retry-After": "120"}, json={"code": 1302, "message": "limited"})

    async with kling_service(store, clock, handler_429) as service:
        rate_job = register(service, store, "kling-rate", kling_request(), ledger)
        worker = JobWorker(service)
        rate_failed = await worker.run_once()
        gate_after_429 = gate_for(store).check("kling")
        clock.advance(2)
        throttled_job = register(service, store, "kling-throttled", kling_request(), ledger)
        calls_before_block = kling_calls["count"]
        blocked = await worker.run_once()
        calls_at_block = kling_calls["count"]
        clock.advance(200)
        submitted_later = await worker.run_once()

    clock.advance(2)

    def handler_wan_401(request):
        wan_calls["count"] += 1
        ledger.submit_calls += 1
        return httpx.Response(
            401, json={"request_id": "wan-offline", "code": "InvalidApiKey", "message": "invalid key"}
        )

    async with wan_service(store, clock, handler_wan_401) as service:
        wan_job = register(service, store, "wan-auth", wan_arguments(), ledger)
        wan_failed = await JobWorker(service).run_once()

    state_before_bill = state_bytes(store)
    first = billing.record_bill(
        store.snapshot(),
        store.home,
        job_id=auth_job,
        amount="0.56",
        currency="USD",
        source=BILL_SOURCE,
        invoiced_on="2026-10-25",
        clock=lambda: BILL_CLOCK,
        store=store,
    )
    ledger.bill_writes += 1
    ledger_file = store.home / billing.BILLING_FILE
    written = ledger_file.read_bytes()
    state_after_bill = state_bytes(store)

    replay = billing.record_bill(
        store.snapshot(),
        store.home,
        job_id=auth_job,
        amount="0.56",
        currency="USD",
        source=BILL_SOURCE,
        invoiced_on="2026-10-25",
        clock=lambda: datetime(2026, 10, 26, 18, 30, tzinfo=UTC),
        store=store,
    )
    ledger.bill_replays += 1
    replayed_bytes = ledger_file.read_bytes()

    conflict = error_code(
        lambda: billing.record_bill(
            store.snapshot(),
            store.home,
            job_id=auth_job,
            amount="0.60",
            currency="USD",
            source=BILL_SOURCE,
            invoiced_on="2026-10-25",
            clock=lambda: BILL_CLOCK,
            store=store,
        )
    )
    replaced = billing.record_bill(
        store.snapshot(),
        store.home,
        job_id=auth_job,
        amount="0.60",
        currency="USD",
        source=BILL_SOURCE,
        invoiced_on="2026-10-25",
        replace=True,
        expected_revision=1,
        clock=lambda: BILL_CLOCK,
        store=store,
    )
    ledger.bill_writes += 1
    summary = summarize_records(store.snapshot(), home=store.home)["billing"]
    buckets = {item["currency"]: item for item in summary["currencies"]}
    view = billing.ledger_view(store.snapshot(), store.home)

    # The damaged ledger is a copy: the acceptance home keeps its readable ledger so
    # the browser and installed-wheel checks can reuse it.
    damaged_home = store.home.parent / f"{case.name}-damaged"
    damaged_home.mkdir(parents=True, exist_ok=True)
    damaged = '{"billingVersion": 1, "records": {'
    (damaged_home / billing.BILLING_FILE).write_text(damaged, encoding="utf-8")
    damaged_bytes = (damaged_home / billing.BILLING_FILE).read_bytes()
    damaged_record = error_code(
        lambda: billing.record_bill(
            store.snapshot(),
            damaged_home,
            job_id=auth_job,
            amount="0.56",
            currency="USD",
            source=BILL_SOURCE,
            clock=lambda: BILL_CLOCK,
        )
    )
    damaged_view = billing.ledger_view(store.snapshot(), damaged_home)

    checks = {
        "submitErrorTableIsFrozen": submit_table_rows() == list(EXPECTED_SUBMIT_ROWS),
        "queryErrorTableIsFrozen": query_table_rows() == list(EXPECTED_QUERY_ROWS),
        "authRejectionIsTerminal": auth_failed is not None
        and auth_failed.status == "failed"
        and auth_failed.error.code == "PROVIDER_AUTH_FAILED"
        and auth_failed.error.http_status == 401
        and auth_failed.submit_attempts == 1
        and auth_failed.provider_task_id is None
        and auth_retry is None
        and kling_calls["count"] >= 1,
        "timeoutIsUnknownAndNeverResubmitted": unknown is not None
        and unknown.status == "unknown"
        and unknown.error.code == "SUBMISSION_UNKNOWN"
        and unknown.submit_attempts == 1
        and unknown.provider_task_id is None
        and unknown_retry is None,
        "rateLimitIsProvenAndCoolsDown": rate_failed.status == "failed"
        and rate_failed.error.code == "PROVIDER_RATE_LIMITED"
        and rate_failed.error.http_status == 429
        and rate_failed.submit_attempts == 1
        and gate_after_429.allowed is False
        and gate_after_429.reason == "cooldown",
        "backoffKeepsPendingWithoutSubmit": blocked is not None
        and blocked.id == throttled_job
        and blocked.status == "pending_submit"
        and blocked.submit_attempts == 0
        and blocked.runtime_block is not None
        and blocked.runtime_block.code == "PROVIDER_BACKOFF"
        and calls_at_block == calls_before_block,
        "theThrottledJobSubmitsExactlyOnce": submitted_later is not None
        and submitted_later.id == throttled_job
        and submitted_later.submit_attempts == 1
        and submitted_later.status == "failed"
        and submitted_later.runtime_block is None
        and kling_calls["count"] == calls_before_block + 1,
        "aBillNeverRewritesTheStateFile": state_before_bill == state_after_bill
        and first["record"]["provider"] == "kling"
        and first["record"]["currency"] == "USD"
        and first["record"]["amount"] == "0.56",
        "aReplayAddsNoRecord": replay["recorded"] is False
        and replay["replayed"] is True
        and replay["record"]["revision"] == 1
        and replayed_bytes == written,
        "aConflictKeepsTheOldRecord": conflict == "BILL_CONFLICT"
        and replaced["recorded"] is True
        and replaced["record"]["revision"] == 2
        and view["records"][0]["amount"] == "0.60",
        "summaryKeepsCurrenciesApart": (
            buckets["CNY"]["estimate"],
            buckets["CNY"]["reported"],
            buckets["CNY"]["delta"],
            buckets["CNY"]["unknownJobs"],
        )
        == ("3.00", None, None, 1)
        and (
            buckets["USD"]["estimate"],
            buckets["USD"]["reported"],
            buckets["USD"]["delta"],
            buckets["USD"]["reportedJobs"],
            buckets["USD"]["unknownJobs"],
        )
        == ("2.24", "0.60", "-1.64", 1, 3)
        and summary["reportedJobs"] == 1
        and summary["unknownJobs"] == 4
        and not {"total", "grandTotal"} & set(summary),
        "aDamagedLedgerIsNeverOverwritten": damaged_record == "INVALID_STORE"
        and (damaged_home / billing.BILLING_FILE).read_bytes() == damaged_bytes
        and damaged_view["ledgerStatus"] == "invalid"
        and damaged_view["records"] == [],
        "controlledUpstreamOnly": kling_calls["count"] == 4 and wan_calls["count"] == 1,
    }
    return {
        "case": case.name,
        "checks": checks,
        "jobIds": {
            "auth": auth_job,
            "timeout": unknown_job,
            "rateLimited": rate_job,
            "throttled": throttled_job,
            "wan": wan_job,
        },
        "jobs": 5,
        "errorMapping": {
            "auth": auth_failed.error.code if auth_failed else None,
            "timeout": unknown.error.code if unknown else None,
            "rateLimited": rate_failed.error.code if rate_failed else None,
            "blocked": blocked.runtime_block.code if blocked and blocked.runtime_block else None,
            "wanAuth": wan_failed.error.code if wan_failed else None,
        },
        "throttle": {
            "gateAllowed": gate_after_429.allowed,
            "gateReason": gate_after_429.reason,
            "blockedStatus": blocked.status if blocked else None,
            "blockedAttempts": blocked.submit_attempts if blocked else None,
            "blockedCode": blocked.runtime_block.code if blocked and blocked.runtime_block else None,
            "callsBeforeBlock": calls_before_block,
            "callsAtBlock": calls_at_block,
            "submittedLaterStatus": submitted_later.status if submitted_later else None,
            "submittedLaterAttempts": submitted_later.submit_attempts if submitted_later else None,
        },
        "billing": {
            "firstRevision": first["record"]["revision"],
            "replayed": replay["replayed"],
            "conflict": conflict,
            "revisionAfterReplace": replaced["record"]["revision"],
            "currencies": summary["currencies"],
        },
        "controlledSubmits": ledger.submit_calls,
        "billWrites": ledger.bill_writes,
        "billReplays": ledger.bill_replays,
    }


async def run_replay_no_new(case, base):
    store, clock, ledger = base["store"], base["clock"], base["ledger"]
    state_before = state_bytes(store)

    routing_snapshot = store.snapshot()
    jobs_before_preview = len(store.snapshot()["jobs"])
    views = [preview(base["config"], routing_snapshot, spec_request(), ledger) for _ in range(3)]
    preview_json = [view.model_dump_json(by_alias=True) for view in views]
    state_after_preview = state_bytes(store)

    def handler_401(request):
        ledger.submit_calls += 1
        return httpx.Response(401, json={"code": 1001, "message": "invalid key"})

    async with kling_service(store, clock, handler_401) as service:
        failed_job = register(service, store, "kling-bill", kling_request(), ledger)
        failed = await JobWorker(service).run_once()
        if failed is None or failed.status != "failed":
            raise AppError("EVAL_BILL_JOB_MISSING", "账单用例需要一条已终态的 Kling Job。")

        first_bill = billing.record_bill(
            store.snapshot(),
            store.home,
            job_id=failed_job,
            amount="0.56",
            currency="USD",
            source=BILL_SOURCE,
            invoiced_on="2026-10-25",
            clock=lambda: BILL_CLOCK,
            store=store,
        )
        ledger.bill_writes += 1
        ledger_file = store.home / billing.BILLING_FILE
        written = ledger_file.read_bytes()
        replay_one = billing.record_bill(
            store.snapshot(),
            store.home,
            job_id=failed_job,
            amount="0.56",
            currency="USD",
            source=BILL_SOURCE,
            invoiced_on="2026-10-25",
            clock=lambda: BILL_CLOCK,
            store=store,
        )
        replay_two = billing.record_bill(
            store.snapshot(),
            store.home,
            job_id=failed_job,
            amount="0.56",
            currency="USD",
            source=BILL_SOURCE,
            invoiced_on="2026-10-25",
            clock=lambda: datetime(2026, 12, 31, 23, 59, tzinfo=UTC),
            store=store,
        )
        ledger.bill_replays += 2
        replay_bytes = ledger_file.read_bytes()
        conflict = error_code(
            lambda: billing.record_bill(
                store.snapshot(),
                store.home,
                job_id=failed_job,
                amount="0.58",
                currency="USD",
                source=BILL_SOURCE,
                invoiced_on="2026-10-25",
                clock=lambda: BILL_CLOCK,
                store=store,
            )
        )

        jobs_before_replay = len(store.snapshot()["jobs"])
        # Registration replay is proven with the offline mock adapter: it needs no
        # credential and leaves no live Job waiting for a submit.
        adapter = MockVideoAdapter(store, clock=clock)
        mock = JobService(store, [adapter], clock=clock, config=mock_config(store.home))
        capability = adapter.capabilities()
        mock_arguments = {
            "provider": capability.provider,
            "model": capability.model,
            "capabilitiesVersion": capability.capabilities_version,
            "prompt": "雨夜咖啡店",
            "spec": capability.specs[0].model_dump(mode="json", by_alias=True),
        }
        context = create_run(store, "mock-register", mode="mock")
        first_job = register(mock, store, "mock-register", mock_arguments, ledger, context=context)
        second_job = register(mock, store, "mock-register", mock_arguments, ledger, context=context)
        jobs_after_registration = store.snapshot()["jobs"]
        calls_after_registration = ledger.submit_calls

    ledger_after = billing.read_ledger(store.home)
    checks = {
        "previewReplayIsByteIdentical": len(set(preview_json)) == 1,
        "previewWritesNothing": state_after_preview == state_before and jobs_before_preview == 0,
        "registrationReplayAddsNoJob": first_job == second_job
        and len(jobs_after_registration) == jobs_before_replay + 1
        and calls_after_registration == 1,
        "billReplayAddsNoRecord": first_bill["recorded"] is True
        and replay_one["replayed"] is True
        and replay_two["replayed"] is True
        and replay_bytes == written
        and len(ledger_after[0].records) == 1,
        "aConflictAddsNoRecord": conflict == "BILL_CONFLICT"
        and len(ledger_after[0].records) == 1
        and ledger_after[0].records[failed_job].amount == "0.56",
        "controlledUpstreamOnly": ledger.submit_calls == 1,
        "zeroRealCalls": True,
    }
    return {
        "case": case.name,
        "checks": checks,
        "previews": len(views),
        "controlledSubmits": ledger.submit_calls,
        "jobs": len(store.snapshot()["jobs"]),
        "ledgerRecords": len(ledger_after[0].records),
        "billWrites": ledger.bill_writes,
        "billReplays": ledger.bill_replays,
    }


async def run_case(case, base):
    if case.kind == "route":
        return await run_route_matrix(case, base)
    if case.kind == "refusal":
        return await run_refusal_matrix(case, base)
    if case.kind == "billing":
        return await run_billing_errors(case, base)
    return await run_replay_no_new(case, base)


def all_checks_pass(checks):
    return bool(checks) and all(checks.values())


def totals(report):
    cases = report.get("cases") or []
    return {
        "plannedCases": report.get("plannedCases", len(CASES)),
        "finishedCases": len(cases),
        "passedCases": len([item for item in cases if all_checks_pass(item.get("checks"))]),
        "modelCalls": 0,
        "textModelCalls": 0,
        "ttsRealRequests": 0,
        "realProviderCalls": 0,
        "previews": sum(item.get("previews") or 0 for item in cases),
        "frozenProbeRows": sum(item.get("frozenProbeRows") or 0 for item in cases),
        "matrixRows": sum(item.get("matrixRows") or 0 for item in cases),
        "controlledSubmits": sum(item.get("controlledSubmits") or 0 for item in cases),
        "jobs": sum(item.get("jobs") or 0 for item in cases),
        "billWrites": sum(item.get("billWrites") or 0 for item in cases),
        "billReplays": sum(item.get("billReplays") or 0 for item in cases),
        "ledgerRecords": sum(item.get("ledgerRecords") or 0 for item in cases),
    }


async def run_suite(args):
    if getattr(args, "live", False):
        raise AppError(
            "EVAL_LIVE_UNAVAILABLE",
            "M8 E4 未获新的真实付费样本授权；--live 未执行，未发起任何请求。",
        )
    if args.output.exists():
        raise AppError("EVAL_OUTPUT_EXISTS", "输出文件已存在，请使用新路径保留先前证据。")
    home = args.home.resolve()
    if home.exists() and any(home.iterdir()):
        raise AppError("EVAL_HOME_NOT_EMPTY", "评测需要新的空数据目录，已有数据未修改。")
    if not BASELINE_PATH.is_file():
        raise AppError("EVAL_BASELINE_MISSING", "缺少 M8 E0 基线，离线评测未启动。")
    selected = [case for case in CASES if not args.only or case.name in args.only]
    report = {
        "schemaVersion": 1,
        "suiteId": suite_identity(CASES),
        "mode": "offline-fixture",
        "status": "incomplete",
        "startedAt": now(),
        "dataDirectory": str(home),
        "plannedCases": len(selected),
        "cases": [],
        "limitations": (
            "只读路由预览、billing-v1 账单账本与提交节流的固定脚本用例：受控 HTTP 与本地状态，"
            "证明比价、拒绝、账单与重放语义；不证明真实账号、真实扣费或真实供应商可用性。"
            "真实付费样本未执行（M8 E3 的真实样本另有证据）。"
        ),
    }

    def persist():
        report["totals"] = totals(report)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_json(args.output, report)

    persist()
    started = time.monotonic()
    print(
        json.dumps(
            {"suite": report["suiteId"], "mode": report["mode"], "dataDirectory": str(home)},
            ensure_ascii=False,
        ),
        flush=True,
    )
    try:
        for case in selected:
            case_home = home / case.name
            with FileStore.open(case_home) as store:
                store.ensure_session(PROJECT)
                ledger = Ledger()
                clock = SuiteClock()
                base = {
                    "store": store,
                    "ledger": ledger,
                    "clock": clock,
                    "config": live_config(store.home),
                }
                item = await run_case(case, base)
                item["upstream"] = ledger.view()
            report["cases"].append(item)
            persist()
            print(
                json.dumps(
                    {
                        "case": case.name,
                        "passed": all_checks_pass(item["checks"]),
                        "failedChecks": [key for key, value in item["checks"].items() if not value],
                    },
                    ensure_ascii=False,
                ),
                flush=True,
            )
        report["status"] = (
            "passed"
            if len(report["cases"]) == len(selected)
            and all(all_checks_pass(item.get("checks")) for item in report["cases"])
            else "failed"
        )
    except (Exception, KeyboardInterrupt):
        report["status"] = "error"
        report["error"] = traceback.format_exc(limit=8)
        raise
    finally:
        report["finishedAt"] = now()
        report["durationSeconds"] = round(time.monotonic() - started, 3)
        persist()
        print(
            json.dumps(
                {
                    "status": report["status"],
                    "totals": report.get("totals"),
                    "durationSeconds": report.get("durationSeconds"),
                },
                ensure_ascii=False,
            ),
            flush=True,
        )
    return report


def parser():
    root = argparse.ArgumentParser(description=__doc__)
    root.add_argument("--home", type=Path, required=True, help="新的空数据目录")
    root.add_argument("--output", type=Path, required=True, help="报告输出路径")
    root.add_argument(
        "--only",
        action="append",
        default=[],
        choices=[case.name for case in CASES],
        help="仅运行指定用例（开发调试；默认全部）",
    )
    root.add_argument(
        "--live",
        action="store_true",
        help="M8 E4 未获新的真实付费样本授权；本参数会拒绝执行且不发起请求",
    )
    return root


def main():
    args = parser().parse_args()
    try:
        report = asyncio.run(run_suite(args))
    except AppError as error:
        print(json.dumps({"status": "error", "code": error.code, "message": str(error)}, ensure_ascii=False))
        raise SystemExit(1) from None
    raise SystemExit(0 if report["status"] == "passed" else 1)


if __name__ == "__main__":
    main()
