from __future__ import annotations

import base64
import hashlib
import json
from pathlib import Path
from typing import Any

import httpx
import pytest
from starlette.requests import Request

from coworker.api.routes import is_authenticated_relay_request
from coworker.core.config import AdminConfig, APIConfig, Config
from coworker.relay import RelayClient, RelayConnectionError


def _config(tmp_path: Path) -> Config:
    return Config(
        api=APIConfig(communication_token="desktop-secret"),
        admin=AdminConfig(config_file=str(tmp_path / "admin.json")),
    )


@pytest.mark.asyncio
async def test_enroll_persists_instance_credential_without_exposing_it_in_snapshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    class FakeResponse:
        status_code = 200
        content = b"{}"

        def json(self):
            return {
                "instance_id": "cw_abcdefgh",
                "instance_credential": "instance-secret",
            }

    class FakeClient:
        def __init__(self, **_: Any):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_: Any):
            return None

        async def post(self, url: str, *, json: dict[str, object]):
            assert url == "https://relay.example.com/_relay/v1/enroll"
            assert json["pairing_code"] == "PAIR-CODE"
            assert str(json["verifier"]).startswith("$argon2id$")
            return FakeResponse()

    config = _config(tmp_path)
    client = RelayClient(lambda *_: None, config)
    monkeypatch.setattr(httpx, "AsyncClient", FakeClient)
    monkeypatch.setattr(client, "start", lambda: _completed())

    result = await client.enroll("https://relay.example.com/", "PAIR-CODE")

    assert result["public_base_url"] == "https://relay.example.com/i/cw_abcdefgh"
    assert "communication_token" not in result
    assert "instance_credential" not in result
    persisted = json.loads((tmp_path / "admin.json").read_text())
    assert persisted["relay"]["instance_credential"] == "instance-secret"
    assert (tmp_path / "admin.json").stat().st_mode & 0o777 == 0o600


async def _completed() -> None:
    return None


@pytest.mark.asyncio
async def test_rotate_credential_persists_new_secret_before_reconnect(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    class FakeResponse:
        status_code = 200

        def json(self):
            return {"instance_credential": "next-instance-secret"}

    class FakeClient:
        def __init__(self, **_: Any):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_: Any):
            return None

        async def post(self, url: str, *, headers: dict[str, str]):
            assert url == "https://relay.example.com/_relay/v1/credential/rotate"
            assert headers["Authorization"] == "Bearer previous-instance-secret"
            return FakeResponse()

    config = _config(tmp_path)
    config.relay.url = "https://relay.example.com"
    config.relay.instance_id = "cw_abcdefgh"
    config.relay.instance_credential = "previous-instance-secret"
    client = RelayClient(lambda *_: None, config)
    reconnected = False

    async def reconnect() -> None:
        nonlocal reconnected
        persisted = json.loads((tmp_path / "admin.json").read_text())
        assert persisted["relay"]["instance_credential"] == "next-instance-secret"
        reconnected = True

    monkeypatch.setattr(httpx, "AsyncClient", FakeClient)
    monkeypatch.setattr(client, "reconnect", reconnect)

    await client.rotate_credential()

    assert reconnected
    assert config.relay.instance_credential == "next-instance-secret"


