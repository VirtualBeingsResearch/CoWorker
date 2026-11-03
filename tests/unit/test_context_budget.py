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
            window_tokens=120_000, model_pin_tokens=20_000, thinking_tokens=6_000
        )
        assert snapshot.model_authored_tokens == 26_000
        assert snapshot.ratio == pytest.approx(26_000 / 120_000)
        assert snapshot.over_budget is True
        payload = snapshot.to_dict()
        assert payload == {
            "window_tokens": 120_000,
            "model_pin_tokens": 20_000,
            "thinking_tokens": 6_000,
            "model_authored_tokens": 26_000,
            "ratio": pytest.approx(0.2167),
            "threshold": BUDGET_THRESHOLD,
            "over_budget": True,
        }

    def test_budget_leaves_room_for_thinking(self) -> None:
        """上限 0.19 ≈ 22.8k：pin 满仓后仍留约 7.8k 给 thinking，两者都写满才越线。"""
        assert ContextBudgetSnapshot(120_000, 15_000, 0).over_budget is False
        assert ContextBudgetSnapshot(120_000, 15_000, 7_000).over_budget is False
        assert ContextBudgetSnapshot(120_000, 15_000, 8_000).over_budget is True

    def test_boundary_is_inclusive(self) -> None:
        assert ContextBudgetSnapshot(1000, 190, 0).over_budget is True
        assert ContextBudgetSnapshot(1000, 189, 0).over_budget is False

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
    return ContextBudgetMonitor(mem, builder, inbox, threshold=threshold), inbox


class TestMonitor:
    @pytest.mark.asyncio
    async def test_reminds_once_per_context(self) -> None:
        mem = _mem(window=1000, model_pins=250)  # 0.25 > 0.19
        monitor, inbox = _monitor(mem)

        await monitor.on_cycle()
        assert mem.context_budget is not None
        assert mem.context_budget.model_authored_tokens == 250
        inbox.push.assert_awaited_once()
        event = inbox.push.await_args.args[0]
        assert event.participant_id == "system"
        assert event.source == "context_budget"
        assert "250" in event.content and "1000" in event.content and "19.0" in event.content
        assert inbox.push.await_args.kwargs["wake"] is False

        # 同一段上下文不再重复提示。
        await monitor.on_cycle()
        assert inbox.push.await_count == 1

    @pytest.mark.asyncio
    async def test_next_compression_checks_and_cancels_pending(self) -> None:
        mem = _mem(window=1000, model_pins=250)
        monitor, inbox = _monitor(mem)

        await monitor.on_cycle()
        mem.compress_generation += 1
        await monitor.on_cycle()

        inbox.cancel.assert_called_once_with("event-1")
        assert inbox.push.await_count == 2

    @pytest.mark.asyncio
    async def test_silent_under_the_limit(self) -> None:
        mem = _mem(window=1000, model_pins=180)  # 0.18 < 0.19
        monitor, inbox = _monitor(mem)

        await monitor.on_cycle()

        inbox.push.assert_not_awaited()
        assert mem.context_budget is not None  # 采样照做

    @pytest.mark.asyncio
    async def test_recompression_rechecks_after_shrinking(self) -> None:
        mem = _mem(window=1000, model_pins=250)
        monitor, inbox = _monitor(mem)
        await monitor.on_cycle()

        mem.pinned_items[0].content = "记" * 10
        mem.compress_generation += 1
        await monitor.on_cycle()

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
        assert MemoryConfig().context_budget_warn_threshold == 0.19

    @pytest.mark.parametrize("value", [0, 1, -0.1, 1.5])
    def test_rejects_out_of_range(self, value) -> None:
        with pytest.raises(ValidationError):
            MemoryConfig(context_budget_warn_threshold=value)
