from __future__ import annotations

import asyncio
import base64
import hashlib
import io
import json
import logging
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock

import httpx
import pytest
from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from PIL import Image
from pydantic import ValidationError

from coworker.channels.access import ChannelAccessController
from coworker.channels.weixin.channel import CONTROL_PARTICIPANT_ID, WeixinChannel
from coworker.channels.weixin.client import (
    DEFAULT_CDN_BASE_URL,
    WeixinClient,
    WeixinCredentials,
    credentials_from_login,
)
from coworker.channels.weixin.connections import WeixinConnectionManager
from coworker.channels.weixin.logging import configure_weixin_polling_logs
from coworker.channels.weixin.media import (
    WeixinMediaTooLargeError,
    decrypt_aes_ecb,
    resolve_media_key,
)
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


@pytest.mark.asyncio
async def test_download_media_leaves_no_ciphertext_copy(tmp_path: Path) -> None:
    key = b"0123456789abcdef"
    plaintext = b"decrypted-payload"
    client = _cdn_client(plaintext, [], key)
    destination = tmp_path / "attachment.bin"

    size = await client.download_media(
        destination,
        full_url="https://cdn.example/x",
        aes_key=key,
    )
    await client.close()

    assert size == len(plaintext)
    assert destination.read_bytes() == plaintext
    # 密文副本只服务于解密：成功路径也不能把它留在附件目录里。
    assert [item.name for item in tmp_path.iterdir()] == ["attachment.bin"]


class _UploadScript:
    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.cdn_statuses = [200]
        self.cdn_calls = 0
        self.upload_body: dict[str, object] | None = {"upload_full_url": "https://cdn.example/secret-upload"}
        self.download_param = "download-param"
        self.block_cdn: asyncio.Event | None = None

    async def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.url.path.endswith("getuploadurl"):
            return httpx.Response(200, json=self.upload_body or {"ret": -1})
        if request.url.path.endswith("secret-upload") or request.url.path.endswith("/upload"):
            if self.block_cdn is not None:
                await self.block_cdn.wait()
            status = self.cdn_statuses[min(self.cdn_calls, len(self.cdn_statuses) - 1)]
            self.cdn_calls += 1
            headers = {"x-encrypted-param": self.download_param} if status == 200 else {}
            return httpx.Response(status, headers=headers)
        return httpx.Response(200, json={"ret": 0})


def _channel_with_upload(tmp_path: Path, script: _UploadScript) -> tuple[WeixinChannel, WeixinRunner, WeixinClient]:
    runner = _runner(tmp_path)
    runner._connection_state("bot-1").context_tokens["user-1"] = "ctx-9"  # noqa: SLF001
    client = WeixinClient(token="secret-token", transport=httpx.MockTransport(script.handle))
    runner._clients["bot-1"] = client  # noqa: SLF001
    channel = WeixinChannel(runner, runner, None)  # type: ignore[arg-type]
    return channel, runner, client


def _leftover_ciphertext() -> set[Path]:
    return set(Path(tempfile.gettempdir()).glob("coworker-weixin-*"))


def test_weixin_upload_limit_rejects_out_of_range_values() -> None:
    assert WeixinConfig().max_upload_mb == 200
    assert WeixinConfig().upload_timeout_seconds == 300
    with pytest.raises(ValidationError):
        WeixinConfig(max_upload_mb=0)
    with pytest.raises(ValidationError):
        WeixinConfig(max_upload_mb=10001)
    with pytest.raises(ValidationError):
        WeixinConfig(upload_timeout_seconds=0)
    with pytest.raises(ValidationError):
        WeixinConfig(upload_timeout_seconds=3601)


