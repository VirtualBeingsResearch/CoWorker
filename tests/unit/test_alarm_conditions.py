from __future__ import annotations

import asyncio
import os
import time
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from coworker.tools.alarm_conditions import validate_public_url
from coworker.tools.alarm_tools import (
    AlarmCondition,
    AlarmManager,
    AlarmOptions,
    ListAlarmsTool,
    SetAlarmTool,
)
from coworker.tools.reasoning_tools import TaskStore


@pytest.fixture
def inbox():
    mock = MagicMock()
    mock.push = AsyncMock()
    return mock


@pytest.fixture(autouse=True)
async def cleanup_tasks():
    yield
    current = asyncio.current_task()
    tasks = [
        task
        for task in asyncio.all_tasks()
        if task is not current and not task.done()
    ]
    for task in tasks:
        task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)


@pytest.fixture
def manager(inbox, tmp_path):
    return AlarmManager(
        inbox,
        persist_path=tmp_path / "alarms.json",
        options=AlarmOptions(
            allowed_roots=[tmp_path],
            min_interval_seconds=1,
            http_max_bytes=1024,
            check_timeout_seconds=1.0,
        ),
    )


async def test_file_condition_baseline_then_change(manager, inbox, tmp_path):
    target = tmp_path / "watched.txt"
    target.write_text("one", encoding="utf-8")
    manager.add_condition(
        "f1", "file changed", AlarmCondition(kind="file_changed", target=str(target)), 1
    )

    assert await manager.check_once("f1") is False
    inbox.push.assert_not_awaited()

    target.write_text("two", encoding="utf-8")

    assert await manager.check_once("f1") is True
    inbox.push.assert_awaited_once()
    event = inbox.push.call_args[0][0]
    assert event.source == "alarm"
    assert "file changed" in event.content
    assert "file_changed" in event.content


async def test_file_touch_without_content_change_does_not_fire(manager, inbox, tmp_path):
    target = tmp_path / "touched.txt"
    target.write_text("same", encoding="utf-8")
    manager.add_condition(
        "touch1",
        "content changed",
        AlarmCondition(kind="file_changed", target=str(target)),
        1,
    )

    assert await manager.check_once("touch1") is False
    os.utime(target, (time.time() + 10, time.time() + 10))
    assert await manager.check_once("touch1") is False
    inbox.push.assert_not_awaited()


async def test_unchanged_check_does_not_rewrite_state(manager, mocker, tmp_path):
    target = tmp_path / "stable.txt"
    target.write_text("stable", encoding="utf-8")
    manager.add_condition(
        "stable1",
        "stable",
        AlarmCondition(kind="file_changed", target=str(target)),
        1,
    )

    assert await manager.check_once("stable1") is False
    spy = mocker.spy(manager, "_save_conditions")
    spy.reset_mock()

    assert await manager.check_once("stable1") is False
    assert spy.call_count == 0


async def test_condition_loop_fires_after_change(manager, inbox, tmp_path):
    target = tmp_path / "looped.txt"
    target.write_text("one", encoding="utf-8")
    await manager.set_condition(
        "loop1",
        "loop changed",
        AlarmCondition(kind="file_changed", target=str(target)),
        1,
    )

    await asyncio.sleep(0.1)
    target.write_text("two", encoding="utf-8")
    await asyncio.sleep(1.5)

    assert inbox.push.await_count == 1
    manager.cancel("loop1")


async def test_file_condition_outside_allowed_roots(manager):
    manager.add_condition(
        "f2", "outside", AlarmCondition(kind="file_changed", target="/etc/passwd"), 1
    )
    with pytest.raises(ValueError):
        await manager.check_once("f2")


async def test_http_condition_baseline_then_change(inbox, tmp_path):
    body = {"text": "version one"}

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=body["text"])

    manager = AlarmManager(
        inbox,
        persist_path=tmp_path / "alarms.json",
        options=AlarmOptions(
            http_transport=httpx.MockTransport(handler),
            min_interval_seconds=1,
            http_max_bytes=1024,
        ),
    )
    manager.add_condition(
        "h1",
        "page changed",
        AlarmCondition(kind="http_changed", target="https://93.184.216.34/status"),
        1,
    )

    assert await manager.check_once("h1") is False
    body["text"] = "version two"

    assert await manager.check_once("h1") is True
    event = inbox.push.call_args[0][0]
    assert "http_changed" in event.content


async def test_http_condition_regex_selects_content(inbox, tmp_path):
    body = {"text": "<main>one</main><footer>noise</footer>"}

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=body["text"])

    manager = AlarmManager(
        inbox,
        options=AlarmOptions(
            http_transport=httpx.MockTransport(handler),
            min_interval_seconds=1,
            http_max_bytes=1024,
        ),
    )
    manager.add_condition(
        "h2",
        "main changed",
        AlarmCondition(
            kind="http_changed",
            target="https://93.184.216.34/page",
            pattern=r"<main>(.*?)</main>",
        ),
        1,
    )

    assert await manager.check_once("h2") is False
    body["text"] = "<main>one</main><footer>changed but ignored</footer>"
    assert await manager.check_once("h2") is False

    body["text"] = "<main>two</main><footer>changed but ignored</footer>"
    assert await manager.check_once("h2") is True


async def test_http_condition_rejects_large_response(inbox):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"x" * 2048)

    manager = AlarmManager(
        inbox,
        options=AlarmOptions(
            http_transport=httpx.MockTransport(handler),
            http_max_bytes=1024,
        ),
    )
    manager.add_condition(
        "h3",
        "too large",
        AlarmCondition(kind="http_changed", target="https://93.184.216.34/big"),
        1,
    )

    with pytest.raises(ValueError):
        await manager.check_once("h3")


