"""StreamChannel: the generic WS/SSE transport as a Channel.

Owns the consolidated live-connection registry (:class:`ConnectionPool`),
persistent participant registrations (:class:`RegistrationStore`), and the
outbox-file fallback. This is the empty-prefix fallback channel in
:class:`~coworker.channels.registry.ChannelRegistry`: it handles any participant_id
no other channel claims -- delivering to a live WS/SSE queue, or writing the
message to the outbox when no connection is live.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from loguru import logger

from coworker.channels.base import ConnectionInfo, InboundHandler
from coworker.channels.inbound import AttachmentStore, InboundEnvelope
from coworker.channels.stream.connection_pool import ConnectionPool
from coworker.channels.stream.registration import (
    RegistrationStore,
    build_registration,
    next_participant_id,
)
from coworker.core.types import CommunicateRequest, IncomingEvent, ToolResult
from coworker.i18n import tr

if TYPE_CHECKING:
    from fastapi import WebSocket

_UNSAFE_OUTBOX_CHARS_RE = re.compile(r'[<>:"/\\|?*\x00-\x1f]+')


class StreamChannel:
    """Generic WS/SSE stream transport (the fallback channel)."""

    name = "stream"
    participant_prefix = ""

    def __init__(self, outbox_dir: str | Path, registrations_path: str | Path) -> None:
        self._outbox = Path(outbox_dir)
        self._pool = ConnectionPool()
        self._registrations = RegistrationStore(registrations_path)
        self._attachments = AttachmentStore(Path(outbox_dir).parent / "attachments")
        self._last_sent_at: dict[str, str] = {}
        self._last_received_at: dict[str, str] = {}
        self._inbound_handler: InboundHandler | None = None

    @property
    def runtime(self) -> StreamChannel:
        """The stream transport owns its own stateful runtime."""
        return self

    def set_inbound_handler(self, handler: InboundHandler | None) -> None:
        self._inbound_handler = handler

    async def publish_inbound(self, event: IncomingEvent) -> None:
        if self._inbound_handler is None:
            raise RuntimeError("no inbound handler registered")
        await self._inbound_handler(event)

    async def receive_raw(self, envelope: InboundEnvelope) -> None:
        raw = envelope.payload
        text = str(raw.get("text") or "") if isinstance(raw, dict) else str(raw)
        content = text
        conversation_id = None
        raw_attachments: list[dict[str, Any]] = []
        if envelope.source == "websocket":
            try:
                parsed = json.loads(text)
                if isinstance(parsed, dict) and any(
                    key in parsed for key in ("message", "conversation_id", "attachments")
                ):
                    content = str(parsed.get("message") or "")
                    raw_conversation = parsed.get("conversation_id")
                    conversation_id = (
                        raw_conversation if isinstance(raw_conversation, str) else None
                    )
                    raw_attachments = [
                        item for item in parsed.get("attachments", []) if isinstance(item, dict)
                    ]
            except (json.JSONDecodeError, TypeError):
                pass
        else:
            payload = raw if isinstance(raw, dict) else {}
            content = str(payload.get("content") or "")
            raw_conversation = payload.get("conversation_id")
            conversation_id = raw_conversation if isinstance(raw_conversation, str) else None
            raw_attachments = [
                item for item in payload.get("attachments", []) if isinstance(item, dict)
            ]
        attachments = [
            self._attachments.save(item, keep_inline_data=envelope.source != "desktop")
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

    # ----------------------------------------------------- connection access

    @property
    def pool(self) -> ConnectionPool:
        return self._pool

    def register_ws(
        self, participant_id: str, queue: Any, *, transport: str = "websocket"
    ) -> bool:
        return self._pool.register_ws(participant_id, queue, transport=transport)

    def unregister_ws(self, participant_id: str, queue: Any | None = None) -> None:
        self._pool.unregister_ws(participant_id, queue)

    def outbound_queue(self, participant_id: str) -> Any | None:
        return self._pool.outbound_queue(participant_id)

    def live_stream_transport(self, participant_id: str) -> str | None:
        return self._pool.live_stream_transport(participant_id)

    def has_live_stream_connection(
        self, participant_id: str, *, transports: Iterable[str] | None = None
    ) -> bool:
        return self._pool.has_live_stream_connection(participant_id, transports=transports)

    def add_connection_listener(self, listener: Any) -> None:
        self._pool.add_connection_listener(listener)

    async def connect(self, participant_id: str, ws: WebSocket, queue: Any | None = None) -> Any:
        return await self._pool.connect(participant_id, ws, queue)

    def disconnect(
        self, participant_id: str, ws: WebSocket | None = None, queue: Any | None = None
    ) -> None:
        self._pool.disconnect(participant_id, ws=ws, queue=queue)

    def is_connected(
        self, participant_id: str, ws: WebSocket | None = None, queue: Any | None = None
    ) -> bool:
        return self._pool.is_connected(participant_id, ws=ws, queue=queue)

    async def run_sender(self, participant_id: str, queue: Any, ws: WebSocket | None = None) -> None:
        await self._pool.run_sender(participant_id, queue, ws)

    def shutdown(self) -> None:
        self._pool.shutdown()

    # ------------------------------------------------------------- registration

    def register_participant(
        self,
        *,
        kind: str,
        client_id: str,
        display_name: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        kind = kind.strip()
        client_id = client_id.strip()
        if not kind:
            raise ValueError("kind is required")
        if not client_id:
            raise ValueError("client_id is required")

        registrations = self._registrations.load()
        live_ids = set(self._pool.list_live_stream_participant_ids())
        reusable = next(
            (
                item
                for item in registrations
                if item.kind == kind
                and item.client_id == client_id
                and item.participant_id not in live_ids
            ),
            None,
        )
        if reusable is not None:
            reusable.display_name = display_name or reusable.display_name
            reusable.last_registered_at = datetime.now().isoformat()
            reusable.metadata = metadata or reusable.metadata
            self._registrations.save(registrations)
            return reusable.to_dict(active=False)

        participant_id = next_participant_id(kind, client_id, registrations, live_ids)
        registration = build_registration(
            kind=kind,
            client_id=client_id,
            display_name=display_name,
            metadata=metadata or {},
            participant_id=participant_id,
        )
        registrations.append(registration)
        self._registrations.save(registrations)
        return registration.to_dict(active=False)

    def list_registrations(self) -> list[dict[str, Any]]:
        live_ids = set(self._pool.list_live_stream_participant_ids())
        return [item.to_dict(active=item.participant_id in live_ids) for item in self._registrations.load()]

    def registration_records(self) -> list:
        return self._registrations.load()

    def delete_registration(self, registration_id: str) -> dict[str, Any]:
        registrations = self._registrations.load()
        live_ids = set(self._pool.list_live_stream_participant_ids())
        for index, item in enumerate(registrations):
            if item.registration_id != registration_id:
                continue
            if item.participant_id in live_ids:
                raise RuntimeError("registration is active; stop the connection before deleting it")
            removed = registrations.pop(index)
            self._registrations.save(registrations)
            return removed.to_dict(active=False)
        raise KeyError(registration_id)

    # ---------------------------------------------------------- Channel protocol

    def resolve(self, participant_id: str) -> str | None:
        # The stream channel is the fallback; it never claims a bare id via
        # checker -- routing reaches it only when no other channel matches.
        return None

    def supports_extra_for(self, participant_id: str) -> bool:
        return self._pool.has_live_stream_connection(participant_id)

    async def send(self, request: CommunicateRequest) -> ToolResult:
        queue = self._pool.outbound_queue(request.participant_id)
        if queue is not None:
            await queue.put(request)
            self._last_sent_at[request.participant_id] = _activity_timestamp()
            return ToolResult(
                tool_call_id="",
                content=tr(
                    "tool_result.communicate.websocket_sent",
                    participant=request.participant_id,
                ),
            )
        try:
            if request.conversation_id:
                return ToolResult(
                    tool_call_id="",
                    content=tr("tool_result.communicate.conversation_unsupported"),
                    is_error=True,
                )
            if request.extra:
                return ToolResult(
                    tool_call_id="",
                    content=tr("tool_result.communicate.extra_unsupported"),
                    is_error=True,
                )
            if request.attachments:
                return ToolResult(
                    tool_call_id="",
                    content=tr("tool_result.communicate.attachments_unsupported"),
                    is_error=True,
                )
            if not request.message:
                return ToolResult(
                    tool_call_id="",
                    content=tr("tool_result.communicate.message_empty"),
                    is_error=True,
                )

            self._outbox.mkdir(parents=True, exist_ok=True)
            ts = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
            safe_participant_id = (
                _UNSAFE_OUTBOX_CHARS_RE.sub("-", request.participant_id).strip(" .-") or "unknown"
            )
            out_file = self._outbox / f"{ts}_{safe_participant_id}.md"
            out_file.write_text(request.message, encoding="utf-8")
            self._last_sent_at[request.participant_id] = _activity_timestamp()

            logger.debug(f"No active WS for {request.participant_id}, message written to outbox only")
            return ToolResult(
                tool_call_id="",
                content=tr("tool_result.communicate.fallback_saved", path=out_file),
            )
        except Exception as e:
            return ToolResult(
                tool_call_id="",
                content=tr("tool_result.communicate.failed", error=e),
                is_error=True,
            )

    def list_connections(self) -> list[ConnectionInfo]:
        return [
            ConnectionInfo(
                participant_id=pid,
                channel="stream",
                kind=self._pool.live_stream_transport(pid) or "websocket",
                active=True,
                last_sent_at=self._last_sent_at.get(pid),
                last_received_at=self._last_received_at.get(pid),
            )
            for pid in self._pool.list_live_stream_participant_ids()
        ]

    def record_received(self, participant_id: str) -> None:
        self._last_received_at[participant_id] = _activity_timestamp()

    def list_live_stream_participant_ids(self) -> list[str]:
        return self._pool.list_live_stream_participant_ids()

    async def start(self) -> None:
        """No background task to start; the WS server is uvicorn-managed."""

    async def stop(self) -> None:
        """No background task to stop; connections are torn down via shutdown()."""


def _activity_timestamp() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")
