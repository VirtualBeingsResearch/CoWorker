from __future__ import annotations

import json
import os
import secrets
import ssl
import tempfile
import time
from typing import Any

import pytest

from coworker.relay.consumer import (
    RelayConsumerError,
    RelayHttpResponse,
    relay_request,
    split_relay_base_url,
)
from coworker.relay.crypto import (
    b64encode,
    challenge_payload,
    derive_server_certificate,
    generate_communication_token,
    private_key,
    verify_text,
)
from coworker.relay.protocol import Frame, FrameDecoder, FrameType, json_bytes


def _server_tls_context(token: str, instance_id: str) -> ssl.SSLContext:
    cert_pem, key_pem, _ = derive_server_certificate(token, instance_id)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_3
    fd, cert_path = tempfile.mkstemp(suffix=".pem")
    os.write(fd, cert_pem)
    os.close(fd)
    fd, key_path = tempfile.mkstemp(suffix=".pem")
    os.write(fd, key_pem)
    os.close(fd)
    context.load_cert_chain(cert_path, key_path)
    os.unlink(cert_path)
    os.unlink(key_path)
    return context


class FakeRelay:
    """Minimal Relay v1 server: entry auth + inner TLS + virtual HTTP echo."""

    def __init__(
        self,
        token: str,
        instance_id: str,
        *,
        verify_entry: bool = True,
        verify_proof: bool = True,
        status: int = 200,
    ) -> None:
        self.token = token
        self.instance_id = instance_id
        self.verify_entry = verify_entry
        self.verify_proof = verify_proof
        self.status = status
        self.requests: list[dict[str, Any]] = []
        self._tls_context = _server_tls_context(token, instance_id)

    async def handle(self, websocket: Any) -> None:
        if not websocket.request.path.endswith("/_relay/v1/connect"):
            await websocket.close(code=1008)
            return
        connection_id = b64encode(secrets.token_bytes(12))
        nonce = b64encode(secrets.token_bytes(16))
        now = int(time.time())
        await websocket.send(
            json.dumps(
                {
                    "type": "auth_challenge",
                    "protocol_version": 1,
                    "instance_id": self.instance_id,
                    "connection_id": connection_id,
                    "nonce": nonce,
                    "epoch": 1,
                    "expires_at": now + 30,
                }
            )
        )
        proof = json.loads(await websocket.recv())
        if self.verify_entry:
            assert proof["type"] == "auth_proof"
            verify_text(
                private_key(self.token, self.instance_id, "relay-entry-auth").public_key(),
                challenge_payload(
                    "desktop",
                    self.instance_id,
                    connection_id,
                    nonce,
                    1,
                    now + 30,
                ),
                proof["signature"],
            )
        session_id = b64encode(secrets.token_bytes(24))
        await websocket.send(
            json.dumps({"type": "auth_ok", "session_id": session_id})
        )

        incoming, outgoing = ssl.MemoryBIO(), ssl.MemoryBIO()
        tls = self._tls_context.wrap_bio(incoming, outgoing, server_side=True)
        while True:
            try:
                tls.do_handshake()
                break
            except ssl.SSLWantReadError:
                await self._flush(websocket, outgoing)
                incoming.write(await websocket.recv())

        proof_nonce = b64encode(secrets.token_bytes(16))
        await self._write_frame(
            websocket,
            tls,
            outgoing,
            Frame(
                FrameType.CLIENT_PROOF_CHALLENGE,
                0,
                json_bytes(
                    {
                        "instance_id": self.instance_id,
                        "session_id": session_id,
                        "nonce": proof_nonce,
                    }
                ),
            ),
        )
        proof_frame = await self._read_frame(websocket, tls, incoming, outgoing)
        assert proof_frame.kind == FrameType.CLIENT_PROOF
        if self.verify_proof:
            payload = json.loads(proof_frame.payload)
            verify_text(
                private_key(
                    self.token, self.instance_id, "inner-client-proof"
                ).public_key(),
                "\n".join(
                    (
                        "coworker-relay-v1",
                        "inner-client",
                        self.instance_id,
                        session_id,
                        proof_nonce,
                    )
                ),
                payload["signature"],
            )
        await self._write_frame(
            websocket,
            tls,
            outgoing,
            Frame(FrameType.CLIENT_READY, 0, json_bytes({"protocol_version": 1})),
        )

        decoder = FrameDecoder()
        start: dict[str, Any] | None = None
        body = bytearray()
        while True:
            frame = await self._read_frame(websocket, tls, incoming, outgoing, decoder)
            if frame.kind == FrameType.REQUEST_START:
                start = json.loads(frame.payload)
            elif frame.kind == FrameType.REQUEST_BODY:
                body.extend(frame.payload)
            elif frame.kind == FrameType.REQUEST_END:
                break
        assert start is not None
        self.requests.append({"start": start, "body": bytes(body)})

        response = json.dumps({"echo": start["target"]}).encode("utf-8")
        await self._write_frame(
            websocket,
            tls,
            outgoing,
            Frame(
                FrameType.RESPONSE_START,
                1,
                json_bytes(
                    {
                        "status": self.status,
                        "headers": [["content-type", "application/json"]],
                    }
                ),
            ),
        )
        await self._write_frame(
            websocket, tls, outgoing, Frame(FrameType.RESPONSE_BODY, 1, response)
        )
        await self._write_frame(
            websocket, tls, outgoing, Frame(FrameType.RESPONSE_END, 1)
        )
        # 等待客户端关闭连接后退出 handler。
        try:
            await websocket.recv()
        except Exception:
            pass

    @staticmethod
    async def _flush(websocket: Any, outgoing: ssl.MemoryBIO) -> None:
        while True:
            chunk = outgoing.read(256 * 1024)
            if not chunk:
                return
            await websocket.send(chunk)

    @staticmethod
    async def _write_frame(
        websocket: Any,
        tls: ssl.SSLObject,
        outgoing: ssl.MemoryBIO,
        frame: Frame,
    ) -> None:
        plain = frame.encode()
        offset = 0
        while offset < len(plain):
            try:
                offset += tls.write(plain[offset:])
            except ssl.SSLWantWriteError:
                await FakeRelay._flush(websocket, outgoing)
        await FakeRelay._flush(websocket, outgoing)

    @staticmethod
    async def _read_frame(
        websocket: Any,
        tls: ssl.SSLObject,
        incoming: ssl.MemoryBIO,
        outgoing: ssl.MemoryBIO,
        decoder: FrameDecoder | None = None,
    ) -> Frame:
        decoder = decoder or FrameDecoder()
        while True:
            try:
                chunk = tls.read(64 * 1024)
            except ssl.SSLWantReadError:
                await FakeRelay._flush(websocket, outgoing)
                incoming.write(await websocket.recv())
                continue
            frames = decoder.feed(chunk)
            if frames:
                return frames[0]


