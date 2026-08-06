from __future__ import annotations

import json
from datetime import date, datetime
from pathlib import Path
from unittest.mock import patch

from coworker.agent.log_store import LogStore
from coworker.agent.usage_stats import UsageStatsCollector


def _collector() -> UsageStatsCollector:
    return UsageStatsCollector(now_fn=lambda: datetime(2026, 6, 29, 12, 0, 0))


def _write_jsonl(path, entries: list[dict]) -> None:
    path.write_text(
        "\n".join(json.dumps(e, ensure_ascii=False) for e in entries) + "\n",
        encoding="utf-8",
    )


def test_empty_snapshot_returns_zeroes():
    stats = _collector().snapshot()

    assert stats["today"]["llm_calls"] == 0
    assert stats["today"]["tracked_calls"] == 0
    assert stats["today"]["exact_calls"] == 0
    assert stats["today"]["untracked_calls"] == 0
    assert stats["today"]["tracking_coverage"] is None
    assert stats["today"]["exact_coverage"] is None
    assert stats["today"]["total_tokens"] == 0
    assert stats["today"]["cache_rate"] is None
    assert stats["today"]["thinking_calls"] == 0
    assert stats["today"]["thinking_seconds"] == 0
    assert stats["today"]["avg_thinking_seconds"] is None
    assert stats["last_7_days"]["tool_calls"] == 0
    assert "tool_successes" not in stats["last_7_days"]
    report = _collector().report()
    assert report["last_7_days"]["tool_successes"] == 0
    assert report["last_7_days"]["tool_errors"] == 0
    assert report["last_7_days"]["tool_incomplete"] == 0
    assert report["last_7_days"]["skill_load_attempts"] == 0
    assert report["last_7_days"]["automatic_skill_loads"] == 0
    assert report["last_7_days"]["bubble_runs"] == 0
    assert report["last_7_days"]["memory_compressions"] == 0
    assert report["last_7_days"]["messages_compressed"] == 0
    assert report["last_7_days"]["memory_compression_total_tokens"] == 0
    assert report["compression_tracking_since"] == "2026-06-29"
    assert stats["lifetime"]["by_model"] == {}
    assert stats["lifetime"]["by_provider_model"] == {}
    assert set(stats["lifetime"]["by_scope"]) == {
        "main",
        "summary",
        "vision",
        "bubble",
        "subconscious",
        "mem0",
    }
    assert stats["lifetime"]["by_scope"]["main"]["total_tokens"] == 0
    assert stats["lifetime"]["by_scope"]["summary"]["total_tokens"] == 0
    assert stats["lifetime"]["by_scope"]["vision"]["total_tokens"] == 0
    assert stats["lifetime"]["by_scope"]["mem0"]["total_tokens"] == 0


def test_aggregates_llm_usage_and_cache_rate():
    collector = _collector()
    collector.load_entries([
        {
            "type": "llm_response",
            "ts": "2026-06-29T08:00:00",
            "provider": "openai",
            "model": "gpt-4o",
            "usage": {"input_tokens": 1000, "output_tokens": 250, "cached_tokens": 400},
        },
        {
            "type": "llm_response",
            "ts": "2026-06-29T08:01:00",
            "provider": "openai",
            "model": "gpt-4o",
            "usage": {"input_tokens": 500, "output_tokens": 100, "cached_tokens": 50},
        },
    ])

    today = collector.snapshot()["today"]

    assert today["llm_calls"] == 2
    assert today["tracked_calls"] == 2
    assert today["exact_calls"] == 2
    assert today["untracked_calls"] == 0
    assert today["tracking_coverage"] == 1
    assert today["exact_coverage"] == 1
    assert today["avg_tokens_per_call"] == 925
    assert today["input_tokens"] == 1500
    assert today["output_tokens"] == 350
    assert today["cached_tokens"] == 450
    assert today["total_tokens"] == 1850
    assert today["cache_rate"] == 0.3
    assert today["by_model"]["gpt-4o"]["llm_calls"] == 2
    assert today["by_provider_model"]["openai/gpt-4o"]["provider"] == "openai"
    assert today["by_provider_model"]["openai/gpt-4o"]["model"] == "gpt-4o"
    assert today["by_provider_model"]["openai/gpt-4o"]["llm_calls"] == 2
    assert today["by_provider_model"]["openai/gpt-4o"]["cache_rate"] == 0.3
    assert today["by_scope"]["main"]["total_tokens"] == 1850
    assert today["by_scope"]["bubble"]["total_tokens"] == 0


def test_provider_model_breakdown_separates_same_model_across_providers():
    collector = _collector()
    collector.load_entries([
        {
            "type": "llm_response",
            "ts": "2026-06-29T08:00:00",
            "provider": "openai",
            "model": "shared-model",
            "usage": {"input_tokens": 100, "output_tokens": 10, "cached_tokens": 20},
        },
        {
            "type": "llm_response",
            "ts": "2026-06-29T08:01:00",
            "provider": "azure-openai",
            "model": "shared-model",
            "usage": {"input_tokens": 50, "output_tokens": 5, "cached_tokens": 5},
        },
    ])

    today = collector.snapshot()["today"]

    assert today["by_model"]["shared-model"]["llm_calls"] == 2
    assert today["by_model"]["shared-model"]["total_tokens"] == 165
    assert set(today["by_provider_model"]) == {
        "azure-openai/shared-model",
        "openai/shared-model",
    }
    assert today["by_provider_model"]["openai/shared-model"]["total_tokens"] == 110
    assert today["by_provider_model"]["openai/shared-model"]["cache_rate"] == 0.2
    assert today["by_provider_model"]["azure-openai/shared-model"]["total_tokens"] == 55
    assert today["by_provider_model"]["azure-openai/shared-model"]["cache_rate"] == 0.1


def test_missing_provider_is_bucketed_as_unknown_provider_model():
    collector = _collector()
    collector.on_entry({
        "type": "llm_response",
        "ts": "2026-06-29T08:00:00",
        "model": "legacy-model",
        "usage": {"input_tokens": 10, "output_tokens": 2, "cached_tokens": 3},
    })

    today = collector.snapshot()["today"]

    assert today["by_model"]["legacy-model"]["total_tokens"] == 12
    assert today["by_provider_model"]["unknown/legacy-model"]["provider"] == "unknown"
    assert today["by_provider_model"]["unknown/legacy-model"]["model"] == "legacy-model"
    assert today["by_provider_model"]["unknown/legacy-model"]["cache_rate"] == 0.3


def test_missing_usage_is_counted_as_call_with_zero_tokens():
    collector = _collector()
    collector.on_entry({
        "type": "llm_response",
        "ts": "2026-06-29T08:00:00",
        "model": "unknownish",
    })

    today = collector.snapshot()["today"]

    assert today["llm_calls"] == 1
    assert today["tracked_calls"] == 0
    assert today["untracked_calls"] == 1
    assert today["tracking_coverage"] == 0
    assert today["avg_tokens_per_call"] is None
    assert today["total_tokens"] == 0
    assert today["cache_rate"] is None


def test_windows_use_today_last_7_days_and_lifetime():
    collector = _collector()
    collector.load_entries([
        {
            "type": "llm_response",
            "ts": "2026-06-20T08:00:00",
            "model": "old",
            "usage": {"input_tokens": 10, "output_tokens": 1},
        },
        {
            "type": "llm_response",
            "ts": "2026-06-23T08:00:00",
            "model": "recent",
            "usage": {"input_tokens": 20, "output_tokens": 2},
        },
        {
            "type": "llm_response",
            "ts": "2026-06-29T08:00:00",
            "model": "today",
            "usage": {"input_tokens": 30, "output_tokens": 3},
        },
    ])

    snapshot = collector.report()

    assert snapshot["today"]["input_tokens"] == 30
    assert snapshot["last_7_days"]["input_tokens"] == 50
    assert snapshot["lifetime"]["input_tokens"] == 60
    assert snapshot["last_7_days"]["by_scope"]["main"]["input_tokens"] == 50
    assert snapshot["lifetime"]["by_scope"]["main"]["input_tokens"] == 60


