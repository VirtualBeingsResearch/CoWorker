from __future__ import annotations

from typing import TYPE_CHECKING, Any

from coworker.channels.base import (
    BaseChannel,
    ChannelCapabilities,
    ConnectionInfo,
    InboundHandler,
)
from coworker.channels.weixin.connections import WeixinConnectionManager
from coworker.channels.weixin.runner import WeixinRunner
from coworker.core.types import CommunicateRequest, ToolResult
from coworker.i18n import tr

CONTROL_PARTICIPANT_ID = "weixin:control"

if TYPE_CHECKING:
    from coworker.channels.runtime import ChannelRuntime


class WeixinChannel(BaseChannel):
    """Personal-Weixin ClawBot communication and connection control."""

    name = "weixin"
    participant_prefix = "weixin:"
    requires_known_participant = True

    def __init__(
        self,
        runtime: ChannelRuntime,
        runner: WeixinRunner,
        connections: WeixinConnectionManager,
    ) -> None:
        super().__init__(runtime=runtime)
        self._runner = runner
        self._connections = connections

    def resolve(self, participant_id: str) -> str | None:
        return self._runner.resolve_participant(participant_id)

    async def send(self, request: CommunicateRequest) -> ToolResult:
        if request.participant_id == CONTROL_PARTICIPANT_ID:
            return await self._control(request.extra)
        if not request.message.strip():
            return self._error(tr("tool_result.communicate.message_empty"))
        try:
            await self._runner.send(request.participant_id, request.message)
        except Exception as error:
            return self._error(
                tr("tool_result.communicate.weixin_failed", error=error)
            )
        return ToolResult(
            tool_call_id="",
            content=tr(
                "tool_result.communicate.weixin_sent",
                participant=request.participant_id,
            ),
        )

    def capabilities_for(self, participant_id: str) -> ChannelCapabilities:
        return ChannelCapabilities(extra=participant_id == CONTROL_PARTICIPANT_ID)

    def set_inbound_handler(self, handler: InboundHandler | None) -> None:
        super().set_inbound_handler(handler)
        self._runner.set_inbound_handler(handler)

    def list_connections(self) -> list[ConnectionInfo]:
        connections = [
            ConnectionInfo(
                participant_id=CONTROL_PARTICIPANT_ID,
                channel=self.name,
                kind="weixin:control",
                display_name=tr("channel.weixin.control_name"),
                active=True,
            )
        ]
        for participant_id in self._runner.participant_ids():
            sent_at, received_at = self._runner.activity_for(participant_id)
            bot_instance_id = participant_id.removeprefix("weixin:")
            connections.append(
                ConnectionInfo(
                    participant_id=participant_id,
                    channel=self.name,
                    kind="weixin:direct",
                    display_name=self._runner.instance_name(bot_instance_id),
                    active=self._runner.is_instance_active(bot_instance_id),
                    last_sent_at=sent_at,
                    last_received_at=received_at,
                )
            )
        return connections

    def agent_instructions(self) -> str:
        return tr("prompt.channel.weixin")

    async def _control(self, extra: dict[str, Any]) -> ToolResult:
        action = str(extra.get("action") or "").strip()
        if action == "connect":
            return await self._connect()
        if action == "status":
            return self._status()
        if action == "verify":
            return await self._verify(extra)
        if action == "remove":
            return await self._remove(extra)
        return self._error(
            tr("tool_result.communicate.weixin_control_action", action=action)
        )

    async def _connect(self) -> ToolResult:
        try:
            login = await self._connections.start_pairing()
        except Exception as error:
            return self._error(
                tr("tool_result.communicate.weixin_control_failed", error=error)
            )
        return ToolResult(
            tool_call_id="",
            content=tr(
                "tool_result.communicate.weixin_control_started",
                session=login["session_id"],
                path=login["qrcode_path"],
                status=login["status"],
            ),
        )

    def _status(self) -> ToolResult:
        result = self._connections.current_pairing()
        if result is None:
            return self._error(tr("tool_result.communicate.weixin_control_no_pairing"))
        participant_id = result.get("participant_id")
        if participant_id:
            content = tr(
                "tool_result.communicate.weixin_control_confirmed",
                session=result["session_id"],
                participant=participant_id,
            )
        else:
            content = tr(
                "tool_result.communicate.weixin_control_status",
                session=result["session_id"],
                status=result["status"],
            )
        return ToolResult(tool_call_id="", content=content)

    async def _verify(self, extra: dict[str, Any]) -> ToolResult:
        session_id = str(extra.get("session_id") or "").strip()
        if not session_id:
            return self._error(
                tr("tool_result.communicate.weixin_control_session")
            )
        verify_code = str(extra.get("verify_code") or "").strip()
        if not verify_code:
            return self._error(
                tr("tool_result.communicate.weixin_control_verify_code")
            )
        try:
            result = await self._connections.submit_verification(
                session_id,
                verify_code,
            )
        except Exception as error:
            return self._error(
                tr("tool_result.communicate.weixin_control_failed", error=error)
            )
        return ToolResult(
            tool_call_id="",
            content=tr(
                "tool_result.communicate.weixin_control_status",
                session=session_id,
                status=result["status"],
            ),
        )

    async def _remove(self, extra: dict[str, Any]) -> ToolResult:
        bot_instance_id = str(extra.get("bot_instance_id") or "").strip()
        if not bot_instance_id:
            return self._error(
                tr("tool_result.communicate.weixin_control_instance")
            )
        if extra.get("confirm") is not True:
            return self._error(
                tr(
                    "tool_result.communicate.weixin_control_confirm_remove",
                    instance=bot_instance_id,
                )
            )
        try:
            removed = await self._connections.remove(bot_instance_id)
        except Exception as error:
            return self._error(
                tr("tool_result.communicate.weixin_control_failed", error=error)
            )
        if not removed:
            return self._error(
                tr(
                    "tool_result.communicate.weixin_control_unknown_instance",
                    instance=bot_instance_id,
                )
            )
        return ToolResult(
            tool_call_id="",
            content=tr(
                "tool_result.communicate.weixin_control_removed",
                instance=bot_instance_id,
            ),
        )

    @staticmethod
    def _error(content: str) -> ToolResult:
        return ToolResult(tool_call_id="", content=content, is_error=True)
