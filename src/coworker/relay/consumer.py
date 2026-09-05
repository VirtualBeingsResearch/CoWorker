"""Consumer-side Relay tunnel client used by the coworker peer channel.

Implements the Desktop consumer role of the E2EE Relay v1 protocol: WebSocket
entry authentication, TLS 1.3 inside the tunnel with the token-derived server
certificate pinned, the one-time inner client proof, and virtual HTTP framing
(see ``docs/operations/relay-protocol.md``). All keys are derived from the
remote communication token, so no pairing flow is required.
"""

from __future__ import annotations

import asyncio
import json
import re
import ssl
import time
from collections import deque
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from cryptography import x509
from cryptography.hazmat.primitives import serialization
from loguru import logger
from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed

from coworker.relay.crypto import (
    b64decode,
    challenge_payload,
    derive_server_certificate,
    is_relay_safe_token,
    private_key,
    sign_text,
)
from coworker.relay.protocol import (
    Frame,
    FrameDecoder,
    FrameType,
    json_bytes,
)

_PROTOCOL = 1
_MAX_OUTER_FRAME = 256 * 1024
_INNER_CHUNK = 64 * 1024
_INSTANCE_ID = re.compile(r"^cw_[A-Za-z0-9_-]{8,80}$")
_CHALLENGE_MAX_AHEAD_SECONDS = 60


class RelayConsumerError(Exception):
    """A localized failure raised while driving a Relay consumer connection."""

    def __init__(self, message_key: str, **params: Any) -> None:
        self.message_key = message_key
        self.params = params
        super().__init__(f"{message_key}: {params}")


@dataclass(frozen=True)
class RelayHttpResponse:
    status: int
    headers: list[tuple[str, str]]
    body: bytes


def split_relay_base_url(base_url: str) -> tuple[str, str]:
    """Return ``(websocket_origin, instance_id)`` for a ``/i/{id}`` base URL."""

    parsed = urlsplit(base_url.strip().rstrip("/"))
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise RelayConsumerError("tool_result.communicate.coworker_relay_url_invalid")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise RelayConsumerError("tool_result.communicate.coworker_relay_url_invalid")
    segments = [segment for segment in parsed.path.split("/") if segment]
    if (
        len(segments) != 2
        or segments[0] != "i"
        or not _INSTANCE_ID.fullmatch(segments[1])
    ):
        raise RelayConsumerError("tool_result.communicate.coworker_relay_url_invalid")
    scheme = "wss" if parsed.scheme == "https" else "ws"
    origin = urlunsplit((scheme, parsed.netloc, "", "", ""))
    return origin, segments[1]


async def relay_request(
    base_url: str,
    token: str,
    method: str,
    target: str,
    headers: list[tuple[str, str]],
    body: bytes = b"",
    *,
    timeout_seconds: float = 30.0,
) -> RelayHttpResponse:
    """Run one virtual HTTP request through a fresh Relay consumer tunnel."""

    if not is_relay_safe_token(token):
        raise RelayConsumerError("tool_result.communicate.coworker_relay_token_invalid")
    origin, instance_id = split_relay_base_url(base_url)
    websocket_url = f"{origin}/i/{instance_id}/_relay/v1/connect"
    try:
        async with asyncio.timeout(timeout_seconds):
            async with connect(
                websocket_url,
                max_size=_MAX_OUTER_FRAME + 64 * 1024,
                open_timeout=15,
                close_timeout=5,
            ) as socket:
                session_id = await _authenticate_entry(socket, instance_id, token)
                tunnel = _Tunnel(socket, token, instance_id)
                await tunnel.handshake()
                tunnel.verify_pinned_key()
                await tunnel.prove_client(session_id)
                return await tunnel.request(method, target, headers, body)
    except RelayConsumerError:
        raise
    except TimeoutError as error:
        raise RelayConsumerError(
            "tool_result.communicate.coworker_relay_timeout"
        ) from error
    except (OSError, ConnectionClosed, ssl.SSLError) as error:
        logger.debug(f"Relay consumer transport failed: {type(error).__name__}")
        raise RelayConsumerError(
            "tool_result.communicate.coworker_relay_unreachable"
        ) from error