def test_connected_account_accepts_attachments_and_control_does_not() -> None:
    channel = WeixinChannel(None, None, None)  # type: ignore[arg-type]
    connected = channel.capabilities_for("weixin:bot-1")
    control = channel.capabilities_for(CONTROL_PARTICIPANT_ID)
    attachment = {"type": "file", "path": "report.pdf"}

    kept, omitted = connected.filter(
        CommunicateRequest(participant_id="weixin:bot-1", attachments=[attachment])
    )
    stripped, dropped = control.filter(
        CommunicateRequest(
            participant_id=CONTROL_PARTICIPANT_ID,
            attachments=[attachment],
            extra={"action": "status"},
        )
    )

    assert connected.attachments is True
    assert omitted == ()
    assert kept.attachments == [attachment]
    assert control.attachments is False
    assert control.extra is True
    assert dropped == ("attachments",)
    assert stripped.attachments == []
    assert stripped.extra == {"action": "status"}


@pytest.mark.asyncio
async def test_outbound_uploads_image_video_and_file(tmp_path: Path) -> None:
    script = _UploadScript()
    channel, runner, client = _channel_with_upload(tmp_path, script)
    image = tmp_path / "photo.png"
    video = tmp_path / "clip.mp4"
    document = tmp_path / "report.pdf"
    image.write_bytes(b"png-bytes")
    video.write_bytes(b"mp4-bytes")
    document.write_bytes(b"pdf-bytes")
    before = _leftover_ciphertext()

    result = await channel.send(
        CommunicateRequest(
            participant_id="weixin:bot-1",
            message="see attached",
            attachments=[
                {"type": "image", "path": str(image), "filename": "photo.png"},
                {"type": "file", "path": str(video)},
                {"type": "file", "path": str(document), "filename": "report.pdf"},
            ],
        )
    )
    await runner.drain_uploads()
    await client.close()

    assert not result.is_error
    assert "尚未送达" in result.content
    assert "cdn.example" not in result.content
    assert _leftover_ciphertext() <= before
    uploads = [
        json.loads(request.content)
        for request in script.requests
        if request.url.path.endswith("getuploadurl")
    ]
    sent = [
        json.loads(request.content)
        for request in script.requests
        if request.url.path.endswith("sendmessage")
    ]
    items = [body["msg"]["item_list"][0] for body in sent]
    texts = [item["text_item"]["text"] for item in items if "text_item" in item]
    assert [item["media_type"] for item in uploads] == [1, 2, 3]
    assert texts == ["see attached"]
    assert all(body["msg"]["context_token"] == "ctx-9" for body in sent)
    image_item = next(item["image_item"] for item in items if "image_item" in item)
    video_item = next(item["video_item"] for item in items if "video_item" in item)
    file_item = next(item["file_item"] for item in items if "file_item" in item)
    assert image_item["media"]["encrypt_query_param"] == "download-param"
    assert image_item["media"]["encrypt_type"] == 1
    assert video_item["video_size"] == image_item["mid_size"]
    assert file_item["file_name"] == "report.pdf"
    assert file_item["len"] == str(len(b"pdf-bytes"))
    posted = [
        request.content
        for request in script.requests
        if request.url.path.endswith("secret-upload")
    ]
    media_items = [image_item, video_item, file_item]
    plaintexts = [b"png-bytes", b"mp4-bytes", b"pdf-bytes"]
    for upload, ciphertext, plaintext, item in zip(
        uploads, posted, plaintexts, media_items, strict=True
    ):
        key = bytes.fromhex(str(upload["aeskey"]))
        assert upload["rawfilemd5"] == hashlib.md5(plaintext).hexdigest()
        assert upload["no_need_thumb"] is True
        assert decrypt_aes_ecb(ciphertext, key) == plaintext
        assert resolve_media_key(None, item["media"]["aes_key"]) == key
    assert "secret" not in result.content


