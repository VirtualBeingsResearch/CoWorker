from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from pydantic import ValidationError

from coworker.core.config import MemoryConfig
from coworker.core.types import PinnedItem
from coworker.memory.context_budget import (
    BUDGET_BANDS,
    MAX_WARNINGS,
    ContextBudgetMonitor,
    ContextBudgetSnapshot,
)
from coworker.memory.short_term import ShortTermMemory


def _snapshot(ratio: float, window: int = 1000) -> ContextBudgetSnapshot:
    """构造一个模型自写部分恰好落在给定比例上的快照。"""
    return ContextBudgetSnapshot(
        window_tokens=window,
        model_pin_tokens=round(ratio * window),
        thinking_tokens=0,
    )


class TestSnapshot:
    def test_ratio_counts_only_model_authored_parts(self) -> None:
        snapshot = ContextBudgetSnapshot(
            window_tokens=120_000,
            model_pin_tokens=6_000,
            thinking_tokens=1_000,
            spine_tokens=36_000,
            system_pin_tokens=10_000,
            prefix_tokens=8_000,
        )
        assert snapshot.model_authored_tokens == 7_000
        assert snapshot.ratio == pytest.approx(7_000 / 120_000)
        # 脊柱/系统 pin/提示词模型改不了，只作观测，不进档位。
        assert snapshot.irreducible_tokens == 60_000
        payload = snapshot.to_dict()
        assert payload["model_authored_tokens"] == 7_000
        assert payload["irreducible_tokens"] == 60_000
        assert payload["ratio"] == pytest.approx(0.0583)
        assert payload["band"] == 1

    def test_band_boundaries(self) -> None:
        low, mid, high = BUDGET_BANDS
        assert _snapshot(low - 0.001).band() == 0
        assert _snapshot(low).band() == 1
        assert _snapshot(mid - 0.001).band() == 1
        assert _snapshot(mid).band() == 2
        assert _snapshot(high).band() == 3
        assert _snapshot(0.99).band() == 3

    def test_zero_window_is_safe(self) -> None:
        snapshot = _snapshot(0.5, window=0)
        assert snapshot.ratio == 0.0
        assert snapshot.band() == 0

    def test_snapshot_uses_its_own_bands(self) -> None:
        custom = ContextBudgetSnapshot(
            window_tokens=1000, model_pin_tokens=450, thinking_tokens=0, bands=(0.2, 0.4, 0.6)
        )
        assert custom.band() == 2


def _mem(window: int = 1000, model_pins: int = 0) -> ShortTermMemory:
    mem = ShortTermMemory(max_tokens=window)
    if model_pins:
        # CJK 一字符一 token，便于精确构造比例。
        mem.pinned_items.append(PinnedItem(pin_id="p", label="p", content="记" * model_pins))
    return mem


def _monitor(mem: ShortTermMemory, thinking: int = 0) -> tuple[ContextBudgetMonitor, MagicMock]:
    inbox = MagicMock()
    inbox.push = AsyncMock(return_value="event-1")
    inbox.cancel = MagicMock()
    builder = MagicMock()
    builder.thinking_tokens = MagicMock(return_value=thinking)
    tools = MagicMock()
    tools.get_schemas = MagicMock(return_value=[])
    return ContextBudgetMonitor(mem, builder, tools, inbox), inbox