class _Tunnel:
    """Inner TLS 1.3 session plus virtual HTTP framing over the Relay WebSocket."""

    def __init__(self, socket: Any, token: str, instance_id: str) -> None:
        self._socket = socket
        self._token = token
        self._instance_id = instance_id
        self._incoming = ssl.MemoryBIO()
        self._outgoing = ssl.MemoryBIO()
        cert_pem, _, _ = derive_server_certificate(token, instance_id)
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        context.check_hostname = False
        context.verify_mode = ssl.CERT_REQUIRED
        context.minimum_version = ssl.TLSVersion.TLSv1_3
        # 令牌派生的自签证书既是叶子也是信任锚：只有持有对端令牌的实例才能通过验证。
        context.load_verify_locations(cadata=cert_pem.decode("ascii"))
        self._tls = context.wrap_bio(
            self._incoming,
            self._outgoing,
            server_side=False,
        )
        self._decoder = FrameDecoder()
        self._pending: deque[Frame] = deque()

    async def handshake(self) -> None:
        while True:
            try:
                self._tls.do_handshake()
                return
            except ssl.SSLWantReadError:
                await self._flush()
                message = await self._socket.recv()
                if isinstance(message, str):
                    raise RelayConsumerError(
                        "tool_result.communicate.coworker_relay_unreachable"
                    ) from None
                self._incoming.write(message)

    def verify_pinned_key(self) -> None:
        der = self._tls.getpeercert(binary_form=True)
        _, _, public_text = derive_server_certificate(self._token, self._instance_id)
        if der is None:
            raise RelayConsumerError("tool_result.communicate.coworker_relay_identity")
        certificate = x509.load_der_x509_certificate(der)
        raw = certificate.public_key().public_bytes(
            serialization.Encoding.Raw,
            serialization.PublicFormat.Raw,
        )
        if raw != b64decode(public_text):
            raise RelayConsumerError("tool_result.communicate.coworker_relay_identity")

    async def prove_client(self, session_id: str) -> None:
        frame = await self.read_frame()
        if frame.kind != FrameType.CLIENT_PROOF_CHALLENGE or frame.stream_id != 0:
            raise RelayConsumerError("tool_result.communicate.coworker_relay_proof")
        payload = json.loads(frame.payload)
        if (
            payload.get("instance_id") != self._instance_id
            or payload.get("session_id") != session_id
            or not payload.get("nonce")
        ):
            raise RelayConsumerError("tool_result.communicate.coworker_relay_proof")
        signature = sign_text(
            private_key(self._token, self._instance_id, "inner-client-proof"),
            _client_proof_payload(
                self._instance_id,
                session_id,
                str(payload["nonce"]),
            ),
        )
        await self.write_frame(
            Frame(FrameType.CLIENT_PROOF, 0, json_bytes({"signature": signature}))
        )
        ready = await self.read_frame()
        if ready.kind != FrameType.CLIENT_READY or ready.stream_id != 0:
            raise RelayConsumerError("tool_result.communicate.coworker_relay_proof")

    async def request(
        self,
        method: str,
        target: str,
        headers: list[tuple[str, str]],
        body: bytes,
    ) -> RelayHttpResponse:
        await self.write_frame(
            Frame(
                FrameType.REQUEST_START,
                1,
                json_bytes(
                    {
                        "method": method.upper(),
                        "path": target.partition("?")[0],
                        "target": target,
                        "headers": [[name, value] for name, value in headers],
                    }
                ),
            )
        )
        for offset in range(0, len(body), _INNER_CHUNK):
            await self.write_frame(
                Frame(FrameType.REQUEST_BODY, 1, body[offset : offset + _INNER_CHUNK])
            )
        await self.write_frame(Frame(FrameType.REQUEST_END, 1, b""))

        status = 0
        response_headers: list[tuple[str, str]] = []
        chunks: list[bytes] = []
        while True:
            frame = await self.read_frame()
            if frame.kind == FrameType.PING:
                await self.write_frame(Frame(FrameType.PONG, 0, frame.payload))
                continue
            if frame.stream_id != 1:
                continue
            if frame.kind == FrameType.RESPONSE_START:
                metadata = json.loads(frame.payload)
                status = int(metadata.get("status") or 0)
                response_headers = [
                    (str(item[0]), str(item[1]))
                    for item in metadata.get("headers", [])
                    if isinstance(item, list) and len(item) >= 2
                ]
            elif frame.kind == FrameType.RESPONSE_BODY:
                chunks.append(frame.payload)
            elif frame.kind == FrameType.RESPONSE_END:
                if status <= 0:
                    raise RelayConsumerError(
                        "tool_result.communicate.coworker_relay_unreachable"
                    )
                return RelayHttpResponse(
                    status=status,
                    headers=response_headers,
                    body=b"".join(chunks),
                )
            elif frame.kind == FrameType.RESPONSE_ERROR:
                raise RelayConsumerError(
                    "tool_result.communicate.coworker_relay_rejected"
                )

    async def read_frame(self) -> Frame:
        while not self._pending:
            try:
                chunk = self._tls.read(64 * 1024)
            except ssl.SSLWantReadError:
                await self._pump()
                continue
            except ssl.SSLZeroReturnError as error:
                raise RelayConsumerError(
                    "tool_result.communicate.coworker_relay_unreachable"
                ) from error
            self._pending.extend(self._decoder.feed(chunk))
        return self._pending.popleft()

    async def write_frame(self, frame: Frame) -> None:
        plain = frame.encode()
        offset = 0
        while offset < len(plain):
            try:
                offset += self._tls.write(plain[offset:])
            except ssl.SSLWantWriteError:
                await self._flush()
        await self._flush()

    async def _pump(self) -> None:
        await self._flush()
        message = await self._socket.recv()
        if isinstance(message, str):
            raise RelayConsumerError(
                "tool_result.communicate.coworker_relay_unreachable"
            ) from None
        self._incoming.write(message)

    async def _flush(self) -> None:
        while True:
            chunk = self._outgoing.read(_MAX_OUTER_FRAME)
            if not chunk:
                return
            await self._socket.send(chunk)


