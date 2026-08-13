from __future__ import annotations

import json
from collections.abc import Callable
from copy import deepcopy
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

from loguru import logger

from coworker.agent.log_store import LogStore
from coworker.core.config import ModelPriceSpec

_TOKEN_KEYS = ("input_tokens", "output_tokens", "cached_tokens")
_METRIC_KEYS = (
    "llm_calls",
    "tracked_calls",
    "estimated_calls",
    "input_tokens",
    "output_tokens",
    "cached_tokens",
    "tool_calls",
    "tool_successes",
    "tool_errors",
    "skill_load_attempts",
    "skill_load_successes",
    "skill_load_errors",
    "automatic_skill_loads",
    "bubble_runs",
    "bubble_done",
    "bubble_errors",
    "bubble_timeouts",
    "bubble_cancelled",
    "bubble_cycles",
    "bubble_resumes",
    "bubble_max_cycles_reached",
    "thinking_calls",
    "memory_compressions",
    "messages_compressed",
    "memory_compression_duration_ms",
    "memory_compression_summary_calls",
    "memory_compression_summary_tracked_calls",
    "memory_compression_input_tokens",
    "memory_compression_output_tokens",
    "memory_compression_cached_tokens",
)
_SCHEMA_VERSION = 11
_REPORT_DAYS = 30
_INTRADAY_HOURS = 24
_SUMMARY_KEYS = (
    "llm_calls",
    "tracked_calls",
    "exact_calls",
    "untracked_calls",
    "estimated_calls",
    "tracking_coverage",
    "exact_coverage",
    "input_tokens",
    "output_tokens",
    "cached_tokens",
    "total_tokens",
    "avg_tokens_per_call",
    "cache_rate",
    "tool_calls",
    "tool_successes",
    "tool_errors",
    "tool_incomplete",
    "skill_load_attempts",
    "skill_load_successes",
    "skill_load_errors",
    "skill_load_incomplete",
    "automatic_skill_loads",
    "bubble_runs",
    "bubble_done",
    "bubble_errors",
    "bubble_timeouts",
    "bubble_cancelled",
    "bubble_cycles",
    "bubble_elapsed_seconds",
    "bubble_resumes",
    "bubble_max_cycles_reached",
    "thinking_calls",
    "thinking_seconds",
    "avg_thinking_seconds",
    "memory_compressions",
    "messages_compressed",
    "memory_compression_duration_ms",
    "avg_memory_compression_duration_ms",
    "memory_compression_summary_calls",
    "memory_compression_summary_tracked_calls",
    "memory_compression_summary_untracked_calls",
    "memory_compression_summary_tracking_coverage",
    "memory_compression_input_tokens",
    "memory_compression_output_tokens",
    "memory_compression_cached_tokens",
    "memory_compression_total_tokens",
    "memory_compression_triggers",
    "last_memory_compression_at",
)
_COST_SUMMARY_KEYS = (
    "estimated_costs",
    "priced_tokens",
    "unpriced_tokens",
    "pricing_coverage",
)
_PRICE_TOKEN_UNIT = Decimal(1_000_000)
type PricingCatalog = dict[tuple[str, str], ModelPriceSpec]
_MAIN_STREAM_ID = "main"
_MAIN_SCOPE = "main"
_SUMMARY_SCOPE = "summary"
_VISION_SCOPE = "vision"
_BUBBLE_SCOPE = "bubble"
_SUBCONSCIOUS_SCOPE = "subconscious"
_MEM0_SCOPE = "mem0"
_UNKNOWN_SCOPE = "unknown"
_DEFAULT_SCOPES = (
    _MAIN_SCOPE,
    _SUMMARY_SCOPE,
    _VISION_SCOPE,
    _BUBBLE_SCOPE,
    _SUBCONSCIOUS_SCOPE,
    _MEM0_SCOPE,
)
_UNKNOWN_PROVIDER = "unknown"
_UNKNOWN_MODEL = "unknown"
_ADMIN_WINDOW_KEYS = {
    "tool_successes",
    "tool_errors",
    "tool_incomplete",
    "tool_success_rate",
    "skill_load_attempts",
    "skill_load_successes",
    "skill_load_errors",
    "skill_load_incomplete",
    "automatic_skill_loads",
    "bubble_runs",
    "bubble_done",
    "bubble_errors",
    "bubble_timeouts",
    "bubble_cancelled",
    "bubble_cycles",
    "bubble_elapsed_seconds",
    "avg_bubble_cycles",
    "avg_bubble_seconds",
    "bubble_resumes",
    "bubble_max_cycles_reached",
    "tool_outcomes",
    "skills",
    "memory_compressions",
    "messages_compressed",
    "memory_compression_duration_ms",
    "avg_memory_compression_duration_ms",
    "memory_compression_summary_calls",
    "memory_compression_summary_tracked_calls",
    "memory_compression_summary_untracked_calls",
    "memory_compression_summary_tracking_coverage",
    "memory_compression_input_tokens",
    "memory_compression_output_tokens",
    "memory_compression_cached_tokens",
    "memory_compression_total_tokens",
    "memory_compression_triggers",
    "last_memory_compression_at",
}


def _new_model_bucket() -> dict[str, int]:
    return {
        "llm_calls": 0,
        "tracked_calls": 0,
        "estimated_calls": 0,
        "input_tokens": 0,
        "output_tokens": 0,
        "cached_tokens": 0,
    }


def _new_provider_model_bucket(provider: str, model: str) -> dict[str, Any]:
    return {
        "provider": provider,
        "model": model,
        "llm_calls": 0,
        "tracked_calls": 0,
        "estimated_calls": 0,
        "input_tokens": 0,
        "output_tokens": 0,
        "cached_tokens": 0,
    }


def _new_tool_outcome_bucket() -> dict[str, int]:
    return {
        "calls": 0,
        "successes": 0,
        "errors": 0,
    }


def _new_skill_bucket() -> dict[str, int]:
    return {
        "explicit_attempts": 0,
        "explicit_successes": 0,
        "explicit_errors": 0,
        "automatic_loads": 0,
    }


def _new_compression_trigger_bucket() -> dict[str, int]:
    return {
        "automatic": 0,
        "admin": 0,
        "tool": 0,
        "other": 0,
    }


def _new_bucket() -> dict[str, Any]:
    return {
        "llm_calls": 0,
        "tracked_calls": 0,
        "estimated_calls": 0,
        "input_tokens": 0,
        "output_tokens": 0,
        "cached_tokens": 0,
        "tool_calls": 0,
        "tool_successes": 0,
        "tool_errors": 0,
        "skill_load_attempts": 0,
        "skill_load_successes": 0,
        "skill_load_errors": 0,
        "automatic_skill_loads": 0,
        "bubble_runs": 0,
        "bubble_done": 0,
        "bubble_errors": 0,
        "bubble_timeouts": 0,
        "bubble_cancelled": 0,
        "bubble_cycles": 0,
        "bubble_elapsed_seconds": 0.0,
        "bubble_resumes": 0,
        "bubble_max_cycles_reached": 0,
        "thinking_calls": 0,
        "thinking_seconds": 0.0,
        "memory_compressions": 0,
        "messages_compressed": 0,
        "memory_compression_duration_ms": 0,
        "memory_compression_summary_calls": 0,
        "memory_compression_summary_tracked_calls": 0,
        "memory_compression_input_tokens": 0,
        "memory_compression_output_tokens": 0,
        "memory_compression_cached_tokens": 0,
        "memory_compression_triggers": _new_compression_trigger_bucket(),
        "last_memory_compression_at": None,
        "by_model": {},
        "by_provider_model": {},
        "tools": {},
        "tool_outcomes": {},
        "skills": {},
    }


def _new_scope_buckets() -> dict[str, dict[str, Any]]:
    return {scope: _new_bucket() for scope in _DEFAULT_SCOPES}


def _int_value(value: Any) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def _float_value(value: Any) -> float:
    try:
        return max(0.0, float(value or 0.0))
    except (TypeError, ValueError):
        return 0.0


def _norm_part(value: Any, fallback: str) -> str:
    text = str(value or "").strip()
    return text or fallback


def _provider_model_key(provider: str, model: str) -> str:
    return f"{provider}/{model}"


def _split_provider_model_key(key: str) -> tuple[str, str]:
    provider, sep, model = str(key).partition("/")
    if not sep:
        return _UNKNOWN_PROVIDER, _norm_part(provider, _UNKNOWN_MODEL)
    return _norm_part(provider, _UNKNOWN_PROVIDER), _norm_part(model, _UNKNOWN_MODEL)


