from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from coworker.agent.inbox_watcher import InboxWatcher
from coworker.agent.loop import AgentLoop
from coworker.core.types import AgentState, IncomingEvent


def _make_loop(paused: bool = True) -> AgentLoop:
    loop = AgentLoop.__new__(AgentLoop)
    loop._config = MagicMock()
    loop._config.agent.passive_mode = False
    loop._config.agent.paused = paused
    loop._inbox = InboxWatcher(str(Path("data/inbox")))
    loop._stop_event = asyncio.Event()
    loop._resume_event = asyncio.Event()
    loop._snapshot_path = None
    loop._task_store = None
    loop._bubble_store = None
    loop._short_term = MagicMock()
    loop.state = AgentState()
    return loop


async def _queue_event(inbox: InboxWatcher, content: str) -> None:
    await inbox.push(IncomingEvent(participant_id="tester", content=content))


async def _wait_until(predicate, timeout: float = 2.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("timed out waiting for condition")
        await asyncio.sleep(0.01)


@pytest.mark.asyncio
async def test_paused_cycle_parks_and_keeps_inbox_queued():
    loop = _make_loop(paused=True)
    await _queue_event(loop._inbox, "hello")
    await _queue_event(loop._inbox, "world")

    cycle_task = asyncio.create_task(loop._cycle())
    await _wait_until(lambda: loop.state.is_sleeping)

    # 停靠期间队列保持原样：消息既不消费也不丢失。
    assert loop._inbox._queue.qsize() == 2

    loop._config.agent.paused = False
    loop.state.is_running = True
    assert loop.resume_from_rest() is True
    await asyncio.wait_for(cycle_task, timeout=2)

    assert loop.state.is_sleeping is False
    assert loop._inbox._queue.qsize() == 2


@pytest.mark.asyncio
async def test_pause_rest_clears_stale_resume_signal():
    loop = _make_loop(paused=True)
    loop._resume_event.set()

    cycle_task = asyncio.create_task(loop._cycle())
    for _ in range(10):
        await asyncio.sleep(0)
    # 残留的恢复信号不会让循环带着 paused 状态空转：仍应停靠等待。
    assert cycle_task.done() is False
    assert loop.state.is_sleeping is True

    loop.state.is_running = True
    loop.resume_from_rest()
    await asyncio.wait_for(cycle_task, timeout=2)


@pytest.mark.asyncio
async def test_resume_from_rest_signals_both_wake_events():
    loop = _make_loop(paused=False)
    loop.state.is_running = True
    loop.state.is_sleeping = True

    assert loop.resume_from_rest() is True
    assert loop._inbox.message_event.is_set()
    assert loop._resume_event.is_set()


@pytest.mark.asyncio
async def test_resume_from_rest_returns_false_when_active():
    loop = _make_loop(paused=False)
    loop.state.is_running = True
    loop.state.is_sleeping = False

    assert loop.resume_from_rest() is False
    assert not loop._inbox.message_event.is_set()
    assert not loop._resume_event.is_set()


@pytest.mark.asyncio
async def test_interrupt_rest_only_sets_message_event():
    loop = _make_loop(paused=False)

    loop.interrupt_rest()

    assert loop._inbox.message_event.is_set()
    assert not loop._resume_event.is_set()


@pytest.mark.asyncio
async def test_run_parks_when_started_paused_and_stop_wakes():
    loop = _make_loop(paused=True)
    await _queue_event(loop._inbox, "queued while paused")

    run_task = asyncio.create_task(loop.run())
    await _wait_until(lambda: loop.state.is_sleeping)
    assert loop.state.is_running is True
    assert loop._inbox._queue.qsize() == 1

    # 暂停停靠中的循环必须能被 stop() 打断，保证暂停期间可正常关闭/重启。
    loop.stop()
    await asyncio.wait_for(run_task, timeout=2)

    assert loop.state.is_running is False


@pytest.mark.asyncio
async def test_run_resumes_into_cycles_after_pause_lifted():
    loop = _make_loop(paused=True)

    async def _end_run() -> None:
        loop._stop_event.set()

    loop._cycle = AsyncMock(side_effect=_end_run)

    run_task = asyncio.create_task(loop.run())
    await _wait_until(lambda: loop.state.is_sleeping)

    loop._config.agent.paused = False
    assert loop.resume_from_rest() is True
    await asyncio.wait_for(run_task, timeout=2)

    loop._cycle.assert_awaited_once()
    assert loop.state.is_running is False