async def test_http_condition_rejects_redirect_loop(inbox):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"location": str(request.url)})

    manager = AlarmManager(
        inbox,
        options=AlarmOptions(
            http_transport=httpx.MockTransport(handler),
            http_max_bytes=1024,
        ),
    )
    manager.add_condition(
        "h4",
        "loop",
        AlarmCondition(kind="http_changed", target="https://93.184.216.34/loop"),
        1,
    )

    with pytest.raises(ValueError):
        await manager.check_once("h4")


async def test_task_condition_status(inbox, tmp_path):
    store = TaskStore(tmp_path / "tasks.json")
    task = store.create("finish the report")
    manager = AlarmManager(
        inbox,
        persist_path=tmp_path / "alarms.json",
        options=AlarmOptions(task_store=store),
    )
    manager.add_condition(
        "t1",
        "task completed",
        AlarmCondition(kind="task_state", target=task.id, status="completed"),
        1,
    )

    assert await manager.check_once("t1") is False
    store.update(task.id, status="completed")

    assert await manager.check_once("t1") is True
    event = inbox.push.call_args[0][0]
    assert "task completed" in event.content


async def test_task_condition_stale(inbox, tmp_path):
    store = TaskStore(tmp_path / "tasks.json")
    task = store.create("stuck task")
    manager = AlarmManager(
        inbox,
        persist_path=tmp_path / "alarms.json",
        options=AlarmOptions(task_store=store),
    )
    manager.add_condition(
        "t2",
        "task is stale",
        AlarmCondition(kind="task_state", target=task.id, stale_seconds=60),
        1,
    )

    assert await manager.check_once("t2") is False
    task.updated_at = (datetime.now() - timedelta(seconds=120)).isoformat()
    assert await manager.check_once("t2") is True
    event = inbox.push.call_args[0][0]
    assert "task is stale" in event.content


async def test_condition_alarm_can_be_listed_and_cancelled(manager):
    manager.add_condition(
        "c1",
        "watch it",
        AlarmCondition(kind="file_changed", target=str(__file__)),
        300,
    )

    listed = manager.list()
    assert listed[0]["kind"] == "condition"
    assert listed[0]["target"] == str(__file__)

    entry = manager.get_condition("c1")
    assert entry is not None
    entry.paused = True
    entry.last_error = "boom"

    result = await ListAlarmsTool(manager).execute()
    assert not result.is_error
    assert "condition" in result.content or "条件" in result.content
    assert "paused" in result.content or "暂停" in result.content

    assert manager.cancel("c1") is True
    assert manager.list() == []


async def test_condition_limit(inbox, tmp_path):
    manager = AlarmManager(
        inbox,
        options=AlarmOptions(allowed_roots=[tmp_path], max_conditions=1),
    )
    condition = AlarmCondition(kind="file_changed", target=str(tmp_path / "a.txt"))
    manager.add_condition("a", "first", condition, 60)
    with pytest.raises(ValueError):
        manager.add_condition("b", "second", condition, 60)


class TestSetAlarmValidation:
    async def test_requires_exactly_one_mode(self, manager):
        tool = SetAlarmTool(manager)
        result = await tool.execute(message="x")
        assert result.is_error

        future = (datetime.now() + timedelta(seconds=60)).strftime("%Y-%m-%d %H:%M:%S")
        result = await tool.execute(
            message="x",
            trigger_at=future,
            condition="file_changed",
            target=__file__,
        )
        assert result.is_error

    async def test_rejects_unknown_condition(self, manager):
        tool = SetAlarmTool(manager)
        result = await tool.execute(message="x", condition="magic", target=__file__)
        assert result.is_error

    async def test_rejects_fields_for_wrong_mode(self, manager):
        tool = SetAlarmTool(manager)
        future = (datetime.now() + timedelta(seconds=60)).strftime("%Y-%m-%d %H:%M:%S")
        result = await tool.execute(message="x", trigger_at=future, target=__file__)
        assert result.is_error

        result = await tool.execute(
            message="x",
            condition="file_changed",
            target=__file__,
            every_seconds=30,
        )
        assert result.is_error

    async def test_task_state_requires_status_or_stale(self, manager):
        tool = SetAlarmTool(manager)
        result = await tool.execute(message="x", condition="task_state")
        assert result.is_error

    async def test_rejects_file_outside_allowed_roots(self, manager, tmp_path):
        tool = SetAlarmTool(manager)
        result = await tool.execute(
            message="x",
            condition="file_changed",
            target=str(tmp_path.parent / "outside.txt"),
            every_seconds=60,
        )
        assert result.is_error

    async def test_rejects_private_http_target(self, manager):
        tool = SetAlarmTool(manager)
        result = await tool.execute(
            message="x",
            condition="http_changed",
            target="http://127.0.0.1/",
            every_seconds=60,
        )
        assert result.is_error

    async def test_sets_condition_alarm(self, manager, tmp_path):
        tool = SetAlarmTool(manager)
        target = tmp_path / "watched.txt"
        target.write_text("initial", encoding="utf-8")
        result = await tool.execute(
            message="watch",
            condition="file_changed",
            target=str(target),
            every_seconds=60,
            alarm_id="cond1",
        )
        assert not result.is_error
        assert "cond1" in result.content
        assert manager.has_condition("cond1")
        manager.cancel("cond1")


def test_validate_public_url_rejects_private_and_credentials():
    with pytest.raises(ValueError):
        validate_public_url("http://127.0.0.1/")
    with pytest.raises(ValueError):
        validate_public_url("http://localhost/")
    with pytest.raises(ValueError):
        validate_public_url("ftp://example.com/")
    with pytest.raises(ValueError):
        validate_public_url("https://user:pass@93.184.216.34/")