class TestMonitor:
    @pytest.mark.asyncio
    async def test_warns_once_and_records_snapshot(self) -> None:
        mem = _mem(window=1000, model_pins=60)
        monitor, inbox = _monitor(mem)

        await monitor.on_cycle("system prompt", cycle=0)

        assert mem.context_budget is not None
        assert mem.context_budget.model_authored_tokens == 60
        inbox.push.assert_awaited_once()
        event = inbox.push.await_args.args[0]
        assert event.participant_id == "system"
        assert event.source == "context_budget"
        assert "60" in event.content and "1000" in event.content
        assert inbox.push.await_args.kwargs["wake"] is False

    @pytest.mark.asyncio
    async def test_silent_below_lowest_band(self) -> None:
        mem = _mem(window=1000, model_pins=10)
        monitor, inbox = _monitor(mem)

        await monitor.on_cycle("system prompt", cycle=0)

        inbox.push.assert_not_awaited()
        assert mem.context_budget is not None  # 采样照做

    @pytest.mark.asyncio
    async def test_sampling_is_throttled(self) -> None:
        mem = _mem(window=1000, model_pins=60)
        monitor, _ = _monitor(mem)

        await monitor.on_cycle("system prompt", cycle=0)
        first = mem.context_budget
        await monitor.on_cycle("system prompt", cycle=1)

        assert mem.context_budget is first  # 间隔不足，未重新采样

    @pytest.mark.asyncio
    async def test_lowest_band_allows_only_one_warning(self) -> None:
        mem = _mem(window=1000, model_pins=60)
        monitor, inbox = _monitor(mem)

        await monitor.on_cycle("system prompt", cycle=0)
        await monitor.on_cycle("system prompt", cycle=100)

        assert inbox.push.await_count == 1

    @pytest.mark.asyncio
    async def test_higher_bands_unlock_more_warnings_up_to_cap(self) -> None:
        mem = _mem(window=1000, model_pins=60)
        monitor, inbox = _monitor(mem)

        await monitor.on_cycle("system prompt", cycle=0)
        mem.pinned_items[0].content = "记" * 100  # 0.10 → band 2
        await monitor.on_cycle("system prompt", cycle=100)
        mem.pinned_items[0].content = "记" * 150  # 0.15 → band 3
        await monitor.on_cycle("system prompt", cycle=200)
        mem.pinned_items[0].content = "记" * 500
        await monitor.on_cycle("system prompt", cycle=300)

        assert inbox.push.await_count == MAX_WARNINGS

    @pytest.mark.asyncio
    async def test_new_compression_resets_and_cancels_pending(self) -> None:
        mem = _mem(window=1000, model_pins=60)
        monitor, inbox = _monitor(mem)

        await monitor.on_cycle("system prompt", cycle=0)
        assert monitor._pending_event == "event-1"

        mem.compress_generation += 1
        await monitor.on_cycle("system prompt", cycle=50)

        inbox.cancel.assert_called_once_with("event-1")
        # 新纪元重新计数，并且重新采样。
        assert monitor._warned == 1
        assert inbox.push.await_count == 2

    @pytest.mark.asyncio
    async def test_falling_back_to_safe_zone_resets_counter(self) -> None:
        mem = _mem(window=1000, model_pins=60)
        monitor, inbox = _monitor(mem)

        await monitor.on_cycle("system prompt", cycle=0)
        await monitor.on_cycle("system prompt", cycle=100)  # band 1 已用满
        mem.pinned_items[0].content = "记" * 10  # 回落到安全区
        await monitor.on_cycle("system prompt", cycle=200)
        mem.pinned_items[0].content = "记" * 60  # 再次进入 band 1
        await monitor.on_cycle("system prompt", cycle=300)

        assert inbox.push.await_count == 2
        assert monitor._warned == 1


class TestThresholdConfig:
    def test_defaults_are_the_documented_bands(self) -> None:
        assert MemoryConfig().context_budget_warn_thresholds == [0.05, 0.10, 0.15]

    def test_accepts_comma_separated_env_style_string(self) -> None:
        config = MemoryConfig(context_budget_warn_thresholds="0.05, 0.10,0.15")
        assert config.context_budget_warn_thresholds == [0.05, 0.10, 0.15]

    @pytest.mark.parametrize(
        "value",
        [[0.6, 0.5, 0.7], [0.4, 0.55, 0.55], [0.0, 0.5, 0.9], [0.4, 1.2], []],
    )
    def test_rejects_invalid_thresholds(self, value) -> None:
        with pytest.raises(ValidationError):
            MemoryConfig(context_budget_warn_thresholds=value)
