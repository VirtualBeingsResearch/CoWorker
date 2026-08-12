from __future__ import annotations

import asyncio
import time
from unittest.mock import AsyncMock, MagicMock

import pytest

from coworker.agent.loop import AgentLoop
from coworker.core.types import AgentState, IncomingEvent
from coworker.memory.short_term import ShortTermMemory
from coworker.tools.reasoning_tools import TaskStore


def _make_loop(task_store, *, interval=10, seconds=300, is_sleeping=False, cooldown_exhausted=True):
    inbox = MagicMock()
    inbox.push = AsyncMock()

    state = AgentState()
    state.cycle_count = 0
    state.is_sleeping = is_sleeping

    loop = AgentLoop.__new__(AgentLoop)
    loop._short_term = ShortTermMemory()
    loop._inbox = inbox
    loop._ilog = None
    loop._task_store = task_store
    loop._task_reminder_interval = interval
    loop._task_reminder_seconds = seconds
    # cooldown_exhausted=True 表示上次提醒时间很久以前（冷却已过）
    loop._last_task_reminder_cycle = 0
    loop._last_task_reminder_time = 0.0 if cooldown_exhausted else time.monotonic()
    loop._stop_event = asyncio.Event()
    loop._environment_runtime = None
    loop.state = state
    return loop


class TestTaskReminderCooldown:
    @pytest.mark.asyncio
    async def test_injects_when_cooldown_passed_by_cycles(self, tmp_path):
        store = TaskStore(tmp_path / "tasks.json")
        store.create("做某件事")
        loop = _make_loop(store, interval=5, seconds=9999)
        loop.state.cycle_count = 5  # 满足周期条件

        await loop._task_reminder()

        assert any("[任务提醒]" in str(m.content) for m in loop._short_term.primary)
        assert loop._short_term.primary[-1].source == "task_reminder"
        content = str(loop._short_term.primary[-1].content)
        # 简单提醒：不再注入任务全文
        assert "做某件事" not in content

    @pytest.mark.asyncio
    async def test_injects_when_cooldown_passed_by_time(self, tmp_path):
        store = TaskStore(tmp_path / "tasks.json")
        store.create("做某件事")
        loop = _make_loop(store, interval=9999, seconds=0)  # 时间阈值=0 立即满足

        await loop._task_reminder()

        assert any("[任务提醒]" in str(m.content) for m in loop._short_term.primary)

    @pytest.mark.asyncio
    async def test_no_inject_within_cooldown(self, tmp_path):
        store = TaskStore(tmp_path / "tasks.json")
        store.create("做某件事")
        # 两个冷却都未到：周期间隔大，时间间隔大且刚刚提醒过
        loop = _make_loop(store, interval=9999, seconds=9999, cooldown_exhausted=False)

        await loop._task_reminder()

        assert not any("[任务提醒]" in str(m.content) for m in loop._short_term.primary)

    @pytest.mark.asyncio
    async def test_no_inject_when_no_active_tasks(self, tmp_path):
        store = TaskStore(tmp_path / "tasks.json")
        loop = _make_loop(store, interval=0, seconds=0)

        await loop._task_reminder()

        assert not loop._short_term.primary

    @pytest.mark.asyncio
    async def test_no_inject_when_task_completed(self, tmp_path):
        store = TaskStore(tmp_path / "tasks.json")
        t = store.create("做某件事")
        store.update(t.id, status="completed")
        loop = _make_loop(store, interval=0, seconds=0)

        await loop._task_reminder()

        assert not loop._short_term.primary

    @pytest.mark.asyncio
    async def test_purges_completed_tasks_on_cooldown(self, tmp_path):
        store = TaskStore(tmp_path / "tasks.json")
        t = store.create("已完成的任务")
        store.update(t.id, status="completed")
        loop = _make_loop(store, interval=0, seconds=0)

        await loop._task_reminder()

        assert store.get(t.id) is None  # 已被清理

    @pytest.mark.asyncio
    async def test_no_purge_within_cooldown(self, tmp_path):
        store = TaskStore(tmp_path / "tasks.json")
        t = store.create("已完成的任务")
        store.update(t.id, status="completed")
        loop = _make_loop(store, interval=9999, seconds=9999, cooldown_exhausted=False)

        await loop._task_reminder()

        assert store.get(t.id) is not None  # 冷却未到，不清理

    @pytest.mark.asyncio
    async def test_updates_last_reminder_tracking(self, tmp_path):
        store = TaskStore(tmp_path / "tasks.json")
        store.create("做某件事")
        loop = _make_loop(store, interval=0, seconds=0)
        loop.state.cycle_count = 3

        await loop._task_reminder()

        assert loop._last_task_reminder_cycle == 3
        assert loop._last_task_reminder_time > 0

    @pytest.mark.asyncio
    async def test_logs_to_ilog(self, tmp_path):
        store = TaskStore(tmp_path / "tasks.json")
        store.create("做某件事")
        loop = _make_loop(store, interval=0, seconds=0)
        ilog = MagicMock()
        loop._ilog = ilog

        await loop._task_reminder()

        ilog.log_task_reminder.assert_called_once()
        _, kwargs = ilog.log_task_reminder.call_args
        assert kwargs.get("source") == "cycle" or ilog.log_task_reminder.call_args[0][1] == "cycle"

    @pytest.mark.asyncio
    async def test_reminder_does_not_inject_task_details(self, tmp_path):
        store = TaskStore(tmp_path / "tasks.json")
        store.create("做某件事", details="SECRET TASK DETAILS")
        loop = _make_loop(store, interval=0, seconds=0)

        await loop._task_reminder()

        content = str(loop._short_term.primary[-1].content)
        assert "SECRET TASK DETAILS" not in content

    @pytest.mark.asyncio
    async def test_logs_has_details_flag(self, tmp_path):
        store = TaskStore(tmp_path / "tasks.json")
        store.create("做某件事", details="SECRET TASK DETAILS")
        loop = _make_loop(store, interval=0, seconds=0)
        ilog = MagicMock()
        loop._ilog = ilog

        await loop._task_reminder()

        tasks = ilog.log_task_reminder.call_args[0][0]
        assert tasks[0]["has_details"] is True
        assert tasks[0]["created_at"]
        assert tasks[0]["updated_at"]
        assert "details" not in tasks[0]


