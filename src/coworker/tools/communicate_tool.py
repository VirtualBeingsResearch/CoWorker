from __future__ import annotations

import asyncio
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import TYPE_CHECKING, Any

from coworker.channels.base import (
    Channel,
    ChannelHost,
    InboundHandler,
    ParticipantIdResolutionError,
)
from coworker.channels.inbound import InboundEnvelope
from coworker.channels.stream import StreamChannel
from coworker.core.types import (
    CommunicateRegistration,
    CommunicateRequest,
    IncomingEvent,
    ToolResult,
)
from coworker.i18n import tr
from coworker.tools.base import Tool, ToolDefinition

ConnectionListener = Callable[[], None]


if TYPE_CHECKING:
    from coworker.core.tool_scope import ToolScope


class _BoundCommunicateTool(Tool):
    """A bubble-scoped communicator restricted to one pre-authorized recipient."""

    def __init__(
        self,
        delegate: CommunicateTool,
        participant_id: str,
        conversation_id: str = "",
        message_prefix: str = "",
        message_extra: dict[str, Any] | None = None,
    ) -> None:
        self._delegate = delegate
        self._participant_id = participant_id
        self._conversation_id = conversation_id
        self._message_prefix = message_prefix
        self._message_extra = dict(message_extra or {})

    @property
    def definition(self) -> ToolDefinition:
        base = self._delegate.definition
        properties = dict(base.parameters["properties"])
        properties["participant_id"] = {
            "type": "string",
            "description": "可选；此泡泡已固定绑定通信对象，不能改为其他对象。",
        }
        parameters = {
            **base.parameters,
            "properties": properties,
            "required": [
                name for name in base.parameters.get("required", []) if name != "participant_id"
            ],
        }
        conversation_note = f"，会话固定为 {self._conversation_id}" if self._conversation_id else ""
        return ToolDefinition(
            name=base.name,
            description=(
                f"向此泡泡绑定的通信对象发送消息（对象固定为 {self._participant_id}"
                f"{conversation_note}）。不得联系其他对象。"
            ),
            parameters=parameters,
        )

    async def execute(
        self,
        participant_id: str = "",
        message: str = "",
        conversation_id: str | None = None,
        attachments: list[dict[str, Any]] | None = None,
        extra: dict[str, Any] | None = None,
        **_,
    ) -> ToolResult:
        requested_participant = participant_id.strip() if isinstance(participant_id, str) else ""
        if requested_participant and requested_participant != self._participant_id:
            return ToolResult(
                tool_call_id="",
                content=tr("tool_result.communicate.bound_participant"),
                is_error=True,
            )

        requested_conversation = conversation_id.strip() if isinstance(conversation_id, str) else ""
        if (
            self._conversation_id
            and requested_conversation
            and (requested_conversation != self._conversation_id)
        ):
            return ToolResult(
                tool_call_id="",
                content=tr("tool_result.communicate.bound_conversation"),
                is_error=True,
            )

        outgoing_message = message
        if self._message_prefix:
            if outgoing_message:
                if not outgoing_message.startswith(self._message_prefix):
                    outgoing_message = f"{self._message_prefix}{outgoing_message}"
            elif attachments:
                outgoing_message = tr(
                    "tool_result.communicate.attachment_fallback",
                    prefix=self._message_prefix,
                )

        if extra is not None and not isinstance(extra, dict):
            return ToolResult(
                tool_call_id="",
                content=tr("tool_result.communicate.extra_object"),
                is_error=True,
            )
        outgoing_extra = dict(extra or {})
        outgoing_extra.update(self._message_extra)
        return await self._delegate.execute(
            participant_id=self._participant_id,
            message=outgoing_message,
            conversation_id=self._conversation_id or requested_conversation or None,
            attachments=attachments,
            extra=outgoing_extra,
        )


