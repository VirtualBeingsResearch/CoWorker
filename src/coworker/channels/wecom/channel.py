"""WeComChannel: the WeCom transport as a Channel.

Wraps :class:`WeComRunner` (WS lifecycle, outbound send, contacts). Outbound
routing uses the runner's ``sender``/``resolve_participant``; ``list_connections`` exposes
known WeCom group chats and single-chat users (the user-requested visibility
into WeCom reachables), including the latest send and receive times.
"""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING

from coworker.channels.access import ChannelAccessController
from coworker.channels.base import (
    BaseChannel,
    ChannelCapabilities,
    ConnectionInfo,
    InboundHandler,
)
from coworker.channels.progress import ProgressTransport
from coworker.channels.wecom.adapter import parse_participant
from coworker.core.types import CommunicateRequest, IncomingEvent, ToolResult
from coworker.i18n import tr

if TYPE_CHECKING:
    from coworker.channels.wecom.runner import WeComRunner


class WeComChannel(BaseChannel):
    """WeCom outbound channel (prefix ``wecom:``)."""

    requires_known_participant = True

    def __init__(self, runner: WeComRunner) -> None:
        super().__init__(
            runtime=runner,
            capabilities=ChannelCapabilities(
                conversation_id=True,
                attachments=True,
                progress=True,
            ),
        )
        self.name = "wecom"
        self.participant_prefix = "wecom:"
        self._runner = runner

    def resolve(self, participant_id: str) -> str | None:
        return self._runner.resolve_participant(participant_id)

    def capabilities_for(self, participant_id: str) -> ChannelCapabilities:
        """Offer placeholders to one-to-one chats only.

        A group reply quotes the inbound frame through ``conversation_id``,
        which an in-place placeholder would consume before the model replies.
        """
        try:
            _, chat_type, _ = parse_participant(participant_id)
        except ValueError:
            chat_type = ""
        if chat_type == "single":
            return self._capabilities
        return replace(self._capabilities, progress=False)

    async def send(self, request: CommunicateRequest) -> ToolResult:
        try:
            await self._runner.send(
                request.participant_id,
                request.message,
                request.attachments,
                request.conversation_id,
            )
            content = tr(
                "tool_result.communicate.wecom_sent",
                participant=request.participant_id,
            )
            return ToolResult(
                tool_call_id="",
                content=content,
            )
        except Exception as error:
            return ToolResult(
                tool_call_id="",
                content=tr("tool_result.communicate.wecom_failed", error=error),
                is_error=True,
            )

    async def open_progress(
        self,
        event: IncomingEvent,
        text: str,
    ) -> ProgressTransport | None:
        payload = await self._runner.open_progress(event, text)
        if payload is None:
            return None
        return ProgressTransport(
            channel=self.name,
            participant_id=event.participant_id,
            frame=payload["frame"],
            stream_id=payload["stream_id"],
        )

    async def overwrite_progress(
        self,
        transport: ProgressTransport,
        request: CommunicateRequest,
    ) -> ToolResult:
        try:
            await self._runner.send(
                request.participant_id,
                request.message,
                request.attachments,
                request.conversation_id,
                reply_frame=transport.frame,
                reply_stream_id=transport.stream_id,
            )
            return ToolResult(
                tool_call_id="",
                content=tr(
                    "tool_result.communicate.wecom_sent",
                    participant=request.participant_id,
                ),
            )
        except Exception as error:
            return ToolResult(
                tool_call_id="",
                content=tr("tool_result.communicate.wecom_failed", error=error),
                is_error=True,
            )

    async def close_progress(self, transport: ProgressTransport, text: str) -> None:
        if text:
            await self._close_stream(transport, text)
            return
        try:
            await self._close_stream(transport, "")
        except Exception:
            await self._close_stream(transport, tr("channel.progress.superseded"))

    async def _close_stream(self, transport: ProgressTransport, text: str) -> None:
        if transport.frame is None or not transport.stream_id:
            return
        await self._runner.close_progress_stream(
            transport.participant_id or "",
            frame=transport.frame,
            stream_id=transport.stream_id,
            text=text,
        )

    def agent_instructions(self) -> str:
        return tr("prompt.channel.wecom")

    def set_inbound_handler(self, handler: InboundHandler | None) -> None:
        super().set_inbound_handler(handler)
        self._runner.set_inbound_handler(self.publish_inbound if handler is not None else None)

    def set_access_controller(self, access: ChannelAccessController) -> None:
        super().set_access_controller(access)
        self._runner.set_access_controller(access)

    def list_connections(self) -> list[ConnectionInfo]:
        return self._runner.list_connections()
