"""Capture the M8 E0 baseline: post-M7 contracts, tools, capabilities, prices.

Run once from the repository root on the released v0.5.0 tree:

    .\\.venv\\Scripts\\python.exe scripts/capture_m8_baseline.py

The committed ``docs/M8_E0_BASELINE.json`` is golden evidence for M8, not an
output to regenerate when a later compatibility check fails. The script never
contacts a provider, never registers a Job, never calls a model and never reads a
credential: it hashes the frozen contract schemas, rebuilds both published tool
surfaces from the real registry (mock and live), records the capability and price
tables, and applies the frozen route ordering to the frozen quotes.

M8 is vendor-side only: it adds no schema, no tool and no storage collection, so
every hash below must stay byte-identical through E1-E4. E1's route preview must
also reproduce ``routeProbe`` exactly (same amounts, same order), because that
probe is the reference application of the frozen sort rule.
"""

from __future__ import annotations

import asyncio
import hashlib
import importlib
import json
import shutil
import sys
from decimal import Decimal
from pathlib import Path

from pydantic import BaseModel

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "docs" / "M8_E0_BASELINE.json"
FROZEN_RELEASE_VERSION = "0.5.0"
# The Store needs a writable home; keep it inside the repository's ignored tmp/.
SCRATCH = ROOT / "tmp" / "m8-e0-baseline-store"

CONTRACT_MODULES = (
    "vagent.storage",
    "vagent.video.contracts",
    "vagent.video.films",
    "vagent.video.episodes",
    "vagent.video.storyboards",
    "vagent.video.renders",
    "vagent.video.audio",
    "vagent.video.subtitles",
    "vagent.video.tts",
)

# The frozen M8 route ordering: display grouping only, never a currency conversion.
CURRENCY_ORDER = ("CNY", "USD")
PROVIDER_ORDER = ("wan", "kling", "fal")
# Default local per-Job ceilings (amount, currency, units); the effective value is
# min(stored, frozen), so these defaults are the widest a registration can accept.
JOB_CEILINGS = {"wan": ("3.00", "CNY", None), "kling": ("0.60", "USD", 4), "fal": ("0.50", "USD", None)}


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def canonical_sha256(value) -> str:
    canonical = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return sha256_bytes(canonical.encode("utf-8"))


def money(value) -> str:
    return format(Decimal(value), ".2f")


# --- contracts ----------------------------------------------------------------


def contract_schemas() -> dict[str, object]:
    """Hash every contract model defined by each module, not imported into it."""
    by_module: dict[str, dict[str, str]] = {}
    for module_name in CONTRACT_MODULES:
        module = importlib.import_module(module_name)
        models: dict[str, str] = {}
        for name, value in vars(module).items():
            if not isinstance(value, type) or not issubclass(value, BaseModel):
                continue
            if value.__module__ != module_name:
                continue
            schema = value.model_json_schema()
            models[name] = sha256_bytes(
                json.dumps(schema, sort_keys=True, separators=(",", ":")).encode("utf-8")
            )
        by_module[module_name] = dict(sorted(models.items()))
    return {
        "modelCount": sum(len(models) for models in by_module.values()),
        "byModule": {name: len(models) for name, models in by_module.items()},
        "schemaSha256": by_module,
        "contractsSha256": canonical_sha256(by_module),
    }


# --- tool surface -------------------------------------------------------------


def _tool_surface(registry) -> dict[str, object]:
    specs = registry.specs()
    features = registry.features
    video = features["video"]
    return {
        "toolsVersion": video["toolsVersion"],
        "rulesVersion": video["rulesVersion"],
        "toolCount": len(specs),
        "names": sorted(item["function"]["name"] for item in specs),
        "specsSha256": canonical_sha256(specs),
        "instructionsSha256": sha256_bytes(registry.instructions.encode("utf-8")),
        "instructionsCharacters": len(registry.instructions),
        "toolFeaturesSha256": canonical_sha256(features),
        "capabilityEntries": len(video["capabilities"]),
        "ttsEntries": len(video["tts"]),
    }


