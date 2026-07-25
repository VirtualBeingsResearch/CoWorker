from __future__ import annotations

from dataclasses import replace
from typing import Any

from coworker.channels.registry import ChannelRegistry, PreparedChannelAction
from coworker.channels.weixin.runner import WeixinRunner
from coworker.core.types import CommunicateRequest, ToolResult
from coworker.i18n import tr


class WeixinChannelAction:
    """Prepare Weixin connection cards sent through the generic communicate path."""

    def __init__(self, runner: WeixinRunner, channels: ChannelRegistry) -> None:
        self._runner = runner
        self._channels = channels

    async def __call__(
        self,
        request: CommunicateRequest,
    ) -> PreparedChannelAction | ToolResult:
        action = request.extra.get("channel_action")
        payload = action if isinstance(action, dict) else {}
        operation = str(payload.get("type") or "")
        if operation == "connect":
            return await self._connect(request)
        if operation == "poll":
            return await self._poll(request, payload)
        return self._error(
            tr("tool_result.communicate.channel_action_operation", operation=operation)
        )

    async def _connect(
        self,
        request: CommunicateRequest,
    ) -> PreparedChannelAction | ToolResult:
        connection = next(
            (
                item
                for item in self._channels.list_connections()
                if item.participant_id == request.participant_id
            ),
            None,
        )
        if connection is None:
            return self._error(tr("tool_result.communicate.channel_action_private"))
        if "group" in connection.kind:
            return self._error(tr("tool_result.communicate.channel_action_private"))
        try:
            login = await self._runner.start_login()
        except Exception as error:
            return self._error(
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
        action_state = {
            "channel": "weixin",
            "type": "connection",
            "status": "waiting",
            "session_id": login["session_id"],
        }
        return PreparedChannelAction(
            request=replace(
                request,
                message=message,
                attachments=[
                    *request.attachments,
                    {"type": "image", "path": login["qrcode_path"]},
                ],
                extra={**request.extra, "channel_action": action_state},
            ),
            result_note=tr(
                "tool_result.communicate.channel_action_started",
                session=login["session_id"],
            ),
        )

    async def _poll(
        self,
        request: CommunicateRequest,
        payload: dict[str, Any],
    ) -> PreparedChannelAction | ToolResult:
        session_id = str(payload.get("session_id") or "")
        if not session_id:
            return self._error(tr("tool_result.communicate.channel_action_session"))
        try:
            result = await self._runner.poll_login(
                session_id,
                str(payload.get("verify_code") or ""),
            )
        except Exception as error:
            return self._error(
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
        return PreparedChannelAction(
            request=replace(
                request,
                message="\n\n".join(
                    part for part in (request.message.strip(), message) if part
                ),
                extra={
                    **request.extra,
                    "channel_action": {
                        "channel": "weixin",
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
    def _error(content: str) -> ToolResult:
        return ToolResult(tool_call_id="", content=content, is_error=True)