def _client_proof_payload(instance_id: str, session_id: str, nonce: str) -> str:
    return "\n".join(
        (
            "coworker-relay-v1",
            "inner-client",
            instance_id,
            session_id,
            nonce,
        )
    )


async def _authenticate_entry(
    socket: Any,
    instance_id: str,
    token: str,
) -> str:
    challenge = json.loads(await socket.recv())
    if (
        challenge.get("type") != "auth_challenge"
        or challenge.get("protocol_version") != _PROTOCOL
        or challenge.get("instance_id") != instance_id
    ):
        raise RelayConsumerError("tool_result.communicate.coworker_relay_challenge")
    connection_id = str(challenge.get("connection_id") or "")
    nonce = str(challenge.get("nonce") or "")
    epoch = int(challenge.get("epoch") or 0)
    expires_at = int(challenge.get("expires_at") or 0)
    now = int(time.time())
    if (
        not connection_id
        or not nonce
        or expires_at < now
        or expires_at > now + _CHALLENGE_MAX_AHEAD_SECONDS
    ):
        raise RelayConsumerError("tool_result.communicate.coworker_relay_challenge")
    signature = sign_text(
        private_key(token, instance_id, "relay-entry-auth"),
        challenge_payload(
            "desktop",
            instance_id,
            connection_id,
            nonce,
            epoch,
            expires_at,
        ),
    )
    await socket.send(
        json.dumps(
            {
                "type": "auth_proof",
                "connection_id": connection_id,
                "signature": signature,
            },
            separators=(",", ":"),
        )
    )
    authenticated = json.loads(await socket.recv())
    if authenticated.get("type") != "auth_ok":
        raise RelayConsumerError("tool_result.communicate.coworker_relay_rejected")
    session_id = str(authenticated.get("session_id") or "")
    if not session_id or len(session_id) > 128:
        raise RelayConsumerError("tool_result.communicate.coworker_relay_rejected")
    return session_id
