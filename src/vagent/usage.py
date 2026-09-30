"""Normalize reported token usage and summarize only observed cache statistics."""

from langchain_core.messages import AIMessage


def token_count(value: object) -> int | None:
    return value if type(value) is int and value >= 0 else None


def extract_usage(message: AIMessage) -> dict:
    metadata = message.usage_metadata or {}
    raw = message.response_metadata.get("token_usage")
    # OpenAI-compatible adapters can synthesize zero counts for absent raw fields.
    # Prefer the original fields when available so missing usage stays unknown.
    if isinstance(raw, dict):
        input_tokens = token_count(raw.get("prompt_tokens"))
        output_tokens = token_count(raw.get("completion_tokens"))
        details = raw.get("prompt_tokens_details") or {}
        if not isinstance(details, dict):
            details = {}
        hit_value = raw.get("prompt_cache_hit_tokens", details.get("cached_tokens"))
        miss_value = raw.get("prompt_cache_miss_tokens")
    else:
        input_tokens = token_count(metadata.get("input_tokens"))
        output_tokens = token_count(metadata.get("output_tokens"))
        hit_value = (metadata.get("input_token_details") or {}).get("cache_read")
        miss_value = None
    hit, miss = token_count(hit_value), token_count(miss_value)
    source = "reported" if hit is not None or miss is not None else "missing"
    invalid = (hit_value is not None and hit is None) or (miss_value is not None and miss is None)
    if not invalid and input_tokens is not None:
        if hit is not None and miss is None:
            miss, source = input_tokens - hit, "derived"
        elif miss is not None and hit is None:
            hit, source = input_tokens - miss, "derived"
        if hit is not None and miss is not None:
            invalid = min(hit, miss) < 0 or hit + miss != input_tokens
    if invalid:
        hit, miss, source = None, None, "invalid"
    return {
        "inputTokens": input_tokens,
        "outputTokens": output_tokens,
        "cacheHitTokens": hit,
        "cacheMissTokens": miss,
        "cacheUsageSource": source,
    }


def summarize_usage(run: dict) -> dict:
    calls = run.get("modelCalls", [])
    start = run.get("usageStartStep")
    untracked = min(run["modelSteps"], start - 1) if start else run["modelSteps"]
    token_calls = [c for c in calls if c.get("inputTokens") is not None and c.get("outputTokens") is not None]
    cache_calls = [
        c for c in calls if c.get("cacheHitTokens") is not None and c.get("cacheMissTokens") is not None
    ]
    known_hits = [c["cacheHitTokens"] for c in calls if c.get("cacheHitTokens") is not None]
    known_misses = [c["cacheMissTokens"] for c in calls if c.get("cacheMissTokens") is not None]
    paired_hits = sum(c["cacheHitTokens"] for c in cache_calls)
    denominator = paired_hits + sum(c["cacheMissTokens"] for c in cache_calls)
    return {
        "modelCallCount": len(calls) if untracked == 0 else None,
        "recordedCallCount": len(calls),
        "untrackedModelSteps": untracked,
        "callsWithTokenUsage": len(token_calls),
        "callsWithCacheUsage": len(cache_calls),
        "observedInputTokens": run["inputTokens"],
        "observedOutputTokens": run["outputTokens"],
        "cacheHitTokens": sum(known_hits) if known_hits else None,
        "cacheMissTokens": sum(known_misses) if known_misses else None,
        "cacheHitRate": paired_hits / denominator if denominator else None,
        "tokenUsageComplete": untracked == 0 and len(token_calls) == len(calls),
        "cacheUsageComplete": untracked == 0 and len(cache_calls) == len(calls),
    }