async def published_surface() -> dict[str, object]:
    """Rebuild the mock and live tool surfaces exactly as the application does."""
    from vagent.storage import FileStore
    from vagent.tools import create_project_tools
    from vagent.video import tools as video_tools
    from vagent.video.jobs import JobService
    from vagent.video.providers.fal import FalAdapter
    from vagent.video.providers.kling import KlingAdapter
    from vagent.video.providers.mock import MockVideoAdapter
    from vagent.video.providers.wan import WanAdapter
    from vagent.video.tools import register_video_tools

    scratch = SCRATCH
    shutil.rmtree(scratch, ignore_errors=True)
    # Create the home without the 0o700 mode FileStore would use: some sandboxes
    # cannot write through a freshly chmod-restricted directory.
    scratch.mkdir(parents=True, exist_ok=True)
    store = FileStore.open(scratch)
    adapters = [MockVideoAdapter(store), WanAdapter(), KlingAdapter(), FalAdapter()]
    try:
        service = JobService(store, adapters)
        core_names = {item["function"]["name"] for item in create_project_tools().specs()}
        groups = {
            "M2": list(video_tools.M2_TOOL_NAMES),
            "M3": list(video_tools.M3_TOOL_NAMES),
            "M5": list(video_tools.M5_TOOL_NAMES),
            "M5E2": list(video_tools.M5_E2_TOOL_NAMES),
            "M5E3": list(video_tools.M5_E3_TOOL_NAMES),
            "M6E1": list(video_tools.M6_E1_TOOL_NAMES),
            "M6E2": list(video_tools.M6_E2_TOOL_NAMES),
            "M6E3": list(video_tools.M6_E3_TOOL_NAMES),
            "M7E1": list(video_tools.M7_E1_TOOL_NAMES),
            "M7E2": list(video_tools.M7_E2_TOOL_NAMES),
            "M7E3": list(video_tools.M7_E3_TOOL_NAMES),
        }
        modes = {}
        for mode in ("mock", "live"):
            registry = register_video_tools(create_project_tools(), service, mode=mode)
            surface = _tool_surface(registry)
            surface["videoToolNames"] = sorted(set(surface["names"]) - core_names)
            modes[mode] = surface
        tables = {
            "live": [entry.model_dump(mode="json", by_alias=True) for entry in service.capabilities("live")],
            "mock": [entry.model_dump(mode="json", by_alias=True) for entry in service.capabilities("mock")],
        }
        return {
            "tools": {
                "videoToolsVersion": video_tools.VIDEO_TOOLS_VERSION,
                "videoRulesVersion": video_tools.VIDEO_RULES_VERSION,
                "liveVideoToolsVersion": video_tools.LIVE_VIDEO_TOOLS_VERSION,
                "liveVideoRulesVersion": video_tools.LIVE_VIDEO_RULES_VERSION,
                "groups": groups,
                "groupedVideoToolNames": sorted({name for names in groups.values() for name in names}),
                "services": {"coreToolCount": len(core_names), "coreToolNames": sorted(core_names)},
                "modes": modes,
            },
            "capabilities": {
                **tables,
                "entriesPerProvider": {
                    provider: sorted(
                        {entry["model"] for entry in tables["live"] if entry["provider"] == provider}
                    )
                    for provider in PROVIDER_ORDER
                },
                "capabilitiesSha256": canonical_sha256(tables),
            },
        }
    finally:
        for adapter in adapters:
            close = getattr(adapter, "aclose", None)
            if close is not None:
                await close()
        store.close()
        shutil.rmtree(scratch, ignore_errors=True)


# --- prices, limits and the route probe ---------------------------------------