def test_admin_report_adds_30_day_trend_previous_periods_and_tracking_metadata():
    collector = _collector()
    collector.load_entries([
        {
            "type": "llm_response",
            "ts": "2026-05-30T08:00:00",
            "provider": "archive",
            "model": "old-model",
            "usage": {"input_tokens": 90, "output_tokens": 9},
        },
        {
            "type": "llm_response",
            "ts": "2026-05-31T08:00:00",
            "provider": "archive",
            "model": "recent-model",
            "usage": {"input_tokens": 10, "output_tokens": 1},
        },
        {
            "type": "llm_response",
            "ts": "2026-06-22T08:00:00",
            "provider": "previous",
            "model": "baseline",
            "usage": {"input_tokens": 40, "output_tokens": 4},
        },
        {
            "type": "llm_response",
            "ts": "2026-06-28T08:00:00",
            "provider": "openai",
            "model": "gpt-4o",
        },
        {
            "type": "llm_response",
            "ts": "2026-06-29T08:00:00",
            "provider": "openai",
            "model": "gpt-4o",
            "usage": {"input_tokens": 100, "output_tokens": 20, "cached_tokens": 10},
        },
        {
            "type": "mem0_llm_response",
            "ts": "2026-06-29T09:00:00",
            "provider": "openai",
            "model": "gpt-4o-mini",
            "usage": {"input_tokens": 30, "output_tokens": 5},
            "usage_source": "estimated",
        },
    ])

    report = collector.report()

    assert report["last_30_days"]["total_tokens"] == 210
    assert report["last_30_days"]["llm_calls"] == 5
    assert report["last_30_days"]["tracked_calls"] == 4
    assert report["last_30_days"]["exact_calls"] == 3
    assert report["last_30_days"]["untracked_calls"] == 1
    assert report["last_30_days"]["estimated_calls"] == 1
    assert report["last_30_days"]["tracking_coverage"] == 0.8
    assert report["last_30_days"]["exact_coverage"] == 0.6
    assert report["previous"]["today"]["llm_calls"] == 1
    assert report["previous"]["today"]["total_tokens"] == 0
    assert report["previous"]["last_7_days"]["total_tokens"] == 44
    assert report["previous"]["last_30_days"]["total_tokens"] == 99
    assert len(report["daily"]) == 30
    assert report["daily"][0]["date"] == "2026-05-31"
    assert report["daily"][0]["total_tokens"] == 11
    assert report["daily"][-1]["date"] == "2026-06-29"
    assert report["daily"][-1]["total_tokens"] == 155
    assert report["daily"][-1]["estimated_calls"] == 1
    assert report["tracking_since"] == "2026-05-30"
    assert report["generated_at"] == "2026-06-29T12:00:00"


def test_admin_report_adds_an_inclusive_custom_range_with_equal_previous_period():
    collector = _collector()
    collector.load_entries([
        {
            "type": "llm_response",
            "ts": "2026-06-23T08:00:00",
            "provider": "previous",
            "model": "baseline",
            "usage": {"input_tokens": 20, "output_tokens": 2},
        },
        {
            "type": "llm_response",
            "ts": "2026-06-25T08:00:00",
            "provider": "selected",
            "model": "range-model",
            "usage": {"input_tokens": 30, "output_tokens": 3},
        },
        {
            "type": "tool_call",
            "id": "skill-load",
            "ts": "2026-06-27T08:00:00",
            "name": "get_skill",
            "arguments": {"skill_name": "browser"},
        },
        {
            "type": "tool_result",
            "id": "skill-load",
            "ts": "2026-06-27T08:00:01",
            "name": "get_skill",
            "is_error": False,
        },
    ])

    report = collector.report(
        start_date=date(2026, 6, 25),
        end_date=date(2026, 6, 27),
    )
    selected = report["selected_range"]

    assert selected["start_date"] == "2026-06-25"
    assert selected["end_date"] == "2026-06-27"
    assert selected["previous_start_date"] == "2026-06-22"
    assert selected["previous_end_date"] == "2026-06-24"
    assert selected["previous"]["total_tokens"] == 22
    assert selected["stats"]["total_tokens"] == 33
    assert selected["stats"]["by_scope"]["main"]["total_tokens"] == 33
    assert selected["stats"]["by_provider_model"]["selected/range-model"][
        "total_tokens"
    ] == 33
    assert selected["stats"]["tool_successes"] == 1
    assert selected["stats"]["skills"]["browser"]["explicit_successes"] == 1
    assert [item["date"] for item in selected["daily"]] == [
        "2026-06-25",
        "2026-06-26",
        "2026-06-27",
    ]
    assert selected["daily"][1]["total_tokens"] == 0


def test_admin_report_treats_one_custom_date_as_a_single_day():
    collector = _collector()
    collector.load_entries([
        {
            "type": "llm_response",
            "ts": "2026-06-24T08:00:00",
            "usage": {"input_tokens": 5, "output_tokens": 1},
        },
        {
            "type": "llm_response",
            "ts": "2026-06-25T08:00:00",
            "usage": {"input_tokens": 10, "output_tokens": 2},
        },
    ])

    selected = collector.report(start_date=date(2026, 6, 25))["selected_range"]

    assert selected["start_date"] == "2026-06-25"
    assert selected["end_date"] == "2026-06-25"
    assert selected["stats"]["total_tokens"] == 12
    assert selected["previous"]["total_tokens"] == 6
    assert len(selected["daily"]) == 1
    assert len(selected["intraday"]) == 24
    assert selected["intraday"][8]["start_time"] == "2026-06-25T08:00:00"
    assert selected["intraday"][8]["end_time"] == "2026-06-25T08:59:59.999999"
    assert selected["intraday"][8]["total_tokens"] == 12
    assert selected["intraday"][7]["total_tokens"] == 0


def test_intraday_report_combines_main_summary_and_bubble_usage_by_hour():
    collector = _collector()
    collector.on_entry({
        "type": "llm_response",
        "seq": 0,
        "ts": "2026-06-29T08:05:00",
        "model": "main-model",
        "usage": {"input_tokens": 10, "output_tokens": 2},
    })
    collector.on_entry({
        "type": "summary_llm_response",
        "seq": 1,
        "ts": "2026-06-29T13:15:00",
        "model": "summary-model",
        "usage": {"input_tokens": 20, "output_tokens": 3},
    })
    collector.on_entry({
        "type": "llm_response",
        "seq": 0,
        "ts": "2026-06-29T08:45:00",
        "model": "bubble-model",
        "usage": {"input_tokens": 30, "output_tokens": 4},
    }, stream_id="bubble:bubbles/bbl_hourly.jsonl")

    intraday = collector.report()["today_intraday"]

    assert len(intraday) == 24
    assert intraday[8]["input_tokens"] == 40
    assert intraday[8]["output_tokens"] == 6
    assert intraday[8]["llm_calls"] == 2
    assert intraday[13]["total_tokens"] == 23
    assert sum(item["total_tokens"] for item in intraday) == 69


