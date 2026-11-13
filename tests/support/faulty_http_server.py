"""Loopback TCP peer that reproduces provider-side HTTP/TCP failures.

The server speaks just enough HTTP/1.1 for the real client stack (httpx +
openai SDK) to consume, and each mode models a failure class that can happen
below or at the HTTP layer:

``ok``
    Canonical OpenAI Responses payload.
``reset``
    RST after the request headers: the peer process died, a middlebox dropped
    the flow, or the connection was half-open and the write hit a dead socket.
``stall``
    The request is accepted but never answered: black hole, overloaded peer, or
    a NAT/LB that keeps the flow open and silent (client read timeout).
``garbage``
    Non-HTTP bytes: wrong service on the port or a proxy error page.
``truncated``
    Headers promise more body than the peer sends, then the connection closes
    (peer crashed mid-body).
``bad_body``
    200 response whose body is not the JSON the API contract promises (captive
    portal, HTML error page, truncated gateway response).
``status:<code>``
    HTTP error status with an OpenAI-style error body (429/5xx retryable,
    4xx terminal in API terms).

TLS is detected implicitly: the first byte of a TLS ClientHello is not ``P``
for a POST, so the server closes the connection and the client fails the
handshake (certificate/protocol mismatch, TLS-terminating proxy gone).
"""

from __future__ import annotations

import asyncio
import json
import socket
import struct
from typing import Any

RESPONSES_OK: dict[str, Any] = {
    "id": "resp_test",
    "object": "response",
    "created_at": 1,
    "status": "completed",
    "model": "gpt-4o",
    "output": [
        {
            "type": "message",
            "id": "msg_test",
            "status": "completed",
            "role": "assistant",
            "content": [
                {"type": "output_text", "text": "primary response", "annotations": []}
            ],
        }
    ],
    "parallel_tool_calls": False,
    "tool_choice": "auto",
    "tools": [],
    "usage": {"input_tokens": 3, "output_tokens": 2, "total_tokens": 5},
}

_ERROR_BODY: dict[str, Any] = {
    "error": {"message": "upstream failure", "type": "server_error", "code": None}
}

_STATUS_TEXT = {
    429: "Too Many Requests",
    500: "Internal Server Error",
    502: "Bad Gateway",
    503: "Service Unavailable",
    504: "Gateway Timeout",
    401: "Unauthorized",
    403: "Forbidden",
}


def _http_response(status: int, body: bytes) -> bytes:
    reason = _STATUS_TEXT.get(status, "Error")
    return (
        f"HTTP/1.1 {status} {reason}\r\n"
        "Content-Type: application/json\r\n"
        f"Content-Length: {len(body)}\r\n"
        "Connection: close\r\n\r\n"
    ).encode() + body


class FaultyHttpServer:
    """A controllable TCP peer; ``mode`` selects the failure to inject."""

    def __init__(self) -> None:
        self.mode = "ok"
        self.connections = 0
        self.requests = 0
        self._server: asyncio.AbstractServer | None = None
        self._release = asyncio.Event()
        self._stalled: list[asyncio.StreamWriter] = []

    @property
    def port(self) -> int:
        if self._server is None or not self._server.sockets:
            raise RuntimeError("server is not running")
        return int(self._server.sockets[0].getsockname()[1])

    async def start(self) -> None:
        self._server = await asyncio.start_server(self._handle, "127.0.0.1", 0)

    async def close(self) -> None:
        self._release.set()
        for writer in list(self._stalled):
            writer.close()
        self._stalled.clear()
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None

    async def _handle(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        self.connections += 1
        try:
            first = await reader.read(1)
            if first != b"P":
                # TLS ClientHello (0x16) or a non-POST peer; fail the exchange.
                writer.close()
                return
            await reader.readuntil(b"\r\n\r\n")
        except (asyncio.IncompleteReadError, ConnectionError, OSError):
            writer.close()
            return

        self.requests += 1
        mode = self.mode
        try:
            if mode == "reset":
                sock = writer.get_extra_info("socket")
                sock.setsockopt(
                    socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0)
                )
                writer.transport.abort()
                return
            if mode == "stall":
                self._stalled.append(writer)
                await self._release.wait()
                writer.close()
                return
            if mode == "garbage":
                writer.write(b"NOT-HTTP sniffing bytes\r\n\r\n")
            elif mode == "truncated":
                body = json.dumps(RESPONSES_OK).encode()
                writer.write(_http_response(200, body[:12]))
            elif mode == "chunked_corrupt":
                writer.write(
                    b"HTTP/1.1 200 OK\r\n"
                    b"Content-Type: application/json\r\n"
                    b"Transfer-Encoding: chunked\r\n"
                    b"Connection: close\r\n\r\n"
                    b"5\r\nhello\r\nZZ\r\n"
                )
            elif mode == "bad_body":
                writer.write(_http_response(200, b"<html>captive portal</html>"))
            elif mode == "error_payload":
                # Some gateways answer 200 with the API's error envelope.
                writer.write(_http_response(200, json.dumps(_ERROR_BODY).encode()))
            elif mode.startswith("status:"):
                writer.write(
                    _http_response(
                        int(mode.split(":", 1)[1]), json.dumps(_ERROR_BODY).encode()
                    )
                )
            else:
                writer.write(_http_response(200, json.dumps(RESPONSES_OK).encode()))
            await writer.drain()
        except (ConnectionError, OSError):
            pass
        finally:
            writer.close()


def closed_port() -> int:
    """Return a loopback port with no listener (connect-stage failure)."""
    probe = socket.socket()
    try:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])
    finally:
        probe.close()
