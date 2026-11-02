from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from coworker.brain.brain import ModelCallSuspended, set_model_call_owner
from coworker.core.llm_errors import FailureCategory, ModelRoutingUnavailable
from tests.unit.test_model_routing import make_brain, think


@pytest.mark.parametrize("protocol", ["chat", "responses", "anthropic"])
def test_rejected_response_preserves_reasoning_usage(protocol):
    from coworker.core.llm_errors import ProviderCallError, capture_response_usage

    fields = {"prompt_tokens": 17, "completion_tokens": 3,
              "completion_tokens_details": SimpleNamespace(reasoning_tokens=2)} if protocol == "chat" else {
                  "input_tokens": 17, "output_tokens": 3,
                  "output_tokens_details": SimpleNamespace(**{"thinking_tokens" if protocol == "anthropic" else "reasoning_tokens": 2}),
              }
    error = ProviderCallError(FailureCategory.CONTENT_POLICY)
    with pytest.raises(ProviderCallError), capture_response_usage(SimpleNamespace(usage=SimpleNamespace(**fields))):
        raise error
    assert error.usage == {"input_tokens": 17, "output_tokens": 3, "reasoning_tokens": 2}


def block_provider(provider, *, first_only=False, ignore_cancel=False):
    started, release = asyncio.Event(), asyncio.Event()
    original = provider.complete
    count = 0

    async def blocked(*args, **kwargs):
        nonlocal count
        count += 1
        if not first_only or count == 1:
            started.set()
            try:
                await release.wait()
            except asyncio.CancelledError:
                if not ignore_cancel:
                    raise
        return await original(*args, **kwargs)

    provider.complete = blocked
    return started, release


@pytest.mark.asyncio
async def test_switch_interrupts_backoff_without_counting_a_model_outage():
    brain, primary, backup, _ = make_brain()
    from coworker.core.model_routing import ModelRoutingConfig

    brain._routing_config = ModelRoutingConfig(retry_delay_seconds=30)
    waiting = asyncio.Event()
    brain.router.add_listener(lambda event: waiting.set() if event.get("outcome") == "failed" else None)
    task = asyncio.create_task(think(brain))
    await asyncio.wait_for(waiting.wait(), 2)
    await brain.switch_model("backup", "backup-model")
    response = await asyncio.wait_for(task, 2)
    assert response.provider == "backup"
    assert len(primary.calls) == len(backup.calls) == 1
    assert brain.router.snapshot() == []
    assert len({entry["routing_id"] for entry in response.routing["attempts"]}) == 1
    assert not brain._inflight_calls


@pytest.mark.asyncio
async def test_reselecting_model_does_not_reset_the_attempt_budget():
    brain, primary, _, _ = make_brain(max_attempts=2)
    primary.failure = None
    started, _ = block_provider(primary)
    task = asyncio.create_task(think(brain))
    for _ in range(2):
        await asyncio.wait_for(started.wait(), 2)
        started.clear()
        await brain.switch_model("primary", "primary-model")
    with pytest.raises(ModelRoutingUnavailable) as raised:
        await asyncio.wait_for(task, 2)
    assert len(raised.value.failures) == 2
    assert all(entry["outcome"] == "cancelled" for entry in raised.value.failures)
    assert brain.router.snapshot() == []
    assert not brain._inflight_calls


@pytest.mark.asyncio
async def test_reselecting_model_does_not_reset_the_request_deadline():
    brain, primary, _, clock = make_brain(request_timeout_seconds=10)
    primary.failure = None
    started, _ = block_provider(primary)
    task = asyncio.create_task(think(brain))
    await asyncio.wait_for(started.wait(), 2)
    clock.advance(11)
    await brain.switch_model("primary", "primary-model")
    with pytest.raises(ModelRoutingUnavailable) as raised:
        await asyncio.wait_for(task, 2)
    assert len(raised.value.failures) == 1
    assert not brain._inflight_calls


@pytest.mark.asyncio
async def test_repeated_switch_limit_is_a_recoverable_wait_not_an_unexpected_error():
    brain, primary, _, _ = make_brain(max_attempts=20)
    primary.failure = None
    started, _ = block_provider(primary)
    task = asyncio.create_task(think(brain))
    for _ in range(9):
        await asyncio.wait_for(started.wait(), 2)
        started.clear()
        await brain.switch_model("primary", "primary-model")
    with pytest.raises(ModelRoutingUnavailable) as raised:
        await asyncio.wait_for(task, 2)
    assert len(raised.value.failures) == 9
    assert brain.router.snapshot() == []
    assert not brain._inflight_calls


