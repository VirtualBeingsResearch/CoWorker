from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import json
import re
import socket
import time
import uuid
from collections import deque
from collections.abc import Sequence
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING, Literal, Protocol, cast
from urllib.parse import urljoin, urlsplit

import httpx
from loguru import logger

from coworker.core.types import IncomingEvent, ToolResult
from coworker.i18n import bind_locale, tr
from coworker.tools.base import Tool, ToolDefinition

if TYPE_CHECKING:
    from coworker.agent.inbox_watcher import InboxWatcher

ConditionKind = Literal["file_changed", "http_changed", "task_state"]

_CONDITION_KINDS: frozenset[str] = frozenset({"file_changed", "http_changed", "task_state"})
_MAX_CHECKS_PER_HOUR = 60
_MIN_CONDITION_INTERVAL_SECONDS = 60
_MAX_REDIRECTS = 3
_USER_AGENT = "CoworkerConditionAlarm/1.0"
_DEFAULT_CHECK_TIMEOUT_SECONDS = 20.0
_DEFAULT_HTTP_MAX_BYTES = 256 * 1024
_DEFAULT_FILE_MAX_BYTES = 8 * 1024 * 1024


def _local_datetime(value: datetime) -> datetime:
    """Return an aware datetime in the runtime's local timezone."""
    return value.astimezone()


def _local_now() -> datetime:
    return datetime.now().astimezone()