def test_memory_compressions_are_exactly_aggregated_by_window_trigger_and_hour():
    collector = _collector()
    collector.load_entries([
        {
            "type": "memory_compression",
            "seq": 0,
            "ts": "2026-06-23T09:15:00",
            "trigger": "tool",
            "mode": "full",
            "storage": "tree",
            "messages_compressed": 20,
            "duration_ms": 500,
            "summary_calls": 2,
            "summary_tracked_calls": 2,
            "summary_input_tokens": 400,
            "summary_output_tokens": 50,
            "summary_cached_tokens": 100,
        },
        {
            "type": "memory_compression",
            "seq": 1,
            "ts": "2026-06-29T08:10:00",
            "trigger": "automatic",
            "mode": "incremental",
            "storage": "tree",
            "messages_compressed": 4,
            "duration_ms": 120,
            "summary_calls": 2,
            "summary_tracked_calls": 2,
            "summary_input_tokens": 100,
            "summary_output_tokens": 20,
            "summary_cached_tokens": 30,
        },
        {
            "type": "memory_compression",
            "seq": 2,
            "ts": "2026-06-29T13:20:00",
            "trigger": "admin",
            "mode": "full",
            "storage": "tree",
            "messages_compressed": 10,
            "duration_ms": 80,
            "summary_calls": 1,
            "summary_tracked_calls": 0,
        },
    ])

    report = collector.report(
        start_date=date(2026, 6, 29),
        end_date=date(2026, 6, 29),
    )
    today = report["today"]

    assert today["memory_compressions"] == 2
    assert today["messages_compressed"] == 14
    assert today["memory_compression_duration_ms"] == 200
    assert today["avg_memory_compression_duration_ms"] == 100
    assert today["memory_compression_summary_calls"] == 3
    assert today["memory_compression_summary_tracked_calls"] == 2
    assert today["memory_compression_summary_untracked_calls"] == 1
    assert today["memory_compression_summary_tracking_coverage"] == 2 / 3
    assert today["memory_compression_input_tokens"] == 100
    assert today["memory_compression_output_tokens"] == 20
    assert today["memory_compression_cached_tokens"] == 30
    assert today["memory_compression_total_tokens"] == 120
    assert today["memory_compression_triggers"] == {
        "automatic": 1,
        "admin": 1,
        "tool": 0,
        "other": 0,
    }
    assert today["last_memory_compression_at"] == "2026-06-29T13:20:00"
    assert report["last_7_days"]["memory_compressions"] == 3
    assert report["last_7_days"]["memory_compression_triggers"]["tool"] == 1
    assert report["compression_tracking_since"] == "2026-06-23"
    assert report["today_intraday"][8]["memory_compressions"] == 1
    assert report["today_intraday"][13]["memory_compressions"] == 1
    assert report["selected_range"]["stats"]["memory_compressions"] == 2
    assert report["selected_range"]["daily"][0]["messages_compressed"] == 14


def test_public_snapshot_does_not_expose_memory_compression_details():
    collector = _collector()
    collector.on_entry({
        "type": "memory_compression",
        "ts": "2026-06-29T08:10:00",
        "trigger": "automatic",
        "messages_compressed": 4,
        "duration_ms": 120,
        "summary_calls": 1,
        "summary_tracked_calls": 1,
        "summary_input_tokens": 100,
        "summary_output_tokens": 20,
    })

    today = collector.snapshot()["today"]

    assert "memory_compressions" not in today
    assert "memory_compression_triggers" not in today
    assert "last_memory_compression_at" not in today


def test_public_snapshot_stays_compact_when_admin_report_is_available():
    collector = _collector()

    snapshot = collector.snapshot()

    assert "last_30_days" not in snapshot
    assert "previous" not in snapshot
    assert "daily" not in snapshot
    assert "tool_outcomes" not in snapshot["today"]
    assert "skills" not in snapshot["today"]
    assert "bubble_runs" not in snapshot["today"]


def test_admin_report_uses_one_date_across_a_midnight_boundary():
    moments = iter([
        datetime(2026, 6, 29, 23, 59, 59),
        datetime(2026, 6, 30, 0, 0, 0),
    ])
    collector = UsageStatsCollector(now_fn=lambda: next(moments))
    collector.load_entries([{
        "type": "llm_response",
        "ts": "2026-06-29T20:00:00",
        "usage": {"input_tokens": 10, "output_tokens": 2},
    }])

    report = collector.report()

    assert report["generated_at"] == "2026-06-29T23:59:59"
    assert report["today"]["total_tokens"] == 12
    assert report["daily"][-1]["date"] == "2026-06-29"


def test_thinking_start_and_llm_response_measure_average_thinking_time():
    collector = _collector()
    collector.load_entries([
        {
            "type": "thinking_start",
            "ts": "2026-06-29T08:00:00",
            "cycle": 1,
        },
        {
            "type": "llm_response",
            "ts": "2026-06-29T08:00:02.500000",
            "model": "gpt-4o",
            "usage": {"input_tokens": 10, "output_tokens": 2},
        },
        {
            "type": "thinking_start",
            "ts": "2026-06-29T08:01:00",
            "cycle": 2,
        },
        {
            "type": "llm_response",
            "ts": "2026-06-29T08:01:07.500000",
            "model": "gpt-4o",
            "usage": {"input_tokens": 20, "output_tokens": 4},
        },
    ])

    today = collector.report()["today"]

    assert today["thinking_calls"] == 2
    assert today["thinking_seconds"] == 10
    assert today["avg_thinking_seconds"] == 5
    assert today["by_scope"]["main"]["thinking_calls"] == 2
    assert today["by_scope"]["main"]["avg_thinking_seconds"] == 5


def test_thinking_time_windows_use_response_day():
    collector = _collector()
    collector.load_entries([
        {
            "type": "thinking_start",
            "ts": "2026-06-20T08:00:00",
        },
        {
            "type": "llm_response",
            "ts": "2026-06-20T08:00:05",
            "model": "old",
            "usage": {"input_tokens": 1, "output_tokens": 1},
        },
        {
            "type": "thinking_start",
            "ts": "2026-06-23T08:00:00",
        },
        {
            "type": "llm_response",
            "ts": "2026-06-23T08:00:10",
            "model": "recent",
            "usage": {"input_tokens": 1, "output_tokens": 1},
        },
        {
            "type": "thinking_start",
            "ts": "2026-06-29T23:59:58",
        },
        {
            "type": "llm_response",
            "ts": "2026-06-30T00:00:02",
            "model": "tomorrow",
            "usage": {"input_tokens": 1, "output_tokens": 1},
        },
        {
            "type": "thinking_start",
            "ts": "2026-06-29T08:00:00",
        },
        {
            "type": "llm_response",
            "ts": "2026-06-29T08:00:20",
            "model": "today",
            "usage": {"input_tokens": 1, "output_tokens": 1},
        },
    ])

    snapshot = collector.report()

    assert snapshot["today"]["thinking_calls"] == 1
    assert snapshot["today"]["thinking_seconds"] == 20
    assert snapshot["last_7_days"]["thinking_calls"] == 2
    assert snapshot["last_7_days"]["thinking_seconds"] == 30
    assert snapshot["last_7_days"]["avg_thinking_seconds"] == 15
    assert snapshot["lifetime"]["thinking_calls"] == 4
    assert snapshot["lifetime"]["thinking_seconds"] == 39


def test_tool_calls_are_counted_and_sorted_by_frequency():
    collector = _collector()
    collector.load_entries([
        {"type": "tool_call", "ts": "2026-06-29T08:00:00", "name": "read_file"},
        {"type": "tool_call", "ts": "2026-06-29T08:01:00", "name": "grep_files"},
        {"type": "tool_call", "ts": "2026-06-29T08:02:00", "name": "read_file"},
    ])

    tools = collector.snapshot()["today"]["tools"]

    assert collector.snapshot()["today"]["tool_calls"] == 3
    assert list(tools) == ["read_file", "grep_files"]
    assert tools["read_file"] == 2


