from __future__ import annotations

import base64
import json
from pathlib import Path

import httpx
import pytest

from coworker.agent.incoming_content import format_event_text
from coworker.channels.access import (
    ChannelAccessController,
    ChannelAccessDeniedError,
)
from coworker.channels.activity import ChannelActivityStore
from coworker.channels.coworker import (
    CoworkerAnnounce,
    CoworkerChannel,
    CoworkerPeerStore,
    CoworkerRuntime,
    resolve_coworker_self_id,
)
from coworker.channels.inbound import AttachmentStore, InboundEnvelope
from coworker.channels.registry import ChannelRegistry
from coworker.core.config import CoworkerPeerConfig
from coworker.core.types import CommunicateRequest, IncomingEvent
from coworker.i18n import locale_context


async def _noop_handler(event: object) -> None:
    return None


def _peer_config(
    base_url: str = "http://127.0.0.1:8001",
    token: str = "peer-token",
    display_name: str = "",
) -> CoworkerPeerConfig:
    return CoworkerPeerConfig(
        base_url=base_url,
        token=token,
        display_name=display_name,
    )


def _channel(
    tmp_path: Path,
    *,
    peers: dict[str, CoworkerPeerConfig] | None = None,
    announce: CoworkerAnnounce | None = None,
    client: httpx.AsyncClient | None = None,
    activity: ChannelActivityStore | None = None,
    max_attachment_bytes: int = 10 * 1024 * 1024,
) -> CoworkerChannel:
    return CoworkerChannel(
        self_id="cw_self",
        peers=peers or {},
        learned=CoworkerPeerStore(tmp_path / "coworker_peers.json"),
        attachments=AttachmentStore(tmp_path / "attachments"),
        announce=announce,
        max_attachment_bytes=max_attachment_bytes,
        runtime=CoworkerRuntime(client=client),
        activity=activity or ChannelActivityStore(),
    )


def _registry(channel: CoworkerChannel) -> ChannelRegistry:
    registry = ChannelRegistry()
    registry.register(channel)
    return registry


def _requests_seen(
    handler_requests: list[httpx.Request],
) -> dict[str, object]:
    assert handler_requests, "no outbound request was made"
    request = handler_requests[0]
    return {
        "url": str(request.url),
        "authorization": request.headers.get("Authorization"),
        "json": json.loads(request.content.decode("utf-8")),
    }


def _mock_client(
    handler_requests: list[httpx.Request],
    handler,
) -> httpx.AsyncClient:
    def transport(request: httpx.Request) -> httpx.Response:
        handler_requests.append(request)
        return handler(request)

    return httpx.AsyncClient(transport=httpx.MockTransport(transport))


# --- self_id -----------------------------------------------------------------


def test_resolve_coworker_self_id_generates_and_persists(tmp_path: Path) -> None:
    identity_dir = tmp_path / "identity"

    generated = resolve_coworker_self_id(identity_dir)
    assert generated.startswith("cw_")
    assert (identity_dir / "coworker_self_id.txt").read_text(encoding="utf-8") == generated
    assert resolve_coworker_self_id(identity_dir) == generated
    assert resolve_coworker_self_id(identity_dir, configured="ava") == "ava"
    with pytest.raises(ValueError):
        resolve_coworker_self_id(identity_dir, configured="Not Valid")


# --- peer store ---------------------------------------------------------------


def test_peer_store_upsert_conflict_and_reload(tmp_path: Path) -> None:
    path = tmp_path / "coworker_peers.json"
    store = CoworkerPeerStore(path)
    assert store.get("ava") is None

    conflict = store.upsert(
        "ava", base_url="http://127.0.0.1:8001", token="t1", display_name="Ava"
    )
    assert conflict is False

    conflict = store.upsert(
        "ava", base_url="http://127.0.0.1:9001", token="t1", display_name="Ava"
    )
    assert conflict is True

    reloaded = CoworkerPeerStore(path)
    learned = reloaded.get("ava")
    assert learned is not None
    assert learned.base_url == "http://127.0.0.1:9001"
    assert learned.token == "t1"
    assert learned.display_name == "Ava"
    assert reloaded.peer_ids() == ["ava"]


# --- outbound -----------------------------------------------------------------