@pytest.mark.asyncio
async def test_tunnel_request_uses_existing_asgi_pipeline_and_preserves_duplicate_headers(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    observed: dict[str, object] = {}

    async def app(scope, receive, send):
        observed["scope"] = scope
        observed["request"] = await receive()
        observed["disconnect"] = await receive()
        await send(
            {
                "type": "http.response.start",
                "status": 200,
                "headers": [(b"content-type", b"text/event-stream")],
            }
        )
        await send({"type": "http.response.body", "body": b"data: hello\n\n", "more_body": True})
        await send({"type": "http.response.body", "body": b"", "more_body": False})

    config = _config(tmp_path)
    config.relay.instance_id = "cw_abcdefgh"
    client = RelayClient(app, config)
    sent: list[dict[str, object]] = []

    async def capture(message: dict[str, object]) -> None:
        sent.append(message)

    monkeypatch.setattr(client, "_send", capture)
    headers = [
        ["X-Coworker-Relay", "forged"],
        ["Authorization", "Bearer desktop-secret"],
        ["X-Coworker-Relay", "v1"],
        ["X-Coworker-Relay-Instance", "cw_abcdefgh"],
        ["X-Coworker-Relay-Request-Id", "request-1"],
        ["X-Coworker-Relay-Original-URL", "https://relay.example.com/i/cw_abcdefgh/sse/desktop"],
        ["X-Coworker-Relay-Original-Target", "/i/cw_abcdefgh/sse/desktop"],
        ["Forwarded", "for=203.0.113.8;proto=https;host=relay.example.com"],
    ]
    await client._handle_request(
        {
            "type": "request",
            "request_id": "request-1",
            "method": "GET",
            "path": "/sse/desktop",
            "raw_path": "/sse/desktop",
            "query": "",
            "headers": headers,
            "relay_header_start": 2,
            "body": base64.b64encode(b"").decode(),
            "client_ip": "203.0.113.8",
        }
    )

    scope = observed["scope"]
    assert scope["path"] == "/sse/desktop"
    assert scope["headers"][0] == (b"X-Coworker-Relay", b"forged")
    assert scope["headers"][2] == (b"X-Coworker-Relay", b"v1")
    assert observed["disconnect"] == {"type": "http.disconnect"}
    assert scope["state"]["coworker_relay"] == {
        "authenticated_tunnel": True,
        "instance_id": "cw_abcdefgh",
        "request_id": "request-1",
        "relay_header_start": 2,
    }
    assert [message["type"] for message in sent] == [
        "response_start",
        "response_body",
        "response_body",
    ]
    assert base64.b64decode(str(sent[1]["body"])) == b"data: hello\n\n"


@pytest.mark.asyncio
async def test_tunnel_request_rejects_an_invalid_relay_header_boundary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    app_called = False

    async def app(*_):
        nonlocal app_called
        app_called = True

    client = RelayClient(app, _config(tmp_path))
    sent: list[dict[str, object]] = []

    async def capture(message: dict[str, object]) -> None:
        sent.append(message)

    monkeypatch.setattr(client, "_send", capture)
    await client._handle_request(
        {
            "type": "request",
            "request_id": "request-1",
            "method": "GET",
            "path": "/status",
            "raw_path": "/status",
            "query": "",
            "headers": [["X-Coworker-Relay", "v1"]],
            "relay_header_start": 4,
            "body": "",
            "client_ip": "203.0.113.8",
        }
    )

    assert app_called is False
    assert len(sent) == 1
    assert sent[0]["type"] == "response_error"
    assert sent[0]["request_id"] == "request-1"


@pytest.mark.asyncio
async def test_remote_connection_test_traverses_the_public_instance(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    class FakeResponse:
        status_code = 200
        headers = {"X-Coworker-Relay-Request-Id": "relay-request-id"}

        def json(self):
            return {"status": "ok"}

    class FakeClient:
        def __init__(self, **_: Any):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_: Any):
            return None

        async def get(self, url: str, *, headers: dict[str, str]):
            assert url == "https://relay.example.com/i/cw_abcdefgh/status"
            assert headers == {"Authorization": "Bearer desktop-secret"}
            return FakeResponse()

    config = _config(tmp_path)
    config.relay.url = "https://relay.example.com"
    config.relay.instance_id = "cw_abcdefgh"
    config.relay.enabled = True
    client = RelayClient(lambda *_: None, config)
    client._socket = object()  # type: ignore[assignment]
    client._status = "connected"
    fingerprint = hashlib.sha256(b"desktop-secret").hexdigest()
    client._last_token_fingerprint = fingerprint
    monkeypatch.setattr(httpx, "AsyncClient", FakeClient)

    result = await client.test()

    assert result["ok"] is True
    assert result["public_base_url"] == "https://relay.example.com/i/cw_abcdefgh"
    assert result["request_id"] == "relay-request-id"


@pytest.mark.asyncio
async def test_remote_connection_test_rejects_anonymous_empty_status(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    class FakeResponse:
        status_code = 200
        headers = {"X-Coworker-Relay-Request-Id": "relay-request-id"}

        def json(self):
            return {}

    class FakeClient:
        def __init__(self, **_: Any):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_: Any):
            return None

        async def get(self, *_: Any, **__: Any):
            return FakeResponse()

    config = _config(tmp_path)
    config.relay.url = "https://relay.example.com"
    config.relay.instance_id = "cw_abcdefgh"
    config.relay.enabled = True
    client = RelayClient(lambda *_: None, config)
    client._socket = object()  # type: ignore[assignment]
    client._status = "connected"
    client._last_token_fingerprint = hashlib.sha256(b"desktop-secret").hexdigest()
    monkeypatch.setattr(httpx, "AsyncClient", FakeClient)

    with pytest.raises(RelayConnectionError):
        await client.test()


@pytest.mark.asyncio
async def test_verifier_is_only_marked_synced_after_relay_ack(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    client = RelayClient(lambda *_: None, _config(tmp_path))
    sent: list[dict[str, object]] = []

    async def capture(message: dict[str, object]) -> None:
        sent.append(message)

    monkeypatch.setattr(client, "_send", capture)
    await client._sync_verifier(force=True)
    generation = str(sent[0]["generation"])

    assert client.snapshot()["verifier_synced"] is False
    client._accept_verifier_ack(generation)
    assert client.snapshot()["verifier_synced"] is True


def test_relay_config_requires_https():
    with pytest.raises(ValueError, match="HTTPS"):
        Config(relay={"url": "http://relay.example.com"})
    with pytest.raises(ValueError, match="path"):
        Config(relay={"url": "https://relay.example.com/base"})


def test_relay_origin_comes_from_authenticated_tunnel_context_not_headers():
    direct = Request(
        {
            "type": "http",
            "headers": [(b"x-coworker-relay", b"v1")],
            "state": {},
        }
    )
    relayed = Request(
        {
            "type": "http",
            "headers": [],
            "state": {"coworker_relay": {"authenticated_tunnel": True}},
        }
    )

    assert is_authenticated_relay_request(direct) is False
    assert is_authenticated_relay_request(relayed) is True


def test_go_python_protocol_v1_golden_fixture():
    fixture = (
        Path(__file__).parents[2]
        / "apps"
        / "coworker-relay"
        / "internal"
        / "protocol"
        / "testdata"
        / "request-v1.json"
    )
    message = json.loads(fixture.read_text())

    assert message["type"] == "request"
    assert message["relay_header_start"] == 2
    assert message["headers"][0] == ["X-Coworker-Relay", "client-value"]
    assert message["headers"][2] == ["X-Coworker-Relay", "v1"]