def price_table() -> dict[str, object]:
    from vagent.video import contracts, tts

    return {
        "wan": {
            "t2v": {
                "model": contracts.WAN_MODEL,
                "region": contracts.WAN_REGION,
                "capabilitiesVersion": contracts.WAN_CAPABILITIES_VERSION,
                "shotCapabilitiesVersion": contracts.WAN_SHOT_CAPABILITIES_VERSION,
                "priceVersion": contracts.WAN_PRICE_VERSION,
                "currency": "CNY",
                "unit": "output_second",
                "unitPrice": "0.60",
                "fiveSecondAmount": money(Decimal("0.60") * 5),
                "checkedAt": "2026-10-16",
                "sourceUrl": contracts.WAN_PRICE_SOURCE,
            },
            "i2v": {
                "model": contracts.WAN_I2V_MODEL,
                "priceVersion": contracts.WAN_I2V_PRICE_VERSION,
                "currency": "CNY",
                "unit": "output_second",
                "unitPrice": "0.60",
                "fiveSecondAmount": money(Decimal("0.60") * 5),
                "checkedAt": "2026-10-16",
                "sourceUrl": contracts.WAN_PRICE_SOURCE,
            },
        },
        "kling": {
            "model": contracts.KLING_MODEL,
            "region": contracts.KLING_REGION,
            "capabilitiesVersion": contracts.MULTI_PROVIDER_CAPABILITIES_VERSION,
            "priceVersion": contracts.KLING_PRICE_VERSION,
            "currency": "USD",
            "unit": "output_second",
            "unitPrice": contracts.KLING_RATE_USD,
            "fiveSecondAmount": money(Decimal(contracts.KLING_RATE_USD) * 5),
            "resourceUnitsPerSecond": contracts.KLING_UNITS_PER_SECOND,
            "fiveSecondUnits": int(Decimal(contracts.KLING_UNITS_PER_SECOND) * 5),
            "checkedAt": contracts.MULTI_PROVIDER_PRICE_CHECKED_AT,
            "sourceUrl": contracts.KLING_PRICE_SOURCE,
        },
        "fal": {
            "model": contracts.FAL_MODEL,
            "region": contracts.FAL_REGION,
            "capabilitiesVersion": contracts.MULTI_PROVIDER_CAPABILITIES_VERSION,
            "priceVersion": contracts.FAL_PRICE_VERSION,
            "currency": "USD",
            "unit": "output_second",
            "unitPrice": contracts.FAL_RATE_USD,
            "fiveSecondAmount": money(Decimal(contracts.FAL_RATE_USD) * 5),
            "checkedAt": contracts.MULTI_PROVIDER_PRICE_CHECKED_AT,
            "sourceUrl": contracts.FAL_PRICE_SOURCE,
        },
        "tts": {
            "provider": tts.COSYVOICE_PROVIDER,
            "model": tts.COSYVOICE_MODEL,
            "providerVersion": tts.TTS_PROVIDERS_VERSION,
            "priceVersion": tts.COSYVOICE_PRICE_VERSION,
            "currency": "CNY",
            "unit": "thousand_characters",
            "unitPrice": "0.20",
            "rpm": tts.COSYVOICE_RPM,
            "checkedAt": "2026-10-22",
            "sourceUrl": tts.COSYVOICE_PRICE_SOURCE,
            "realSample": False,
        },
    }


