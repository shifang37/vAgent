"""M8 E2: per-provider submit timing, 429 cooldown and in-memory runtime metrics.

This module is the *only* place that decides "may this Job start its one submit
now?". It answers with a diagnostic, never with a retry: a blocked submit keeps the
Job in ``pending_submit`` with ``submitAttempts=0`` and writes one
``PROVIDER_BACKOFF`` ``RuntimeBlock``. M1-C's "at most one submit per Job, unknown is
never resubmitted" stays byte-for-byte intact — throttling is scheduling, not retry.

Everything here is memory state per Store (like the existing 1.0-second global call
interval): a restart only forgets "wait a little longer", never "you may resubmit".
The frozen numbers come from docs/M8_CONTRACTS.md §5.4; local settings can only
extend them, never shorten them.

Design boundary: new module only. The nine hashed contract modules stay byte
identical, so M8 adds behaviour without a schema change.
"""

from __future__ import annotations

import math
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from weakref import WeakKeyDictionary

PROVIDERS = ("wan", "kling", "fal")

# Frozen defaults (§5.4). Local settings clamp into the same closed ranges.
SUBMIT_INTERVAL_SECONDS = 2.0
SUBMIT_INTERVAL_BOUNDS = (2.0, 60.0)
RATE_LIMIT_COOLDOWN_SECONDS = 60.0
COOLDOWN_BOUNDS = (60.0, 900.0)
RETRY_AFTER_CAP_SECONDS = 900.0
GLOBAL_CALL_INTERVAL_SECONDS = 1.0
BACKOFF_CODE = "PROVIDER_BACKOFF"
LATENCY_WINDOW = 64


def _submit_row(code, outcome, job_status, *, cooldown=False) -> dict:
    """One row of the frozen §5.1 submit mapping."""
    return {"code": code, "outcome": outcome, "jobStatus": job_status, "cooldown": cooldown}


# The frozen submit mapping of §5.1, as data: 2xx+confirmed ID is the only accepted
# outcome; 401/403/404/400/422 and the Kling business codes are proven rejections that
# end the Job; 429 (Kling 1302/1303) is the one proven limitation that starts a
# cooldown; everything else is SUBMISSION_UNKNOWN and is never resubmitted.
# Adapter behaviour is compared against this table so the "unified error table" cannot
# drift from the code that implements it.
SUBMIT_ERROR_TABLE = (
    _submit_row(None, "accepted", "queued"),
    _submit_row("PROVIDER_AUTH_FAILED", "not_accepted", "failed"),
    _submit_row("PROVIDER_ACCESS_DENIED", "not_accepted", "failed"),
    _submit_row("PROVIDER_MODEL_UNAVAILABLE", "not_accepted", "failed"),
    _submit_row("PROVIDER_RATE_LIMITED", "not_accepted", "failed", cooldown=True),
    _submit_row("PROVIDER_BILLING_BLOCKED", "not_accepted", "failed"),
    _submit_row("PROVIDER_CONTENT_REJECTED", "not_accepted", "failed"),
    _submit_row("PROVIDER_INVALID_REQUEST", "not_accepted", "failed"),
    _submit_row("SUBMISSION_UNKNOWN", "unknown", "unknown"),
)

SUBMIT_ERROR_CODES = frozenset(item["code"] for item in SUBMIT_ERROR_TABLE if item["code"])
COOLDOWN_CODES = frozenset(item["code"] for item in SUBMIT_ERROR_TABLE if item["cooldown"])

QUERY_ERROR_TABLE = (
    {"statuses": (401,), "code": "PROVIDER_AUTH_FAILED", "pause": "configuration"},
    {"statuses": (403,), "code": "PROVIDER_ACCESS_DENIED", "pause": "configuration"},
    {"statuses": (404,), "code": "PROVIDER_TASK_UNAVAILABLE", "pause": "task_unavailable"},
    {"statuses": (429,), "code": "PROVIDER_RATE_LIMITED", "pause": None},
    {"statuses": (500, 502, 503, 504), "code": "QUERY_HTTP_ERROR", "pause": None},
)


def _number(value) -> float | None:
    """Accept only finite numbers; strings are parsed once, never coerced silently."""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return float(value)
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, str):
        try:
            parsed = float(value.strip())
        except ValueError:
            return None
        return parsed if math.isfinite(parsed) else None
    return None


