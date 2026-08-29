"""Open model-API turns: reply fan-out to attached responses, lifecycle watchdog.

One HTTP request opens one turn on a conversation. The agent's ``communicate``
messages stream to every attached response; the reply marked ``end_turn`` (or a
``tool_calls`` reply, or a lifecycle timeout) closes the turn and every attached
response with it.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Literal

from loguru import logger

TurnEndReason = Literal["end_turn", "tool_calls", "timeout", "shutdown"]


@dataclass
class TurnItem:
    """One outbound event on a turn's fan-out stream."""

    kind: Literal["message", "close"]
    text: str = ""
    tool_calls: list[dict[str, Any]] | None = None
    end_reason: TurnEndReason | Literal[""] = ""
    usage: dict[str, int] = field(default_factory=dict)


class TurnStream:
    """One open conversation turn fanned out to all attached HTTP responses."""

    def __init__(self, participant_id: str, conversation_id: str) -> None:
        self.participant_id = participant_id
        self.conversation_id = conversation_id
        self.subscribers: list[asyncio.Queue[TurnItem]] = []
        self.texts: list[str] = []
        self.tool_calls: list[dict[str, Any]] | None = None
        self.last_output_at = time.monotonic()
        self.nudged = False
        self.closed = False
        self.end_reason: TurnEndReason | Literal[""] = ""

    def attach(self) -> asyncio.Queue[TurnItem]:
        queue: asyncio.Queue[TurnItem] = asyncio.Queue()
        self.subscribers.append(queue)
        return queue

    def detach(self, queue: asyncio.Queue[TurnItem]) -> None:
        try:
            self.subscribers.remove(queue)
        except ValueError:
            pass

    def publish(self, item: TurnItem) -> None:
        if self.closed:
            return
        if item.kind == "message":
            if item.text:
                self.texts.append(item.text)
            if item.tool_calls:
                self.tool_calls = item.tool_calls
            self.last_output_at = time.monotonic()
            self.nudged = False
        for queue in list(self.subscribers):
            queue.put_nowait(item)

    def close(self, end_reason: TurnEndReason) -> None:
        if self.closed:
            return
        self.closed = True
        self.end_reason = end_reason
        for queue in list(self.subscribers):
            queue.put_nowait(TurnItem(kind="close", end_reason=end_reason))


class TurnRegistry:
    """Open turns keyed by ``(participant_id, conversation_id)``."""

    def __init__(
        self,
        *,
        nudge_seconds: float = 300.0,
        timeout_seconds: float = 1200.0,
    ) -> None:
        self._turns: dict[tuple[str, str], TurnStream] = {}
        self.nudge_seconds = nudge_seconds
        self.timeout_seconds = timeout_seconds
        self.on_nudge: Callable[[TurnStream], Awaitable[None]] | None = None
        self.on_timeout: Callable[[TurnStream], Awaitable[None]] | None = None

    def open_or_get(self, participant_id: str, conversation_id: str) -> TurnStream:
        key = (participant_id, conversation_id)
        turn = self._turns.get(key)
        if turn is None or turn.closed:
            turn = TurnStream(participant_id, conversation_id)
            self._turns[key] = turn
        return turn

    def get(self, participant_id: str, conversation_id: str | None) -> TurnStream | None:
        if conversation_id:
            turn = self._turns.get((participant_id, conversation_id))
            return turn if turn is not None and not turn.closed else None
        open_turns = [
            turn
            for (participant, _), turn in self._turns.items()
            if participant == participant_id and not turn.closed
        ]
        return open_turns[0] if len(open_turns) == 1 else None

    def close(self, turn: TurnStream, end_reason: TurnEndReason) -> None:
        turn.close(end_reason)
        self._turns.pop((turn.participant_id, turn.conversation_id), None)

    def close_all(self, end_reason: TurnEndReason = "shutdown") -> None:
        for turn in list(self._turns.values()):
            turn.close(end_reason)
        self._turns.clear()

    async def run_watchdog(self, *, interval: float = 15.0) -> None:
        """Nudge turns that stopped producing output; close them at the timeout."""
        while True:
            await asyncio.sleep(interval)
            now = time.monotonic()
            for turn in list(self._turns.values()):
                if turn.closed:
                    continue
                idle = now - turn.last_output_at
                if idle >= self.timeout_seconds:
                    await self._notify(self.on_timeout, turn)
                    self.close(turn, "timeout")
                elif idle >= self.nudge_seconds and not turn.nudged:
                    turn.nudged = True
                    await self._notify(self.on_nudge, turn)

    async def _notify(
        self,
        callback: Callable[[TurnStream], Awaitable[None]] | None,
        turn: TurnStream,
    ) -> None:
        if callback is None:
            return
        try:
            await callback(turn)
        except Exception as error:
            logger.warning(f"model API turn callback failed: {error}")


class TurnWatchdogRuntime:
    """``ChannelRuntime`` that runs the turn watchdog with the channel system."""

    name = "model-api-turns"

    def __init__(self, turns: TurnRegistry) -> None:
        self._turns = turns
        self._task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(
                self._turns.run_watchdog(), name="model-api-turn-watchdog"
            )

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        self._turns.close_all("shutdown")
