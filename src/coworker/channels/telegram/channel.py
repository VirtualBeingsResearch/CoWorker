from __future__ import annotations

from dataclasses import replace

from loguru import logger

from coworker.channels.access import ChannelAccessController
from coworker.channels.base import (
    BaseChannel,
    ChannelCapabilities,
    ConnectionInfo,
    InboundHandler,
)
from coworker.channels.progress import ProgressTransport, partial_delivery_note
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

    def capabilities_for(self, participant_id: str) -> ChannelCapabilities:
        """Offer placeholders in private chats only.

        A group message cannot be overwritten in place without deciding which
        member it belongs to, so those chats keep the normal send path.
        """
        if self._is_private_chat(participant_id):
            return self._capabilities
        return replace(self._capabilities, progress=False)

    def _is_private_chat(self, participant_id: str) -> bool:
        contact = self._runner.contact_for(participant_id)
        if contact is not None:
            return contact.kind == "private"
        # A chat's first update is published before its contact is recorded, so
        # fall back to Telegram's ID convention: users are positive, groups,
        # supergroups and channels are negative.
        try:
            _, chat_id = parse_participant(participant_id)
        except ValueError:
            return False
        return chat_id > 0

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
        # 与 send() 的空消息判定保持一致：纯空白不算正文，拆分函数不会替我们过滤。
        chunks = split_telegram_text(request.message) if request.message.strip() else []
        if not chunks:
            # 没有正文就没什么可覆盖的：撤掉占位，再按普通发送的语义回报。
            await self._discard_placeholder(request.participant_id, transport)
            if not request.attachments:
                return ToolResult(
                    tool_call_id="",
                    content=tr("tool_result.communicate.message_empty"),
                    is_error=True,
                )
            await self._runner.send(
                request.participant_id,
                "",
                request.attachments,
                request.conversation_id,
            )
            return self._sent(request)
        try:
            await self._runner.edit_progress_message(
                request.participant_id,
                transport.telegram_chat_id,
                transport.telegram_message_id,
                chunks[0],
            )
        except Exception as error:
            # 正文没能写进占位，用户什么也没收到：撤掉占位后交给协调器降级为普通发送。
            await self._discard_placeholder(request.participant_id, transport)
            return ToolResult(
                tool_call_id="",
                content=tr("tool_result.communicate.telegram_failed", error=error),
                is_error=True,
            )
        delivery = await self._runner.send_progress_tail(
            request.participant_id,
            transport.telegram_chat_id,
            request.conversation_id,
            chunks[1:],
            request.attachments,
        )
        if not delivery.complete:
            # 正文开头已经在原占位消息里：删掉它等于撤回已送达的回复，整条重发又会
            # 重复。如实报告送到了哪一块、从哪一段起没送到，让模型只补发缺的部分。
            return self._sent(
                request,
                content=partial_delivery_note(
                    sent_key="tool_result.communicate.telegram_sent_partial",
                    participant_id=request.participant_id,
                    chunks=chunks,
                    attachments=request.attachments,
                    delivery=delivery,
                ),
            )
        return self._sent(request)

    def _sent(self, request: CommunicateRequest, *, content: str | None = None) -> ToolResult:
        return ToolResult(
            tool_call_id="",
            content=content
            or tr(
                "tool_result.communicate.telegram_sent",
                participant=request.participant_id,
            ),
        )

    async def _discard_placeholder(
        self,
        participant_id: str,
        transport: ProgressTransport,
    ) -> None:
        if transport.telegram_chat_id is None or transport.telegram_message_id is None:
            return
        try:
            await self._runner.delete_progress_message(
                participant_id,
                transport.telegram_chat_id,
                transport.telegram_message_id,
            )
        except Exception as error:
            logger.warning(f"telegram placeholder cleanup failed participant={participant_id}: {error}")

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
