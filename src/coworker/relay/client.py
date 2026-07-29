"""Outbound-only client for the end-to-end encrypted Coworker Relay."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import ipaddress
import json
import os
import random
import re
import secrets
import ssl
import tempfile
import time
import uuid
from datetime import UTC, datetime
from functools import partial
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlsplit, urlunsplit

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from loguru import logger
from websockets.asyncio.client import ClientConnection, connect

from coworker.core.config import (
    Config,
    _deep_merge,
    load_admin_overrides,
    write_admin_overrides,
)
from coworker.i18n import tr
from coworker.relay.crypto import (
    b64decode,
    b64encode,
    challenge_payload,
    derive_server_certificate,
    generate_communication_token,
    is_relay_safe_token,
    key_sync_payload,
    load_public_key,
    private_key,
    public_key_text,
    session_payload,
    sign_text,
    verify_text,
)
from coworker.relay.policy import relay_route_allowed
from coworker.relay.protocol import (
    Frame,
    FrameDecoder,
    FrameType,
    json_bytes,
    parse_json,
)

_PROTOCOL = 1
_MAX_OUTER_FRAME = 256 * 1024
_MAX_REQUEST_BODY = 32 * 1024 * 1024
_MAX_CONCURRENT_STREAMS = 64
_MAX_HEADERS = 128
_MAX_HEADER_BYTES = 64 * 1024
_MAX_TARGET_BYTES = 16 * 1024
_HTTP_TOKEN = re.compile(rb"^[!#$%&'*+.^_`|~0-9A-Za-z-]+$")
_INSTANCE_ID = re.compile(r"^cw_[A-Za-z0-9_-]{8,80}$")
_PAIRING_CODE = re.compile(r"^pair_([A-Za-z0-9_-]{8,80})\.([A-Za-z0-9_-]{40,80})$")


class RelayConnectionError(RuntimeError):
    """A safe-to-display Relay connection failure."""


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _websocket_url(base_url: str, path: str) -> str:
    parsed = urlsplit(base_url)
    scheme = "wss" if parsed.scheme == "https" else "ws"
    return urlunsplit((scheme, parsed.netloc, path, "", ""))


def _instance_private_key(value: str) -> Ed25519PrivateKey:
    raw = b64decode(value)
    if len(raw) != 32:
        raise RelayConnectionError("invalid Relay instance private key")
    return Ed25519PrivateKey.from_private_bytes(raw)


def _private_key_text(key: Ed25519PrivateKey) -> str:
    return b64encode(
        key.private_bytes(
            serialization.Encoding.Raw,
            serialization.PrivateFormat.Raw,
            serialization.NoEncryption(),
        )
    )


def _pair_payload(
    pairing_id: str,
    nonce: str,
    instance_public_key: str,
) -> str:
    return "\n".join(
        (
            "coworker-relay-v1",
            "pair",
            pairing_id,
            nonce,
            instance_public_key,
        )
    )


def _pair_ok_payload(
    instance_id: str,
    nonce: str,
    instance_public_key: str,
    relay_public_key: str,
) -> str:
    return "\n".join(
        (
            "coworker-relay-v1",
            "pair-ok",
            instance_id,
            nonce,
            instance_public_key,
            relay_public_key,
        )
    )


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


class _TLSSession:
    def __init__(
        self,
        owner: RelayClient,
        session_id: str,
        connection_id: str,
        source_ip: str,
        public_origin: str,
        context: ssl.SSLContext,
        token: str,
    ) -> None:
        self.owner = owner
        self.session_id = session_id
        self.connection_id = connection_id
        self.source_ip = source_ip
        self.public_origin = public_origin
        self.token = token
        self.incoming = ssl.MemoryBIO()
        self.outgoing = ssl.MemoryBIO()
        self.tls = context.wrap_bio(
            self.incoming,
            self.outgoing,
            server_side=True,
        )
        self.handshake_complete = False
        self.authenticated = False
        self.challenge = ""
        self.decoder = FrameDecoder()
        self.write_lock = asyncio.Lock()
        self.requests: dict[int, asyncio.Queue[dict[str, Any]]] = {}
        self.request_bytes: dict[int, int] = {}
        self.tasks: dict[int, asyncio.Task[None]] = {}
        self.closed = False

    async def feed(self, encrypted: bytes) -> None:
        if self.closed:
            return
        self.incoming.write(encrypted)
        try:
            if not self.handshake_complete:
                await self._handshake()
            if self.handshake_complete:
                await self._read_plaintext()
        except (ssl.SSLError, ValueError, RelayConnectionError) as error:
            logger.warning("Relay E2EE session rejected: {}", str(error)[:300])
            await self.close()
            return
        await self._flush_ciphertext()

    async def _handshake(self) -> None:
        try:
            self.tls.do_handshake()
        except ssl.SSLWantReadError:
            await self._flush_ciphertext()
            return
        self.handshake_complete = True
        self.challenge = b64encode(secrets.token_bytes(32))
        await self.send_frame(
            Frame(
                FrameType.CLIENT_PROOF_CHALLENGE,
                0,
                json_bytes(
                    {
                        "instance_id": self.owner._config.relay.instance_id,
                        "session_id": self.session_id,
                        "nonce": self.challenge,
                    }
                ),
            )
        )

    async def _read_plaintext(self) -> None:
        while True:
            try:
                chunk = self.tls.read(64 * 1024)
            except ssl.SSLWantReadError:
                return
            except ssl.SSLZeroReturnError:
                await self.close()
                return
            if not chunk:
                return
            for frame in self.decoder.feed(chunk):
                await self._handle_frame(frame)

    async def _handle_frame(self, frame: Frame) -> None:
        if not self.authenticated:
            if frame.kind != FrameType.CLIENT_PROOF or frame.stream_id != 0:
                raise RelayConnectionError("inner client proof is required")
            payload = parse_json(frame.payload)
            signature = str(payload.get("signature", ""))
            public = private_key(
                self.token,
                self.owner._config.relay.instance_id,
                "inner-client-proof",
            ).public_key()
            verify_text(
                public,
                _client_proof_payload(
                    self.owner._config.relay.instance_id,
                    self.session_id,
                    self.challenge,
                ),
                signature,
            )
            self.authenticated = True
            await self.send_frame(
                Frame(
                    FrameType.CLIENT_READY,
                    0,
                    json_bytes({"protocol_version": _PROTOCOL}),
                )
            )
            return
        if frame.kind == FrameType.REQUEST_START:
            if frame.stream_id == 0 or frame.stream_id in self.tasks:
                raise RelayConnectionError("invalid or duplicate Relay stream")
            if len(self.tasks) >= _MAX_CONCURRENT_STREAMS:
                raise RelayConnectionError("too many concurrent Relay streams")
            metadata = parse_json(frame.payload)
            queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=16)
            self.requests[frame.stream_id] = queue
            self.request_bytes[frame.stream_id] = 0
            task = asyncio.create_task(
                self._dispatch(frame.stream_id, metadata, queue),
                name=f"relay-e2ee-request:{self.session_id}:{frame.stream_id}",
            )
            self.tasks[frame.stream_id] = task
            task.add_done_callback(partial(self._request_finished, frame.stream_id))
        elif frame.kind == FrameType.REQUEST_BODY:
            body_queue = self.requests.get(frame.stream_id)
            if body_queue is not None:
                size = self.request_bytes.get(frame.stream_id, 0) + len(frame.payload)
                if size > _MAX_REQUEST_BODY:
                    raise RelayConnectionError("Relay request body exceeds the limit")
                self.request_bytes[frame.stream_id] = size
                await body_queue.put(
                    {"type": "http.request", "body": frame.payload, "more_body": True}
                )
        elif frame.kind == FrameType.REQUEST_END:
            end_queue = self.requests.get(frame.stream_id)
            if end_queue is not None:
                await end_queue.put(
                    {"type": "http.request", "body": b"", "more_body": False}
                )
        elif frame.kind == FrameType.REQUEST_CANCEL:
            request_task = self.tasks.get(frame.stream_id)
            if request_task is not None:
                request_task.cancel()
        elif frame.kind == FrameType.PING:
            await self.send_frame(Frame(FrameType.PONG, 0, frame.payload))
        else:
            raise RelayConnectionError("unexpected inner Relay frame")

    def _request_finished(
        self,
        stream_id: int,
        _task: asyncio.Task[None] | None = None,
    ) -> None:
        self.tasks.pop(stream_id, None)
        self.requests.pop(stream_id, None)
        self.request_bytes.pop(stream_id, None)

    async def _dispatch(
        self,
        stream_id: int,
        metadata: dict[str, Any],
        receive_queue: asyncio.Queue[dict[str, Any]],
    ) -> None:
        request_id = str(uuid.uuid4())
        try:
            method = str(metadata.get("method", "")).upper()
            path = str(metadata.get("path", ""))
            raw_target = str(metadata.get("target", path))
            if not relay_route_allowed(method, path):
                await self._simple_error(stream_id, 404, "relay_route_not_exposed")
                return
            if (
                not path.startswith("/")
                or len(raw_target.encode("utf-8")) > _MAX_TARGET_BYTES
                or "\r" in raw_target
                or "\n" in raw_target
            ):
                raise RelayConnectionError("invalid Relay request target")
            raw_headers = metadata.get("headers", [])
            headers = _validated_headers(raw_headers)
            relay_header_start = len(headers)
            public_target = f"/i/{self.owner._config.relay.instance_id}{raw_target}"
            original_url = self.public_origin.rstrip("/") + public_target
            trusted_headers = [
                (b"x-coworker-relay", b"v1"),
                (
                    b"x-coworker-relay-instance",
                    self.owner._config.relay.instance_id.encode("ascii"),
                ),
                (b"x-coworker-relay-request-id", request_id.encode("ascii")),
                (b"x-coworker-relay-original-url", original_url.encode("utf-8")),
                (b"x-coworker-relay-original-target", public_target.encode("utf-8")),
                (
                    b"forwarded",
                    (
                        f'for="{self.source_ip}";proto='
                        f"{urlsplit(self.public_origin).scheme};"
                        f"host={urlsplit(self.public_origin).netloc}"
                    ).encode("ascii"),
                ),
            ]
            headers.extend(trusted_headers)
            path_only, _, query = raw_target.partition("?")
            scope = {
                "type": "http",
                "asgi": {"version": "3.0", "spec_version": "2.3"},
                "http_version": "1.1",
                "method": method,
                "scheme": urlsplit(self.public_origin).scheme,
                "path": path,
                "raw_path": path_only.encode("ascii", "surrogateescape"),
                "query_string": query.encode("ascii", "surrogateescape"),
                "root_path": "",
                "headers": headers,
                "client": (self.source_ip, 0),
                "server": ("coworker-relay-e2ee", 0),
                "state": {
                    "coworker_relay": {
                        "authenticated_tunnel": True,
                        "e2ee": True,
                        "instance_id": self.owner._config.relay.instance_id,
                        "session_id": self.session_id,
                        "request_id": request_id,
                        "relay_header_start": relay_header_start,
                        "source_ip": self.source_ip,
                    }
                },
            }
            response_started = False

            async def receive() -> dict[str, Any]:
                return await receive_queue.get()

            async def send(event: dict[str, Any]) -> None:
                nonlocal response_started
                if event["type"] == "http.response.start":
                    response_started = True
                    response_headers = [
                        [
                            bytes(name).decode("latin-1"),
                            bytes(value).decode("latin-1"),
                        ]
                        for name, value in event.get("headers", [])
                        if bytes(name).lower()
                        not in {b"connection", b"transfer-encoding", b"content-length"}
                    ]
                    response_headers.append(
                        ["x-coworker-relay-request-id", request_id]
                    )
                    await self.send_frame(
                        Frame(
                            FrameType.RESPONSE_START,
                            stream_id,
                            json_bytes(
                                {
                                    "status": int(event["status"]),
                                    "headers": response_headers,
                                }
                            ),
                        )
                    )
                elif event["type"] == "http.response.body":
                    body = bytes(event.get("body", b""))
                    if body:
                        for index in range(0, len(body), 64 * 1024):
                            await self.send_frame(
                                Frame(
                                    FrameType.RESPONSE_BODY,
                                    stream_id,
                                    body[index : index + 64 * 1024],
                                )
                            )
                    if not event.get("more_body", False):
                        await self.send_frame(Frame(FrameType.RESPONSE_END, stream_id))

            await self.owner._app(scope, receive, send)
            if not response_started:
                await self._simple_error(stream_id, 500, "response_not_started")
        except asyncio.CancelledError:
            raise
        except Exception as error:
            logger.exception("Relay E2EE request failed")
            await self.send_frame(
                Frame(
                    FrameType.RESPONSE_ERROR,
                    stream_id,
                    json_bytes({"error": type(error).__name__}),
                )
            )

    async def _simple_error(self, stream_id: int, status: int, code: str) -> None:
        body = json_bytes({"error": code})
        await self.send_frame(
            Frame(
                FrameType.RESPONSE_START,
                stream_id,
                json_bytes(
                    {
                        "status": status,
                        "headers": [
                            ["content-type", "application/json"],
                            ["content-length", str(len(body))],
                        ],
                    }
                ),
            )
        )
        await self.send_frame(Frame(FrameType.RESPONSE_BODY, stream_id, body))
        await self.send_frame(Frame(FrameType.RESPONSE_END, stream_id))

    async def send_frame(self, frame: Frame) -> None:
        async with self.write_lock:
            plain = frame.encode()
            offset = 0
            while offset < len(plain):
                try:
                    offset += self.tls.write(plain[offset:])
                except ssl.SSLWantWriteError:
                    await self._flush_ciphertext()
            await self._flush_ciphertext()

    async def _flush_ciphertext(self) -> None:
        while True:
            chunk = self.outgoing.read()
            if not chunk:
                return
            await self.owner._send_session_data(self.session_id, chunk)

    async def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        for task in self.tasks.values():
            task.cancel()
        if self.tasks:
            await asyncio.gather(*self.tasks.values(), return_exceptions=True)
        self.tasks.clear()
        self.requests.clear()
        self.request_bytes.clear()


def _validated_headers(raw_headers: object) -> list[tuple[bytes, bytes]]:
    if not isinstance(raw_headers, list) or len(raw_headers) > _MAX_HEADERS:
        raise RelayConnectionError("invalid Relay request headers")
    result: list[tuple[bytes, bytes]] = []
    total = 0
    for item in raw_headers:
        if (
            not isinstance(item, list)
            or len(item) != 2
            or not all(isinstance(value, str) for value in item)
        ):
            raise RelayConnectionError("invalid Relay request header")
        name = item[0].encode("ascii", "strict")
        value = item[1].encode("latin-1", "strict")
        if not _HTTP_TOKEN.fullmatch(name) or b"\r" in value or b"\n" in value:
            raise RelayConnectionError("invalid Relay request header")
        total += len(name) + len(value)
        if total > _MAX_HEADER_BYTES:
            raise RelayConnectionError("Relay request headers are too large")
        result.append((name.lower(), value))
    return result


class RelayClient:
    def __init__(self, app: Any, config: Config) -> None:
        self._app = app
        self._config = config
        self._supervisor: asyncio.Task[None] | None = None
        self._socket: ClientConnection | None = None
        self._send_lock = asyncio.Lock()
        self._wake = asyncio.Event()
        self._stopping = False
        self._status = "disabled"
        self._last_error = ""
        self._connected_at = ""
        self._last_heartbeat = ""
        self._latency_ms: float | None = None
        self._connection_id = ""
        self._auth_key_synced = False
        self._seen_session_ids: set[str] = set()
        self._pending_token: str | None = None
        self._token_rotation_done: asyncio.Event | None = None
        self._sessions: dict[str, _TLSSession] = {}
        self._tls_context: ssl.SSLContext | None = None
        self._tls_tempdir: tempfile.TemporaryDirectory[str] | None = None

    def snapshot(self, *, include_token: bool = False) -> dict[str, object]:
        relay = self._config.relay
        result: dict[str, object] = {
            "enabled": relay.enabled,
            "status": self._status,
            "relay_url": relay.url,
            "instance_id": relay.instance_id,
            "public_base_url": (
                f"{relay.url}/i/{relay.instance_id}"
                if relay.url and relay.instance_id
                else ""
            ),
            "connected_at": self._connected_at,
            "last_heartbeat": self._last_heartbeat,
            "latency_ms": self._latency_ms,
            "last_error": self._last_error,
            "protocol_version": _PROTOCOL,
            "e2ee": True,
            "auth_epoch": relay.auth_epoch,
            "auth_key_synced": self._auth_key_synced,
            "active_sessions": len(self._sessions),
            "communication_token_compatible": (
                not self._config.api.communication_token.strip()
                or is_relay_safe_token(self._config.api.communication_token.strip())
            ),
        }
        if include_token:
            result["communication_token"] = self._config.api.communication_token
        return result

    async def start(self) -> None:
        if self._supervisor is not None and not self._supervisor.done():
            return
        self._stopping = False
        self._supervisor = asyncio.create_task(self._run(), name="relay-e2ee-client")

    async def stop(self) -> None:
        self._stopping = True
        self._wake.set()
        if self._socket is not None:
            await self._socket.close(code=1001, reason="coworker stopping")
        if self._supervisor is not None:
            self._supervisor.cancel()
            await asyncio.gather(self._supervisor, return_exceptions=True)
        await self._close_sessions()
        self._cleanup_tls()
        self._supervisor = None
        self._socket = None
        self._status = "disabled" if not self._config.relay.enabled else "disconnected"

    async def reconnect(self) -> None:
        self._wake.set()
        if self._socket is not None:
            await self._socket.close(code=1012, reason="reconnect requested")
        await self.start()

    async def rotate_token(self) -> dict[str, object]:
        """Stage a token, synchronize its public key, then commit it atomically."""

        if not self._config.relay.instance_id:
            token = generate_communication_token()
            self._config.api.communication_token = token
            self._update_api_token(token)
            self._persist()
            result = self.snapshot(include_token=True)
            result["desktop_reconfiguration_required"] = True
            return result
        if self._pending_token is not None:
            raise RelayConnectionError("a Relay token rotation is already in progress")
        token = generate_communication_token()
        rotation_done = asyncio.Event()
        self._pending_token = token
        self._token_rotation_done = rotation_done
        self._auth_key_synced = False
        await self.reconnect()
        try:
            await asyncio.wait_for(rotation_done.wait(), timeout=30)
        except TimeoutError as error:
            if self._pending_token == token:
                self._pending_token = None
                self._token_rotation_done = None
                await self.reconnect()
            raise RelayConnectionError(
                "Relay did not acknowledge the new authentication key"
            ) from error
        if self._config.api.communication_token != token:
            raise RelayConnectionError("Relay token rotation was not committed")
        result = self.snapshot(include_token=True)
        result["desktop_reconfiguration_required"] = True
        return result

    def _commit_token_rotation(self, token: str) -> None:
        if self._pending_token != token:
            return
        self._config.api.communication_token = token
        self._update_api_token(token)
        self._pending_token = None
        self._persist()
        if self._token_rotation_done is not None:
            self._token_rotation_done.set()
        self._token_rotation_done = None

    def _ensure_relay_token(self) -> str:
        token = self._config.api.communication_token.strip()
        if not token:
            token = generate_communication_token()
            self._config.api.communication_token = token
            self._update_api_token(token)
            overrides = load_admin_overrides(self._config.admin.config_file)
            write_admin_overrides(
                self._config.admin.config_file,
                _deep_merge(overrides, {"api": {"communication_token": token}}),
            )
        if not is_relay_safe_token(token):
            raise RelayConnectionError(tr("api.relay.communication_token_unsafe"))
        return token

    @staticmethod
    def _update_api_token(token: str) -> None:
        from coworker.api.routes import update_communication_token

        update_communication_token(token)

    async def enroll(self, relay_url: str, pairing_code: str) -> dict[str, object]:
        relay_url = relay_url.strip().rstrip("/")
        parsed = urlsplit(relay_url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise RelayConnectionError(tr("api.relay.url_invalid"))
        if (
            parsed.username
            or parsed.password
            or parsed.path not in ("", "/")
            or parsed.query
            or parsed.fragment
        ):
            raise RelayConnectionError(tr("api.relay.url_components_invalid"))
        match = _PAIRING_CODE.fullmatch(pairing_code.strip())
        if match is None:
            raise RelayConnectionError(tr("api.relay.pairing_rejected"))
        self._ensure_relay_token()
        pairing_id, pairing_secret = match.groups()
        instance_key = Ed25519PrivateKey.generate()
        instance_public = public_key_text(instance_key)
        try:
            async with connect(
                _websocket_url(relay_url, "/_relay/v1/pair"),
                max_size=64 * 1024,
                open_timeout=15,
                close_timeout=5,
            ) as socket:
                challenge = json.loads(await socket.recv())
                if (
                    challenge.get("type") != "pair_challenge"
                    or challenge.get("protocol_version") != _PROTOCOL
                ):
                    raise RelayConnectionError(tr("api.relay.enrollment_invalid"))
                nonce = str(challenge.get("nonce", ""))
                relay_public = str(challenge.get("relay_public_key", ""))
                load_public_key(relay_public)
                proof = hmac.new(
                    pairing_secret.encode(),
                    _pair_payload(pairing_id, nonce, instance_public).encode(),
                    hashlib.sha256,
                ).digest()
                await socket.send(
                    json.dumps(
                        {
                            "type": "pair_proof",
                            "pairing_id": pairing_id,
                            "instance_public_key": instance_public,
                            "proof": b64encode(proof),
                        },
                        separators=(",", ":"),
                    )
                )
                result = json.loads(await socket.recv())
        except RelayConnectionError:
            raise
        except Exception as error:
            raise RelayConnectionError(
                tr("api.relay.unreachable", error=str(error))
            ) from error
        if result.get("type") != "pair_ok":
            raise RelayConnectionError(tr("api.relay.pairing_rejected"))
        instance_id = str(result.get("instance_id", ""))
        response_relay_key = str(result.get("relay_public_key", ""))
        if not _INSTANCE_ID.fullmatch(instance_id) or response_relay_key != relay_public:
            raise RelayConnectionError(tr("api.relay.enrollment_incomplete"))
        verify_text(
            load_public_key(relay_public),
            _pair_ok_payload(instance_id, nonce, instance_public, relay_public),
            str(result.get("signature", "")),
        )
        relay = self._config.relay
        relay.url = relay_url
        relay.instance_id = instance_id
        relay.instance_private_key = _private_key_text(instance_key)
        relay.relay_public_key = relay_public
        relay.auth_epoch = 0
        relay.enabled = True
        self._persist()
        self._wake.set()
        await self.start()
        return self.snapshot()

    async def disconnect(self) -> None:
        await self.stop()
        relay = self._config.relay
        relay.enabled = False
        relay.url = ""
        relay.instance_id = ""
        relay.instance_private_key = ""
        relay.relay_public_key = ""
        relay.auth_epoch = 0
        self._last_error = ""
        self._persist()
        self._status = "disabled"

    async def test(self) -> dict[str, object]:
        relay = self._config.relay
        if not (relay.enabled and self._status == "connected" and self._auth_key_synced):
            raise RelayConnectionError(tr("api.relay.tunnel_not_connected"))
        return {
            "ok": True,
            "e2ee": True,
            "latency_ms": self._latency_ms,
            "public_base_url": f"{relay.url}/i/{relay.instance_id}",
            "active_sessions": len(self._sessions),
        }

    def _persist(self) -> None:
        overrides = load_admin_overrides(self._config.admin.config_file)
        write_admin_overrides(
            self._config.admin.config_file,
            _deep_merge(
                overrides,
                {
                    "relay": self._config.relay.model_dump(mode="json"),
                    "api": {
                        "communication_token": self._config.api.communication_token
                    },
                },
            ),
        )

    async def _run(self) -> None:
        delay = 1.0
        while not self._stopping:
            relay = self._config.relay
            if not (
                relay.enabled
                and relay.url
                and relay.instance_id
                and relay.instance_private_key
                and relay.relay_public_key
            ):
                self._status = "disabled"
                self._wake.clear()
                await self._wake.wait()
                continue
            try:
                token = self._pending_token or self._ensure_relay_token()
                self._prepare_tls(token)
                self._status = "connecting"
                url = _websocket_url(
                    relay.url,
                    f"/_relay/v1/coworker?instance_id={quote(relay.instance_id)}",
                )
                async with connect(
                    url,
                    max_size=_MAX_OUTER_FRAME + 64 * 1024,
                    ping_interval=20,
                    ping_timeout=30,
                    open_timeout=15,
                    close_timeout=5,
                ) as socket:
                    self._socket = socket
                    await self._authenticate_control(socket, token)
                    self._seen_session_ids.clear()
                    self._status = "connected"
                    self._connected_at = _now()
                    self._last_heartbeat = self._connected_at
                    delay = 1.0
                    await self._listen(socket, token)
            except asyncio.CancelledError:
                raise
            except Exception as error:
                self._last_error = str(error)[:500]
                self._status = "disconnected"
                self._auth_key_synced = False
                logger.warning("Relay E2EE connection lost: {}", self._last_error)
            finally:
                self._socket = None
                self._connection_id = ""
                self._seen_session_ids.clear()
                await self._close_sessions()
            if self._stopping:
                break
            self._wake.clear()
            try:
                await asyncio.wait_for(self._wake.wait(), delay + random.random())
            except TimeoutError:
                pass
            delay = min(delay * 2, 60.0)

    async def _authenticate_control(
        self,
        socket: ClientConnection,
        token: str,
    ) -> None:
        relay = self._config.relay
        challenge = json.loads(await socket.recv())
        if (
            challenge.get("type") != "control_challenge"
            or challenge.get("instance_id") != relay.instance_id
            or challenge.get("protocol_version") != _PROTOCOL
        ):
            raise RelayConnectionError("invalid Relay control challenge")
        payload = challenge_payload(
            "control",
            relay.instance_id,
            str(challenge.get("connection_id", "")),
            str(challenge.get("nonce", "")),
            int(challenge.get("epoch", 0)),
            int(challenge.get("expires_at", 0)),
        )
        verify_text(
            load_public_key(relay.relay_public_key),
            payload,
            str(challenge.get("signature", "")),
        )
        if int(challenge.get("expires_at", 0)) < int(time.time()):
            raise RelayConnectionError("expired Relay control challenge")
        instance_key = _instance_private_key(relay.instance_private_key)
        await socket.send(
            json.dumps(
                {
                    "type": "control_proof",
                    "connection_id": challenge["connection_id"],
                    "signature": sign_text(instance_key, payload),
                },
                separators=(",", ":"),
            )
        )
        ready = json.loads(await socket.recv())
        if ready.get("type") != "control_ready":
            raise RelayConnectionError("Relay control authentication failed")
        self._connection_id = str(challenge["connection_id"])
        current_epoch = int(challenge.get("epoch", 0))
        relay.auth_epoch = max(relay.auth_epoch, current_epoch) + 1
        auth_public = public_key_text(
            private_key(token, relay.instance_id, "relay-entry-auth")
        )
        payload = key_sync_payload(
            relay.instance_id,
            self._connection_id,
            auth_public,
            relay.auth_epoch,
        )
        await socket.send(
            json.dumps(
                {
                    "type": "auth_key",
                    "connection_id": self._connection_id,
                    "epoch": relay.auth_epoch,
                    "public_key": auth_public,
                    "signature": sign_text(instance_key, payload),
                },
                separators=(",", ":"),
            )
        )
        ack = json.loads(await socket.recv())
        if ack.get("type") != "auth_key_ack" or ack.get("epoch") != relay.auth_epoch:
            raise RelayConnectionError("Relay authentication key was not acknowledged")
        if token != self._config.api.communication_token:
            if self._pending_token != token:
                raise RelayConnectionError("Relay token rotation was cancelled")
            self._commit_token_rotation(token)
        self._auth_key_synced = True
        self._persist()

    async def _listen(self, socket: ClientConnection, token: str) -> None:
        async for raw in socket:
            self._last_heartbeat = _now()
            if isinstance(raw, bytes):
                if len(raw) < 17:
                    raise RelayConnectionError("invalid Relay data frame")
                session_id = raw[:16].hex()
                session = self._sessions.get(session_id)
                if session is not None:
                    await session.feed(raw[16:])
                continue
            message = json.loads(raw)
            kind = message.get("type")
            if kind == "session_open":
                await self._open_session(message, token)
            elif kind == "session_close":
                await self._close_signed_session(message)
            elif kind == "ping":
                sent_at = int(message.get("sent_at", 0))
                self._latency_ms = max(0.0, (time.time_ns() - sent_at) / 1_000_000)
                await self._send_json({"type": "pong", "sent_at": sent_at})
            else:
                raise RelayConnectionError("unknown Relay control frame")

    async def _open_session(self, message: dict[str, Any], token: str) -> None:
        relay = self._config.relay
        session_id = str(message.get("session_id", ""))
        connection_id = str(message.get("connection_id", ""))
        source_ip = str(message.get("source_ip", ""))
        origin = str(message.get("public_origin", ""))
        try:
            bytes.fromhex(session_id)
            ipaddress.ip_address(source_ip)
        except ValueError as error:
            raise RelayConnectionError("invalid Relay session metadata") from error
        if (
            len(session_id) != 32
            or message.get("instance_id") != relay.instance_id
            or connection_id != self._connection_id
            or session_id in self._seen_session_ids
        ):
            raise RelayConnectionError("invalid Relay session binding")
        payload = session_payload(
            "session-open",
            relay.instance_id,
            connection_id,
            session_id,
            source_ip,
            origin,
        )
        verify_text(
            load_public_key(relay.relay_public_key),
            payload,
            str(message.get("signature", "")),
        )
        if self._tls_context is None:
            raise RelayConnectionError("Relay TLS context is unavailable")
        await self._close_session(session_id)
        self._seen_session_ids.add(session_id)
        self._sessions[session_id] = _TLSSession(
            self,
            session_id,
            connection_id,
            source_ip,
            origin,
            self._tls_context,
            token,
        )

    async def _close_signed_session(self, message: dict[str, Any]) -> None:
        relay = self._config.relay
        session_id = str(message.get("session_id", ""))
        session = self._sessions.get(session_id)
        if session is None:
            return
        if (
            message.get("instance_id") != relay.instance_id
            or message.get("connection_id") != session.connection_id
            or message.get("source_ip") != session.source_ip
            or message.get("public_origin") != session.public_origin
        ):
            raise RelayConnectionError("invalid Relay session close binding")
        payload = session_payload(
            "session-close",
            relay.instance_id,
            session.connection_id,
            session_id,
            session.source_ip,
            session.public_origin,
        )
        verify_text(
            load_public_key(relay.relay_public_key),
            payload,
            str(message.get("signature", "")),
        )
        await self._close_session(session_id)

    async def _close_session(self, session_id: str) -> None:
        session = self._sessions.pop(session_id, None)
        if session is not None:
            await session.close()

    async def _close_sessions(self) -> None:
        sessions = list(self._sessions.values())
        self._sessions.clear()
        if sessions:
            await asyncio.gather(
                *(session.close() for session in sessions),
                return_exceptions=True,
            )

    async def _send_json(self, value: dict[str, Any]) -> None:
        socket = self._socket
        if socket is None:
            raise RelayConnectionError("Relay control tunnel is not connected")
        async with self._send_lock:
            await socket.send(json.dumps(value, separators=(",", ":")))

    async def _send_session_data(self, session_id: str, payload: bytes) -> None:
        socket = self._socket
        if socket is None:
            raise RelayConnectionError("Relay control tunnel is not connected")
        if len(payload) > _MAX_OUTER_FRAME:
            for index in range(0, len(payload), _MAX_OUTER_FRAME):
                await self._send_session_data(
                    session_id,
                    payload[index : index + _MAX_OUTER_FRAME],
                )
            return
        frame = bytes.fromhex(session_id) + payload
        async with self._send_lock:
            await socket.send(frame)

    def _prepare_tls(self, token: str) -> None:
        self._cleanup_tls()
        certificate, private_key_pem, _ = derive_server_certificate(
            token,
            self._config.relay.instance_id,
        )
        self._tls_tempdir = tempfile.TemporaryDirectory(prefix="coworker-relay-tls-")
        directory = Path(self._tls_tempdir.name)
        cert_path = directory / "certificate.pem"
        key_path = directory / "private-key.pem"
        for path, value in ((cert_path, certificate), (key_path, private_key_pem)):
            descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, "wb") as output:
                output.write(value)
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.minimum_version = ssl.TLSVersion.TLSv1_3
        context.maximum_version = ssl.TLSVersion.TLSv1_3
        context.load_cert_chain(cert_path, key_path)
        context.options |= ssl.OP_NO_COMPRESSION
        self._tls_context = context

    def _cleanup_tls(self) -> None:
        self._tls_context = None
        if self._tls_tempdir is not None:
            self._tls_tempdir.cleanup()
            self._tls_tempdir = None
