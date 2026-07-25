from __future__ import annotations

from typing import TYPE_CHECKING, Any

from coworker.agent.bubble_handoff import (
    BubbleHandoffNotifier,
    bubble_reply_fallback_prefix,
    bubble_reply_message_extra,
)
from coworker.core.types import ToolResult
from coworker.i18n import tr
from coworker.tools.communicate_tool import CommunicateTool

if TYPE_CHECKING:
    from coworker.agent.bubble import Bubble
    from coworker.channels.registry import ChannelRegistry


class BubbleCommunicateTool(CommunicateTool):
    """CommunicateTool with one Bubble's fixed target and lifecycle semantics."""

    def __init__(
        self,
        channels: ChannelRegistry,
        bubble: Bubble,
        notifier: BubbleHandoffNotifier,
    ) -> None:
        super().__init__(channels)
        self._bubble = bubble
        self._notifier = notifier

    @classmethod
    def from_tool(
        cls,
        tool: CommunicateTool,
        bubble: Bubble,
        notifier: BubbleHandoffNotifier,
    ) -> BubbleCommunicateTool:
        return cls(tool._channels, bubble, notifier)

    async def execute(
        self,
        participant_id: str = "",
        message: str = "",
        conversation_id: str | None = None,
        attachments: list[dict[str, Any]] | None = None,
        extra: dict[str, Any] | None = None,
        **_,
    ) -> ToolResult:
        bubble = self._bubble
        requested_conversation = conversation_id.strip() if isinstance(conversation_id, str) else ""
        issues = self._argument_issues(
            participant_id,
            conversation_id,
            extra,
        )
        if issues:
            return ToolResult(
                tool_call_id="",
                content=self._format_argument_issues(issues),
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
            if self.supports_message_extra(
                bubble.participant_id,
                provenance,
            ):
                outgoing_extra.update(provenance)
            await self._notifier.announce_started(
                bubble,
                resumed=bubble.resume_count > 0,
            )
        return await super().execute(
            participant_id=bubble.participant_id,
            message=outgoing_message,
            conversation_id=bubble.conversation_id or requested_conversation or None,
            attachments=attachments,
            extra=outgoing_extra,
        )

    def _argument_issues(
        self,
        participant_id: object,
        conversation_id: object,
        extra: object,
    ) -> list[str]:
        bubble = self._bubble
        issues: list[str] = []
        if (
            not isinstance(participant_id, str)
            or (
                participant_id.strip()
                and participant_id.strip() != bubble.participant_id
            )
        ):
            issues.append(tr("tool_result.communicate.bound_participant"))
        if (
            conversation_id is not None
            and (
                not isinstance(conversation_id, str)
                or (
                    bubble.conversation_id
                    and conversation_id.strip()
                    and conversation_id.strip() != bubble.conversation_id
                )
            )
        ):
            issues.append(tr("tool_result.communicate.bound_conversation"))
        if extra is not None and not isinstance(extra, dict):
            issues.append(tr("tool_result.communicate.extra_object"))
        return issues

    def _format_argument_issues(self, issues: list[str]) -> str:
        bubble = self._bubble
        conversation = (
            tr(
                "tool_result.communicate.bound_target_conversation",
                conversation=bubble.conversation_id,
            )
            if bubble.conversation_id
            else ""
        )
        return tr(
            "tool_result.communicate.bound_invalid",
            issues="\n".join(f"- {issue}" for issue in issues),
            participant=bubble.participant_id,
            conversation=conversation,
        )