def accounting_limits() -> dict[str, object]:
    from vagent.config import VIDEO_SETTINGS
    from vagent.video import audio, contracts, episodes
    from vagent.video import batches as batch_module
    from vagent.video import tools as video_tools

    # Stored settings may only tighten a frozen ceiling, so the default is the widest.
    defaults = {name: default for name, (_, _, default) in VIDEO_SETTINGS.items()}

    return {
        "storageSchemaVersion": 9,
        "liveJobsPerRun": contracts.LIVE_JOBS_PER_RUN,
        "episode": {
            "costCap": episodes.EPISODE_COST_CAP,
            "shotEstimateAmount": episodes.SHOT_ESTIMATE_AMOUNT,
            "jobCap": contracts.EPISODE_JOB_CAP,
            "maxShots": episodes.EPISODE_MAX_SHOTS,
            "maxSeconds": episodes.EPISODE_MAX_SECONDS,
        },
        "batch": {
            "episodes": batch_module.BATCH_EPISODE_LIMIT,
            "jobs": batch_module.BATCH_JOB_LIMIT,
            "amount": batch_module.BATCH_AMOUNT_LIMIT,
            "perEpisodeJobs": batch_module.BATCH_EPISODE_JOB_LIMIT,
            "perProjectBatches": batch_module.BATCH_PER_PROJECT_LIMIT,
        },
        "providerCeilings": {
            "kling": {
                "maxJobAmount": contracts.KLING_MAX_JOB_USD,
                "maxRunAmount": contracts.KLING_MAX_RUN_USD,
                "maxJobUnits": contracts.KLING_MAX_JOB_UNITS,
                "maxRunUnits": contracts.KLING_MAX_RUN_UNITS,
                "currency": "USD",
            },
            "fal": {
                "maxJobAmount": contracts.FAL_MAX_JOB_USD,
                "maxRunAmount": contracts.FAL_MAX_RUN_USD,
                "currency": "USD",
            },
            "wan": {
                "maxJobAmount": defaults["videoMaxJobCost"],
                "maxRunAmount": defaults["videoMaxRunCost"],
                "currency": "CNY",
            },
        },
        "tts": {
            "episodeCharacters": audio.TTS_EPISODE_CHARACTER_LIMIT,
            "requestCharacters": audio.TTS_REQUEST_CHARACTER_LIMIT,
            "episodeRequests": audio.TTS_EPISODE_REQUEST_LIMIT,
            "episodeCostLimit": str(audio.TTS_EPISODE_COST_LIMIT),
            "requestCostLimit": str(audio.TTS_REQUEST_COST_LIMIT),
            "rpm": 180,
        },
        "wait": {"setLimit": video_tools.WAIT_SET_LIMIT, "timeoutSeconds": video_tools.WAIT_TIMEOUT_SECONDS},
    }


def _currency(entry: dict) -> str:
    """The billing currency of one capability entry, in the provider's own money."""
    return entry["price"]["currency"] if entry["provider"] == "wan" else entry["rate"]["currency"]


def _quote(entry: dict, duration: int) -> dict[str, object]:
    """The frozen quote of one capability entry for one duration (no provider call)."""
    from vagent.video.contracts import (
        ProviderRate,
        ResourceRate,
        VideoPrice,
        provider_price,
        resource_estimate,
        shot_price,
    )

    if entry["provider"] == "wan":
        price = shot_price(VideoPrice.model_validate(entry["price"]), duration)
    else:
        price = provider_price(ProviderRate.model_validate(entry["rate"]), duration)
    payload: dict[str, object] = {
        "currency": price.currency,
        "unitPrice": price.unit_price,
        "quantity": price.quantity,
        "amount": price.amount,
        "priceVersion": price.price_version,
        "sourceUrl": price.source_url,
    }
    if entry.get("resource") is not None:
        units = resource_estimate(ResourceRate.model_validate(entry["resource"]), duration)
        payload["resource"] = {"unit": units.unit, "units": units.units}
    return payload


def _spec_matches(entry: dict, request: dict) -> bool:
    mode, duration, aspect = request["mode"], request["durationSeconds"], request["aspectRatio"]
    return any(
        spec["mode"] == mode
        and spec["durationSeconds"] == duration
        and (mode != "t2v" or spec["aspectRatio"] == aspect)
        for spec in entry.get("specs", ())
    )


def _job_ceiling(entry: dict, quote: dict) -> str | None:
    """The per-Job ceiling the registration path would enforce (VIDEO_COST_LIMIT)."""
    amount, currency, units = JOB_CEILINGS[entry["provider"]]
    if quote["currency"] != currency or Decimal(quote["amount"]) > Decimal(amount):
        return "VIDEO_COST_LIMIT"
    resource = quote.get("resource") or {}
    if units is not None and int(resource.get("units", 0)) > units:
        return "VIDEO_COST_LIMIT"
    return None