class TestTaskWatcher:
    def _make_watcher_loop(self, store, *, is_sleeping):
        loop = _make_loop(store, seconds=0.01, is_sleeping=is_sleeping)
        # push 后立即设置 stop_event，让 watcher 在下一轮退出
        async def push_and_stop(event):
            loop._stop_event.set()
        loop._inbox.push = AsyncMock(side_effect=push_and_stop)
        return loop

    @pytest.mark.asyncio
    async def test_pushes_to_inbox_when_sleeping(self, tmp_path):
        store = TaskStore(tmp_path / "tasks.json")
        store.create("待完成任务")
        loop = self._make_watcher_loop(store, is_sleeping=True)

        await asyncio.wait_for(loop._task_watcher(), timeout=1.0)

        loop._inbox.push.assert_called_once()
        event: IncomingEvent = loop._inbox.push.call_args[0][0]
        assert "任务提醒" in event.content
        assert "待完成任务" not in event.content  # 简单唤醒，不注入任务全文
        assert event.source == "task_reminder"

    @pytest.mark.asyncio
    async def test_wake_does_not_push_task_details(self, tmp_path):
        store = TaskStore(tmp_path / "tasks.json")
        store.create("待完成任务", details="SECRET TASK DETAILS")
        loop = self._make_watcher_loop(store, is_sleeping=True)

        await asyncio.wait_for(loop._task_watcher(), timeout=1.0)

        event: IncomingEvent = loop._inbox.push.call_args[0][0]
        assert "任务提醒" in event.content
        assert "SECRET TASK DETAILS" not in event.content

    @pytest.mark.asyncio
    async def test_no_push_when_not_sleeping(self, tmp_path):
        store = TaskStore(tmp_path / "tasks.json")
        store.create("待完成任务")
        loop = _make_loop(store, seconds=0.01, is_sleeping=False)
        # 不会 push，所以需要在超时后验证；用 stop_event 控制退出
        async def stop_after_one_tick():
            await asyncio.sleep(0.05)
            loop._stop_event.set()
        stop_task = asyncio.create_task(stop_after_one_tick())

        await asyncio.wait_for(loop._task_watcher(), timeout=1.0)
        await stop_task

        loop._inbox.push.assert_not_called()

    @pytest.mark.asyncio
    async def test_no_push_when_no_active_tasks(self, tmp_path):
        store = TaskStore(tmp_path / "tasks.json")
        loop = _make_loop(store, seconds=0.01, is_sleeping=True)
        async def stop_after_one_tick():
            await asyncio.sleep(0.05)
            loop._stop_event.set()
        stop_task = asyncio.create_task(stop_after_one_tick())

        await asyncio.wait_for(loop._task_watcher(), timeout=1.0)
        await stop_task

        loop._inbox.push.assert_not_called()

    @pytest.mark.asyncio
    async def test_exits_when_stop_event_set(self, tmp_path):
        store = TaskStore(tmp_path / "tasks.json")
        store.create("待完成任务")
        loop = _make_loop(store, seconds=60, is_sleeping=True)
        loop._stop_event.set()

        await asyncio.wait_for(loop._task_watcher(), timeout=1.0)

        loop._inbox.push.assert_not_called()

    @pytest.mark.asyncio
    async def test_logs_to_ilog_on_sleep_interrupt(self, tmp_path):
        store = TaskStore(tmp_path / "tasks.json")
        store.create("待完成任务")
        loop = self._make_watcher_loop(store, is_sleeping=True)
        ilog = MagicMock()
        loop._ilog = ilog

        await asyncio.wait_for(loop._task_watcher(), timeout=1.0)

        ilog.log_task_reminder.assert_called_once()
        args = ilog.log_task_reminder.call_args
        source = args[0][1] if len(args[0]) > 1 else args[1].get("source")
        assert source == "sleep_interrupt"


