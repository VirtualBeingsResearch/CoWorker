from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING

from coworker.core.ids import new_compact_id
from coworker.core.types import CommunicateRequest, ToolResult
from coworker.i18n import tr

if TYPE_CHECKING:
    from coworker.channels.stream import StreamChannel

DESKTOP_PREFIX = "coworker-desktop:"


class DesktopCommunicateSender:
    def __init__(self, runtime: StreamChannel) -> None:
        self.runtime = runtime

    async def send(self, request: CommunicateRequest) -> ToolResult:
        queue = self.runtime.outbound_queue(request.participant_id)
        if queue is None:
            return ToolResult(
                tool_call_id="",
                content=tr(
                    "tool_result.communicate.desktop_disconnected",
                    participant=request.participant_id,
                ),
                is_error=True,
            )

        extra = dict(request.extra)
        request_id = str(extra.get("request_id") or new_compact_id("req_"))
        extra["request_id"] = request_id
        await queue.put(replace(request, extra=extra))

        conversation = (
            tr(
                "tool_result.communicate.desktop_conversation",
                conversation=request.conversation_id,
            )
            if request.conversation_id
            else ""
        )
        content = tr(
            "tool_result.communicate.desktop_sent",
            request_id=request_id,
            conversation=conversation,
        )
        return ToolResult(tool_call_id="", content=content)