def _add_usage(
    bucket: dict[str, Any],
    usage: dict[str, Any],
    provider: str,
    model: str,
    usage_source: str = "",
) -> None:
    provider = _norm_part(provider, _UNKNOWN_PROVIDER)
    model = _norm_part(model, _UNKNOWN_MODEL)
    bucket["llm_calls"] += 1
    model_bucket = bucket["by_model"].setdefault(model, _new_model_bucket())
    provider_model_key = _provider_model_key(provider, model)
    provider_model_bucket = bucket["by_provider_model"].setdefault(
        provider_model_key,
        _new_provider_model_bucket(provider, model),
    )
    model_bucket["llm_calls"] += 1
    provider_model_bucket["llm_calls"] += 1
    tracked = any(key in usage and usage.get(key) is not None for key in _TOKEN_KEYS)
    estimated = tracked and usage_source == "estimated"
    if tracked:
        bucket["tracked_calls"] += 1
        model_bucket["tracked_calls"] += 1
        provider_model_bucket["tracked_calls"] += 1
    if estimated:
        bucket["estimated_calls"] += 1
        model_bucket["estimated_calls"] += 1
        provider_model_bucket["estimated_calls"] += 1
    for key in _TOKEN_KEYS:
        value = _int_value(usage.get(key))
        bucket[key] += value
        model_bucket[key] += value
        provider_model_bucket[key] += value


def _add_tool_call(bucket: dict[str, Any], tool_name: str) -> None:
    bucket["tool_calls"] += 1
    bucket["tools"][tool_name] = bucket["tools"].get(tool_name, 0) + 1
    outcome = bucket["tool_outcomes"].setdefault(tool_name, _new_tool_outcome_bucket())
    outcome["calls"] += 1


def _add_tool_result(bucket: dict[str, Any], tool_name: str, is_error: bool) -> None:
    outcome = bucket["tool_outcomes"].setdefault(tool_name, _new_tool_outcome_bucket())
    key = "errors" if is_error else "successes"
    outcome[key] += 1
    bucket[f"tool_{key}"] += 1


def _add_skill_attempt(bucket: dict[str, Any], skill_name: str) -> None:
    bucket["skill_load_attempts"] += 1
    skill = bucket["skills"].setdefault(skill_name, _new_skill_bucket())
    skill["explicit_attempts"] += 1


def _add_skill_result(bucket: dict[str, Any], skill_name: str, is_error: bool) -> None:
    key = "errors" if is_error else "successes"
    bucket[f"skill_load_{key}"] += 1
    skill = bucket["skills"].setdefault(skill_name, _new_skill_bucket())
    skill[f"explicit_{key}"] += 1


def _add_automatic_skill_load(bucket: dict[str, Any], skill_name: str) -> None:
    bucket["automatic_skill_loads"] += 1
    skill = bucket["skills"].setdefault(skill_name, _new_skill_bucket())
    skill["automatic_loads"] += 1


def _add_bubble_outcome(bucket: dict[str, Any], entry: dict[str, Any]) -> None:
    status = str(entry.get("status") or "")
    status_key = {
        "done": "bubble_done",
        "error": "bubble_errors",
        "timeout": "bubble_timeouts",
        "cancelled": "bubble_cancelled",
    }.get(status)
    if status_key is None:
        return
    cycles = _int_value(entry.get("cycles_used"))
    max_cycles = _int_value(entry.get("max_cycles"))
    bucket["bubble_runs"] += 1
    bucket[status_key] += 1
    bucket["bubble_cycles"] += cycles
    bucket["bubble_elapsed_seconds"] += _float_value(entry.get("elapsed_seconds"))
    bucket["bubble_resumes"] += _int_value(entry.get("resume_count"))
    if max_cycles and cycles >= max_cycles:
        bucket["bubble_max_cycles_reached"] += 1


def _add_thinking_duration(bucket: dict[str, Any], seconds: float) -> None:
    bucket["thinking_calls"] += 1
    bucket["thinking_seconds"] += seconds


def _add_memory_compression(
    bucket: dict[str, Any],
    entry: dict[str, Any],
    occurred_at: str,
) -> None:
    bucket["memory_compressions"] += 1
    bucket["messages_compressed"] += _int_value(entry.get("messages_compressed"))
    bucket["memory_compression_duration_ms"] += _int_value(entry.get("duration_ms"))
    bucket["memory_compression_summary_calls"] += _int_value(entry.get("summary_calls"))
    bucket["memory_compression_summary_tracked_calls"] += _int_value(
        entry.get("summary_tracked_calls")
    )
    bucket["memory_compression_input_tokens"] += _int_value(
        entry.get("summary_input_tokens")
    )
    bucket["memory_compression_output_tokens"] += _int_value(
        entry.get("summary_output_tokens")
    )
    bucket["memory_compression_cached_tokens"] += _int_value(
        entry.get("summary_cached_tokens")
    )
    trigger = str(entry.get("trigger") or "other")
    if trigger not in {"automatic", "admin", "tool"}:
        trigger = "other"
    triggers = bucket["memory_compression_triggers"]
    triggers[trigger] = triggers.get(trigger, 0) + 1
    if occurred_at and occurred_at > str(bucket.get("last_memory_compression_at") or ""):
        bucket["last_memory_compression_at"] = occurred_at


def _merge_model_bucket(dst: dict[str, int], src: dict[str, int]) -> None:
    for key in ("llm_calls", "tracked_calls", "estimated_calls", *_TOKEN_KEYS):
        dst[key] = dst.get(key, 0) + _int_value(src.get(key))


def _merge_provider_model_bucket(dst: dict[str, Any], src: dict[str, Any]) -> None:
    dst["provider"] = _norm_part(dst.get("provider") or src.get("provider"), _UNKNOWN_PROVIDER)
    dst["model"] = _norm_part(dst.get("model") or src.get("model"), _UNKNOWN_MODEL)
    for key in ("llm_calls", "tracked_calls", "estimated_calls", *_TOKEN_KEYS):
        dst[key] = dst.get(key, 0) + _int_value(src.get(key))


def _merge_bucket(dst: dict[str, Any], src: dict[str, Any]) -> None:
    for key in _METRIC_KEYS:
        dst[key] += _int_value(src.get(key))
    dst["thinking_seconds"] += _float_value(src.get("thinking_seconds"))
    dst["bubble_elapsed_seconds"] += _float_value(src.get("bubble_elapsed_seconds"))
    for trigger, count in src.get("memory_compression_triggers", {}).items():
        trigger_name = str(trigger)
        if trigger_name not in {"automatic", "admin", "tool"}:
            trigger_name = "other"
        dst["memory_compression_triggers"][trigger_name] += _int_value(count)
    last_compression_at = str(src.get("last_memory_compression_at") or "")
    if last_compression_at > str(dst.get("last_memory_compression_at") or ""):
        dst["last_memory_compression_at"] = last_compression_at
    for model, model_bucket in src.get("by_model", {}).items():
        model = _norm_part(model, _UNKNOWN_MODEL)
        _merge_model_bucket(dst["by_model"].setdefault(model, _new_model_bucket()), model_bucket)
    provider_model_buckets = src.get("by_provider_model", {})
    if isinstance(provider_model_buckets, dict) and provider_model_buckets:
        for key, provider_model_bucket in provider_model_buckets.items():
            provider, model = _split_provider_model_key(str(key))
            if isinstance(provider_model_bucket, dict):
                provider = _norm_part(provider_model_bucket.get("provider"), provider)
                model = _norm_part(provider_model_bucket.get("model"), model)
            provider_model_key = _provider_model_key(provider, model)
            dst_bucket = dst["by_provider_model"].setdefault(
                provider_model_key,
                _new_provider_model_bucket(provider, model),
            )
            if isinstance(provider_model_bucket, dict):
                _merge_provider_model_bucket(dst_bucket, provider_model_bucket)
    else:
        for model, model_bucket in src.get("by_model", {}).items():
            model = _norm_part(model, _UNKNOWN_MODEL)
            provider_model_key = _provider_model_key(_UNKNOWN_PROVIDER, model)
            _merge_provider_model_bucket(
                dst["by_provider_model"].setdefault(
                    provider_model_key,
                    _new_provider_model_bucket(_UNKNOWN_PROVIDER, model),
                ),
                model_bucket,
            )
    for tool, count in src.get("tools", {}).items():
        dst["tools"][tool] = dst["tools"].get(tool, 0) + _int_value(count)
    for tool, outcome in src.get("tool_outcomes", {}).items():
        if not isinstance(outcome, dict):
            continue
        tool_name = _norm_part(tool, "unknown")
        dst_outcome = dst["tool_outcomes"].setdefault(
            tool_name,
            _new_tool_outcome_bucket(),
        )
        for key in ("calls", "successes", "errors"):
            dst_outcome[key] += _int_value(outcome.get(key))
    for skill, item in src.get("skills", {}).items():
        if not isinstance(item, dict):
            continue
        skill_name = _norm_part(skill, "unknown")
        dst_skill = dst["skills"].setdefault(skill_name, _new_skill_bucket())
        for key in (
            "explicit_attempts",
            "explicit_successes",
            "explicit_errors",
            "automatic_loads",
        ):
            dst_skill[key] += _int_value(item.get(key))


