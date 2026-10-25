"""M8 E2: the per-provider submit gate, its frozen bounds and the error table.

Every number here comes from docs/M8_CONTRACTS.md §5.4; the tests use an injected
clock so the interval and the 429 cooldown are observed exactly, without sleeping and
without any provider request.
"""

from datetime import UTC, datetime, timedelta

import pytest

from vagent.video.providers.gate import (
    COOLDOWN_BOUNDS,
    COOLDOWN_CODES,
    GLOBAL_CALL_INTERVAL_SECONDS,
    RATE_LIMIT_COOLDOWN_SECONDS,
    RETRY_AFTER_CAP_SECONDS,
    SUBMIT_ERROR_CODES,
    SUBMIT_ERROR_TABLE,
    SUBMIT_INTERVAL_BOUNDS,
    SUBMIT_INTERVAL_SECONDS,
    LatencyWindow,
    RuntimeMetrics,
    SubmitGate,
    bounded_seconds,
    cooldown_seconds,
    gate_for,
    metrics_for,
    submit_interval_seconds,
)


class Clock:
    def __init__(self, value=None):
        self.value = value or datetime(2030, 1, 1, tzinfo=UTC)

    def __call__(self):
        return self.value

    def advance(self, seconds):
        self.value += timedelta(seconds=seconds)
        return self.value

    def at(self, seconds_after_start):
        return datetime(2030, 1, 1, tzinfo=UTC) + timedelta(seconds=seconds_after_start)


def gate(clock):
    return SubmitGate(clock=clock)


def test_the_frozen_defaults_and_ranges_are_the_contract_numbers():
    assert SUBMIT_INTERVAL_SECONDS == 2.0
    assert SUBMIT_INTERVAL_BOUNDS == (2.0, 60.0)
    assert RATE_LIMIT_COOLDOWN_SECONDS == 60.0
    assert COOLDOWN_BOUNDS == (60.0, 900.0)
    assert RETRY_AFTER_CAP_SECONDS == 900.0
    assert GLOBAL_CALL_INTERVAL_SECONDS == 1.0
    assert COOLDOWN_CODES == frozenset({"PROVIDER_RATE_LIMITED"})


def test_local_settings_can_only_extend_and_never_shorten():
    class Config:
        video_submit_interval_seconds = 1.0
        video_rate_limit_cooldown_seconds = 5.0

    # Below the frozen floor: the floor still applies.
    assert submit_interval_seconds(Config()) == 2.0
    assert cooldown_seconds(Config()) == 60.0

    class Wider:
        video_submit_interval_seconds = 12.5
        video_rate_limit_cooldown_seconds = 300.0

    assert submit_interval_seconds(Wider()) == 12.5
    assert cooldown_seconds(Wider()) == 300.0

    class Huge:
        video_submit_interval_seconds = 600.0
        video_rate_limit_cooldown_seconds = 4000.0

    # Above the frozen ceiling: clamping, not an unbounded window.
    assert submit_interval_seconds(Huge()) == 60.0
    assert cooldown_seconds(Huge()) == 900.0

    class Broken:
        video_submit_interval_seconds = "not-a-number"
        video_rate_limit_cooldown_seconds = None

    assert submit_interval_seconds(Broken()) == SUBMIT_INTERVAL_SECONDS
    assert cooldown_seconds(Broken()) == RATE_LIMIT_COOLDOWN_SECONDS
    assert submit_interval_seconds(None) == SUBMIT_INTERVAL_SECONDS
    assert cooldown_seconds(None) == RATE_LIMIT_COOLDOWN_SECONDS


def test_bounded_seconds_ignores_booleans_and_non_finite_values():
    assert bounded_seconds(True, low=2.0, high=60.0, default=2.0) == 2.0
    assert bounded_seconds(float("inf"), low=2.0, high=60.0, default=2.0) == 2.0
    assert bounded_seconds("3", low=2.0, high=60.0, default=2.0) == 3.0


def test_the_submit_interval_delays_only_the_next_submit():
    clock = Clock()
    instance = gate(clock)
    assert instance.check("kling").allowed is True

    instance.record_submit("kling")
    clock.advance(1.9)
    blocked = instance.check("kling")
    assert blocked.allowed is False and blocked.reason == "interval"
    assert blocked.remaining_seconds == pytest.approx(0.1, abs=1e-6)
    assert blocked.retry_at == clock.at(2.0).isoformat()
    assert "kling" in blocked.message and clock.at(2.0).isoformat() in blocked.message

    clock.advance(0.1)
    assert instance.check("kling").allowed is True
    # Another provider is untouched: the interval is per provider, not global.
    assert instance.check("wan").allowed is True


def test_a_proven_429_opens_a_per_provider_cooldown():
    clock = Clock()
    instance = gate(clock)
    instance.record_submit("kling")
    clock.advance(5)

    decision = instance.record_rate_limit("kling")
    assert decision.reason == "cooldown" and decision.truncated is False
    assert decision.retry_at == clock.at(65.0).isoformat()

    clock.advance(59.9)
    blocked = instance.check("kling")
    assert blocked.allowed is False and blocked.reason == "cooldown"
    assert blocked.remaining_seconds == pytest.approx(0.1, abs=1e-6)
    assert instance.check("wan").allowed is True
    assert instance.check("fal").allowed is True

    clock.advance(0.1)
    assert instance.check("kling").allowed is True


