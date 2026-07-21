from __future__ import annotations

import asyncio
import json
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING

from loguru import logger

from coworker.core.types import IncomingEvent, ToolResult
from coworker.tools.base import Tool, ToolDefinition

if TYPE_CHECKING:
    from coworker.agent.inbox_watcher import InboxWatcher


class AlarmManager:
    def __init__(self, inbox: InboxWatcher, persist_path: Path | None = None) -> None:
        self._inbox = inbox
        self._persist_path = persist_path
        # alarm_id -> (task, next_trigger_at, message, repeat_seconds)
        self._alarms: dict[str, tuple[asyncio.Task, datetime, str, int | None]] = {}

    async def set(
        self,
        alarm_id: str,
        trigger_at: datetime,
        message: str,
        repeat_seconds: int | None = None,
    ) -> None:
        self._cancel(alarm_id)
        delay = max(0.0, (trigger_at - datetime.now()).total_seconds())
        task = asyncio.create_task(self._fire(alarm_id, delay, message, repeat_seconds))
        self._alarms[alarm_id] = (task, trigger_at, message, repeat_seconds)
        self._save()

    async def restore(self) -> int:
        if not self._persist_path or not self._persist_path.exists():
            return 0
        try:
            data = json.loads(self._persist_path.read_text(encoding="utf-8"))
        except Exception as e:
            logger.warning(f"Failed to read alarm state from {self._persist_path}: {e}")
            return 0

        count = 0
        now = datetime.now()
        for entry in data:
            alarm_id = entry["alarm_id"]
            next_trigger = datetime.fromisoformat(entry["next_trigger_at"])
            message = entry["message"]
            repeat_seconds = entry.get("repeat_seconds")
            delay = max(0.0, (next_trigger - now).total_seconds())
            overdue_note: str | None = None
            if next_trigger < now:
                late = int((now - next_trigger).total_seconds())
                overdue_note = f"迟到 {late} 秒触发"
            task = asyncio.create_task(
                self._fire(alarm_id, delay, message, repeat_seconds, overdue_note=overdue_note)
            )
            self._alarms[alarm_id] = (task, next_trigger, message, repeat_seconds)
            count += 1

        if count > 0:
            logger.info(f"Restored {count} alarm(s) from {self._persist_path}")
        return count

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
            display = f"{message}（{overdue_note}）" if overdue_note else message
            event = IncomingEvent(
                participant_id="alarm",
                content=f"[闹钟提醒] {alarm_id}: {display}",
                timestamp=datetime.now(),
                source="alarm",
            )
            await self._inbox.push(event)
            logger.info(f"Alarm fired: [{alarm_id}] {message}")

            if repeat_seconds and repeat_seconds > 0:
                next_trigger = datetime.now() + timedelta(seconds=repeat_seconds)
                new_task = asyncio.create_task(
                    self._fire(alarm_id, float(repeat_seconds), message, repeat_seconds)
                )
                self._alarms[alarm_id] = (new_task, next_trigger, message, repeat_seconds)
                self._save()
            else:
                self._alarms.pop(alarm_id, None)
                self._save()
        except asyncio.CancelledError:
            pass  # 状态由 _cancel()/_save() 在外部管理，event loop 关闭时不应抹掉持久化

    def _cancel(self, alarm_id: str) -> bool:
        entry = self._alarms.pop(alarm_id, None)
        if entry:
            task, *_ = entry
            task.cancel()
            return True
        return False

    def cancel(self, alarm_id: str) -> bool:
        result = self._cancel(alarm_id)
        if result:
            self._save()
        return result

    def list(self) -> list[dict]:
        result = []
        for alarm_id, (task, trigger_at, message, repeat_seconds) in self._alarms.items():
            result.append({
                "id": alarm_id,
                "trigger_at": trigger_at.strftime("%Y-%m-%d %H:%M:%S"),
                "message": message,
                "repeat_seconds": repeat_seconds,
                "done": task.done(),
            })
        return result

    def _save(self) -> None:
        if not self._persist_path:
            return
        try:
            self._persist_path.parent.mkdir(parents=True, exist_ok=True)
            records = [
                {
                    "alarm_id": alarm_id,
                    "next_trigger_at": trigger_at.strftime("%Y-%m-%d %H:%M:%S"),
                    "message": message,
                    "repeat_seconds": repeat_seconds,
                }
                for alarm_id, (_, trigger_at, message, repeat_seconds) in self._alarms.items()
            ]
            self._persist_path.write_text(
                json.dumps(records, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        except Exception as e:
            logger.warning(f"Failed to save alarm state: {e}")


class SetAlarmTool(Tool):
    def __init__(self, manager: AlarmManager) -> None:
        self._manager = manager

    @property
    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="set_alarm",
            description=(
                "设置一个定时闹钟，在指定时间向自己发送提醒消息，唤醒自己处理事项。"
                "支持一次性或循环触发。重启后闹钟状态会自动恢复。"
            ),
            parameters={
                "type": "object",
                "properties": {
                    "trigger_at": {
                        "type": "string",
                        "description": "首次触发时间，格式 YYYY-MM-DD HH:MM:SS",
                    },
                    "message": {
                        "type": "string",
                        "description": "提醒内容",
                    },
                    "alarm_id": {
                        "type": "string",
                        "description": "闹钟 ID，可选，用于后续取消。不填则自动生成。",
                    },
                    "repeat_seconds": {
                        "type": "integer",
                        "description": "循环间隔秒数。不填或为 0 表示一次性闹钟；填入正整数则每隔 N 秒重复触发，直到手动取消。",
                    },
                },
                "required": ["trigger_at", "message"],
            },
        )

    async def execute(
        self,
        trigger_at: str,
        message: str,
        alarm_id: str | None = None,
        repeat_seconds: int | None = None,
        **_,
    ) -> ToolResult:
        try:
            dt = datetime.fromisoformat(trigger_at)
        except ValueError:
            return ToolResult(
                tool_call_id="",
                content="时间格式错误，请使用 YYYY-MM-DD HH:MM:SS",
                is_error=True,
            )

        delay = (dt - datetime.now()).total_seconds()
        if delay < 0:
            return ToolResult(
                tool_call_id="",
                content=f"指定的时间 {trigger_at} 已过去",
                is_error=True,
            )

        if not alarm_id:
            alarm_id = uuid.uuid4().hex[:8]

        repeat = repeat_seconds if repeat_seconds and repeat_seconds > 0 else None
        await self._manager.set(alarm_id, dt, message, repeat)

        mode = f"每 {repeat} 秒循环" if repeat else "一次性"
        return ToolResult(
            tool_call_id="",
            content=f"闹钟已设置：[{alarm_id}] 将在 {trigger_at} 触发（{delay:.0f} 秒后），{mode}",
        )


class ListAlarmsTool(Tool):
    def __init__(self, manager: AlarmManager) -> None:
        self._manager = manager

    @property
    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="list_alarms",
            description="列出所有待触发的闹钟，包含 ID、触发时间、内容和是否循环。",
            parameters={"type": "object", "properties": {}, "required": []},
        )

    async def execute(self, **_) -> ToolResult:
        alarms = self._manager.list()
        if not alarms:
            return ToolResult(tool_call_id="", content="当前没有待触发的闹钟")
        lines = []
        for a in alarms:
            mode = f"每 {a['repeat_seconds']} 秒重复" if a["repeat_seconds"] else "一次性"
            lines.append(f"- [{a['id']}] {a['trigger_at']} | {mode} | {a['message']}")
        return ToolResult(tool_call_id="", content="待触发闹钟：\n" + "\n".join(lines))


class CancelAlarmTool(Tool):
    def __init__(self, manager: AlarmManager) -> None:
        self._manager = manager

    @property
    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="cancel_alarm",
            description="取消指定 ID 的闹钟（一次性或循环）。",
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
            return ToolResult(tool_call_id="", content=f"闹钟 [{alarm_id}] 已取消")
        return ToolResult(
            tool_call_id="",
            content=f"未找到闹钟 [{alarm_id}]",
            is_error=True,
        )
