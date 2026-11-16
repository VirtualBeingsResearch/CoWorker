from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest

from coworker.tools.alarm import AlarmCondition, AlarmManager, AlarmOptions


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


def _manager(inbox, tmp_path, path_name="alarms.json"):
    return AlarmManager(
        inbox,
        persist_path=tmp_path / path_name,
        options=AlarmOptions(allowed_roots=[tmp_path]),
    )


def _read(path):
    return json.loads(path.read_text(encoding="utf-8"))


def _write(path, payload):
    path.write_text(json.dumps(payload), encoding="utf-8")


def _state(alarms=None, conditions=None):
    return {"version": 2, "alarms": alarms or [], "conditions": conditions or []}


def _condition_record(target: str, **overrides: object) -> dict:
    record = {
        "alarm_id": "c1",
        "kind": "condition",
        "message": "watch",
        "condition": {
            "kind": "file_changed",
            "target": target,
            "pattern": "",
            "status": "",
            "stale_seconds": 0,
        },
        "every_seconds": 300,
        "last_state": "",
        "last_stat": "",
        "last_error": "",
        "failures": 0,
        "paused": False,
    }
    record.update(overrides)
    return record


def _time_record(alarm_id: str = "time1", message: str = "time") -> dict:
    future = (datetime.now() + timedelta(seconds=60)).strftime("%Y-%m-%d %H:%M:%S")
    return {
        "alarm_id": alarm_id,
        "next_trigger_at": future,
        "message": message,
        "repeat_seconds": None,
    }


async def test_time_and_condition_share_one_file(inbox, tmp_path):
    manager = _manager(inbox, tmp_path)
    await manager.set("t1", datetime.now() + timedelta(seconds=60), "time")
    condition = AlarmCondition(kind="file_changed", target=str(tmp_path / "a.txt"))
    await manager.set_condition("c1", "watch", condition, 300, start=False)

    assert not (tmp_path / "alarm_conditions.json").exists()
    data = _read(tmp_path / "alarms.json")
    assert data["version"] == 2
    assert [item["alarm_id"] for item in data["alarms"]] == ["t1"]
    assert [item["alarm_id"] for item in data["conditions"]] == ["c1"]
    assert data["conditions"][0]["condition"]["kind"] == "file_changed"
    assert data["conditions"][0]["every_seconds"] == 300

    manager.cancel("t1")


async def test_condition_is_restored(inbox, tmp_path):
    manager = _manager(inbox, tmp_path)
    condition = AlarmCondition(kind="file_changed", target=str(tmp_path / "a.txt"))
    await manager.set_condition("c1", "watch", condition, 300, start=False)

    restored = _manager(inbox, tmp_path)
    count = await restored.restore()

    assert count == 1
    restored_condition = restored.get_condition("c1")
    assert restored_condition is not None
    assert restored_condition.condition.kind == "file_changed"
    restored.cancel("c1")


async def test_paused_condition_is_restored_without_a_task(inbox, tmp_path):
    manager = _manager(inbox, tmp_path)
    condition = AlarmCondition(kind="file_changed", target=str(tmp_path / "a.txt"))
    await manager.set_condition("c1", "watch", condition, 300, start=False)

    path = tmp_path / "alarms.json"
    data = _read(path)
    data["conditions"][0]["paused"] = True
    _write(path, data)

    restored = _manager(inbox, tmp_path)
    await restored.restore()

    entry = restored.get_condition("c1")
    assert entry is not None
    assert entry.paused is True
    assert entry.task is None
    restored.cancel("c1")


async def test_legacy_flat_list_is_upgraded_and_backed_up(inbox, tmp_path):
    path = tmp_path / "alarms.json"
    legacy = [
        _time_record(),
        _condition_record(str(tmp_path / "a.txt"), alarm_id="cond1"),
    ]
    _write(path, legacy)

    manager = _manager(inbox, tmp_path)
    count = await manager.restore()

    assert count == 2
    data = _read(path)
    assert data["version"] == 2
    assert [item["alarm_id"] for item in data["alarms"]] == ["time1"]
    assert [item["alarm_id"] for item in data["conditions"]] == ["cond1"]
    backup = tmp_path / "alarms.json.v1.bak"
    assert backup.exists()
    assert json.loads(backup.read_text(encoding="utf-8")) == legacy
    manager.cancel("time1")
    manager.cancel("cond1")


async def test_malformed_legacy_record_does_not_block_restore(inbox, tmp_path):
    path = tmp_path / "alarms.json"
    _write(path, [{"alarm_id": "bad", "message": "no time"}, _time_record("good", "fine")])

    manager = _manager(inbox, tmp_path)
    count = await manager.restore()

    assert count == 1
    assert any(item["id"] == "good" for item in manager.list())
    assert [item["alarm_id"] for item in _read(path)["alarms"]] == ["good"]
    manager.cancel("good")


async def test_corrupt_file_does_not_block_startup(inbox, tmp_path):
    path = tmp_path / "alarms.json"
    path.write_text("{broken", encoding="utf-8")

    manager = _manager(inbox, tmp_path)
    count = await manager.restore()

    assert count == 0
    assert path.read_text(encoding="utf-8") == "{broken"


async def test_unknown_version_is_left_untouched(inbox, tmp_path):
    path = tmp_path / "alarms.json"
    future_state = {"version": 99, "alarms": [], "conditions": []}
    _write(path, future_state)

    manager = _manager(inbox, tmp_path)
    count = await manager.restore()
    assert count == 0

    await manager.set("t1", datetime.now() + timedelta(seconds=60), "time")
    manager.cancel("t1")

    assert _read(path) == future_state


async def test_condition_outside_allowed_roots_restores_paused(inbox, tmp_path):
    outside = str(tmp_path.parent / "outside.txt")
    _write(tmp_path / "alarms.json", _state(conditions=[_condition_record(outside)]))

    manager = _manager(inbox, tmp_path)
    count = await manager.restore()

    assert count == 1
    entry = manager.get_condition("c1")
    assert entry is not None
    assert entry.paused is True
    assert entry.task is None
    assert entry.last_error
    manager.cancel("c1")


async def test_missing_file_state_survives_restore(inbox, tmp_path):
    target = tmp_path / "later.txt"
    _write(
        tmp_path / "alarms.json",
        _state(
            conditions=[
                _condition_record(str(target), last_state="missing", paused=True)
            ]
        ),
    )

    manager = _manager(inbox, tmp_path)
    await manager.restore()

    entry = manager.get_condition("c1")
    assert entry is not None
    assert entry.last_state == "missing"

    target.write_text("now", encoding="utf-8")
    assert await manager.check_once("c1") is True
    manager.cancel("c1")
