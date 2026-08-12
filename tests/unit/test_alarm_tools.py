from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest

from coworker.tools.alarm_tools import AlarmManager, CancelAlarmTool, ListAlarmsTool, SetAlarmTool


@pytest.fixture
def mock_inbox():
    inbox = MagicMock()
    inbox.push = AsyncMock()
    return inbox


@pytest.fixture
def manager(mock_inbox):
    return AlarmManager(mock_inbox)


@pytest.fixture
def persisted_manager(mock_inbox, tmp_path):
    return AlarmManager(mock_inbox, persist_path=tmp_path / "alarms.json")


@pytest.fixture(autouse=True)
async def cleanup_tasks():
    yield
    current = asyncio.current_task()
    tasks = [t for t in asyncio.all_tasks() if t is not current and not t.done()]
    for task in tasks:
        task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)


class TestAlarmManager:
    async def test_set_and_fire_oneshot(self, manager, mock_inbox):
        trigger_at = datetime.now() + timedelta(milliseconds=50)
        await manager.set("a1", trigger_at, "hello", repeat_seconds=None)
        assert "a1" in manager._alarms

        await asyncio.sleep(0.15)

        mock_inbox.push.assert_awaited_once()
        event = mock_inbox.push.call_args[0][0]
        assert "a1" in event.content
        assert "hello" in event.content
        assert event.source == "alarm"
        assert "a1" not in manager._alarms

    async def test_set_and_fire_recurring(self, manager, mock_inbox):
        trigger_at = datetime.now() + timedelta(milliseconds=50)
        await manager.set("r1", trigger_at, "ping", repeat_seconds=1)

        await asyncio.sleep(0.15)
        assert mock_inbox.push.await_count == 1
        assert "r1" in manager._alarms

        manager.cancel("r1")

    async def test_cancel_pending_alarm(self, manager, mock_inbox):
        trigger_at = datetime.now() + timedelta(seconds=10)
        await manager.set("c1", trigger_at, "won't fire", repeat_seconds=None)

        result = manager.cancel("c1")

        assert result is True
        assert "c1" not in manager._alarms
        await asyncio.sleep(0.05)
        mock_inbox.push.assert_not_awaited()

    async def test_cancel_nonexistent_returns_false(self, manager):
        assert manager.cancel("no-such-id") is False

    async def test_overwrite_existing_id(self, manager, mock_inbox):
        trigger_far = datetime.now() + timedelta(seconds=60)
        await manager.set("dup", trigger_far, "first", repeat_seconds=None)

        trigger_soon = datetime.now() + timedelta(milliseconds=50)
        await manager.set("dup", trigger_soon, "second", repeat_seconds=None)

        await asyncio.sleep(0.15)
        mock_inbox.push.assert_awaited_once()
        event = mock_inbox.push.call_args[0][0]
        assert "second" in event.content