@pytest.mark.asyncio
async def test_outbound_file_uses_upload_param_when_full_url_is_absent(tmp_path: Path) -> None:
    script = _UploadScript()
    script.upload_body = {"upload_param": "PARAM"}
    channel, runner, client = _channel_with_upload(tmp_path, script)
    document = tmp_path / "notes.txt"
    document.write_bytes(b"notes")

    result = await channel.send(
        CommunicateRequest(
            participant_id="weixin:bot-1",
            attachments=[{"type": "file", "path": str(document)}],
        )
    )
    await runner.drain_uploads()
    await client.close()

    upload = next(request for request in script.requests if request.url.path.endswith("getuploadurl"))
    filekey = str(json.loads(upload.content)["filekey"])
    cdn = next(request for request in script.requests if request.url.path.endswith("/upload"))
    sent = [
        json.loads(request.content)["msg"]["item_list"][0]
        for request in script.requests
        if request.url.path.endswith("sendmessage")
    ]
    assert not result.is_error
    assert "尚未送达" in result.content
    assert cdn.url.params["encrypted_query_param"] == "PARAM"
    assert cdn.url.params["filekey"] == filekey
    assert str(cdn.url).startswith(f"{DEFAULT_CDN_BASE_URL}/upload?")
    assert len([item for item in sent if "file_item" in item]) == 1
    assert not any("text_item" in item for item in sent)


@pytest.mark.asyncio
async def test_outbound_upload_retries_server_errors_and_stops_on_client_errors(
    tmp_path: Path,
) -> None:
    script = _UploadScript()
    script.cdn_statuses = [500, 200]
    channel, runner, client = _channel_with_upload(tmp_path, script)
    document = tmp_path / "notes.txt"
    document.write_bytes(b"notes")
    statuses: list[IncomingEvent] = []

    async def collect(event: IncomingEvent) -> None:
        statuses.append(event)

    runner.set_inbound_handler(collect)

    recovered = await channel.send(
        CommunicateRequest(
            participant_id="weixin:bot-1",
            conversation_id="session-1",
            attachments=[{"type": "file", "path": str(document)}],
        )
    )
    await runner.drain_uploads()
    assert not recovered.is_error
    assert "尚未送达" in recovered.content
    assert script.cdn_calls == 2
    assert statuses[-1].source == "channel_upload"
    assert statuses[-1].participant_id == "weixin:bot-1"
    assert statuses[-1].conversation_id == "session-1"
    assert statuses[-1].content == "[微信上传] 已发送：notes.txt"

    script.requests.clear()
    script.cdn_calls = 0
    script.cdn_statuses = [400]
    statuses.clear()
    rejected = await channel.send(
        CommunicateRequest(
            participant_id="weixin:bot-1",
            attachments=[{"type": "file", "path": str(document)}],
        )
    )
    await runner.drain_uploads()
    await client.close()

    notices = [
        json.loads(request.content)["msg"]["item_list"][0].get("text_item", {}).get("text", "")
        for request in script.requests
        if request.url.path.endswith("sendmessage")
    ]
    assert not rejected.is_error
    assert script.cdn_calls == 1
    assert "cdn.example" not in rejected.content
    assert "secret-upload" not in rejected.content
    assert any(notice.startswith("上传失败：notes.txt") for notice in notices)
    assert all("cdn.example" not in notice and "secret-upload" not in notice for notice in notices)
    assert statuses[-1].source == "channel_upload"
    assert "上传失败：notes.txt" in statuses[-1].content
    assert "cdn.example" not in statuses[-1].content


@pytest.mark.asyncio
async def test_outbound_reports_partial_delivery_without_leaking_upload_details(
    tmp_path: Path,
) -> None:
    script = _UploadScript()
    channel, runner, client = _channel_with_upload(tmp_path, script)
    first = tmp_path / "first.txt"
    first.write_bytes(b"one")
    missing = tmp_path / "missing.txt"

    result = await channel.send(
        CommunicateRequest(
            participant_id="weixin:bot-1",
            message="intro",
            attachments=[
                {"type": "file", "path": str(first), "filename": "first.txt"},
                {"type": "file", "path": str(missing), "filename": "second.txt"},
            ],
        )
    )
    await runner.drain_uploads()
    await client.close()

    assert not result.is_error
    assert "first.txt" in result.content
    assert "second.txt" in result.content
    assert "没有开始上传" in result.content
    assert "不要重发" in result.content
    assert "cdn.example" not in result.content
    assert "intro" not in result.content