@pytest.mark.asyncio
async def test_send_posts_announce_and_token(tmp_path: Path) -> None:
    seen: list[httpx.Request] = []
    client = _mock_client(
        seen,
        lambda request: httpx.Response(200, json={"status": "queued"}),
    )
    channel = _channel(
        tmp_path,
        peers={"ava": _peer_config()},
        announce=CoworkerAnnounce(
            base_url="http://127.0.0.1:8000",
            token="my-inbound-token",
            display_name="Me",
        ),
        client=client,
    )

    result = await channel.send(
        CommunicateRequest(
            participant_id="coworker:ava",
            message="hello",
            conversation_id="conv-1",
        )
    )
    assert result.is_error is False
    payload = _requests_seen(seen)
    assert payload["url"] == "http://127.0.0.1:8001/messages"
    assert payload["authorization"] == "Bearer peer-token"
    body = payload["json"]
    assert isinstance(body, dict)
    assert body["sender_id"] == "coworker:cw_self"
    assert body["content"] == "hello"
    assert body["conversation_id"] == "conv-1"
    assert body["coworker_peer"] == {
        "base_url": "http://127.0.0.1:8000",
        "token": "my-inbound-token",
        "display_name": "Me",
    }


@pytest.mark.asyncio
async def test_send_reports_unauthorized_and_unreachable(tmp_path: Path) -> None:
    seen: list[httpx.Request] = []
    unauthorized = _channel(
        tmp_path,
        peers={"ava": _peer_config()},
        client=_mock_client(seen, lambda request: httpx.Response(401)),
    )
    result = await unauthorized.send(
        CommunicateRequest(participant_id="coworker:ava", message="hi")
    )
    assert result.is_error is True
    assert "Bearer" in result.content

    def raise_connect_error(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    unreachable = _channel(
        tmp_path,
        peers={"ava": _peer_config()},
        client=_mock_client([], raise_connect_error),
    )
    result = await unreachable.send(
        CommunicateRequest(participant_id="coworker:ava", message="hi")
    )
    assert result.is_error is True
    assert "8001" in result.content


@pytest.mark.asyncio
async def test_send_encodes_attachments_and_enforces_limit(tmp_path: Path) -> None:
    seen: list[httpx.Request] = []
    channel = _channel(
        tmp_path,
        peers={"ava": _peer_config()},
        client=_mock_client(seen, lambda request: httpx.Response(200)),
    )
    attachment_path = tmp_path / "note.txt"
    attachment_path.write_text("peer-data", encoding="utf-8")

    result = await channel.send(
        CommunicateRequest(
            participant_id="coworker:ava",
            message="see attachment",
            attachments=[
                {
                    "type": "file",
                    "filename": "note.txt",
                    "media_type": "text/plain",
                    "path": str(attachment_path),
                }
            ],
        )
    )
    assert result.is_error is False
    payload = _requests_seen(seen)
    body = payload["json"]
    assert isinstance(body, dict)
    encoded = body["attachments"][0]
    assert encoded["filename"] == "note.txt"
    assert base64.b64decode(encoded["data"]) == b"peer-data"

    limited = _channel(
        tmp_path,
        peers={"ava": _peer_config()},
        client=_mock_client([], lambda request: httpx.Response(200)),
        max_attachment_bytes=4,
    )
    result = await limited.send(
        CommunicateRequest(
            participant_id="coworker:ava",
            message="too big",
            attachments=[{"path": str(attachment_path)}],
        )
    )
    assert result.is_error is True
    assert "note.txt" in result.content


@pytest.mark.asyncio
async def test_registry_rejects_unknown_peer(tmp_path: Path) -> None:
    channel = _channel(tmp_path, peers={"ava": _peer_config()})
    registry = _registry(channel)

    result = await registry.send(
        CommunicateRequest(participant_id="coworker:ghost", message="hi")
    )
    assert result.is_error is True
    assert "coworker:ghost" in result.content


# --- inbound / learning --------------------------------------------------------


@pytest.mark.asyncio
async def test_receive_raw_learns_peer_strips_announce_saves_attachment(
    tmp_path: Path,
) -> None:
    events: list[IncomingEvent] = []

    async def capture(event: IncomingEvent) -> None:
        events.append(event)

    channel = _channel(tmp_path)
    channel.set_inbound_handler(capture)

    await channel.receive_raw(
        InboundEnvelope(
            participant_id="coworker:ava",
            source="rest",
            payload={
                "sender_id": "coworker:ava",
                "content": "hello from ava",
                "conversation_id": "conv-9",
                "coworker_peer": {
                    "base_url": "http://10.0.0.5:8001/",
                    "token": "ava-token",
                    "display_name": "Ava",
                },
                "attachments": [
                    {
                        "filename": "img.png",
                        "media_type": "image/png",
                        "data": base64.b64encode(b"png-bytes").decode("ascii"),
                    }
                ],
            },
        )
    )

    assert len(events) == 1
    event = events[0]
    assert event.participant_id == "coworker:ava"
    assert event.source == "coworker"
    assert event.conversation_id == "conv-9"
    assert event.content == "hello from ava"
    assert len(event.attachments) == 1
    assert Path(event.attachments[0].saved_path).read_bytes() == b"png-bytes"

    learned = channel._learned.get("ava")
    assert learned is not None
    assert learned.base_url == "http://10.0.0.5:8001"
    assert learned.token == "ava-token"

    with locale_context("zh-CN"):
        text = format_event_text(event)
    assert "coworker:ava" in text
    assert "hello from ava" in text


@pytest.mark.asyncio
async def test_explicit_peer_is_not_overwritten_by_announce(tmp_path: Path) -> None:
    channel = _channel(
        tmp_path,
        peers={"ava": _peer_config(base_url="http://configured:8001")},
    )
    channel.set_inbound_handler(_noop_handler)

    await channel.receive_raw(
        InboundEnvelope(
            participant_id="coworker:ava",
            source="rest",
            payload={
                "content": "hi",
                "coworker_peer": {"base_url": "http://announced:9999", "token": "t"},
            },
        )
    )
    assert channel._learned.get("ava") is None


@pytest.mark.asyncio
async def test_reply_uses_learned_announce_single_sided(tmp_path: Path) -> None:
    seen: list[httpx.Request] = []
    channel = _channel(
        tmp_path,
        announce=CoworkerAnnounce(base_url="http://127.0.0.1:8000"),
        client=_mock_client(seen, lambda request: httpx.Response(200)),
    )
    channel.set_inbound_handler(_noop_handler)
    await channel.receive_raw(
        InboundEnvelope(
            participant_id="coworker:ava",
            source="rest",
            payload={
                "content": "hello",
                "coworker_peer": {"base_url": "http://10.0.0.5:8001", "token": "ava-token"},
            },
        )
    )

    result = await channel.send(
        CommunicateRequest(participant_id="coworker:ava", message="reply")
    )
    assert result.is_error is False
    payload = _requests_seen(seen)
    assert payload["url"] == "http://10.0.0.5:8001/messages"
    assert payload["authorization"] == "Bearer ava-token"
    body = payload["json"]
    assert isinstance(body, dict)
    assert body["coworker_peer"] == {"base_url": "http://127.0.0.1:8000"}


def test_list_connections_merges_explicit_and_learned(tmp_path: Path) -> None:
    channel = _channel(
        tmp_path,
        peers={"ava": _peer_config(display_name="Ava")},
    )
    channel.set_inbound_handler(_noop_handler)

    connections = {info.participant_id: info for info in channel.list_connections()}
    assert set(connections) == {"coworker:ava"}


@pytest.mark.asyncio
async def test_learned_peer_appears_in_list_connections(tmp_path: Path) -> None:
    channel = _channel(tmp_path)
    channel.set_inbound_handler(_noop_handler)
    await channel.receive_raw(
        InboundEnvelope(
            participant_id="coworker:bob",
            source="rest",
            payload={
                "content": "hi",
                "coworker_peer": {"base_url": "http://10.0.0.9:8002"},
            },
        )
    )

    connections = {info.participant_id: info for info in channel.list_connections()}
    assert set(connections) == {"coworker:bob"}
    assert connections["coworker:bob"].channel == "coworker"


# --- access control ------------------------------------------------------------


@pytest.mark.asyncio
async def test_denied_inbound_is_not_learned(tmp_path: Path) -> None:
    channel = _channel(tmp_path)
    registry = ChannelRegistry(access=_deny_access("inbound"))
    registry.register(channel)
    registry.set_inbound_handler(_noop_handler)

    with pytest.raises(ChannelAccessDeniedError):
        await registry.receive_raw(
            InboundEnvelope(
                participant_id="coworker:ava",
                source="rest",
                payload={
                    "content": "hi",
                    "coworker_peer": {"base_url": "http://10.0.0.5:8001"},
                },
            )
        )
    assert channel._learned.get("ava") is None


@pytest.mark.asyncio
async def test_denied_outbound_is_refused(tmp_path: Path) -> None:
    seen: list[httpx.Request] = []
    channel = _channel(
        tmp_path,
        peers={"ava": _peer_config()},
        client=_mock_client(seen, lambda request: httpx.Response(200)),
    )
    registry = ChannelRegistry(access=_deny_access("outbound"))
    registry.register(channel)

    result = await registry.send(
        CommunicateRequest(participant_id="coworker:ava", message="hi")
    )
    assert result.is_error is True
    assert seen == []


def _deny_access(direction: str) -> ChannelAccessController:
    from coworker.core.config import ChannelAccessConfig

    config = ChannelAccessConfig.model_validate(
        {"coworker": {f"{direction}_deny": ["coworker:*"]}}
    )
    return ChannelAccessController(config)


# --- config validation ---------------------------------------------------------


def _reset_routes_auth_state() -> None:
    from coworker.api import routes

    routes._coworker_inbound_token = ""
    routes._communication_token = ""
    routes._communication_token_explicit = False
    routes._extra_communication_tokens = {}


def test_coworker_peer_auth_accepts_inbound_token(monkeypatch: pytest.MonkeyPatch) -> None:
    from fastapi import HTTPException

    from coworker.api import routes

    _reset_routes_auth_state()
    monkeypatch.setattr(routes, "_coworker_inbound_token", "cw-inbound")

    # 专用入站令牌通过，即使没有配置任何主通信令牌。
    routes._verify_coworker_peer_authorization("Bearer cw-inbound")
    with pytest.raises(HTTPException) as exc_info:
        routes._verify_coworker_peer_authorization("Bearer wrong")
    assert exc_info.value.status_code == 401
    with pytest.raises(HTTPException):
        routes._verify_coworker_peer_authorization(None)

    # 主令牌对搭档消息同样有效；专用令牌在主令牌存在时依然有效（轮换互不影响）。
    monkeypatch.setattr(routes, "_communication_token", "primary-token")
    routes._verify_coworker_peer_authorization("Bearer primary-token")
    routes._verify_coworker_peer_authorization("Bearer cw-inbound")
    with pytest.raises(HTTPException):
        routes._verify_coworker_peer_authorization("Bearer wrong")

    _reset_routes_auth_state()


# --- relay transport ------------------------------------------------------------


@pytest.mark.asyncio
async def test_relay_shaped_peer_uses_relay_transport(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from coworker.channels import coworker as coworker_module
    from coworker.relay.consumer import RelayHttpResponse

    seen: dict[str, object] = {}

    async def fake_relay_request(**kwargs: object) -> RelayHttpResponse:
        seen.update(kwargs)
        return RelayHttpResponse(status=200, headers=[], body=b"")

    monkeypatch.setattr(coworker_module, "relay_request", fake_relay_request)
    channel = _channel(
        tmp_path,
        peers={
            "bob": _peer_config(
                base_url="http://relay.example.com:8443/i/cw_remote0001",
                token="cwct_v1_" + base64.b64encode(b"x" * 32).decode().rstrip("="),
            )
        },
        announce=CoworkerAnnounce(base_url="http://127.0.0.1:8000"),
    )

    result = await channel.send(
        CommunicateRequest(participant_id="coworker:bob", message="via relay")
    )
    assert result.is_error is False
    assert seen["base_url"] == "http://relay.example.com:8443/i/cw_remote0001"
    assert seen["method"] == "POST"
    assert seen["target"] == "/messages"
    body = json.loads(seen["body"])  # type: ignore[arg-type]
    assert body["content"] == "via relay"
    assert body["coworker_peer"] == {"base_url": "http://127.0.0.1:8000"}


@pytest.mark.asyncio
async def test_relay_consumer_error_is_localized(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from coworker.channels import coworker as coworker_module
    from coworker.relay.consumer import RelayConsumerError

    async def failing_relay_request(**kwargs: object) -> None:
        raise RelayConsumerError(
            "tool_result.communicate.coworker_relay_identity",
            participant="coworker:bob",
        )

    monkeypatch.setattr(coworker_module, "relay_request", failing_relay_request)
    channel = _channel(
        tmp_path,
        peers={
            "bob": _peer_config(
                base_url="http://relay.example.com:8443/i/cw_remote0001",
                token="cwct_v1_" + base64.b64encode(b"x" * 32).decode().rstrip("="),
            )
        },
    )

    result = await channel.send(
        CommunicateRequest(participant_id="coworker:bob", message="hi")
    )
    assert result.is_error is True
    assert "coworker:bob" in result.content


def test_relay_peer_requires_cwct_token() -> None:
    from coworker.core.config import CoworkerConfig

    with pytest.raises(ValueError):
        CoworkerConfig.model_validate(
            {
                "peers": {
                    "bob": {
                        "base_url": "http://relay.example.com:8443/i/cw_remote0001",
                        "token": "weak-token",
                    }
                }
            }
        )


def test_coworker_config_validation() -> None:
    from coworker.core.config import CoworkerConfig

    valid = CoworkerConfig.model_validate(
        {
            "self_id": "ava",
            "peers": {"bob": {"base_url": "http://127.0.0.1:8002"}},
        }
    )
    assert valid.peers["bob"].base_url == "http://127.0.0.1:8002"

    with pytest.raises(ValueError):
        CoworkerConfig.model_validate({"peers": {"Not-Valid": {"base_url": "http://x"}}})
    with pytest.raises(ValueError):
        CoworkerConfig.model_validate({"self_id": "Not Valid"})
    with pytest.raises(ValueError):
        CoworkerConfig.model_validate({"peers": {"bob": {"base_url": "not-a-url"}}})
