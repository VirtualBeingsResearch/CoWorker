"""上下文预算：计量模型自己写进上下文的部分，超限时提醒。

模型能自己写、又没有硬上限的只有两处：pin 与 ``data/thinking.md``。脊柱有结构性 cap，
pin 有 5k/15k 写入闸门，但都没有"你正在吃掉多少窗口"的反馈。这里把这两项算成占
``MEMORY__SHORT_TERM_MAX_TOKENS`` 的比例，按档位通过 inbound 提醒。

两项共用同一份预算：pin 多占，thinking.md 的余量就少。所以顶档设在 pin 上限之上，
顶档意味着"两者都占了不少"，pin 单靠自己到不了顶档。

只提醒不拦截；两次压缩之间最多提醒 3 次，档位越高允许得越多，比例回落到安全区或
下一次压缩（``compress_generation`` 变化）时清零。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING

from loguru import logger

from coworker.core.token_utils import estimate_text_tokens
from coworker.core.types import IncomingEvent
from coworker.i18n import tr

if TYPE_CHECKING:
    from coworker.agent.inbox_watcher import InboxWatcher
    from coworker.memory.short_term import ShortTermMemory
    from coworker.prompts.system_prompt import SystemPromptBuilder
    from coworker.tools.registry import ToolRegistry

# 模型自写部分占窗口的比例。pin 与 thinking 共用这份预算，顶档要高于 pin 上限
# （15k = 0.125）：0.05 ≈ 6k（pin 用了四成）、0.10 ≈ 12k、0.15 ≈ 18k（pin 满仓后仍有
# 约 3k 留给 thinking.md，pin 单靠自己够不到顶档）。
BUDGET_BANDS: tuple[float, ...] = (0.05, 0.10, 0.15)
MAX_WARNINGS = 3
MIN_GAP_CYCLES = 8


@dataclass(frozen=True)
class ContextBudgetSnapshot:
    """一次采样：``model_authored_tokens`` 参与档位，其余字段只用于展示。"""

    window_tokens: int
    model_pin_tokens: int
    thinking_tokens: int
    spine_tokens: int = 0
    system_pin_tokens: int = 0
    prefix_tokens: int = 0
    bands: tuple[float, ...] = BUDGET_BANDS

    @property
    def model_authored_tokens(self) -> int:
        return self.model_pin_tokens + self.thinking_tokens

    @property
    def irreducible_tokens(self) -> int:
        """整段压缩砍不掉的量，仅用于观测。"""
        return (
            self.spine_tokens
            + self.model_pin_tokens
            + self.system_pin_tokens
            + self.prefix_tokens
        )

    @property
    def ratio(self) -> float:
        if self.window_tokens <= 0:
            return 0.0
        return self.model_authored_tokens / self.window_tokens

    def band(self) -> int:
        """0=安全区，1..len(bands)=逐级超限。"""
        band = 0
        for index, threshold in enumerate(self.bands, start=1):
            if self.ratio >= threshold:
                band = index
        return band

    def to_dict(self) -> dict[str, int | float]:
        return {
            "window_tokens": self.window_tokens,
            "model_pin_tokens": self.model_pin_tokens,
            "thinking_tokens": self.thinking_tokens,
            "model_authored_tokens": self.model_authored_tokens,
            "spine_tokens": self.spine_tokens,
            "system_pin_tokens": self.system_pin_tokens,
            "prefix_tokens": self.prefix_tokens,
            "irreducible_tokens": self.irreducible_tokens,
            "ratio": round(self.ratio, 4),
            "band": self.band(),
        }


class ContextBudgetMonitor:
    """每个 cycle 调一次 ``on_cycle``：采样、节流、必要时投递一条 inbound 提醒。"""

    def __init__(
        self,
        short_term: ShortTermMemory,
        prompt_builder: SystemPromptBuilder,
        tools: ToolRegistry,
        inbox: InboxWatcher,
        *,
        bands: tuple[float, ...] = BUDGET_BANDS,
    ) -> None:
        if not bands:
            raise ValueError("bands must not be empty")
        self.snapshot: ContextBudgetSnapshot | None = None
        self._short_term = short_term
        self._prompt_builder = prompt_builder
        self._tools = tools
        self._inbox = inbox
        self._bands = tuple(bands)
        self._generation: int | None = None
        self._last_sample_cycle: int | None = None
        self._warned = 0
        self._last_warning_cycle: int | None = None
        self._pending_event: str | None = None

    async def on_cycle(self, system_prompt: str, cycle: int) -> None:
        generation = self._short_term.compress_generation
        if not self._sample_due(generation, cycle):
            return
        if generation != self._generation:
            if self._generation is not None:
                self._cancel_pending()
            self._generation = generation
            self._warned = 0
            self._last_warning_cycle = None
        self._last_sample_cycle = cycle

        snapshot = self._measure(system_prompt)
        self.snapshot = snapshot
        self._short_term.context_budget = snapshot

        remaining = self._throttle(snapshot, cycle)
        if remaining is None:
            return
        self._pending_event = await self._inbox.push(
            IncomingEvent(
                participant_id="system",
                content=tr(
                    "memory.budget.notice",
                    authored=snapshot.model_authored_tokens,
                    window=snapshot.window_tokens,
                    percent=f"{snapshot.ratio * 100:.1f}",
                    model_pins=snapshot.model_pin_tokens,
                    thinking=snapshot.thinking_tokens,
                ),
                source="context_budget",
                timestamp=datetime.now(),
            ),
            wake=False,
        )
        logger.info(
            f"Context budget warning: model-authored ~{snapshot.model_authored_tokens} tokens "
            f"({snapshot.ratio:.1%} of {snapshot.window_tokens}), "
            f"{remaining} reminder(s) left before the next compression"
        )

    def _sample_due(self, generation: int, cycle: int) -> bool:
        """压缩后的首轮必采；其余每 ``MIN_GAP_CYCLES`` 轮补采一次。"""
        if generation != self._generation:
            return True
        last = self._last_sample_cycle
        return last is None or cycle - last >= MIN_GAP_CYCLES

    def _measure(self, system_prompt: str) -> ContextBudgetSnapshot:
        model_pins = 0
        system_pins = 0
        for item in self._short_term.pinned_items:
            tokens = estimate_text_tokens(item.content)
            if item.system_managed:
                system_pins += tokens
            else:
                model_pins += tokens
        tool_tokens = estimate_text_tokens(
            json.dumps(self._tools.get_schemas(), ensure_ascii=False)
        )
        return ContextBudgetSnapshot(
            window_tokens=self._short_term.max_tokens,
            model_pin_tokens=model_pins,
            thinking_tokens=self._prompt_builder.thinking_tokens(),
            spine_tokens=self._short_term.tree.spine_tokens(),
            system_pin_tokens=system_pins,
            prefix_tokens=estimate_text_tokens(system_prompt) + tool_tokens,
            bands=self._bands,
        )

    def _throttle(self, snapshot: ContextBudgetSnapshot, cycle: int) -> int | None:
        """返回本次提醒后剩余的额度；``None`` 表示这次不提醒。"""
        band = snapshot.band()
        if band == 0:
            self._warned = 0
            self._last_warning_cycle = None
            return None
        if self._warned >= min(band, MAX_WARNINGS):
            return None
        if (
            self._last_warning_cycle is not None
            and cycle - self._last_warning_cycle < MIN_GAP_CYCLES
        ):
            return None
        self._warned += 1
        self._last_warning_cycle = cycle
        return MAX_WARNINGS - self._warned

    def _cancel_pending(self) -> None:
        """压缩后旧提醒的数字已过期，撤回尚未投递的那条。"""
        event_id = self._pending_event
        self._pending_event = None
        if event_id is not None:
            self._inbox.cancel(event_id)