@pytest.mark.asyncio
async def test_outbound_rejects_oversized_invalid_and_url_less_uploads(tmp_path: Path) -> None:
    script = _UploadScript()
    runner = WeixinRunner(
        WeixinConfig(enabled=True, max_upload_mb=1),
        [_connection()],
        tmp_path / "weixin-state.json",
    )
    client = WeixinClient(token="secret-token", transport=httpx.MockTransport(script.handle))
    runner._clients["bot-1"] = client  # noqa: SLF001
    channel = WeixinChannel(runner, runner, None)  # type: ignore[arg-type]
    huge = tmp_path / "huge.bin"
    huge.write_bytes(b"x" * (1024 * 1024 + 1))

    oversized = await channel.send(
        CommunicateRequest(
            participant_id="weixin:bot-1",
            attachments=[{"type": "file", "path": str(huge)}],
        )
    )
    invalid = await channel.send(
        CommunicateRequest(
            participant_id="weixin:bot-1",
            attachments=[{"type": "voice", "path": str(huge)}],
        )
    )
    script.upload_body = {"ret": -1}
    notes = tmp_path / "notes.txt"
    notes.write_bytes(b"notes")
    missing_url = await channel.send(
        CommunicateRequest(
            participant_id="weixin:bot-1",
            attachments=[{"type": "file", "path": str(notes)}],
        )
    )
    await runner.drain_uploads()
    await client.close()

    notices = [
        json.loads(request.content)["msg"]["item_list"][0].get("text_item", {}).get("text", "")
        for request in script.requests
        if request.url.path.endswith("sendmessage")
    ]
    assert oversized.is_error
    assert "1048577" in oversized.content
    assert invalid.is_error
    assert "voice" in invalid.content
    assert not missing_url.is_error
    assert "尚未送达" in missing_url.content
    assert "ret=-1" not in missing_url.content
    assert "https://" not in missing_url.content
    assert any(notice.startswith("上传失败：notes.txt") for notice in notices)
    assert all("ret=-1" not in notice and "https://" not in notice for notice in notices)
    assert script.cdn_calls == 0


@pytest.mark.asyncio
async def test_outbound_upload_returns_before_the_cdn_transfer(tmp_path: Path) -> None:
    script = _UploadScript()
    script.block_cdn = asyncio.Event()
    channel, runner, client = _channel_with_upload(tmp_path, script)
    document = tmp_path / "notes.txt"
    document.write_bytes(b"notes")

    result = await asyncio.wait_for(
        channel.send(
            CommunicateRequest(
                participant_id="weixin:bot-1",
                attachments=[{"type": "file", "path": str(document)}],
            )
        ),
        timeout=2,
    )

    assert not result.is_error
    assert "尚未送达" in result.content
    assert script.cdn_calls == 0
    assert script.block_cdn is not None
    script.block_cdn.set()
    await runner.drain_uploads()
    await client.close()
    assert script.cdn_calls == 1


@pytest.mark.asyncio
async def test_outbound_upload_uses_configured_timeout(tmp_path: Path) -> None:
    script = _UploadScript()
    channel, runner, client = _channel_with_upload(tmp_path, script)
    runner._config = WeixinConfig(enabled=True, upload_timeout_seconds=42)  # noqa: SLF001
    document = tmp_path / "notes.txt"
    document.write_bytes(b"notes")
    seen: list[object] = []
    original = client._http.post

    async def post(url: str, *args: object, **kwargs: object) -> httpx.Response:
        if str(url).endswith("secret-upload"):
            seen.append(kwargs.get("timeout"))
        return await original(url, *args, **kwargs)  # type: ignore[arg-type]

    client._http.post = post  # type: ignore[method-assign]
    await channel.send(
        CommunicateRequest(
            participant_id="weixin:bot-1",
            attachments=[{"type": "file", "path": str(document)}],
        )
    )
    await runner.drain_uploads()
    await client.close()

    assert seen == [42.0]
