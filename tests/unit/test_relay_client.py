from __future__ import annotations

import json
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization

from coworker.core.config import Config
from coworker.relay.client import (
    RelayClient,
    RelayConnectionError,
    _validated_headers,
    _websocket_url,
)
from coworker.relay.crypto import (
    TOKEN_PREFIX,
    derive_server_certificate,
    generate_communication_token,
    is_relay_safe_token,
    private_key,
    public_key_text,
)
from coworker.relay.protocol import Frame, FrameDecoder, FrameType

_VECTOR_TOKEN = "cwct_v1_AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"


def _config(tmp_path: Path) -> Config:
    return Config(
        admin={"config_file": str(tmp_path / "admin.json")},
        api={"communication_token": _VECTOR_TOKEN},
    )


def test_generated_token_has_a_versioned_256_bit_format():
    token = generate_communication_token()
    assert token.startswith(TOKEN_PREFIX)
    assert is_relay_safe_token(token)
    assert not is_relay_safe_token("legacy-secret")
    assert not is_relay_safe_token("cwct_v1_short")


def test_cross_language_hkdf_ed25519_vectors_are_domain_separated():
    expected = {
        "relay-entry-auth": "_HrMmpvtYBpH-TfU1g5CzpFIz-JkUzOc9q8TTm_RRJw",
        "inner-tls-server": "VrPGKrcJDU85egv1SpAAy0H1-T0LuuZDU7jAfkfz7rE",
        "inner-client-proof": "OGxYF17x1eB3sAmxqoKY3jg7HIC8i0KJoxl6GouwYUQ",
    }
    observed = {
        purpose: public_key_text(private_key(_VECTOR_TOKEN, "cw_abcdefgh", purpose))
        for purpose in expected
    }
    assert observed == expected
    assert len(set(observed.values())) == len(observed)


def test_inner_tls_certificate_contains_the_derived_server_key():
    certificate, private_pem, pin = derive_server_certificate(
        _VECTOR_TOKEN, "cw_abcdefgh"
    )
    assert b"BEGIN CERTIFICATE" in certificate
    loaded = serialization.load_pem_private_key(private_pem, password=None)
    assert public_key_text(loaded) == pin
    assert pin == "VrPGKrcJDU85egv1SpAAy0H1-T0LuuZDU7jAfkfz7rE"


def test_binary_frames_are_incrementally_decoded_and_bounded():
    first = Frame(FrameType.REQUEST_START, 7, b'{"method":"GET"}').encode()
    second = Frame(FrameType.REQUEST_END, 7).encode()
    decoder = FrameDecoder()
    assert decoder.feed(first[:4]) == []
    frames = decoder.feed(first[4:] + second)
    assert [(frame.kind, frame.stream_id) for frame in frames] == [
        (FrameType.REQUEST_START, 7),
        (FrameType.REQUEST_END, 7),
    ]
    corrupted = bytearray(first)
    corrupted[0] = 99
    with pytest.raises(ValueError, match="protocol|header"):
        FrameDecoder().feed(corrupted)


def test_duplicate_client_headers_are_preserved_in_order():
    headers = _validated_headers(
        [
            ["X-Coworker-Relay", "forged"],
            ["Authorization", "Bearer secret"],
            ["X-Coworker-Relay", "forged-again"],
        ]
    )
    assert headers == [
        (b"x-coworker-relay", b"forged"),
        (b"authorization", b"Bearer secret"),
        (b"x-coworker-relay", b"forged-again"),
    ]
    with pytest.raises(RelayConnectionError):
        _validated_headers([["x-test", "bad\r\nheader"]])


def test_relay_config_accepts_http_origin_but_rejects_paths_and_credentials():
    assert Config(relay={"url": "http://relay.example.com:8443"}).relay.url.startswith(
        "http://"
    )
    with pytest.raises(ValueError):
        Config(relay={"url": "http://relay.example.com/base"})
    with pytest.raises(ValueError):
        Config(relay={"url": "http://user@relay.example.com"})
    assert (
        _websocket_url("http://relay.example.com:8443", "/_relay/v1/pair")
        == "ws://relay.example.com:8443/_relay/v1/pair"
    )


@pytest.mark.asyncio
async def test_missing_token_is_generated_before_pairing_attempt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    config = Config(admin={"config_file": str(tmp_path / "admin.json")})
    client = RelayClient(lambda *_: None, config)

    class RefuseConnection:
        async def __aenter__(self):
            raise OSError("offline")

        async def __aexit__(self, *_):
            return None

    monkeypatch.setattr(
        "coworker.relay.client.connect",
        lambda *_args, **_kwargs: RefuseConnection(),
    )
    code = "pair_abcdefghijkl." + "A" * 43
    with pytest.raises(RelayConnectionError, match="offline|连接"):
        await client.enroll("http://relay.example.com:8443", code)
    assert is_relay_safe_token(config.api.communication_token)
    persisted = json.loads((tmp_path / "admin.json").read_text())
    assert persisted["api"]["communication_token"] == config.api.communication_token


@pytest.mark.asyncio
async def test_legacy_explicit_token_blocks_relay_enrollment(tmp_path: Path):
    config = Config(
        admin={"config_file": str(tmp_path / "admin.json")},
        api={"communication_token": "human-password"},
    )
    client = RelayClient(lambda *_: None, config)
    code = "pair_abcdefghijkl." + "A" * 43
    with pytest.raises(RelayConnectionError, match="高熵|high-entropy"):
        await client.enroll("http://relay.example.com:8443", code)

    snapshot = client.snapshot()
    assert snapshot["communication_token_compatible"] is False


@pytest.mark.asyncio
async def test_legacy_token_can_be_explicitly_rotated_before_pairing(tmp_path: Path):
    config = Config(
        admin={"config_file": str(tmp_path / "admin.json")},
        api={"communication_token": "human-password"},
    )
    client = RelayClient(lambda *_: None, config)

    result = await client.rotate_token()

    assert result["desktop_reconfiguration_required"] is True
    assert result["communication_token_compatible"] is True
    assert is_relay_safe_token(config.api.communication_token)
    persisted = json.loads((tmp_path / "admin.json").read_text())
    assert persisted["api"]["communication_token"] == config.api.communication_token


@pytest.mark.asyncio
async def test_token_rotation_persists_new_token_and_requires_desktop_update(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    config = _config(tmp_path)
    config.relay.instance_id = "cw_abcdefgh"
    client = RelayClient(lambda *_: None, config)

    async def reconnect() -> None:
        assert client._pending_token is not None
        client._commit_token_rotation(client._pending_token)

    monkeypatch.setattr(client, "reconnect", reconnect)
    result = await client.rotate_token()
    assert is_relay_safe_token(config.api.communication_token)
    assert config.api.communication_token != _VECTOR_TOKEN
    assert result["desktop_reconfiguration_required"] is True
    from coworker.api import routes

    assert routes._communication_token == config.api.communication_token
