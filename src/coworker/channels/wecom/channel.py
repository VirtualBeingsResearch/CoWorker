"""WeComChannel: the WeCom transport as a Channel.

Wraps :class:`WeComRunner` (WS lifecycle, outbound send, contacts). Outbound
routing uses the runner's ``sender``/``checker``; ``list_connections`` exposes
known WeCom group chats and single-chat users (the user-requested visibility
into WeCom reachables), including the latest send and receive times.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

from coworker.channels.base import ConnectionInfo, InboundHandler, InlineChannel

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
            runtime=runner,
        )
        self._runner = runner

    def set_inbound_handler(self, handler: InboundHandler | None) -> None:
        super().set_inbound_handler(handler)
        self._runner.set_inbound_handler(handler)

    def list_connections(self) -> list[ConnectionInfo]:
        now = time.monotonic()
        out: list[ConnectionInfo] = []
        for chat_id, chat_type in self._runner._contacts.items():
            item = self._runner._frame_cache.get(chat_id)
            active = item is not None and now < item[1]
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
