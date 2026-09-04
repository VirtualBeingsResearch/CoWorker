"""Event-sourced collector for privacy-safe resource usage statistics."""

from __future__ import annotations

from collections.abc import Callable
from copy import deepcopy
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from loguru import logger

from coworker.agent.log_store import LogStore

from . import report
from .buckets import (
    BUBBLE_SCOPE,
    LONG_TERM_SCOPE,
    MAIN_STREAM_ID,
    SUBCONSCIOUS_SCOPE,
    SUMMARY_SCOPE,
    VISION_SCOPE,
    add_automatic_skill_load,
    add_bubble_outcome,
    add_memory_compression,
    add_skill_attempt,
    add_skill_result,
    add_thinking_duration,
    add_tool_call,
    add_tool_result,
    add_usage,
    bucket_has_data,
    merge_bucket,
    new_bucket,
    new_scope_buckets,
    norm_part,
    scope_for_stream_id,
)
from .pricing import PricingCatalog
from .store import CollectorState, iter_jsonl, load_state, persist_state


class UsageStatsCollector:
    """Aggregate privacy-safe resource usage and execution statistics."""

    def __init__(
        self,
        log_store: LogStore | None = None,
        now_fn: Callable[[], datetime] = datetime.now,
        state_path: str | Path | None = None,
    ) -> None:
        self._now_fn = now_fn
        self._state = CollectorState()
        self._state_path = Path(state_path) if state_path is not None else None
        self._loading_history = False
        self._snapshot_cache_date: date | None = None
        self._snapshot_cache: dict[str, Any] | None = None
        if log_store is not None:
            self.load_history(log_store)

    def load_history(self, log_store: LogStore) -> None:
        loaded_state = load_state(self._state, self._state_path)
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
        if self._state.compression_tracking_since is None:
            self._state.compression_tracking_since = self._now_fn().date()
        self._persist_state()

    def load_entries(self, entries: list[dict[str, Any]]) -> None:
        for entry in entries:
            self.on_entry(entry, persist=False)

    def load_bubble_history(self, logs_dir: str | Path) -> None:
        root = Path(logs_dir)
        if self._state.bubble_history_scanned:
            self._load_pending_bubble_streams(root)
            self._persist_state()
            return

        paths = [
            *sorted((root / "bubbles").glob("*.jsonl")),
            *sorted((root / "subconscious" / "bubbles").glob("*.jsonl")),
        ]
        if not paths:
            self._state.bubble_history_scanned = True
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
                    stream_id not in self._state.last_seq_by_stream
                    and self._state.bubble_history_key is not None
                    and file_key <= self._state.bubble_history_key
                ):
                    continue
                for entry in iter_jsonl(path):
                    self.on_entry(entry, persist=False, stream_id=stream_id)
                processed_keys.append(file_key)
                processed_streams.append(stream_id)
            if processed_keys:
                self._advance_bubble_history(max(processed_keys))
                for stream_id in processed_streams:
                    self._state.last_seq_by_stream.pop(stream_id, None)
                    self._state.pending_thinking_starts.pop(stream_id, None)
                    self._discard_pending_tool_calls(stream_id)
            self._state.bubble_history_scanned = True
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
        self._state.bubble_history_scanned = True
        stream_id = self.bubble_stream_id(root, path)
        self._state.last_seq_by_stream.pop(stream_id, None)
        self._state.pending_thinking_starts.pop(stream_id, None)
        self._discard_pending_tool_calls(stream_id)
        self._persist_state()

    def on_entry(
        self,
        entry: dict[str, Any],
        persist: bool = True,
        stream_id: str = MAIN_STREAM_ID,
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
            provider = norm_part(entry.get("provider"), "unknown")
            model = norm_part(entry.get("model"), "unknown")
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
        elif t in ("summary_llm_response", "vision_llm_response", "long_term_llm_response"):
            usage = entry.get("usage")
            if not isinstance(usage, dict):
                usage = {}
            provider = norm_part(entry.get("provider"), "unknown")
            model = norm_part(entry.get("model"), "unknown")
            stream_scope = scope_for_stream_id(stream_id)
            if stream_scope in (BUBBLE_SCOPE, SUBCONSCIOUS_SCOPE):
                scope = stream_scope
            elif t == "summary_llm_response":
                scope = SUMMARY_SCOPE
            elif t == "vision_llm_response":
                scope = VISION_SCOPE
            else:
                scope = LONG_TERM_SCOPE
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
            tool_name = norm_part(entry.get("name"), "unknown")
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
                        norm_part(skill_name, "unknown"),
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
        today_bucket = deepcopy(self._state.days.get(today, new_bucket()))
        today_scopes = deepcopy(self._state.days_by_scope.get(today, new_scope_buckets()))
        last_7_bucket = new_bucket()
        last_7_scopes = new_scope_buckets()
        for day, bucket in self._state.days.items():
            if last_7_start <= day <= today:
                merge_bucket(last_7_bucket, bucket)
        for day, scopes in self._state.days_by_scope.items():
            if last_7_start <= day <= today:
                self._merge_scope_buckets(last_7_scopes, scopes)
        payload = {
            "today": report.finalize_window(today_bucket, today_scopes, pricing),
            "last_7_days": report.finalize_window(last_7_bucket, last_7_scopes, pricing),
            "lifetime": report.finalize_window(
                deepcopy(self._state.lifetime),
                deepcopy(self._state.lifetime_by_scope),
                pricing,
            ),
        }
        if detailed:
            return payload
        return {key: report.compact_window(value) for key, value in payload.items()}

    def report(
        self,
        *,
        start_date: date | None = None,
        end_date: date | None = None,
        model_prices: list | None = None,
    ) -> dict[str, Any]:
        """Return the authenticated management report without expanding public status."""
        pricing = (
            None
            if model_prices is None
            else {(price.provider, price.model): price for price in model_prices}
        )
        now = self._now_fn()
        today = now.date()
        last_30_start = today - timedelta(days=report.REPORT_DAYS - 1)
        last_30_bucket, last_30_scopes = self._aggregate_range(last_30_start, today)
        previous_ranges = {
            "today": (today - timedelta(days=1), today - timedelta(days=1)),
            "last_7_days": (today - timedelta(days=13), today - timedelta(days=7)),
            "last_30_days": (today - timedelta(days=59), today - timedelta(days=30)),
        }
        tracked_days = [
            day for day, bucket in self._state.days.items() if bucket_has_data(bucket)
        ]
        payload = {
            **self._snapshot_for_date(today, detailed=True, pricing=pricing),
            "last_30_days": report.finalize_window(
                last_30_bucket,
                last_30_scopes,
                pricing,
            ),
            "previous": {
                key: report.summary_window(
                    *self._aggregate_range(start, end), pricing
                )
                for key, (start, end) in previous_ranges.items()
            },
            "daily": [
                {
                    "date": day.isoformat(),
                    **report.summary_window(
                        deepcopy(self._state.days.get(day, new_bucket())),
                        deepcopy(
                            self._state.days_by_scope.get(day, new_scope_buckets())
                        ),
                        pricing,
                    ),
                }
                for day in (
                    last_30_start + timedelta(days=offset)
                    for offset in range(report.REPORT_DAYS)
                )
            ],
            "today_intraday": self._intraday_report(today, pricing),
            "generated_at": now.isoformat(),
            "tracking_since": min(tracked_days).isoformat() if tracked_days else None,
            "compression_tracking_since": (
                self._state.compression_tracking_since or today
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
            previous = report.summary_window(
                *self._aggregate_range(previous_start, previous_end),
                pricing,
            )
        report_payload = {
            "start_date": start.isoformat(),
            "end_date": end.isoformat(),
            "previous_start_date": (
                previous_start.isoformat() if previous_start is not None else None
            ),
            "previous_end_date": (
                previous_end.isoformat() if previous_end is not None else None
            ),
            "stats": report.finalize_window(bucket, scopes, pricing),
            "previous": previous,
            "daily": [
                {
                    "date": day.isoformat(),
                    **report.summary_window(
                        deepcopy(self._state.days.get(day, new_bucket())),
                        deepcopy(
                            self._state.days_by_scope.get(day, new_scope_buckets())
                        ),
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
            report_payload["intraday"] = self._intraday_report(start, pricing)
        return report_payload

    def _intraday_report(
        self,
        day: date,
        pricing: PricingCatalog | None = None,
    ) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for hour in range(report.INTRADAY_HOURS):
            hour_key = f"{day.isoformat()}T{hour:02d}:00:00"
            rows.append({
                "start_time": hour_key,
                "end_time": f"{day.isoformat()}T{hour:02d}:59:59.999999",
                **report.summary_window(
                    deepcopy(self._state.hours.get(hour_key, new_bucket())),
                    deepcopy(
                        self._state.hours_by_scope.get(hour_key, new_scope_buckets())
                    ),
                    pricing,
                ),
            })
        return rows

    def _aggregate_range(
        self,
        start: date,
        end: date,
    ) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
        bucket = new_bucket()
        scopes = new_scope_buckets()
        for day, day_bucket in self._state.days.items():
            if start <= day <= end:
                merge_bucket(bucket, day_bucket)
        for day, day_scopes in self._state.days_by_scope.items():
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
        bucket = self._state.days.setdefault(day, new_bucket())
        add_usage(bucket, usage, provider, model, usage_source)
        add_usage(
            self._state.hours.setdefault(hour, new_bucket()),
            usage,
            provider,
            model,
            usage_source,
        )
        add_usage(self._state.lifetime, usage, provider, model, usage_source)
        scope = scope_for_stream_id(stream_id)
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
        bucket = self._state.days.setdefault(day, new_bucket())
        add_usage(bucket, usage, provider, model, usage_source)
        add_usage(
            self._state.hours.setdefault(hour, new_bucket()),
            usage,
            provider,
            model,
            usage_source,
        )
        add_usage(self._state.lifetime, usage, provider, model, usage_source)
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
        add_usage(
            self._scope_bucket_for_hour(hour, scope),
            usage,
            provider,
            model,
            usage_source,
        )
        add_usage(
            self._scope_bucket_for_day(day, scope),
            usage,
            provider,
            model,
            usage_source,
        )
        add_usage(
            self._scope_bucket_for_lifetime(scope),
            usage,
            provider,
            model,
            usage_source,
        )

    def _record_tool_call(self, day: date, tool_name: str, stream_id: str) -> None:
        bucket = self._state.days.setdefault(day, new_bucket())
        add_tool_call(bucket, tool_name)
        add_tool_call(self._state.lifetime, tool_name)
        scope = scope_for_stream_id(stream_id)
        add_tool_call(self._scope_bucket_for_day(day, scope), tool_name)
        add_tool_call(self._scope_bucket_for_lifetime(scope), tool_name)

    def _record_tool_result(self, entry: dict[str, Any], stream_id: str) -> None:
        call_id = str(entry.get("id") or "")
        if not call_id:
            return
        pending = self._state.pending_tool_calls.pop(
            self._pending_tool_key(stream_id, call_id), None
        )
        if pending is None:
            return
        try:
            day = date.fromisoformat(pending["day"])
        except (KeyError, TypeError, ValueError):
            day = self._entry_date(entry)
        tool_name = norm_part(pending.get("tool_name"), "unknown")
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
        add_tool_result(self._state.days.setdefault(day, new_bucket()), tool_name, is_error)
        add_tool_result(self._state.lifetime, tool_name, is_error)
        scope = scope_for_stream_id(stream_id)
        add_tool_result(self._scope_bucket_for_day(day, scope), tool_name, is_error)
        add_tool_result(self._scope_bucket_for_lifetime(scope), tool_name, is_error)

    def _record_skill_attempt(self, day: date, skill_name: str, stream_id: str) -> None:
        add_skill_attempt(self._state.days.setdefault(day, new_bucket()), skill_name)
        add_skill_attempt(self._state.lifetime, skill_name)
        scope = scope_for_stream_id(stream_id)
        add_skill_attempt(self._scope_bucket_for_day(day, scope), skill_name)
        add_skill_attempt(self._scope_bucket_for_lifetime(scope), skill_name)

    def _add_skill_result_for_day(
        self,
        day: date,
        skill_name: str,
        stream_id: str,
        is_error: bool,
    ) -> None:
        add_skill_result(
            self._state.days.setdefault(day, new_bucket()), skill_name, is_error
        )
        add_skill_result(self._state.lifetime, skill_name, is_error)
        scope = scope_for_stream_id(stream_id)
        add_skill_result(self._scope_bucket_for_day(day, scope), skill_name, is_error)
        add_skill_result(self._scope_bucket_for_lifetime(scope), skill_name, is_error)

    def _record_automatic_skill_load(
        self,
        day: date,
        skill_name: str,
        stream_id: str,
    ) -> None:
        add_automatic_skill_load(
            self._state.days.setdefault(day, new_bucket()), skill_name
        )
        add_automatic_skill_load(self._state.lifetime, skill_name)
        scope = scope_for_stream_id(stream_id)
        add_automatic_skill_load(self._scope_bucket_for_day(day, scope), skill_name)
        add_automatic_skill_load(self._scope_bucket_for_lifetime(scope), skill_name)

    def _record_bubble_outcome(
        self,
        day: date,
        entry: dict[str, Any],
        stream_id: str,
    ) -> None:
        scope = scope_for_stream_id(stream_id)
        if scope not in (BUBBLE_SCOPE, SUBCONSCIOUS_SCOPE):
            return
        add_bubble_outcome(self._state.days.setdefault(day, new_bucket()), entry)
        add_bubble_outcome(self._state.lifetime, entry)
        add_bubble_outcome(self._scope_bucket_for_day(day, scope), entry)
        add_bubble_outcome(self._scope_bucket_for_lifetime(scope), entry)

    def _record_memory_compression(
        self,
        entry: dict[str, Any],
        stream_id: str,
    ) -> None:
        day = self._entry_date(entry)
        hour = self._entry_hour(entry)
        occurred_at_value = self._entry_datetime(entry)
        occurred_at = occurred_at_value.isoformat() if occurred_at_value is not None else ""
        add_memory_compression(
            self._state.days.setdefault(day, new_bucket()), entry, occurred_at
        )
        add_memory_compression(
            self._state.hours.setdefault(hour, new_bucket()), entry, occurred_at
        )
        add_memory_compression(self._state.lifetime, entry, occurred_at)
        scope = scope_for_stream_id(stream_id)
        add_memory_compression(
            self._scope_bucket_for_hour(hour, scope),
            entry,
            occurred_at,
        )
        add_memory_compression(self._scope_bucket_for_day(day, scope), entry, occurred_at)
        add_memory_compression(
            self._scope_bucket_for_lifetime(scope), entry, occurred_at
        )
        if (
            self._state.compression_tracking_since is None
            or day < self._state.compression_tracking_since
        ):
            self._state.compression_tracking_since = day

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
        self._state.pending_tool_calls[self._pending_tool_key(stream_id, call_id)] = {
            "stream_id": stream_id,
            "day": day.isoformat(),
            "tool_name": tool_name,
            "skill_name": skill_name,
        }

    @staticmethod
    def _pending_tool_key(stream_id: str, call_id: str) -> str:
        return f"{stream_id}\x1f{call_id}"

    @staticmethod
    def _skill_name_from_call(entry: dict[str, Any]) -> str:
        arguments = entry.get("arguments")
        if not isinstance(arguments, dict):
            return "unknown"
        return norm_part(arguments.get("skill_name"), "unknown")

    def _record_thinking_start(self, entry: dict[str, Any], stream_id: str) -> None:
        started_at = self._entry_datetime(entry)
        if started_at is None:
            return
        self._state.pending_thinking_starts[stream_id] = (
            started_at,
            self._entry_date(entry),
        )

    def _record_thinking_finish(self, entry: dict[str, Any], stream_id: str) -> None:
        pending = self._state.pending_thinking_starts.pop(stream_id, None)
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
        bucket = self._state.days.setdefault(day, new_bucket())
        add_thinking_duration(bucket, seconds)
        add_thinking_duration(self._state.lifetime, seconds)
        scope = scope_for_stream_id(stream_id)
        add_thinking_duration(self._scope_bucket_for_day(day, scope), seconds)
        add_thinking_duration(self._scope_bucket_for_lifetime(scope), seconds)

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
        scopes = self._state.days_by_scope.setdefault(day, new_scope_buckets())
        return scopes.setdefault(scope, new_bucket())

    def _scope_bucket_for_hour(self, hour: str, scope: str) -> dict[str, Any]:
        scopes = self._state.hours_by_scope.setdefault(hour, new_scope_buckets())
        return scopes.setdefault(scope, new_bucket())

    def _scope_bucket_for_lifetime(self, scope: str) -> dict[str, Any]:
        return self._state.lifetime_by_scope.setdefault(scope, new_bucket())

    @staticmethod
    def _merge_scope_buckets(
        dst: dict[str, dict[str, Any]],
        src: dict[str, dict[str, Any]],
    ) -> None:
        for scope, bucket in src.items():
            if isinstance(bucket, dict):
                merge_bucket(dst.setdefault(str(scope), new_bucket()), bucket)

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
            entries = iter_entries_after(self._last_seq(MAIN_STREAM_ID))
            for entry in entries:
                self.on_entry(entry, persist=False)
            return
        for entry in log_store.iter_all_entries():
            self.on_entry(entry, persist=False)

    def _should_process(self, entry: dict[str, Any], stream_id: str) -> bool:
        seq = self._entry_seq(entry)
        if seq is None:
            return True
        if seq <= self._last_seq(stream_id):
            return False
        self._state.last_seq_by_stream[stream_id] = seq
        return True

    def _last_seq(self, stream_id: str) -> int:
        return self._state.last_seq_by_stream.get(stream_id, -1)

    @staticmethod
    def _entry_seq(entry: dict[str, Any]) -> int | None:
        try:
            return int(entry["seq"])
        except (KeyError, TypeError, ValueError):
            return None

    def _load_state(self) -> bool:
        return load_state(self._state, self._state_path)

    def _persist_state(self) -> None:
        persist_state(self._state, self._state_path, self._now_fn)

    def _discard_pending_tool_calls(self, stream_id: str) -> None:
        stale = [
            key
            for key, item in self._state.pending_tool_calls.items()
            if item.get("stream_id") == stream_id
        ]
        for key in stale:
            self._state.pending_tool_calls.pop(key, None)

    @staticmethod
    def bubble_stream_id(root: Path, path: Path) -> str:
        try:
            rel = path.relative_to(root).as_posix()
        except ValueError:
            rel = path.name
        return f"bubble:{rel}"

    def _advance_bubble_history(self, key: tuple[int, str]) -> None:
        if self._state.bubble_history_key is None or key > self._state.bubble_history_key:
            self._state.bubble_history_key = key

    def _load_pending_bubble_streams(self, root: Path) -> None:
        pending = [
            stream_id
            for stream_id in sorted(self._state.last_seq_by_stream)
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
                for entry in iter_jsonl(path):
                    if entry.get("__meta__"):
                        saw_meta = True
                    self.on_entry(entry, persist=False, stream_id=stream_id)
                if saw_meta:
                    self._state.last_seq_by_stream.pop(stream_id, None)
                    self._state.pending_thinking_starts.pop(stream_id, None)
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
