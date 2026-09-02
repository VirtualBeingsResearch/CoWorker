"""Call a tool provided by the current OpenAI-compat HTTP caller."""

from __future__ import annotations

from typing import Any

from coworker.channels.openai.channel import OpenAIChannel
from coworker.core.communication_tokens import OPENAI_PREFIX
from coworker.core.types import ToolCall, ToolResult
from coworker.i18n import tr
from coworker.tools.base import Tool, ToolDefinition


class CallClientTool(Tool):
    """Proxy a caller-provided function through the OpenAI HTTP waiter."""

    def __init__(self, channel: OpenAIChannel) -> None:
        self._channel = channel

    @property
    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="call_client_tool",
            description=(
                "Call a function supplied by the current OpenAI-compatible HTTP "
                "caller. name must be in that request's catalog; copy "
                "participant_id and conversation_id from the inbound OpenAI message."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "description": "Client-provided function name",
                    },
                    "arguments": {
                        "type": "object",
                        "description": "Arguments object passed to that client function",
                    },
                    "participant_id": {
                        "type": "string",
                        "description": "OpenAI channel address such as openai:api",
                    },
                    "conversation_id": {
                        "type": "string",
                        "description": "Window ID under that address",
                    },
                },
                "required": ["name", "participant_id"],
            },
        )

    def prepare_batch(self, tool_calls: list[ToolCall]) -> None:
        client_calls = [call for call in tool_calls if call.name == "call_client_tool"]
        if not client_calls:
            return
        first = client_calls[0].arguments
        participant_id = str(first.get("participant_id") or "")
        conversation_id = str(first.get("conversation_id") or "")
        self._channel.prepare_client_tool_batch(
            participant_id,
            conversation_id,
            len(client_calls),
        )

    async def execute(
        self,
        name: str = "",
        arguments: dict[str, Any] | None = None,
        participant_id: str = "",
        conversation_id: str | None = None,
        **_: Any,
    ) -> ToolResult:
        participant = str(participant_id or "").strip()
        tool_name = str(name or "").strip()
        if not tool_name:
            return ToolResult(
                tool_call_id="",
                content=tr("tool_result.client_tool.needs_name"),
                is_error=True,
            )
        if not participant.startswith(OPENAI_PREFIX):
            return ToolResult(
                tool_call_id="",
                content=tr("tool_result.client_tool.not_openai", participant=participant),
                is_error=True,
            )
        payload = arguments if isinstance(arguments, dict) else {}
        return await self._channel.call_client_tool(
            name=tool_name,
            arguments=payload,
            participant_id=participant,
            conversation_id=str(conversation_id).strip() if conversation_id else None,
        )