def test_tool_results_are_paired_per_stream_and_report_verifiable_outcomes():
    collector = _collector()
    collector.load_entries([
        {
            "type": "tool_call",
            "id": "call-1",
            "ts": "2026-06-29T08:00:00",
            "name": "read_file",
        },
        {
            "type": "tool_result",
            "id": "call-1",
            "ts": "2026-06-29T08:00:01",
            "name": "read_file",
            "is_error": False,
        },
        {
            "type": "tool_call",
            "id": "call-2",
            "ts": "2026-06-29T08:01:00",
            "name": "read_file",
        },
        {
            "type": "tool_result",
            "id": "call-2",
            "ts": "2026-06-29T08:01:01",
            "name": "read_file",
            "is_error": True,
        },
        {
            "type": "tool_call",
            "id": "call-3",
            "ts": "2026-06-29T08:02:00",
            "name": "grep_files",
        },
        {
            "type": "tool_result",
            "id": "orphan",
            "ts": "2026-06-29T08:03:00",
            "name": "grep_files",
            "is_error": False,
        },
    ])

    today = collector.report()["today"]

    assert today["tool_calls"] == 3
    assert today["tool_successes"] == 1
    assert today["tool_errors"] == 1
    assert today["tool_incomplete"] == 1
    assert today["tool_success_rate"] == 0.5
    assert today["tool_outcomes"]["read_file"] == {
        "calls": 2,
        "successes": 1,
        "errors": 1,
        "incomplete": 0,
        "success_rate": 0.5,
    }
    assert today["tool_outcomes"]["grep_files"]["incomplete"] == 1


def test_tool_result_pairing_isolated_by_stream_and_attributed_to_call_day():
    collector = _collector()
    collector.on_entry({
        "type": "tool_call",
        "id": "shared",
        "seq": 0,
        "ts": "2026-06-28T23:59:59",
        "name": "main_tool",
    })
    collector.on_entry({
        "type": "tool_call",
        "id": "shared",
        "seq": 0,
        "ts": "2026-06-29T00:00:00",
        "name": "bubble_tool",
    }, stream_id="bubble:bubbles/bbl_a.jsonl")
    collector.on_entry({
        "type": "tool_result",
        "id": "shared",
        "seq": 1,
        "ts": "2026-06-29T00:00:01",
        "name": "bubble_tool",
        "is_error": False,
    }, stream_id="bubble:bubbles/bbl_a.jsonl")
    collector.on_entry({
        "type": "tool_result",
        "id": "shared",
        "seq": 1,
        "ts": "2026-06-29T00:00:02",
        "name": "main_tool",
        "is_error": True,
    })

    snapshot = collector.report()

    assert snapshot["today"]["tool_calls"] == 1
    assert snapshot["today"]["tool_successes"] == 1
    assert snapshot["today"]["tool_errors"] == 0
    assert snapshot["today"]["by_scope"]["bubble"]["tool_successes"] == 1
    assert snapshot["lifetime"]["by_scope"]["main"]["tool_errors"] == 1


def test_skill_loads_distinguish_explicit_results_and_palace_automatic_loads():
    collector = _collector()
    collector.load_entries([
        {
            "type": "tool_call",
            "id": "skill-ok",
            "ts": "2026-06-29T08:00:00",
            "name": "get_skill",
            "arguments": {"skill_name": "browser"},
        },
        {
            "type": "tool_result",
            "id": "skill-ok",
            "ts": "2026-06-29T08:00:01",
            "name": "get_skill",
            "is_error": False,
        },
        {
            "type": "tool_call",
            "id": "skill-error",
            "ts": "2026-06-29T08:01:00",
            "name": "get_skill",
            "arguments": {"skill_name": "missing-skill"},
        },
        {
            "type": "tool_result",
            "id": "skill-error",
            "ts": "2026-06-29T08:01:01",
            "name": "get_skill",
            "is_error": True,
        },
        {
            "type": "palace_injection",
            "ts": "2026-06-29T08:02:00",
            "critical_skills": ["browser", "research"],
        },
    ])

    today = collector.report()["today"]

    assert today["skill_load_attempts"] == 2
    assert today["skill_load_successes"] == 1
    assert today["skill_load_errors"] == 1
    assert today["skill_load_incomplete"] == 0
    assert today["automatic_skill_loads"] == 2
    assert today["skills"]["browser"] == {
        "explicit_attempts": 1,
        "explicit_successes": 1,
        "explicit_errors": 0,
        "explicit_incomplete": 0,
        "automatic_loads": 1,
    }
    assert today["skills"]["missing-skill"]["explicit_errors"] == 1
    assert today["skills"]["research"]["automatic_loads"] == 1


def test_bubble_terminal_metadata_reports_technical_outcomes_by_scope():
    collector = _collector()
    collector.on_entry({
        "__meta__": True,
        "seq": 0,
        "ts": "2026-06-29T08:00:00",
        "status": "done",
        "cycles_used": 3,
        "max_cycles": 8,
        "elapsed_seconds": 12.5,
        "resume_count": 1,
    }, stream_id="bubble:bubbles/bbl_a.jsonl")
    collector.on_entry({
        "__meta__": True,
        "seq": 0,
        "ts": "2026-06-29T09:00:00",
        "status": "timeout",
        "cycles_used": 5,
        "max_cycles": 5,
        "elapsed_seconds": 27.5,
        "resume_count": 0,
    }, stream_id="bubble:subconscious/bubbles/bbl_s_audit.jsonl")

    today = collector.report()["today"]

    assert today["bubble_runs"] == 2
    assert today["bubble_done"] == 1
    assert today["bubble_timeouts"] == 1
    assert today["bubble_errors"] == 0
    assert today["bubble_cancelled"] == 0
    assert today["bubble_cycles"] == 8
    assert today["avg_bubble_cycles"] == 4
    assert today["bubble_elapsed_seconds"] == 40
    assert today["avg_bubble_seconds"] == 20
    assert today["bubble_resumes"] == 1
    assert today["bubble_max_cycles_reached"] == 1
    assert today["by_scope"]["bubble"]["bubble_done"] == 1
    assert today["by_scope"]["subconscious"]["bubble_timeouts"] == 1


def test_summary_and_vision_llm_responses_are_scoped():
    collector = _collector()
    collector.load_entries([
        {
            "type": "summary_llm_response",
            "ts": "2026-06-29T08:00:00",
            "provider": "openai",
            "model": "gpt-4o-mini",
            "usage": {"input_tokens": 100, "output_tokens": 20, "cached_tokens": 10},
        },
        {
            "type": "vision_llm_response",
            "ts": "2026-06-29T08:01:00",
            "provider": "openai",
            "model": "gpt-4o",
            "usage": {"input_tokens": 40, "output_tokens": 6, "cached_tokens": 4},
        },
    ])

    today = collector.snapshot()["today"]

    assert today["total_tokens"] == 166
    assert today["llm_calls"] == 2
    assert today["by_scope"]["summary"]["total_tokens"] == 120
    assert today["by_scope"]["summary"]["llm_calls"] == 1
    summary_model = today["by_scope"]["summary"]["by_provider_model"]["openai/gpt-4o-mini"]
    assert summary_model["total_tokens"] == 120
    assert today["by_scope"]["vision"]["total_tokens"] == 46
    assert today["by_scope"]["vision"]["llm_calls"] == 1
    assert today["by_scope"]["vision"]["by_provider_model"]["openai/gpt-4o"]["total_tokens"] == 46
    assert today["by_scope"]["main"]["total_tokens"] == 0


def test_bubble_summary_and_vision_usage_stays_in_originating_scope():
    collector = _collector()
    collector.on_entry(
        {
            "type": "summary_llm_response",
            "ts": "2026-06-29T08:00:00",
            "provider": "openai",
            "model": "gpt-4o-mini",
            "usage": {"input_tokens": 100, "output_tokens": 20},
        },
        stream_id="bubble:bubbles/bbl_test.jsonl",
    )
    collector.on_entry(
        {
            "type": "vision_llm_response",
            "ts": "2026-06-29T08:01:00",
            "provider": "openai",
            "model": "gpt-4o",
            "usage": {"input_tokens": 40, "output_tokens": 6},
        },
        stream_id="bubble:subconscious/bubbles/bbl_test_audit.jsonl",
    )

    today = collector.snapshot()["today"]

    assert today["by_scope"]["bubble"]["total_tokens"] == 120
    assert today["by_scope"]["subconscious"]["total_tokens"] == 46
    assert today["by_scope"]["summary"]["total_tokens"] == 0
    assert today["by_scope"]["vision"]["total_tokens"] == 0


def test_mem0_llm_responses_are_scoped():
    collector = _collector()
    collector.on_entry({
        "type": "mem0_llm_response",
        "ts": "2026-06-29T08:00:00",
        "provider": "anthropic",
        "model": "claude-haiku",
        "usage": {"input_tokens": 30, "output_tokens": 7},
    })

    today = collector.snapshot()["today"]

    assert today["total_tokens"] == 37
    assert today["llm_calls"] == 1
    assert today["by_scope"]["mem0"]["total_tokens"] == 37
    assert today["by_scope"]["mem0"]["llm_calls"] == 1
    assert today["by_scope"]["main"]["total_tokens"] == 0
    assert today["by_scope"]["mem0"]["by_provider_model"]["anthropic/claude-haiku"]["total_tokens"] == 37


def test_summary_and_vision_usage_rebuilds_from_interaction_log(tmp_path):
    state_path = tmp_path / "usage_stats.json"
    _write_jsonl(tmp_path / "interactions.jsonl", [
        {
            "type": "summary_llm_response",
            "seq": 0,
            "ts": "2026-06-29T08:00:00",
            "provider": "mock",
            "model": "summary-model",
            "usage": {"input_tokens": 7, "output_tokens": 3},
        },
        {
            "type": "vision_llm_response",
            "seq": 1,
            "ts": "2026-06-29T08:01:00",
            "provider": "mock",
            "model": "vision-model",
            "usage": {"input_tokens": 11, "output_tokens": 5},
        },
    ])

    first = UsageStatsCollector(
        LogStore(tmp_path),
        now_fn=lambda: datetime(2026, 6, 29),
        state_path=state_path,
    )
    second = UsageStatsCollector(
        LogStore(tmp_path),
        now_fn=lambda: datetime(2026, 6, 29),
        state_path=state_path,
    )

    assert first.snapshot()["lifetime"]["total_tokens"] == 26
    assert second.snapshot()["lifetime"]["total_tokens"] == 26
    assert second.snapshot()["lifetime"]["by_scope"]["summary"]["total_tokens"] == 10
    assert second.snapshot()["lifetime"]["by_scope"]["vision"]["total_tokens"] == 16


def test_load_history_prefers_streaming_iterator_over_read_all():
    class FakeLogStore:
        def iter_all_entries(self):
            yield {
                "type": "llm_response",
                "ts": "2026-06-29T08:00:00",
                "model": "gpt-4o",
                "usage": {"input_tokens": 3, "output_tokens": 4},
            }

        def read_all(self):
            raise AssertionError("read_all should not be used when streaming is available")

    collector = UsageStatsCollector(FakeLogStore(), now_fn=lambda: datetime(2026, 6, 29))

    assert collector.snapshot()["today"]["total_tokens"] == 7


def test_state_file_makes_restart_incremental_without_double_counting(tmp_path):
    log_path = tmp_path / "interactions.jsonl"
    state_path = tmp_path / "usage_stats.json"
    _write_jsonl(log_path, [
        {
            "type": "llm_response",
            "seq": 0,
            "ts": "2026-06-29T08:00:00",
            "model": "gpt-4o",
            "usage": {"input_tokens": 10, "output_tokens": 1},
        }
    ])

    first = UsageStatsCollector(
        LogStore(tmp_path),
        now_fn=lambda: datetime(2026, 6, 29),
        state_path=state_path,
    )
    assert first.snapshot()["lifetime"]["total_tokens"] == 11

    with log_path.open("a", encoding="utf-8") as f:
        f.write(json.dumps({
            "type": "llm_response",
            "seq": 1,
            "ts": "2026-06-29T08:01:00",
            "model": "gpt-4o",
            "usage": {"input_tokens": 3, "output_tokens": 4},
        }, ensure_ascii=False) + "\n")

    second = UsageStatsCollector(
        LogStore(tmp_path),
        now_fn=lambda: datetime(2026, 6, 29),
        state_path=state_path,
    )

    assert second.snapshot()["lifetime"]["total_tokens"] == 18


def test_empty_cold_start_checkpoint_does_not_skip_first_entry(tmp_path):
    state_path = tmp_path / "usage_stats.json"
    UsageStatsCollector(
        LogStore(tmp_path),
        now_fn=lambda: datetime(2026, 6, 29),
        state_path=state_path,
    )
    _write_jsonl(tmp_path / "interactions.jsonl", [
        {
            "type": "llm_response",
            "seq": 0,
            "ts": "2026-06-29T08:00:00",
            "model": "gpt-4o",
            "usage": {"input_tokens": 1, "output_tokens": 1},
        }
    ])

    collector = UsageStatsCollector(
        LogStore(tmp_path),
        now_fn=lambda: datetime(2026, 6, 29),
        state_path=state_path,
    )

    assert collector.snapshot()["lifetime"]["total_tokens"] == 2


def test_runtime_entry_persists_seq_checkpoint(tmp_path):
    state_path = tmp_path / "usage_stats.json"
    collector = UsageStatsCollector(
        now_fn=lambda: datetime(2026, 6, 29),
        state_path=state_path,
    )

    collector.on_entry({
        "type": "tool_call",
        "seq": 9,
        "ts": "2026-06-29T08:00:00",
        "name": "read_file",
    })

    state = json.loads(state_path.read_text(encoding="utf-8"))
    assert state["checkpoint"] == {"seq": 9}
    assert state["checkpoints"]["main"] == {"seq": 9}
    assert state["schema_version"] == 10
    assert state["lifetime_by_scope"]["main"]["tool_calls"] == 1


def test_pending_tool_call_survives_restart_and_settles_on_original_day(tmp_path):
    state_path = tmp_path / "usage_stats.json"
    first = UsageStatsCollector(
        now_fn=lambda: datetime(2026, 6, 29),
        state_path=state_path,
    )
    first.on_entry({
        "type": "tool_call",
        "id": "call-across-restart",
        "seq": 0,
        "ts": "2026-06-28T23:59:59",
        "name": "get_skill",
        "arguments": {"skill_name": "browser"},
    })

    second = UsageStatsCollector(
        LogStore(tmp_path),
        now_fn=lambda: datetime(2026, 6, 29),
        state_path=state_path,
    )
    second.on_entry({
        "type": "tool_result",
        "id": "call-across-restart",
        "seq": 1,
        "ts": "2026-06-29T00:00:01",
        "name": "get_skill",
        "is_error": False,
    })

    snapshot = second.report()

    assert snapshot["today"]["tool_calls"] == 0
    assert snapshot["today"]["tool_successes"] == 0
    assert snapshot["lifetime"]["tool_successes"] == 1
    assert snapshot["lifetime"]["skill_load_successes"] == 1
    persisted = json.loads(state_path.read_text(encoding="utf-8"))
    assert persisted["pending_tool_calls"] == {}


