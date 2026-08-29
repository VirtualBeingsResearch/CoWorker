"""OpenAI-compatible model API channel.

Participants use the ``api:`` prefix. One HTTP request opens one conversation
turn; the agent's ``communicate`` messages stream into that request, and the
message carrying ``extra={"end_turn": true}`` (or a ``tool_calls`` reply, or
the lifecycle watchdog) closes it.
"""

from __future__ import annotations

from coworker.channels.base import BaseChannel, ChannelCapabilities
from coworker.channels.inbound import InboundEnvelope
from coworker.channels.modelapi.runtime import ModelApiRuntime
from coworker.channels.modelapi.turns import TurnItem, TurnStream
from coworker.channels.runtime import ChannelRuntime
from coworker.core.types import CommunicateRequest, IncomingEvent, ToolResult
from coworker.i18n import tr


class ModelApiChannel(BaseChannel):
    """Deliver agent replies into open model-API turns."""

    name = "model-api"
    participant_prefix = "api:"
    requires_known_participant = False

    def __init__(
        self,
        api_runtime: ModelApiRuntime,
        *,
        watchdog: ChannelRuntime | None = None,
    ) -> None:
        super().__init__(
            runtime=watchdog,
            capabilities=ChannelCapabilities(conversation_id=True, extra=True),
        )
        self._api_runtime = api_runtime

    @property
    def api_runtime(self) -> ModelApiRuntime:
        return self._api_runtime

    @property
    def turns(self):
        return self._api_runtime.turns

    async def receive_raw(self, envelope: InboundEnvelope) -> None:
        payload = envelope.payload if isinstance(envelope.payload, dict) else {}
        await self.publish_inbound(
            IncomingEvent(
                participant_id=envelope.participant_id,
                content=str(payload.get("content") or ""),
                conversation_id=(
                    payload.get("conversation_id")
                    if isinstance(payload.get("conversation_id"), str)
                    else None
                ),
                source=envelope.source,
            )
        )

    async def send(self, request: CommunicateRequest) -> ToolResult:
        extra = request.extra if isinstance(request.extra, dict) else {}
        tool_calls = extra.get("tool_calls")
        if tool_calls is not None and not _valid_tool_calls(tool_calls):
            return ToolResult(
                tool_call_id="",
                content=tr("tool_result.model_api.invalid_tool_calls"),
                is_error=True,
            )
        end_turn = extra.get("end_turn") is True
        turns = self._api_runtime.turns
        turn = turns.get(request.participant_id, request.conversation_id)
        if turn is None:
            return ToolResult(
                tool_call_id="",
                content=tr(
                    "tool_result.model_api.not_delivered",
                    participant=request.participant_id,
                ),
            )
        turn.publish(
            TurnItem(
                kind="message",
                text=request.message,
                tool_calls=tool_calls if isinstance(tool_calls, list) else None,
            )
        )
        self._record_sent(request.participant_id)
        if tool_calls:
            turns.close(turn, "tool_calls")
            return ToolResult(
                tool_call_id="",
                content=tr(
                    "tool_result.model_api.tool_calls_sent",
                    participant=request.participant_id,
                    conversation=turn.conversation_id,
                ),
            )
        if end_turn:
            turns.close(turn, "end_turn")
            return ToolResult(
                tool_call_id="",
                content=tr(
                    "tool_result.model_api.final_sent",
                    participant=request.participant_id,
                    conversation=turn.conversation_id,
                ),
            )
        return ToolResult(
            tool_call_id="",
            content=tr(
                "tool_result.model_api.report_sent",
                participant=request.participant_id,
                conversation=turn.conversation_id,
            ),
        )

    def agent_instructions(self) -> str:
        return tr("prompt.channel.model_api")

    async def nudge_turn(self, turn: TurnStream) -> None:
        """Remind the agent that a client is still waiting on an open turn."""
        await self.publish_inbound(
            IncomingEvent(
                participant_id="system",
                content=tr(
                    "channel.model_api.nudge",
                    participant=turn.participant_id,
                    conversation=turn.conversation_id,
                ),
                source="system",
            )
        )

    async def timeout_turn(self, turn: TurnStream) -> None:
        """Tell the agent that a timed-out turn's HTTP response has been closed."""
        await self.publish_inbound(
            IncomingEvent(
                participant_id="system",
                content=tr(
                    "channel.model_api.timeout",
                    participant=turn.participant_id,
                    conversation=turn.conversation_id,
                ),
                source="system",
            )
        )


def _valid_tool_calls(value: object) -> bool:
    if not isinstance(value, list):
        return False
    for entry in value:
        if not isinstance(entry, dict):
            return False
        function = entry.get("function")
        if not isinstance(function, dict) or not str(function.get("name") or "").strip():
            return False
    return True
