"""WeComChannel: the WeCom transport as a Channel.

Wraps :class:`WeComRunner` (WS lifecycle, outbound send, contacts). Outbound
routing uses the runner's ``sender``/``checker``; ``list_connections`` exposes
known WeCom group chats and single-chat users (the user-requested visibility
into WeCom reachables), including the latest send and receive times.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from coworker.channels.base import ConnectionInfo, InlineChannel
from coworker.channels.inbound import InboundEnvelope

if TYPE_CHECKING:
    from coworker.channels.wecom.runner import WeComRunner


class WeComChannel(InlineChannel):
    """WeCom outbound channel (prefix ``wecom:``)."""

    def __init__(self, runner: WeComRunner) -> None:
        super().__init__(
            "wecom:",
            runner.sender,
            checker=runner.checker,
            supports_extra=False,
            name="wecom",
            inbound_sources=frozenset({"wecom"}),
        )
        self._runner = runner
        self._runner.set_channel_receiver(self.receive_raw)

    async def receive_raw(self, envelope: InboundEnvelope) -> None:
        if not isinstance(envelope.payload, dict):
            raise TypeError("WeCom inbound payload must be an SDK frame object")
        event = await self._runner.normalize_inbound(envelope.payload)
        await self.publish_inbound(event)

    def list_connections(self) -> list[ConnectionInfo]:
        out: list[ConnectionInfo] = []
        for chat_id, chat_type, active in self._runner.contact_states():
            participant_id = f"wecom:{chat_type}:{chat_id}"
            last_sent_at, last_received_at = self._runner.activity_for(participant_id)
            out.append(
                ConnectionInfo(
                    participant_id=participant_id,
                    channel="wecom",
                    kind=f"wecom:{chat_type}",
                    active=active,
                    last_sent_at=last_sent_at,
                    last_received_at=last_received_at,
                )
            )
        return out

    async def start(self) -> None:
        await self._runner.start()

    async def stop(self) -> None:
        await self._runner.stop()