class TestPersistence:
    async def test_save_on_set(self, persisted_manager, tmp_path):
        future = datetime.now() + timedelta(seconds=60)
        await persisted_manager.set("p1", future, "saved?", repeat_seconds=None)

        persist_file = tmp_path / "alarms.json"
        assert persist_file.exists()
        records = json.loads(persist_file.read_text(encoding="utf-8"))
        assert len(records) == 1
        assert records[0]["alarm_id"] == "p1"
        assert records[0]["message"] == "saved?"
        assert records[0]["repeat_seconds"] is None

        persisted_manager.cancel("p1")

    async def test_save_on_cancel(self, persisted_manager, tmp_path):
        future = datetime.now() + timedelta(seconds=60)
        await persisted_manager.set("p2", future, "bye", repeat_seconds=None)
        persisted_manager.cancel("p2")

        persist_file = tmp_path / "alarms.json"
        records = json.loads(persist_file.read_text(encoding="utf-8"))
        assert records == []

    async def test_restore_future_alarm(self, mock_inbox, tmp_path):
        future = datetime.now() + timedelta(seconds=60)
        persist_file = tmp_path / "alarms.json"
        persist_file.write_text(
            json.dumps([{
                "alarm_id": "r1",
                "next_trigger_at": future.strftime("%Y-%m-%d %H:%M:%S"),
                "message": "restored",
                "repeat_seconds": None,
            }]),
            encoding="utf-8",
        )

        new_manager = AlarmManager(mock_inbox, persist_path=persist_file)
        count = await new_manager.restore()

        assert count == 1
        assert "r1" in new_manager._alarms
        mock_inbox.push.assert_not_awaited()
        new_manager.cancel("r1")

    async def test_restore_missed_oneshot_fires_immediately(self, mock_inbox, tmp_path):
        past = datetime.now() - timedelta(seconds=30)
        persist_file = tmp_path / "alarms.json"
        persist_file.write_text(
            json.dumps([{
                "alarm_id": "missed",
                "next_trigger_at": past.strftime("%Y-%m-%d %H:%M:%S"),
                "message": "overdue task",
                "repeat_seconds": None,
            }]),
            encoding="utf-8",
        )

        new_manager = AlarmManager(mock_inbox, persist_path=persist_file)
        await new_manager.restore()
        await asyncio.sleep(0.05)

        mock_inbox.push.assert_awaited_once()
        event = mock_inbox.push.call_args[0][0]
        assert "overdue task" in event.content
        assert "迟到" in event.content

    async def test_restore_missed_recurring_fires_once_then_continues(self, mock_inbox, tmp_path):
        past = datetime.now() - timedelta(seconds=30)
        persist_file = tmp_path / "alarms.json"
        persist_file.write_text(
            json.dumps([{
                "alarm_id": "rec",
                "next_trigger_at": past.strftime("%Y-%m-%d %H:%M:%S"),
                "message": "recurring",
                "repeat_seconds": 60,
            }]),
            encoding="utf-8",
        )

        new_manager = AlarmManager(mock_inbox, persist_path=persist_file)
        await new_manager.restore()
        await asyncio.sleep(0.05)

        # Fires once immediately
        assert mock_inbox.push.await_count == 1
        event = mock_inbox.push.call_args[0][0]
        assert "迟到" in event.content
        # Rescheduled for next interval
        assert "rec" in new_manager._alarms
        new_manager.cancel("rec")

    async def test_restore_multiple_alarms_preserves_each_entry(self, mock_inbox, tmp_path):
        past = datetime.now() - timedelta(seconds=30)
        persist_file = tmp_path / "alarms.json"
        persist_file.write_text(
            json.dumps([
                {
                    "alarm_id": "first",
                    "next_trigger_at": past.strftime("%Y-%m-%d %H:%M:%S"),
                    "message": "first message",
                    "repeat_seconds": None,
                },
                {
                    "alarm_id": "second",
                    "next_trigger_at": past.strftime("%Y-%m-%d %H:%M:%S"),
                    "message": "second message",
                    "repeat_seconds": None,
                },
            ]),
            encoding="utf-8",
        )

        new_manager = AlarmManager(mock_inbox, persist_path=persist_file)
        await new_manager.restore()
        await asyncio.sleep(0.05)

        assert mock_inbox.push.await_count == 2
        contents = [call.args[0].content for call in mock_inbox.push.await_args_list]
        assert sum("first message" in content for content in contents) == 1
        assert sum("second message" in content for content in contents) == 1
        assert new_manager.list() == []
        assert json.loads(persist_file.read_text(encoding="utf-8")) == []

    async def test_restore_is_idempotent_for_pending_alarm(self, mock_inbox, tmp_path):
        future = datetime.now() + timedelta(milliseconds=100)
        persist_file = tmp_path / "alarms.json"
        persist_file.write_text(
            json.dumps([{
                "alarm_id": "once",
                "next_trigger_at": future.isoformat(),
                "message": "only once",
                "repeat_seconds": None,
            }]),
            encoding="utf-8",
        )

        new_manager = AlarmManager(mock_inbox, persist_path=persist_file)
        await new_manager.restore()
        await new_manager.restore()
        await asyncio.sleep(0.2)

        mock_inbox.push.assert_awaited_once()

    async def test_no_persist_path_no_file(self, manager, tmp_path):
        future = datetime.now() + timedelta(seconds=60)
        await manager.set("np", future, "no persist")
        assert not (tmp_path / "alarms.json").exists()
        manager.cancel("np")

    async def test_restore_missing_file_returns_zero(self, mock_inbox, tmp_path):
        new_manager = AlarmManager(mock_inbox, persist_path=tmp_path / "nonexistent.json")
        count = await new_manager.restore()
        assert count == 0

    async def test_save_updates_next_trigger_after_recurring_fire(self, mock_inbox, tmp_path):
        persist_file = tmp_path / "alarms.json"
        manager = AlarmManager(mock_inbox, persist_path=persist_file)
        trigger_soon = datetime.now() + timedelta(milliseconds=50)
        await manager.set("rec2", trigger_soon, "tick", repeat_seconds=600)

        await asyncio.sleep(0.15)

        records = json.loads(persist_file.read_text(encoding="utf-8"))
        assert len(records) == 1
        # next_trigger_at should have been updated to ~now+600s
        next_t = datetime.fromisoformat(records[0]["next_trigger_at"])
        assert next_t > datetime.now().astimezone()
        manager.cancel("rec2")


