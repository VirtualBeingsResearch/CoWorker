"""多会话并发高峰检测：窗口内未接管会话达到阈值时提示主线并用泡泡并行处理。"""

from __future__ import annotations

import time
from collections import deque
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import TYPE_CHECKING

from coworker.core.types import IncomingEvent

if TYPE_CHECKING:
    from coworker.agent.bubble import BubbleStore

# 滑动窗口时长：窗口内出现过来信的会话视为「同时活跃」。
_WINDOW_SECONDS = 180.0
# 窗口内未接管会话数达到该值时触发提示。
_THRESHOLD = 2
# 两次提示之间的最小间隔，避免对同一波高峰反复刷屏。
_COOLDOWN_SECONDS = 600.0

# 内部生产者的事件不代表外部会话，不参与计数；新增内部来源需要在此登记。
# 信道来源（wecom/weixin/telegram/openai/stream 等）默认全部计数。
_INTERNAL_SOURCES = frozenset(
    {
        "system",
        "system_recovery",
        "alarm",
        "task_reminder",
        "bubble",
        "code_job",
        "file",
        "sleep_interrupt",
        "compress_memory",
        "tick",
    }
)


@dataclass
class ConcurrencyHint:
    count: int


class ConcurrencyHintTracker:
    """按 (participant_id, conversation_id) 统计滑动窗口内的活跃会话。

    提示只在会话数从阈值下方上穿时触发，且受冷却与泡泡满员约束；已被活跃
    泡泡接管的会话不计入，因此搭档按提示绑定泡泡后提示条件会自然消失。
    """

    def __init__(
        self,
        window_seconds: float = _WINDOW_SECONDS,
        threshold: int = _THRESHOLD,
        cooldown_seconds: float = _COOLDOWN_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._window_seconds = window_seconds
        self._threshold = threshold
        self._cooldown_seconds = cooldown_seconds
        self._clock = clock
        self._events: deque[tuple[float, tuple[str, str]]] = deque()
        self._last_count = 0
        self._last_hint_at: float | None = None

    def configure(
        self,
        *,
        window_seconds: float,
        threshold: int,
        cooldown_seconds: float,
    ) -> None:
        """管理端热更新检测参数；保留窗口内已记录的事件。"""
        self._window_seconds = window_seconds
        self._threshold = threshold
        self._cooldown_seconds = cooldown_seconds

    def observe(
        self,
        events: Iterable[IncomingEvent],
        bubble_store: BubbleStore | None = None,
    ) -> ConcurrencyHint | None:
        """记录一批事件并判断当前是否应该注入并发提示。"""
        now = self._clock()
        for event in events:
            if event.source in _INTERNAL_SOURCES:
                continue
            self._events.append((now, (event.participant_id, event.conversation_id or "")))
        cutoff = now - self._window_seconds
        while self._events and self._events[0][0] < cutoff:
            self._events.popleft()

        conversations = {key for _, key in self._events}
        if bubble_store is not None:
            conversations = {
                key
                for key in conversations
                if bubble_store.find_active_for_message(key[0], key[1] or None) is None
            }

        count = len(conversations)
        rising_edge = count >= self._threshold and self._last_count < self._threshold
        self._last_count = count
        if not rising_edge:
            return None
        if self._last_hint_at is not None and now - self._last_hint_at < self._cooldown_seconds:
            return None
        if (
            bubble_store is not None
            and len(bubble_store.list_active()) >= bubble_store.max_concurrent
        ):
            # 满员时提示只会诱导注定失败的 spawn；重置边沿，等有空位后重新评估。
            self._last_count = 0
            return None
        self._last_hint_at = now
        return ConcurrencyHint(count=count)
