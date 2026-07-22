"""Consolidated live WS/SSE connection registry for the stream channel.

This absorbs the old ``api/ws.py`` ``ConnectionPool`` (ws sockets + sender
loop) and the WS/SSE queue bookkeeping that previously lived in
``CommunicateTool`` (``_ws_connections`` / ``_stream_transports`` /
connection listeners). There is now a single registry: one queue map
(``_outboxes``) instead of the previous dual ``_ws_connections`` +
``ConnectionPool._outboxes`` pointing at the same queues.

WS endpoints register a queue (:meth:`register_ws`) then attach the socket
(:meth:`connect`) and run :meth:`run_sender` to drain it. SSE endpoints
register a queue and drain it directly from their streaming response -- no
socket is attached.
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

    def unregister_ws(self, participant_id: str, queue: asyncio.Queue | None = None) -> None:
        # Identity guard: when a queue is passed, only remove it if it is the
        # currently registered one. Prevents SSE and WS sharing a participant_id
        # from deleting each other's queue, and fixes a race where an old WS
        # disconnect could delete a new connection's queue.
        if queue is not None and self._outboxes.get(participant_id) is not queue:
            return
        if participant_id not in self._outboxes:
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

    def list_connected(self) -> list[str]:
        return list(self._outboxes.keys())

    # --------------------------------------------------------- ws lifecycle

    async def connect(
        self,
        participant_id: str,
        ws: WebSocket,
        queue: asyncio.Queue | None = None,
    ) -> asyncio.Queue:
        if participant_id in self._connections:
            raise ValueError(
                tr("api.attachment.participant_connected", participant=participant_id)
            )
        await ws.accept()
        self._connections[participant_id] = ws
        queue = queue or self._outboxes.get(participant_id) or asyncio.Queue()
        self._outboxes.setdefault(participant_id, queue)
        logger.info(f"WS connected: {participant_id}")
        return queue

    def disconnect(
        self,
        participant_id: str,
        ws: WebSocket | None = None,
        queue: asyncio.Queue | None = None,
    ) -> None:
        if ws is not None and self._connections.get(participant_id) is not ws:
            return
        if queue is not None and self._outboxes.get(participant_id) is not queue:
            return
        self._connections.pop(participant_id, None)
        # Drop the queue + transport too: with a single registry this is the
        # WS teardown point (SSE tears down via unregister_ws). Notifying here
        # keeps connection listeners (e.g. desktop registry) accurate even
        # though unregister_ws becomes a no-op when the queue is already gone.
        removed = participant_id in self._outboxes
        self._outboxes.pop(participant_id, None)
        self._transports.pop(participant_id, None)
        if removed:
            self._notify_connection_listeners()
        logger.info(f"WS disconnected: {participant_id}")

    def is_connected(
        self,
        participant_id: str,
        ws: WebSocket | None = None,
        queue: asyncio.Queue | None = None,
    ) -> bool:
        if participant_id not in self._connections:
            return False
        if ws is not None and self._connections.get(participant_id) is not ws:
            return False
        if queue is not None and self._outboxes.get(participant_id) is not queue:
            return False
        return True

    async def transmit(
        self,
        participant_id: str,
        message: Any,
        ws: WebSocket | None = None,
        queue: asyncio.Queue | None = None,
    ) -> None:
        ws = ws or self._connections.get(participant_id)
        if ws:
            try:
                await ws.send_text(serialize_outbound_message(message))
            except Exception as e:
                logger.warning(f"Failed to send WS message to {participant_id}: {e}")
                self.disconnect(participant_id, ws=ws, queue=queue)

    async def run_sender(
        self,
        participant_id: str,
        queue: asyncio.Queue,
        ws: WebSocket | None = None,
    ) -> None:
        while self.is_connected(participant_id, ws=ws, queue=queue):
            try:
                message = await asyncio.wait_for(queue.get(), timeout=1.0)
                if message == SHUTDOWN_SENTINEL:
                    break
                await self.transmit(participant_id, message, ws=ws, queue=queue)
            except TimeoutError:
                continue
            except Exception as e:
                logger.error(f"WS sender error for {participant_id}: {e}")
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
                logger.warning(f"Communicate connection listener raised, ignored: {error}")