def test_different_streams_can_share_seq_values():
    collector = _collector()
    collector.on_entry({
        "type": "llm_response",
        "seq": 0,
        "ts": "2026-06-29T08:00:00",
        "model": "main-model",
        "usage": {"input_tokens": 10, "output_tokens": 1},
    })
    collector.on_entry({
        "type": "llm_response",
        "seq": 0,
        "ts": "2026-06-29T08:01:00",
        "model": "bubble-model",
        "usage": {"input_tokens": 20, "output_tokens": 2},
    }, stream_id="bubble:bubbles/bbl_a.jsonl")

    today = collector.snapshot()["today"]

    assert today["llm_calls"] == 2
    assert today["total_tokens"] == 33
    assert set(today["by_model"]) == {"main-model", "bubble-model"}
    assert today["by_scope"]["main"]["total_tokens"] == 11
    assert today["by_scope"]["bubble"]["total_tokens"] == 22
    assert today["by_scope"]["subconscious"]["total_tokens"] == 0


def test_usage_stats_by_scope_splits_main_bubble_and_subconscious():
    collector = _collector()
    collector.on_entry({
        "type": "thinking_start",
        "seq": 0,
        "ts": "2026-06-29T07:59:57",
    })
    collector.on_entry({
        "type": "llm_response",
        "seq": 1,
        "ts": "2026-06-29T08:00:00",
        "provider": "openai",
        "model": "main-model",
        "usage": {"input_tokens": 10, "output_tokens": 1, "cached_tokens": 2},
    })
    collector.on_entry({
        "type": "tool_call",
        "seq": 2,
        "ts": "2026-06-29T08:01:00",
        "name": "read_file",
    })
    collector.on_entry({
        "type": "thinking_start",
        "seq": 0,
        "ts": "2026-06-29T08:01:55",
    }, stream_id="bubble:bubbles/bbl_a.jsonl")
    collector.on_entry({
        "type": "llm_response",
        "seq": 1,
        "ts": "2026-06-29T08:02:00",
        "provider": "mock",
        "model": "bubble-model",
        "usage": {"input_tokens": 20, "output_tokens": 2},
    }, stream_id="bubble:bubbles/bbl_a.jsonl")
    collector.on_entry({
        "type": "tool_call",
        "seq": 2,
        "ts": "2026-06-29T08:03:00",
        "name": "bubble_done",
    }, stream_id="bubble:bubbles/bbl_a.jsonl")
    collector.on_entry({
        "type": "thinking_start",
        "seq": 0,
        "ts": "2026-06-29T08:03:52",
    }, stream_id="bubble:subconscious/bubbles/bbl_s_audit.jsonl")
    collector.on_entry({
        "type": "llm_response",
        "seq": 1,
        "ts": "2026-06-29T08:04:00",
        "provider": "mock",
        "model": "sub-model",
        "usage": {"input_tokens": 30, "output_tokens": 3},
    }, stream_id="bubble:subconscious/bubbles/bbl_s_audit.jsonl")

    today = collector.snapshot()["today"]

    assert today["total_tokens"] == 66
    assert today["llm_calls"] == 3
    assert today["tool_calls"] == 2
    assert today["thinking_calls"] == 3
    assert today["thinking_seconds"] == 16
    assert today["by_scope"]["main"]["total_tokens"] == 11
    assert today["by_scope"]["main"]["tool_calls"] == 1
    assert today["by_scope"]["main"]["cache_rate"] == 0.2
    assert today["by_scope"]["main"]["avg_thinking_seconds"] == 3
    assert today["by_scope"]["bubble"]["total_tokens"] == 22
    assert today["by_scope"]["bubble"]["tools"] == {"bubble_done": 1}
    assert today["by_scope"]["bubble"]["avg_thinking_seconds"] == 5
    assert today["by_scope"]["subconscious"]["total_tokens"] == 33
    assert today["by_scope"]["subconscious"]["avg_thinking_seconds"] == 8
    assert today["by_scope"]["subconscious"]["by_provider_model"]["mock/sub-model"]["total_tokens"] == 33


def test_invalid_or_unpaired_thinking_times_are_ignored():
    collector = _collector()
    collector.load_entries([
        {
            "type": "llm_response",
            "ts": "2026-06-29T08:00:00",
            "model": "no-start",
            "usage": {"input_tokens": 1, "output_tokens": 1},
        },
        {
            "type": "thinking_start",
            "ts": "not-a-time",
        },
        {
            "type": "llm_response",
            "ts": "2026-06-29T08:00:01",
            "model": "invalid-start",
            "usage": {"input_tokens": 1, "output_tokens": 1},
        },
        {
            "type": "thinking_start",
            "ts": "2026-06-29T08:00:10",
        },
        {
            "type": "llm_response",
            "ts": "2026-06-29T08:00:09",
            "model": "negative",
            "usage": {"input_tokens": 1, "output_tokens": 1},
        },
    ])

    today = collector.snapshot()["today"]

    assert today["llm_calls"] == 3
    assert today["thinking_calls"] == 0
    assert today["thinking_seconds"] == 0
    assert today["avg_thinking_seconds"] is None


def test_pending_thinking_start_survives_restart(tmp_path):
    log_path = tmp_path / "interactions.jsonl"
    state_path = tmp_path / "usage_stats.json"
    _write_jsonl(log_path, [
        {
            "type": "thinking_start",
            "seq": 0,
            "ts": "2026-06-29T08:00:00",
            "cycle": 1,
        },
    ])

    first = UsageStatsCollector(
        LogStore(tmp_path),
        now_fn=lambda: datetime(2026, 6, 29),
        state_path=state_path,
    )
    assert first.snapshot()["lifetime"]["thinking_calls"] == 0

    with log_path.open("a", encoding="utf-8") as f:
        f.write(json.dumps({
            "type": "llm_response",
            "seq": 1,
            "ts": "2026-06-29T08:00:04",
            "model": "gpt-4o",
            "usage": {"input_tokens": 3, "output_tokens": 4},
        }, ensure_ascii=False) + "\n")

    second = UsageStatsCollector(
        LogStore(tmp_path),
        now_fn=lambda: datetime(2026, 6, 29),
        state_path=state_path,
    )

    assert second.snapshot()["lifetime"]["thinking_calls"] == 1
    assert second.snapshot()["lifetime"]["avg_thinking_seconds"] == 4


def test_unknown_scope_only_appears_when_it_has_data():
    collector = _collector()

    assert "unknown" not in collector.snapshot()["today"]["by_scope"]

    collector.on_entry({
        "type": "tool_call",
        "seq": 0,
        "ts": "2026-06-29T08:00:00",
        "name": "mystery",
    }, stream_id="bubble:other/place.jsonl")

    today = collector.snapshot()["today"]
    assert today["by_scope"]["unknown"]["tool_calls"] == 1


