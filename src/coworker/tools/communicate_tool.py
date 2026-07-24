from __future__ import annotations

from typing import Any, Protocol

from coworker.channels.base import ConnectionInfo, ParticipantIdResolutionError
from coworker.channels.registry import ChannelRegistry
from coworker.core.types import (
    CommunicateRequest,
    ToolResult,
)
from coworker.i18n import tr
from coworker.tools.base import Tool, ToolDefinition


class ConnectionSource(Protocol):
    def list_connections(self) -> list[ConnectionInfo]: ...


class CommunicateTool(Tool):
    """Tool boundary for communication through the channel registry."""

    def __init__(self, channels: ChannelRegistry) -> None:
        self._channels = channels

    def supports_message_extra(
        self,
        participant_id: str,
        extra: dict[str, object] | None = None,
    ) -> bool:
        """Whether the participant's selected transport accepts structured ``extra``."""
        return self._channels.supports_message_extra(participant_id, extra)

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
                        "properties": {
                            "mentioned_list": {
                                "type": "array",
                                "items": {"type": "string"},
                                "description": "企业微信 Markdown 中需要提醒的 userid 列表。",
                            },
                        },
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