def _select_entry(entries: list[dict], request: dict) -> dict:
    """One row per provider: the frozen entry that best serves the request."""
    mode = request["mode"]
    for entry in entries:
        if mode in tuple(entry.get("shotModes") or ()) and _spec_matches(entry, request):
            return entry
    for entry in entries:
        if mode in tuple(entry.get("shotModes") or ()):
            return entry
    return entries[0]


def _candidate(entries: list[dict], request: dict) -> dict[str, object]:
    """Apply the frozen availability precedence to one provider's entries.

    Precedence: environment -> credentials -> capability -> price -> accounting.
    The per-Job ceiling is checked because registration enforces it; without that
    check the preview would suggest a quote that ``VIDEO_COST_LIMIT`` rejects.
    """
    entry = _select_entry(entries, request)
    mode = request["mode"]
    scope = request["scope"]
    reason = None
    quote = None
    if mode not in tuple(entry.get("shotModes") or ()):
        reason = "VIDEO_UNSUPPORTED_MODE"
    elif not _spec_matches(entry, request):
        reason = "VIDEO_UNSUPPORTED_SPEC"
    elif scope in {"episode", "batch"} and _currency(entry) != "CNY":
        reason = "EPISODE_COST_LIMIT"
    else:
        quote = _quote(entry, request["durationSeconds"])
        reason = _job_ceiling(entry, quote)
    if reason is not None:
        quote = None
    return {
        "provider": entry["provider"],
        "model": entry["model"],
        "capabilitiesVersion": entry["capabilitiesVersion"],
        "audioMode": entry.get("audioMode"),
        "available": reason is None,
        "reason": reason,
        "quote": quote,
    }


def _ordered(candidates: list[dict]) -> list[dict]:
    currency_rank = {name: index for index, name in enumerate(CURRENCY_ORDER)}
    provider_rank = {name: index for index, name in enumerate(PROVIDER_ORDER)}
    available = [item for item in candidates if item["available"]]
    blocked = [item for item in candidates if not item["available"]]
    available.sort(
        key=lambda item: (
            currency_rank[item["quote"]["currency"]],
            Decimal(item["quote"]["amount"]),
            provider_rank[item["provider"]],
        )
    )
    blocked.sort(key=lambda item: provider_rank[item["provider"]])
    return available + blocked


def _by_provider(live_entries: list[dict]) -> dict[str, list[dict]]:
    grouped: dict[str, list[dict]] = {}
    for entry in live_entries:
        grouped.setdefault(entry["provider"], []).append(entry)
    return grouped


def route_probe(live_entries: list[dict]) -> dict[str, object]:
    """Apply the frozen sort/refusal rules to the frozen table, without a clock."""
    requests = (
        {"scope": "shot", "mode": "t2v", "durationSeconds": 5, "aspectRatio": "16:9"},
        {"scope": "shot", "mode": "t2v", "durationSeconds": 10, "aspectRatio": "16:9"},
        {"scope": "shot", "mode": "i2v", "durationSeconds": 5, "aspectRatio": None},
        {"scope": "episode", "mode": "t2v", "durationSeconds": 5, "aspectRatio": "16:9"},
    )
    rows = []
    for request in requests:
        candidates = _ordered(
            [
                _candidate(entries, request)
                for _, entries in sorted(
                    _by_provider(live_entries).items(),
                    key=lambda item: PROVIDER_ORDER.index(item[0]),
                )
            ]
        )
        rows.append(
            {
                "request": request,
                "candidates": candidates,
                "recommended": next((item["provider"] for item in candidates if item["available"]), None),
            }
        )
    return {
        "currencyOrder": list(CURRENCY_ORDER),
        "providerOrder": list(PROVIDER_ORDER),
        "sortKey": ["available", "currencyOrder", "amount", "providerOrder"],
        "keysConfigured": {"wan": True, "kling": True, "fal": True},
        "videoMode": "live",
        "jobCeilings": {
            provider: {"amount": amount, "currency": currency, "units": units}
            for provider, (amount, currency, units) in JOB_CEILINGS.items()
        },
        "jobCeilingsNote": "默认本地设置下的有效单 Job 上限；真实预览用实际配置计算",
        "rows": rows,
        "rowsSha256": canonical_sha256(rows),
    }


