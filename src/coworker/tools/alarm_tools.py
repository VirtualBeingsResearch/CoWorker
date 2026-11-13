from __future__ import annotations

import asyncio
import json
import re
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING, Literal, cast

import httpx
from loguru import logger

from coworker.core.types import IncomingEvent, ToolResult
from coworker.i18n import bind_locale, tr
from coworker.tools.alarm_conditions import (
    AlarmCondition,
    ConditionChecker,
    ConditionKind,
    TaskStoreLike,
)
from coworker.tools.base import Tool, ToolDefinition

if TYPE_CHECKING:
    from coworker.agent.inbox_watcher import InboxWatcher

_MAX_CHECKS_PER_HOUR = 60
_MIN_CONDITION_INTERVAL_SECONDS = 60
_CONDITION_KINDS: frozenset[str] = frozenset({"file_changed", "http_changed", "task_state"})
_DEFAULT_INTERVAL_SECONDS = 300
_MAX_MESSAGE_CHARS = 2000


def _local_datetime(value: datetime) -> datetime:
    return value.astimezone()


def _local_now() -> datetime:
    return datetime.now().astimezone()


@dataclass
class AlarmOptions:
    task_store: TaskStoreLike | None = None
    allowed_roots: list[Path] = field(default_factory=list)
    max_conditions: int = 32
    min_interval_seconds: int = 60
    http_max_bytes: int = 256 * 1024
    check_timeout_seconds: float = 20.0
    file_max_bytes: int = 8 * 1024 * 1024
    http_transport: httpx.AsyncBaseTransport | None = None
    condition_persist_path: Path | None = None


@dataclass
class ConditionAlarm:
    alarm_id: str
    message: str
    condition: AlarmCondition
    every_seconds: int
    task: asyncio.Task[None] | None = None
    last_state: str = ""
    last_stat: str = ""
    last_error: str = ""
    last_checked_at: datetime | None = None
    next_check_at: datetime | None = None
    failures: int = 0
    paused: bool = False
    checks: deque[float] = field(default_factory=deque)


