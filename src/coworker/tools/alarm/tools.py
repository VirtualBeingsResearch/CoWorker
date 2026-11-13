from __future__ import annotations

import re
import uuid
from datetime import datetime
from typing import cast

from coworker.core.types import ToolResult
from coworker.i18n import tr
from coworker.tools.alarm.conditions import AlarmCondition, ConditionKind
from coworker.tools.alarm.constants import (
    CONDITION_KINDS,
    DEFAULT_INTERVAL_SECONDS,
    MAX_MESSAGE_CHARS,
    MIN_CONDITION_INTERVAL_SECONDS,
)
from coworker.tools.alarm.manager import AlarmManager, _local_datetime, _local_now
from coworker.tools.base import Tool, ToolDefinition


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
                        "default": DEFAULT_INTERVAL_SECONDS,
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
        if len(message) > MAX_MESSAGE_CHARS:
            return self._error("tool_result.alarm.message_too_long", limit=MAX_MESSAGE_CHARS)

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
        if condition not in CONDITION_KINDS:
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
        minimum = max(MIN_CONDITION_INTERVAL_SECONDS, self._manager.min_interval_seconds)
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
        interval = every_seconds if every_seconds > 0 else DEFAULT_INTERVAL_SECONDS
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



