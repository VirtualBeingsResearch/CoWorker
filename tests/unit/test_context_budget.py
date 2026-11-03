from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from pydantic import ValidationError

from coworker.core.config import MemoryConfig
from coworker.core.types import PinnedItem
from coworker.memory.context_budget import (
    BUDGET_THRESHOLD,
    ContextBudgetMonitor,
    ContextBudgetSnapshot,
)
from coworker.memory.short_term import ShortTermMemory


class TestSnapshot:
    def test_ratio_counts_only_model_authored_parts(self) -> None:
        snapshot = ContextBudgetSnapshot(
            window_tokens=120_000,
            model_pin_tokens=15_000,
            thinking_tokens=3_000,
            spine_tokens=36_000,
            system_pin_tokens=10_000,
            prefix_tokens=8_000,
        )
        assert snapshot.model_authored_tokens == 18_000
        assert snapshot.ratio == pytest.approx(18_000 / 120_000)
        assert snapshot.over_budget is True
        # 脊柱/系统 pin/提示词模型改不了，只作观测，不参与上限判断。
        assert snapshot.irreducible_tokens == 69_000
        payload = snapshot.to_dict()
        assert payload["model_authored_tokens"] == 18_000
        assert payload["irreducible_tokens"] == 69_000
        assert payload["ratio"] == pytest.approx(0.15)
        assert payload["threshold"] == BUDGET_THRESHOLD
        assert payload["over_budget"] is True

    def test_pins_alone_cannot_reach_the_limit(self) -> None:
        """pin 上限 15k = 0.125，单靠 pin 到不了 0.15：越过上限必须两者都有内容。"""
        pins_only = ContextBudgetSnapshot(window_tokens=120_000, model_pin_tokens=15_000, thinking_tokens=0)
        assert pins_only.over_budget is False
        both = ContextBudgetSnapshot(window_tokens=120_000, model_pin_tokens=15_000, thinking_tokens=3_000)
        assert both.over_budget is True

    def test_boundary_is_inclusive(self) -> None:
        assert ContextBudgetSnapshot(1000, 150, 0, threshold=0.15).over_budget is True
        assert ContextBudgetSnapshot(1000, 149, 0, threshold=0.15).over_budget is False

    def test_zero_window_is_safe(self) -> None:
        snapshot = ContextBudgetSnapshot(window_tokens=0, model_pin_tokens=5, thinking_tokens=0)
        assert snapshot.ratio == 0.0
        assert snapshot.over_budget is False


def _mem(window: int = 1000, model_pins: int = 0) -> ShortTermMemory:
    mem = ShortTermMemory(max_tokens=window)
    if model_pins:
        # CJK 一字符一 token，便于精确构造比例。
        mem.pinned_items.append(PinnedItem(pin_id="p", label="p", content="记" * model_pins))
    return mem


def _monitor(
    mem: ShortTermMemory, thinking: int = 0, threshold: float = BUDGET_THRESHOLD
) -> tuple[ContextBudgetMonitor, MagicMock]:
    inbox = MagicMock()
    inbox.push = AsyncMock(return_value="event-1")
    inbox.cancel = MagicMock()
    builder = MagicMock()
    builder.thinking_tokens = MagicMock(return_value=thinking)
    tools = MagicMock()
    tools.get_schemas = MagicMock(return_value=[])
    monitor = ContextBudgetMonitor(mem, builder, tools, inbox, threshold=threshold)
    return monitor, inbox


class TestMonitor:
    @pytest.mark.asyncio
    async def test_reminds_once_per_context_after_compression(self) -> None:
        mem = _mem(window=1000, model_pins=200)  # 0.20 > 0.15
        monitor, inbox = _monitor(mem)

        await monitor.on_cycle("system prompt")
        assert mem.context_budget is not None
        assert mem.context_budget.model_authored_tokens == 200
        inbox.push.assert_awaited_once()
        event = inbox.push.await_args.args[0]
        assert event.participant_id == "system"
        assert event.source == "context_budget"
        assert "200" in event.content and "1000" in event.content and "15.0" in event.content
        assert inbox.push.await_args.kwargs["wake"] is False

        # 同一段上下文不再重复提示。
        await monitor.on_cycle("system prompt")
        assert inbox.push.await_count == 1

    @pytest.mark.asyncio
    async def test_next_compression_checks_and_cancels_pending(self) -> None:
        mem = _mem(window=1000, model_pins=200)
        monitor, inbox = _monitor(mem)

        await monitor.on_cycle("system prompt")
        mem.compress_generation += 1
        await monitor.on_cycle("system prompt")

        inbox.cancel.assert_called_once_with("event-1")
        assert inbox.push.await_count == 2

    @pytest.mark.asyncio
    async def test_silent_under_the_limit(self) -> None:
        mem = _mem(window=1000, model_pins=100)  # 0.10 < 0.15
        monitor, inbox = _monitor(mem)

        await monitor.on_cycle("system prompt")

        inbox.push.assert_not_awaited()
        assert mem.context_budget is not None  # 采样照做

    @pytest.mark.asyncio
    async def test_recompression_rechecks_after_shrinking(self) -> None:
        mem = _mem(window=1000, model_pins=200)
        monitor, inbox = _monitor(mem)
        await monitor.on_cycle("system prompt")

        mem.pinned_items[0].content = "记" * 10
        mem.compress_generation += 1
        await monitor.on_cycle("system prompt")

        assert inbox.push.await_count == 1  # 已回落到上限内
        assert mem.context_budget.model_authored_tokens == 10

    def test_rejects_invalid_threshold(self) -> None:
        mem = _mem()
        with pytest.raises(ValueError):
            _monitor(mem, threshold=0)
        with pytest.raises(ValueError):
            _monitor(mem, threshold=1)


class TestThresholdConfig:
    def test_default(self) -> None:
        assert MemoryConfig().context_budget_warn_threshold == 0.15

    @pytest.mark.parametrize("value", [0, 1, -0.1, 1.5])
    def test_rejects_out_of_range(self, value) -> None:
        with pytest.raises(ValidationError):
            MemoryConfig(context_budget_warn_threshold=value)