def bounded_seconds(value, *, low: float, high: float, default: float) -> float:
    """Clamp one setting into its frozen range: local settings may only extend."""
    number = _number(value)
    if number is None:
        return default
    return min(max(number, low), high)


def submit_interval_seconds(config=None) -> float:
    """The effective per-provider minimum submit interval (§5.4, 2.0–60.0)."""
    return bounded_seconds(
        getattr(config, "video_submit_interval_seconds", None),
        low=SUBMIT_INTERVAL_BOUNDS[0],
        high=SUBMIT_INTERVAL_BOUNDS[1],
        default=SUBMIT_INTERVAL_SECONDS,
    )


def cooldown_seconds(config=None) -> float:
    """The effective per-provider 429 cooldown (§5.4, 60–900)."""
    return bounded_seconds(
        getattr(config, "video_rate_limit_cooldown_seconds", None),
        low=COOLDOWN_BOUNDS[0],
        high=COOLDOWN_BOUNDS[1],
        default=RATE_LIMIT_COOLDOWN_SECONDS,
    )


def _timestamp(value: datetime) -> str:
    return value.astimezone(UTC).isoformat()


def _format_seconds(value: float) -> str:
    return f"{value:g}"


@dataclass(frozen=True)
class GateDecision:
    """One scheduling answer: allowed, or blocked with the earliest allowed time."""

    provider: str
    allowed: bool
    reason: str | None = None
    retry_at: str | None = None
    remaining_seconds: float | None = None
    truncated: bool = False
    message: str | None = None

    def as_view(self) -> dict:
        return {
            "provider": self.provider,
            "allowed": self.allowed,
            "reason": self.reason,
            "retryAt": self.retry_at,
            "remainingSeconds": self.remaining_seconds,
            "truncated": self.truncated,
        }


@dataclass
class _ProviderState:
    last_submit: datetime | None = None
    cooldown_until: datetime | None = None
    retry_after_truncated: bool = False


