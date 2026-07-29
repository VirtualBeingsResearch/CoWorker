from __future__ import annotations

import asyncio

import pytest

from coworker.agent.inbox_watcher import InboxWatcher
from coworker.core.autonomy import (
    AutonomyBlockedError,
    AutonomyController,
    AutonomyLevel,
    AutonomyScope,
    AutonomyThresholds,
    replace_autonomy_trigger,
)
from coworker.core.config import AgentConfig
from coworker.core.types import IncomingEvent


def test_removed_passive_environment_variable_is_ignored(monkeypatch):
    monkeypatch.setenv("AGENT__PASSIVE_MODE", "true")

    config = AgentConfig()

    assert config.autonomy_level is AutonomyLevel.AUTONOMOUS
    assert not hasattr(config, "passive_mode")


def test_autonomy_environment_variables_parse_level_and_thresholds(monkeypatch):
    monkeypatch.setenv("AGENT__AUTONOMY_LEVEL", "reactive")
    monkeypatch.setenv(
        "AGENT__AUTONOMY_THRESHOLDS",
        '{"main":"reactive","bubble":"event_driven","subconscious":"autonomous",'
        '"summary":"reactive","vision":"event_driven","mem0":"autonomous"}',
    )

    config = AgentConfig()

    assert config.autonomy_level is AutonomyLevel.REACTIVE
    assert config.autonomy_thresholds.bubble is AutonomyLevel.EVENT_DRIVEN
    assert config.autonomy_thresholds.mem0 is AutonomyLevel.AUTONOMOUS


def test_scope_threshold_is_combined_with_trigger_level():
    controller = AutonomyController(
        AutonomyLevel.EVENT_DRIVEN,
        AutonomyThresholds(summary=AutonomyLevel.AUTONOMOUS),
    )

    assert controller.allows(
        AutonomyScope.MAIN,
        trigger=AutonomyLevel.EVENT_DRIVEN,
    )
    assert not controller.allows(
        AutonomyScope.SUMMARY,
        trigger=AutonomyLevel.REACTIVE,
    )


def test_silent_is_global_even_with_a_silent_scope_threshold():
    controller = AutonomyController(
        AutonomyLevel.SILENT,
        AutonomyThresholds(main=AutonomyLevel.SILENT),
    )

    assert not controller.allows(AutonomyScope.MAIN)
    with pytest.raises(AutonomyBlockedError):
        controller.ensure_allowed(AutonomyScope.MAIN)


@pytest.mark.asyncio
async def test_lowering_level_drains_in_flight_call_and_blocks_the_next():
    controller = AutonomyController(
        AutonomyLevel.AUTONOMOUS,
        AutonomyThresholds(),
    )
    entered = asyncio.Event()
    release = asyncio.Event()

    async def in_flight() -> None:
        async with controller.model_call(AutonomyScope.MAIN):
            entered.set()
            await release.wait()

    task = asyncio.create_task(in_flight())
    await entered.wait()
    controller.update(level=AutonomyLevel.SILENT)

    assert controller.is_draining is True
    release.set()
    await task
    assert controller.is_draining is False
    with pytest.raises(AutonomyBlockedError):
        async with controller.model_call(AutonomyScope.MAIN):
            pass


@pytest.mark.asyncio
async def test_policy_wait_is_observable_and_can_be_aborted():
    controller = AutonomyController(
        AutonomyLevel.SILENT,
        AutonomyThresholds(),
    )

    task = asyncio.create_task(
        controller.wait_until_allowed(AutonomyScope.MEM0)
    )
    await asyncio.sleep(0)

    assert controller.is_policy_paused is True
    assert controller.waiting_scopes == [AutonomyScope.MEM0]

    controller.abort_waiters()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert controller.is_policy_paused is False
    assert controller.waiting_scopes == []


@pytest.mark.asyncio
async def test_aborted_controller_rejects_new_model_calls():
    controller = AutonomyController(
        AutonomyLevel.REACTIVE,
        AutonomyThresholds(),
    )
    controller.abort_waiters()

    with pytest.raises(asyncio.CancelledError):
        async with controller.model_call(AutonomyScope.MAIN):
            pass


@pytest.mark.asyncio
async def test_raising_scope_threshold_marks_existing_call_as_draining():
    controller = AutonomyController(
        AutonomyLevel.REACTIVE,
        AutonomyThresholds(),
    )
    entered = asyncio.Event()
    release = asyncio.Event()

    async def in_flight() -> None:
        async with controller.model_call(AutonomyScope.BUBBLE):
            entered.set()
            await release.wait()

    task = asyncio.create_task(in_flight())
    await entered.wait()
    controller.update(
        thresholds=AutonomyThresholds(bubble=AutonomyLevel.AUTONOMOUS),
    )

    assert controller.is_draining is True
    release.set()
    await task
    assert controller.is_draining is False


@pytest.mark.asyncio
async def test_nested_operation_waits_and_retries_after_policy_change():
    controller = AutonomyController(
        AutonomyLevel.SILENT,
        AutonomyThresholds(),
    )
    attempts = 0

    async def operation() -> str:
        nonlocal attempts
        attempts += 1
        async with controller.model_call(AutonomyScope.SUMMARY):
            return "done"

    task = asyncio.create_task(
        controller.retry_when_allowed(AutonomyScope.SUMMARY, operation)
    )
    await asyncio.sleep(0)

    assert attempts == 1
    assert not task.done()

    controller.update(level=AutonomyLevel.REACTIVE)

    assert await asyncio.wait_for(task, timeout=1) == "done"
    assert attempts == 2


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "scope",
    [
        AutonomyScope.BUBBLE,
        AutonomyScope.SUMMARY,
        AutonomyScope.VISION,
        AutonomyScope.MEM0,
    ],
)
async def test_child_model_work_inherits_origin_trigger(scope):
    controller = AutonomyController(
        AutonomyLevel.REACTIVE,
        AutonomyThresholds(),
    )
    replace_autonomy_trigger(AutonomyLevel.AUTONOMOUS)

    async def child_call() -> None:
        async with controller.model_call(scope):
            pass

    try:
        task = asyncio.create_task(child_call())
        with pytest.raises(AutonomyBlockedError) as caught:
            await task
        assert caught.value.required is AutonomyLevel.AUTONOMOUS
    finally:
        replace_autonomy_trigger(AutonomyLevel.SILENT)


@pytest.mark.asyncio
async def test_pending_events_round_trip_and_keep_wake_level(tmp_path):
    pending_path = tmp_path / "pending_events.sqlite3"
    first = InboxWatcher(tmp_path / "inbox", pending_path=pending_path)
    await first.push(
        IncomingEvent(
            participant_id="alarm",
            content="wake later",
            source="alarm",
            wake_level=AutonomyLevel.EVENT_DRIVEN,
        )
    )

    restored = InboxWatcher(tmp_path / "inbox", pending_path=pending_path)

    assert restored.pending_count == 1
    [event] = await restored.get_pending()
    assert event.content == "wake later"
    assert event.wake_level is AutonomyLevel.EVENT_DRIVEN
    assert restored.pending_count == 0
