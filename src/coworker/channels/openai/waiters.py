"""HTTP completion waiters for the OpenAI-compatible channel."""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from typing import Any, Literal

from coworker.core.ids import new_compact_id
from coworker.i18n import tr


@dataclass(frozen=True)
class ClientToolCall:
    id: str
    name: str
    arguments: str


@dataclass(frozen=True)
class OpenAICompletion:
    kind: Literal["stop", "tool_calls"]
    content: str | None = None
    tool_calls: tuple[ClientToolCall, ...] = ()
    timed_out: bool = False


@dataclass
class _PendingClientCall:
    name: str
    arguments: str
    openai_id: str
    result: asyncio.Future[str]


class OpenAITurn:
    """One held HTTP request waiting for communicate or client-tool calls."""

    def __init__(
        self,
        *,
        participant_id: str,
        conversation_id: str,
        catalog: dict[str, dict[str, Any]],
        timeout_seconds: float,
    ) -> None:
        self.participant_id = participant_id
        self.conversation_id = conversation_id
        self.catalog = catalog
        self.timeout_seconds = timeout_seconds
        self.completion: asyncio.Future[OpenAICompletion] = asyncio.get_running_loop().create_future()
        self.expected_client_calls = 0
        self._pending: list[_PendingClientCall] = []
        self._closed = False

    def prepare_client_calls(self, count: int) -> None:
        self.expected_client_calls = count

    def register_client_call(self, name: str, arguments: dict[str, Any]) -> _PendingClientCall:
        if name not in self.catalog:
            raise ValueError(tr("tool_result.client_tool.unknown_name", name=name))
        pending = _PendingClientCall(
            name=name,
            arguments=json.dumps(arguments, ensure_ascii=False, separators=(",", ":")),
            openai_id=f"call_{new_compact_id()}",
            result=asyncio.get_running_loop().create_future(),
        )
        self._pending.append(pending)
        if (
            self.expected_client_calls > 0
            and len(self._pending) >= self.expected_client_calls
        ):
            self._flush_tool_calls()
        return pending

    def flush_tool_calls(self) -> None:
        self._flush_tool_calls()

    def _flush_tool_calls(self) -> None:
        if self.completion.done():
            return
        self.completion.set_result(
            OpenAICompletion(
                kind="tool_calls",
                tool_calls=tuple(
                    ClientToolCall(
                        id=item.openai_id,
                        name=item.name,
                        arguments=item.arguments,
                    )
                    for item in self._pending
                ),
            )
        )

    def deliver_tool_results(self, results: dict[str, str]) -> None:
        known = {item.openai_id: item for item in self._pending}
        missing = [call_id for call_id in known if call_id not in results]
        if missing:
            raise ValueError(
                tr("api.openai.tool_results_incomplete", ids=", ".join(missing))
            )
        extra = [call_id for call_id in results if call_id not in known]
        if extra:
            raise ValueError(
                tr("api.openai.tool_results_unknown", ids=", ".join(extra))
            )
        for call_id, content in results.items():
            future = known[call_id].result
            if not future.done():
                future.set_result(content)

    def fulfill_stop(self, message: str) -> bool:
        if self._closed or self.completion.done():
            return False
        self.completion.set_result(OpenAICompletion(kind="stop", content=message))
        return True

    def expire(self) -> None:
        self._closed = True
        if not self.completion.done():
            self.completion.set_result(
                OpenAICompletion(kind="stop", content="", timed_out=True)
            )
        for item in self._pending:
            if not item.result.done():
                item.result.set_exception(
                    TimeoutError(tr("tool_result.client_tool.timeout"))
                )

    @property
    def awaiting_client(self) -> bool:
        return bool(self._pending) and self.completion.done() and all(
            not item.result.done() for item in self._pending
        )

    @property
    def in_flight(self) -> bool:
        return not self.completion.done()


class OpenAISessionTable:
    """Waiters keyed by (participant_id, conversation_id)."""

    def __init__(self) -> None:
        self._turns: dict[tuple[str, str], OpenAITurn] = {}
        self._awaiting_tools: dict[tuple[str, str], OpenAITurn] = {}

    def get_active(self, participant_id: str, conversation_id: str) -> OpenAITurn | None:
        key = (participant_id, conversation_id)
        turn = self._turns.get(key)
        if turn is not None:
            return turn
        return self._awaiting_tools.get(key)

    def begin_user_turn(self, turn: OpenAITurn) -> None:
        key = (turn.participant_id, turn.conversation_id)
        if key in self._awaiting_tools:
            raise BusyError("tools")
        current = self._turns.get(key)
        if current is not None and current.in_flight:
            raise BusyError("turn")
        self._turns[key] = turn

    def begin_tool_followup(self, turn: OpenAITurn, results: dict[str, str]) -> OpenAITurn:
        key = (turn.participant_id, turn.conversation_id)
        pending = self._awaiting_tools.get(key)
        if pending is None:
            raise ValueError(tr("api.openai.tool_followup_unexpected"))
        pending.deliver_tool_results(results)
        self._awaiting_tools.pop(key, None)
        self._turns[key] = turn
        return pending

    def mark_awaiting_tools(self, turn: OpenAITurn) -> None:
        key = (turn.participant_id, turn.conversation_id)
        if self._turns.get(key) is turn:
            self._turns.pop(key, None)
        self._awaiting_tools[key] = turn

    def discard(self, turn: OpenAITurn) -> None:
        key = (turn.participant_id, turn.conversation_id)
        if self._turns.get(key) is turn:
            self._turns.pop(key, None)
        if self._awaiting_tools.get(key) is turn:
            self._awaiting_tools.pop(key, None)

    def pending_tool_turn(self, participant_id: str, conversation_id: str) -> OpenAITurn | None:
        return self._awaiting_tools.get((participant_id, conversation_id))

    def awaiting_tools(self, participant_id: str, conversation_id: str) -> bool:
        return (participant_id, conversation_id) in self._awaiting_tools

    def take_awaiting_tools(self, participant_id: str, conversation_id: str) -> OpenAITurn | None:
        return self._awaiting_tools.pop((participant_id, conversation_id), None)

    def in_flight_for(self, participant_id: str) -> list[OpenAITurn]:
        return [
            turn
            for turn in self._turns.values()
            if turn.participant_id == participant_id and turn.in_flight
        ]


class BusyError(Exception):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason
