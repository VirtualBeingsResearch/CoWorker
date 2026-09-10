from __future__ import annotations

from coworker.channels.access import ChannelAccessController
from coworker.channels.base import (
    BaseChannel,
    ChannelCapabilities,
    ConnectionInfo,
    InboundHandler,
)
from coworker.channels.progress import ProgressTransport
from coworker.channels.telegram.adapter import parse_participant
from coworker.channels.telegram.runner import TelegramRunner, split_telegram_text
from coworker.core.types import CommunicateRequest, IncomingEvent, ToolResult
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
                progress=True,
            ),
        )
        self._runner = runner

    def resolve(self, participant_id: str) -> str | None:
        return self._runner.resolve_participant(participant_id)

    def progress_is_group(self, event: IncomingEvent) -> bool:
        try:
            _, chat_id = parse_participant(event.participant_id)
        except ValueError:
            return False
        if event.speaker_id is None:
            return False
        try:
            return int(event.speaker_id) != chat_id
        except ValueError:
            return True

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

    async def open_progress(
        self,
        event: IncomingEvent,
        text: str,
    ) -> ProgressTransport | None:
        chat_id, message_id, _thread_id = await self._runner.send_progress_message(
            event.participant_id,
            text,
            event.conversation_id,
        )
        return ProgressTransport(
            channel=self.name,
            participant_id=event.participant_id,
            telegram_chat_id=chat_id,
            telegram_message_id=message_id,
        )

    async def overwrite_progress(
        self,
        transport: ProgressTransport,
        request: CommunicateRequest,
    ) -> ToolResult:
        if transport.telegram_chat_id is None or transport.telegram_message_id is None:
            return await self.send(request)
        chunks = split_telegram_text(request.message)
        if not chunks and not request.attachments:
            await self._runner.delete_progress_message(
                request.participant_id,
                transport.telegram_chat_id,
                transport.telegram_message_id,
            )
            return ToolResult(
                tool_call_id="",
                content=tr(
                    "tool_result.communicate.telegram_sent",
                    participant=request.participant_id,
                ),
            )
        if not chunks:
            await self._runner.delete_progress_message(
                request.participant_id,
                transport.telegram_chat_id,
                transport.telegram_message_id,
            )
            await self._runner.send(
                request.participant_id,
                "",
                request.attachments,
                request.conversation_id,
            )
            return ToolResult(
                tool_call_id="",
                content=tr(
                    "tool_result.communicate.telegram_sent",
                    participant=request.participant_id,
                ),
            )
        try:
            await self._runner.edit_progress_message(
                request.participant_id,
                transport.telegram_chat_id,
                transport.telegram_message_id,
                chunks[0],
                request.conversation_id,
                chunks[1:],
                request.attachments,
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

    async def close_progress(self, transport: ProgressTransport, text: str) -> None:
        if transport.telegram_chat_id is None or transport.telegram_message_id is None:
            return
        if text:
            try:
                await self._runner.edit_progress_message(
                    transport.participant_id,
                    transport.telegram_chat_id,
                    transport.telegram_message_id,
                    text,
                    None,
                    [],
                    [],
                )
                return
            except Exception:
                pass
        await self._runner.delete_progress_message(
            transport.participant_id,
            transport.telegram_chat_id,
            transport.telegram_message_id,
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