def test_stream_checkpoint_prevents_duplicate_bubble_entries_after_restart(tmp_path):
    state_path = tmp_path / "usage_stats.json"
    first = UsageStatsCollector(now_fn=lambda: datetime(2026, 6, 29), state_path=state_path)
    first.on_entry({
        "type": "llm_response",
        "seq": 0,
        "ts": "2026-06-29T08:00:00",
        "model": "bubble-model",
        "usage": {"input_tokens": 10, "output_tokens": 1},
    }, stream_id="bubble:bubbles/bbl_a.jsonl")

    second = UsageStatsCollector(
        LogStore(tmp_path),
        now_fn=lambda: datetime(2026, 6, 29),
        state_path=state_path,
    )
    second.on_entry({
        "type": "llm_response",
        "seq": 0,
        "ts": "2026-06-29T08:00:00",
        "model": "bubble-model",
        "usage": {"input_tokens": 10, "output_tokens": 1},
    }, stream_id="bubble:bubbles/bbl_a.jsonl")
    second.on_entry({
        "type": "llm_response",
        "seq": 1,
        "ts": "2026-06-29T08:01:00",
        "model": "bubble-model",
        "usage": {"input_tokens": 2, "output_tokens": 3},
    }, stream_id="bubble:bubbles/bbl_a.jsonl")

    assert second.snapshot()["lifetime"]["total_tokens"] == 16
    assert second.snapshot()["lifetime"]["by_scope"]["bubble"]["total_tokens"] == 16


def test_old_state_schema_is_rebuilt_from_logs_for_scope_split(tmp_path):
    state_path = tmp_path / "usage_stats.json"
    old_model_bucket = {
        "llm_calls": 1,
        "input_tokens": 10,
        "output_tokens": 1,
        "cached_tokens": 0,
    }
    old_bucket = {
        **old_model_bucket,
        "tool_calls": 0,
        "by_model": {"old-main": old_model_bucket},
        "tools": {},
    }
    state_path.write_text(json.dumps({
        "schema_version": 4,
        "updated_at": "2026-06-29T08:00:00",
        "checkpoint": {"seq": 3},
        "lifetime": old_bucket,
        "days": {"2026-06-29": old_bucket},
    }, ensure_ascii=False), encoding="utf-8")
    _write_jsonl(tmp_path / "interactions.jsonl", [
        {
            "type": "llm_response",
            "seq": 0,
            "ts": "2026-06-29T08:00:00",
            "model": "rebuilt-main",
            "usage": {"input_tokens": 4, "output_tokens": 1},
        },
    ])
    bubble_dir = tmp_path / "bubbles"
    bubble_dir.mkdir()
    _write_jsonl(bubble_dir / "bbl_a.jsonl", [
        {
            "type": "llm_response",
            "seq": 0,
            "ts": "2026-06-29T08:10:00",
            "model": "bubble-model",
            "usage": {"input_tokens": 2, "output_tokens": 3},
        },
        {
            "type": "tool_call",
            "seq": 1,
            "ts": "2026-06-29T08:11:00",
            "name": "bubble_done",
        },
    ])

    collector = UsageStatsCollector(
        LogStore(tmp_path),
        now_fn=lambda: datetime(2026, 6, 29),
        state_path=state_path,
    )
    collector.load_bubble_history(tmp_path)

    lifetime = collector.snapshot()["lifetime"]
    migrated = json.loads(state_path.read_text(encoding="utf-8"))

    assert lifetime["total_tokens"] == 10
    assert lifetime["tool_calls"] == 1
    assert "unknown/old-main" not in lifetime["by_provider_model"]
    assert lifetime["by_provider_model"]["unknown/rebuilt-main"]["total_tokens"] == 5
    assert lifetime["by_provider_model"]["unknown/bubble-model"]["total_tokens"] == 5
    assert lifetime["by_scope"]["main"]["total_tokens"] == 5
    assert lifetime["by_scope"]["bubble"]["total_tokens"] == 5
    assert migrated["schema_version"] == 10
    assert migrated["checkpoint"] == {"seq": 0}
    assert "bubble:bubbles/bbl_a.jsonl" not in migrated["checkpoints"]
    assert migrated["bubble_history"]["path"] == "bubbles/bbl_a.jsonl"


def test_v10_state_loads_provider_model_scope_thinking_tracking_and_hour_buckets(tmp_path):
    state_path = tmp_path / "usage_stats.json"
    model_bucket = {
        "llm_calls": 2,
        "tracked_calls": 2,
        "estimated_calls": 0,
        "input_tokens": 20,
        "output_tokens": 5,
        "cached_tokens": 4,
    }
    old_bucket = {
        "llm_calls": 2,
        "tracked_calls": 2,
        "estimated_calls": 0,
        "input_tokens": 20,
        "output_tokens": 5,
        "cached_tokens": 4,
        "tool_calls": 0,
        "thinking_calls": 2,
        "thinking_seconds": 12.0,
        "by_model": {"legacy-model": model_bucket},
        "tools": {},
    }
    state_path.write_text(json.dumps({
        "schema_version": 10,
        "updated_at": "2026-06-29T08:00:00",
        "checkpoint": {"seq": -1},
        "checkpoints": {"main": {"seq": -1}},
        "pending_thinking_starts": {},
        "bubble_history": {"scanned": True},
        "lifetime": old_bucket,
        "days": {"2026-06-29": old_bucket},
        "hours": {"2026-06-29T08:00:00": old_bucket},
        "lifetime_by_scope": {"main": old_bucket},
        "days_by_scope": {"2026-06-29": {"main": old_bucket}},
    }, ensure_ascii=False), encoding="utf-8")

    collector = UsageStatsCollector(
        LogStore(tmp_path),
        now_fn=lambda: datetime(2026, 6, 29),
        state_path=state_path,
    )

    lifetime = collector.snapshot()["lifetime"]
    migrated = json.loads(state_path.read_text(encoding="utf-8"))

    assert lifetime["by_model"]["legacy-model"]["total_tokens"] == 25
    assert lifetime["by_provider_model"]["unknown/legacy-model"]["provider"] == "unknown"
    assert lifetime["by_provider_model"]["unknown/legacy-model"]["model"] == "legacy-model"
    assert lifetime["by_provider_model"]["unknown/legacy-model"]["total_tokens"] == 25
    assert lifetime["by_provider_model"]["unknown/legacy-model"]["cache_rate"] == 0.2
    assert lifetime["thinking_calls"] == 2
    assert lifetime["thinking_seconds"] == 12
    assert lifetime["avg_thinking_seconds"] == 6
    assert lifetime["tracking_coverage"] == 1
    assert lifetime["by_scope"]["main"]["by_provider_model"]["unknown/legacy-model"]["total_tokens"] == 25
    assert migrated["schema_version"] == 10
    assert collector.report()["today_intraday"][8]["total_tokens"] == 25


def test_v8_state_is_rebuilt_from_logs_for_intraday_buckets(tmp_path):
    state_path = tmp_path / "usage_stats.json"
    stale_bucket = _new_empty_test_bucket()
    stale_bucket["llm_calls"] = 99
    state_path.write_text(json.dumps({
        "schema_version": 8,
        "checkpoint": {"seq": 99},
        "checkpoints": {"main": {"seq": 99}},
        "lifetime": stale_bucket,
        "days": {"2026-06-29": stale_bucket},
        "lifetime_by_scope": {"main": stale_bucket},
        "days_by_scope": {"2026-06-29": {"main": stale_bucket}},
    }, ensure_ascii=False), encoding="utf-8")
    _write_jsonl(tmp_path / "interactions.jsonl", [{
        "type": "llm_response",
        "seq": 0,
        "ts": "2026-06-29T14:20:00",
        "model": "rebuilt-model",
        "usage": {"input_tokens": 7, "output_tokens": 3},
    }])

    collector = UsageStatsCollector(
        LogStore(tmp_path),
        now_fn=lambda: datetime(2026, 6, 29),
        state_path=state_path,
    )
    migrated = json.loads(state_path.read_text(encoding="utf-8"))

    assert collector.snapshot()["lifetime"]["llm_calls"] == 1
    assert collector.report()["today_intraday"][14]["total_tokens"] == 10
    assert migrated["schema_version"] == 10
    assert migrated["hours"]["2026-06-29T14:00:00"]["input_tokens"] == 7


