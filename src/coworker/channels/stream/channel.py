"""StreamChannel: the generic WS/SSE transport as a Channel.

Owns the consolidated live-connection registry (:class:`ConnectionPool`),
persistent participant registrations (:class:`RegistrationStore`), and the
outbox-file fallback. This is the empty-prefix fallback channel in
:class:`~coworker.channels.base.ChannelHost`: it handles any participant_id
no other channel claims -- delivering to a live WS/SSE queue, or writing the
message to the outbox when no connection is live.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from loguru import logger

from coworker.channels.base import ConnectionInfo
from coworker.channels.stream.connection_pool import ConnectionPool
from coworker.channels.stream.registration import (
    RegistrationStore,
    build_registration,
    next_participant_id,
)
from coworker.core.types import CommunicateRequest, ToolResult
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

    def _notify_connection_listeners(self) -> None:
        """Fire connection listeners (used by subclasses managing virtual connections)."""
        self._pool._notify_connection_listeners()

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
        live_ids = set(self._pool.list_connected())
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
        live_ids = set(self._pool.list_connected())
        return [item.to_dict(active=item.participant_id in live_ids) for item in self._registrations.load()]

    def registration_records(self) -> list:
        return self._registrations.load()

    def delete_registration(self, registration_id: str) -> dict[str, Any]:
        registrations = self._registrations.load()
        live_ids = set(self._pool.list_connected())
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
            )
            for pid in self._pool.list_connected()
        ]

    def list_connected(self) -> list[str]:
        return self._pool.list_connected()

    async def start(self) -> None:
        """No background task to start; the WS server is uvicorn-managed."""

    async def stop(self) -> None:
        """No background task to stop; connections are torn down via shutdown()."""
