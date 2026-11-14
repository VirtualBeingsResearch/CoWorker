from __future__ import annotations

import asyncio
import json
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING, Literal, cast

import httpx
from loguru import logger

from coworker.core.types import IncomingEvent
from coworker.i18n import bind_locale, tr
from coworker.tools.alarm.conditions import (
    MISSING_MATCH_STATE,
    MISSING_STATE,
    AlarmCondition,
    ConditionChecker,
    ConditionKind,
    TaskStoreLike,
    local_datetime,
    local_now,
    parse_optional_time,
)
from coworker.tools.alarm.constants import (
    CONDITION_KINDS,
    MAX_CHECKS_PER_HOUR,
)

if TYPE_CHECKING:
    from coworker.agent.inbox_watcher import InboxWatcher


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
        trigger_at = local_datetime(trigger_at)
        delay = max(0.0, (trigger_at - local_now()).total_seconds())
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
        tasks = [task for task, *_ in self._alarms.values()]
        tasks.extend(
            entry.task for entry in self._conditions.values() if entry.task is not None
        )
        pending = [task for task in tasks if not task.done()]
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
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
            next_trigger = local_datetime(datetime.fromisoformat(entry["next_trigger_at"]))
            message = str(entry["message"])
            repeat_seconds = entry.get("repeat_seconds")
        except (KeyError, TypeError, ValueError) as error:
            logger.warning(f"Skipping invalid alarm record {entry.get('alarm_id')}: {error}")
            return False
        now = local_now()
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
        if kind not in CONDITION_KINDS:
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
            and last_state not in {MISSING_STATE, MISSING_MATCH_STATE}
        ):
            last_state = ""
        # A condition that no longer passes validation (for example after the
        # allowed roots shrank) is restored paused so it neither burns checks nor
        # fails silently; the reason shows up in list_alarms and the admin panel.
        try:
            self._checker.validate_target(condition)
        except ValueError as error:
            logger.warning(f"Condition alarm {alarm_id} cannot run: {error}")
            validation_error = str(error)
        else:
            validation_error = ""
        restored = ConditionAlarm(
            alarm_id=alarm_id,
            message=str(entry.get("message") or ""),
            condition=condition,
            every_seconds=every_seconds,
            last_state=last_state,
            last_stat=str(entry.get("last_stat") or ""),
            last_error=str(entry.get("last_error") or "") or validation_error,
            last_checked_at=parse_optional_time(entry.get("last_checked_at")),
            failures=max(0, int(entry.get("failures") or 0)),
            paused=bool(entry.get("paused")) or bool(validation_error),
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
                next_trigger = local_now() + timedelta(seconds=repeat_seconds)
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
                    entry.last_checked_at = local_now()
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
        while entry.checks and entry.checks[0] <= cutoff:
            entry.checks.popleft()
        if len(entry.checks) >= MAX_CHECKS_PER_HOUR:
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
        entry.last_checked_at = local_now()
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

    def resume_condition(self, alarm_id: str) -> bool:
        """Restart a paused condition; returns False when the alarm is unknown."""
        entry = self._conditions.get(alarm_id)
        if entry is None:
            return False
        if entry.paused:
            entry.paused = False
            entry.failures = 0
            entry.last_error = ""
            entry.next_check_at = None
            entry.checks.clear()
            self._start_condition(entry)
            self._save_conditions()
        return True

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
                "pattern": entry.condition.pattern,
                "status": entry.condition.status,
                "stale_seconds": entry.condition.stale_seconds,
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