def test_retry_after_only_extends_the_cooldown_until_the_frozen_cap():
    clock = Clock()
    instance = gate(clock)

    # A longer server window replaces the local one.
    instance.record_rate_limit("kling", (clock.at(0) + timedelta(seconds=120)).isoformat())
    assert instance.check("kling").retry_at == clock.at(120.0).isoformat()

    # A shorter server window never shortens an existing cooldown.
    clock.advance(10)
    instance.record_rate_limit("kling", (clock.at(10) + timedelta(seconds=5)).isoformat())
    assert instance.check("kling").retry_at == clock.at(120.0).isoformat()

    # Beyond 900 seconds the window is clamped and the diagnostic says so.
    clock.advance(200)
    truncated = instance.record_rate_limit("wan", (clock.at(210) + timedelta(seconds=5000)).isoformat())
    assert truncated.truncated is True
    assert truncated.retry_at == clock.at(210 + 900).isoformat()
    assert "900" in truncated.message and "截断" in truncated.message
    assert instance.check("wan").truncated is True


def test_configure_updates_the_live_window_within_the_frozen_bounds():
    clock = Clock()
    instance = gate(clock)
    instance.record_submit("kling")
    clock.advance(1)
    assert instance.check("kling").allowed is False

    instance.configure(interval_seconds=5.0)
    clock.advance(3.5)
    # 4.5 s after the submit, a 5 s interval still blocks.
    assert instance.check("kling").allowed is False
    clock.advance(0.5)
    assert instance.check("kling").allowed is True

    instance.configure(interval_seconds=0.5, cooldown_seconds=1.0)
    assert instance.interval_seconds == 2.0 and instance.cooldown_seconds == 60.0
    instance.configure(cooldown_seconds=300.0)
    assert instance.cooldown_seconds == 300.0


def test_clear_forgets_one_provider_or_everything_without_touching_anything_else():
    clock = Clock()
    instance = gate(clock)
    instance.record_submit("kling")
    instance.record_rate_limit("fal")
    instance.clear("kling")
    assert instance.check("kling").allowed is True
    assert instance.check("fal").allowed is False
    instance.clear()
    assert instance.check("fal").allowed is True


def test_the_submit_error_table_is_the_frozen_mapping():
    assert SUBMIT_ERROR_CODES == {
        "PROVIDER_AUTH_FAILED",
        "PROVIDER_ACCESS_DENIED",
        "PROVIDER_MODEL_UNAVAILABLE",
        "PROVIDER_RATE_LIMITED",
        "PROVIDER_BILLING_BLOCKED",
        "PROVIDER_CONTENT_REJECTED",
        "PROVIDER_INVALID_REQUEST",
        "SUBMISSION_UNKNOWN",
    }
    assert len(SUBMIT_ERROR_TABLE) == 9
    for row in SUBMIT_ERROR_TABLE:
        if row["code"] is None:
            assert row["outcome"] == "accepted" and row["jobStatus"] == "queued"
            continue
        assert row["outcome"] in {"not_accepted", "unknown"}
        assert row["jobStatus"] == ("failed" if row["outcome"] == "not_accepted" else "unknown")
        # No automatic resubmission exists, so a cooldown never implies a retry.
        assert row["cooldown"] is (row["code"] == "PROVIDER_RATE_LIMITED")
    assert "PROVIDER_RATE_LIMITED" in {item["code"] for item in SUBMIT_ERROR_TABLE if item["cooldown"]}


def test_one_gate_and_one_metric_table_are_shared_per_store():
    class Store:
        pass

    store = Store()
    clock = Clock()
    first = gate_for(store, clock=clock)
    assert gate_for(store, clock=clock) is first
    metrics = metrics_for(store)
    assert metrics_for(store) is metrics
    assert gate_for(Store(), clock=clock) is not first


def test_latency_percentiles_use_nearest_rank_over_a_bounded_window():
    window = LatencyWindow(window=4)
    for value in (10, 20, 30, 40):
        window.add(value)
    summary = window.summary()
    assert summary == {"samples": 4, "window": 4, "p50": 20.0, "p95": 40.0}
    window.add(50)
    assert window.summary()["samples"] == 4 and window.summary()["p95"] == 50.0
    window.add(-1)
    window.add(float("nan"))
    assert window.summary()["samples"] == 4
    empty = LatencyWindow().summary()
    assert empty == {"samples": 0, "window": 64, "p50": 0.0, "p95": 0.0}


def test_runtime_metrics_are_labelled_not_durable_and_reset_cleanly():
    metrics = RuntimeMetrics()
    metrics.record_submit_latency(0.25)
    metrics.record_query_latency(0.5)
    metrics.record_backoff("kling")
    metrics.record_rate_limit_hit("kling")
    snapshot = metrics.snapshot()
    assert snapshot["durable"] is False
    assert snapshot["submitLatencyMs"]["samples"] == 1
    assert snapshot["submitLatencyMs"]["p50"] == 250.0
    assert snapshot["queryLatencyMs"]["p95"] == 500.0
    assert snapshot["backoffCount"] == 1 and snapshot["backoffByProvider"]["kling"] == 1
    assert snapshot["rateLimitHits"] == 1 and snapshot["rateLimitByProvider"]["kling"] == 1
    assert "重启归零" in snapshot["note"]

    metrics.reset()
    cleared = metrics.snapshot()
    assert cleared["backoffCount"] == 0 and cleared["rateLimitHits"] == 0
    assert cleared["submitLatencyMs"]["samples"] == 0
