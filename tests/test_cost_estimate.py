"""H6 token->money: exact decimal math, honest unknowns, and the cache saving number."""

import copy
import json
from pathlib import Path

from vagent import pricing
from vagent.cli import parser


def run_of(model="deepseek-flash", *calls):
    return {
        "id": "run-1",
        "sessionId": "coffee",
        "model": model,
        "status": "completed",
        "modelSteps": len(calls),
        "usageStartStep": 1,
        "inputTokens": sum(call.get("inputTokens") or 0 for call in calls),
        "outputTokens": sum(call.get("outputTokens") or 0 for call in calls),
        "modelCalls": list(calls),
    }


def call(input_tokens=None, output_tokens=None, hit=None, miss=None):
    return {
        "step": 1,
        "status": "responded",
        "inputTokens": input_tokens,
        "outputTokens": output_tokens,
        "cacheHitTokens": hit,
        "cacheMissTokens": miss,
    }


# --- exact math -------------------------------------------------------------------


def test_one_million_tokens_of_each_kind_prices_to_the_table():
    estimate = pricing.estimate_tokens(
        "deepseek-flash", input_tokens=1_000_000, output_tokens=1_000_000, cache_miss_tokens=1_000_000
    )
    # 1M input at the miss rate (2) + 1M output (8).
    assert estimate["amount"] == "10.000000"
    assert estimate["currency"] == "CNY"
    assert estimate["cacheUsage"] == "derived"
    assert estimate["pricingVersion"] == pricing.PRICING_VERSION


def test_the_reported_cache_split_is_priced_as_reported():
    estimate = pricing.estimate_tokens(
        "deepseek-flash",
        input_tokens=1_000_000,
        output_tokens=1_000_000,
        cache_hit_tokens=400_000,
        cache_miss_tokens=600_000,
    )
    # 0.6 * 2 (miss) + 0.4 * 0.5 (hit) + 1 * 8 (output) = 9.4
    assert estimate["amount"] == "9.400000"
    assert estimate["cacheUsage"] == "reported"
    assert (estimate["inputCacheHitTokens"], estimate["inputCacheMissTokens"]) == (400_000, 600_000)


def test_a_half_reported_split_is_derived_and_an_impossible_one_falls_back_to_miss():
    derived = pricing.estimate_tokens(
        "deepseek-flash", input_tokens=1_000_000, output_tokens=0, cache_hit_tokens=700_000
    )
    assert derived["cacheUsage"] == "derived"
    assert derived["inputCacheMissTokens"] == 300_000
    # 0.7 * 0.5 + 0.3 * 2 = 0.95
    assert derived["amount"] == "0.950000"

    impossible = pricing.estimate_tokens(
        "deepseek-flash",
        input_tokens=1_000_000,
        output_tokens=0,
        cache_hit_tokens=2_000_000,
        cache_miss_tokens=0,
    )
    assert impossible["cacheUsage"] == "assumed-miss"
    assert impossible["amount"] == "2.000000"


def test_amounts_stay_exact_at_micro_scales():
    assert pricing.estimate_tokens("deepseek-flash", input_tokens=1, output_tokens=0, cache_miss_tokens=1)[
        "amount"
    ] == ("0.000002")
    # Three one-token calls summed through Decimal: 6e-06, not 5.999999e-06.
    run = run_of(
        "deepseek-flash",
        call(1, 0, None, 1),
        call(1, 0, None, 1),
        call(1, 0, None, 1),
    )
    assert pricing.estimate_run(run)["amount"] == "0.000006"


# --- honest unknowns --------------------------------------------------------------


def test_an_unknown_model_is_never_guessed():
    assert pricing.price_for("some-future-model") is None
    assert pricing.estimate_tokens("some-future-model", input_tokens=10, output_tokens=10) is None
    estimate = pricing.estimate_run(run_of("some-future-model", call(10, 10)))
    assert estimate["status"] == "unpriced" and estimate["reason"] == "unknown-model"
    assert estimate["amount"] is None and estimate["currency"] is None


def test_a_call_without_token_usage_is_reported_not_estimated():
    assert pricing.estimate_tokens("deepseek-flash", input_tokens=None, output_tokens=5) is None
    run = run_of("deepseek-flash", call(1_000_000, 0, None, 1_000_000), call(None, None))
    estimate = pricing.estimate_run(run)
    assert estimate["status"] == "partial"
    assert (estimate["pricedCalls"], estimate["unpricedCalls"]) == (1, 1)
    assert estimate["amount"] == "2.000000"


