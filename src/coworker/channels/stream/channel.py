"""Generic WS/SSE protocol channel."""

from __future__ import annotations

import json
from typing import Any

from coworker.channels.base import ConnectionInfo, InboundHandler
from coworker.channels.inbound import InboundEnvelope
from coworker.channels.stream.runtime import StreamRuntime
from coworker.core.types import CommunicateRequest, IncomingEvent, ToolResult


class StreamChannel:
    """Normalize stream messages while delegating state to ``StreamRuntime``."""

    name = "stream"
    participant_prefix = ""

    def __init__(self, runtime: StreamRuntime) -> None:
        self._runtime = runtime
        self._inbound_handler: InboundHandler | None = None

    @property
    def runtime(self) -> StreamRuntime:
        return self._runtime

    def resolve(self, participant_id: str) -> str | None:
        return None

    def set_inbound_handler(self, handler: InboundHandler | None) -> None:
        self._inbound_handler = handler

    async def publish_inbound(self, event: IncomingEvent) -> None:
        if self._inbound_handler is None:
            raise RuntimeError("no inbound handler registered")
        await self._inbound_handler(event)

    async def receive_raw(self, envelope: InboundEnvelope) -> None:
        content, conversation_id, raw_attachments = self._parse_inbound(envelope)
        attachments = [
            self._runtime.save_attachment(
                item,
                keep_inline_data=envelope.source != "desktop",
            )
            for item in raw_attachments
        ]
        self.record_received(envelope.participant_id)
        await self.publish_inbound(
            IncomingEvent(
                participant_id=envelope.participant_id,
                content=content,
                conversation_id=conversation_id,
                source=envelope.source,
                attachments=attachments,
            )
        )

    def supports_extra_for(self, participant_id: str) -> bool:
        return self._runtime.supports_message_extra(participant_id)

    async def send(self, request: CommunicateRequest) -> ToolResult:
        return await self._runtime.send(request)

    def list_connections(self) -> list[ConnectionInfo]:
        return self._runtime.list_connections()

    def record_received(self, participant_id: str) -> None:
        self._runtime.record_received(participant_id)

    @staticmethod
    def _parse_inbound(
        envelope: InboundEnvelope,
    ) -> tuple[str, str | None, list[dict[str, Any]]]:
        raw = envelope.payload
        if envelope.source == "websocket":
            text = str(raw.get("text") or "") if isinstance(raw, dict) else str(raw)
            try:
                parsed = json.loads(text)
            except (json.JSONDecodeError, TypeError):
                return text, None, []
            if not isinstance(parsed, dict) or not any(
                key in parsed for key in ("message", "conversation_id", "attachments")
            ):
                return text, None, []
            payload = parsed
            content = str(payload.get("message") or "")
        else:
            payload = raw if isinstance(raw, dict) else {}
            content = str(payload.get("content") or "")

        conversation = payload.get("conversation_id")
        conversation_id = conversation if isinstance(conversation, str) else None
        raw_attachments = [
            item for item in payload.get("attachments", []) if isinstance(item, dict)
        ]
        return content, conversation_id, raw_attachments
