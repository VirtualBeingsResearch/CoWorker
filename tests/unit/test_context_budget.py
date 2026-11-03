from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from pydantic import ValidationError

from coworker.agent.loop import AgentLoop
from coworker.core.config import MemoryConfig
from coworker.core.types import AgentState, PinnedItem
from coworker.memory.context_budget import (
    BUDGET_BANDS,
    MAX_BUDGET_WARNINGS,
    ContextBudgetSnapshot,
    ContextBudgetWatcher,
    band_for,
    measure_pin_tokens,
)
from coworker.memory.short_term import ShortTermMemory


def _snapshot(ratio: float, window: int = 1000) -> ContextBudgetSnapshot:
    """构造一个恰好落在给定比例上的快照（其余桶留空）。"""
    return ContextBudgetSnapshot(
        window_tokens=window,
        spine_tokens=round(ratio * window),
        model_pin_tokens=0,
        system_pin_tokens=0,
        prefix_tokens=0,
        thinking_tokens=0,
        tail_tokens=0,
    )


class TestBands:
    def test_band_boundaries(self) -> None:
        low, mid, high = BUDGET_BANDS
        assert band_for(low - 0.001) == 0
        assert band_for(low) == 1
        assert band_for(mid - 0.001) == 1
        assert band_for(mid) == 2
        assert band_for(high) == 3
        assert band_for(0.99) == 3

    def test_snapshot_ratio_and_dict(self) -> None:
        snapshot = ContextBudgetSnapshot(
            window_tokens=1000,
            spine_tokens=300,
            model_pin_tokens=80,
            system_pin_tokens=20,
            prefix_tokens=50,
            thinking_tokens=10,
            tail_tokens=400,
        )
        assert snapshot.pin_tokens == 100
        assert snapshot.irreducible_tokens == 450
        assert snapshot.ratio == pytest.approx(0.45)
        assert snapshot.band() == 2
        payload = snapshot.to_dict()
        assert payload["irreducible_tokens"] == 450
        assert payload["tail_tokens"] == 400  # 尾部不进分子
        assert payload["ratio"] == pytest.approx(0.45)

    def test_snapshot_classifies_with_its_own_bands(self) -> None:
        snapshot = _snapshot(0.49)
        assert snapshot.band() == 3
        custom = ContextBudgetSnapshot(
            window_tokens=1000,
            spine_tokens=490,
            model_pin_tokens=0,
            system_pin_tokens=0,
            prefix_tokens=0,
            thinking_tokens=0,
            tail_tokens=0,
            bands=(0.20, 0.40, 0.60),
        )
        assert custom.band() == 2

    def test_zero_window_is_safe(self) -> None:
        snapshot = _snapshot(0.5, window=0)
        assert snapshot.ratio == 0.0
        assert snapshot.band() == 0


class TestMeasurePins:
    def test_splits_model_and_system_pins(self) -> None:
        mem = ShortTermMemory()
        mem.pinned_items.append(PinnedItem(pin_id="mine", label="a", content="一二三四五"))
        mem.pinned_items.append(
            PinnedItem(pin_id="sys", label="b", content="一二三", system_managed=True)
        )
        model_tokens, system_tokens = measure_pin_tokens(mem)
        assert model_tokens == 5
        assert system_tokens == 3

    def test_empty(self) -> None:
        assert measure_pin_tokens(ShortTermMemory()) == (0, 0)


class TestWatcher:
    def test_first_warning_and_remaining(self) -> None:
        watcher = ContextBudgetWatcher()
        assert watcher.warned == 0
        remaining = watcher.observe(_snapshot(0.42), cycle=10, generation=0)
        assert remaining == MAX_BUDGET_WARNINGS - 1
        assert watcher.warned == 1

    def test_below_lowest_band_never_warns(self) -> None:
        watcher = ContextBudgetWatcher()
        assert watcher.observe(_snapshot(0.10), cycle=10, generation=0) is None
        assert watcher.warned == 0

    def test_low_band_allows_only_one_warning(self) -> None:
        watcher = ContextBudgetWatcher()
        assert watcher.observe(_snapshot(0.42), cycle=10, generation=0) is not None
        # 同一档位、间隔已够，也不该再提醒。
        assert watcher.observe(_snapshot(0.42), cycle=100, generation=0) is None

    def test_higher_band_unlocks_more_warnings(self) -> None:
        watcher = ContextBudgetWatcher()
        assert watcher.observe(_snapshot(0.42), cycle=0, generation=0) is not None
        assert watcher.observe(_snapshot(0.46), cycle=100, generation=0) is not None
        assert watcher.observe(_snapshot(0.49), cycle=200, generation=0) is not None
        # 已经到上限 3 次，再高也不提醒。
        assert watcher.observe(_snapshot(0.99), cycle=300, generation=0) is None
        assert watcher.warned == MAX_BUDGET_WARNINGS

    def test_min_gap_between_warnings(self) -> None:
        watcher = ContextBudgetWatcher(min_gap_cycles=8)
        assert watcher.observe(_snapshot(0.80), cycle=0, generation=0) is not None
        assert watcher.observe(_snapshot(0.80), cycle=7, generation=0) is None
        assert watcher.observe(_snapshot(0.80), cycle=8, generation=0) is not None

    def test_next_compression_resets_counter(self) -> None:
        watcher = ContextBudgetWatcher()
        assert watcher.observe(_snapshot(0.80), cycle=0, generation=0) is not None
        assert watcher.observe(_snapshot(0.80), cycle=10, generation=0) is not None
        assert watcher.observe(_snapshot(0.80), cycle=20, generation=0) is not None
        assert watcher.observe(_snapshot(0.80), cycle=30, generation=0) is None
        # 压缩发生（generation 变化）后重新计数。
        assert watcher.observe(_snapshot(0.80), cycle=31, generation=1) is not None
        assert watcher.warned == 1

    def test_falling_back_to_safe_zone_resets_counter(self) -> None:
        watcher = ContextBudgetWatcher()
        assert watcher.observe(_snapshot(0.80), cycle=0, generation=0) is not None
        assert watcher.observe(_snapshot(0.80), cycle=10, generation=0) is not None
        assert watcher.observe(_snapshot(0.20), cycle=20, generation=0) is None
        assert watcher.warned == 0
        # 模型精简后又涨回来：同一次压缩之后仍应重新提醒。
        assert watcher.observe(_snapshot(0.80), cycle=30, generation=0) is not None

    def test_rejects_invalid_configuration(self) -> None:
        with pytest.raises(ValueError):
            ContextBudgetWatcher(bands=())
        with pytest.raises(ValueError):
            ContextBudgetWatcher(max_warnings=0)


