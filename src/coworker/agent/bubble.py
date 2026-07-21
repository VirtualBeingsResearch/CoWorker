from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Literal

from coworker.i18n import tr

if TYPE_CHECKING:
    from coworker.brain.brain import Brain
    from coworker.core.types import Message, PinnedItem
    from coworker.memory.memory_tree import MemoryBlockTree


BubbleStatus = Literal["running", "done", "error", "cancelled", "timeout"]


@dataclass
class Bubble:
    id: str
    goal: str
    provider: str = ""
    model: str = ""
    status: BubbleStatus = "running"
    forked_context: list[Message] = field(default_factory=list)
    forked_tree: MemoryBlockTree | None = field(default=None, repr=False)
    inner_messages: list[Message] = field(default_factory=list)
    result: str = ""
    error: str = ""
    max_cycles: int = 10
    cycles_used: int = 0
    # 该泡泡服务的对象 id（用于续接路由；空表示非特定对象）。
    participant_id: str = ""
    # 可选的会话绑定。与 participant_id 一起用于把后续通信精确交给该泡泡。
    conversation_id: str = ""
    # 对已显式配置的通信对象，外部可见地标识泡泡接管、回复和结束。
    handoff_transparency: bool = False
    # 挂载的宫殿名列表（续接路由时按宫殿/participant/目标配对）。
    palaces: list[str] = field(default_factory=list)
    # memory_tags 并集(来自挂载的宫殿),收尾时用于把结论按标签写回长期记忆。
    palace_tags: list[str] = field(default_factory=list)
    # 宫殿注入摘要(供泡泡日志记录:挂了哪些宫殿、强加载哪些 skill、召回了哪些记忆)。
    # 在 bubble_spawn 注入时填充,泡泡启动写日志时消费;None 表示未挂宫殿。
    palace_injection: dict | None = field(default=None, repr=False)
    created_at: datetime = field(default_factory=datetime.now)
    finished_at: datetime | None = None
    partial_results: list[str] = field(default_factory=list)
    checkpoint_count: int = 0
    initial_max_cycles: int = 0
    # 已经从超时状态恢复并继续执行的次数。恢复仍复用同一 bubble id 与上下文。
    resume_count: int = 0
    last_resumed_at: datetime | None = None
    # 泡泡自己的 pinned context；超时后续跑时需要一并恢复，不能只保留消息列表。
    pinned_items: list[PinnedItem] = field(default_factory=list, repr=False)
    inbox: asyncio.Queue = field(default_factory=asyncio.Queue, repr=False)
    task: asyncio.Task | None = field(default=None, repr=False)
    brain: Brain | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        if self.initial_max_cycles <= 0:
            self.initial_max_cycles = self.max_cycles

    def is_terminal(self) -> bool:
        return self.status in ("done", "error", "cancelled", "timeout")

    def elapsed_seconds(self) -> float:
        if self.finished_at:
            return (self.finished_at - self.created_at).total_seconds()
        return (datetime.now() - self.created_at).total_seconds()


