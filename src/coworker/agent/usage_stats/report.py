"""Window shaping for usage statistics: finalize, summary and compact views."""

from __future__ import annotations

from typing import Any

from .buckets import (
    DEFAULT_SCOPES,
    bucket_has_data,
    float_value,
    int_value,
    new_bucket,
    new_compression_trigger_bucket,
    norm_part,
    split_provider_model_key,
)
from .pricing import PricingCatalog, calculate_model_cost, pricing_summary

REPORT_DAYS = 30
INTRADAY_HOURS = 24
SUMMARY_KEYS = (
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
    "events",
)
COST_SUMMARY_KEYS = (
    "estimated_costs",
    "priced_tokens",
    "unpriced_tokens",
    "pricing_coverage",
)
ADMIN_WINDOW_KEYS = {
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


def finalize_model_bucket(bucket: dict[str, int]) -> dict[str, Any]:
    input_tokens = int_value(bucket.get("input_tokens"))
    output_tokens = int_value(bucket.get("output_tokens"))
    cached_tokens = int_value(bucket.get("cached_tokens"))
    llm_calls = int_value(bucket.get("llm_calls"))
    tracked_calls = min(llm_calls, int_value(bucket.get("tracked_calls")))
    estimated_calls = min(tracked_calls, int_value(bucket.get("estimated_calls")))
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


def finalize_provider_model_bucket(
    key: str,
    bucket: dict[str, Any],
    pricing: PricingCatalog | None = None,
) -> dict[str, Any]:
    provider, model = split_provider_model_key(key)
    provider = norm_part(bucket.get("provider"), provider)
    model = norm_part(bucket.get("model"), model)
    finalized = finalize_model_bucket(bucket)
    payload = {
        "provider": provider,
        "model": model,
        **finalized,
    }
    if pricing is not None:
        currency, cost, _ = calculate_model_cost(bucket, provider, model, pricing)
        payload["currency"] = currency
        payload["estimated_cost"] = float(cost) if cost is not None else None
    return payload


def finalize_bucket(
    bucket: dict[str, Any],
    pricing: PricingCatalog | None = None,
) -> dict[str, Any]:
    input_tokens = int_value(bucket.get("input_tokens"))
    output_tokens = int_value(bucket.get("output_tokens"))
    cached_tokens = int_value(bucket.get("cached_tokens"))
    thinking_calls = int_value(bucket.get("thinking_calls"))
    thinking_seconds = float_value(bucket.get("thinking_seconds"))
    tools = {
        name: count
        for name, count in sorted(
            bucket.get("tools", {}).items(),
            key=lambda item: (-int_value(item[1]), str(item[0])),
        )
    }
    tool_outcomes = {}
    for name, outcome in sorted(
        bucket.get("tool_outcomes", {}).items(),
        key=lambda item: (
            -int_value(item[1].get("calls")) if isinstance(item[1], dict) else 0,
            str(item[0]),
        ),
    ):
        if not isinstance(outcome, dict):
            continue
        calls = int_value(outcome.get("calls"))
        successes = min(calls, int_value(outcome.get("successes")))
        errors = min(max(0, calls - successes), int_value(outcome.get("errors")))
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
                int_value(pair[1].get("explicit_attempts"))
                + int_value(pair[1].get("automatic_loads"))
            )
            if isinstance(pair[1], dict)
            else 0,
            str(pair[0]),
        ),
    ):
        if not isinstance(item, dict):
            continue
        attempts = int_value(item.get("explicit_attempts"))
        successes = min(attempts, int_value(item.get("explicit_successes")))
        errors = min(max(0, attempts - successes), int_value(item.get("explicit_errors")))
        skills[str(name)] = {
            "explicit_attempts": attempts,
            "explicit_successes": successes,
            "explicit_errors": errors,
            "explicit_incomplete": max(0, attempts - successes - errors),
            "automatic_loads": int_value(item.get("automatic_loads")),
        }
    by_model = {
        model: finalize_model_bucket(model_bucket)
        for model, model_bucket in sorted(bucket.get("by_model", {}).items())
    }
    by_provider_model = {
        key: finalize_provider_model_bucket(key, provider_model_bucket, pricing)
        for key, provider_model_bucket in sorted(bucket.get("by_provider_model", {}).items())
    }
    llm_calls = int_value(bucket.get("llm_calls"))
    tracked_calls = min(llm_calls, int_value(bucket.get("tracked_calls")))
    estimated_calls = min(tracked_calls, int_value(bucket.get("estimated_calls")))
    exact_calls = max(0, tracked_calls - estimated_calls)
    tool_calls = int_value(bucket.get("tool_calls"))
    tool_successes = min(tool_calls, int_value(bucket.get("tool_successes")))
    tool_errors = min(
        max(0, tool_calls - tool_successes),
        int_value(bucket.get("tool_errors")),
    )
    settled_tool_calls = tool_successes + tool_errors
    skill_load_attempts = int_value(bucket.get("skill_load_attempts"))
    skill_load_successes = min(
        skill_load_attempts,
        int_value(bucket.get("skill_load_successes")),
    )
    skill_load_errors = min(
        max(0, skill_load_attempts - skill_load_successes),
        int_value(bucket.get("skill_load_errors")),
    )
    bubble_runs = int_value(bucket.get("bubble_runs"))
    bubble_elapsed_seconds = float_value(bucket.get("bubble_elapsed_seconds"))
    bubble_cycles = int_value(bucket.get("bubble_cycles"))
    memory_compressions = int_value(bucket.get("memory_compressions"))
    memory_compression_duration_ms = int_value(
        bucket.get("memory_compression_duration_ms")
    )
    memory_compression_summary_calls = int_value(
        bucket.get("memory_compression_summary_calls")
    )
    memory_compression_summary_tracked_calls = min(
        memory_compression_summary_calls,
        int_value(bucket.get("memory_compression_summary_tracked_calls")),
    )
    memory_compression_input_tokens = int_value(
        bucket.get("memory_compression_input_tokens")
    )
    memory_compression_output_tokens = int_value(
        bucket.get("memory_compression_output_tokens")
    )
    compression_triggers = new_compression_trigger_bucket()
    raw_compression_triggers = bucket.get("memory_compression_triggers", {})
    if isinstance(raw_compression_triggers, dict):
        for trigger, count in raw_compression_triggers.items():
            trigger_name = str(trigger)
            if trigger_name not in compression_triggers:
                trigger_name = "other"
            compression_triggers[trigger_name] += int_value(count)
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
        "automatic_skill_loads": int_value(bucket.get("automatic_skill_loads")),
        "bubble_runs": bubble_runs,
        "bubble_done": min(bubble_runs, int_value(bucket.get("bubble_done"))),
        "bubble_errors": min(bubble_runs, int_value(bucket.get("bubble_errors"))),
        "bubble_timeouts": min(bubble_runs, int_value(bucket.get("bubble_timeouts"))),
        "bubble_cancelled": min(bubble_runs, int_value(bucket.get("bubble_cancelled"))),
        "bubble_cycles": bubble_cycles,
        "bubble_elapsed_seconds": round(bubble_elapsed_seconds, 3),
        "avg_bubble_cycles": bubble_cycles / bubble_runs if bubble_runs else None,
        "avg_bubble_seconds": bubble_elapsed_seconds / bubble_runs if bubble_runs else None,
        "bubble_resumes": int_value(bucket.get("bubble_resumes")),
        "bubble_max_cycles_reached": int_value(
            bucket.get("bubble_max_cycles_reached")
        ),
        "thinking_calls": thinking_calls,
        "thinking_seconds": round(thinking_seconds, 3),
        "avg_thinking_seconds": (
            thinking_seconds / thinking_calls if thinking_calls else None
        ),
        "memory_compressions": memory_compressions,
        "messages_compressed": int_value(bucket.get("messages_compressed")),
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
        "memory_compression_cached_tokens": int_value(
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
        "events": finalize_events(bucket.get("events") or {}),
    }
    if pricing is not None:
        payload.update(pricing_summary(bucket, pricing))
    return payload


