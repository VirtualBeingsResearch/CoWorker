"""Live WS/SSE connection state owned by ``StreamRuntime``.

WebSocket endpoints register a queue, attach its socket, then run a sender
task. SSE endpoints register and drain the queue directly.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterable
from typing import TYPE_CHECKING, Any

from loguru import logger

from coworker.channels.stream.wire import SHUTDOWN_SENTINEL, serialize_outbound_message
from coworker.i18n import tr

if TYPE_CHECKING:
    from fastapi import WebSocket

__all__ = ["ConnectionPool"]


class ConnectionPool:
    """Single live-connection registry for WS/SSE stream participants."""

    def __init__(self) -> None:
        self._connections: dict[str, WebSocket] = {}
        self._outboxes: dict[str, asyncio.Queue] = {}
        self._transports: dict[str, str] = {}
        self._connection_listeners: list[Any] = []

    # ---------------------------------------------------------- registration

    def register_ws(
        self,
        participant_id: str,
        queue: asyncio.Queue,
        *,
        transport: str = "websocket",
    ) -> bool:
        """Register an outbound queue for a participant.

        The first live connection owns the participant_id. Later SSE/WS
        connections using the same id are rejected instead of replacing it.
        """
        if transport not in {"websocket", "sse"}:
            raise ValueError(f"unsupported stream transport: {transport}")
        if participant_id in self._outboxes:
            return False
        self._outboxes[participant_id] = queue
        self._transports[participant_id] = transport
        self._notify_connection_listeners()
        return True

    def unregister_ws(self, participant_id: str, queue: asyncio.Queue) -> None:
        if self._outboxes.get(participant_id) is not queue:
            return
        self._outboxes.pop(participant_id, None)
        self._transports.pop(participant_id, None)
        self._notify_connection_listeners()

    def outbound_queue(self, participant_id: str) -> asyncio.Queue | None:
        return self._outboxes.get(participant_id)

    def live_stream_transport(self, participant_id: str) -> str | None:
        """Return the transport of a participant's current outbound reply stream."""
        return self._transports.get(participant_id)

    def has_live_stream_connection(
        self,
        participant_id: str,
        *,
        transports: Iterable[str] | None = None,
    ) -> bool:
        """Whether a participant has a live matching WebSocket or SSE reply stream."""
        transport = self.live_stream_transport(participant_id)
        if transport is None:
            return False
        return transports is None or transport in set(transports)

    def list_live_stream_participant_ids(self) -> list[str]:
        return list(self._outboxes.keys())

    # --------------------------------------------------------- ws lifecycle

    async def connect(
        self,
        participant_id: str,
        ws: WebSocket,
        queue: asyncio.Queue,
    ) -> asyncio.Queue:
        if (
            participant_id in self._connections
            or self._outboxes.get(participant_id) is not queue
        ):
            raise ValueError(
                tr("api.attachment.participant_connected", participant=participant_id)
            )
        await ws.accept()
        self._connections[participant_id] = ws
        logger.info(f"WS connected: {participant_id}")
        return queue

    def disconnect(
        self,
        participant_id: str,
        ws: WebSocket,
        queue: asyncio.Queue,
    ) -> None:
        if self._connections.get(participant_id) is not ws:
            return
        if self._outboxes.get(participant_id) is not queue:
            return
        self._connections.pop(participant_id, None)
        removed = participant_id in self._outboxes
        self._outboxes.pop(participant_id, None)
        self._transports.pop(participant_id, None)
        if removed:
            self._notify_connection_listeners()
        logger.info(f"WS disconnected: {participant_id}")

    def is_connected(
        self,
        participant_id: str,
        ws: WebSocket,
        queue: asyncio.Queue,
    ) -> bool:
        return (
            self._connections.get(participant_id) is ws
            and self._outboxes.get(participant_id) is queue
        )

    async def transmit(
        self,
        participant_id: str,
        message: Any,
        ws: WebSocket,
        queue: asyncio.Queue,
    ) -> None:
        try:
            await ws.send_text(serialize_outbound_message(message))
        except Exception as error:
            logger.warning(f"Failed to send WS message to {participant_id}: {error}")
            self.disconnect(participant_id, ws=ws, queue=queue)

    async def run_sender(
        self,
        participant_id: str,
        queue: asyncio.Queue,
        ws: WebSocket,
    ) -> None:
        while self.is_connected(participant_id, ws=ws, queue=queue):
            try:
                message = await asyncio.wait_for(queue.get(), timeout=1.0)
                if message == SHUTDOWN_SENTINEL:
                    break
                await self.transmit(participant_id, message, ws=ws, queue=queue)
            except TimeoutError:
                continue
            except Exception as error:
                logger.error(f"WS sender error for {participant_id}: {error}")
                break

    # ---------------------------------------------------------- shutdown

    def shutdown(self) -> None:
        """Wake every outbound queue so blocked WS/SSE senders can exit."""
        for q in list(self._outboxes.values()):
            try:
                q.put_nowait(SHUTDOWN_SENTINEL)
            except Exception:
                pass

    # ----------------------------------------------------- listeners

    def add_connection_listener(self, listener: Any) -> None:
        self._connection_listeners.append(listener)

    def _notify_connection_listeners(self) -> None:
        for listener in list(self._connection_listeners):
            try:
                listener()
            except Exception as error:
                logger.warning(f"Stream connection listener raised, ignored: {error}")
