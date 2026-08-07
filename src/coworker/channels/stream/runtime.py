from __future__ import annotations

import asyncio
import re
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from loguru import logger

from coworker.channels.access import (
    ChannelAccessDeniedError,
    inbound_access_denied_message,
)
from coworker.channels.activity import ChannelActivityStore
from coworker.channels.base import ConnectionInfo
from coworker.channels.inbound import AttachmentStore
from coworker.channels.stream.connection_pool import ConnectionPool
from coworker.channels.stream.registration import (
    RegistrationStore,
    build_registration,
    next_participant_id,
)
from coworker.channels.traffic import ChannelTrafficStore
from coworker.core.types import (
    AttachmentData,
    CommunicateRegistration,
    CommunicateRequest,
    ToolResult,
)
from coworker.i18n import tr

if TYPE_CHECKING:
    from fastapi import WebSocket

_UNSAFE_OUTBOX_CHARS_RE = re.compile(r'[<>:"/\\|?*\x00-\x1f]+')
_ACCESS_DENIED_CLOSE_CODE = 1008


class StreamRuntime:
    """Mutable state and host integration for stream-backed channels."""

    name = "stream"

    def __init__(
        self,
        outbox_dir: str | Path,
        registrations_path: str | Path,
        activity: ChannelActivityStore | None = None,
        traffic: ChannelTrafficStore | None = None,
    ) -> None:
        self._outbox = Path(outbox_dir)
        self._pool = ConnectionPool()
        self._registrations = RegistrationStore(registrations_path)
        self._attachments = AttachmentStore(self._outbox.parent / "attachments")
        self._activity = activity or ChannelActivityStore()
        self._traffic = traffic if traffic is not None else ChannelTrafficStore()

    def register_session(
        self,
        participant_id: str,
        queue: asyncio.Queue[Any],
        *,
        transport: str = "websocket",
    ) -> bool:
        return self._pool.register_session(participant_id, queue, transport=transport)

    def unregister_session(self, participant_id: str, queue: asyncio.Queue[Any]) -> None:
        self._pool.unregister_session(participant_id, queue)

    def outbound_queue(self, participant_id: str) -> asyncio.Queue[Any] | None:
        return self._pool.outbound_queue(participant_id)

    def live_stream_transport(self, participant_id: str) -> str | None:
        return self._pool.live_stream_transport(participant_id)

    def add_connection_listener(self, listener: Any) -> None:
        self._pool.add_connection_listener(listener)

    async def connect(
        self,
        participant_id: str,
        ws: WebSocket,
        queue: asyncio.Queue[Any],
    ) -> asyncio.Queue[Any]:
        return await self._pool.connect(participant_id, ws, queue)

    def disconnect(
        self, participant_id: str, ws: WebSocket, queue: asyncio.Queue[Any]
    ) -> None:
        self._pool.disconnect(participant_id, ws=ws, queue=queue)

    async def run_sender(
        self,
        participant_id: str,
        queue: asyncio.Queue[Any],
        ws: WebSocket,
    ) -> None:
        await self._pool.run_sender(participant_id, queue, ws)

    async def reject_inbound_access(
        self,
        ws: WebSocket,
        error: ChannelAccessDeniedError,
    ) -> None:
        """Notify and disconnect a stream client rejected by channel policy."""
        try:
            await ws.send_text(inbound_access_denied_message())
            self._traffic.record(
                direction="outbound",
                channel=error.channel,
                participant_id=error.participant_id,
                status="sent",
                source="access_policy",
                reason="rejection_notice",
            )
        except Exception as reply_error:
            self._traffic.record(
                direction="outbound",
                channel=error.channel,
                participant_id=error.participant_id,
                status="failed",
                source="access_policy",
                reason="rejection_notice",
            )
            logger.warning(
                tr(
                    "channel.access.inbound_denied_reply_failed",
                    channel=error.channel,
                    participant=error.participant_id,
                    error=reply_error,
                )
            )
        await ws.close(
            code=_ACCESS_DENIED_CLOSE_CODE,
            reason=tr("api.message.channel_access_denied_websocket"),
        )

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

    def registration_records(self) -> list[CommunicateRegistration]:
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
            self._activity.record_sent(request.participant_id)
            return ToolResult(
                tool_call_id="",
                content=tr(
                    "tool_result.communicate.websocket_sent",
                    participant=request.participant_id,
                ),
            )
        try:
            if not request.message:
                return ToolResult(
                    tool_call_id="",
                    content=tr("tool_result.communicate.message_empty"),
                    is_error=True,
                )
            self._outbox.mkdir(parents=True, exist_ok=True)
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
            safe_participant_id = (
                _UNSAFE_OUTBOX_CHARS_RE.sub("-", request.participant_id).strip(" .-") or "unknown"
            )
            out_file = self._outbox / f"{timestamp}_{safe_participant_id}.md"
            out_file.write_text(request.message, encoding="utf-8")
            self._activity.record_sent(request.participant_id)
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
        live_ids = set(self._pool.list_live_stream_participant_ids())
        registrations = {
            registration.participant_id: registration
            for registration in self._registrations.load()
            if registration.participant_id
        }
        participant_ids = list(registrations)
        participant_ids.extend(sorted(live_ids - registrations.keys()))

        connections: list[ConnectionInfo] = []
        for participant_id in participant_ids:
            registration = registrations.get(participant_id)
            last_sent_at, last_received_at = self._activity.activity_for(participant_id)
            connections.append(
                ConnectionInfo(
                    participant_id=participant_id,
                    channel="stream",
                    kind=(
                        registration.kind
                        if registration is not None
                        else self._pool.live_stream_transport(participant_id) or "websocket"
                    ),
                    display_name=(
                        registration.display_name if registration is not None else ""
                    ),
                    active=participant_id in live_ids,
                    last_sent_at=last_sent_at,
                    last_received_at=last_received_at,
                )
            )
        return connections

    def record_received(self, participant_id: str) -> None:
        self._activity.record_received(participant_id)

    def record_sent(self, participant_id: str) -> None:
        self._activity.record_sent(participant_id)

    def activity_for(self, participant_id: str) -> tuple[str | None, str | None]:
        return self._activity.activity_for(participant_id)

    def list_live_stream_participant_ids(self) -> list[str]:
        return self._pool.list_live_stream_participant_ids()

    async def start(self) -> None:
        """The API server owns stream connection tasks."""

    async def stop(self) -> None:
        """The API shutdown path closes stream connections."""