@pytest.mark.asyncio
@pytest.mark.parametrize("pause_first", [False, True])
async def test_pause_wins_a_concurrent_model_switch(pause_first):
    set_model_call_owner("main")
    brain, primary, backup, _ = make_brain()
    primary.failure = None
    started, _ = block_provider(primary)
    task = asyncio.create_task(think(brain))
    await asyncio.wait_for(started.wait(), 2)
    if pause_first:
        brain.suspend_main_calls()
    await brain.switch_model("backup", "backup-model")
    if not pause_first:
        brain.suspend_main_calls()
    with pytest.raises(ModelCallSuspended):
        await asyncio.wait_for(task, 2)
    assert backup.calls == []
    assert brain.router.snapshot() == []
    brain.resume_main_calls()
    assert (await think(brain)).provider == "backup"


@pytest.mark.asyncio
async def test_shutdown_after_switch_still_propagates_external_cancellation():
    brain, primary, backup, _ = make_brain()
    primary.failure = None
    started, _ = block_provider(primary)
    task = asyncio.create_task(think(brain))
    await asyncio.wait_for(started.wait(), 2)
    await brain.switch_model("backup", "backup-model")
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert backup.calls == []
    assert brain.router.snapshot() == []
    assert not brain._inflight_calls


@pytest.mark.asyncio
async def test_switch_discards_a_provider_response_that_ignores_cancellation():
    brain, primary, _, _ = make_brain()
    primary.failure = None
    started, _ = block_provider(primary, ignore_cancel=True)
    task = asyncio.create_task(think(brain))
    await asyncio.wait_for(started.wait(), 2)
    await brain.switch_model("backup", "backup-model")
    response = await asyncio.wait_for(task, 2)
    assert response.provider == "backup"
    assert brain.model_config_snapshot()["last_used"]["provider"] == "backup"


@pytest.mark.asyncio
async def test_pausing_recovery_releases_probe_and_resume_recovers_preference():
    set_model_call_owner("main")
    brain, primary, _, clock = make_brain(failure_threshold=1)
    assert (await think(brain)).provider == "backup"
    clock.advance(60)
    primary.failure = None
    started, _ = block_provider(primary, first_only=True)
    task = asyncio.create_task(think(brain))
    await asyncio.wait_for(started.wait(), 2)
    assert any(state["probing"] for state in brain.router.snapshot())
    brain.suspend_main_calls()
    with pytest.raises(ModelCallSuspended):
        await task
    assert all(not state["probing"] and state["recovery_successes"] == 0 for state in brain.router.snapshot())
    brain.resume_main_calls()
    assert (await think(brain)).provider == "primary"
    assert (await think(brain)).provider == "primary"
    assert brain.router.snapshot() == []


@pytest.mark.asyncio
@pytest.mark.parametrize("role", ["bubble", "subconscious"])
@pytest.mark.parametrize("inherit", [False, True])
async def test_main_controls_do_not_cancel_independent_child_brains(role, inherit):
    from coworker.agent.subconscious import SubconsciousScheduler
    from coworker.tools.bubble_tools import _create_bubble_brain

    brain, primary, backup, _ = make_brain()
    primary.failure = None
    if role == "bubble":
        child = _create_bubble_brain(brain, "primary", "primary-model", inherit_policy=inherit)
    else:
        scheduler = SubconsciousScheduler.__new__(SubconsciousScheduler)
        scheduler._brain = brain
        child = scheduler._create_brain()
    started, release = block_provider(primary, first_only=True)

    async def child_call():
        set_model_call_owner("bubble")
        return await think(child)

    task = asyncio.create_task(child_call())
    await asyncio.wait_for(started.wait(), 2)
    assert brain.suspend_main_calls() == 0
    await brain.switch_model("backup", "backup-model")
    assert not task.done()
    release.set()
    assert (await asyncio.wait_for(task, 2)).provider == "primary"
    expected = "backup" if inherit or role == "subconscious" else "primary"
    assert (await think(child)).provider == expected
    assert brain.current_provider_name == "backup"
    assert len(backup.calls) == (1 if expected == "backup" else 0)


@pytest.mark.asyncio
async def test_model_switch_while_a_fallback_is_inflight_reselects_new_preference():
    brain, primary, backup, _ = make_brain(category=FailureCategory.RATE_LIMIT)
    started, _ = block_provider(backup)
    task = asyncio.create_task(think(brain))
    await asyncio.wait_for(started.wait(), 2)
    primary.failure = None
    await brain.switch_model("primary", "primary-model")
    response = await asyncio.wait_for(task, 2)
    assert response.provider == brain.current_provider_name == "primary"
    assert all(state["state"] == "half_open" and state["recovery_successes"] == 1 for state in brain.router.snapshot())
    assert (await think(brain)).provider == "primary"
    assert brain.router.snapshot() == []


