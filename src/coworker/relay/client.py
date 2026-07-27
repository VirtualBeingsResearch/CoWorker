"""A thin, outbound-only transport adapter for Coworker Relay.

The client owns enrollment, the authenticated WSS connection, reconnects, and
verifier synchronization. Requests received from the tunnel are dispatched
through the existing ASGI application, so routing and authorization remain
owned by Coworker's normal HTTP stack.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import random
import time
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import httpx
from argon2 import PasswordHasher
from loguru import logger
from websockets.asyncio.client import ClientConnection, connect

from coworker.core.config import (
    Config,
    _deep_merge,
    effective_communication_token,
    load_admin_overrides,
    write_admin_overrides,
)
from coworker.i18n import tr

_PROTOCOL = 1
_MAX_REQUEST_BODY_BYTES = 32 * 1024 * 1024
_MAX_FRAME_BYTES = 48 * 1024 * 1024
_PASSWORD_HASHER = PasswordHasher(
    time_cost=2,
    memory_cost=19 * 1024,
    parallelism=1,
    hash_len=32,
    salt_len=16,
)


class RelayConnectionError(RuntimeError):
    """A safe-to-display relay connection failure."""


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _websocket_url(base_url: str) -> str:
    parsed = urlsplit(base_url)
    return urlunsplit(("wss", parsed.netloc, "/_relay/v1/connect", "", ""))


class RelayClient:
    def __init__(self, app: Any, config: Config) -> None:
        self._app = app
        self._config = config
        self._supervisor: asyncio.Task[None] | None = None
        self._socket: ClientConnection | None = None
        self._send_lock = asyncio.Lock()
        self._request_tasks: dict[str, asyncio.Task[None]] = {}
        self._wake = asyncio.Event()
        self._stopping = False
        self._status = "disabled"
        self._last_error = ""
        self._connected_at = ""
        self._last_heartbeat = ""
        self._latency_ms: float | None = None
        self._last_token_fingerprint = ""
        self._pending_verifier_fingerprint = ""

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
            "verifier_synced": self._verifier_is_synced(),
        }
        if include_token:
            result["communication_token"] = effective_communication_token(self._config)
        return result

    async def start(self) -> None:
        if self._supervisor is not None and not self._supervisor.done():
            return
        self._stopping = False
        self._supervisor = asyncio.create_task(self._run(), name="relay-client")

    async def stop(self) -> None:
        self._stopping = True
        self._wake.set()
        socket = self._socket
        if socket is not None:
            await socket.close(code=1001, reason="coworker stopping")
        if self._supervisor is not None:
            self._supervisor.cancel()
            await asyncio.gather(self._supervisor, return_exceptions=True)
        await self._cancel_requests()
        self._supervisor = None
        self._socket = None
        self._status = "disabled" if not self._config.relay.enabled else "disconnected"

    async def reconnect(self) -> None:
        self._wake.set()
        socket = self._socket
        if socket is not None:
            await socket.close(code=1012, reason="reconnect requested")
        await self.start()

    async def enroll(self, relay_url: str, pairing_code: str) -> dict[str, object]:
        relay_url = relay_url.strip().rstrip("/")
        parsed = urlsplit(relay_url)
        if parsed.scheme != "https" or not parsed.hostname:
            raise RelayConnectionError(tr("api.relay.url_invalid"))
        if (
            parsed.username
            or parsed.password
            or parsed.path not in ("", "/")
            or parsed.query
            or parsed.fragment
        ):
            raise RelayConnectionError(tr("api.relay.url_components_invalid"))
        token = effective_communication_token(self._config)
        if not token:
            raise RelayConnectionError(tr("api.relay.communication_token_missing"))
        verifier = _PASSWORD_HASHER.hash(token)
        try:
            async with httpx.AsyncClient(timeout=15, follow_redirects=False) as client:
                response = await client.post(
                    f"{relay_url}/_relay/v1/enroll",
                    json={
                        "pairing_code": pairing_code.strip(),
                        "verifier": verifier,
                        "protocol_version": _PROTOCOL,
                    },
                )
        except httpx.HTTPError as error:
            raise RelayConnectionError(
                tr("api.relay.unreachable", error=str(error))
            ) from error
        if response.status_code != 200:
            if response.status_code == 401:
                raise RelayConnectionError(tr("api.relay.pairing_rejected"))
            if response.status_code == 400:
                raise RelayConnectionError(tr("api.relay.enrollment_invalid"))
            raise RelayConnectionError(
                tr("api.relay.enrollment_failed", status=response.status_code)
            )
        payload = response.json()
        instance_id = str(payload.get("instance_id", ""))
        credential = str(payload.get("instance_credential", ""))
        if not instance_id or not credential:
            raise RelayConnectionError(tr("api.relay.enrollment_incomplete"))
        self._config.relay.url = relay_url
        self._config.relay.instance_id = instance_id
        self._config.relay.instance_credential = credential
        self._config.relay.enabled = True
        self._persist()
        self._last_token_fingerprint = hashlib.sha256(token.encode()).hexdigest()
        self._wake.set()
        await self.start()
        return self.snapshot()

    async def disconnect(self) -> None:
        await self.stop()
        self._config.relay.enabled = False
        self._config.relay.url = ""
        self._config.relay.instance_id = ""
        self._config.relay.instance_credential = ""
        self._last_token_fingerprint = ""
        self._pending_verifier_fingerprint = ""
        self._last_error = ""
        self._persist()
        self._status = "disabled"

    async def test(self) -> dict[str, object]:
        relay_url = self._config.relay.url
        if not relay_url:
            raise RelayConnectionError(tr("api.relay.not_configured"))
        started = time.monotonic()
        try:
            async with httpx.AsyncClient(timeout=10, follow_redirects=False) as client:
                response = await client.get(f"{relay_url}/_relay/v1/health")
                response.raise_for_status()
        except httpx.HTTPError as error:
            raise RelayConnectionError(
                tr("api.relay.connectivity_failed", error=str(error))
            ) from error
        return {
            "ok": True,
            "latency_ms": round((time.monotonic() - started) * 1000, 1),
        }

    def _persist(self) -> None:
        path = self._config.admin.config_file
        overrides = load_admin_overrides(path)
        relay_data = self._config.relay.model_dump(mode="json")
        write_admin_overrides(path, _deep_merge(overrides, {"relay": relay_data}))

    async def _run(self) -> None:
        delay = 1.0
        while not self._stopping:
            relay = self._config.relay
            if not (
                relay.enabled
                and relay.url
                and relay.instance_id
                and relay.instance_credential
            ):
                self._status = "disabled"
                self._wake.clear()
                await self._wake.wait()
                continue
            self._status = "connecting"
            self._last_error = ""
            try:
                async with connect(
                    _websocket_url(relay.url),
                    additional_headers={
                        "Authorization": f"Bearer {relay.instance_credential}",
                        "X-Coworker-Relay-Instance": relay.instance_id,
                        "X-Coworker-Relay-Protocol": str(_PROTOCOL),
                    },
                    max_size=_MAX_FRAME_BYTES,
                    ping_interval=20,
                    ping_timeout=30,
                    open_timeout=15,
                    close_timeout=5,
                ) as socket:
                    self._socket = socket
                    self._status = "connected"
                    self._connected_at = _now()
                    self._last_heartbeat = self._connected_at
                    delay = 1.0
                    await self._sync_verifier(force=True)
                    verifier_task = asyncio.create_task(
                        self._watch_verifier(),
                        name="relay-verifier-watch",
                    )
                    try:
                        await self._listen(socket)
                    finally:
                        verifier_task.cancel()
                        await asyncio.gather(verifier_task, return_exceptions=True)
            except asyncio.CancelledError:
                raise
            except Exception as error:
                self._last_error = str(error)[:500]
                self._status = "disconnected"
                logger.warning("Relay connection lost: {}", self._last_error)
            finally:
                self._socket = None
                await self._cancel_requests()
            if self._stopping:
                break
            self._wake.clear()
            try:
                await asyncio.wait_for(self._wake.wait(), delay + random.random())
            except TimeoutError:
                pass
            delay = min(delay * 2, 60.0)

    async def _listen(self, socket: ClientConnection) -> None:
        async for raw in socket:
            if not isinstance(raw, str):
                continue
            message = json.loads(raw)
            kind = message.get("type")
            if kind == "request":
                request_id = str(message.get("request_id", ""))
                if not request_id or request_id in self._request_tasks:
                    continue
                request_task = asyncio.create_task(
                    self._handle_request(message),
                    name=f"relay-request:{request_id}",
                )
                self._request_tasks[request_id] = request_task
                request_task.add_done_callback(
                    self._request_finished_callback(request_id)
                )
            elif kind == "cancel":
                cancelled_task = self._request_tasks.get(
                    str(message.get("request_id", ""))
                )
                if cancelled_task is not None:
                    cancelled_task.cancel()
            elif kind == "ping":
                sent_at = float(message.get("sent_at", 0))
                self._last_heartbeat = _now()
                self._latency_ms = max(0.0, round((time.time() - sent_at) * 1000, 1))
                await self._send({"type": "pong", "sent_at": sent_at})
            elif kind == "verifier_required":
                await self._sync_verifier(force=True)
            elif kind == "verifier_ack":
                self._accept_verifier_ack(str(message.get("generation", "")))

    def _request_finished_callback(
        self,
        request_id: str,
    ) -> Callable[[asyncio.Task[None]], None]:
        def remove(_task: asyncio.Task[None]) -> None:
            self._request_tasks.pop(request_id, None)

        return remove

    async def _watch_verifier(self) -> None:
        while True:
            await asyncio.sleep(5)
            await self._sync_verifier()

    async def _sync_verifier(self, *, force: bool = False) -> None:
        token = effective_communication_token(self._config)
        if not token:
            raise RelayConnectionError(tr("api.relay.communication_token_missing"))
        fingerprint = hashlib.sha256(token.encode()).hexdigest()
        if not force and fingerprint == self._last_token_fingerprint:
            return
        self._pending_verifier_fingerprint = fingerprint
        await self._send(
            {
                "type": "verifier",
                "verifier": _PASSWORD_HASHER.hash(token),
                "generation": fingerprint,
            }
        )

    def _verifier_is_synced(self) -> bool:
        token = effective_communication_token(self._config)
        if not token:
            return False
        return (
            not self._pending_verifier_fingerprint
            and hashlib.sha256(token.encode()).hexdigest()
            == self._last_token_fingerprint
        )

    def _accept_verifier_ack(self, generation: str) -> None:
        if generation == self._pending_verifier_fingerprint:
            self._last_token_fingerprint = generation
            self._pending_verifier_fingerprint = ""

    async def _handle_request(self, message: dict[str, Any]) -> None:
        request_id = str(message["request_id"])
        try:
            body = base64.b64decode(str(message.get("body", "")), validate=True)
            if len(body) > _MAX_REQUEST_BODY_BYTES:
                raise ValueError(tr("api.relay.request_body_too_large"))
            raw_headers = message.get("headers", [])
            headers = [
                (str(name).encode("latin-1"), str(value).encode("latin-1"))
                for name, value in raw_headers
            ]
            path = str(message.get("path", "/"))
            raw_path = str(message.get("raw_path", path)).encode("ascii", "surrogateescape")
            query = str(message.get("query", "")).encode("ascii", "surrogateescape")
            client_ip = str(message.get("client_ip", ""))
            scope = {
                "type": "http",
                "asgi": {"version": "3.0", "spec_version": "2.3"},
                "http_version": "1.1",
                "method": str(message.get("method", "GET")),
                "scheme": "https",
                "path": path,
                "raw_path": raw_path,
                "query_string": query,
                "root_path": "",
                "headers": headers,
                "client": (client_ip, 0),
                "server": ("coworker-relay", 443),
                "state": {
                    "coworker_relay": {
                        "authenticated_tunnel": True,
                        "instance_id": self._config.relay.instance_id,
                        "request_id": request_id,
                        "relay_header_start": int(message.get("relay_header_start", len(headers))),
                    }
                },
            }
            received = False

            async def receive() -> dict[str, object]:
                nonlocal received
                if not received:
                    received = True
                    return {"type": "http.request", "body": body, "more_body": False}
                await asyncio.Future()
                return {"type": "http.disconnect"}

            started = False

            async def send(event: dict[str, Any]) -> None:
                nonlocal started
                if event["type"] == "http.response.start":
                    started = True
                    await self._send(
                        {
                            "type": "response_start",
                            "request_id": request_id,
                            "status": int(event["status"]),
                            "headers": [
                                [name.decode("latin-1"), value.decode("latin-1")]
                                for name, value in event.get("headers", [])
                            ],
                        }
                    )
                elif event["type"] == "http.response.body":
                    if not started:
                        raise RuntimeError("ASGI response body sent before response start")
                    chunk = bytes(event.get("body", b""))
                    await self._send(
                        {
                            "type": "response_body",
                            "request_id": request_id,
                            "body": base64.b64encode(chunk).decode(),
                            "more": bool(event.get("more_body", False)),
                        }
                    )

            await self._app(scope, receive, send)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            logger.warning("Relay request {} failed: {}", request_id, error)
            await self._send(
                {
                    "type": "response_error",
                    "request_id": request_id,
                    "error": tr("api.relay.request_failed"),
                }
            )

    async def _send(self, message: dict[str, object]) -> None:
        socket = self._socket
        if socket is None:
            raise RelayConnectionError(tr("api.relay.tunnel_not_connected"))
        async with self._send_lock:
            await socket.send(json.dumps(message, separators=(",", ":")))

    async def _cancel_requests(self) -> None:
        tasks = list(self._request_tasks.values())
        self._request_tasks.clear()
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
