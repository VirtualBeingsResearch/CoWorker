"""WeComChannel: the WeCom transport as a Channel.

Wraps :class:`WeComRunner` (WS lifecycle, outbound send, contacts). Outbound
routing uses the runner's ``sender``/``resolve_participant``; ``list_connections`` exposes
known WeCom group chats and single-chat users (the user-requested visibility
into WeCom reachables), including the latest send and receive times.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

from coworker.channels.base import (
    BaseChannel,
    ChannelCapabilities,
    ConnectionInfo,
    InboundHandler,
)
from coworker.core.types import CommunicateRequest, ToolResult
from coworker.i18n import tr

if TYPE_CHECKING:
    from coworker.channels.wecom.runner import WeComRunner


class WeComChannel(BaseChannel):
    """WeCom outbound channel (prefix ``wecom:``)."""

    def __init__(self, runner: WeComRunner) -> None:
        super().__init__(
            runtime=runner,
            capabilities=ChannelCapabilities(
                conversation_id=True,
                attachments=True,
                extra=True,
            ),
        )
        self.name = "wecom"
        self.participant_prefix = "wecom:"
        self._runner = runner

    def resolve(self, participant_id: str) -> str | None:
        return self._runner.resolve_participant(participant_id)

    async def send(self, request: CommunicateRequest) -> ToolResult:
        mentioned_users, unsupported_extra = _parse_extra(request.extra)
        try:
            await self._runner.send(
                request.participant_id,
                request.message,
                request.attachments,
                request.conversation_id,
                mentioned_users,
            )
            content = tr(
                "tool_result.communicate.wecom_sent",
                participant=request.participant_id,
            )
            if unsupported_extra:
                content += "\n" + tr(
                    "tool_result.communicate.wecom_extra_unsupported",
                    fields=", ".join(unsupported_extra),
                    supported="mentioned_list",
                )
            return ToolResult(
                tool_call_id="",
                content=content,
            )
        except Exception as error:
            return ToolResult(
                tool_call_id="",
                content=tr("tool_result.communicate.wecom_failed", error=error),
                is_error=True,
            )

    def set_inbound_handler(self, handler: InboundHandler | None) -> None:
        super().set_inbound_handler(handler)
        self._runner.set_inbound_handler(handler)

    def supports_extra(
        self,
        participant_id: str,
        extra: dict[str, object] | None = None,
    ) -> bool:
        return extra is None or set(extra) <= {"mentioned_list"}

    def list_connections(self) -> list[ConnectionInfo]:
        now = time.monotonic()
        out: list[ConnectionInfo] = []
        for chat_id, chat_type in self._runner._contacts.items():
            active = any(
                cached_chat_id == chat_id and now < expires
                for (cached_chat_id, _), (_, expires) in self._runner._frame_cache.items()
            )
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


def _parse_extra(extra: dict[str, object]) -> tuple[list[str], list[str]]:
    mentioned_list = extra.get("mentioned_list")
    mentioned_users = (
        list(
            dict.fromkeys(
                user_id.strip()
                for user_id in mentioned_list
                if isinstance(user_id, str) and user_id.strip()
            )
        )
        if isinstance(mentioned_list, list)
        else []
    )
    unsupported = [key for key in extra if key != "mentioned_list"]
    if "mentioned_list" in extra and not isinstance(mentioned_list, list):
        unsupported.append("mentioned_list")
    return mentioned_users, unsupported
