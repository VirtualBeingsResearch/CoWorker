"""Export-contract shape guards for usage statistics.

These tests pin the exact key sets of public JSON payloads (``snapshot``,
``report`` and the persisted state file). They fail when a field is added or
removed so the change to the user-visible contract is always deliberate:
update the expected sets here together with the producing code.
"""

from __future__ import annotations

import json
from datetime import datetime

from coworker.agent.usage_stats import UsageStatsCollector

_NOW = datetime(2026, 6, 29, 12, 0, 0)

# Compact windows (public /status payload).
_SNAPSHOT_WINDOW_KEYS = {
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
    "thinking_calls",
    "thinking_seconds",
    "avg_thinking_seconds",
    "by_model",
    "by_provider_model",
    "tools",
    "events",
    "by_scope",
}

# Detailed windows (authenticated admin report): the full finalized view.
_REPORT_WINDOW_KEYS = {
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
    "by_model",
    "by_provider_model",
    "tools",
    "tool_outcomes",
    "skills",
    "events",
    "by_scope",
}

# Summary windows (report previous/daily/intraday sections).
_SUMMARY_WINDOW_KEYS = {
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
    "by_scope",
}

_REPORT_TOP_LEVEL_KEYS = {
    "today",
    "last_7_days",
    "lifetime",
    "last_30_days",
    "previous",
    "daily",
    "today_intraday",
    "generated_at",
    "tracking_since",
    "compression_tracking_since",
}

_STATE_TOP_LEVEL_KEYS = {
    "schema_version",
    "updated_at",
    "checkpoint",
    "checkpoints",
    "pending_thinking_starts",
    "pending_tool_calls",
    "compression_tracking_since",
    "bubble_history",
    "lifetime",
    "days",
    "hours",
    "hours_by_scope",
    "lifetime_by_scope",
    "days_by_scope",
}


def _collector(state_path=None) -> UsageStatsCollector:
    return UsageStatsCollector(
        now_fn=lambda: _NOW,
        state_path=state_path,
    )


def test_snapshot_windows_expose_the_contract_keys():
    snapshot = _collector().snapshot()

    assert snapshot["today"].keys() == _SNAPSHOT_WINDOW_KEYS
    assert snapshot["last_7_days"].keys() == _SNAPSHOT_WINDOW_KEYS
    assert snapshot["lifetime"].keys() == _SNAPSHOT_WINDOW_KEYS
    assert (
        snapshot["lifetime"]["by_scope"]["main"].keys()
        == _SNAPSHOT_WINDOW_KEYS - {"by_scope"}
    )


def test_report_windows_expose_the_contract_keys():
    payload = _collector().report()

    assert payload.keys() == _REPORT_TOP_LEVEL_KEYS
    assert payload["today"].keys() == _REPORT_WINDOW_KEYS
    assert payload["last_30_days"].keys() == _REPORT_WINDOW_KEYS
    assert payload["daily"][0].keys() == {"date"} | _SUMMARY_WINDOW_KEYS
    assert payload["today_intraday"][0].keys() == {
        "start_time",
        "end_time",
    } | _SUMMARY_WINDOW_KEYS
    assert payload["previous"]["today"].keys() == _SUMMARY_WINDOW_KEYS


def test_report_windows_with_pricing_add_cost_keys():
    payload = _collector().report(model_prices=[])

    cost_keys = {"estimated_costs", "priced_tokens", "unpriced_tokens", "pricing_coverage"}
    assert payload["today"].keys() == _REPORT_WINDOW_KEYS | cost_keys
    assert payload["previous"]["today"].keys() == _SUMMARY_WINDOW_KEYS | cost_keys


def test_state_file_exposes_the_contract_keys(tmp_path):
    state_path = tmp_path / "usage_stats.json"
    collector = _collector(state_path=state_path)
    collector.on_entry({
        "type": "llm_response",
        "ts": "2026-06-29T08:00:00",
        "provider": "openai",
        "model": "gpt-4o",
        "usage": {"input_tokens": 10, "output_tokens": 2},
    })
    collector._persist_state()

    data = json.loads(state_path.read_text(encoding="utf-8"))

    assert data.keys() == _STATE_TOP_LEVEL_KEYS