def _make_loop(mem: ShortTermMemory) -> AgentLoop:
    loop = AgentLoop.__new__(AgentLoop)
    loop._short_term = mem
    loop._tools = MagicMock()
    loop._tools.get_schemas = MagicMock(return_value=[])
    loop._prompt_builder = MagicMock()
    loop._prompt_builder.build = MagicMock(return_value="system prompt")
    loop._prompt_builder.thinking_tokens = MagicMock(return_value=0)
    loop._inbox = MagicMock()
    loop._inbox.push = AsyncMock(return_value="event-1")
    loop._inbox.cancel = MagicMock()
    loop.state = AgentState(cycle_count=0)
    loop._budget_watcher = ContextBudgetWatcher()
    loop._budget_sampled_generation = None
    loop._budget_last_sample_cycle = None
    loop._budget_warning_event_id = None
    return loop


def _mem_with_pin(tokens: int, window: int = 100) -> ShortTermMemory:
    mem = ShortTermMemory(max_tokens=window)
    # CJK 按 1 字 1 token 计，便于精确构造占比。
    mem.pinned_items.append(PinnedItem(pin_id="big", label="big", content="记" * tokens))
    return mem


class TestLoopSampling:
    @pytest.mark.asyncio
    async def test_snapshot_is_stored_on_short_term(self) -> None:
        mem = _mem_with_pin(60, window=100)
        loop = _make_loop(mem)

        await loop._maybe_warn_context_budget("system prompt")

        snapshot = mem.context_budget
        assert snapshot is not None
        assert snapshot.window_tokens == 100
        assert snapshot.model_pin_tokens == 60
        assert snapshot.irreducible_tokens == 60 + snapshot.prefix_tokens
        assert snapshot.ratio >= BUDGET_BANDS[0]

    @pytest.mark.asyncio
    async def test_warning_goes_out_as_inbound_event(self) -> None:
        mem = _mem_with_pin(60, window=100)
        loop = _make_loop(mem)

        await loop._maybe_warn_context_budget("system prompt")

        loop._inbox.push.assert_awaited_once()
        event = loop._inbox.push.await_args.args[0]
        assert event.participant_id == "system"
        assert event.source == "context_budget"
        # 正文必须给出可核对的具体数字，而不是泛泛要求"优化"。
        assert "100" in event.content
        assert "60" in event.content
        assert loop._inbox.push.await_args.kwargs["wake"] is False

    @pytest.mark.asyncio
    async def test_no_warning_below_threshold(self) -> None:
        mem = _mem_with_pin(5, window=1000)
        loop = _make_loop(mem)

        await loop._maybe_warn_context_budget("system prompt")

        loop._inbox.push.assert_not_awaited()
        assert mem.context_budget is not None  # 采样照做，只是不提醒

    @pytest.mark.asyncio
    async def test_sampling_is_throttled_between_cycles(self) -> None:
        mem = _mem_with_pin(60, window=100)
        loop = _make_loop(mem)

        await loop._maybe_warn_context_budget("system prompt")
        first = mem.context_budget
        loop.state.cycle_count = 1
        await loop._maybe_warn_context_budget("system prompt")

        assert mem.context_budget is first  # 间隔不足，未重新采样

    @pytest.mark.asyncio
    async def test_compression_triggers_fresh_sample_and_cancels_pending(self) -> None:
        mem = _mem_with_pin(60, window=100)
        loop = _make_loop(mem)

        await loop._maybe_warn_context_budget("system prompt")
        assert loop._budget_warning_event_id == "event-1"

        mem.compress_generation += 1
        loop.state.cycle_count = 1
        await loop._maybe_warn_context_budget("system prompt")

        # 上一次压缩留下的提醒作废并撤回，压缩后重新采样。
        loop._inbox.cancel.assert_called_once_with("event-1")
        assert loop._budget_sampled_generation == mem.compress_generation
        loop._inbox.push.assert_awaited()


class TestThresholdConfig:
    def test_defaults_are_the_documented_bands(self) -> None:
        assert MemoryConfig().context_budget_warn_thresholds == [0.40, 0.45, 0.48]

    def test_accepts_comma_separated_env_style_string(self) -> None:
        config = MemoryConfig(context_budget_warn_thresholds="0.4, 0.45,0.48")
        assert config.context_budget_warn_thresholds == [0.40, 0.45, 0.48]

    @pytest.mark.parametrize(
        "value",
        [[0.6, 0.5, 0.7], [0.4, 0.55, 0.55], [0.0, 0.5, 0.9], [0.4, 1.2], []],
    )
    def test_rejects_invalid_thresholds(self, value) -> None:
        with pytest.raises(ValidationError):
            MemoryConfig(context_budget_warn_thresholds=value)
