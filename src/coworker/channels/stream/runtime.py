from __future__ import annotations

import re
from collections.abc import Iterable
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from loguru import logger

from coworker.channels.base import ConnectionInfo
from coworker.channels.inbound import AttachmentStore
from coworker.channels.stream.connection_pool import ConnectionPool
from coworker.channels.stream.registration import (
    RegistrationStore,
    build_registration,
    next_participant_id,
)
from coworker.core.types import AttachmentData, CommunicateRequest, ToolResult
from coworker.i18n import tr

if TYPE_CHECKING:
    from fastapi import WebSocket

_UNSAFE_OUTBOX_CHARS_RE = re.compile(r'[<>:"/\\|?*\x00-\x1f]+')


class StreamRuntime:
    """Mutable state and host integration for stream-backed channels."""

    name = "stream"

    def __init__(self, outbox_dir: str | Path, registrations_path: str | Path) -> None:
        self._outbox = Path(outbox_dir)
        self._pool = ConnectionPool()
        self._registrations = RegistrationStore(registrations_path)
        self._attachments = AttachmentStore(self._outbox.parent / "attachments")
        self._last_sent_at: dict[str, str] = {}
        self._last_received_at: dict[str, str] = {}

    @property
    def pool(self) -> ConnectionPool:
        return self._pool

    def register_ws(
        self, participant_id: str, queue: Any, *, transport: str = "websocket"
    ) -> bool:
        return self._pool.register_ws(participant_id, queue, transport=transport)

    def unregister_ws(self, participant_id: str, queue: Any) -> None:
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

    async def connect(self, participant_id: str, ws: WebSocket, queue: Any) -> Any:
        return await self._pool.connect(participant_id, ws, queue)

    def disconnect(
        self, participant_id: str, ws: WebSocket, queue: Any
    ) -> None:
        self._pool.disconnect(participant_id, ws=ws, queue=queue)

    def is_connected(
        self, participant_id: str, ws: WebSocket, queue: Any
    ) -> bool:
        return self._pool.is_connected(participant_id, ws=ws, queue=queue)

    async def run_sender(self, participant_id: str, queue: Any, ws: WebSocket) -> None:
        await self._pool.run_sender(participant_id, queue, ws)

    def shutdown(self) -> None:
        self._pool.shutdown()

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
        return [
            item.to_dict(active=item.participant_id in live_ids)
            for item in self._registrations.load()
        ]

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

    def save_attachment(
        self, attachment: dict[str, Any], *, keep_inline_data: bool
    ) -> AttachmentData:
        return self._attachments.save(attachment, keep_inline_data=keep_inline_data)

    def supports_message_extra(self, participant_id: str) -> bool:
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
            unsupported = self._validate_outbox_request(request)
            if unsupported is not None:
                return unsupported
            self._outbox.mkdir(parents=True, exist_ok=True)
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
            safe_participant_id = (
                _UNSAFE_OUTBOX_CHARS_RE.sub("-", request.participant_id).strip(" .-") or "unknown"
            )
            out_file = self._outbox / f"{timestamp}_{safe_participant_id}.md"
            out_file.write_text(request.message, encoding="utf-8")
            self._last_sent_at[request.participant_id] = _activity_timestamp()
            logger.debug(
                f"No active stream for {request.participant_id}, message written to outbox only"
            )
            return ToolResult(
                tool_call_id="",
                content=tr("tool_result.communicate.fallback_saved", path=out_file),
            )
        except Exception as error:
            return ToolResult(
                tool_call_id="",
                content=tr("tool_result.communicate.failed", error=error),
                is_error=True,
            )

    def list_connections(self) -> list[ConnectionInfo]:
        return [
            ConnectionInfo(
                participant_id=participant_id,
                channel="stream",
                kind=self._pool.live_stream_transport(participant_id) or "websocket",
                active=True,
                last_sent_at=self._last_sent_at.get(participant_id),
                last_received_at=self._last_received_at.get(participant_id),
            )
            for participant_id in self._pool.list_live_stream_participant_ids()
        ]

    def record_received(self, participant_id: str) -> None:
        self._last_received_at[participant_id] = _activity_timestamp()

    def activity_for(self, participant_id: str) -> tuple[str | None, str | None]:
        return self._last_sent_at.get(participant_id), self._last_received_at.get(participant_id)

    def list_live_stream_participant_ids(self) -> list[str]:
        return self._pool.list_live_stream_participant_ids()

    async def start(self) -> None:
        """The API server owns stream connection tasks."""

    async def stop(self) -> None:
        """The API shutdown path closes stream connections."""

    @staticmethod
    def _validate_outbox_request(request: CommunicateRequest) -> ToolResult | None:
        if request.conversation_id:
            key = "tool_result.communicate.conversation_unsupported"
        elif request.extra:
            key = "tool_result.communicate.extra_unsupported"
        elif request.attachments:
            key = "tool_result.communicate.attachments_unsupported"
        elif not request.message:
            key = "tool_result.communicate.message_empty"
        else:
            return None
        return ToolResult(tool_call_id="", content=tr(key), is_error=True)


def _activity_timestamp() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")
