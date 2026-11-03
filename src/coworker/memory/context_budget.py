"""上下文预算：计量模型自己写进上下文的部分，超过上限时提醒。

模型能自己写、又没有硬上限的只有两处：pin 与 ``data/thinking.md``。脊柱有结构性 cap，
pin 有 5k/15k 写入闸门，但都没有"你正在吃掉多少窗口"的反馈。这里把这两项算成占
``MEMORY__SHORT_TERM_MAX_TOKENS`` 的比例，**每次上下文重建后**（启动首轮、以及每次压缩
后的首轮）检查一次，超过上限就通过 inbound 提醒。

两项共用同一份预算：pin 多占，thinking.md 的余量就少。所以上限设在 pin 上限之上，
越过上限意味着"两者都占了不少"，pin 单靠自己到不了。
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

# 模型自写部分占窗口的上限。预算按「pin 15k + thinking 10k = 25k」定，120k 窗口下约 0.21：
# pin 满仓也只占一半多，仍给 thinking.md 留出 10k；两者同时写满才刚够到上限。
BUDGET_THRESHOLD = 0.21


@dataclass(frozen=True)
class ContextBudgetSnapshot:
    """一次采样：``model_authored_tokens`` 与上限比较，其余字段只用于展示。"""

    window_tokens: int
    model_pin_tokens: int
    thinking_tokens: int
    spine_tokens: int = 0
    system_pin_tokens: int = 0
    prefix_tokens: int = 0
    threshold: float = BUDGET_THRESHOLD

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

    @property
    def over_budget(self) -> bool:
        return self.ratio >= self.threshold

    def to_dict(self) -> dict[str, int | float | bool]:
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
            "threshold": self.threshold,
            "over_budget": self.over_budget,
        }


class ContextBudgetMonitor:
    """每个 cycle 调一次 ``on_cycle``；只在 ``compress_generation`` 变化后检查并提醒。"""

    def __init__(
        self,
        short_term: ShortTermMemory,
        prompt_builder: SystemPromptBuilder,
        tools: ToolRegistry,
        inbox: InboxWatcher,
        *,
        threshold: float = BUDGET_THRESHOLD,
    ) -> None:
        if not 0 < threshold < 1:
            raise ValueError("threshold must be between 0 and 1")
        self.snapshot: ContextBudgetSnapshot | None = None
        self._short_term = short_term
        self._prompt_builder = prompt_builder
        self._tools = tools
        self._inbox = inbox
        self._threshold = threshold
        self._generation: int | None = None
        self._pending_event: str | None = None

    async def on_cycle(self, system_prompt: str) -> None:
        generation = self._short_term.compress_generation
        if generation == self._generation:
            return
        if self._generation is not None:
            # 上一段上下文的提醒数字已过期，撤回尚未投递的那条。
            self._cancel_pending()
        self._generation = generation

        snapshot = self._measure(system_prompt)
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
            threshold=self._threshold,
        )

    def _cancel_pending(self) -> None:
        event_id = self._pending_event
        self._pending_event = None
        if event_id is not None:
            self._inbox.cancel(event_id)
