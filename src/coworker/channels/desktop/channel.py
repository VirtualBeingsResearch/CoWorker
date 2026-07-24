"""DesktopChannel: the CoWorker Desktop transport as a Channel.

Wraps :class:`DesktopCommunicateSender` (outbound),
:class:`DesktopRegistry` (actor state for ``list_connections``), and
:class:`DesktopDispatcher` (inbound envelope normalization).
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from coworker.channels.base import ConnectionInfo, InlineChannel
from coworker.channels.desktop import inbound as desktop_inbound
from coworker.channels.desktop.communicate_sender import DESKTOP_PREFIX
from coworker.channels.inbound import AttachmentStore, InboundEnvelope
from coworker.core.types import IncomingEvent

if TYPE_CHECKING:
    from coworker.channels.desktop.communicate_sender import DesktopCommunicateSender
    from coworker.channels.desktop.dispatcher import DesktopDispatcher
    from coworker.channels.desktop.registry import DesktopRegistry


class DesktopChannel(InlineChannel):
    """CoWorker Desktop outbound channel (prefix ``coworker-desktop:``)."""

    def __init__(
        self,
        sender: DesktopCommunicateSender,
        registry: DesktopRegistry,
        dispatcher: DesktopDispatcher,
        attachments_dir: str | Path,
    ) -> None:
        super().__init__(
            DESKTOP_PREFIX,
            sender.send,
            supports_extra=True,
            name="desktop",
            runtime=sender.runtime,
        )
        self._registry = registry
        self._dispatcher = dispatcher
        self._attachments = AttachmentStore(attachments_dir)

    async def receive_raw(self, envelope: InboundEnvelope) -> None:
        payload = envelope.payload if isinstance(envelope.payload, dict) else {}
        desktop_envelope = {
            "protocol_version": payload.get("protocol_version"),
            "message_id": payload.get("message_id"),
            "request_id": payload.get("request_id"),
            "conversation_id": payload.get("conversation_id"),
            "created_at": payload.get("created_at"),
            "type": payload.get("type"),
            "payload": payload.get("payload"),
        }
        text = desktop_inbound.render(desktop_envelope, envelope.participant_id, self._dispatcher)
        self.record_received(envelope.participant_id)
        if text is None:
            return
        raw_attachments = [
            item for item in payload.get("attachments", []) if isinstance(item, dict)
        ]
        nested = payload.get("payload")
        if payload.get("type") == "desktop.thread.event" and isinstance(nested, dict):
            nested_attachments = nested.get("attachments")
            raw_attachments.extend(
                item
                for item in (nested_attachments if isinstance(nested_attachments, list) else [])
                if isinstance(item, dict)
            )
        attachments = [
            self._attachments.save(item, keep_inline_data=False) for item in raw_attachments
        ]
        conversation_id = payload.get("conversation_id")
        await self.publish_inbound(
            IncomingEvent(
                participant_id=envelope.participant_id,
                content=text,
                conversation_id=conversation_id if isinstance(conversation_id, str) else None,
                source="coworker_desktop",
                attachments=attachments,
            )
        )

    def list_connections(self) -> list[ConnectionInfo]:
        # The registry prunes actors whose participant_id is no longer live, so
        # every actor here is a currently-connected desktop participant.
        out: list[ConnectionInfo] = []
        for state in self._registry.actors.values():
            last_sent_at, last_received_at = self.activity_for(state.participant_id)
            out.append(
                ConnectionInfo(
                    participant_id=state.participant_id,
                    channel="desktop",
                    kind=f"desktop:actor:{state.actor_id}",
                    display_name=state.display_name,
                    active=True,
                    last_sent_at=last_sent_at,
                    last_received_at=last_received_at,
                )
            )
        return out