class TestSetAlarmTool:
    async def test_returns_confirmation(self, manager):
        tool = SetAlarmTool(manager)
        future = (datetime.now() + timedelta(seconds=60)).strftime("%Y-%m-%d %H:%M:%S")
        result = await tool.execute(trigger_at=future, message="做事", alarm_id="t1")
        assert not result.is_error
        assert "t1" in result.content
        manager.cancel("t1")

    async def test_auto_generate_alarm_id(self, manager):
        tool = SetAlarmTool(manager)
        future = (datetime.now() + timedelta(seconds=60)).strftime("%Y-%m-%d %H:%M:%S")
        result = await tool.execute(trigger_at=future, message="auto")
        assert not result.is_error
        assert len(manager._alarms) == 1
        auto_id = list(manager._alarms.keys())[0]
        assert auto_id in result.content
        manager.cancel(auto_id)

    async def test_invalid_datetime_format(self, manager):
        tool = SetAlarmTool(manager)
        result = await tool.execute(trigger_at="not-a-date", message="oops")
        assert result.is_error

    async def test_past_datetime_rejected(self, manager):
        tool = SetAlarmTool(manager)
        past = (datetime.now() - timedelta(seconds=1)).strftime("%Y-%m-%d %H:%M:%S")
        result = await tool.execute(trigger_at=past, message="too late")
        assert result.is_error

    async def test_timezone_aware_datetime_is_supported(self, manager):
        tool = SetAlarmTool(manager)
        future = datetime.now(UTC) + timedelta(seconds=60)

        result = await tool.execute(
            trigger_at=future.isoformat().replace("+00:00", "Z"),
            message="aware",
        )

        assert not result.is_error
        alarm = manager.list()[0]
        assert datetime.fromisoformat(alarm["trigger_at"]).tzinfo is not None
        manager.cancel(alarm["id"])

    async def test_recurring_label_in_result(self, manager):
        tool = SetAlarmTool(manager)
        future = (datetime.now() + timedelta(seconds=60)).strftime("%Y-%m-%d %H:%M:%S")
        result = await tool.execute(trigger_at=future, message="loop", repeat_seconds=300)
        assert not result.is_error
        assert "循环" in result.content
        alarm_id = list(manager._alarms.keys())[0]
        manager.cancel(alarm_id)

    async def test_oneshot_label_in_result(self, manager):
        tool = SetAlarmTool(manager)
        future = (datetime.now() + timedelta(seconds=60)).strftime("%Y-%m-%d %H:%M:%S")
        result = await tool.execute(trigger_at=future, message="once")
        assert not result.is_error
        assert "一次性" in result.content
        alarm_id = list(manager._alarms.keys())[0]
        manager.cancel(alarm_id)


class TestListAlarmsTool:
    async def test_list_empty(self, manager):
        tool = ListAlarmsTool(manager)
        result = await tool.execute()
        assert not result.is_error
        assert "没有" in result.content

    async def test_list_shows_oneshot_and_recurring(self, manager):
        future = datetime.now() + timedelta(seconds=60)
        await manager.set("once1", future, "单次任务", repeat_seconds=None)
        await manager.set("loop1", future, "循环任务", repeat_seconds=600)

        tool = ListAlarmsTool(manager)
        result = await tool.execute()
        assert not result.is_error
        assert "once1" in result.content
        assert "loop1" in result.content
        assert "一次性" in result.content
        assert "600 秒重复" in result.content

        manager.cancel("once1")
        manager.cancel("loop1")


class TestCancelAlarmTool:
    async def test_cancel_success(self, manager):
        future = datetime.now() + timedelta(seconds=60)
        await manager.set("del1", future, "bye", repeat_seconds=None)

        tool = CancelAlarmTool(manager)
        result = await tool.execute(alarm_id="del1")
        assert not result.is_error
        assert "del1" in result.content

    async def test_cancel_missing_id(self, manager):
        tool = CancelAlarmTool(manager)
        result = await tool.execute(alarm_id="ghost")
        assert result.is_error
