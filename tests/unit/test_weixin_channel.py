from __future__ import annotations

import asyncio
import base64
import io
import json
import logging
from pathlib import Path
from unittest.mock import AsyncMock

import httpx
import pytest
from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from PIL import Image

from coworker.channels.access import ChannelAccessController
from coworker.channels.weixin.channel import CONTROL_PARTICIPANT_ID, WeixinChannel
from coworker.channels.weixin.client import (
    WeixinClient,
    WeixinCredentials,
    credentials_from_login,
)
from coworker.channels.weixin.connections import WeixinConnectionManager
from coworker.channels.weixin.logging import configure_weixin_polling_logs
from coworker.channels.weixin.media import WeixinMediaTooLargeError
from coworker.channels.weixin.module import WeixinManagement, create_weixin_module
from coworker.channels.weixin.repository import (
    WeixinConnection,
    WeixinConnectionRepository,
)
from coworker.channels.weixin.runner import WeixinRunner
from coworker.core.config import ChannelAccessConfig, WeixinConfig
from coworker.core.types import CommunicateRequest, IncomingEvent


def _connection() -> WeixinConnection:
    return WeixinConnection(
        bot_instance_id="bot-1",
        token="secret-token",
        weixin_user_id="user-1",
        display_name="personal",
    )


def _runner(tmp_path: Path) -> WeixinRunner:
    return WeixinRunner(
        WeixinConfig(enabled=True),
        [_connection()],
        tmp_path / "weixin-state.json",
    )


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
    assert payload["msg"]["to_user_id"] == "user-1"
    assert payload["msg"]["context_token"] == "context-1"


def test_confirmed_login_requires_complete_credentials() -> None:
    credentials = credentials_from_login(
        {
            "status": "confirmed",
            "ilink_bot_id": "bot-1",
            "bot_token": "token-1",
            "ilink_user_id": "owner-1",
        }
    )

    assert credentials == WeixinCredentials(
        bot_id="bot-1",
        token="token-1",
        user_id="owner-1",
    )
    assert credentials_from_login({"status": "wait"}) is None
    assert credentials_from_login({"status": "confirmed", "ilink_bot_id": "bot-1"}) is None


@pytest.mark.asyncio
async def test_connection_repository_owns_bound_instances(tmp_path: Path) -> None:
    path = tmp_path / "weixin-connections.json"
    repository = WeixinConnectionRepository(path)

    await repository.save(_connection())

    restored = WeixinConnectionRepository(path).list()
    assert restored == [_connection()]
    assert "secret-token" in path.read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_connection_repository_updates_metadata_without_losing_secret(
    tmp_path: Path,
) -> None:
    repository = WeixinConnectionRepository(tmp_path / "weixin-connections.json")
    await repository.save(_connection())

    updated = await repository.update("bot-1", display_name="home", enabled=False)

    assert updated is not None
    assert updated.display_name == "home"
    assert updated.enabled is False
    assert updated.token == "secret-token"


@pytest.mark.asyncio
async def test_runner_uses_bot_instance_as_participant_and_state_key(
    tmp_path: Path,
) -> None:
    runner = _runner(tmp_path)
    events = []

    async def collect(event: object) -> None:
        events.append(event)

    runner.set_inbound_handler(collect)
    client = AsyncMock()
    await runner._publish_message(  # noqa: SLF001
        "bot-1",
        {
            "message_type": 1,
            "from_user_id": "user-1",
            "context_token": "context-1",
            "message_id": "message-1",
            "item_list": [{"type": 1, "text_item": {"text": "hello"}}],
        },
        client,
    )

    assert events[0].participant_id == "weixin:bot-1"
    assert runner.participant_ids() == ["weixin:bot-1"]
    assert runner.resolve_participant("personal") == "weixin:bot-1"


@pytest.mark.asyncio
async def test_runner_checks_inbound_access_before_context_and_activity(
    tmp_path: Path,
) -> None:
    runner = _runner(tmp_path)
    collect = AsyncMock()
    runner.set_inbound_handler(collect)
    runner.set_access_controller(
        ChannelAccessController(
            ChannelAccessConfig.model_validate(
                {"weixin": {"inbound_deny": ["weixin:bot-1"]}}
            )
        )
    )
    client = AsyncMock()

    await runner._publish_message(  # noqa: SLF001
        "bot-1",
        {
            "message_type": 1,
            "from_user_id": "user-1",
            "context_token": "blocked-context",
            "message_id": "message-1",
            "item_list": [{"type": 1, "text_item": {"text": "hello"}}],
        },
        client,
    )

    collect.assert_not_awaited()
    client.send_text.assert_awaited_once()
    assert client.send_text.await_args.args[0] == "user-1"
    assert "拒绝" in client.send_text.await_args.args[1]
    assert client.send_text.await_args.args[2] == "blocked-context"
    assert runner._state.connections == {}  # noqa: SLF001
    assert runner.activity_for("weixin:bot-1") == (None, None)
    assert [entry["status"] for entry in runner._access.traffic.recent(2)] == [  # noqa: SLF001
        "sent",
        "denied",
    ]