@pytest.mark.asyncio
async def test_explicit_summary_survives_main_pause_even_with_main_owner():
    from coworker.core.types import Message

    set_model_call_owner("main")
    brain, primary, _, _ = make_brain()
    primary.failure = None
    brain._summary_provider_name = "primary"
    brain._summary_model = "primary-model"
    started, release = block_provider(primary)
    task = asyncio.create_task(brain.summarize([Message(role="user", content="demo history")]))
    await asyncio.wait_for(started.wait(), 2)
    assert brain.suspend_main_calls() == 0
    release.set()
    assert await asyncio.wait_for(task, 2) == "primary"


@pytest.mark.asyncio
@pytest.mark.parametrize("full", [False, True])
async def test_real_background_compression_pause_retains_history_and_can_retry(full):
    from coworker.core.types import Message
    from coworker.memory.short_term import ShortTermMemory
    from tests.unit.test_loop_summaries import _make_loop

    set_model_call_owner("main")
    brain, primary, _, _ = make_brain()
    primary.failure = None
    mem = ShortTermMemory(max_tokens=100, compress_ratio=0.30)
    mem.primary = [Message(role="user", content=f"demo {index}:" + "x" * 400) for index in range(8)]
    original = list(mem.primary)
    loop = _make_loop(brain, mem)
    loop._inbox.message_event = asyncio.Event()
    started, release = block_provider(primary, first_only=True)

    def schedule():
        if full:
            assert mem.schedule_full_compression(brain)
            return mem._full_compress_task
        loop._maybe_schedule_compression(100, "demo prompt")
        return loop._compress_task

    task = schedule()
    assert task is not None
    await asyncio.wait_for(started.wait(), 2)
    assert loop.request_pause() == 1
    await asyncio.wait_for(task, 2)
    assert mem.primary == original
    assert not mem.compression_in_progress
    assert loop._compress_retry_at == 0
    brain.resume_main_calls()
    release.set()
    retried = schedule()
    assert retried is not None
    await asyncio.wait_for(retried, 2)
    assert len(mem.primary) < len(original)
    assert mem.tree.nodes
    assert not brain._inflight_calls


@pytest.mark.asyncio
async def test_main_pause_saves_consumed_message_and_resume_does_not_consume_it_twice(tmp_path):
    from coworker.agent.inbox_watcher import InboxWatcher
    from coworker.core.types import IncomingEvent
    from coworker.memory.short_term import ShortTermMemory
    from tests.unit.test_loop_summaries import _make_loop

    brain, primary, _, _ = make_brain()
    primary.failure = None
    mem = ShortTermMemory()
    loop = _make_loop(brain, mem)
    loop._stop_event = asyncio.Event()
    loop._resume_event = asyncio.Event()
    loop._config.agent.passive_mode = False
    loop._snapshot_path = tmp_path / "snapshot.json"
    loop.state.restart_requested = False
    loop._inbox = InboxWatcher(str(tmp_path / "inbox"))
    await loop._inbox.push(IncomingEvent(participant_id="demo-user", content="demo question"))
    started, _ = block_provider(primary, first_only=True)
    original = primary.complete

    async def stop_after_response(*args, **kwargs):
        response = await original(*args, **kwargs)
        loop.stop()
        return response

    primary.complete = stop_after_response
    snapshot_saved = asyncio.Event()
    save = mem.save_to_file

    def save_snapshot(path):
        save(path)
        snapshot_saved.set()

    mem.save_to_file = save_snapshot
    task = asyncio.create_task(loop.run())
    try:
        await asyncio.wait_for(started.wait(), 2)
        loop._config.agent.paused = True
        assert loop.request_pause() == 1
        await asyncio.wait_for(snapshot_saved.wait(), 2)
        restored = ShortTermMemory.load_from_file(loop._snapshot_path)
        assert restored is not None
        assert len([m for m in restored.primary if "demo question" in str(m.content)]) == 1
        assert loop._consecutive_errors == 0
        for _ in range(20):
            if loop.state.is_sleeping:
                break
            await asyncio.sleep(0)
        assert loop.state.is_sleeping
        loop._config.agent.paused = False
        assert loop.resume_from_rest()
        await asyncio.wait_for(task, 2)
        assert len([m for m in mem.primary if "demo question" in str(m.content)]) == 1
        assert len([m for m in mem.primary if m.role == "assistant"]) == 1
        assert not brain._inflight_calls
    finally:
        loop.stop()
        if not task.done():
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
