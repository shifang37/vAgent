"""H6 token->money estimates from a versioned, hand-maintained, offline price table.

Prices are data in this file, never fetched and never inferred: an unknown model or an
unknown token count yields **no** estimate, because a guessed price is worse than a
missing one — the same discipline the rest of the harness applies to missing usage.

The table is a *reference* mapping: the model alias is the one this project sends, the
numbers are the provider's published per-million-token prices at ``PRICE_AS_OF``, and the
operator is expected to check them against the invoice. Currency is carried per model and
is never converted (the billing ledger's rule).
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

PRICING_VERSION = "deepseek-reference-v1"
PRICE_AS_OF = "2026-11-05"
PER_TOKENS = Decimal(1_000_000)
AMOUNT_EXPONENT = Decimal("0.000001")


@dataclass(frozen=True)
class Price:
    """Per-million-token prices for one model, in one currency."""

    currency: str
    input_cache_hit: Decimal
    input_cache_miss: Decimal
    output: Decimal


def _price(currency: str, hit: str, miss: str, output: str) -> Price:
    return Price(currency, Decimal(hit), Decimal(miss), Decimal(output))


# Reference prices, CNY per million tokens. DeepSeek publishes a cache-hit input price
# that is a quarter of the cache-miss price, which is why the split is priced, not summed.
PRICES: dict[str, Price] = {
    "deepseek-flash": _price("CNY", "0.5", "2", "8"),
    "deepseek-chat": _price("CNY", "0.5", "2", "8"),
    "deepseek-reasoner": _price("CNY", "1", "4", "16"),
}


def price_for(model: str | None) -> Price | None:
    """The reference price of an exact model name; unknown models are not guessed."""
    if not isinstance(model, str) or not model:
        return None
    return PRICES.get(model)


def _amount(value: Decimal) -> str:
    return format(value.quantize(AMOUNT_EXPONENT), "f")


def estimate_tokens(
    model: str | None,
    *,
    input_tokens: int | None,
    output_tokens: int | None,
    cache_hit_tokens: int | None = None,
    cache_miss_tokens: int | None = None,
    cache_blind: bool = False,
) -> dict | None:
    """Price one model call, or None when the model or the token counts are unknown.

    ``cache_blind`` prices every input token at the cache-miss rate: the comparison
    baseline that shows what the reported cache split is actually saving.
    """
    price = price_for(model)
    if price is None or input_tokens is None or output_tokens is None:
        return None
    if input_tokens < 0 or output_tokens < 0:
        return None
    hit, miss, cache_usage = cache_hit_tokens, cache_miss_tokens, "reported"
    if cache_blind:
        hit, miss, cache_usage = 0, input_tokens, "assumed-miss"
    elif hit is not None and miss is not None:
        if hit < 0 or miss < 0 or hit + miss != input_tokens:
            hit, miss, cache_usage = 0, input_tokens, "assumed-miss"
    elif hit is not None and hit <= input_tokens:
        miss, cache_usage = input_tokens - hit, "derived"
    elif miss is not None and miss <= input_tokens:
        hit, cache_usage = input_tokens - miss, "derived"
    else:
        hit, miss, cache_usage = 0, input_tokens, "assumed-miss"
    cost = (
        Decimal(hit) * price.input_cache_hit
        + Decimal(miss) * price.input_cache_miss
        + Decimal(output_tokens) * price.output
    ) / PER_TOKENS
    return {
        "model": model,
        "currency": price.currency,
        "amount": _amount(cost),
        "inputCacheHitTokens": hit,
        "inputCacheMissTokens": miss,
        "outputTokens": output_tokens,
        "cacheUsage": cache_usage,
        "pricingVersion": PRICING_VERSION,
        "priceAsOf": PRICE_AS_OF,
    }


def _total(amounts: list[dict]) -> Decimal:
    total = Decimal(0)
    for item in amounts:
        try:
            total += Decimal(item["amount"])
        except InvalidOperation:  # pragma: no cover - amounts are produced by _amount
            return Decimal(0)
    return total


def estimate_run(run: dict, *, cache_blind: bool = False) -> dict:
    """Price every recorded model call of one Run; honest about what could not be priced."""
    model = run.get("model")
    priced: list[dict] = []
    unpriced = 0
    currencies = set()
    for call in run.get("modelCalls", []):
        estimate = estimate_tokens(
            model,
            input_tokens=call.get("inputTokens"),
            output_tokens=call.get("outputTokens"),
            cache_hit_tokens=call.get("cacheHitTokens"),
            cache_miss_tokens=call.get("cacheMissTokens"),
            cache_blind=cache_blind,
        )
        if estimate is None:
            unpriced += 1
            continue
        priced.append(estimate)
        currencies.add(estimate["currency"])
    base = {
        "model": model,
        "pricingVersion": PRICING_VERSION,
        "priceAsOf": PRICE_AS_OF,
        "cacheBlind": cache_blind,
        "pricedCalls": len(priced),
        "unpricedCalls": unpriced,
        "cacheAssumedMissCalls": sum(1 for item in priced if item["cacheUsage"] == "assumed-miss"),
    }
    untracked = _untracked_model_steps(run)
    base["untrackedModelSteps"] = untracked
    if not priced:
        return {
            **base,
            "status": "unpriced",
            "reason": "unknown-model" if price_for(model) is None else "no-priced-calls",
            "currency": None,
            "amount": None,
        }
    if len(currencies) > 1:
        return {**base, "status": "mixed-currency", "currency": None, "amount": None}
    # Model steps recorded before per-call tracking began have tokens in the Run totals
    # but no call row to price: the sum is a lower bound, and saying "priced" would be
    # a lie about a number the operator may reconcile against an invoice.
    partial = unpriced > 0 or untracked > 0
    estimate = {
        **base,
        "status": "partial" if partial else "priced",
        "currency": currencies.pop(),
        "amount": _amount(_total(priced)),
        "inputCacheHitTokens": sum(item["inputCacheHitTokens"] for item in priced),
        "inputCacheMissTokens": sum(item["inputCacheMissTokens"] for item in priced),
        "outputTokens": sum(item["outputTokens"] for item in priced),
    }
    if untracked:
        estimate["amountIsLowerBound"] = True
        estimate["note"] = f"另有 {untracked} 个模型步没有调用明细，金额是已记录调用的下限。"
    return estimate


def _untracked_model_steps(run: dict) -> int:
    from vagent.usage import summarize_usage

    try:
        return summarize_usage(run)["untrackedModelSteps"]
    except KeyError:  # a partial record (a reporting script may pass one)
        return 0


def estimate_runs(runs, *, cache_blind: bool = False) -> dict:
    """Aggregate runs of one currency; a mixed result reports per-currency totals."""
    per_currency: dict[str, Decimal] = {}
    priced_runs = 0
    unpriced_runs = 0
    partial_runs = 0
    for run in runs:
        estimate = estimate_run(run, cache_blind=cache_blind)
        if estimate["status"] == "unpriced":
            unpriced_runs += 1
            continue
        if estimate["status"] == "partial":
            partial_runs += 1
        if estimate["status"] == "mixed-currency":
            unpriced_runs += 1
            continue
        priced_runs += 1
        per_currency[estimate["currency"]] = per_currency.get(estimate["currency"], Decimal(0)) + Decimal(
            estimate["amount"]
        )
    return {
        "pricingVersion": PRICING_VERSION,
        "priceAsOf": PRICE_AS_OF,
        "cacheBlind": cache_blind,
        "pricedRuns": priced_runs,
        "partialRuns": partial_runs,
        "unpricedRuns": unpriced_runs,
        # Any partial Run makes the aggregate a lower bound, never a full quote.
        "amountIsLowerBound": partial_runs > 0,
        "totals": [
            {"currency": currency, "amount": _amount(amount)}
            for currency, amount in sorted(per_currency.items())
        ],
    }


def savings(runs) -> dict:
    """What the reported cache split saves against pricing every input at miss rate."""
    aware = estimate_runs(runs)
    blind = estimate_runs(runs, cache_blind=True)
    rows = []
    blind_by_currency = {item["currency"]: Decimal(item["amount"]) for item in blind["totals"]}
    for item in aware["totals"]:
        compared = blind_by_currency.get(item["currency"])
        if compared is None:
            continue
        saved = compared - Decimal(item["amount"])
        percent = (saved / compared * 100) if compared else Decimal(0)
        rows.append(
            {
                "currency": item["currency"],
                "cacheAware": item["amount"],
                "cacheBlind": _amount(compared),
                "saved": _amount(saved),
                "savedPercent": format(percent.quantize(Decimal("0.01")), "f"),
            }
        )
    return {"pricingVersion": PRICING_VERSION, "priceAsOf": PRICE_AS_OF, "rows": rows}