def _parse_iso(value: str) -> datetime | None:
    text = (value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return _local_datetime(parsed)


class _TaskLike(Protocol):
    id: str
    status: str
    updated_at: str


class _TaskStoreLike(Protocol):
    def get(self, task_id: str) -> _TaskLike | None: ...

    def list(self) -> Sequence[_TaskLike]: ...


@dataclass(frozen=True)
class AlarmCondition:
    kind: ConditionKind
    target: str = ""
    pattern: str = ""
    status: str = ""
    stale_seconds: int = 0


@dataclass
class ConditionAlarm:
    alarm_id: str
    message: str
    condition: AlarmCondition
    every_seconds: int
    task: asyncio.Task[None] | None = None
    last_state: str = ""
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
        task_store: _TaskStoreLike | None = None,
        allowed_roots: list[Path] | None = None,
        max_conditions: int = 32,
        min_interval_seconds: int = 60,
        http_max_bytes: int = _DEFAULT_HTTP_MAX_BYTES,
        check_timeout_seconds: float = _DEFAULT_CHECK_TIMEOUT_SECONDS,
        file_max_bytes: int = _DEFAULT_FILE_MAX_BYTES,
        http_transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._inbox = inbox
        self._persist_path = persist_path
        # alarm_id -> (task, next_trigger_at, message, repeat_seconds)
        self._alarms: dict[str, tuple[asyncio.Task, datetime, str, int | None]] = {}
        self._conditions: dict[str, ConditionAlarm] = {}
        self._task_store = task_store
        roots = allowed_roots or [Path.cwd()]
        self._allowed_roots = [root.expanduser().resolve() for root in roots]
        self._max_conditions = max(1, max_conditions)
        self._min_interval_seconds = max(1, min_interval_seconds)
        self._http_max_bytes = max(1024, http_max_bytes)
        self._check_timeout_seconds = max(1.0, check_timeout_seconds)
        self._file_max_bytes = max(1024, file_max_bytes)
        self._http_transport = http_transport

    @property
    def max_conditions(self) -> int:
        return self._max_conditions

    @property
    def condition_count(self) -> int:
        return len(self._conditions)

    @property
    def min_interval_seconds(self) -> int:
        return self._min_interval_seconds

    def has_condition(self, alarm_id: str) -> bool:
        return alarm_id in self._conditions

    def get_condition(self, alarm_id: str) -> ConditionAlarm | None:
        return self._conditions.get(alarm_id)

    async def set(
        self,
        alarm_id: str,
        trigger_at: datetime,
        message: str,
        repeat_seconds: int | None = None,
    ) -> None:
        self._cancel(alarm_id)
        trigger_at = _local_datetime(trigger_at)
        delay = max(0.0, (trigger_at - _local_now()).total_seconds())
        task = asyncio.create_task(
            bind_locale(partial(self._fire, alarm_id, delay, message, repeat_seconds))
        )
        self._alarms[alarm_id] = (task, trigger_at, message, repeat_seconds)
        self._save()

    def add_condition(
        self,
        alarm_id: str,
        message: str,
        condition: AlarmCondition,
        every_seconds: int,
    ) -> ConditionAlarm:
        if alarm_id not in self._conditions and len(self._conditions) >= self._max_conditions:
            raise ValueError(tr("tool_result.alarm.condition_limit", limit=self._max_conditions))
        self._cancel(alarm_id)
        entry = ConditionAlarm(
            alarm_id=alarm_id,
            message=message,
            condition=condition,
            every_seconds=max(self._min_interval_seconds, every_seconds),
        )
        self._conditions[alarm_id] = entry
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
        self._save()

    def _start_condition(self, entry: ConditionAlarm) -> None:
        if entry.paused:
            return
        entry.task = asyncio.create_task(
            bind_locale(partial(self._condition_loop, entry.alarm_id))
        )

    async def restore(self) -> int:
        if not self._persist_path or not self._persist_path.exists():
            return 0
        try:
            data = json.loads(self._persist_path.read_text(encoding="utf-8"))
        except Exception as e:
            logger.warning(f"Failed to read alarm state from {self._persist_path}: {e}")
            return 0
        if not isinstance(data, list):
            logger.warning(f"Alarm state is not a list: {self._persist_path}")
            return 0

        count = 0
        now = _local_now()
        for entry in data:
            if not isinstance(entry, dict) or not entry.get("alarm_id"):
                continue
            if entry.get("kind") == "condition":
                if self._restore_condition(entry):
                    count += 1
                continue

            alarm_id = str(entry["alarm_id"])
            next_trigger = _local_datetime(datetime.fromisoformat(entry["next_trigger_at"]))
            message = str(entry["message"])
            repeat_seconds = entry.get("repeat_seconds")
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
            count += 1

        if count > 0:
            logger.info(f"Restored {count} trigger(s) from {self._persist_path}")
        return count

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
            every_seconds = max(self._min_interval_seconds, int(entry.get("every_seconds") or 60))
            alarm_id = str(entry["alarm_id"])
        except (TypeError, ValueError) as error:
            logger.warning(f"Skipping invalid condition alarm {entry.get('alarm_id')}: {error}")
            return False

        self._cancel(alarm_id)
        restored = ConditionAlarm(
            alarm_id=alarm_id,
            message=str(entry.get("message") or ""),
            condition=condition,
            every_seconds=every_seconds,
            last_state=str(entry.get("last_state") or ""),
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
            event = IncomingEvent(
                participant_id="alarm",
                content=tr("tool_result.alarm.reminder", id=alarm_id, message=display),
                timestamp=datetime.now(),
                source="alarm",
            )
            await self._inbox.push(event)
            logger.info(f"Alarm fired: [{alarm_id}] {message}")

            if repeat_seconds and repeat_seconds > 0:
                next_trigger = _local_now() + timedelta(seconds=repeat_seconds)
                new_task = asyncio.create_task(
                    bind_locale(
                        partial(
                            self._fire,
                            alarm_id,
                            float(repeat_seconds),
                            message,
                            repeat_seconds,
                        )
                    )
                )
                self._alarms[alarm_id] = (new_task, next_trigger, message, repeat_seconds)
                self._save()
            else:
                self._alarms.pop(alarm_id, None)
                self._save()
        except asyncio.CancelledError:
            pass  # 状态由 _cancel()/_save() 在外部管理，event loop 关闭时不应抹掉持久化

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
                entry.next_check_at = _local_now()
                if not self._admit_check(entry):
                    delay = float(entry.every_seconds)
                    continue
                try:
                    await asyncio.wait_for(
                        self.check_once(alarm_id), timeout=self._check_timeout_seconds
                    )
                except asyncio.CancelledError:
                    raise
                except Exception as error:
                    entry.failures += 1
                    logger.warning(f"Condition alarm {alarm_id} check failed: {error}")
                    if entry.failures >= 5:
                        entry.paused = True
                        self._save()
                        await self._push_condition_failure(entry, error)
                        return
                    self._save()
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
        """Run one condition check and fire when the state changed."""
        entry = self._conditions.get(alarm_id)
        if entry is None:
            raise KeyError(alarm_id)
        state, degraded = await self._check_condition(entry.condition)
        fired = False
        if entry.last_state and state != entry.last_state:
            await self._fire_condition(entry, degraded)
            fired = True
        entry.last_state = state
        entry.next_check_at = _local_now() + timedelta(seconds=entry.every_seconds)
        self._save()
        return fired

    async def _check_condition(self, condition: AlarmCondition) -> tuple[str, bool]:
        if condition.kind == "file_changed":
            return await asyncio.to_thread(self._check_file, condition)
        if condition.kind == "http_changed":
            return await self._check_http(condition)
        if condition.kind == "task_state":
            return await asyncio.to_thread(self._check_task, condition)
        raise ValueError(f"unknown condition kind: {condition.kind}")

    def _check_file(self, condition: AlarmCondition) -> tuple[str, bool]:
        path = self._resolve_file_target(condition.target)
        stat = path.stat()
        if stat.st_size > self._file_max_bytes:
            return f"{stat.st_mtime_ns}:{stat.st_size}", True
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        return f"{stat.st_mtime_ns}:{stat.st_size}:{digest.hexdigest()}", False

    async def _check_http(self, condition: AlarmCondition) -> tuple[str, bool]:
        if not condition.target:
            raise ValueError("http condition requires a target URL")
        url = condition.target
        async with httpx.AsyncClient(
            follow_redirects=False,
            timeout=self._check_timeout_seconds,
            transport=self._http_transport,
        ) as client:
            for _ in range(_MAX_REDIRECTS + 1):
                await asyncio.to_thread(_validate_public_http_url, url)
                async with client.stream(
                    "GET", url, headers={"User-Agent": _USER_AGENT}
                ) as response:
                    if response.is_redirect:
                        location = response.headers.get("location")
                        if not location:
                            raise ValueError("redirect without a location")
                        url = urljoin(url, location)
                        continue
                    response.raise_for_status()
                    body = bytearray()
                    async for chunk in response.aiter_bytes():
                        body.extend(chunk)
                        if len(body) > self._http_max_bytes:
                            raise ValueError("response exceeds the byte limit")
                    text = bytes(body).decode("utf-8", errors="replace")
                    if condition.pattern:
                        match = re.search(condition.pattern, text, re.S)
                        if match is None:
                            raise ValueError("pattern was not found")
                        text = match.group(1) if match.groups() else match.group(0)
                    normalized = " ".join(text.split())
                    return hashlib.sha256(normalized.encode("utf-8")).hexdigest(), False
        raise ValueError("too many redirects")

    def _check_task(self, condition: AlarmCondition) -> tuple[str, bool]:
        if self._task_store is None:
            raise ValueError("task state condition requires a task store")
        tasks = list(self._task_store.list())
        if condition.target:
            task = self._task_store.get(condition.target)
            if task is None:
                return "missing", False
            tasks = [task]

        parts: list[str] = []
        if condition.status:
            matching = sorted(task.id for task in tasks if task.status == condition.status)
            parts.append("status:" + ",".join(matching))
        if condition.stale_seconds > 0:
            now = _local_now()
            stale = sorted(
                task.id for task in tasks if _task_is_stale(task, condition.stale_seconds, now)
            )
            parts.append("stale:" + ",".join(stale))
        if not parts:
            raise ValueError("task condition requires status or stale_seconds")
        return "|".join(parts), False

    def _resolve_file_target(self, target: str) -> Path:
        text = (target or "").strip()
        if not text:
            raise ValueError("file condition requires a target path")
        path = Path(text).expanduser()
        if not path.is_absolute():
            path = Path.cwd() / path
        resolved = path.resolve()
        if not any(
            resolved == root or root in resolved.parents for root in self._allowed_roots
        ):
            raise ValueError("path is outside the allowed roots")
        return resolved

    async def _fire_condition(self, entry: ConditionAlarm, degraded: bool) -> None:
        event = IncomingEvent(
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
        await self._inbox.push(event)
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

    def _cancel(self, alarm_id: str) -> bool:
        entry = self._alarms.pop(alarm_id, None)
        if entry:
            task, *_ = entry
            task.cancel()
            return True
        condition = self._conditions.pop(alarm_id, None)
        if condition:
            if condition.task is not None:
                condition.task.cancel()
            return True
        return False

    def cancel(self, alarm_id: str) -> bool:
        result = self._cancel(alarm_id)
        if result:
            self._save()
        return result

    def list(self) -> list[dict]:
        result: list[dict] = []
        for alarm_id, (task, trigger_at, message, repeat_seconds) in self._alarms.items():
            result.append(
                {
                    "id": alarm_id,
                    "kind": "time",
                    "trigger_at": trigger_at.isoformat(timespec="seconds"),
                    "message": message,
                    "repeat_seconds": repeat_seconds,
                    "done": task.done(),
                }
            )
        for alarm_id, entry in self._conditions.items():
            result.append(
                {
                    "id": alarm_id,
                    "kind": "condition",
                    "condition": entry.condition.kind,
                    "target": entry.condition.target,
                    "every_seconds": entry.every_seconds,
                    "message": entry.message,
                    "failures": entry.failures,
                    "paused": entry.paused,
                    "last_state": entry.last_state,
                    "next_check_at": (
                        entry.next_check_at.isoformat(timespec="seconds")
                        if entry.next_check_at
                        else None
                    ),
                    "done": entry.task.done() if entry.task is not None else True,
                }
            )
        return result

    def _save(self) -> None:
        if not self._persist_path:
            return
        try:
            self._persist_path.parent.mkdir(parents=True, exist_ok=True)
            records: list[dict] = [
                {
                    "alarm_id": alarm_id,
                    "next_trigger_at": trigger_at.isoformat(timespec="seconds"),
                    "message": message,
                    "repeat_seconds": repeat_seconds,
                }
                for alarm_id, (_, trigger_at, message, repeat_seconds) in self._alarms.items()
            ]
            records.extend(
                {
                    "alarm_id": entry.alarm_id,
                    "kind": "condition",
                    "message": entry.message,
                    "condition": asdict(entry.condition),
                    "every_seconds": entry.every_seconds,
                    "last_state": entry.last_state,
                    "failures": entry.failures,
                    "paused": entry.paused,
                }
                for entry in self._conditions.values()
            )
            temp_path = self._persist_path.with_suffix(self._persist_path.suffix + ".tmp")
            temp_path.write_text(
                json.dumps(records, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            temp_path.replace(self._persist_path)
        except Exception as e:
            logger.warning(f"Failed to save alarm state: {e}")


def _task_is_stale(task: _TaskLike, seconds: int, now: datetime) -> bool:
    if task.status not in {"pending", "in_progress"}:
        return False
    updated = _parse_iso(task.updated_at)
    if updated is None:
        return False
    return (now - updated).total_seconds() >= seconds


def _validate_public_http_url(url: str) -> None:
    parsed = urlsplit((url or "").strip())
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("only http/https URLs with a host are allowed")
    if parsed.username or parsed.password:
        raise ValueError("credentials in the URL are not allowed")
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    infos = socket.getaddrinfo(parsed.hostname, port, type=socket.SOCK_STREAM)
    if not infos:
        raise ValueError("the host did not resolve")
    for info in infos:
        address = info[4][0]
        ip = ipaddress.ip_address(address)
        if not ip.is_global:
            raise ValueError("private or reserved addresses are not allowed")


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
        if every_seconds and every_seconds < max(
            _MIN_CONDITION_INTERVAL_SECONDS, self._manager.min_interval_seconds
        ):
            return self._error(
                "tool_result.alarm.interval_too_small",
                seconds=max(
                    _MIN_CONDITION_INTERVAL_SECONDS,
                    self._manager.min_interval_seconds,
                ),
            )
        if not alarm_id:
            alarm_id = uuid.uuid4().hex[:8]
        if (
            not self._manager.has_condition(alarm_id)
            and self._manager.condition_count >= self._manager.max_conditions
        ):
            return self._error(
                "tool_result.alarm.condition_limit",
                limit=self._manager.max_conditions,
            )

        alarm_condition = AlarmCondition(
            kind=cast(ConditionKind, condition),
            target=target,
            pattern=pattern,
            status=status,
            stale_seconds=max(0, stale_seconds),
        )
        interval = every_seconds if every_seconds > 0 else 300
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
