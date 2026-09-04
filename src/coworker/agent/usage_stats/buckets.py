"""Declared bucket schema and aggregation primitives for usage statistics.

Buckets remain plain dicts (they are the persisted JSON state format), but every
additive field is declared once here so construction, merging and has-data
checks all derive from a single table instead of parallel hand-written key
lists. Adding a new additive counter means adding one ``FieldSpec`` row.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

# Per-call token counters reported by providers.
TOKEN_KEYS = ("input_tokens", "output_tokens", "cached_tokens")

MAIN_STREAM_ID = "main"
MAIN_SCOPE = "main"
SUMMARY_SCOPE = "summary"
VISION_SCOPE = "vision"
BUBBLE_SCOPE = "bubble"
SUBCONSCIOUS_SCOPE = "subconscious"
LONG_TERM_SCOPE = "long_term"
UNKNOWN_SCOPE = "unknown"
DEFAULT_SCOPES = (
    MAIN_SCOPE,
    SUMMARY_SCOPE,
    VISION_SCOPE,
    BUBBLE_SCOPE,
    SUBCONSCIOUS_SCOPE,
    LONG_TERM_SCOPE,
)
UNKNOWN_PROVIDER = "unknown"
UNKNOWN_MODEL = "unknown"

TOOL_OUTCOME_KEYS = ("calls", "successes", "errors")
SKILL_BUCKET_KEYS = (
    "explicit_attempts",
    "explicit_successes",
    "explicit_errors",
    "automatic_loads",
)


@dataclass(frozen=True)
class FieldSpec:
    """One additive bucket field.

    ``int`` fields are clamped non-negative counters, ``float`` fields are
    clamped non-negative accumulators and ``last_seen`` fields keep the
    lexicographically greatest timestamp string seen (empty string means none).
    """

    name: str
    kind: str = "int"


_INT_FIELD_SPECS = tuple(
    FieldSpec(name)
    for name in (
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
)
_FLOAT_FIELD_SPECS = (
    FieldSpec("bubble_elapsed_seconds", "float"),
    FieldSpec("thinking_seconds", "float"),
)
_LAST_SEEN_FIELD_SPECS = (FieldSpec("last_memory_compression_at", "last_seen"),)

#: Every declared additive field, in construction/merge order.
FIELDS: tuple[FieldSpec, ...] = (
    *_INT_FIELD_SPECS,
    *_FLOAT_FIELD_SPECS,
    *_LAST_SEEN_FIELD_SPECS,
)

#: Integer counter fields; presence of a positive value marks a bucket as used.
INT_FIELDS: tuple[str, ...] = tuple(field.name for field in _INT_FIELD_SPECS)
FLOAT_FIELDS: tuple[str, ...] = tuple(field.name for field in _FLOAT_FIELD_SPECS)

# Legacy alias kept for callers that enumerate the integer counter keys.
METRIC_KEYS = INT_FIELDS

COMPRESSION_TRIGGERS = ("automatic", "admin", "tool", "other")


def new_model_bucket() -> dict[str, int]:
    return {
        "llm_calls": 0,
        "tracked_calls": 0,
        "estimated_calls": 0,
        "input_tokens": 0,
        "output_tokens": 0,
        "cached_tokens": 0,
    }


def new_provider_model_bucket(provider: str, model: str) -> dict[str, Any]:
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


def new_tool_outcome_bucket() -> dict[str, int]:
    return {
        "calls": 0,
        "successes": 0,
        "errors": 0,
    }


def new_skill_bucket() -> dict[str, int]:
    return {
        "explicit_attempts": 0,
        "explicit_successes": 0,
        "explicit_errors": 0,
        "automatic_loads": 0,
    }


def new_compression_trigger_bucket() -> dict[str, int]:
    return {trigger: 0 for trigger in COMPRESSION_TRIGGERS}


def new_bucket() -> dict[str, Any]:
    bucket: dict[str, Any] = {field.name: 0 for field in _INT_FIELD_SPECS}
    for field in _FLOAT_FIELD_SPECS:
        bucket[field.name] = 0.0
    bucket["memory_compression_triggers"] = new_compression_trigger_bucket()
    bucket["last_memory_compression_at"] = None
    bucket["by_model"] = {}
    bucket["by_provider_model"] = {}
    bucket["tools"] = {}
    bucket["tool_outcomes"] = {}
    bucket["skills"] = {}
    return bucket


def new_scope_buckets() -> dict[str, dict[str, Any]]:
    return {scope: new_bucket() for scope in DEFAULT_SCOPES}


def int_value(value: Any) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def float_value(value: Any) -> float:
    try:
        return max(0.0, float(value or 0.0))
    except (TypeError, ValueError):
        return 0.0


def norm_part(value: Any, fallback: str) -> str:
    text = str(value or "").strip()
    return text or fallback


def provider_model_key(provider: str, model: str) -> str:
    return f"{provider}/{model}"


def split_provider_model_key(key: str) -> tuple[str, str]:
    provider, sep, model = str(key).partition("/")
    if not sep:
        return UNKNOWN_PROVIDER, norm_part(provider, UNKNOWN_MODEL)
    return norm_part(provider, UNKNOWN_PROVIDER), norm_part(model, UNKNOWN_MODEL)


def scope_for_stream_id(stream_id: str) -> str:
    if stream_id == MAIN_STREAM_ID:
        return MAIN_SCOPE
    if stream_id.startswith("bubble:subconscious/bubbles/"):
        return SUBCONSCIOUS_SCOPE
    if stream_id.startswith("bubble:bubbles/"):
        return BUBBLE_SCOPE
    return UNKNOWN_SCOPE


def add_usage(
    bucket: dict[str, Any],
    usage: dict[str, Any],
    provider: str,
    model: str,
    usage_source: str = "",
) -> None:
    provider = norm_part(provider, UNKNOWN_PROVIDER)
    model = norm_part(model, UNKNOWN_MODEL)
    bucket["llm_calls"] += 1
    model_bucket = bucket["by_model"].setdefault(model, new_model_bucket())
    key = provider_model_key(provider, model)
    provider_model_bucket = bucket["by_provider_model"].setdefault(
        key,
        new_provider_model_bucket(provider, model),
    )
    model_bucket["llm_calls"] += 1
    provider_model_bucket["llm_calls"] += 1
    tracked = any(key in usage and usage.get(key) is not None for key in TOKEN_KEYS)
    estimated = tracked and usage_source == "estimated"
    if tracked:
        bucket["tracked_calls"] += 1
        model_bucket["tracked_calls"] += 1
        provider_model_bucket["tracked_calls"] += 1
    if estimated:
        bucket["estimated_calls"] += 1
        model_bucket["estimated_calls"] += 1
        provider_model_bucket["estimated_calls"] += 1
    for key in TOKEN_KEYS:
        value = int_value(usage.get(key))
        bucket[key] += value
        model_bucket[key] += value
        provider_model_bucket[key] += value


def add_tool_call(bucket: dict[str, Any], tool_name: str) -> None:
    bucket["tool_calls"] += 1
    bucket["tools"][tool_name] = bucket["tools"].get(tool_name, 0) + 1
    outcome = bucket["tool_outcomes"].setdefault(tool_name, new_tool_outcome_bucket())
    outcome["calls"] += 1


def add_tool_result(bucket: dict[str, Any], tool_name: str, is_error: bool) -> None:
    outcome = bucket["tool_outcomes"].setdefault(tool_name, new_tool_outcome_bucket())
    key = "errors" if is_error else "successes"
    outcome[key] += 1
    bucket[f"tool_{key}"] += 1


def add_skill_attempt(bucket: dict[str, Any], skill_name: str) -> None:
    bucket["skill_load_attempts"] += 1
    skill = bucket["skills"].setdefault(skill_name, new_skill_bucket())
    skill["explicit_attempts"] += 1


def add_skill_result(bucket: dict[str, Any], skill_name: str, is_error: bool) -> None:
    key = "errors" if is_error else "successes"
    bucket[f"skill_load_{key}"] += 1
    skill = bucket["skills"].setdefault(skill_name, new_skill_bucket())
    skill[f"explicit_{key}"] += 1


def add_automatic_skill_load(bucket: dict[str, Any], skill_name: str) -> None:
    bucket["automatic_skill_loads"] += 1
    skill = bucket["skills"].setdefault(skill_name, new_skill_bucket())
    skill["automatic_loads"] += 1


def add_bubble_outcome(bucket: dict[str, Any], entry: dict[str, Any]) -> None:
    status = str(entry.get("status") or "")
    status_key = {
        "done": "bubble_done",
        "error": "bubble_errors",
        "timeout": "bubble_timeouts",
        "cancelled": "bubble_cancelled",
    }.get(status)
    if status_key is None:
        return
    cycles = int_value(entry.get("cycles_used"))
    max_cycles = int_value(entry.get("max_cycles"))
    bucket["bubble_runs"] += 1
    bucket[status_key] += 1
    bucket["bubble_cycles"] += cycles
    bucket["bubble_elapsed_seconds"] += float_value(entry.get("elapsed_seconds"))
    bucket["bubble_resumes"] += int_value(entry.get("resume_count"))
    if max_cycles and cycles >= max_cycles:
        bucket["bubble_max_cycles_reached"] += 1


def add_thinking_duration(bucket: dict[str, Any], seconds: float) -> None:
    bucket["thinking_calls"] += 1
    bucket["thinking_seconds"] += seconds


def add_memory_compression(
    bucket: dict[str, Any],
    entry: dict[str, Any],
    occurred_at: str,
) -> None:
    bucket["memory_compressions"] += 1
    bucket["messages_compressed"] += int_value(entry.get("messages_compressed"))
    bucket["memory_compression_duration_ms"] += int_value(entry.get("duration_ms"))
    bucket["memory_compression_summary_calls"] += int_value(entry.get("summary_calls"))
    bucket["memory_compression_summary_tracked_calls"] += int_value(
        entry.get("summary_tracked_calls")
    )
    bucket["memory_compression_input_tokens"] += int_value(
        entry.get("summary_input_tokens")
    )
    bucket["memory_compression_output_tokens"] += int_value(
        entry.get("summary_output_tokens")
    )
    bucket["memory_compression_cached_tokens"] += int_value(
        entry.get("summary_cached_tokens")
    )
    trigger = str(entry.get("trigger") or "other")
    if trigger not in COMPRESSION_TRIGGERS:
        trigger = "other"
    triggers = bucket["memory_compression_triggers"]
    triggers[trigger] = triggers.get(trigger, 0) + 1
    if occurred_at and occurred_at > str(bucket.get("last_memory_compression_at") or ""):
        bucket["last_memory_compression_at"] = occurred_at


def merge_model_bucket(dst: dict[str, int], src: dict[str, int]) -> None:
    for key in ("llm_calls", "tracked_calls", "estimated_calls", *TOKEN_KEYS):
        dst[key] = dst.get(key, 0) + int_value(src.get(key))


def merge_provider_model_bucket(dst: dict[str, Any], src: dict[str, Any]) -> None:
    dst["provider"] = norm_part(dst.get("provider") or src.get("provider"), UNKNOWN_PROVIDER)
    dst["model"] = norm_part(dst.get("model") or src.get("model"), UNKNOWN_MODEL)
    for key in ("llm_calls", "tracked_calls", "estimated_calls", *TOKEN_KEYS):
        dst[key] = dst.get(key, 0) + int_value(src.get(key))


def merge_bucket(dst: dict[str, Any], src: dict[str, Any]) -> None:
    for field in FIELDS:
        if field.kind == "int":
            dst[field.name] += int_value(src.get(field.name))
        elif field.kind == "float":
            dst[field.name] += float_value(src.get(field.name))
        else:  # last_seen: keep the greatest timestamp string
            seen = str(src.get(field.name) or "")
            if seen > str(dst.get(field.name) or ""):
                dst[field.name] = seen
    for trigger, count in src.get("memory_compression_triggers", {}).items():
        trigger_name = str(trigger)
        if trigger_name not in COMPRESSION_TRIGGERS:
            trigger_name = "other"
        dst["memory_compression_triggers"][trigger_name] += int_value(count)
    for model, model_bucket in src.get("by_model", {}).items():
        model = norm_part(model, UNKNOWN_MODEL)
        merge_model_bucket(dst["by_model"].setdefault(model, new_model_bucket()), model_bucket)
    provider_model_buckets = src.get("by_provider_model", {})
    if isinstance(provider_model_buckets, dict) and provider_model_buckets:
        for key, provider_model_bucket in provider_model_buckets.items():
            provider, model = split_provider_model_key(str(key))
            if isinstance(provider_model_bucket, dict):
                provider = norm_part(provider_model_bucket.get("provider"), provider)
                model = norm_part(provider_model_bucket.get("model"), model)
            key = provider_model_key(provider, model)
            dst_bucket = dst["by_provider_model"].setdefault(
                key,
                new_provider_model_bucket(provider, model),
            )
            if isinstance(provider_model_bucket, dict):
                merge_provider_model_bucket(dst_bucket, provider_model_bucket)
    else:
        for model, model_bucket in src.get("by_model", {}).items():
            model = norm_part(model, UNKNOWN_MODEL)
            key = provider_model_key(UNKNOWN_PROVIDER, model)
            merge_provider_model_bucket(
                dst["by_provider_model"].setdefault(
                    key,
                    new_provider_model_bucket(UNKNOWN_PROVIDER, model),
                ),
                model_bucket,
            )
    for tool, count in src.get("tools", {}).items():
        dst["tools"][tool] = dst["tools"].get(tool, 0) + int_value(count)
    for tool, outcome in src.get("tool_outcomes", {}).items():
        if not isinstance(outcome, dict):
            continue
        tool_name = norm_part(tool, "unknown")
        dst_outcome = dst["tool_outcomes"].setdefault(
            tool_name,
            new_tool_outcome_bucket(),
        )
        for key in TOOL_OUTCOME_KEYS:
            dst_outcome[key] += int_value(outcome.get(key))
    for skill, item in src.get("skills", {}).items():
        if not isinstance(item, dict):
            continue
        skill_name = norm_part(skill, "unknown")
        dst_skill = dst["skills"].setdefault(skill_name, new_skill_bucket())
        for key in SKILL_BUCKET_KEYS:
            dst_skill[key] += int_value(item.get(key))


def bucket_has_data(bucket: dict[str, Any]) -> bool:
    return any(int_value(bucket.get(name)) > 0 for name in INT_FIELDS)
