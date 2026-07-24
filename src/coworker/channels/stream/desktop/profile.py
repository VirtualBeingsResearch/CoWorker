"""Coworker Desktop behavior layered on the shared stream transport."""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING, Any

from coworker.channels.base import ChannelCapabilities, ConnectionInfo
from coworker.channels.inbound import InboundEnvelope
from coworker.channels.stream.desktop import inbound as desktop_inbound
from coworker.channels.stream.profile import StreamProfile
from coworker.channels.stream.runtime import StreamRuntime
from coworker.core.ids import new_compact_id
from coworker.core.types import CommunicateRequest, IncomingEvent, ToolResult
from coworker.i18n import tr

if TYPE_CHECKING:
    from coworker.channels.stream.desktop.dispatcher import DesktopDispatcher
    from coworker.channels.stream.desktop.registry import DesktopRegistry

DESKTOP_PREFIX = "coworker-desktop:"
_CAPABILITIES = ChannelCapabilities(
    conversation_id=True,
    attachments=True,
    extra=True,
)


class DesktopProfile(StreamProfile):
    """Desktop protocol semantics over StreamRuntime sessions and queues."""

    name = "desktop"
    participant_prefix = DESKTOP_PREFIX

    def __init__(
        self,
        registry: DesktopRegistry,
        dispatcher: DesktopDispatcher,
    ) -> None:
        self._registry = registry
        self._dispatcher = dispatcher

    def capabilities_for(self, participant_id: str) -> ChannelCapabilities:
        return _CAPABILITIES

    async def send(
        self,
        request: CommunicateRequest,
        runtime: StreamRuntime,
    ) -> ToolResult:
        queue = runtime.outbound_queue(request.participant_id)
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
        runtime.record_sent(request.participant_id)

        conversation = (
            tr(
                "tool_result.communicate.desktop_conversation",
                conversation=request.conversation_id,
            )
            if request.conversation_id
            else ""
        )
        return ToolResult(
            tool_call_id="",
            content=tr(
                "tool_result.communicate.desktop_sent",
                request_id=request_id,
                conversation=conversation,
            ),
        )

    def normalize_inbound(
        self,
        envelope: InboundEnvelope,
        runtime: StreamRuntime,
    ) -> IncomingEvent | None:
        payload = envelope.payload if isinstance(envelope.payload, dict) else {}
        text = desktop_inbound.render(
            self._desktop_envelope(payload),
            envelope.participant_id,
            self._dispatcher,
        )
        if text is None:
            return None
        attachments = [
            runtime.save_attachment(item, keep_inline_data=False)
            for item in self._raw_attachments(payload)
        ]
        conversation_id = payload.get("conversation_id")
        return IncomingEvent(
            participant_id=envelope.participant_id,
            content=text,
            conversation_id=conversation_id if isinstance(conversation_id, str) else None,
            source="coworker_desktop",
            attachments=attachments,
        )

    def list_connections(self, runtime: StreamRuntime) -> list[ConnectionInfo]:
        connections: list[ConnectionInfo] = []
        for state in self._registry.actors.values():
            last_sent_at, last_received_at = runtime.activity_for(state.participant_id)
            connections.append(
                ConnectionInfo(
                    participant_id=state.participant_id,
                    channel=self.name,
                    kind=f"desktop:actor:{state.actor_id}",
                    display_name=state.display_name,
                    active=True,
                    last_sent_at=last_sent_at,
                    last_received_at=last_received_at,
                )
            )
        return connections

    @staticmethod
    def _desktop_envelope(payload: dict[str, Any]) -> dict[str, Any]:
        return {
            "protocol_version": payload.get("protocol_version"),
            "message_id": payload.get("message_id"),
            "request_id": payload.get("request_id"),
            "conversation_id": payload.get("conversation_id"),
            "created_at": payload.get("created_at"),
            "type": payload.get("type"),
            "payload": payload.get("payload"),
        }

    @staticmethod
    def _raw_attachments(payload: dict[str, Any]) -> list[dict[str, Any]]:
        attachments = [
            item for item in payload.get("attachments", []) if isinstance(item, dict)
        ]
        nested = payload.get("payload")
        if payload.get("type") != "desktop.thread.event" or not isinstance(nested, dict):
            return attachments
        nested_attachments = nested.get("attachments")
        attachments.extend(
            item
            for item in (nested_attachments if isinstance(nested_attachments, list) else [])
            if isinstance(item, dict)
        )
        return attachments
