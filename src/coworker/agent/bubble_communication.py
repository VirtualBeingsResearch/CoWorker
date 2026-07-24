from __future__ import annotations

from typing import TYPE_CHECKING, Any

from coworker.agent.bubble_handoff import (
    BubbleHandoffNotifier,
    bubble_reply_fallback_prefix,
    bubble_reply_message_extra,
)
from coworker.core.types import ToolResult
from coworker.i18n import tr

if TYPE_CHECKING:
    from coworker.agent.bubble import Bubble
    from coworker.tools.communicate_tool import CommunicateTool


class BubbleCommunicator:
    """Apply Bubble binding and lifecycle rules to the shared communicate tool."""

    def __init__(
        self,
        delegate: CommunicateTool,
        bubble: Bubble,
        notifier: BubbleHandoffNotifier,
    ) -> None:
        self._delegate = delegate
        self._bubble = bubble
        self._notifier = notifier

    def adapt_schemas(self, schemas: list[dict[str, Any]]) -> list[dict[str, Any]]:
        adapted: list[dict[str, Any]] = []
        for schema in schemas:
            if schema.get("name") != self._delegate.definition.name:
                adapted.append(schema)
                continue
            parameters = dict(schema["parameters"])
            parameters["required"] = [
                name for name in parameters.get("required", []) if name != "participant_id"
            ]
            adapted.append({**schema, "parameters": parameters})
        return adapted

    async def send(
        self,
        participant_id: str = "",
        message: str = "",
        conversation_id: str | None = None,
        attachments: list[dict[str, Any]] | None = None,
        extra: dict[str, Any] | None = None,
        **_,
    ) -> ToolResult:
        bubble = self._bubble
        requested_participant = participant_id.strip() if isinstance(participant_id, str) else ""
        if requested_participant and requested_participant != bubble.participant_id:
            return ToolResult(
                tool_call_id="",
                content=tr("tool_result.communicate.bound_participant"),
                is_error=True,
            )
        requested_conversation = conversation_id.strip() if isinstance(conversation_id, str) else ""
        if (
            bubble.conversation_id
            and requested_conversation
            and requested_conversation != bubble.conversation_id
        ):
            return ToolResult(
                tool_call_id="",
                content=tr("tool_result.communicate.bound_conversation"),
                is_error=True,
            )
        if extra is not None and not isinstance(extra, dict):
            return ToolResult(
                tool_call_id="",
                content=tr("tool_result.communicate.extra_object"),
                is_error=True,
            )

        outgoing_message = message
        outgoing_extra = dict(extra or {})
        if bubble.handoff_transparency:
            prefix = bubble_reply_fallback_prefix(bubble.participant_id)
            if prefix:
                if outgoing_message and not outgoing_message.startswith(prefix):
                    outgoing_message = f"{prefix}{outgoing_message}"
                elif not outgoing_message and attachments:
                    outgoing_message = tr(
                        "tool_result.communicate.attachment_fallback",
                        prefix=prefix,
                    )
            provenance = bubble_reply_message_extra(bubble.id)
            if self._delegate.supports_message_extra(
                bubble.participant_id,
                provenance,
            ):
                outgoing_extra.update(provenance)
            await self._notifier.announce_started(
                bubble,
                resumed=bubble.resume_count > 0,
            )
        return await self._delegate.execute(
            participant_id=bubble.participant_id,
            message=outgoing_message,
            conversation_id=bubble.conversation_id or requested_conversation or None,
            attachments=attachments,
            extra=outgoing_extra,
        )