class AlarmManager:
    def __init__(
        self,
        inbox: InboxWatcher,
        persist_path: Path | None = None,
        *,
        options: AlarmOptions | None = None,
    ) -> None:
        self._inbox = inbox
        self._persist_path = persist_path
        self._options = options or AlarmOptions()
        self._condition_path = self._options.condition_persist_path
        if self._condition_path is None and persist_path is not None:
            self._condition_path = persist_path.with_name("alarm_conditions.json")
        # alarm_id -> (task, next_trigger_at, message, repeat_seconds)
        self._alarms: dict[str, tuple[asyncio.Task, datetime, str, int | None]] = {}
        self._conditions: dict[str, ConditionAlarm] = {}
        self._checker = ConditionChecker(
            task_store=self._options.task_store,
            allowed_roots=self._options.allowed_roots,
            http_max_bytes=self._options.http_max_bytes,
            check_timeout_seconds=self._options.check_timeout_seconds,
            file_max_bytes=self._options.file_max_bytes,
            http_transport=self._options.http_transport,
        )
        self._max_conditions = max(1, self._options.max_conditions)
        self._min_interval_seconds = max(1, self._options.min_interval_seconds)

    @property
    def max_conditions(self) -> int:
        return self._max_conditions

    @property
    def min_interval_seconds(self) -> int:
        return self._min_interval_seconds

    @property
    def condition_count(self) -> int:
        return len(self._conditions)

    @property
    def condition_persist_path(self) -> Path | None:
        return self._condition_path

    def has_condition(self, alarm_id: str) -> bool:
        return alarm_id in self._conditions

    def get_condition(self, alarm_id: str) -> ConditionAlarm | None:
        return self._conditions.get(alarm_id)

    def validate_condition(self, condition: AlarmCondition) -> None:
        self._checker.validate_target(condition)

    async def set(
        self,
        alarm_id: str,
        trigger_at: datetime,
        message: str,
        repeat_seconds: int | None = None,
    ) -> None:
        removed = self._cancel(alarm_id)
        trigger_at = _local_datetime(trigger_at)
        delay = max(0.0, (trigger_at - _local_now()).total_seconds())
        task = asyncio.create_task(
            bind_locale(partial(self._fire, alarm_id, delay, message, repeat_seconds))
        )
        self._alarms[alarm_id] = (task, trigger_at, message, repeat_seconds)
        self._save_time()
        if removed == "condition":
            self._save_conditions()

    def add_condition(
        self,
        alarm_id: str,
        message: str,
        condition: AlarmCondition,
        every_seconds: int,
    ) -> ConditionAlarm:
        if alarm_id not in self._conditions and len(self._conditions) >= self._max_conditions:
            raise ValueError(tr("tool_result.alarm.condition_limit", limit=self._max_conditions))
        removed = self._cancel(alarm_id)
        entry = ConditionAlarm(
            alarm_id=alarm_id,
            message=message,
            condition=condition,
            every_seconds=max(self._min_interval_seconds, every_seconds),
        )
        self._conditions[alarm_id] = entry
        if removed == "time":
            self._save_time()
        return entry

    async def set_condition(
        self,
        alarm_id: str,
        message: str,
        condition: AlarmCondition,
        every_seconds: int,
        *,
        start: bool = True,
    ) -> None:
        entry = self.add_condition(alarm_id, message, condition, every_seconds)
        if start:
            self._start_condition(entry)
        self._save_conditions()

    def _start_condition(self, entry: ConditionAlarm) -> None:
        if entry.paused:
            return
        entry.task = asyncio.create_task(
            bind_locale(partial(self._condition_loop, entry.alarm_id))
        )

    async def close(self) -> None:
        tasks = [entry.task for entry in self._conditions.values() if entry.task is not None]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        await self._checker.close()

    async def restore(self) -> int:
        count = 0
        migrated = False
        for entry in _load_records(self._persist_path):
            if entry.get("kind") == "condition":
                if self._restore_condition(entry):
                    count += 1
                    migrated = True
                continue
            if self._restore_time(entry):
                count += 1
        for entry in _load_records(self._condition_path):
            if self._restore_condition(entry):
                count += 1
        if migrated:
            self._save_time()
            self._save_conditions()
        if count:
            logger.info(f"Restored {count} trigger(s)")
        return count

    def _restore_time(self, entry: dict) -> bool:
        try:
            alarm_id = str(entry["alarm_id"])
            next_trigger = _local_datetime(datetime.fromisoformat(entry["next_trigger_at"]))
            message = str(entry["message"])
            repeat_seconds = entry.get("repeat_seconds")
        except (KeyError, TypeError, ValueError) as error:
            logger.warning(f"Skipping invalid alarm record {entry.get('alarm_id')}: {error}")
            return False
        now = _local_now()
        delay = max(0.0, (next_trigger - now).total_seconds())
        overdue_note: str | None = None
        if next_trigger < now:
            late = int((now - next_trigger).total_seconds())
            overdue_note = tr("tool_result.alarm.overdue", seconds=late)
        self._cancel(alarm_id)
        task = asyncio.create_task(
            bind_locale(
                partial(
                    self._fire,
                    alarm_id,
                    delay,
                    message,
                    repeat_seconds,
                    overdue_note=overdue_note,
                )
            )
        )
        self._alarms[alarm_id] = (task, next_trigger, message, repeat_seconds)
        return True

    def _restore_condition(self, entry: dict) -> bool:
        raw = entry.get("condition")
        if not isinstance(raw, dict):
            logger.warning(f"Skipping condition alarm without a condition: {entry}")
            return False
        kind = str(raw.get("kind") or "")
        if kind not in _CONDITION_KINDS:
            logger.warning(f"Skipping condition alarm with unknown kind: {kind}")
            return False
        try:
            condition = AlarmCondition(
                kind=cast(ConditionKind, kind),
                target=str(raw.get("target") or ""),
                pattern=str(raw.get("pattern") or ""),
                status=str(raw.get("status") or ""),
                stale_seconds=max(0, int(raw.get("stale_seconds") or 0)),
            )
            every_seconds = max(
                self._min_interval_seconds, int(entry.get("every_seconds") or 60)
            )
            alarm_id = str(entry["alarm_id"])
        except (TypeError, ValueError) as error:
            logger.warning(f"Skipping invalid condition alarm {entry.get('alarm_id')}: {error}")
            return False

        self._cancel(alarm_id)
        last_state = str(entry.get("last_state") or "")
        if (
            condition.kind in {"file_changed", "http_changed"}
            and last_state
            and not last_state.startswith(("sha256:", "stat:"))
        ):
            last_state = ""
        restored = ConditionAlarm(
            alarm_id=alarm_id,
            message=str(entry.get("message") or ""),
            condition=condition,
            every_seconds=every_seconds,
            last_state=last_state,
            last_stat=str(entry.get("last_stat") or ""),
            last_error=str(entry.get("last_error") or ""),
            last_checked_at=_parse_optional_time(entry.get("last_checked_at")),
            failures=max(0, int(entry.get("failures") or 0)),
            paused=bool(entry.get("paused")),
        )
        self._conditions[alarm_id] = restored
        if not restored.paused:
            self._start_condition(restored)
        return True

    async def _fire(
        self,
        alarm_id: str,
        delay: float,
        message: str,
        repeat_seconds: int | None,
        overdue_note: str | None = None,
    ) -> None:
        try:
            await asyncio.sleep(delay)
            display = (
                tr("tool_result.alarm.display", message=message, note=overdue_note)
                if overdue_note
                else message
            )
            await self._inbox.push(
                IncomingEvent(
                    participant_id="alarm",
                    content=tr("tool_result.alarm.reminder", id=alarm_id, message=display),
                    timestamp=datetime.now(),
                    source="alarm",
                )
            )
            logger.info(f"Alarm fired: [{alarm_id}] {message}")
            if repeat_seconds and repeat_seconds > 0:
                next_trigger = _local_now() + timedelta(seconds=repeat_seconds)
                new_task = asyncio.create_task(
                    bind_locale(
                        partial(self._fire, alarm_id, float(repeat_seconds), message, repeat_seconds)
                    )
                )
                self._alarms[alarm_id] = (new_task, next_trigger, message, repeat_seconds)
                self._save_time()
            else:
                self._alarms.pop(alarm_id, None)
                self._save_time()
        except asyncio.CancelledError:
            pass

    async def _condition_loop(self, alarm_id: str) -> None:
        entry = self._conditions.get(alarm_id)
        if entry is None:
            return
        delay = 0.0
        try:
            while self._conditions.get(alarm_id) is entry and not entry.paused:
                if delay > 0:
                    await asyncio.sleep(delay)
                if entry.paused:
                    break
                if not self._admit_check(entry):
                    delay = float(entry.every_seconds)
                    continue
                try:
                    await asyncio.wait_for(
                        self.check_once(alarm_id), timeout=self._options.check_timeout_seconds
                    )
                except asyncio.CancelledError:
                    raise
                except Exception as error:
                    entry.failures += 1
                    entry.last_error = str(error)[:200]
                    entry.last_checked_at = _local_now()
                    logger.warning(f"Condition alarm {alarm_id} check failed: {error}")
                    if entry.failures >= 5:
                        entry.paused = True
                        self._save_conditions()
                        await self._push_condition_failure(entry, error)
                        return
                    self._save_conditions()
                    delay = min(
                        300.0,
                        float(entry.every_seconds) * (2 ** min(entry.failures, 8)),
                    )
                    continue
                entry.failures = 0
                delay = float(entry.every_seconds)
        except asyncio.CancelledError:
            pass

    def _admit_check(self, entry: ConditionAlarm) -> bool:
        now = time.monotonic()
        cutoff = now - 3600
        while entry.checks and entry.checks[0] < cutoff:
            entry.checks.popleft()
        if len(entry.checks) >= _MAX_CHECKS_PER_HOUR:
            logger.warning(f"Condition alarm {entry.alarm_id} exceeded hourly check budget")
            return False
        entry.checks.append(now)
        return True

    async def check_once(self, alarm_id: str) -> bool:
        entry = self._conditions.get(alarm_id)
        if entry is None:
            raise KeyError(alarm_id)
        result = await self._checker.check(
            entry.condition,
            previous_state=entry.last_state,
            previous_stat=entry.last_stat,
        )
        baseline = not entry.last_state
        changed = bool(entry.last_state) and result.state != entry.last_state
        had_error = bool(entry.last_error)
        if changed:
            await self._fire_condition(entry, result.degraded)
        entry.last_state = result.state
        entry.last_stat = result.stat_key
        entry.last_error = ""
        entry.last_checked_at = _local_now()
        entry.next_check_at = entry.last_checked_at + timedelta(seconds=entry.every_seconds)
        if baseline or changed or had_error:
            self._save_conditions()
        return changed

    async def _fire_condition(self, entry: ConditionAlarm, degraded: bool) -> None:
        await self._inbox.push(
            IncomingEvent(
                participant_id="alarm",
                content=tr(
                    "tool_result.alarm.condition_fired",
                    id=entry.alarm_id,
                    message=entry.message,
                    kind=entry.condition.kind,
                    target=entry.condition.target,
                    degraded=tr("tool_result.alarm.degraded") if degraded else "",
                ),
                timestamp=datetime.now(),
                source="alarm",
            )
        )
        logger.info(f"Condition alarm fired: [{entry.alarm_id}] {entry.condition.kind}")

    async def _push_condition_failure(self, entry: ConditionAlarm, error: Exception) -> None:
        await self._inbox.push(
            IncomingEvent(
                participant_id="alarm",
                content=tr(
                    "tool_result.alarm.condition_paused",
                    id=entry.alarm_id,
                    error=str(error)[:200],
                ),
                timestamp=datetime.now(),
                source="alarm",
            )
        )

    def _cancel(self, alarm_id: str) -> Literal["time", "condition"] | None:
        entry = self._alarms.pop(alarm_id, None)
        if entry:
            entry[0].cancel()
            return "time"
        condition = self._conditions.pop(alarm_id, None)
        if condition:
            if condition.task is not None:
                condition.task.cancel()
            return "condition"
        return None

    def cancel(self, alarm_id: str) -> bool:
        removed = self._cancel(alarm_id)
        if removed == "time":
            self._save_time()
        elif removed == "condition":
            self._save_conditions()
        return removed is not None

    def list(self) -> list[dict]:
        result: list[dict] = [
            {
                "id": alarm_id,
                "kind": "time",
                "trigger_at": trigger_at.isoformat(timespec="seconds"),
                "message": message,
                "repeat_seconds": repeat_seconds,
                "done": task.done(),
            }
            for alarm_id, (task, trigger_at, message, repeat_seconds) in self._alarms.items()
        ]
        result.extend(
            {
                "id": entry.alarm_id,
                "kind": "condition",
                "condition": entry.condition.kind,
                "target": entry.condition.target,
                "every_seconds": entry.every_seconds,
                "message": entry.message,
                "failures": entry.failures,
                "paused": entry.paused,
                "last_error": entry.last_error,
                "last_checked_at": (
                    entry.last_checked_at.isoformat(timespec="seconds")
                    if entry.last_checked_at
                    else None
                ),
                "next_check_at": (
                    entry.next_check_at.isoformat(timespec="seconds")
                    if entry.next_check_at
                    else None
                ),
                "done": entry.task.done() if entry.task is not None else True,
            }
            for entry in self._conditions.values()
        )
        return result

    def _save_time(self) -> None:
        _write_records(
            self._persist_path,
            [
                {
                    "alarm_id": alarm_id,
                    "next_trigger_at": trigger_at.isoformat(timespec="seconds"),
                    "message": message,
                    "repeat_seconds": repeat_seconds,
                }
                for alarm_id, (_, trigger_at, message, repeat_seconds) in self._alarms.items()
            ],
        )

    def _save_conditions(self) -> None:
        _write_records(
            self._condition_path,
            [
                {
                    "alarm_id": entry.alarm_id,
                    "kind": "condition",
                    "message": entry.message,
                    "condition": {
                        "kind": entry.condition.kind,
                        "target": entry.condition.target,
                        "pattern": entry.condition.pattern,
                        "status": entry.condition.status,
                        "stale_seconds": entry.condition.stale_seconds,
                    },
                    "every_seconds": entry.every_seconds,
                    "last_state": entry.last_state,
                    "last_stat": entry.last_stat,
                    "last_error": entry.last_error,
                    "last_checked_at": (
                        entry.last_checked_at.isoformat(timespec="seconds")
                        if entry.last_checked_at
                        else None
                    ),
                    "failures": entry.failures,
                    "paused": entry.paused,
                }
                for entry in self._conditions.values()
            ],
        )


