from __future__ import annotations


def sanitize_usage(usage):
    """Persist token telemetry only; provider-specific debug strings may contain prompts."""
    if not isinstance(usage, dict):
        return None
    sanitized = {
        key: usage[key]
        for key in ("prompt_tokens", "completion_tokens", "total_tokens")
        if type(usage.get(key)) is int and usage[key] >= 0
    }
    for section, allowed in {
        "prompt_tokens_details": ("cached_tokens", "audio_tokens"),
        "completion_tokens_details": (
            "reasoning_tokens",
            "audio_tokens",
            "accepted_prediction_tokens",
            "rejected_prediction_tokens",
        ),
    }.items():
        details = usage.get(section)
        if isinstance(details, dict):
            values = {key: details[key] for key in allowed if type(details.get(key)) is int and details[key] >= 0}
            if values:
                sanitized[section] = values
    return sanitized or None


def account(provider, model, usage, usage_source, http_pricing):
    usage = sanitize_usage(usage)
    if usage is None:
        usage_source = "unknown"
    rates = (
        {"input": 1.0, "output": 2.0, "cached_input": 0.1, "version": "mock-hypothetical-v1"}
        if provider == "mock"
        else http_pricing.get(model)
    )
    estimate = None
    input_tokens = usage.get("prompt_tokens") if usage else None
    output_tokens = usage.get("completion_tokens") if usage else None
    details = usage.get("prompt_tokens_details", {}) if usage else {}
    cached = details.get("cached_tokens") if isinstance(details, dict) else None
    if type(cached) is not int or cached < 0 or (input_tokens is not None and cached > input_tokens):
        cached = None
    # Missing cache usage is unknown. A full-input rate estimate is an upper bound only.
    if rates and input_tokens is not None and output_tokens is not None and "input" in rates and "output" in rates:
        if cached is not None and cached > 0 and "cached_input" not in rates:
            estimate = None
        else:
            estimate = (
                (input_tokens - (cached or 0)) * rates["input"]
                + (cached or 0) * rates.get("cached_input", 0)
                + output_tokens * rates["output"]
            ) / 1_000_000
    return {
        "provider": provider,
        "model": model,
        "usage_source": usage_source,
        "usage": usage,
        "uncached_input_tokens": input_tokens - cached if input_tokens is not None and cached is not None else None,
        "discounted_cached_input_tokens": cached,
        "output_tokens": output_tokens,
        "pricing_version": rates["version"] if rates else None,
        "rate_card_estimate_usd": estimate,
        "estimate_basis": "hypothetical_mock_rates" if provider == "mock" else "configured_rates",
        "cache_discount_observed": cached is not None,
        "uncached_rate_upper_bound": provider != "mock" and cached is None and estimate is not None,
        "invoice_reconciled_usd": None,
        "tool_cost_usd": None,
        "retries": 0,
    }