@pytest.mark.asyncio
async def test_runner_prunes_state_when_connection_is_removed(tmp_path: Path) -> None:
    state_path = tmp_path / "weixin-state.json"
    runner = _runner(tmp_path)
    client = AsyncMock()
    await runner._publish_message(  # noqa: SLF001
        "bot-1",
        {
            "message_type": 1,
            "from_user_id": "user-1",
            "context_token": "context-1",
            "item_list": [{"type": 1, "text_item": {"text": "hello"}}],
        },
        client,
    )

    await runner.replace_connections([])

    saved = json.loads(state_path.read_text(encoding="utf-8"))
    assert saved["connections"] == {}


@pytest.mark.asyncio
async def test_renaming_connection_does_not_restart_message_polling(
    tmp_path: Path,
) -> None:
    runner = _runner(tmp_path)
    runner._cancel_account_tasks = AsyncMock()  # type: ignore[method-assign]  # noqa: SLF001
    renamed = WeixinConnection(
        bot_instance_id="bot-1",
        token="secret-token",
        weixin_user_id="user-1",
        display_name="home",
    )

    await runner.replace_connections([renamed])

    runner._cancel_account_tasks.assert_not_awaited()  # type: ignore[attr-defined]  # noqa: SLF001
    assert runner.instance_name("bot-1") == "home"