def _load_records(path: Path | None) -> list[dict]:
    if path is None or not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as error:
        logger.warning(f"Failed to read alarm state from {path}: {error}")
        return []
    if not isinstance(data, list):
        logger.warning(f"Alarm state is not a list: {path}")
        return []
    return [entry for entry in data if isinstance(entry, dict)]


def _write_records(path: Path | None, records: list[dict]) -> None:
    if path is None:
        return
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = path.with_suffix(path.suffix + ".tmp")
        temp_path.write_text(json.dumps(records, ensure_ascii=False, indent=2), encoding="utf-8")
        temp_path.replace(path)
    except Exception as error:
        logger.warning(f"Failed to save alarm state to {path}: {error}")


def _parse_optional_time(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return _local_datetime(datetime.fromisoformat(value))
    except ValueError:
        return None


class SetAlarmTool(Tool):
    def __init__(self, manager: AlarmManager) -> None:
        self._manager = manager

    @property
    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="set_alarm",
            description=(
                "设置闹钟。给 trigger_at 表示定时提醒；给 condition + target 表示条件触发："
                "file_changed（文件变化）、http_changed（网页内容变化）、task_state（任务状态变化）。"
                "两种模式只能选一种，重启后自动恢复。"
            ),
            parameters={
                "type": "object",
                "properties": {
                    "trigger_at": {
                        "type": "string",
                        "description": "定时模式：首次触发时间，格式 YYYY-MM-DD HH:MM:SS。",
                    },
                    "condition": {
                        "type": "string",
                        "enum": ["file_changed", "http_changed", "task_state"],
                        "description": "条件模式：监听哪类变化。",
                    },
                    "target": {
                        "type": "string",
                        "description": "条件模式的目标：文件路径、URL 或 task_id；task_state 可留空表示任意任务。",
                    },
                    "every_seconds": {
                        "type": "integer",
                        "minimum": 60,
                        "default": _DEFAULT_INTERVAL_SECONDS,
                        "description": "条件模式的检查间隔秒数，最小 60，默认 300。",
                    },
                    "pattern": {
                        "type": "string",
                        "description": "http_changed 可选正则；只对匹配内容计算变化。",
                    },
                    "status": {
                        "type": "string",
                        "description": "task_state 可选目标状态，如 completed。",
                    },
                    "stale_seconds": {
                        "type": "integer",
                        "minimum": 0,
                        "description": "task_state 可选：任务超过该秒数未更新也算变化。",
                    },
                    "message": {
                        "type": "string",
                        "description": "提醒内容。",
                    },
                    "alarm_id": {
                        "type": "string",
                        "description": "闹钟 ID，可选，用于后续取消。不填则自动生成。",
                    },
                    "repeat_seconds": {
                        "type": "integer",
                        "description": "定时模式的循环间隔秒数。条件模式不需要。",
                    },
                },
                "required": ["message"],
            },
        )

    async def execute(
        self,
        message: str = "",
        trigger_at: str = "",
        condition: str = "",
        target: str = "",
        every_seconds: int = 0,
        pattern: str = "",
        status: str = "",
        stale_seconds: int = 0,
        alarm_id: str | None = None,
        repeat_seconds: int | None = None,
        **_,
    ) -> ToolResult:
        has_time = bool(trigger_at.strip())
        has_condition = bool(condition.strip())
        if has_time == has_condition:
            return self._error("tool_result.alarm.invalid_mode")
        if not message.strip():
            return self._error("tool_result.alarm.message_required")
        if len(message) > _MAX_MESSAGE_CHARS:
            return self._error("tool_result.alarm.message_too_long", limit=_MAX_MESSAGE_CHARS)

        if has_time:
            if target or pattern or status or stale_seconds or every_seconds:
                return self._error("tool_result.alarm.invalid_condition_fields")
            return await self._set_time(
                message=message,
                trigger_at=trigger_at,
                alarm_id=alarm_id,
                repeat_seconds=repeat_seconds,
            )

        if repeat_seconds not in (None, 0):
            return self._error("tool_result.alarm.invalid_condition_fields")
        return await self._set_condition(
            message=message,
            condition=condition.strip(),
            target=target.strip(),
            every_seconds=every_seconds,
            pattern=pattern,
            status=status.strip(),
            stale_seconds=stale_seconds,
            alarm_id=alarm_id,
        )

    async def _set_time(
        self,
        *,
        message: str,
        trigger_at: str,
        alarm_id: str | None,
        repeat_seconds: int | None,
    ) -> ToolResult:
        try:
            dt = datetime.fromisoformat(trigger_at)
        except ValueError:
            return self._error("tool_result.alarm.invalid_time")
        dt = _local_datetime(dt)
        delay = (dt - _local_now()).total_seconds()
        if delay < 0:
            return self._error("tool_result.alarm.past_time", time=trigger_at)
        if not alarm_id:
            alarm_id = uuid.uuid4().hex[:8]
        repeat = repeat_seconds if repeat_seconds and repeat_seconds > 0 else None
        await self._manager.set(alarm_id, dt, message, repeat)
        mode = (
            tr("tool_result.alarm.repeat_set", seconds=repeat)
            if repeat
            else tr("tool_result.alarm.once")
        )
        return ToolResult(
            tool_call_id="",
            content=tr(
                "tool_result.alarm.set",
                id=alarm_id,
                time=trigger_at,
                delay=f"{delay:.0f}",
                mode=mode,
            ),
        )

    async def _set_condition(
        self,
        *,
        message: str,
        condition: str,
        target: str,
        every_seconds: int,
        pattern: str,
        status: str,
        stale_seconds: int,
        alarm_id: str | None,
    ) -> ToolResult:
        if condition not in _CONDITION_KINDS:
            return self._error("tool_result.alarm.invalid_condition", condition=condition)
        if condition != "task_state" and not target:
            return self._error("tool_result.alarm.invalid_target")
        if condition != "http_changed" and pattern:
            return self._error("tool_result.alarm.invalid_condition_fields")
        if condition != "task_state" and (status or stale_seconds):
            return self._error("tool_result.alarm.invalid_condition_fields")
        if condition == "task_state" and not status and stale_seconds <= 0:
            return self._error("tool_result.alarm.invalid_condition_fields")
        if pattern:
            if len(pattern) > 512:
                return self._error("tool_result.alarm.invalid_condition_fields")
            try:
                re.compile(pattern)
            except re.error:
                return self._error("tool_result.alarm.invalid_condition_fields")
        minimum = max(_MIN_CONDITION_INTERVAL_SECONDS, self._manager.min_interval_seconds)
        if every_seconds and every_seconds < minimum:
            return self._error("tool_result.alarm.interval_too_small", seconds=minimum)
        if not alarm_id:
            alarm_id = uuid.uuid4().hex[:8]
        if (
            not self._manager.has_condition(alarm_id)
            and self._manager.condition_count >= self._manager.max_conditions
        ):
            return self._error(
                "tool_result.alarm.condition_limit", limit=self._manager.max_conditions
            )

        alarm_condition = AlarmCondition(
            kind=cast(ConditionKind, condition),
            target=target,
            pattern=pattern,
            status=status,
            stale_seconds=max(0, stale_seconds),
        )
        try:
            self._manager.validate_condition(alarm_condition)
        except ValueError as error:
            return self._error("tool_result.alarm.invalid_condition_target", error=str(error))
        interval = every_seconds if every_seconds > 0 else _DEFAULT_INTERVAL_SECONDS
        await self._manager.set_condition(alarm_id, message, alarm_condition, interval)
        return ToolResult(
            tool_call_id="",
            content=tr(
                "tool_result.alarm.set_condition",
                id=alarm_id,
                kind=condition,
                target=target or "*",
                seconds=interval,
                message=message,
            ),
        )

    @staticmethod
    def _error(key: str, **values: object) -> ToolResult:
        return ToolResult(tool_call_id="", content=tr(key, **values), is_error=True)


