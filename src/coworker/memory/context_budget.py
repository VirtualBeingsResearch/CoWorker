"""上下文预算：计量模型自己写进上下文的部分，超过上限时提醒。

模型能自己写、又没有硬上限的只有两处：pin 与 ``data/thinking.md``。脊柱有结构性 cap，
pin 有 5k/15k 写入闸门，但都没有"你正在吃掉多少窗口"的反馈。这里把这两项算成占
``MEMORY__SHORT_TERM_MAX_TOKENS`` 的比例，每次上下文重建后比较一次上限。

两项共用同一份预算：pin 多占，thinking.md 的余量就少。上限因此高于 pin 上限（15k），
越过它意味着两者都写了不少，pin 单靠自己越不过去。
"""

from __future__ import annotations

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

# 模型自写部分占窗口的上限。120k 窗口下 0.19 ≈ 22.8k：pin 满仓（15k）后仍给
# thinking.md 留约 7.8k，两者都写满就会越线。
BUDGET_THRESHOLD = 0.19


@dataclass(frozen=True)
class ContextBudgetSnapshot:
    """一次采样：模型自写部分与窗口上限的比较结果。"""

    window_tokens: int
    model_pin_tokens: int
    thinking_tokens: int
    threshold: float = BUDGET_THRESHOLD

    @property
    def model_authored_tokens(self) -> int:
        return self.model_pin_tokens + self.thinking_tokens

    @property
    def ratio(self) -> float:
        if self.window_tokens <= 0:
            return 0.0
        return self.model_authored_tokens / self.window_tokens

    @property
    def over_budget(self) -> bool:
        return self.ratio >= self.threshold

    def to_dict(self) -> dict[str, int | float | bool]:
        return {
            "window_tokens": self.window_tokens,
            "model_pin_tokens": self.model_pin_tokens,
            "thinking_tokens": self.thinking_tokens,
            "model_authored_tokens": self.model_authored_tokens,
            "ratio": round(self.ratio, 4),
            "threshold": self.threshold,
            "over_budget": self.over_budget,
        }


class ContextBudgetMonitor:
    """每个 cycle 调一次 ``on_cycle``；只在 ``compress_generation`` 变化后检查并提醒。"""

    def __init__(
        self,
        short_term: ShortTermMemory,
        prompt_builder: SystemPromptBuilder,
        inbox: InboxWatcher,
        *,
        threshold: float = BUDGET_THRESHOLD,
    ) -> None:
        if not 0 < threshold < 1:
            raise ValueError("threshold must be between 0 and 1")
        self.snapshot: ContextBudgetSnapshot | None = None
        self._short_term = short_term
        self._prompt_builder = prompt_builder
        self._inbox = inbox
        self._threshold = threshold
        self._generation: int | None = None
        self._pending_event: str | None = None

    async def on_cycle(self) -> None:
        generation = self._short_term.compress_generation
        if generation == self._generation:
            return
        if self._generation is not None:
            # 上一段上下文的提醒数字已过期，撤回尚未投递的那条。
            self._cancel_pending()
        self._generation = generation

        snapshot = self._measure()
        self.snapshot = snapshot
        self._short_term.context_budget = snapshot
        if not snapshot.over_budget:
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
                    threshold=f"{snapshot.threshold * 100:.1f}",
                ),
                source="context_budget",
                timestamp=datetime.now(),
            ),
            wake=False,
        )
        logger.info(
            f"Context budget reminder: model-authored ~{snapshot.model_authored_tokens} tokens "
            f"({snapshot.ratio:.1%} > {snapshot.threshold:.0%} of {snapshot.window_tokens})"
        )

    def _measure(self) -> ContextBudgetSnapshot:
        model_pins = sum(
            estimate_text_tokens(item.content)
            for item in self._short_term.pinned_items
            if not item.system_managed
        )
        return ContextBudgetSnapshot(
            window_tokens=self._short_term.max_tokens,
            model_pin_tokens=model_pins,
            thinking_tokens=self._prompt_builder.thinking_tokens(),
            threshold=self._threshold,
        )

    def _cancel_pending(self) -> None:
        event_id = self._pending_event
        self._pending_event = None
        if event_id is not None:
            self._inbox.cancel(event_id)