class SubmitGate:
    """Per-provider submit interval plus 429 cooldown, in memory only.

    ``check`` is a pure read of the injected clock; ``record_submit`` marks the
    moment a request is actually started, and ``record_rate_limit`` opens (or
    extends) a cooldown that a later check will honour. No method sends anything.
    """

    def __init__(
        self,
        *,
        clock: Callable[[], datetime] | None = None,
        interval_seconds: float = SUBMIT_INTERVAL_SECONDS,
        cooldown_seconds: float = RATE_LIMIT_COOLDOWN_SECONDS,
    ):
        self.clock = clock or (lambda: datetime.now(UTC))
        self.interval_seconds = SUBMIT_INTERVAL_SECONDS
        self.cooldown_seconds = RATE_LIMIT_COOLDOWN_SECONDS
        self.configure(interval_seconds=interval_seconds, cooldown_seconds=cooldown_seconds)
        self._state = {provider: _ProviderState() for provider in PROVIDERS}

    def configure(self, *, interval_seconds=None, cooldown_seconds=None) -> None:
        """Apply hot settings; both stay inside the frozen ranges (§5.4)."""
        if interval_seconds is not None:
            self.interval_seconds = bounded_seconds(
                interval_seconds,
                low=SUBMIT_INTERVAL_BOUNDS[0],
                high=SUBMIT_INTERVAL_BOUNDS[1],
                default=SUBMIT_INTERVAL_SECONDS,
            )
        if cooldown_seconds is not None:
            self.cooldown_seconds = bounded_seconds(
                cooldown_seconds,
                low=COOLDOWN_BOUNDS[0],
                high=COOLDOWN_BOUNDS[1],
                default=RATE_LIMIT_COOLDOWN_SECONDS,
            )

    def state(self, provider: str) -> _ProviderState:
        if provider not in self._state:
            raise KeyError(provider)
        return self._state[provider]

    def check(self, provider: str) -> GateDecision:
        """May this provider start one submit now? Read-only over the injected clock."""
        now = self.clock()
        state = self.state(provider)
        if state.cooldown_until is not None and now < state.cooldown_until:
            remaining = (state.cooldown_until - now).total_seconds()
            return GateDecision(
                provider=provider,
                allowed=False,
                reason="cooldown",
                retry_at=_timestamp(state.cooldown_until),
                remaining_seconds=round(remaining, 3),
                truncated=state.retry_after_truncated,
                message=self._blocked_message(provider, "cooldown", state.cooldown_until),
            )
        if state.last_submit is not None:
            earliest = state.last_submit + timedelta(seconds=self.interval_seconds)
            if now < earliest:
                remaining = (earliest - now).total_seconds()
                return GateDecision(
                    provider=provider,
                    allowed=False,
                    reason="interval",
                    retry_at=_timestamp(earliest),
                    remaining_seconds=round(remaining, 3),
                    message=self._blocked_message(provider, "interval", earliest),
                )
        return GateDecision(provider=provider, allowed=True)

    def _blocked_message(self, provider: str, reason: str, when: datetime) -> str:
        """The durable diagnostic: provider, reason, earliest time and configured window.

        The remaining seconds are reported on the decision (for a live view), not in
        the message: the Worker deduplicates ``runtimeBlock`` by
        ``stage/code/message``, so a ticking countdown would rewrite ``state.json`` on
        every idle turn of a long cooldown.
        """
        if reason == "cooldown":
            text = (
                f"{provider} 正在按已证明的限流冷却：最早可在 {_timestamp(when)} 之后提交"
                f"（冷却 {_format_seconds(self.cooldown_seconds)} 秒）。"
            )
            if self.state(provider).retry_after_truncated:
                text += (
                    f"上游 Retry-After 超过 {_format_seconds(RETRY_AFTER_CAP_SECONDS)} 秒上限，已按上限截断。"
                )
            return text
        return (
            f"{provider} 的提交最小间隔尚未到期：最早可在 {_timestamp(when)} 之后提交"
            f"（间隔 {_format_seconds(self.interval_seconds)} 秒）。"
        )

    def record_submit(self, provider: str) -> None:
        """Mark the moment a submission is actually started (never a dry run)."""
        self.state(provider).last_submit = self.clock()

    def record_rate_limit(self, provider: str, retry_after_at: str | None = None) -> GateDecision:
        """Open or extend a cooldown after a proven 429.

        ``Retry-After`` may only extend the window and is capped at 900 seconds; an
        existing cooldown is never shortened by a shorter new one.
        """
        now = self.clock()
        state = self.state(provider)
        until = now + timedelta(seconds=self.cooldown_seconds)
        truncated = False
        if isinstance(retry_after_at, str):
            try:
                parsed = datetime.fromisoformat(retry_after_at)
            except ValueError:
                parsed = None
            if parsed is not None and parsed.utcoffset() is not None:
                cap = now + timedelta(seconds=RETRY_AFTER_CAP_SECONDS)
                if parsed > cap:
                    parsed, truncated = cap, True
                if parsed > until:
                    until = parsed
        if state.cooldown_until is not None and state.cooldown_until > until:
            until = state.cooldown_until
        state.cooldown_until = until
        state.retry_after_truncated = truncated if truncated else state.retry_after_truncated
        return GateDecision(
            provider=provider,
            allowed=False,
            reason="cooldown",
            retry_at=_timestamp(until),
            remaining_seconds=round((until - now).total_seconds(), 3),
            truncated=state.retry_after_truncated,
            message=self._blocked_message(provider, "cooldown", until),
        )

    def clear(self, provider: str | None = None) -> None:
        """Forget timing state (tests and explicit resets); never touches Jobs."""
        for name in PROVIDERS if provider is None else (provider,):
            self._state[name] = _ProviderState()


@dataclass
class LatencyWindow:
    """Nearest-rank percentiles over the last ``window`` samples (memory only)."""

    window: int = LATENCY_WINDOW
    samples: deque = field(default_factory=deque)

    def __post_init__(self):
        self.samples = deque(maxlen=self.window)

    def add(self, milliseconds: float) -> None:
        if math.isfinite(milliseconds) and milliseconds >= 0:
            self.samples.append(milliseconds)

    @staticmethod
    def _percentile(ordered: list[float], fraction: float) -> float:
        if not ordered:
            return 0.0
        rank = max(1, math.ceil(fraction * len(ordered)))
        return ordered[min(rank, len(ordered)) - 1]

    def summary(self) -> dict:
        ordered = sorted(self.samples)
        return {
            "samples": len(ordered),
            "window": self.window,
            "p50": round(self._percentile(ordered, 0.5), 1),
            "p95": round(self._percentile(ordered, 0.95), 1),
        }