def _bucket_has_data(bucket: dict[str, Any]) -> bool:
    return any(_int_value(bucket.get(key)) > 0 for key in _METRIC_KEYS)


def _finalize_model_bucket(bucket: dict[str, int]) -> dict[str, Any]:
    input_tokens = _int_value(bucket.get("input_tokens"))
    output_tokens = _int_value(bucket.get("output_tokens"))
    cached_tokens = _int_value(bucket.get("cached_tokens"))
    llm_calls = _int_value(bucket.get("llm_calls"))
    tracked_calls = min(llm_calls, _int_value(bucket.get("tracked_calls")))
    estimated_calls = min(tracked_calls, _int_value(bucket.get("estimated_calls")))
    exact_calls = max(0, tracked_calls - estimated_calls)
    return {
        "llm_calls": llm_calls,
        "tracked_calls": tracked_calls,
        "exact_calls": exact_calls,
        "untracked_calls": max(0, llm_calls - tracked_calls),
        "estimated_calls": estimated_calls,
        "tracking_coverage": tracked_calls / llm_calls if llm_calls else None,
        "exact_coverage": exact_calls / llm_calls if llm_calls else None,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cached_tokens": cached_tokens,
        "total_tokens": input_tokens + output_tokens,
        "avg_tokens_per_call": (
            (input_tokens + output_tokens) / tracked_calls if tracked_calls else None
        ),
        "cache_rate": cached_tokens / input_tokens if input_tokens else None,
    }


def _calculate_model_cost(
    bucket: dict[str, Any],
    provider: str,
    model: str,
    pricing: PricingCatalog,
) -> tuple[str | None, Decimal | None, int]:
    input_tokens = _int_value(bucket.get("input_tokens"))
    output_tokens = _int_value(bucket.get("output_tokens"))
    total_tokens = input_tokens + output_tokens
    price = pricing.get((provider, model))
    if price is None:
        return None, None, total_tokens
    cached_tokens = min(input_tokens, _int_value(bucket.get("cached_tokens")))
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
    ) / _PRICE_TOKEN_UNIT
    return price.currency, cost, total_tokens


