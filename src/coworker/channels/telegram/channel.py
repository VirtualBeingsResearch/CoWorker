from __future__ import annotations

from coworker.channels.access import ChannelAccessController
from coworker.channels.base import (
    BaseChannel,
    ChannelCapabilities,
    ConnectionInfo,
    InboundHandler,
)
from coworker.channels.telegram.runner import TelegramRunner
from coworker.core.types import CommunicateRequest, ToolResult
from coworker.i18n import tr


class TelegramChannel(BaseChannel):
    """Multi-Bot Telegram channel using compact ``tg:`` participant IDs."""

    name = "telegram"
    participant_prefix = "tg:"
    requires_known_participant = True

    def __init__(self, runner: TelegramRunner) -> None:
        super().__init__(
            runtime=runner,
            capabilities=ChannelCapabilities(
                conversation_id=True,
                attachments=True,
            ),
        )
        self._runner = runner

    def resolve(self, participant_id: str) -> str | None:
        return self._runner.resolve_participant(participant_id)

    async def send(self, request: CommunicateRequest) -> ToolResult:
        if not request.message.strip() and not request.attachments:
            return ToolResult(
                tool_call_id="",
                content=tr("tool_result.communicate.message_empty"),
                is_error=True,
            )
        try:
            await self._runner.send(
                request.participant_id,
                request.message,
                request.attachments,
                request.conversation_id,
            )
        except Exception as error:
            return ToolResult(
                tool_call_id="",
                content=tr("tool_result.communicate.telegram_failed", error=error),
                is_error=True,
            )
        return ToolResult(
            tool_call_id="",
            content=tr(
                "tool_result.communicate.telegram_sent",
                participant=request.participant_id,
            ),
        )

    def set_inbound_handler(self, handler: InboundHandler | None) -> None:
        super().set_inbound_handler(handler)
        self._runner.set_inbound_handler(
            self.publish_inbound if handler is not None else None
        )

    def set_access_controller(self, access: ChannelAccessController) -> None:
        super().set_access_controller(access)
        self._runner.set_access_controller(access)

    def list_connections(self) -> list[ConnectionInfo]:
        connections: list[ConnectionInfo] = []
        for instance_id, contact, active in self._runner.contacts():
            participant_id = contact.participant_id(instance_id)
            sent_at, received_at = self._runner.activity_for(participant_id)
            connections.append(
                ConnectionInfo(
                    participant_id=participant_id,
                    channel=self.name,
                    kind=f"telegram:{contact.kind}",
                    display_name=contact.display_name,
                    active=active,
                    last_sent_at=sent_at,
                    last_received_at=received_at,
                )
            )
        return connections

    def agent_instructions(self) -> str:
        return tr("prompt.channel.telegram")