async def _run_against_fake_relay(
    relay: FakeRelay,
    token: str,
    instance_id: str,
    **kwargs: Any,
) -> RelayHttpResponse | RelayConsumerError:
    from websockets.asyncio.server import serve

    async with serve(relay.handle, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        try:
            return await relay_request(
                base_url=f"http://127.0.0.1:{port}/i/{instance_id}",
                token=token,
                method="POST",
                target="/messages",
                headers=[("authorization", f"Bearer {token}")],
                body=b'{"content":"hi"}',
                timeout_seconds=15.0,
                **kwargs,
            )
        except RelayConsumerError as error:
            return error


@pytest.mark.asyncio
async def test_relay_request_roundtrip_against_fake_relay() -> None:
    token = generate_communication_token()
    relay = FakeRelay(token, "cw_fakeinstance01")
    result = await _run_against_fake_relay(relay, token, "cw_fakeinstance01")
    assert isinstance(result, RelayHttpResponse), getattr(result, "message_key", None)
    assert result.status == 200
    assert json.loads(result.body) == {"echo": "/messages"}

    request = relay.requests[0]
    assert request["start"]["method"] == "POST"
    assert request["start"]["path"] == "/messages"
    assert request["start"]["target"] == "/messages"
    assert ["authorization", f"Bearer {token}"] in request["start"]["headers"]
    assert request["body"] == b'{"content":"hi"}'


@pytest.mark.asyncio
async def test_relay_request_rejects_mismatched_token() -> None:
    server_token = generate_communication_token()
    client_token = generate_communication_token()
    relay = FakeRelay(server_token, "cw_fakeinstance01", verify_entry=False)
    result = await _run_against_fake_relay(relay, client_token, "cw_fakeinstance01")
    # 令牌不匹配时：入口签名或内层 TLS 身份必有一处失败，错误需可定位。
    assert isinstance(result, RelayConsumerError)
    assert result.message_key in {
        "tool_result.communicate.coworker_relay_rejected",
        "tool_result.communicate.coworker_relay_unreachable",
        "tool_result.communicate.coworker_relay_identity",
    }


@pytest.mark.asyncio
async def test_relay_request_maps_http_error_status() -> None:
    token = generate_communication_token()
    relay = FakeRelay(token, "cw_fakeinstance01", status=401)
    result = await _run_against_fake_relay(relay, token, "cw_fakeinstance01")
    assert isinstance(result, RelayHttpResponse)
    assert result.status == 401


@pytest.mark.asyncio
async def test_relay_token_must_be_relay_safe() -> None:
    with pytest.raises(RelayConsumerError) as exc_info:
        await relay_request(
            base_url="http://relay.example/i/cw_fakeinstance01",
            token="not-a-cwct-token",
            method="POST",
            target="/messages",
            headers=[],
        )
    assert exc_info.value.message_key == (
        "tool_result.communicate.coworker_relay_token_invalid"
    )


def test_split_relay_base_url_valid_and_invalid() -> None:
    origin, instance_id = split_relay_base_url(
        "https://relay.example.com:8443/i/cw_abc12345"
    )
    assert origin == "wss://relay.example.com:8443"
    assert instance_id == "cw_abc12345"

    for invalid in (
        "https://relay.example.com/",
        "https://relay.example.com/i/",
        "https://relay.example.com/i/cw_short/i/extra",
        "https://relay.example.com/i/not-an-instance",
        "ftp://relay.example.com/i/cw_abc12345",
    ):
        with pytest.raises(RelayConsumerError):
            split_relay_base_url(invalid)