@pytest.mark.asyncio
async def test_start_login_creates_qr_image_for_chat_local_storage(
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

        async def poll_login(self, qrcode: str, verify_code: str) -> dict[str, str]:
            return {"status": "wait"}

        async def close(self) -> None:
            return None

    runner = _runner(tmp_path)
    monkeypatch.setattr(
        "coworker.channels.weixin.runner.WeixinClient",
        lambda *args, **kwargs: LoginClient(),
    )

    result = await runner.start_login()

    image_path = Path(result["qrcode_path"])
    assert result["qrcode_data_url"].startswith("data:image/png;base64,")
    assert image_path.is_file()
    with Image.open(image_path) as image:
        assert image.format == "PNG"


class _PairingRuntime:
    def __init__(self, status: str = "confirmed") -> None:
        self.status = status
        self.config = WeixinConfig(enabled=True)
        self.replaced: list[WeixinConnection] = []

    async def start_login(self) -> dict[str, str]:
        return {
            "session_id": "session-1",
            "qrcode_path": "pairing.png",
            "qrcode_data_url": "data:image/png;base64,abc",
            "status": "wait",
        }

    async def poll_login(
        self,
        session_id: str,
        verify_code: str = "",
    ) -> dict[str, object]:
        if self.status != "confirmed":
            return {"status": self.status, "credentials": None}
        return {
            "status": "confirmed",
            "credentials": WeixinCredentials(
                bot_id="bot-new",
                token="token-new",
                user_id="owner-new",
            ),
        }

    async def replace_connections(
        self,
        connections: list[WeixinConnection],
    ) -> None:
        self.replaced = connections


@pytest.mark.asyncio
async def test_connection_manager_polls_and_persists_in_background(tmp_path: Path) -> None:
    runtime = _PairingRuntime()
    repository = WeixinConnectionRepository(tmp_path / "connections.json")
    manager = WeixinConnectionManager(runtime, repository)  # type: ignore[arg-type]

    await manager.start_pairing()
    for _ in range(10):
        if manager.current_pairing()["status"] == "confirmed":  # type: ignore[index]
            break
        await asyncio.sleep(0)

    pairing = manager.current_pairing()
    assert pairing is not None
    assert pairing["participant_id"] == "weixin:bot-new"
    assert repository.list()[0].bot_instance_id == "bot-new"
    await manager.stop()


@pytest.mark.asyncio
async def test_disabled_channel_rejects_pairing_without_creating_session(
    tmp_path: Path,
) -> None:
    runtime = _PairingRuntime()
    runtime.config = WeixinConfig(enabled=False)
    manager = WeixinConnectionManager(  # type: ignore[arg-type]
        runtime,
        WeixinConnectionRepository(tmp_path / "connections.json"),
    )

    with pytest.raises(RuntimeError, match="停用"):
        await manager.start_pairing()

    assert manager.current_pairing() is None


@pytest.mark.asyncio
async def test_control_connect_returns_qr_without_selecting_recipient(
    tmp_path: Path,
) -> None:
    runtime = _PairingRuntime(status="wait")
    manager = WeixinConnectionManager(  # type: ignore[arg-type]
        runtime,
        WeixinConnectionRepository(tmp_path / "connections.json"),
    )
    channel = WeixinChannel(runtime, runtime, manager)  # type: ignore[arg-type]

    result = await channel.send(
        CommunicateRequest(
            participant_id=CONTROL_PARTICIPANT_ID,
            extra={"action": "connect"},
        )
    )

    assert not result.is_error
    assert "pairing.png" in result.content
    await manager.stop()


@pytest.mark.asyncio
async def test_control_remove_requires_explicit_confirmation(tmp_path: Path) -> None:
    repository = WeixinConnectionRepository(tmp_path / "connections.json")
    await repository.save(_connection())
    runtime = _PairingRuntime(status="wait")
    manager = WeixinConnectionManager(runtime, repository)  # type: ignore[arg-type]
    channel = WeixinChannel(runtime, runtime, manager)  # type: ignore[arg-type]

    unconfirmed = await channel.send(
        CommunicateRequest(
            participant_id=CONTROL_PARTICIPANT_ID,
            extra={"action": "remove", "bot_instance_id": "bot-1"},
        )
    )
    confirmed = await channel.send(
        CommunicateRequest(
            participant_id=CONTROL_PARTICIPANT_ID,
            extra={
                "action": "remove",
                "bot_instance_id": "bot-1",
                "confirm": True,
            },
        )
    )

    assert unconfirmed.is_error
    assert not confirmed.is_error
    assert repository.list() == []


@pytest.mark.asyncio
async def test_weixin_management_exposes_channel_owned_resources(tmp_path: Path) -> None:
    repository = WeixinConnectionRepository(tmp_path / "connections.json")
    await repository.save(_connection())
    manager = WeixinConnectionManager(  # type: ignore[arg-type]
        _PairingRuntime(status="wait"),
        repository,
    )
    management = WeixinManagement(manager)

    snapshot = await management.snapshot()
    await management.execute(
        "update_connection",
        {"bot_instance_id": "bot-1", "display_name": "home"},
    )

    assert snapshot["connections"][0]["bot_instance_id"] == "bot-1"  # type: ignore[index]
    assert repository.list()[0].display_name == "home"


def test_module_owns_connection_and_runtime_paths(tmp_path: Path) -> None:
    module = create_weixin_module(WeixinConfig(enabled=False), tmp_path, None)  # type: ignore[arg-type]

    assert module.name == "weixin"
    assert module.management is not None
    assert module.settings.config_key == "weixin"


def _encrypt_aes_ecb(plaintext: bytes, key: bytes) -> bytes:
    padder = padding.PKCS7(128).padder()
    padded = padder.update(plaintext) + padder.finalize()
    encryptor = Cipher(algorithms.AES(key), modes.ECB()).encryptor()
    return encryptor.update(padded) + encryptor.finalize()


def _cdn_client(payload: bytes, urls: list[str], key: bytes) -> WeixinClient:
    async def handle(request: httpx.Request) -> httpx.Response:
        urls.append(str(request.url))
        return httpx.Response(200, content=_encrypt_aes_ecb(payload, key))

    return WeixinClient(token="secret-token", transport=httpx.MockTransport(handle))


def _png_bytes() -> bytes:
    output = io.BytesIO()
    with Image.new("RGB", (2, 2), color=(10, 20, 30)) as image:
        image.save(output, format="PNG")
    return output.getvalue()


async def _collect_inbound(
    runner: WeixinRunner,
    client: WeixinClient,
    item_list: list[dict[str, object]],
) -> IncomingEvent:
    events: list[IncomingEvent] = []

    async def collect(event: IncomingEvent) -> None:
        events.append(event)

    runner.set_inbound_handler(collect)
    await runner._publish_message(  # noqa: SLF001
        "bot-1",
        {
            "message_type": 1,
            "from_user_id": "user-1",
            "context_token": "context-1",
            "message_id": "message-1",
            "item_list": item_list,
        },
        client,
    )
    assert len(events) == 1
    return events[0]


@pytest.mark.asyncio
async def test_inbound_image_is_downloaded_decrypted_and_attached(tmp_path: Path) -> None:
    key = b"0123456789abcdef"
    plaintext = _png_bytes()
    urls: list[str] = []
    client = _cdn_client(plaintext, urls, key)
    runner = _runner(tmp_path)

    event = await _collect_inbound(
        runner,
        client,
        [
            {"type": 1, "text_item": {"text": "看这张图"}},
            {
                "type": 2,
                "image_item": {
                    "media": {
                        "encrypt_query_param": "ENC-PARAM",
                        "aes_key": base64.b64encode(key).decode("ascii"),
                    }
                },
            },
        ],
    )
    await client.close()

    assert "[图片]" in event.content
    assert len(event.attachments) == 1
    attachment = event.attachments[0]
    assert attachment.media_type == "image/png"
    assert attachment.filename.endswith(".png")
    assert attachment.data == base64.b64encode(plaintext).decode("ascii")
    saved = Path(attachment.saved_path)
    assert saved.read_bytes() == plaintext
    assert urls == [
        "https://novac2c.cdn.weixin.qq.com/c2c/download"
        "?encrypted_query_param=ENC-PARAM"
    ]


@pytest.mark.asyncio
async def test_inbound_image_prefers_hex_aeskey_over_media_key(tmp_path: Path) -> None:
    hex_key = "0123456789abcdef0987654321fedcba"
    plaintext = _png_bytes()
    urls: list[str] = []
    client = _cdn_client(plaintext, urls, bytes.fromhex(hex_key))
    runner = _runner(tmp_path)

    event = await _collect_inbound(
        runner,
        client,
        [
            {
                "type": 2,
                "image_item": {
                    "aeskey": hex_key,
                    "media": {
                        "encrypt_query_param": "ENC-PARAM",
                        "aes_key": base64.b64encode(b"wrong-wrong-wrong!").decode("ascii"),
                    },
                },
            }
        ],
    )
    await client.close()

    assert len(event.attachments) == 1
    assert event.attachments[0].saved_path and (
        Path(event.attachments[0].saved_path).read_bytes() == plaintext
    )


@pytest.mark.asyncio
async def test_inbound_file_item_keeps_filename_and_guessed_mime(tmp_path: Path) -> None:
    key = b"fedcba9876543210"
    plaintext = b"%PDF-1.4 minimal"
    client = _cdn_client(plaintext, [], key)
    runner = _runner(tmp_path)

    event = await _collect_inbound(
        runner,
        client,
        [
            {
                "type": 4,
                "file_item": {
                    "file_name": "..\\report.pdf",
                    "media": {
                        "encrypt_query_param": "ENC-PARAM",
                        "aes_key": base64.b64encode(key).decode("ascii"),
                    },
                },
            }
        ],
    )
    await client.close()

    assert len(event.attachments) == 1
    attachment = event.attachments[0]
    assert attachment.media_type == "application/pdf"
    assert attachment.filename == "report.pdf"
    assert Path(attachment.saved_path).name.endswith("_report.pdf")
    assert attachment.data == base64.b64encode(plaintext).decode("ascii")


@pytest.mark.asyncio
async def test_inbound_image_without_cdn_coordinates_stays_placeholder(tmp_path: Path) -> None:
    client = AsyncMock()
    runner = _runner(tmp_path)

    event = await _collect_inbound(
        runner,
        client,
        [{"type": 2, "image_item": {"media": {}}}],
    )

    assert event.content == "[图片]"
    assert event.attachments == []
    client.download_media.assert_not_awaited()


@pytest.mark.asyncio
async def test_inbound_media_download_failure_appends_note(tmp_path: Path) -> None:
    async def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500)

    client = WeixinClient(token="secret-token", transport=httpx.MockTransport(handle))
    runner = _runner(tmp_path)

    event = await _collect_inbound(
        runner,
        client,
        [
            {
                "type": 2,
                "image_item": {
                    "media": {
                        "encrypt_query_param": "ENC-PARAM",
                        "aes_key": base64.b64encode(b"0123456789abcdef").decode("ascii"),
                    }
                },
            }
        ],
    )
    await client.close()

    assert "[图片]" in event.content
    assert "附件下载失败" in event.content
    assert event.attachments == []
    assert not (tmp_path / "attachments").exists() or not any(
        (tmp_path / "attachments").iterdir()
    )


