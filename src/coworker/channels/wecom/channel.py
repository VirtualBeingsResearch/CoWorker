"""WeComChannel: the WeCom transport as a Channel.

Wraps :class:`WeComRunner` (WS lifecycle, outbound send, contacts). Outbound
routing uses the runner's ``sender``/``checker``; ``list_connections`` exposes
known WeCom group chats and single-chat users (the user-requested visibility
into WeCom reachables), with ``active`` reflecting a recent inbound frame.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

from coworker.channels.base import ConnectionInfo, InlineChannel

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
        )
        self._runner = runner

    def list_connections(self) -> list[ConnectionInfo]:
        now = time.monotonic()
        out: list[ConnectionInfo] = []
        for chat_id, chat_type in self._runner._contacts.items():
            item = self._runner._frame_cache.get(chat_id)
            active = item is not None and now < item[1]
            out.append(
                ConnectionInfo(
                    participant_id=f"wecom:{chat_type}:{chat_id}",
                    channel="wecom",
                    kind=f"wecom:{chat_type}",
                    active=active,
                )
            )
        return out

    async def start(self) -> None:
        await self._runner.start()

    async def stop(self) -> None:
        await self._runner.stop()