class CommunicateTool(Tool):
    """Outbound communication tool.

    Routing (prefix senders + checkers) still lives here during the channel
    refactor; it moves to :class:`~coworker.channels.base.ChannelHost` in a
    later phase. Transport (WS/SSE connections, participant registrations,
    outbox fallback) is delegated to the composed :class:`StreamChannel`.
    """

    def __init__(self, outbox_dir: str) -> None:
        self._stream = StreamChannel(
            outbox_dir, Path(outbox_dir).parent / "communicate_registrations.json"
        )
        self._host = ChannelHost()
        self._host.register(self._stream)  # stream is the empty-prefix fallback

    @property
    def stream(self) -> StreamChannel:
        """The underlying WS/SSE stream channel (transport + registrations)."""
        return self._stream

    @property
    def host(self) -> ChannelHost:
        """The channel router owning participant_id routing + lifecycle."""
        return self._host

    def register_channel(self, channel: Channel) -> None:
        """Register a communication channel (e.g. desktop, wecom)."""
        self._host.register(channel)

    def add_connection_listener(self, listener: ConnectionListener) -> None:
        self._stream.add_connection_listener(listener)

    def fork(self, scope: ToolScope) -> Tool:
        participant_id = str(getattr(scope, "communicate_participant_id", "")).strip()
        if not participant_id:
            return self
        conversation_id = str(getattr(scope, "communicate_conversation_id", "")).strip()
        message_prefix = str(getattr(scope, "communicate_message_prefix", ""))
        message_extra = getattr(scope, "communicate_message_extra", {})
        return _BoundCommunicateTool(
            self,
            participant_id,
            conversation_id,
            message_prefix,
            message_extra if isinstance(message_extra, dict) else {},
        )

    # --------------------------------------------------- stream transport delegates

    def register_ws(
        self,
        participant_id: str,
        queue: asyncio.Queue,
        *,
        transport: str = "websocket",
    ) -> bool:
        return self._stream.register_ws(participant_id, queue, transport=transport)

    def unregister_ws(self, participant_id: str, queue: asyncio.Queue | None = None) -> None:
        self._stream.unregister_ws(participant_id, queue)

    def outbound_queue(self, participant_id: str) -> asyncio.Queue | None:
        return self._stream.outbound_queue(participant_id)

    def live_stream_transport(self, participant_id: str) -> str | None:
        return self._stream.live_stream_transport(participant_id)

    def has_live_stream_connection(
        self,
        participant_id: str,
        *,
        transports: Iterable[str] | None = None,
    ) -> bool:
        return self._stream.has_live_stream_connection(participant_id, transports=transports)

    def list_live_stream_participant_ids(self) -> list[str]:
        return self._host.list_live_stream_participant_ids()

    def list_connections(self) -> list:
        """Aggregate reachable participants across all channels (for list_connections tool)."""
        return self._host.list_connections()

    def record_received(self, participant_id: str) -> None:
        """Record an inbound message for the participant's selected channel."""
        self._host.record_received(participant_id)

    def set_inbound_handler(self, handler: InboundHandler | None) -> None:
        """Attach the inbox owner to all registered communication channels."""
        self._host.set_inbound_handler(handler)

    async def publish_inbound(self, event: IncomingEvent) -> None:
        """Publish a normalized inbound event through the channel host."""
        await self._host.publish_inbound(event)

    async def receive_raw(self, envelope: InboundEnvelope) -> None:
        """Route raw protocol input to the owning channel for normalization."""
        await self._host.receive_raw(envelope)

    def shutdown(self) -> None:
        """Wake all live WS/SSE queues so blocked senders can exit on shutdown."""
        self._stream.shutdown()

    # ---------------------------------------------------------- registration delegates

    def register_participant(
        self,
        *,
        kind: str,
        client_id: str,
        display_name: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return self._stream.register_participant(
            kind=kind,
            client_id=client_id,
            display_name=display_name,
            metadata=metadata,
        )

    def list_registrations(self) -> list[dict[str, Any]]:
        return self._stream.list_registrations()

    def registration_records(self) -> list[CommunicateRegistration]:
        return self._stream.registration_records()

    def delete_registration(self, registration_id: str) -> dict[str, Any]:
        return self._stream.delete_registration(registration_id)

    # ------------------------------------------------------------------ routing

    def supports_message_extra(self, participant_id: str) -> bool:
        """Whether the participant's selected transport accepts structured ``extra``."""
        return self._host.supports_message_extra(participant_id)

    def resolve_participant_id(self, participant_id: str) -> str:
        """Expand a shorthand participant ID without sending a message.

        Full IDs and IDs that no channel recognizes are returned unchanged.  A
        shorthand that matches more than one channel is rejected in the same
        way as :meth:`execute`, so callers such as ``bubble_spawn`` cannot bind
        an ambiguous recipient.
        """
        return self._host.resolve_participant_id(participant_id)

    @property
    def definition(self) -> ToolDefinition:
        description = (
            "向指定通信对象发送消息。participant_id 表示对象；conversation_id "
            "表示同一对象下的某段会话。attachments 和 extra 的支持情况由目标信道决定。"
        )
        return ToolDefinition(
            name="communicate",
            description=description,
            parameters={
                "type": "object",
                "properties": {
                    "participant_id": {"type": "string", "description": "接收方的 ID"},
                    "message": {"type": "string", "description": "要发送的消息内容"},
                    "conversation_id": {
                        "type": "string",
                        "description": "同一 participant_id 下的会话 ID。",
                    },
                    "attachments": {
                        "type": "array",
                        "description": "可选附件；具体支持情况由目标信道决定。",
                        "items": {
                            "type": "object",
                            "properties": {
                                "type": {
                                    "type": "string",
                                    "description": "附件类型：image / file",
                                    "enum": ["image", "file"],
                                },
                                "filename": {
                                    "type": "string",
                                    "description": "可选展示文件名；默认使用 path 的文件名",
                                },
                                "media_type": {
                                    "type": "string",
                                    "description": "可选 MIME 类型；默认按文件扩展名推断",
                                },
                                "path": {
                                    "type": "string",
                                    "description": "本地文件绝对或相对路径",
                                },
                            },
                            "required": ["path"],
                        },
                    },
                    "extra": {
                        "type": "object",
                        "description": "低频信道扩展；具体白名单由目标信道决定。",
                    },
                },
                "required": ["participant_id"],
            },
        )

    async def execute(
        self,
        participant_id: str,
        message: str = "",
        conversation_id: str | None = None,
        attachments: list[dict[str, Any]] | None = None,
        extra: dict[str, Any] | None = None,
        **_,
    ) -> ToolResult:
        attachments = attachments or []
        extra = extra or {}
        if not isinstance(extra, dict):
            return ToolResult(
                tool_call_id="",
                content=tr("tool_result.communicate.extra_object"),
                is_error=True,
            )

        request = CommunicateRequest(
            participant_id=participant_id,
            message=message,
            conversation_id=conversation_id,
            attachments=attachments,
            extra=extra,
        )
        try:
            return await self._host.send(request)
        except ParticipantIdResolutionError as error:
            return ToolResult(tool_call_id="", content=str(error), is_error=True)


class ListConnectionTool(Tool):
    def __init__(self, communicate: CommunicateTool) -> None:
        self._communicate = communicate

    @property
    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="list_connections",
            description="列出所有信道的在线连接与已知通信对象（WS/SSE 在线流、企微群聊/用户、Desktop actor）",
            parameters={
                "type": "object",
                "properties": {},
                "required": [],
            },
        )

    async def execute(self, **_) -> ToolResult:
        infos = self._communicate.list_connections()
        if not infos:
            return ToolResult(
                tool_call_id="", content=tr("tool_result.communicate.no_connections")
            )
        by_channel: dict[str, list[Any]] = {}
        for info in infos:
            by_channel.setdefault(info.channel, []).append(info)
        lines: list[str] = []
        for channel in sorted(by_channel):
            lines.append(f"{channel}:")
            for info in sorted(by_channel[channel], key=lambda i: i.participant_id):
                activity = tr(
                    "tool_result.communicate.connection_activity",
                    sent=info.last_sent_at or tr("tool_result.communicate.connection_none"),
                    received=info.last_received_at
                    or tr("tool_result.communicate.connection_none"),
                )
                display_name = f" ({info.display_name})" if info.display_name else ""
                lines.append(
                    tr(
                        "tool_result.communicate.connection_line",
                        participant=info.participant_id,
                        activity=activity,
                        display_name=display_name,
                    )
                )
        return ToolResult(tool_call_id="", content="\n".join(lines))
