"""Local cost estimation for usage-statistics windows."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from coworker.core.config import ModelPriceSpec

from .buckets import int_value, norm_part, split_provider_model_key

PRICE_TOKEN_UNIT = Decimal(1_000_000)

type PricingCatalog = dict[tuple[str, str], ModelPriceSpec]


def calculate_model_cost(
    bucket: dict[str, Any],
    provider: str,
    model: str,
    pricing: PricingCatalog,
) -> tuple[str | None, Decimal | None, int]:
    input_tokens = int_value(bucket.get("input_tokens"))
    output_tokens = int_value(bucket.get("output_tokens"))
    total_tokens = input_tokens + output_tokens
    price = pricing.get((provider, model))
    if price is None:
        return None, None, total_tokens
    cached_tokens = min(input_tokens, int_value(bucket.get("cached_tokens")))
    uncached_input_tokens = input_tokens - cached_tokens
    cached_rate = (
        price.cached_input_per_million
        if price.cached_input_per_million is not None
        else price.input_per_million
    )
    cost = (
        Decimal(uncached_input_tokens) * Decimal(str(price.input_per_million))
        + Decimal(cached_tokens) * Decimal(str(cached_rate))
        + Decimal(output_tokens) * Decimal(str(price.output_per_million))
    ) / PRICE_TOKEN_UNIT
    return price.currency, cost, total_tokens


def pricing_summary(
    bucket: dict[str, Any],
    pricing: PricingCatalog,
) -> dict[str, Any]:
    estimated_costs: dict[str, Decimal] = {}
    priced_tokens = 0
    unpriced_tokens = 0
    provider_model_buckets = bucket.get("by_provider_model", {})
    if isinstance(provider_model_buckets, dict):
        for key, provider_model_bucket in provider_model_buckets.items():
            if not isinstance(provider_model_bucket, dict):
                continue
            provider, model = split_provider_model_key(str(key))
            provider = norm_part(provider_model_bucket.get("provider"), provider)
            model = norm_part(provider_model_bucket.get("model"), model)
            currency, cost, total_tokens = calculate_model_cost(
                provider_model_bucket,
                provider,
                model,
                pricing,
            )
            if currency is None or cost is None:
                unpriced_tokens += total_tokens
                continue
            priced_tokens += total_tokens
            estimated_costs[currency] = estimated_costs.get(currency, Decimal(0)) + cost
    total_tokens = int_value(bucket.get("input_tokens")) + int_value(
        bucket.get("output_tokens")
    )
    unpriced_tokens += max(0, total_tokens - priced_tokens - unpriced_tokens)
    return {
        "estimated_costs": {
            currency: float(amount)
            for currency, amount in sorted(estimated_costs.items())
        },
        "priced_tokens": priced_tokens,
        "unpriced_tokens": unpriced_tokens,
        "pricing_coverage": priced_tokens / total_tokens if total_tokens else None,
    }
