from __future__ import annotations

import json
from pathlib import Path
from uuid import UUID

import httpx
import pytest
from PIL import Image

from coworker.channels.base import ConnectionInfo
from coworker.channels.registry import PreparedChannelAction
from coworker.channels.weixin.action import WeixinChannelAction
from coworker.channels.weixin.channel import WeixinChannel
from coworker.channels.weixin.client import WeixinClient, credentials_from_login
from coworker.channels.weixin.runner import WeixinRunner
from coworker.core.config import WeixinAccountConfig, WeixinConfig
from coworker.core.types import CommunicateRequest, ToolResult

ACCOUNT_ID = UUID("7ad0bbf4-93d2-4807-94ca-f6de629095de")


def _config() -> WeixinConfig:
    return WeixinConfig(
        enabled=True,
        accounts=[
            WeixinAccountConfig(
                id=ACCOUNT_ID,
                name="personal",
                bot_id="bot-1",
                token="secret-token",
            )
        ],
    )


class _ConnectionSource:
    def __init__(self, kind: str = "stream:private") -> None:
        self.kind = kind

    def list_connections(self) -> list[ConnectionInfo]:
        return [
            ConnectionInfo(
                participant_id="recipient:user-1",
                channel="stream",
                kind=self.kind,
                active=True,
            )
        ]


class _ActionRunner:
    async def start_login(self) -> dict[str, str]:
        return {
            "session_id": "connection-session-1",
            "qrcode_content": "https://example.test/pair",
            "qrcode_path": "pairing.png",
        }


@pytest.mark.asyncio
async def test_client_uses_ilink_auth_headers_and_message_shape() -> None:
    requests: list[httpx.Request] = []

    async def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"ret": 0})

    client = WeixinClient(
        token="secret-token",
        transport=httpx.MockTransport(handle),
    )
    await client.send_text("user-1", "hello", "context-1")
    await client.close()

    request = requests[0]
    payload = json.loads(request.content)
    assert request.url.path == "/ilink/bot/sendmessage"
    assert request.headers["authorization"] == "Bearer secret-token"
    assert request.headers["authorizationtype"] == "ilink_bot_token"
    assert request.headers["ilink-app-id"] == "bot"
    assert payload["msg"]["to_user_id"] == "user-1"
    assert payload["msg"]["context_token"] == "context-1"
    assert payload["msg"]["item_list"][0]["text_item"]["text"] == "hello"


def test_confirmed_login_requires_complete_credentials() -> None:
    credentials = credentials_from_login(
        {
            "status": "confirmed",
            "ilink_bot_id": "bot-1",
            "bot_token": "token-1",
            "ilink_user_id": "owner-1",
        }
    )

    assert credentials is not None
    assert credentials.bot_id == "bot-1"
    assert credentials.token == "token-1"
    assert credentials.user_id == "owner-1"
    assert credentials_from_login({"status": "wait"}) is None
    assert credentials_from_login({"status": "confirmed", "ilink_bot_id": "bot-1"}) is None


@pytest.mark.asyncio
async def test_runner_isolates_participants_by_account_and_persists_context(
    tmp_path: Path,
) -> None:
    runner = WeixinRunner(_config(), tmp_path / "weixin-state.json")
    events = []

    async def collect(event: object) -> None:
        events.append(event)

    runner.set_inbound_handler(collect)

    await runner._publish_message(  # noqa: SLF001
        str(ACCOUNT_ID),
        {
            "message_type": 1,
            "from_user_id": "user-1",
            "context_token": "context-1",
            "message_id": "message-1",
            "item_list": [{"type": 1, "text_item": {"text": "hello"}}],
        },
    )

    participant_id = f"weixin:{ACCOUNT_ID}:user-1"
    assert events[0].participant_id == participant_id
    assert events[0].source == "weixin"
    assert events[0].content == "hello"
    assert runner.participant_ids() == [participant_id]
    assert runner.resolve_participant("user-1") == participant_id