class TestTaskBoardSync:
    def _board(self, loop):
        return next(
            (item for item in loop._short_term.pinned_items if item.pin_id == "task_board"), None
        )

    def test_pins_single_board(self, tmp_path):
        store = TaskStore(tmp_path / "tasks.json")
        t = store.create("做某件事")
        loop = _make_loop(store, interval=0, seconds=0)

        loop._sync_task_pins()

        board = self._board(loop)
        assert board is not None
        assert board.system_managed is True
        assert board.content.startswith(f"- [{t.id}]")
        assert "做某件事" in board.content

    def test_board_marks_details_without_content(self, tmp_path):
        store = TaskStore(tmp_path / "tasks.json")
        store.create("做某件事", details="SECRET TASK DETAILS")
        loop = _make_loop(store, interval=0, seconds=0)

        loop._sync_task_pins()

        board = self._board(loop)
        assert board is not None
        assert "has_details=true" in board.content
        assert "task_get" in board.content
        assert "SECRET TASK DETAILS" not in board.content

    def test_no_board_when_no_active_tasks(self, tmp_path):
        store = TaskStore(tmp_path / "tasks.json")
        loop = _make_loop(store, interval=0, seconds=0)

        loop._sync_task_pins()

        assert self._board(loop) is None

    def test_unpins_board_when_all_completed(self, tmp_path):
        store = TaskStore(tmp_path / "tasks.json")
        t = store.create("做某件事")
        loop = _make_loop(store, interval=0, seconds=0)
        loop._sync_task_pins()
        assert self._board(loop) is not None

        store.update(t.id, status="completed")
        loop._sync_task_pins()

        assert self._board(loop) is None

    def test_updates_board_on_description_change(self, tmp_path):
        store = TaskStore(tmp_path / "tasks.json")
        t = store.create("旧描述")
        loop = _make_loop(store, interval=0, seconds=0)
        loop._sync_task_pins()
        assert "旧描述" in self._board(loop).content

        store.update(t.id, description="新描述")
        loop._sync_task_pins()

        board = self._board(loop)
        assert board is not None
        assert "旧描述" not in board.content
        assert "新描述" in board.content

    def test_board_more_cap(self, tmp_path, monkeypatch):
        from coworker.agent import loop as loop_module

        monkeypatch.setattr(loop_module, "_TASK_BOARD_MAX_TASKS", 2)
        store = TaskStore(tmp_path / "tasks.json")
        for i in range(5):
            store.create(f"任务{i}")
        loop = _make_loop(store, interval=0, seconds=0)

        loop._sync_task_pins()

        board = self._board(loop)
        assert board is not None
        assert "任务0" in board.content
        assert "任务1" in board.content
        assert "任务2" not in board.content
        assert "task_list" in board.content  # 折叠提示指向 task_list
