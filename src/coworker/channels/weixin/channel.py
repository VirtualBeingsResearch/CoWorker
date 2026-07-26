from __future__ import annotations

from dataclasses import replace
from typing import Any

from coworker.channels.base import (
    BaseChannel,
    ConnectionInfo,
    InboundHandler,
    PreparedOutbound,
)
from coworker.channels.weixin.runner import WeixinRunner
from coworker.core.types import CommunicateRequest, ToolResult
from coworker.i18n import tr


class WeixinChannel(BaseChannel):
    """Personal-Weixin ClawBot channel using participant IDs prefixed by ``weixin:``."""

    name = "weixin"
    participant_prefix = "weixin:"
    requires_known_participant = True

    def __init__(self, runner: WeixinRunner) -> None:
        super().__init__(runtime=runner)
        self._runner = runner

    def resolve(self, participant_id: str) -> str | None:
        return self._runner.resolve_participant(participant_id)

    async def prepare_action(
        self,
        request: CommunicateRequest,
        recipient: ConnectionInfo | None,
    ) -> PreparedOutbound | ToolResult:
        raw_action = request.extra.get("channel_action")
        action = raw_action if isinstance(raw_action, dict) else {}
        operation = str(action.get("type") or "")
        if operation == "connect":
            return await self._prepare_connect(request, recipient)
        if operation == "poll":
            return await self._prepare_poll(request, action)
        return self._action_error(
            tr("tool_result.communicate.channel_action_operation", operation=operation)
        )

    async def send(self, request: CommunicateRequest) -> ToolResult:
        try:
            await self._runner.send(request.participant_id, request.message)
        except Exception as error:
            return ToolResult(
                tool_call_id="",
                content=tr("tool_result.communicate.weixin_failed", error=error),
                is_error=True,
            )
        return ToolResult(
            tool_call_id="",
            content=tr(
                "tool_result.communicate.weixin_sent",
                participant=request.participant_id,
            ),
        )

    def set_inbound_handler(self, handler: InboundHandler | None) -> None:
        super().set_inbound_handler(handler)
        self._runner.set_inbound_handler(handler)

    def list_connections(self) -> list[ConnectionInfo]:
        connections: list[ConnectionInfo] = []
        for participant_id in self._runner.participant_ids():
            sent_at, received_at = self._runner.activity_for(participant_id)
            account_id = participant_id.split(":", maxsplit=2)[1]
            connections.append(
                ConnectionInfo(
                    participant_id=participant_id,
                    channel=self.name,
                    kind="weixin:direct",
                    active=self._runner.is_account_active(account_id),
                    last_sent_at=sent_at,
                    last_received_at=received_at,
                )
            )
        return connections

    async def _prepare_connect(
        self,
        request: CommunicateRequest,
        recipient: ConnectionInfo | None,
    ) -> PreparedOutbound | ToolResult:
        if recipient is None or "group" in recipient.kind:
            return self._action_error(
                tr("tool_result.communicate.channel_action_private")
            )
        try:
            login = await self._runner.start_login()
        except Exception as error:
            return self._action_error(
                tr("tool_result.communicate.channel_action_failed", error=error)
            )
        message = "\n\n".join(
            part
            for part in (
                request.message.strip(),
                tr(
                    "channel.weixin.connection_invitation",
                    url=login["qrcode_content"],
                ),
            )
            if part
        )
        return PreparedOutbound(
            request=replace(
                request,
                message=message,
                attachments=[
                    *request.attachments,
                    {"type": "image", "path": login["qrcode_path"]},
                ],
                extra={
                    **request.extra,
                    "channel_action": {
                        "channel": self.name,
                        "type": "connection",
                        "status": "waiting",
                        "session_id": login["session_id"],
                    },
                },
            ),
            result_note=tr(
                "tool_result.communicate.channel_action_started",
                session=login["session_id"],
            ),
        )

    async def _prepare_poll(
        self,
        request: CommunicateRequest,
        action: dict[str, Any],
    ) -> PreparedOutbound | ToolResult:
        session_id = str(action.get("session_id") or "")
        if not session_id:
            return self._action_error(
                tr("tool_result.communicate.channel_action_session")
            )
        try:
            result = await self._runner.poll_login(
                session_id,
                str(action.get("verify_code") or ""),
            )
        except Exception as error:
            return self._action_error(
                tr("tool_result.communicate.channel_action_failed", error=error)
            )
        status = str(result["status"])
        credentials = result.get("credentials")
        if credentials is not None:
            from coworker.api.admin import persist_weixin_credentials

            account_id, _ = await persist_weixin_credentials(credentials)
            status = "confirmed"
            message = tr(
                "channel.weixin.connection_confirmed",
                account=account_id,
            )
        else:
            message = tr("channel.weixin.connection_status", status=status)
        return PreparedOutbound(
            request=replace(
                request,
                message="\n\n".join(
                    part for part in (request.message.strip(), message) if part
                ),
                extra={
                    **request.extra,
                    "channel_action": {
                        "channel": self.name,
                        "type": "connection",
                        "status": status,
                        "session_id": session_id,
                    },
                },
            ),
            result_note=tr(
                "tool_result.communicate.channel_action_status",
                status=status,
                session=session_id,
            ),
        )

    @staticmethod
    def _action_error(content: str) -> ToolResult:
        return ToolResult(tool_call_id="", content=content, is_error=True)