@pytest.mark.asyncio
async def test_start_login_creates_real_png_for_admin_and_chat(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class LoginClient:
        async def start_login(self, local_tokens: list[str]) -> dict[str, str]:
            assert local_tokens == ["secret-token"]
            return {
                "qrcode": "session-secret",
                "qrcode_img_content": "https://example.test/pair/session-secret",
            }

        async def poll_login(
            self,
            qrcode: str,
            verify_code: str,
        ) -> dict[str, str]:
            assert qrcode == "session-secret"
            assert verify_code == ""
            return {
                "status": "confirmed",
                "ilink_bot_id": "bot-new",
                "bot_token": "token-new",
            }

        async def close(self) -> None:
            return None

    runner = WeixinRunner(_config(), tmp_path / "weixin-state.json")
    monkeypatch.setattr(
        "coworker.channels.weixin.runner.WeixinClient",
        lambda *args, **kwargs: LoginClient(),
    )

    result = await runner.start_login()

    image_path = Path(result["qrcode_path"])
    assert result["qrcode_data_url"].startswith("data:image/png;base64,")
    assert result["qrcode_content"] == "https://example.test/pair/session-secret"
    assert result["session_id"]
    assert image_path.is_file()
    with Image.open(image_path) as image:
        assert image.format == "PNG"
        assert image.width > 100

    second = await runner.start_login()
    assert second["session_id"] != result["session_id"]
    assert second["qrcode_path"] != result["qrcode_path"]
    confirmed = await runner.poll_login(result["session_id"])
    assert confirmed["status"] == "confirmed"
    assert not image_path.exists()
    assert Path(second["qrcode_path"]).exists()


@pytest.mark.asyncio
async def test_connect_action_prepares_qrcode_for_generic_communicate() -> None:
    action = WeixinChannelAction(
        _ActionRunner(),  # type: ignore[arg-type]
        _ConnectionSource(),  # type: ignore[arg-type]
    )

    prepared = await action(
        CommunicateRequest(
            participant_id="recipient:user-1",
            extra={"channel_action": {"channel": "weixin", "type": "connect"}},
        )
    )

    assert isinstance(prepared, PreparedChannelAction)
    assert prepared.request.attachments == [{"type": "image", "path": "pairing.png"}]
    assert prepared.request.extra["channel_action"]["session_id"] == "connection-session-1"
    assert "https://example.test/pair" in prepared.request.message
    assert "connection-session-1" in prepared.result_note


@pytest.mark.asyncio
async def test_connect_action_rejects_group_delivery() -> None:
    action = WeixinChannelAction(
        _ActionRunner(),  # type: ignore[arg-type]
        _ConnectionSource("stream:group"),  # type: ignore[arg-type]
    )

    result = await action(
        CommunicateRequest(
            participant_id="recipient:user-1",
            extra={"channel_action": {"channel": "weixin", "type": "connect"}},
        )
    )

    assert isinstance(result, ToolResult)
    assert result.is_error


@pytest.mark.asyncio
async def test_channel_routes_send_and_reports_multi_account_participant() -> None:
    class Runtime:
        sent: list[tuple[str, str]] = []

        async def send(self, participant_id: str, message: str) -> None:
            self.sent.append((participant_id, message))

        def participant_ids(self) -> list[str]:
            return [f"weixin:{ACCOUNT_ID}:user-1"]

        def activity_for(self, participant_id: str) -> tuple[None, None]:
            return None, None

        def is_account_active(self, account_id: str) -> bool:
            return account_id == str(ACCOUNT_ID)

        def resolve_participant(self, participant_id: str) -> str | None:
            return f"weixin:{ACCOUNT_ID}:{participant_id}"

        def set_inbound_handler(self, handler: object) -> None:
            return None

    runtime = Runtime()
    channel = WeixinChannel(runtime)  # type: ignore[arg-type]
    participant_id = f"weixin:{ACCOUNT_ID}:user-1"

    result = await channel.send(
        CommunicateRequest(participant_id=participant_id, message="hello")
    )

    assert not result.is_error
    assert runtime.sent == [(participant_id, "hello")]
    assert channel.list_connections()[0].active
    assert channel.resolve("user-1") == participant_id