class BubbleStore:
    _MAX_HISTORY = 20

    def __init__(
        self,
        max_concurrent: int = 5,
        timeout_resume_seconds: int = 300,
    ) -> None:
        self._active: dict[str, Bubble] = {}
        self._history: list[Bubble] = []
        self.max_concurrent = max_concurrent
        # 0 表示禁用超时泡泡续跑；负数同样按禁用处理，避免异常配置放开限制。
        self.timeout_resume_seconds = max(0, timeout_resume_seconds)

    def create(
        self,
        goal: str,
        forked_context: list[Message],
        max_cycles: int,
        provider: str = "",
        model: str = "",
    ) -> Bubble | str:
        if len(self._active) >= self.max_concurrent:
            active_ids = ", ".join(self._active.keys())
            return tr(
                "tool_result.bubble.create_concurrency",
                max=self.max_concurrent,
                active=active_ids,
            )
        ts = datetime.now().strftime("%y%m%d%H%M%S")
        bubble_id = f"bbl_{ts}"
        n = 2
        while bubble_id in self._active or any(b.id == bubble_id for b in self._history):
            bubble_id = f"bbl_{ts}_{n}"
            n += 1
        bubble = Bubble(
            id=bubble_id,
            goal=goal,
            provider=provider,
            model=model,
            forked_context=list(forked_context),
            max_cycles=max_cycles,
        )
        self._active[bubble_id] = bubble
        return bubble

    def get(self, bubble_id: str) -> Bubble | None:
        return self._active.get(bubble_id) or next(
            (b for b in self._history if b.id == bubble_id), None
        )

    def list_active(self) -> list[Bubble]:
        return list(self._active.values())

    def find_active_for_message(
        self,
        participant_id: str,
        conversation_id: str | None = None,
    ) -> Bubble | None:
        """Return the unambiguous active bubble bound to an inbound message.

        A conversation-specific binding wins over a participant-only binding.
        If two bubbles could receive the same message, deliberately return None:
        sending it to the main loop is safer than silently handing it to the
        wrong task.
        """
        candidates = [
            bubble
            for bubble in self._active.values()
            if not bubble.is_terminal() and bubble.participant_id == participant_id
        ]
        if not candidates:
            return None

        if conversation_id:
            exact = [bubble for bubble in candidates if bubble.conversation_id == conversation_id]
            if len(exact) == 1:
                return exact[0]
            if len(exact) > 1:
                return None

        participant_only = [bubble for bubble in candidates if not bubble.conversation_id]
        return participant_only[0] if len(participant_only) == 1 else None

    def mark_done(self, bubble: Bubble) -> None:
        bubble.finished_at = datetime.now()
        self._active.pop(bubble.id, None)
        self._history.append(bubble)
        if len(self._history) > self._MAX_HISTORY:
            self._history = self._history[-self._MAX_HISTORY :]

    def resume(
        self,
        bubble_id: str,
        *,
        additional_cycles: int,
        max_cycles_cap: int,
    ) -> Bubble | str:
        """Reactivate a recently timed-out bubble with an expanded cumulative budget.

        The caller starts the new mini-loop only after this method succeeds.  Keeping
        the state transition in the store makes the concurrent-capacity check atomic
        from the event loop's perspective and prevents two callers from resuming the
        same bubble at once.
        """
        bubble = self.get(bubble_id)
        if bubble is None:
            return tr("tool_result.bubble.missing", id=bubble_id)
        if bubble.status != "timeout":
            return tr(
                "tool_result.bubble.resume_wrong_status",
                id=bubble_id,
                status=bubble.status,
            )
        if bubble.id in self._active:
            return tr("tool_result.bubble.resume_ending_cleanup", id=bubble_id)
        if bubble.task is not None and not bubble.task.done():
            return tr("tool_result.bubble.resume_still_cleanup", id=bubble_id)
        if bubble.finished_at is None:
            return tr("tool_result.bubble.resume_missing_finished_at", id=bubble_id)
        if self.timeout_resume_seconds <= 0:
            return tr("tool_result.bubble.resume_disabled")

        now = datetime.now()
        elapsed = max(0.0, (now - bubble.finished_at).total_seconds())
        if elapsed > self.timeout_resume_seconds:
            return tr(
                "tool_result.bubble.resume_expired",
                id=bubble_id,
                elapsed=f"{elapsed:.0f}",
                window=self.timeout_resume_seconds,
            )
        if len(self._active) >= self.max_concurrent:
            active_ids = ", ".join(self._active.keys())
            return tr(
                "tool_result.bubble.resume_concurrency",
                max=self.max_concurrent,
                active=active_ids,
            )

        requested_cycles = max(1, additional_cycles)
        next_max_cycles = min(max_cycles_cap, bubble.max_cycles + requested_cycles)
        if next_max_cycles <= bubble.cycles_used:
            return tr("tool_result.bubble.resume_cycle_cap", id=bubble_id, cap=max_cycles_cap)

        self._history = [item for item in self._history if item is not bubble]
        self._active[bubble.id] = bubble
        bubble.status = "running"
        bubble.error = ""
        bubble.finished_at = None
        bubble.max_cycles = next_max_cycles
        bubble.resume_count += 1
        bubble.last_resumed_at = now
        return bubble

    def cancel_all(self) -> None:
        for bubble in list(self._active.values()):
            if bubble.task and not bubble.task.done():
                bubble.task.cancel()
