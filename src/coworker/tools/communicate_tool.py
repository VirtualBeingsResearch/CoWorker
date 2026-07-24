from __future__ import annotations

from typing import TYPE_CHECKING, Any, Protocol

from coworker.channels.base import ConnectionInfo, ParticipantIdResolutionError
from coworker.channels.registry import ChannelRegistry
from coworker.core.types import (
    CommunicateRequest,
    ToolResult,
)
from coworker.i18n import tr
from coworker.tools.base import Tool, ToolDefinition

if TYPE_CHECKING:
    from coworker.core.tool_scope import ToolScope


class ConnectionSource(Protocol):
    def list_connections(self) -> list[ConnectionInfo]: ...


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
    """Tool boundary for communication through the channel registry."""

    def __init__(self, channels: ChannelRegistry) -> None:
        self._channels = channels

    @property
    def channels(self) -> ChannelRegistry:
        return self._channels

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

    def supports_message_extra(self, participant_id: str) -> bool:
        """Whether the participant's selected transport accepts structured ``extra``."""
        return self._channels.supports_message_extra(participant_id)

    def resolve_participant_id(self, participant_id: str) -> str:
        """Expand a shorthand participant ID without sending a message.

        Full IDs and IDs that no channel recognizes are returned unchanged.  A
        shorthand that matches more than one channel is rejected in the same
        way as :meth:`execute`, so callers such as ``bubble_spawn`` cannot bind
        an ambiguous recipient.
        """
        return self._channels.resolve_participant_id(participant_id)

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
            return await self._channels.send(request)
        except ParticipantIdResolutionError as error:
            return ToolResult(tool_call_id="", content=str(error), is_error=True)


class ListConnectionTool(Tool):
    def __init__(self, source: ConnectionSource) -> None:
        self._source = source

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
        infos = self._source.list_connections()
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