class ListAlarmsTool(Tool):
    def __init__(self, manager: AlarmManager) -> None:
        self._manager = manager

    @property
    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="list_alarms",
            description="列出所有待触发的闹钟，包含 ID、类型、触发条件、内容和是否循环。",
            parameters={"type": "object", "properties": {}, "required": []},
        )

    async def execute(self, **_) -> ToolResult:
        alarms = self._manager.list()
        if not alarms:
            return ToolResult(tool_call_id="", content=tr("tool_result.alarm.none"))
        lines = []
        for alarm in alarms:
            if alarm.get("kind") == "condition":
                mode = tr(
                    "tool_result.alarm.condition_mode",
                    kind=alarm["condition"],
                    target=alarm["target"] or "*",
                    seconds=alarm["every_seconds"],
                )
                if alarm.get("paused"):
                    mode += " " + tr(
                        "tool_result.alarm.condition_paused_label",
                        error=alarm.get("last_error") or "-",
                    )
                elif alarm.get("failures"):
                    mode += " " + tr(
                        "tool_result.alarm.condition_failures_label",
                        count=alarm["failures"],
                    )
            else:
                mode = (
                    tr("tool_result.alarm.repeat_list", seconds=alarm["repeat_seconds"])
                    if alarm["repeat_seconds"]
                    else tr("tool_result.alarm.once")
                )
            lines.append(f"- [{alarm['id']}] {mode} | {alarm['message']}")
        return ToolResult(
            tool_call_id="",
            content=tr("tool_result.alarm.title") + "\n" + "\n".join(lines),
        )


class CancelAlarmTool(Tool):
    def __init__(self, manager: AlarmManager) -> None:
        self._manager = manager

    @property
    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="cancel_alarm",
            description="取消指定 ID 的闹钟（定时或条件）。",
            parameters={
                "type": "object",
                "properties": {
                    "alarm_id": {
                        "type": "string",
                        "description": "要取消的闹钟 ID",
                    },
                },
                "required": ["alarm_id"],
            },
        )

    async def execute(self, alarm_id: str, **_) -> ToolResult:
        if self._manager.cancel(alarm_id):
            return ToolResult(
                tool_call_id="", content=tr("tool_result.alarm.cancelled", id=alarm_id)
            )
        return ToolResult(
            tool_call_id="",
            content=tr("tool_result.alarm.missing", id=alarm_id),
            is_error=True,
        )