def _pricing_summary(
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
            provider, model = _split_provider_model_key(str(key))
            provider = _norm_part(provider_model_bucket.get("provider"), provider)
            model = _norm_part(provider_model_bucket.get("model"), model)
            currency, cost, total_tokens = _calculate_model_cost(
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
    total_tokens = _int_value(bucket.get("input_tokens")) + _int_value(
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


def _finalize_provider_model_bucket(
    key: str,
    bucket: dict[str, Any],
    pricing: PricingCatalog | None = None,
) -> dict[str, Any]:
    provider, model = _split_provider_model_key(key)
    provider = _norm_part(bucket.get("provider"), provider)
    model = _norm_part(bucket.get("model"), model)
    finalized = _finalize_model_bucket(bucket)
    payload = {
        "provider": provider,
        "model": model,
        **finalized,
    }
    if pricing is not None:
        currency, cost, _ = _calculate_model_cost(bucket, provider, model, pricing)
        payload["currency"] = currency
        payload["estimated_cost"] = float(cost) if cost is not None else None
    return payload


def _finalize_bucket(
    bucket: dict[str, Any],
    pricing: PricingCatalog | None = None,
) -> dict[str, Any]:
    input_tokens = _int_value(bucket.get("input_tokens"))
    output_tokens = _int_value(bucket.get("output_tokens"))
    cached_tokens = _int_value(bucket.get("cached_tokens"))
    thinking_calls = _int_value(bucket.get("thinking_calls"))
    thinking_seconds = _float_value(bucket.get("thinking_seconds"))
    tools = {
        name: count
        for name, count in sorted(
            bucket.get("tools", {}).items(),
            key=lambda item: (-_int_value(item[1]), str(item[0])),
        )
    }
    tool_outcomes = {}
    for name, outcome in sorted(
        bucket.get("tool_outcomes", {}).items(),
        key=lambda item: (
            -_int_value(item[1].get("calls")) if isinstance(item[1], dict) else 0,
            str(item[0]),
        ),
    ):
        if not isinstance(outcome, dict):
            continue
        calls = _int_value(outcome.get("calls"))
        successes = min(calls, _int_value(outcome.get("successes")))
        errors = min(max(0, calls - successes), _int_value(outcome.get("errors")))
        settled = successes + errors
        tool_outcomes[str(name)] = {
            "calls": calls,
            "successes": successes,
            "errors": errors,
            "incomplete": max(0, calls - settled),
            "success_rate": successes / settled if settled else None,
        }
    skills = {}
    for name, item in sorted(
        bucket.get("skills", {}).items(),
        key=lambda pair: (
            -(
                _int_value(pair[1].get("explicit_attempts"))
                + _int_value(pair[1].get("automatic_loads"))
            )
            if isinstance(pair[1], dict)
            else 0,
            str(pair[0]),
        ),
    ):
        if not isinstance(item, dict):
            continue
        attempts = _int_value(item.get("explicit_attempts"))
        successes = min(attempts, _int_value(item.get("explicit_successes")))
        errors = min(max(0, attempts - successes), _int_value(item.get("explicit_errors")))
        skills[str(name)] = {
            "explicit_attempts": attempts,
            "explicit_successes": successes,
            "explicit_errors": errors,
            "explicit_incomplete": max(0, attempts - successes - errors),
            "automatic_loads": _int_value(item.get("automatic_loads")),
        }
    by_model = {
        model: _finalize_model_bucket(model_bucket)
        for model, model_bucket in sorted(bucket.get("by_model", {}).items())
    }
    by_provider_model = {
        key: _finalize_provider_model_bucket(key, provider_model_bucket, pricing)
        for key, provider_model_bucket in sorted(bucket.get("by_provider_model", {}).items())
    }
    llm_calls = _int_value(bucket.get("llm_calls"))
    tracked_calls = min(llm_calls, _int_value(bucket.get("tracked_calls")))
    estimated_calls = min(tracked_calls, _int_value(bucket.get("estimated_calls")))
    exact_calls = max(0, tracked_calls - estimated_calls)
    tool_calls = _int_value(bucket.get("tool_calls"))
    tool_successes = min(tool_calls, _int_value(bucket.get("tool_successes")))
    tool_errors = min(
        max(0, tool_calls - tool_successes),
        _int_value(bucket.get("tool_errors")),
    )
    settled_tool_calls = tool_successes + tool_errors
    skill_load_attempts = _int_value(bucket.get("skill_load_attempts"))
    skill_load_successes = min(
        skill_load_attempts,
        _int_value(bucket.get("skill_load_successes")),
    )
    skill_load_errors = min(
        max(0, skill_load_attempts - skill_load_successes),
        _int_value(bucket.get("skill_load_errors")),
    )
    bubble_runs = _int_value(bucket.get("bubble_runs"))
    bubble_elapsed_seconds = _float_value(bucket.get("bubble_elapsed_seconds"))
    bubble_cycles = _int_value(bucket.get("bubble_cycles"))
    memory_compressions = _int_value(bucket.get("memory_compressions"))
    memory_compression_duration_ms = _int_value(
        bucket.get("memory_compression_duration_ms")
    )
    memory_compression_summary_calls = _int_value(
        bucket.get("memory_compression_summary_calls")
    )
    memory_compression_summary_tracked_calls = min(
        memory_compression_summary_calls,
        _int_value(bucket.get("memory_compression_summary_tracked_calls")),
    )
    memory_compression_input_tokens = _int_value(
        bucket.get("memory_compression_input_tokens")
    )
    memory_compression_output_tokens = _int_value(
        bucket.get("memory_compression_output_tokens")
    )
    compression_triggers = _new_compression_trigger_bucket()
    raw_compression_triggers = bucket.get("memory_compression_triggers", {})
    if isinstance(raw_compression_triggers, dict):
        for trigger, count in raw_compression_triggers.items():
            trigger_name = str(trigger)
            if trigger_name not in compression_triggers:
                trigger_name = "other"
            compression_triggers[trigger_name] += _int_value(count)
    payload = {
        "llm_calls": llm_calls,
        "tracked_calls": tracked_calls,
        "exact_calls": exact_calls,
        "untracked_calls": max(0, llm_calls - tracked_calls),
        "estimated_calls": estimated_calls,
        "tracking_coverage": tracked_calls / llm_calls if llm_calls else None,
        "exact_coverage": exact_calls / llm_calls if llm_calls else None,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cached_tokens": cached_tokens,
        "total_tokens": input_tokens + output_tokens,
        "avg_tokens_per_call": (
            (input_tokens + output_tokens) / tracked_calls if tracked_calls else None
        ),
        "cache_rate": cached_tokens / input_tokens if input_tokens else None,
        "tool_calls": tool_calls,
        "tool_successes": tool_successes,
        "tool_errors": tool_errors,
        "tool_incomplete": max(0, tool_calls - settled_tool_calls),
        "tool_success_rate": (
            tool_successes / settled_tool_calls if settled_tool_calls else None
        ),
        "skill_load_attempts": skill_load_attempts,
        "skill_load_successes": skill_load_successes,
        "skill_load_errors": skill_load_errors,
        "skill_load_incomplete": max(
            0,
            skill_load_attempts - skill_load_successes - skill_load_errors,
        ),
        "automatic_skill_loads": _int_value(bucket.get("automatic_skill_loads")),
        "bubble_runs": bubble_runs,
        "bubble_done": min(bubble_runs, _int_value(bucket.get("bubble_done"))),
        "bubble_errors": min(bubble_runs, _int_value(bucket.get("bubble_errors"))),
        "bubble_timeouts": min(bubble_runs, _int_value(bucket.get("bubble_timeouts"))),
        "bubble_cancelled": min(bubble_runs, _int_value(bucket.get("bubble_cancelled"))),
        "bubble_cycles": bubble_cycles,
        "bubble_elapsed_seconds": round(bubble_elapsed_seconds, 3),
        "avg_bubble_cycles": bubble_cycles / bubble_runs if bubble_runs else None,
        "avg_bubble_seconds": bubble_elapsed_seconds / bubble_runs if bubble_runs else None,
        "bubble_resumes": _int_value(bucket.get("bubble_resumes")),
        "bubble_max_cycles_reached": _int_value(
            bucket.get("bubble_max_cycles_reached")
        ),
        "thinking_calls": thinking_calls,
        "thinking_seconds": round(thinking_seconds, 3),
        "avg_thinking_seconds": (
            thinking_seconds / thinking_calls if thinking_calls else None
        ),
        "memory_compressions": memory_compressions,
        "messages_compressed": _int_value(bucket.get("messages_compressed")),
        "memory_compression_duration_ms": memory_compression_duration_ms,
        "avg_memory_compression_duration_ms": (
            memory_compression_duration_ms / memory_compressions
            if memory_compressions
            else None
        ),
        "memory_compression_summary_calls": memory_compression_summary_calls,
        "memory_compression_summary_tracked_calls": (
            memory_compression_summary_tracked_calls
        ),
        "memory_compression_summary_untracked_calls": max(
            0,
            memory_compression_summary_calls - memory_compression_summary_tracked_calls,
        ),
        "memory_compression_summary_tracking_coverage": (
            memory_compression_summary_tracked_calls / memory_compression_summary_calls
            if memory_compression_summary_calls
            else None
        ),
        "memory_compression_input_tokens": memory_compression_input_tokens,
        "memory_compression_output_tokens": memory_compression_output_tokens,
        "memory_compression_cached_tokens": _int_value(
            bucket.get("memory_compression_cached_tokens")
        ),
        "memory_compression_total_tokens": (
            memory_compression_input_tokens + memory_compression_output_tokens
        ),
        "memory_compression_triggers": compression_triggers,
        "last_memory_compression_at": (
            str(bucket.get("last_memory_compression_at"))
            if bucket.get("last_memory_compression_at")
            else None
        ),
        "by_model": by_model,
        "by_provider_model": by_provider_model,
        "tools": tools,
        "tool_outcomes": tool_outcomes,
        "skills": skills,
    }
    if pricing is not None:
        payload.update(_pricing_summary(bucket, pricing))
    return payload


def _summary_bucket(
    bucket: dict[str, Any],
    pricing: PricingCatalog | None = None,
) -> dict[str, Any]:
    finalized = _finalize_bucket(bucket, pricing)
    keys = _SUMMARY_KEYS + (_COST_SUMMARY_KEYS if pricing is not None else ())
    return {key: finalized[key] for key in keys}


def _summary_scope_buckets(
    scopes: dict[str, dict[str, Any]],
    pricing: PricingCatalog | None = None,
) -> dict[str, dict[str, Any]]:
    payload = {
        scope: _summary_bucket(scopes.get(scope, _new_bucket()), pricing)
        for scope in _DEFAULT_SCOPES
    }
    for scope, bucket in sorted(scopes.items()):
        if scope in payload or not isinstance(bucket, dict):
            continue
        if _bucket_has_data(bucket):
            payload[str(scope)] = _summary_bucket(bucket, pricing)
    return payload


def _summary_window(
    bucket: dict[str, Any],
    scopes: dict[str, dict[str, Any]],
    pricing: PricingCatalog | None = None,
) -> dict[str, Any]:
    return {
        **_summary_bucket(bucket, pricing),
        "by_scope": _summary_scope_buckets(scopes, pricing),
    }


class UsageStatsCollector:
    """Aggregate privacy-safe resource usage and execution statistics."""

    def __init__(
        self,
        log_store: LogStore | None = None,
        now_fn: Callable[[], datetime] = datetime.now,
        state_path: str | Path | None = None,
    ) -> None:
        self._now_fn = now_fn
        self._days: dict[date, dict[str, Any]] = {}
        self._hours: dict[str, dict[str, Any]] = {}
        self._hours_by_scope: dict[str, dict[str, dict[str, Any]]] = {}
        self._lifetime = _new_bucket()
        self._days_by_scope: dict[date, dict[str, dict[str, Any]]] = {}
        self._lifetime_by_scope = _new_scope_buckets()
        self._state_path = Path(state_path) if state_path is not None else None
        self._last_seq_by_stream: dict[str, int] = {}
        self._pending_thinking_starts: dict[str, tuple[datetime, date]] = {}
        self._pending_tool_calls: dict[str, dict[str, str]] = {}
        self._compression_tracking_since: date | None = None
        self._bubble_history_key: tuple[int, str] | None = None
        self._bubble_history_scanned = False
        self._loading_history = False
        self._snapshot_cache_date: date | None = None
        self._snapshot_cache: dict[str, Any] | None = None
        if log_store is not None:
            self.load_history(log_store)

    def load_history(self, log_store: LogStore) -> None:
        loaded_state = self._load_state()
        try:
            self._loading_history = True
            if loaded_state:
                self._load_incremental(log_store)
            else:
                self._load_cold(log_store)
        except Exception as e:
            logger.warning(f"Failed to load usage stats from interaction log: {e}")
            return
        finally:
            self._loading_history = False
        if self._compression_tracking_since is None:
            self._compression_tracking_since = self._now_fn().date()
        self._persist_state()

    def load_entries(self, entries: list[dict[str, Any]]) -> None:
        for entry in entries:
            self.on_entry(entry, persist=False)

    def load_bubble_history(self, logs_dir: str | Path) -> None:
        root = Path(logs_dir)
        if self._bubble_history_scanned:
            self._load_pending_bubble_streams(root)
            self._persist_state()
            return

        paths = [
            *sorted((root / "bubbles").glob("*.jsonl")),
            *sorted((root / "subconscious" / "bubbles").glob("*.jsonl")),
        ]
        if not paths:
            self._bubble_history_scanned = True
            self._persist_state()
            return
        try:
            self._loading_history = True
            processed_keys: list[tuple[int, str]] = []
            processed_streams: list[str] = []
            for path in sorted(paths, key=lambda p: self._bubble_file_key(root, p)):
                stream_id = self.bubble_stream_id(root, path)
                file_key = self._bubble_file_key(root, path)
                if (
                    stream_id not in self._last_seq_by_stream
                    and self._bubble_history_key is not None
                    and file_key <= self._bubble_history_key
                ):
                    continue
                for entry in self._iter_jsonl(path):
                    self.on_entry(entry, persist=False, stream_id=stream_id)
                processed_keys.append(file_key)
                processed_streams.append(stream_id)
            if processed_keys:
                self._advance_bubble_history(max(processed_keys))
                for stream_id in processed_streams:
                    self._last_seq_by_stream.pop(stream_id, None)
                    self._pending_thinking_starts.pop(stream_id, None)
                    self._discard_pending_tool_calls(stream_id)
            self._bubble_history_scanned = True
        except Exception as e:
            logger.warning(f"Failed to load usage stats from bubble logs: {e}")
            return
        finally:
            self._loading_history = False
        self._persist_state()

    def mark_bubble_log_complete(self, logs_dir: str | Path, log_path: str | Path) -> None:
        root = Path(logs_dir)
        path = Path(log_path)
        self._advance_bubble_history(self._bubble_file_key(root, path))
        self._bubble_history_scanned = True
        stream_id = self.bubble_stream_id(root, path)
        self._last_seq_by_stream.pop(stream_id, None)
        self._pending_thinking_starts.pop(stream_id, None)
        self._discard_pending_tool_calls(stream_id)
        self._persist_state()

    def on_entry(
        self,
        entry: dict[str, Any],
        persist: bool = True,
        stream_id: str = _MAIN_STREAM_ID,
    ) -> None:
        if not self._should_process(entry, stream_id):
            return
        self._snapshot_cache = None
        t = entry.get("type")
        if t == "thinking_start":
            self._record_thinking_start(entry, stream_id)
        elif t == "llm_response":
            usage = entry.get("usage")
            if not isinstance(usage, dict):
                usage = {}
            provider = _norm_part(entry.get("provider"), _UNKNOWN_PROVIDER)
            model = _norm_part(entry.get("model"), _UNKNOWN_MODEL)
            self._record_usage(
                self._entry_date(entry),
                self._entry_hour(entry),
                usage,
                provider,
                model,
                stream_id,
                str(entry.get("usage_source") or ""),
            )
            self._record_thinking_finish(entry, stream_id)
        elif t in ("summary_llm_response", "vision_llm_response", "mem0_llm_response"):
            usage = entry.get("usage")
            if not isinstance(usage, dict):
                usage = {}
            provider = _norm_part(entry.get("provider"), _UNKNOWN_PROVIDER)
            model = _norm_part(entry.get("model"), _UNKNOWN_MODEL)
            stream_scope = self._scope_for_stream_id(stream_id)
            if stream_scope in (_BUBBLE_SCOPE, _SUBCONSCIOUS_SCOPE):
                scope = stream_scope
            elif t == "summary_llm_response":
                scope = _SUMMARY_SCOPE
            elif t == "vision_llm_response":
                scope = _VISION_SCOPE
            else:
                scope = _MEM0_SCOPE
            self._record_usage_with_scope(
                self._entry_date(entry),
                self._entry_hour(entry),
                usage,
                provider,
                model,
                scope,
                str(entry.get("usage_source") or ""),
            )
        elif t == "memory_compression":
            self._record_memory_compression(entry, stream_id)
        elif t == "tool_call":
            tool_name = _norm_part(entry.get("name"), "unknown")
            day = self._entry_date(entry)
            self._record_tool_call(day, tool_name, stream_id)
            skill_name = self._skill_name_from_call(entry) if tool_name == "get_skill" else ""
            if tool_name == "get_skill":
                self._record_skill_attempt(day, skill_name, stream_id)
            self._remember_tool_call(entry, day, tool_name, skill_name, stream_id)
        elif t == "tool_result":
            self._record_tool_result(entry, stream_id)
        elif t == "palace_injection":
            critical_skills = entry.get("critical_skills")
            if isinstance(critical_skills, list):
                for skill_name in critical_skills:
                    self._record_automatic_skill_load(
                        self._entry_date(entry),
                        _norm_part(skill_name, "unknown"),
                        stream_id,
                    )
        elif entry.get("__meta__"):
            self._record_bubble_outcome(self._entry_date(entry), entry, stream_id)
        if persist and not self._loading_history:
            self._persist_state()

    def snapshot(self) -> dict[str, Any]:
        today = self._now_fn().date()
        if self._snapshot_cache_date == today and self._snapshot_cache is not None:
            return self._snapshot_cache
        snapshot = self._snapshot_for_date(today, detailed=False)
        self._snapshot_cache_date = today
        self._snapshot_cache = snapshot
        return snapshot

    def _snapshot_for_date(
        self,
        today: date,
        *,
        detailed: bool,
        pricing: PricingCatalog | None = None,
    ) -> dict[str, Any]:
        last_7_start = today - timedelta(days=6)
        today_bucket = deepcopy(self._days.get(today, _new_bucket()))
        today_scopes = deepcopy(self._days_by_scope.get(today, _new_scope_buckets()))
        last_7_bucket = _new_bucket()
        last_7_scopes = _new_scope_buckets()
        for day, bucket in self._days.items():
            if last_7_start <= day <= today:
                _merge_bucket(last_7_bucket, bucket)
        for day, scopes in self._days_by_scope.items():
            if last_7_start <= day <= today:
                self._merge_scope_buckets(last_7_scopes, scopes)
        payload = {
            "today": self._finalize_window(today_bucket, today_scopes, pricing),
            "last_7_days": self._finalize_window(last_7_bucket, last_7_scopes, pricing),
            "lifetime": self._finalize_window(
                deepcopy(self._lifetime),
                deepcopy(self._lifetime_by_scope),
                pricing,
            ),
        }
        if detailed:
            return payload
        return {key: self._compact_window(value) for key, value in payload.items()}

    def report(
        self,
        *,
        start_date: date | None = None,
        end_date: date | None = None,
        model_prices: list[ModelPriceSpec] | None = None,
    ) -> dict[str, Any]:
        """Return the authenticated management report without expanding public status."""
        pricing = (
            None
            if model_prices is None
            else {(price.provider, price.model): price for price in model_prices}
        )
        now = self._now_fn()
        today = now.date()
        last_30_start = today - timedelta(days=_REPORT_DAYS - 1)
        last_30_bucket, last_30_scopes = self._aggregate_range(last_30_start, today)
        previous_ranges = {
            "today": (today - timedelta(days=1), today - timedelta(days=1)),
            "last_7_days": (today - timedelta(days=13), today - timedelta(days=7)),
            "last_30_days": (today - timedelta(days=59), today - timedelta(days=30)),
        }
        tracked_days = [day for day, bucket in self._days.items() if _bucket_has_data(bucket)]
        payload = {
            **self._snapshot_for_date(today, detailed=True, pricing=pricing),
            "last_30_days": self._finalize_window(
                last_30_bucket,
                last_30_scopes,
                pricing,
            ),
            "previous": {
                key: _summary_window(*self._aggregate_range(start, end), pricing)
                for key, (start, end) in previous_ranges.items()
            },
            "daily": [
                {
                    "date": day.isoformat(),
                    **_summary_window(
                        deepcopy(self._days.get(day, _new_bucket())),
                        deepcopy(self._days_by_scope.get(day, _new_scope_buckets())),
                        pricing,
                    ),
                }
                for day in (
                    last_30_start + timedelta(days=offset)
                    for offset in range(_REPORT_DAYS)
                )
            ],
            "today_intraday": self._intraday_report(today, pricing),
            "generated_at": now.isoformat(),
            "tracking_since": min(tracked_days).isoformat() if tracked_days else None,
            "compression_tracking_since": (
                self._compression_tracking_since or today
            ).isoformat(),
        }
        if start_date is not None or end_date is not None:
            selected_start = start_date or end_date
            selected_end = end_date or start_date
            if selected_start is None or selected_end is None:
                raise ValueError("invalid_usage_date_range")
            if selected_start > selected_end:
                raise ValueError("invalid_usage_date_range")
            payload["selected_range"] = self._selected_range_report(
                selected_start,
                selected_end,
                pricing,
            )
        return payload

    def _selected_range_report(
        self,
        start: date,
        end: date,
        pricing: PricingCatalog | None,
    ) -> dict[str, Any]:
        bucket, scopes = self._aggregate_range(start, end)
        span = (end - start).days + 1
        previous_start: date | None = None
        previous_end: date | None = None
        previous: dict[str, Any] | None = None
        try:
            previous_end = start - timedelta(days=1)
            previous_start = previous_end - timedelta(days=span - 1)
        except OverflowError:
            previous_start = None
            previous_end = None
        else:
            previous = _summary_window(
                *self._aggregate_range(previous_start, previous_end),
                pricing,
            )
        report = {
            "start_date": start.isoformat(),
            "end_date": end.isoformat(),
            "previous_start_date": (
                previous_start.isoformat() if previous_start is not None else None
            ),
            "previous_end_date": (
                previous_end.isoformat() if previous_end is not None else None
            ),
            "stats": self._finalize_window(bucket, scopes, pricing),
            "previous": previous,
            "daily": [
                {
                    "date": day.isoformat(),
                    **_summary_window(
                        deepcopy(self._days.get(day, _new_bucket())),
                        deepcopy(self._days_by_scope.get(day, _new_scope_buckets())),
                        pricing,
                    ),
                }
                for day in (
                    start + timedelta(days=offset)
                    for offset in range(span)
                )
            ],
        }
        if start == end:
            report["intraday"] = self._intraday_report(start, pricing)
        return report

    def _intraday_report(
        self,
        day: date,
        pricing: PricingCatalog | None = None,
    ) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for hour in range(_INTRADAY_HOURS):
            hour_key = f"{day.isoformat()}T{hour:02d}:00:00"
            rows.append({
                "start_time": hour_key,
                "end_time": f"{day.isoformat()}T{hour:02d}:59:59.999999",
                **_summary_window(
                    deepcopy(self._hours.get(hour_key, _new_bucket())),
                    deepcopy(
                        self._hours_by_scope.get(hour_key, _new_scope_buckets())
                    ),
                    pricing,
                ),
            })
        return rows

    @classmethod
    def _compact_window(cls, window: dict[str, Any]) -> dict[str, Any]:
        compact = {key: value for key, value in window.items() if key not in _ADMIN_WINDOW_KEYS}
        scopes = window.get("by_scope")
        if isinstance(scopes, dict):
            compact["by_scope"] = {
                str(name): {
                    key: value
                    for key, value in scope.items()
                    if key not in _ADMIN_WINDOW_KEYS
                }
                for name, scope in scopes.items()
                if isinstance(scope, dict)
            }
        return compact

    def _aggregate_range(
        self,
        start: date,
        end: date,
    ) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
        bucket = _new_bucket()
        scopes = _new_scope_buckets()
        for day, day_bucket in self._days.items():
            if start <= day <= end:
                _merge_bucket(bucket, day_bucket)
        for day, day_scopes in self._days_by_scope.items():
            if start <= day <= end:
                self._merge_scope_buckets(scopes, day_scopes)
        return bucket, scopes

    def _record_usage(
        self,
        day: date,
        hour: str,
        usage: dict[str, Any],
        provider: str,
        model: str,
        stream_id: str,
        usage_source: str,
    ) -> None:
        bucket = self._days.setdefault(day, _new_bucket())
        _add_usage(bucket, usage, provider, model, usage_source)
        _add_usage(
            self._hours.setdefault(hour, _new_bucket()),
            usage,
            provider,
            model,
            usage_source,
        )
        _add_usage(self._lifetime, usage, provider, model, usage_source)
        scope = self._scope_for_stream_id(stream_id)
        self._record_usage_for_scope(
            day,
            hour,
            usage,
            provider,
            model,
            scope,
            usage_source,
        )

    def _record_usage_with_scope(
        self,
        day: date,
        hour: str,
        usage: dict[str, Any],
        provider: str,
        model: str,
        scope: str,
        usage_source: str,
    ) -> None:
        bucket = self._days.setdefault(day, _new_bucket())
        _add_usage(bucket, usage, provider, model, usage_source)
        _add_usage(
            self._hours.setdefault(hour, _new_bucket()),
            usage,
            provider,
            model,
            usage_source,
        )
        _add_usage(self._lifetime, usage, provider, model, usage_source)
        self._record_usage_for_scope(
            day,
            hour,
            usage,
            provider,
            model,
            scope,
            usage_source,
        )

    def _record_usage_for_scope(
        self,
        day: date,
        hour: str,
        usage: dict[str, Any],
        provider: str,
        model: str,
        scope: str,
        usage_source: str,
    ) -> None:
        _add_usage(
            self._scope_bucket_for_hour(hour, scope),
            usage,
            provider,
            model,
            usage_source,
        )
        _add_usage(
            self._scope_bucket_for_day(day, scope),
            usage,
            provider,
            model,
            usage_source,
        )
        _add_usage(
            self._scope_bucket_for_lifetime(scope),
            usage,
            provider,
            model,
            usage_source,
        )

    def _record_tool_call(self, day: date, tool_name: str, stream_id: str) -> None:
        bucket = self._days.setdefault(day, _new_bucket())
        _add_tool_call(bucket, tool_name)
        _add_tool_call(self._lifetime, tool_name)
        scope = self._scope_for_stream_id(stream_id)
        _add_tool_call(self._scope_bucket_for_day(day, scope), tool_name)
        _add_tool_call(self._scope_bucket_for_lifetime(scope), tool_name)

    def _record_tool_result(self, entry: dict[str, Any], stream_id: str) -> None:
        call_id = str(entry.get("id") or "")
        if not call_id:
            return
        pending = self._pending_tool_calls.pop(self._pending_tool_key(stream_id, call_id), None)
        if pending is None:
            return
        try:
            day = date.fromisoformat(pending["day"])
        except (KeyError, TypeError, ValueError):
            day = self._entry_date(entry)
        tool_name = _norm_part(pending.get("tool_name"), "unknown")
        is_error = bool(entry.get("is_error"))
        self._add_tool_result_for_day(day, tool_name, stream_id, is_error)
        skill_name = str(pending.get("skill_name") or "")
        if tool_name == "get_skill":
            self._add_skill_result_for_day(day, skill_name, stream_id, is_error)

    def _add_tool_result_for_day(
        self,
        day: date,
        tool_name: str,
        stream_id: str,
        is_error: bool,
    ) -> None:
        _add_tool_result(self._days.setdefault(day, _new_bucket()), tool_name, is_error)
        _add_tool_result(self._lifetime, tool_name, is_error)
        scope = self._scope_for_stream_id(stream_id)
        _add_tool_result(self._scope_bucket_for_day(day, scope), tool_name, is_error)
        _add_tool_result(self._scope_bucket_for_lifetime(scope), tool_name, is_error)

    def _record_skill_attempt(self, day: date, skill_name: str, stream_id: str) -> None:
        _add_skill_attempt(self._days.setdefault(day, _new_bucket()), skill_name)
        _add_skill_attempt(self._lifetime, skill_name)
        scope = self._scope_for_stream_id(stream_id)
        _add_skill_attempt(self._scope_bucket_for_day(day, scope), skill_name)
        _add_skill_attempt(self._scope_bucket_for_lifetime(scope), skill_name)

    def _add_skill_result_for_day(
        self,
        day: date,
        skill_name: str,
        stream_id: str,
        is_error: bool,
    ) -> None:
        _add_skill_result(self._days.setdefault(day, _new_bucket()), skill_name, is_error)
        _add_skill_result(self._lifetime, skill_name, is_error)
        scope = self._scope_for_stream_id(stream_id)
        _add_skill_result(self._scope_bucket_for_day(day, scope), skill_name, is_error)
        _add_skill_result(self._scope_bucket_for_lifetime(scope), skill_name, is_error)

    def _record_automatic_skill_load(
        self,
        day: date,
        skill_name: str,
        stream_id: str,
    ) -> None:
        _add_automatic_skill_load(self._days.setdefault(day, _new_bucket()), skill_name)
        _add_automatic_skill_load(self._lifetime, skill_name)
        scope = self._scope_for_stream_id(stream_id)
        _add_automatic_skill_load(self._scope_bucket_for_day(day, scope), skill_name)
        _add_automatic_skill_load(self._scope_bucket_for_lifetime(scope), skill_name)

    def _record_bubble_outcome(
        self,
        day: date,
        entry: dict[str, Any],
        stream_id: str,
    ) -> None:
        scope = self._scope_for_stream_id(stream_id)
        if scope not in (_BUBBLE_SCOPE, _SUBCONSCIOUS_SCOPE):
            return
        _add_bubble_outcome(self._days.setdefault(day, _new_bucket()), entry)
        _add_bubble_outcome(self._lifetime, entry)
        _add_bubble_outcome(self._scope_bucket_for_day(day, scope), entry)
        _add_bubble_outcome(self._scope_bucket_for_lifetime(scope), entry)

    def _record_memory_compression(
        self,
        entry: dict[str, Any],
        stream_id: str,
    ) -> None:
        day = self._entry_date(entry)
        hour = self._entry_hour(entry)
        occurred_at_value = self._entry_datetime(entry)
        occurred_at = occurred_at_value.isoformat() if occurred_at_value is not None else ""
        _add_memory_compression(self._days.setdefault(day, _new_bucket()), entry, occurred_at)
        _add_memory_compression(self._hours.setdefault(hour, _new_bucket()), entry, occurred_at)
        _add_memory_compression(self._lifetime, entry, occurred_at)
        scope = self._scope_for_stream_id(stream_id)
        _add_memory_compression(
            self._scope_bucket_for_hour(hour, scope),
            entry,
            occurred_at,
        )
        _add_memory_compression(self._scope_bucket_for_day(day, scope), entry, occurred_at)
        _add_memory_compression(self._scope_bucket_for_lifetime(scope), entry, occurred_at)
        if self._compression_tracking_since is None or day < self._compression_tracking_since:
            self._compression_tracking_since = day

    def _remember_tool_call(
        self,
        entry: dict[str, Any],
        day: date,
        tool_name: str,
        skill_name: str,
        stream_id: str,
    ) -> None:
        call_id = str(entry.get("id") or "")
        if not call_id:
            return
        self._pending_tool_calls[self._pending_tool_key(stream_id, call_id)] = {
            "stream_id": stream_id,
            "day": day.isoformat(),
            "tool_name": tool_name,
            "skill_name": skill_name,
        }

    @staticmethod
    def _pending_tool_key(stream_id: str, call_id: str) -> str:
        return f"{stream_id}\u001f{call_id}"

    @staticmethod
    def _skill_name_from_call(entry: dict[str, Any]) -> str:
        arguments = entry.get("arguments")
        if not isinstance(arguments, dict):
            return "unknown"
        return _norm_part(arguments.get("skill_name"), "unknown")

    def _record_thinking_start(self, entry: dict[str, Any], stream_id: str) -> None:
        started_at = self._entry_datetime(entry)
        if started_at is None:
            return
        self._pending_thinking_starts[stream_id] = (started_at, self._entry_date(entry))

    def _record_thinking_finish(self, entry: dict[str, Any], stream_id: str) -> None:
        pending = self._pending_thinking_starts.pop(stream_id, None)
        if pending is None:
            return
        started_at, _started_day = pending
        finished_at = self._entry_datetime(entry)
        if finished_at is None:
            return
        try:
            seconds = (finished_at - started_at).total_seconds()
        except TypeError:
            return
        if seconds < 0:
            return
        self._record_thinking_duration(self._entry_date(entry), seconds, stream_id)

    def _record_thinking_duration(self, day: date, seconds: float, stream_id: str) -> None:
        bucket = self._days.setdefault(day, _new_bucket())
        _add_thinking_duration(bucket, seconds)
        _add_thinking_duration(self._lifetime, seconds)
        scope = self._scope_for_stream_id(stream_id)
        _add_thinking_duration(self._scope_bucket_for_day(day, scope), seconds)
        _add_thinking_duration(self._scope_bucket_for_lifetime(scope), seconds)

    def _entry_date(self, entry: dict[str, Any]) -> date:
        ts = entry.get("ts")
        if isinstance(ts, str) and len(ts) >= 10:
            try:
                return date.fromisoformat(ts[:10])
            except ValueError:
                pass
        return self._now_fn().date()

    def _entry_hour(self, entry: dict[str, Any]) -> str:
        occurred_at = self._entry_datetime(entry)
        if occurred_at is None:
            occurred_at = self._now_fn()
        return f"{occurred_at.date().isoformat()}T{occurred_at.hour:02d}:00:00"

    @staticmethod
    def _entry_datetime(entry: dict[str, Any]) -> datetime | None:
        ts = entry.get("ts")
        if not isinstance(ts, str) or not ts:
            return None
        try:
            return datetime.fromisoformat(ts.replace("Z", "+00:00"))
        except ValueError:
            return None

    def _scope_bucket_for_day(self, day: date, scope: str) -> dict[str, Any]:
        scopes = self._days_by_scope.setdefault(day, _new_scope_buckets())
        return scopes.setdefault(scope, _new_bucket())

    def _scope_bucket_for_hour(self, hour: str, scope: str) -> dict[str, Any]:
        scopes = self._hours_by_scope.setdefault(hour, _new_scope_buckets())
        return scopes.setdefault(scope, _new_bucket())

    def _scope_bucket_for_lifetime(self, scope: str) -> dict[str, Any]:
        return self._lifetime_by_scope.setdefault(scope, _new_bucket())

    @staticmethod
    def _scope_for_stream_id(stream_id: str) -> str:
        if stream_id == _MAIN_STREAM_ID:
            return _MAIN_SCOPE
        if stream_id.startswith("bubble:subconscious/bubbles/"):
            return _SUBCONSCIOUS_SCOPE
        if stream_id.startswith("bubble:bubbles/"):
            return _BUBBLE_SCOPE
        return _UNKNOWN_SCOPE

    @staticmethod
    def _merge_scope_buckets(
        dst: dict[str, dict[str, Any]],
        src: dict[str, dict[str, Any]],
    ) -> None:
        for scope, bucket in src.items():
            if isinstance(bucket, dict):
                _merge_bucket(dst.setdefault(str(scope), _new_bucket()), bucket)

    @staticmethod
    def _finalize_window(
        bucket: dict[str, Any],
        scopes: dict[str, dict[str, Any]],
        pricing: PricingCatalog | None = None,
    ) -> dict[str, Any]:
        scope_payload: dict[str, Any] = {}
        for scope in _DEFAULT_SCOPES:
            scope_payload[scope] = _finalize_bucket(
                scopes.get(scope, _new_bucket()),
                pricing,
            )
        for scope, scope_bucket in sorted(scopes.items()):
            if scope in scope_payload or not isinstance(scope_bucket, dict):
                continue
            if _bucket_has_data(scope_bucket):
                scope_payload[scope] = _finalize_bucket(scope_bucket, pricing)
        return {
            **_finalize_bucket(bucket, pricing),
            "by_scope": scope_payload,
        }

    def _load_cold(self, log_store: LogStore) -> None:
        iter_entries = getattr(log_store, "iter_all_entries", None)
        if callable(iter_entries):
            for entry in iter_entries():
                self.on_entry(entry, persist=False)
            return
        entries, _complete = log_store.read_all()
        self.load_entries(entries)

    def _load_incremental(self, log_store: LogStore) -> None:
        iter_entries_after = getattr(log_store, "iter_entries_after", None)
        if callable(iter_entries_after):
            entries = iter_entries_after(self._last_seq(_MAIN_STREAM_ID))
            for entry in entries:
                self.on_entry(entry, persist=False)
            return
        for entry in log_store.iter_all_entries():
            self.on_entry(entry, persist=False)

    @staticmethod
    def _load_scope_map(data: Any) -> dict[str, dict[str, Any]]:
        scopes = _new_scope_buckets()
        if not isinstance(data, dict):
            return scopes
        for scope, bucket in data.items():
            if not isinstance(bucket, dict):
                continue
            dst = scopes.setdefault(str(scope), _new_bucket())
            _merge_bucket(dst, bucket)
        return scopes

    def _should_process(self, entry: dict[str, Any], stream_id: str) -> bool:
        seq = self._entry_seq(entry)
        if seq is None:
            return True
        if seq <= self._last_seq(stream_id):
            return False
        self._last_seq_by_stream[stream_id] = seq
        return True

    def _last_seq(self, stream_id: str) -> int:
        return self._last_seq_by_stream.get(stream_id, -1)

    @staticmethod
    def _entry_seq(entry: dict[str, Any]) -> int | None:
        try:
            return int(entry["seq"])
        except (KeyError, TypeError, ValueError):
            return None

    def _load_state(self) -> bool:
        if self._state_path is None or not self._state_path.exists():
            return False
        try:
            data = json.loads(self._state_path.read_text(encoding="utf-8"))
        except Exception as e:
            logger.warning(f"Failed to read usage stats state {self._state_path}: {e}")
            return False
        schema_version = data.get("schema_version")
        if schema_version != _SCHEMA_VERSION:
            return False
        try:
            compression_tracking_since = data.get("compression_tracking_since")
            self._compression_tracking_since = (
                date.fromisoformat(compression_tracking_since)
                if isinstance(compression_tracking_since, str)
                else None
            )
            self._lifetime = _new_bucket()
            _merge_bucket(self._lifetime, data.get("lifetime", {}))
            self._days = {}
            for day_str, bucket in data.get("days", {}).items():
                day = date.fromisoformat(day_str)
                self._days[day] = _new_bucket()
                _merge_bucket(self._days[day], bucket)
            self._hours = {}
            for hour_str, bucket in data.get("hours", {}).items():
                hour = datetime.fromisoformat(hour_str)
                hour_key = f"{hour.date().isoformat()}T{hour.hour:02d}:00:00"
                self._hours[hour_key] = _new_bucket()
                _merge_bucket(self._hours[hour_key], bucket)
            self._hours_by_scope = {}
            for hour_str, scopes in data.get("hours_by_scope", {}).items():
                hour = datetime.fromisoformat(hour_str)
                hour_key = f"{hour.date().isoformat()}T{hour.hour:02d}:00:00"
                self._hours_by_scope[hour_key] = self._load_scope_map(scopes)
            self._lifetime_by_scope = self._load_scope_map(data.get("lifetime_by_scope", {}))
            self._days_by_scope = {}
            for day_str, scopes in data.get("days_by_scope", {}).items():
                day = date.fromisoformat(day_str)
                self._days_by_scope[day] = self._load_scope_map(scopes)
            self._last_seq_by_stream = {}
            checkpoints = data.get("checkpoints", {})
            for stream_id, checkpoint in checkpoints.items():
                if isinstance(checkpoint, dict):
                    self._last_seq_by_stream[str(stream_id)] = int(checkpoint.get("seq", -1))
            if _MAIN_STREAM_ID not in self._last_seq_by_stream:
                checkpoint = data.get("checkpoint", {})
                self._last_seq_by_stream[_MAIN_STREAM_ID] = int(checkpoint.get("seq", -1))
            self._pending_thinking_starts = self._load_pending_thinking_starts(
                data.get("pending_thinking_starts", {})
            )
            self._pending_tool_calls = self._load_pending_tool_calls(
                data.get("pending_tool_calls", {})
            )
            bubble_history = data.get("bubble_history", {})
            if isinstance(bubble_history, dict):
                mtime_ns = int(bubble_history.get("mtime_ns", -1))
                path = str(bubble_history.get("path", ""))
                if mtime_ns >= 0 and path:
                    self._bubble_history_key = (mtime_ns, path)
                self._bubble_history_scanned = bool(bubble_history.get("scanned"))
            elif self._bubble_history_key is not None:
                self._bubble_history_scanned = True
        except Exception as e:
            logger.warning(f"Failed to parse usage stats state {self._state_path}: {e}")
            self._days = {}
            self._hours = {}
            self._hours_by_scope = {}
            self._lifetime = _new_bucket()
            self._days_by_scope = {}
            self._lifetime_by_scope = _new_scope_buckets()
            self._last_seq_by_stream = {}
            self._pending_thinking_starts = {}
            self._pending_tool_calls = {}
            self._compression_tracking_since = None
            return False
        return True

    @staticmethod
    def _load_pending_thinking_starts(data: Any) -> dict[str, tuple[datetime, date]]:
        pending: dict[str, tuple[datetime, date]] = {}
        if not isinstance(data, dict):
            return pending
        for stream_id, item in data.items():
            if not isinstance(item, dict):
                continue
            ts = item.get("ts")
            if not isinstance(ts, str):
                continue
            try:
                started_at = datetime.fromisoformat(ts.replace("Z", "+00:00"))
            except ValueError:
                continue
            day_str = item.get("day")
            try:
                day = date.fromisoformat(day_str) if isinstance(day_str, str) else started_at.date()
            except ValueError:
                day = started_at.date()
            pending[str(stream_id)] = (started_at, day)
        return pending

    def _persist_state(self) -> None:
        if self._state_path is None:
            return
        checkpoints = {
            stream_id: {"seq": seq}
            for stream_id, seq in sorted(self._last_seq_by_stream.items())
        }
        payload = {
            "schema_version": _SCHEMA_VERSION,
            "updated_at": self._now_fn().isoformat(),
            "checkpoint": checkpoints.get(_MAIN_STREAM_ID, {"seq": -1}),
            "checkpoints": checkpoints,
            "pending_thinking_starts": self._format_pending_thinking_starts(),
            "pending_tool_calls": self._pending_tool_calls,
            "compression_tracking_since": (
                self._compression_tracking_since.isoformat()
                if self._compression_tracking_since is not None
                else None
            ),
            "bubble_history": self._format_bubble_history(),
            "lifetime": self._lifetime,
            "days": {day.isoformat(): bucket for day, bucket in sorted(self._days.items())},
            "hours": {hour: bucket for hour, bucket in sorted(self._hours.items())},
            "hours_by_scope": {
                hour: scopes for hour, scopes in sorted(self._hours_by_scope.items())
            },
            "lifetime_by_scope": self._lifetime_by_scope,
            "days_by_scope": {
                day.isoformat(): scopes for day, scopes in sorted(self._days_by_scope.items())
            },
        }
        try:
            self._state_path.parent.mkdir(parents=True, exist_ok=True)
            self._state_path.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except Exception as e:
            logger.warning(f"Failed to persist usage stats state {self._state_path}: {e}")

    def _format_pending_thinking_starts(self) -> dict[str, dict[str, str]]:
        return {
            stream_id: {"ts": started_at.isoformat(), "day": day.isoformat()}
            for stream_id, (started_at, day) in sorted(self._pending_thinking_starts.items())
        }

    @staticmethod
    def _load_pending_tool_calls(data: Any) -> dict[str, dict[str, str]]:
        pending: dict[str, dict[str, str]] = {}
        if not isinstance(data, dict):
            return pending
        for key, item in data.items():
            if not isinstance(item, dict):
                continue
            stream_id = str(item.get("stream_id") or "")
            day = str(item.get("day") or "")
            tool_name = str(item.get("tool_name") or "")
            if not stream_id or not day or not tool_name:
                continue
            try:
                date.fromisoformat(day)
            except ValueError:
                continue
            pending[str(key)] = {
                "stream_id": stream_id,
                "day": day,
                "tool_name": tool_name,
                "skill_name": str(item.get("skill_name") or ""),
            }
        return pending

    def _discard_pending_tool_calls(self, stream_id: str) -> None:
        stale = [
            key
            for key, item in self._pending_tool_calls.items()
            if item.get("stream_id") == stream_id
        ]
        for key in stale:
            self._pending_tool_calls.pop(key, None)

    @staticmethod
    def bubble_stream_id(root: Path, path: Path) -> str:
        try:
            rel = path.relative_to(root).as_posix()
        except ValueError:
            rel = path.name
        return f"bubble:{rel}"

    def _advance_bubble_history(self, key: tuple[int, str]) -> None:
        if self._bubble_history_key is None or key > self._bubble_history_key:
            self._bubble_history_key = key

    def _format_bubble_history(self) -> dict[str, Any]:
        if self._bubble_history_key is None:
            return {"scanned": self._bubble_history_scanned}
        mtime_ns, path = self._bubble_history_key
        return {"mtime_ns": mtime_ns, "path": path, "scanned": self._bubble_history_scanned}

    def _load_pending_bubble_streams(self, root: Path) -> None:
        pending = [
            stream_id
            for stream_id in sorted(self._last_seq_by_stream)
            if stream_id.startswith("bubble:")
        ]
        if not pending:
            return
        try:
            self._loading_history = True
            for stream_id in pending:
                rel = stream_id[len("bubble:"):]
                path = root / Path(rel)
                saw_meta = False
                for entry in self._iter_jsonl(path):
                    if entry.get("__meta__"):
                        saw_meta = True
                    self.on_entry(entry, persist=False, stream_id=stream_id)
                if saw_meta:
                    self._last_seq_by_stream.pop(stream_id, None)
                    self._pending_thinking_starts.pop(stream_id, None)
                    self._discard_pending_tool_calls(stream_id)
                    self._advance_bubble_history(self._bubble_file_key(root, path))
        except Exception as e:
            logger.warning(f"Failed to load pending bubble usage streams: {e}")
        finally:
            self._loading_history = False

    @staticmethod
    def _bubble_file_key(root: Path, path: Path) -> tuple[int, str]:
        try:
            rel = path.relative_to(root)
        except ValueError:
            rel = path
        rel_path = Path(rel).as_posix()
        try:
            mtime_ns = path.stat().st_mtime_ns
        except OSError:
            mtime_ns = 0
        return (mtime_ns, rel_path)

    @staticmethod
    def _iter_jsonl(path: Path):
        try:
            with path.open("r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        entry = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if isinstance(entry, dict):
                        yield entry
        except OSError as e:
            logger.warning(f"Failed to stream bubble usage log {path}: {e}")