def test_untracked_model_steps_make_the_estimate_partial():
    run = run_of("deepseek-flash", call(1_000_000, 0, None, 1_000_000))
    run["modelSteps"] = 3
    run["usageStartStep"] = 2  # one older step was never given a call row
    estimate = pricing.estimate_run(run)
    assert estimate["status"] == "partial"
    assert estimate["untrackedModelSteps"] == 1
    assert estimate["amountIsLowerBound"] is True
    assert "下限" in estimate["note"]
    assert estimate["amount"] == "2.000000"
    assert pricing.estimate_runs([run])["amountIsLowerBound"] is True


def test_fully_tracked_run_stays_priced():
    run = run_of("deepseek-flash", call(1_000_000, 0, None, 1_000_000))
    estimate = pricing.estimate_run(run)
    assert estimate["status"] == "priced"
    assert estimate["untrackedModelSteps"] == 0
    assert "amountIsLowerBound" not in estimate and "note" not in estimate
    assert pricing.estimate_runs([run])["amountIsLowerBound"] is False


def test_an_unpriceable_run_says_why():
    estimate = pricing.estimate_run(run_of("deepseek-flash", call(None, None)))
    assert estimate["status"] == "unpriced" and estimate["reason"] == "no-priced-calls"


# --- the comparison number --------------------------------------------------------


def test_cache_aware_costing_beats_pricing_every_input_at_miss_rate():
    runs = [
        run_of(
            "deepseek-flash",
            call(1_000_000, 1_000_000, 400_000, 600_000),
            call(1_000_000, 1_000_000, 400_000, 600_000),
        )
    ]
    comparison = pricing.savings(runs)
    row = comparison["rows"][0]
    assert row["currency"] == "CNY"
    assert row["cacheAware"] == "18.800000"
    assert row["cacheBlind"] == "20.000000"
    assert row["saved"] == "1.200000"
    assert row["savedPercent"] == "6.00"


def test_estimating_does_not_mutate_the_run():
    run = run_of("deepseek-flash", call(1_000_000, 1_000, 100, 999_900))
    before = copy.deepcopy(run)
    pricing.estimate_run(run)
    pricing.savings([run])
    assert run == before


def test_estimation_is_deterministic():
    run = run_of("deepseek-flash", call(1_000_000, 1_000, 100, 999_900))
    assert json.dumps(pricing.estimate_run(run), sort_keys=True) == json.dumps(
        pricing.estimate_run(run), sort_keys=True
    )


# --- wiring and discipline --------------------------------------------------------


def test_the_cli_flag_is_opt_in():
    assert parser().parse_args(["usage"]).cost is False
    assert parser().parse_args(["usage", "--cost"]).cost is True
    assert parser().parse_args(["usage", "--project", "coffee", "--cost"]).cost is True


def test_the_usage_payload_is_unchanged_unless_cost_is_asked_for():
    from vagent.cli import usage_payload

    runs = [run_of("deepseek-flash", call(1_000_000, 1_000_000, 400_000, 600_000))]
    plain = usage_payload(runs)
    assert "costTotal" not in plain and "cost" not in plain["runs"][0]
    assert set(plain["runs"][0]) == {
        "id",
        "sessionId",
        "model",
        "status",
        "contextVersion",
        "readOnly",
        "usage",
        "modelCalls",
    }

    priced = usage_payload(runs, cost=True)
    assert priced["runs"][0]["cost"]["amount"] == "9.400000"
    assert priced["costTotal"]["totals"] == [{"currency": "CNY", "amount": "9.400000"}]


def test_pricing_module_is_offline_and_dependency_free():
    text = (Path(__file__).resolve().parents[1] / "src" / "vagent" / "pricing.py").read_text(encoding="utf-8")
    for forbidden in ("httpx", "requests", "socket", "urllib", "open("):
        assert forbidden not in text


def test_the_price_table_carries_its_provenance():
    assert pricing.PRICING_VERSION and pricing.PRICE_AS_OF
    for model, price in pricing.PRICES.items():
        assert model and price.currency in {"CNY", "USD"}
        assert price.input_cache_hit < price.input_cache_miss
        assert price.output > 0
