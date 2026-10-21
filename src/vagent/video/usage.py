"""Read-side aggregation of local Job, cost, token and library records (M2 D3).

Nothing here contacts a provider or a model: these numbers are the local record of
what was registered, estimated and observed. An actual bill that was never
reported stays unknown, and unknown usage is never counted as zero.
"""

from collections import defaultdict
from datetime import UTC, datetime
from decimal import Decimal

TERMINAL_JOB_STATUSES = {"succeeded", "failed", "unknown", "download_failed"}
DAILY_LIMIT = 31


def money(value) -> str:
    return format(Decimal(value), ".2f")


def summarize_runs(runs) -> dict:
    """Weighted token and cache coverage over every recorded model call of a scope."""
    calls, untracked, model_steps = [], 0, 0
    for run in runs:
        start = run.get("usageStartStep")
        steps = run.get("modelSteps", 0)
        untracked += min(steps, start - 1) if start else steps
        model_steps += steps
        calls.extend(run.get("modelCalls", []))
    token_calls = [
        call for call in calls if call.get("inputTokens") is not None and call.get("outputTokens") is not None
    ]
    cache_calls = [
        call
        for call in calls
        if call.get("cacheHitTokens") is not None and call.get("cacheMissTokens") is not None
    ]
    hits = [call["cacheHitTokens"] for call in calls if call.get("cacheHitTokens") is not None]
    misses = [call["cacheMissTokens"] for call in calls if call.get("cacheMissTokens") is not None]
    paired = sum(call["cacheHitTokens"] + call["cacheMissTokens"] for call in cache_calls)
    return {
        "runs": len(runs),
        "modelSteps": model_steps,
        "recordedCallCount": len(calls),
        "untrackedModelSteps": untracked,
        "callsWithTokenUsage": len(token_calls),
        "callsWithCacheUsage": len(cache_calls),
        "observedInputTokens": sum(run.get("inputTokens", 0) for run in runs),
        "observedOutputTokens": sum(run.get("outputTokens", 0) for run in runs),
        "cacheHitTokens": sum(hits) if hits else None,
        "cacheMissTokens": sum(misses) if misses else None,
        "cacheHitRate": (sum(call["cacheHitTokens"] for call in cache_calls) / paired if paired else None),
        "tokenUsageComplete": bool(runs) and untracked == 0 and len(token_calls) == len(calls),
        "cacheUsageComplete": bool(runs) and untracked == 0 and len(cache_calls) == len(calls),
    }


def summarize_records(state: dict, *, project_id: str | None = None) -> dict:
    """Aggregate one project (or the whole data directory) without any external call."""
    jobs = [
        raw
        for raw in state["jobs"].values()
        if project_id is None or raw["context"]["projectId"] == project_id
    ]
    by_status: dict[str, int] = defaultdict(int)
    by_mode: dict[str, int] = defaultdict(int)
    by_price: dict[str, int] = defaultdict(int)
    by_provider: dict[str, int] = defaultdict(int)
    provider_amounts: dict[tuple[str, str], Decimal] = defaultdict(Decimal)
    provider_units: dict[str, int] = defaultdict(int)
    daily: dict[str, dict] = {}
    estimated = Decimal(0)
    reported = Decimal(0)
    reported_jobs = 0
    unknown_jobs = 0
    open_jobs = 0
    for raw in jobs:
        by_status[raw.get("status", "unknown")] += 1
        by_mode[raw.get("mode", "unknown")] += 1
        if raw.get("status") not in TERMINAL_JOB_STATUSES:
            open_jobs += 1
        cost = raw.get("cost")
        amount = Decimal(0)
        provider, currency = "wan", "CNY"
        if raw.get("contractVersion") == 6:
            provider = raw["request"]["provider"]
            currency = cost["estimate"]["currency"] if cost else "USD"
        by_provider[provider] += 1
        if cost:
            amount = Decimal(cost["estimate"]["amount"])
            provider_amounts[(provider, currency)] += amount
            if raw.get("contractVersion") == 6:
                units = (cost.get("resourceEstimate") or {}).get("units")
                if isinstance(units, int):
                    provider_units[provider] += units
            # Top-level totals stay the Wan CNY bucket for historical filters.
            if currency == "CNY":
                estimated += amount
            actual = cost.get("actual") or {}
            if actual.get("status") == "reported" and actual.get("amount") is not None:
                reported += Decimal(actual["amount"])
                reported_jobs += 1
            else:
                unknown_jobs += 1
            price_version = cost["estimate"].get("priceVersion")
            if price_version:
                by_price[price_version] += 1
        day = str(raw.get("createdAt") or "")[:10]
        if day:
            bucket = daily.setdefault(day, {"date": day, "jobs": 0, "live": 0, "estimated": Decimal(0)})
            bucket["jobs"] += 1
            bucket["live"] += 1 if raw.get("mode") == "live" else 0
            bucket["estimated"] += amount if currency == "CNY" else Decimal(0)
    storyboards = [
        raw for raw in state["storyboards"].values() if project_id is None or raw["projectId"] == project_id
    ]
    assets = state["assets"]
    project_assets = [raw for raw in assets.values() if project_id is None or raw["projectId"] == project_id]
    runs = [raw for raw in state["runs"].values() if project_id is None or raw["sessionId"] == project_id]
    timeline = sorted(daily.values(), key=lambda item: item["date"])[-DAILY_LIMIT:]
    return {
        "projectId": project_id,
        "generatedAt": datetime.now(UTC).isoformat(),
        "jobs": {
            "total": len(jobs),
            "open": open_jobs,
            "live": by_mode.get("live", 0),
            "mock": by_mode.get("mock", 0),
            "byStatus": dict(sorted(by_status.items())),
            "byMode": dict(sorted(by_mode.items())),
            "byProvider": dict(sorted(by_provider.items())),
        },
        "cost": {
            "currency": "CNY",
            "estimateAmount": money(estimated),
            "byPriceVersion": dict(sorted(by_price.items())),
            "actual": {
                "status": "unknown" if unknown_jobs else "reported" if reported_jobs else "unknown",
                "reportedAmount": money(reported),
                "reportedJobs": reported_jobs,
                "unknownJobs": unknown_jobs,
            },
        },
        "costByProvider": [
            {
                "provider": provider,
                "currency": currency,
                "jobs": by_provider[provider],
                "estimated": money(amount),
                **(
                    {"units": provider_units[provider]}
                    if (provider, currency) in provider_amounts and provider_units[provider]
                    else {}
                ),
            }
            for (provider, currency), amount in sorted(provider_amounts.items())
        ],
        "storyboards": {
            "total": len(storyboards),
            "shotVersions": sum(len(raw.get("shotVersions", ())) for raw in storyboards),
        },
        "assets": {"total": len(project_assets), "libraryTotal": len(assets)},
        "runs": summarize_runs(runs),
        "daily": [{**item, "estimated": money(item["estimated"])} for item in timeline],
    }
