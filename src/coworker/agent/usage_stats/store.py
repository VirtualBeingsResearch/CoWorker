"""Persistence for usage statistics.

``CollectorState`` models exactly the aggregation state that survives restarts;
everything else on the collector (caches, pending pairing maps used only within
a run) stays out of the state file.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any

from loguru import logger

from .buckets import MAIN_STREAM_ID, merge_bucket, new_bucket, new_scope_buckets

SCHEMA_VERSION = 11


@dataclass
class CollectorState:
    """All aggregation state written to the JSON state file."""

    lifetime: dict[str, Any] = field(default_factory=new_bucket)
    days: dict[date, dict[str, Any]] = field(default_factory=dict)
    hours: dict[str, dict[str, Any]] = field(default_factory=dict)
    days_by_scope: dict[date, dict[str, dict[str, Any]]] = field(default_factory=dict)
    hours_by_scope: dict[str, dict[str, dict[str, Any]]] = field(default_factory=dict)
    lifetime_by_scope: dict[str, dict[str, Any]] = field(default_factory=new_scope_buckets)
    last_seq_by_stream: dict[str, int] = field(default_factory=dict)
    pending_thinking_starts: dict[str, tuple[datetime, date]] = field(default_factory=dict)
    pending_tool_calls: dict[str, dict[str, str]] = field(default_factory=dict)
    compression_tracking_since: date | None = None
    bubble_history_key: tuple[int, str] | None = None
    bubble_history_scanned: bool = False


def load_state(state: CollectorState, path: Path | None) -> bool:
    """Populate ``state`` from ``path``; return True when a state file was applied."""
    if path is None or not path.exists():
        return False
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        logger.warning(f"Failed to read usage stats state {path}: {e}")
        return False
    schema_version = data.get("schema_version")
    if schema_version != SCHEMA_VERSION:
        return False
    try:
        compression_tracking_since = data.get("compression_tracking_since")
        state.compression_tracking_since = (
            date.fromisoformat(compression_tracking_since)
            if isinstance(compression_tracking_since, str)
            else None
        )
        state.lifetime = new_bucket()
        merge_bucket(state.lifetime, data.get("lifetime", {}))
        state.days = {}
        for day_str, bucket in data.get("days", {}).items():
            day = date.fromisoformat(day_str)
            state.days[day] = new_bucket()
            merge_bucket(state.days[day], bucket)
        state.hours = {}
        for hour_str, bucket in data.get("hours", {}).items():
            hour = datetime.fromisoformat(hour_str)
            hour_key = f"{hour.date().isoformat()}T{hour.hour:02d}:00:00"
            state.hours[hour_key] = new_bucket()
            merge_bucket(state.hours[hour_key], bucket)
        state.hours_by_scope = {}
        for hour_str, scopes in data.get("hours_by_scope", {}).items():
            hour = datetime.fromisoformat(hour_str)
            hour_key = f"{hour.date().isoformat()}T{hour.hour:02d}:00:00"
            state.hours_by_scope[hour_key] = load_scope_map(scopes)
        state.lifetime_by_scope = load_scope_map(data.get("lifetime_by_scope", {}))
        state.days_by_scope = {}
        for day_str, scopes in data.get("days_by_scope", {}).items():
            day = date.fromisoformat(day_str)
            state.days_by_scope[day] = load_scope_map(scopes)
        state.last_seq_by_stream = {}
        checkpoints = data.get("checkpoints", {})
        for stream_id, checkpoint in checkpoints.items():
            if isinstance(checkpoint, dict):
                state.last_seq_by_stream[str(stream_id)] = int(checkpoint.get("seq", -1))
        if MAIN_STREAM_ID not in state.last_seq_by_stream:
            checkpoint = data.get("checkpoint", {})
            state.last_seq_by_stream[MAIN_STREAM_ID] = int(checkpoint.get("seq", -1))
        state.pending_thinking_starts = load_pending_thinking_starts(
            data.get("pending_thinking_starts", {})
        )
        state.pending_tool_calls = load_pending_tool_calls(
            data.get("pending_tool_calls", {})
        )
        bubble_history = data.get("bubble_history", {})
        if isinstance(bubble_history, dict):
            mtime_ns = int(bubble_history.get("mtime_ns", -1))
            path_str = str(bubble_history.get("path", ""))
            if mtime_ns >= 0 and path_str:
                state.bubble_history_key = (mtime_ns, path_str)
            state.bubble_history_scanned = bool(bubble_history.get("scanned"))
        elif state.bubble_history_key is not None:
            state.bubble_history_scanned = True
    except Exception as e:
        logger.warning(f"Failed to parse usage stats state {path}: {e}")
        state.days = {}
        state.hours = {}
        state.hours_by_scope = {}
        state.lifetime = new_bucket()
        state.days_by_scope = {}
        state.lifetime_by_scope = new_scope_buckets()
        state.last_seq_by_stream = {}
        state.pending_thinking_starts = {}
        state.pending_tool_calls = {}
        state.compression_tracking_since = None
        return False
    return True


def persist_state(state: CollectorState, path: Path | None, now_fn: Any) -> None:
    if path is None:
        return
    checkpoints = {
        stream_id: {"seq": seq}
        for stream_id, seq in sorted(state.last_seq_by_stream.items())
    }
    payload = {
        "schema_version": SCHEMA_VERSION,
        "updated_at": now_fn().isoformat(),
        "checkpoint": checkpoints.get(MAIN_STREAM_ID, {"seq": -1}),
        "checkpoints": checkpoints,
        "pending_thinking_starts": format_pending_thinking_starts(state),
        "pending_tool_calls": state.pending_tool_calls,
        "compression_tracking_since": (
            state.compression_tracking_since.isoformat()
            if state.compression_tracking_since is not None
            else None
        ),
        "bubble_history": format_bubble_history(state),
        "lifetime": state.lifetime,
        "days": {day.isoformat(): bucket for day, bucket in sorted(state.days.items())},
        "hours": {hour: bucket for hour, bucket in sorted(state.hours.items())},
        "hours_by_scope": {
            hour: scopes for hour, scopes in sorted(state.hours_by_scope.items())
        },
        "lifetime_by_scope": state.lifetime_by_scope,
        "days_by_scope": {
            day.isoformat(): scopes for day, scopes in sorted(state.days_by_scope.items())
        },
    }
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except Exception as e:
        logger.warning(f"Failed to persist usage stats state {path}: {e}")


def load_scope_map(data: Any) -> dict[str, dict[str, Any]]:
    scopes = new_scope_buckets()
    if not isinstance(data, dict):
        return scopes
    for scope, bucket in data.items():
        if not isinstance(bucket, dict):
            continue
        dst = scopes.setdefault(str(scope), new_bucket())
        merge_bucket(dst, bucket)
    return scopes


def load_pending_thinking_starts(data: Any) -> dict[str, tuple[datetime, date]]:
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


def load_pending_tool_calls(data: Any) -> dict[str, dict[str, str]]:
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


def format_pending_thinking_starts(state: CollectorState) -> dict[str, dict[str, str]]:
    return {
        stream_id: {"ts": started_at.isoformat(), "day": day.isoformat()}
        for stream_id, (started_at, day) in sorted(state.pending_thinking_starts.items())
    }


def format_bubble_history(state: CollectorState) -> dict[str, Any]:
    if state.bubble_history_key is None:
        return {"scanned": state.bubble_history_scanned}
    mtime_ns, path_str = state.bubble_history_key
    return {
        "mtime_ns": mtime_ns,
        "path": path_str,
        "scanned": state.bubble_history_scanned,
    }


def iter_jsonl(path: Path) -> Iterator[dict[str, Any]]:
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