class RuntimeMetrics:
    """Per-Store in-memory counters: latencies, backoffs and proven rate limits.

    These numbers never enter the billing ledger or an acceptance conclusion: they
    are labelled ``durable: false`` and reset on restart (§5.6).
    """

    durable = False

    def __init__(self, *, window: int = LATENCY_WINDOW):
        self.submit_latency = LatencyWindow(window=window)
        self.query_latency = LatencyWindow(window=window)
        self.backoff_count = 0
        self.rate_limit_hits = 0
        self.backoff_by_provider = {provider: 0 for provider in PROVIDERS}
        self.rate_limit_by_provider = {provider: 0 for provider in PROVIDERS}

    @staticmethod
    def _bump(table: dict[str, int], provider: str) -> None:
        table[provider] = table.get(provider, 0) + 1

    def record_submit_latency(self, seconds: float) -> None:
        self.submit_latency.add(max(0.0, seconds) * 1000.0)

    def record_query_latency(self, seconds: float) -> None:
        self.query_latency.add(max(0.0, seconds) * 1000.0)

    def record_backoff(self, provider: str) -> None:
        self.backoff_count += 1
        self._bump(self.backoff_by_provider, provider)

    def record_rate_limit_hit(self, provider: str) -> None:
        self.rate_limit_hits += 1
        self._bump(self.rate_limit_by_provider, provider)

    def reset(self) -> None:
        """Forget every in-memory sample and counter of this Store."""
        self.submit_latency = LatencyWindow(window=self.submit_latency.window)
        self.query_latency = LatencyWindow(window=self.query_latency.window)
        self.backoff_count = 0
        self.rate_limit_hits = 0
        self.backoff_by_provider = {provider: 0 for provider in PROVIDERS}
        self.rate_limit_by_provider = {provider: 0 for provider in PROVIDERS}

    def snapshot(self) -> dict:
        return {
            "durable": False,
            "window": self.submit_latency.window,
            "submitLatencyMs": self.submit_latency.summary(),
            "queryLatencyMs": self.query_latency.summary(),
            "backoffCount": self.backoff_count,
            "backoffByProvider": dict(self.backoff_by_provider),
            "rateLimitHits": self.rate_limit_hits,
            "rateLimitByProvider": dict(self.rate_limit_by_provider),
            "note": "进程内计数，重启归零；不进入账单，也不作为验收结论。",
        }


# One gate and one metrics table per Store, shared by every Worker of this process.
_GATES: WeakKeyDictionary = WeakKeyDictionary()
_METRICS: WeakKeyDictionary = WeakKeyDictionary()


def gate_for(store, *, clock: Callable[[], datetime] | None = None, config=None) -> SubmitGate:
    """The single gate of one Store (created on first use, then reused)."""
    gate = _GATES.get(store)
    if gate is None:
        gate = SubmitGate(
            clock=clock,
            interval_seconds=submit_interval_seconds(config),
            cooldown_seconds=cooldown_seconds(config),
        )
        _GATES[store] = gate
    else:
        gate.configure(
            interval_seconds=submit_interval_seconds(config),
            cooldown_seconds=cooldown_seconds(config),
        )
    return gate


def metrics_for(store) -> RuntimeMetrics:
    """The single metrics table of one Store (created on first use, then reused)."""
    metrics = _METRICS.get(store)
    if metrics is None:
        metrics = RuntimeMetrics()
        _METRICS[store] = metrics
    return metrics


__all__ = [
    "BACKOFF_CODE",
    "COOLDOWN_BOUNDS",
    "COOLDOWN_CODES",
    "GLOBAL_CALL_INTERVAL_SECONDS",
    "GateDecision",
    "LATENCY_WINDOW",
    "PROVIDERS",
    "QUERY_ERROR_TABLE",
    "RATE_LIMIT_COOLDOWN_SECONDS",
    "RETRY_AFTER_CAP_SECONDS",
    "RuntimeMetrics",
    "SUBMIT_ERROR_CODES",
    "SUBMIT_ERROR_TABLE",
    "SUBMIT_INTERVAL_BOUNDS",
    "SUBMIT_INTERVAL_SECONDS",
    "SubmitGate",
    "bounded_seconds",
    "cooldown_seconds",
    "gate_for",
    "metrics_for",
    "submit_interval_seconds",
]