# --- frozen M8 decisions (documented in docs/M8_CONTRACTS.md) ------------------


def frozen_decisions() -> dict[str, object]:
    return {
        "storageSchemaVersion": 9,
        "toolsVersion": 18,
        "rulesVersion": 18,
        "newVideoTools": [],
        "newErrorCodes": [
            "ROUTE_UNAVAILABLE",
            "PROVIDER_BACKOFF",
            "BILL_RECORD_INVALID",
            "BILL_CONFLICT",
        ],
        "billing": {
            "ledgerVersion": 1,
            "ledgerFile": "billing.json",
            "insideStateJson": False,
            "currencies": ["CNY", "USD"],
            "crossCurrencyAggregation": False,
            "unknownIsZero": False,
            "automaticProviderBillingFetch": False,
        },
        "route": {
            "tool": None,
            "schemaChange": None,
            "clock": None,
            "currencyOrder": list(CURRENCY_ORDER),
            "providerOrder": list(PROVIDER_ORDER),
            "crossCurrencyComparison": False,
            "reasonPrecedence": [
                "VIDEO_MODE_OFF",
                "VIDEO_PROVIDER_UNAVAILABLE",
                "VIDEO_KEY_MISSING",
                "VIDEO_UNSUPPORTED_MODE",
                "VIDEO_UNSUPPORTED_SPEC",
                "VIDEO_PRICE_UNKNOWN",
                "EPISODE_COST_LIMIT",
                "VIDEO_COST_LIMIT",
            ],
        },
        "backoff": {
            "workerConcurrency": 1,
            "submitsInFlightPerProvider": 1,
            "globalCallIntervalSeconds": 1.0,
            "submitIntervalSeconds": {"wan": 2.0, "kling": 2.0, "fal": 2.0},
            "submitIntervalBoundsSeconds": [2.0, 60.0],
            "cooldownSeconds": 60.0,
            "cooldownBoundsSeconds": [60.0, 900.0],
            "retryAfterCapSeconds": 900.0,
            "automaticResubmit": False,
            "retryTransportFailure": False,
            "retryProvenNonAccepted": False,
            "resubmitUnknown": False,
        },
    }


def main() -> int:
    contracts = contract_schemas()
    surface = asyncio.run(published_surface())
    report = {
        "baseline": "m8-e0",
        "purpose": (
            "Pin the post-M7 contract schemas, published tool surfaces, provider capability "
            "and price tables, accounting ceilings and the frozen route ordering M8 extends "
            "without changing any of them."
        ),
        "releaseVersion": _release_version(),
        "zeroSpend": {
            "submit": 0,
            "modelCalls": 0,
            "providerRequests": 0,
            "credentialsRead": False,
            "jobsRegistered": 0,
        },
        "contracts": contracts,
        "tools": surface["tools"],
        "capabilities": surface["capabilities"],
        "prices": price_table(),
        "limits": accounting_limits(),
        "routeProbe": route_probe(surface["capabilities"]["live"]),
        "frozen": frozen_decisions(),
    }
    OUTPUT.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    print(f"wrote {OUTPUT.relative_to(ROOT)}")
    return 0


def _release_version() -> str:
    """The release the E0 baseline was frozen on, not the version regenerating it.

    E0 froze these bytes on the v0.5.0 tree. The file records that release, so the
    v0.6.0 version bump at E4 must not rewrite a single frozen byte; reading the
    current ``pyproject.toml`` here would silently change ``releaseVersion`` and
    invalidate the SHA-256 the acceptance documents pin.
    """
    return FROZEN_RELEASE_VERSION


if __name__ == "__main__":
    sys.exit(main())
