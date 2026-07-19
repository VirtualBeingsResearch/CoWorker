from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

import coworker.agent.loop as loop_module
from coworker.agent.loop import AgentLoop
from coworker.core.types import AgentState, Message
from coworker.memory.short_term import ShortTermMemory


def _make_recovery_loop(memory: ShortTermMemory, snapshot_path) -> AgentLoop:
    loop = AgentLoop.__new__(AgentLoop)
    loop._short_term = memory
    loop._snapshot_path = snapshot_path
    loop._consecutive_errors = 0
    loop._stop_event = asyncio.Event()
    loop._task_store = None
    loop._bubble_store = None
    loop.state = AgentState()
    loop._cycle = AsyncMock(side_effect=RuntimeError("provider unavailable"))
    return loop


def _stop_after_recovery_cooldown(monkeypatch, loop: AgentLoop) -> None:
    async def fast_sleep(seconds: float) -> None:
        if seconds == loop_module._RECOVERY_COOLDOWN_SECONDS:
            loop._stop_event.set()

    monkeypatch.setattr(loop_module.asyncio, "sleep", fast_sleep)


@pytest.mark.asyncio
async def test_emergency_recovery_backs_up_then_clears_context(tmp_path, monkeypatch):
    """A recovery saves the full context before starting over with a recovery notice."""
    memory = ShortTermMemory()
    memory.primary.extend(
        Message(role="user", content=f"message-{index}")
        for index in range(1, 7)
    )

    loop = _make_recovery_loop(memory, tmp_path / "short_term.json")
    _stop_after_recovery_cooldown(monkeypatch, loop)

    await loop.run()

    backups = list(tmp_path.glob("emergency_backup_*.json"))
    assert len(backups) == 1

    backup = ShortTermMemory.load_from_file(backups[0])
    assert [message.content for message in backup.primary] == [
        "message-1",
        "message-2",
        "message-3",
        "message-4",
        "message-5",
        "message-6",
        "[系统错误] RuntimeError: provider unavailable",
    ]
    assert len(loop._short_term.primary) == 1
    assert loop._short_term.primary[-1].source == "system_recovery"
    recovery_notice = str(loop._short_term.primary[-1].content)
    assert "上一轮因连续 5 次执行错误而中断" in recovery_notice
    assert "应急备份已创建：" in recovery_notice
    assert str(backups[0]) in recovery_notice
    assert "当前上下文已经清空重置" in recovery_notice


@pytest.mark.asyncio
async def test_emergency_recovery_reports_backup_failure(tmp_path, monkeypatch):
    memory = ShortTermMemory()
    memory.primary.append(Message(role="user", content="message-1"))
    memory.save_to_file = MagicMock(side_effect=OSError("disk full"))
    loop = _make_recovery_loop(memory, tmp_path / "short_term.json")
    _stop_after_recovery_cooldown(monkeypatch, loop)

    await loop.run()

    assert len(loop._short_term.primary) == 1
    recovery_notice = str(loop._short_term.primary[-1].content)
    assert "应急备份创建失败（目标：" in recovery_notice
    assert "错误：OSError: disk full" in recovery_notice
    assert "当前上下文已经清空重置" in recovery_notice
