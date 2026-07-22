from __future__ import annotations

from datetime import datetime
from typing import Any

from coworker.channels.base import ConnectionInfo
from coworker.core.types import CommunicateRequest, ToolResult
from coworker.tools.communicate_tool import CommunicateTool

DEFAULT_LAB_WS_CONNECTIONS: tuple[str, ...] = ("explore_lab",)


class LabCommunicateTool(CommunicateTool):
    """Explore Lab communication shim.

    Branches need to exercise the same communicate/list_connections tools as
    production, but they should not send real external messages. This subclass
    adds configurable in-lab participants and records outbound requests for the
    control UI/API to inspect.
    """

    def __init__(
        self,
        outbox_dir: str,
        *,
        virtual_connections: list[str] | tuple[str, ...] = DEFAULT_LAB_WS_CONNECTIONS,
    ) -> None:
        super().__init__(outbox_dir)
        self._virtual_connections: set[str] = set()
        self._outbound_messages: list[dict[str, Any]] = []
        self.set_virtual_connections(virtual_connections)

    def set_virtual_connections(self, participant_ids: list[str] | tuple[str, ...]) -> None:
        self._virtual_connections = {
            str(pid).strip()
            for pid in participant_ids
            if str(pid).strip()
        }
        self._notify_connection_listeners()

    def virtual_connections(self) -> list[str]:
        return sorted(self._virtual_connections)

    def list_connected(self) -> list[str]:
        return sorted(set(super().list_connected()) | self._virtual_connections)

    def list_connections(self) -> list[ConnectionInfo]:
        infos = super().list_connections()
        known_participants = {info.participant_id for info in infos}
        infos.extend(
            ConnectionInfo(
                participant_id=participant_id,
                channel="explore_lab",
                kind="virtual",
                active=True,
            )
            for participant_id in sorted(self._virtual_connections - known_participants)
        )
        return infos

    def outbound_messages(self) -> list[dict[str, Any]]:
        return list(self._outbound_messages)

    async def execute(
        self,
        participant_id: str,
        message: str = "",
        conversation_id: str | None = None,
        attachments: list[dict[str, Any]] | None = None,
        extra: dict[str, Any] | None = None,
        **kwargs,
    ) -> ToolResult:
        if participant_id not in self._virtual_connections:
            return await super().execute(
                participant_id=participant_id,
                message=message,
                conversation_id=conversation_id,
                attachments=attachments,
                extra=extra,
                **kwargs,
            )

        attachments = attachments or []
        extra = extra or {}
        if not isinstance(extra, dict):
            return ToolResult(tool_call_id="", content="extra 必须是对象。", is_error=True)
        if not message and not extra:
            return ToolResult(tool_call_id="", content="message 不能为空。", is_error=True)

        request = CommunicateRequest(
            participant_id=participant_id,
            message=message,
            conversation_id=conversation_id,
            attachments=attachments,
            extra=extra,
        )
        payload = request.to_dict()
        payload["timestamp"] = datetime.now().isoformat()
        self._outbound_messages.append(payload)
        return ToolResult(
            tool_call_id="",
            content=f"消息已发送给 Explore Lab 模拟连接 {participant_id}",
        )