@pytest.mark.asyncio
async def test_inbound_media_too_large_appends_skipped_note(tmp_path: Path) -> None:
    limits: list[int] = []

    class _TooLargeClient:
        async def download_media(self, destination: object, **kwargs: object) -> int:
            limits.append(int(kwargs.get("max_bytes")))
            raise WeixinMediaTooLargeError(300 * 1024 * 1024, 200 * 1024 * 1024)

    runner = _runner(tmp_path)

    event = await _collect_inbound(
        runner,
        _TooLargeClient(),  # type: ignore[arg-type]
        [
            {
                "type": 5,
                "video_item": {
                    "media": {
                        "encrypt_query_param": "ENC-PARAM",
                        "aes_key": base64.b64encode(b"0123456789abcdef").decode("ascii"),
                    }
                },
            }
        ],
    )

    assert "[视频]" in event.content
    assert "因过大" in event.content
    assert "300 MB" in event.content
    assert "200 MB" in event.content
    assert event.attachments == []
    assert limits == [200 * 1024 * 1024]


@pytest.mark.asyncio
async def test_inbound_media_content_length_fast_fail_reports_size(tmp_path: Path) -> None:
    async def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-length": str(5 * 1024 * 1024)})

    client = WeixinClient(token="secret-token", transport=httpx.MockTransport(handle))
    runner = WeixinRunner(
        WeixinConfig(enabled=True, max_download_mb=1),
        [_connection()],
        tmp_path / "weixin-state.json",
    )

    event = await _collect_inbound(
        runner,
        client,
        [
            {
                "type": 5,
                "video_item": {
                    "media": {
                        "encrypt_query_param": "ENC-PARAM",
                        "aes_key": base64.b64encode(b"0123456789abcdef").decode("ascii"),
                    }
                },
            }
        ],
    )
    await client.close()

    assert "因过大" in event.content
    assert "5 MB" in event.content
    assert "1 MB" in event.content
    assert event.attachments == []
    assert not (tmp_path / "attachments").exists() or not any(
        (tmp_path / "attachments").iterdir()
    )