def finalize_events(events: dict[str, Any]) -> dict[str, Any]:
    """Shape the bucket's ``events`` sub-dict for report and snapshot output."""
    shaped: dict[str, Any] = {}
    for key, value in sorted(events.items()):
        if isinstance(value, dict):
            shaped[str(key)] = {
                str(label): int_value(count) for label, count in sorted(value.items())
            }
        else:
            shaped[str(key)] = int_value(value)
    return shaped


def summary_bucket(
    bucket: dict[str, Any],
    pricing: PricingCatalog | None = None,
) -> dict[str, Any]:
    finalized = finalize_bucket(bucket, pricing)
    keys = SUMMARY_KEYS + (COST_SUMMARY_KEYS if pricing is not None else ())
    return {key: finalized[key] for key in keys}


def summary_scope_buckets(
    scopes: dict[str, dict[str, Any]],
    pricing: PricingCatalog | None = None,
) -> dict[str, dict[str, Any]]:
    payload = {
        scope: summary_bucket(scopes.get(scope, new_bucket()), pricing)
        for scope in DEFAULT_SCOPES
    }
    for scope, bucket in sorted(scopes.items()):
        if scope in payload or not isinstance(bucket, dict):
            continue
        if bucket_has_data(bucket):
            payload[str(scope)] = summary_bucket(bucket, pricing)
    return payload


def summary_window(
    bucket: dict[str, Any],
    scopes: dict[str, dict[str, Any]],
    pricing: PricingCatalog | None = None,
) -> dict[str, Any]:
    return {
        **summary_bucket(bucket, pricing),
        "by_scope": summary_scope_buckets(scopes, pricing),
    }


def finalize_window(
    bucket: dict[str, Any],
    scopes: dict[str, dict[str, Any]],
    pricing: PricingCatalog | None = None,
) -> dict[str, Any]:
    scope_payload: dict[str, Any] = {}
    for scope in DEFAULT_SCOPES:
        scope_payload[scope] = finalize_bucket(
            scopes.get(scope, new_bucket()),
            pricing,
        )
    for scope, scope_bucket in sorted(scopes.items()):
        if scope in scope_payload or not isinstance(scope_bucket, dict):
            continue
        if bucket_has_data(scope_bucket):
            scope_payload[scope] = finalize_bucket(scope_bucket, pricing)
    return {
        **finalize_bucket(bucket, pricing),
        "by_scope": scope_payload,
    }


def compact_window(window: dict[str, Any]) -> dict[str, Any]:
    compact = {key: value for key, value in window.items() if key not in ADMIN_WINDOW_KEYS}
    scopes = window.get("by_scope")
    if isinstance(scopes, dict):
        compact["by_scope"] = {
            str(name): {
                key: value
                for key, value in scope.items()
                if key not in ADMIN_WINDOW_KEYS
            }
            for name, scope in scopes.items()
            if isinstance(scope, dict)
        }
    return compact
