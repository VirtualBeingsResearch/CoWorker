from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest

from coworker.tools.alarm_tools import AlarmCondition, AlarmManager


@pytest.fixture
def inbox():
    mock = MagicMock()
    mock.push = AsyncMock()
    return mock


@pytest.fixture(autouse=True)
async def cleanup_tasks():
    yield
    current = asyncio.current_task()
    tasks = [task for task in asyncio.all_tasks() if task is not current and not task.done()]
    for task in tasks:
        task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)


async def test_condition_is_saved_with_kind(inbox, tmp_path):
    path = tmp_path / "alarms.json"
    manager = AlarmManager(inbox, persist_path=path, allowed_roots=[tmp_path])
    condition = AlarmCondition(kind="file_changed", target=str(tmp_path / "a.txt"))
    await manager.set_condition("c1", "watch", condition, 300, start=False)

    records = json.loads(path.read_text(encoding="utf-8"))
    assert len(records) == 1
    assert records[0]["alarm_id"] == "c1"
    assert records[0]["kind"] == "condition"
    assert records[0]["condition"]["kind"] == "file_changed"
    assert records[0]["every_seconds"] == 300


async def test_condition_is_restored(inbox, tmp_path):
    path = tmp_path / "alarms.json"
    manager = AlarmManager(inbox, persist_path=path, allowed_roots=[tmp_path])
    condition = AlarmCondition(kind="file_changed", target=str(tmp_path / "a.txt"))
    await manager.set_condition("c1", "watch", condition, 300, start=False)

    restored = AlarmManager(inbox, persist_path=path, allowed_roots=[tmp_path])
    count = await restored.restore()

    assert count == 1
    restored_condition = restored.get_condition("c1")
    assert restored_condition is not None
    assert restored_condition.condition.kind == "file_changed"
    restored.cancel("c1")


async def test_paused_condition_is_restored_without_a_task(inbox, tmp_path):
    path = tmp_path / "alarms.json"
    condition = AlarmCondition(kind="file_changed", target=str(tmp_path / "a.txt"))
    manager = AlarmManager(inbox, persist_path=path, allowed_roots=[tmp_path])
    await manager.set_condition("c1", "watch", condition, 300, start=False)

    records = json.loads(path.read_text(encoding="utf-8"))
    records[0]["paused"] = True
    path.write_text(json.dumps(records), encoding="utf-8")

    restored = AlarmManager(inbox, persist_path=path, allowed_roots=[tmp_path])
    await restored.restore()

    entry = restored.get_condition("c1")
    assert entry is not None
    assert entry.paused is True
    assert entry.task is None
    restored.cancel("c1")


async def test_mixed_old_time_alarm_and_condition_restore(inbox, tmp_path):
    path = tmp_path / "alarms.json"
    future = (datetime.now() + timedelta(seconds=60)).strftime("%Y-%m-%d %H:%M:%S")
    path.write_text(
        json.dumps(
            [
                {
                    "alarm_id": "time1",
                    "next_trigger_at": future,
                    "message": "time",
                    "repeat_seconds": None,
                },
                {
                    "alarm_id": "cond1",
                    "kind": "condition",
                    "message": "condition",
                    "condition": {
                        "kind": "file_changed",
                        "target": str(tmp_path / "a.txt"),
                        "pattern": "",
                        "status": "",
                        "stale_seconds": 0,
                    },
                    "every_seconds": 300,
                    "last_state": "",
                    "failures": 0,
                    "paused": False,
                },
            ]
        ),
        encoding="utf-8",
    )

    manager = AlarmManager(inbox, persist_path=path, allowed_roots=[tmp_path])
    count = await manager.restore()

    assert count == 2
    assert any(item["id"] == "time1" and item["kind"] == "time" for item in manager.list())
    restored_condition = manager.get_condition("cond1")
    assert restored_condition is not None
    manager.cancel("time1")
    manager.cancel("cond1")
