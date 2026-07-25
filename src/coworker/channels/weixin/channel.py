from __future__ import annotations

from coworker.channels.base import BaseChannel, ConnectionInfo, InboundHandler
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