@pytest.mark.asyncio
async def test_inbound_media_honors_configured_download_limit(tmp_path: Path) -> None:
    limits: list[int] = []

    class _RecordingClient:
        async def download_media(self, destination: Path, **kwargs: object) -> int:
            limits.append(int(kwargs.get("max_bytes")))
            destination.write_bytes(b"video")
            return 5

    runner = WeixinRunner(
        WeixinConfig(enabled=True, max_download_mb=1),
        [_connection()],
        tmp_path / "weixin-state.json",
    )

    event = await _collect_inbound(
        runner,
        _RecordingClient(),  # type: ignore[arg-type]
        [
            {
                "type": 5,
                "video_item": {
                    "media": {
                        "encrypt_query_param": "ENC-PARAM",
                        "aes_key": base64.b64encode(b"0123456789abcdef").decode("ascii"),
                    }
                },
            }
        ],
    )

    assert limits == [1 * 1024 * 1024]
    assert len(event.attachments) == 1
    assert event.attachments[0].media_type == "video/mp4"


@pytest.mark.asyncio
async def test_inbound_voice_without_transcript_stays_placeholder(tmp_path: Path) -> None:
    client = AsyncMock()
    runner = _runner(tmp_path)

    event = await _collect_inbound(
        runner,
        client,
        [{"type": 3, "voice_item": {}}],
    )

    assert event.content == "[未提供转写的语音消息]"
    assert event.attachments == []
    client.download_media.assert_not_awaited()



def test_weixin_polling_http_logs_only_downgrade_poll_requests() -> None:
    configure_weixin_polling_logs()
    updates = logging.LogRecord(
        "httpx",
        logging.INFO,
        __file__,
        1,
        "HTTP Request: POST https://ilinkai.weixin.qq.com/ilink/bot/getupdates",
        (),
        None,
    )
    snapshot = logging.LogRecord(
        "uvicorn.access",
        logging.INFO,
        __file__,
        1,
        '127.0.0.1 - "GET /api/admin/channels/weixin/management HTTP/1.1" 200',
        (),
        None,
    )
    command = logging.LogRecord(
        "uvicorn.access",
        logging.INFO,
        __file__,
        1,
        '127.0.0.1 - "POST /api/admin/channels/weixin/management/remove_connection HTTP/1.1" 200',
        (),
        None,
    )

    for logger_name, record in (
        ("httpx", updates),
        ("uvicorn.access", snapshot),
        ("uvicorn.access", command),
    ):
        for log_filter in logging.getLogger(logger_name).filters:
            log_filter.filter(record)

    assert updates.levelno == logging.DEBUG
    assert snapshot.levelno == logging.DEBUG
    assert command.levelno == logging.INFO