def test_v5_state_is_rebuilt_from_logs_for_thinking_time(tmp_path):
    state_path = tmp_path / "usage_stats.json"
    old_bucket = _new_empty_test_bucket()
    old_bucket["llm_calls"] = 99
    state_path.write_text(json.dumps({
        "schema_version": 5,
        "updated_at": "2026-06-29T08:00:00",
        "checkpoint": {"seq": 99},
        "checkpoints": {"main": {"seq": 99}},
        "bubble_history": {"scanned": True},
        "lifetime": old_bucket,
        "days": {"2026-06-29": old_bucket},
        "lifetime_by_scope": {"main": old_bucket},
        "days_by_scope": {"2026-06-29": {"main": old_bucket}},
    }, ensure_ascii=False), encoding="utf-8")
    _write_jsonl(tmp_path / "interactions.jsonl", [
        {
            "type": "thinking_start",
            "seq": 0,
            "ts": "2026-06-29T08:00:00",
        },
        {
            "type": "llm_response",
            "seq": 1,
            "ts": "2026-06-29T08:00:06",
            "model": "rebuilt-main",
            "usage": {"input_tokens": 4, "output_tokens": 1},
        },
    ])

    collector = UsageStatsCollector(
        LogStore(tmp_path),
        now_fn=lambda: datetime(2026, 6, 29),
        state_path=state_path,
    )
    lifetime = collector.snapshot()["lifetime"]
    migrated = json.loads(state_path.read_text(encoding="utf-8"))

    assert lifetime["llm_calls"] == 1
    assert lifetime["total_tokens"] == 5
    assert lifetime["thinking_calls"] == 1
    assert lifetime["avg_thinking_seconds"] == 6
    assert migrated["schema_version"] == 10


def test_mark_bubble_log_complete_compacts_stream_checkpoint(tmp_path):
    state_path = tmp_path / "usage_stats.json"
    bubble_dir = tmp_path / "bubbles"
    bubble_dir.mkdir()
    log_path = bubble_dir / "bbl_done.jsonl"
    _write_jsonl(log_path, [
        {
            "type": "llm_response",
            "seq": 0,
            "ts": "2026-06-29T08:00:00",
            "model": "bubble-model",
            "usage": {"input_tokens": 1, "output_tokens": 2},
        },
    ])
    collector = UsageStatsCollector(now_fn=lambda: datetime(2026, 6, 29), state_path=state_path)
    collector.on_entry({
        "type": "llm_response",
        "seq": 0,
        "ts": "2026-06-29T08:00:00",
        "model": "bubble-model",
        "usage": {"input_tokens": 1, "output_tokens": 2},
    }, stream_id="bubble:bubbles/bbl_done.jsonl")

    collector.mark_bubble_log_complete(tmp_path, log_path)
    state = json.loads(state_path.read_text(encoding="utf-8"))

    assert collector.snapshot()["lifetime"]["total_tokens"] == 3
    assert collector.snapshot()["lifetime"]["by_scope"]["bubble"]["total_tokens"] == 3
    assert "bubble:bubbles/bbl_done.jsonl" not in state["checkpoints"]
    assert state["bubble_history"]["path"] == "bubbles/bbl_done.jsonl"


def test_load_bubble_history_splits_bubble_and_subconscious_scopes(tmp_path):
    state_path = tmp_path / "usage_stats.json"
    bubble_dir = tmp_path / "bubbles"
    subconscious_dir = tmp_path / "subconscious" / "bubbles"
    bubble_dir.mkdir()
    subconscious_dir.mkdir(parents=True)
    _write_jsonl(bubble_dir / "bbl_a.jsonl", [
        {
            "type": "llm_response",
            "seq": 0,
            "ts": "2026-06-29T08:00:00",
            "model": "bubble-model",
            "usage": {"input_tokens": 10, "output_tokens": 1},
        },
    ])
    _write_jsonl(subconscious_dir / "bbl_s_audit.jsonl", [
        {
            "type": "llm_response",
            "seq": 0,
            "ts": "2026-06-29T08:05:00",
            "model": "sub-model",
            "usage": {"input_tokens": 20, "output_tokens": 2},
        },
        {
            "type": "tool_call",
            "seq": 1,
            "ts": "2026-06-29T08:06:00",
            "name": "bubble_done",
        },
    ])

    collector = UsageStatsCollector(now_fn=lambda: datetime(2026, 6, 29), state_path=state_path)
    collector.load_bubble_history(tmp_path)
    collector.load_bubble_history(tmp_path)

    lifetime = collector.snapshot()["lifetime"]
    assert lifetime["total_tokens"] == 33
    assert lifetime["by_scope"]["bubble"]["total_tokens"] == 11
    assert lifetime["by_scope"]["subconscious"]["total_tokens"] == 22
    assert lifetime["by_scope"]["subconscious"]["tools"] == {"bubble_done": 1}


def test_empty_bubble_history_marks_scanned(tmp_path):
    state_path = tmp_path / "usage_stats.json"
    collector = UsageStatsCollector(now_fn=lambda: datetime(2026, 6, 29), state_path=state_path)

    collector.load_bubble_history(tmp_path)
    state = json.loads(state_path.read_text(encoding="utf-8"))

    assert state["bubble_history"] == {"scanned": True}


def test_scanned_history_loads_pending_stream_without_glob(tmp_path):
    state_path = tmp_path / "usage_stats.json"
    state_path.write_text(json.dumps({
        "schema_version": 10,
        "updated_at": "2026-06-29T08:00:00",
        "checkpoint": {"seq": -1},
        "checkpoints": {
            "main": {"seq": -1},
            "bubble:bubbles/bbl_pending.jsonl": {"seq": 0},
        },
        "pending_thinking_starts": {},
        "bubble_history": {"scanned": True},
        "lifetime": _new_empty_test_bucket(),
        "days": {},
        "lifetime_by_scope": {},
        "days_by_scope": {},
    }, ensure_ascii=False), encoding="utf-8")
    bubble_dir = tmp_path / "bubbles"
    bubble_dir.mkdir()
    _write_jsonl(bubble_dir / "bbl_pending.jsonl", [
        {
            "type": "llm_response",
            "seq": 0,
            "ts": "2026-06-29T08:00:00",
            "model": "bubble-model",
            "usage": {"input_tokens": 100, "output_tokens": 1},
        },
        {
            "type": "llm_response",
            "seq": 1,
            "ts": "2026-06-29T08:01:00",
            "model": "bubble-model",
            "usage": {"input_tokens": 2, "output_tokens": 3},
        },
    ])

    collector = UsageStatsCollector(
        LogStore(tmp_path),
        now_fn=lambda: datetime(2026, 6, 29),
        state_path=state_path,
    )
    with patch.object(Path, "glob", side_effect=AssertionError("should not scan all bubbles")):
        collector.load_bubble_history(tmp_path)

    lifetime = collector.snapshot()["lifetime"]
    assert lifetime["total_tokens"] == 5
    assert lifetime["by_scope"]["bubble"]["total_tokens"] == 5


def _new_empty_test_bucket():
    return {
        "llm_calls": 0,
        "tracked_calls": 0,
        "estimated_calls": 0,
        "input_tokens": 0,
        "output_tokens": 0,
        "cached_tokens": 0,
        "tool_calls": 0,
        "thinking_calls": 0,
        "thinking_seconds": 0.0,
        "by_model": {},
        "by_provider_model": {},
        "tools": {},
    }
