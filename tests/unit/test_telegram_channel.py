from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from coworker.agent.incoming_content import build_content_blocks, format_event_text
from coworker.channels.access import ChannelAccessController
from coworker.channels.activity import ChannelActivityStore
from coworker.channels.telegram import adapter
from coworker.channels.telegram import client as telegram_client_module
from coworker.channels.telegram import runner as telegram_runner_module
from coworker.channels.telegram.channel import TelegramChannel
from coworker.channels.telegram.client import (
    MAX_DOWNLOAD_BYTES,
    TelegramClient,
    TelegramFileTooLargeError,
)
from coworker.channels.telegram.runner import TelegramRunner, split_telegram_text
from coworker.channels.telegram.state import (
    TelegramContact,
    TelegramState,
    TelegramStateStore,
)
from coworker.core.config import (
    ChannelAccessConfig,
    TelegramBotConfig,
    TelegramConfig,
)
from coworker.core.types import CommunicateRequest, IncomingEvent
from coworker.i18n import locale_context


class _FakeClient:
    def __init__(self, bot_user_id: int = 1) -> None:
        self.bot_user_id = bot_user_id
        self.messages: list[tuple[int, str, int | None]] = []
        self.attachments: list[tuple[int, dict, int | None]] = []
        self.downloads: list[str] = []
        self.closed = False

    async def close(self) -> None:
        self.closed = True

    async def get_me(self) -> dict:
        return {"id": self.bot_user_id, "is_bot": True}

    async def get_updates(self, offset: int, timeout_seconds: float) -> list[dict]:
        return []

    async def send_message(
        self,
        chat_id: int,
        text: str,
        message_thread_id: int | None = None,
    ) -> None:
        self.messages.append((chat_id, text, message_thread_id))

    async def send_attachment(
        self,
        chat_id: int,
        attachment: dict,
        message_thread_id: int | None = None,
    ) -> None:
        self.attachments.append((chat_id, attachment, message_thread_id))

    async def download_file(self, file_id: str) -> bytes:
        self.downloads.append(file_id)
        return b"telegram-image"


def _config(**bots: dict) -> TelegramConfig:
    return TelegramConfig.model_validate({"bots": bots})


def _runner(tmp_path: Path, config: TelegramConfig) -> TelegramRunner:
    return TelegramRunner(
        config,
        tmp_path / "state",
        tmp_path / "attachments",
        ChannelActivityStore(),
        client_factory=lambda _: _FakeClient(),  # type: ignore[arg-type,return-value]
    )


@pytest.mark.asyncio
async def test_python_telegram_bot_adapter_uses_typed_bot_api() -> None:
    bot = AsyncMock()
    bot.bot = SimpleNamespace(to_dict=lambda: {"id": 42, "is_bot": True})
    bot.get_updates.return_value = [
        SimpleNamespace(to_dict=lambda: {"update_id": 7, "message": {}})
    ]
    client = TelegramClient(
        "secret-token",
        "https://telegram.example/api",
        bot=bot,
    )

    assert await client.get_me() == {"id": 42, "is_bot": True}
    assert await client.get_updates(7, 30) == [{"update_id": 7, "message": {}}]
    await client.send_message(-1001, "hello", 19)
    await client.close()

    bot.initialize.assert_awaited_once()
    bot.get_updates.assert_awaited_once()
    assert bot.get_updates.await_args.kwargs["offset"] == 7
    assert bot.get_updates.await_args.kwargs["allowed_updates"] == (
        "message",
        "channel_post",
    )
    bot.send_message.assert_awaited_once_with(
        chat_id=-1001,
        text="hello",
        message_thread_id=19,
    )
    bot.shutdown.assert_awaited_once()


def test_client_builds_custom_bot_and_file_api_urls(monkeypatch) -> None:
    bot_factory = MagicMock()
    monkeypatch.setattr(telegram_client_module, "Bot", bot_factory)

    TelegramClient(
        " secret-token ",
        "https://telegram.example/custom/",
        local_mode=True,
    )

    bot_factory.assert_called_once_with(
        token="secret-token",
        base_url="https://telegram.example/custom/bot",
        base_file_url="https://telegram.example/custom/file/bot",
        local_mode=True,
    )


@pytest.mark.asyncio
async def test_local_mode_sends_a_shared_file_path(tmp_path: Path) -> None:
    path = tmp_path / "report.txt"
    path.write_text("shared", encoding="utf-8")
    bot = AsyncMock()
    client = TelegramClient("secret", local_mode=True, bot=bot)

    await client.send_attachment(
        42,
        {"type": "file", "path": str(path), "filename": "report.txt"},
    )

    bot.send_document.assert_awaited_once_with(
        chat_id=42,
        document=path,
        filename="report.txt",
        message_thread_id=None,
        read_timeout=60.0,
        write_timeout=60.0,
    )


@pytest.mark.asyncio
async def test_client_rejects_oversize_download_before_loading_bytes() -> None:
    bot = AsyncMock()
    telegram_file = SimpleNamespace(
        file_size=MAX_DOWNLOAD_BYTES + 1,
        download_as_bytearray=AsyncMock(),
    )
    bot.get_file.return_value = telegram_file
    client = TelegramClient("secret", bot=bot)

    with pytest.raises(TelegramFileTooLargeError):
        await client.download_file("large")

    telegram_file.download_as_bytearray.assert_not_awaited()


@pytest.mark.asyncio
async def test_same_chat_is_namespaced_by_bot_instance(tmp_path: Path) -> None:
    runner = _runner(
        tmp_path,
        _config(
            work={"bot_token": "work-token", "display_name": "Work"},
            home={"bot_token": "home-token", "display_name": "Home"},
        ),
    )
    events = []

    async def collect(event: object) -> None:
        events.append(event)

    runner.set_inbound_handler(collect)
    update = {
        "update_id": 10,
        "message": {
            "message_id": 2,
            "chat": {"id": 123, "type": "private", "first_name": "Alice"},
            "text": "hello",
        },
    }
    await runner._bots["work"]._consume_update(_FakeClient(1), update)  # noqa: SLF001
    await runner._bots["home"]._consume_update(_FakeClient(2), update)  # noqa: SLF001

    assert [event.participant_id for event in events] == ["tg:work:123", "tg:home:123"]
    assert runner.resolve_participant("work:123") == "tg:work:123"
    assert runner.resolve_participant("123") is None
    assert {item[0] for item in runner.contacts()} == {"work", "home"}
    for state_path in (tmp_path / "state").glob("*.json"):
        assert "token" not in state_path.read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_failed_inbound_delivery_keeps_offset_for_retry(tmp_path: Path) -> None:
    runner = _runner(tmp_path, _config(main={"bot_token": "token"}))
    attempts = 0

    async def collect(_: object) -> None:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("temporary inbox failure")

    runner.set_inbound_handler(collect)
    bot = runner._bots["main"]  # noqa: SLF001
    update = {
        "update_id": 10,
        "message": {
            "message_id": 2,
            "chat": {"id": 123, "type": "private", "first_name": "Alice"},
            "text": "hello",
        },
    }

    assert await bot._consume_update(_FakeClient(), update) is False  # noqa: SLF001
    assert bot._state.offset == 0  # noqa: SLF001
    assert bot.contact_for_chat(123) is None
    assert runner.activity_for("tg:main:123") == (None, None)

    assert await bot._consume_update(_FakeClient(), update) is True  # noqa: SLF001
    assert attempts == 2
    assert bot._state.offset == 11  # noqa: SLF001
    assert bot.contact_for_chat(123) == TelegramContact(123, "private", "Alice")
    assert runner.activity_for("tg:main:123")[1] is not None
    assert TelegramStateStore(tmp_path / "state" / "main.json").load().offset == 11


@pytest.mark.asyncio
async def test_failed_update_stops_the_current_poll_batch(
    tmp_path: Path,
    monkeypatch,
) -> None:
    runner = _runner(tmp_path, _config(main={"bot_token": "token"}))
    bot = runner._bots["main"]  # noqa: SLF001
    collect = AsyncMock(side_effect=RuntimeError("temporary inbox failure"))
    runner.set_inbound_handler(collect)
    updates = [
        {
            "update_id": update_id,
            "message": {
                "message_id": update_id,
                "chat": {"id": 123, "type": "private"},
                "text": f"message {update_id}",
            },
        }
        for update_id in (10, 11)
    ]

    class BatchClient(_FakeClient):
        calls = 0

        async def get_updates(
            self,
            offset: int,
            timeout_seconds: float,
        ) -> list[dict]:
            self.calls += 1
            if self.calls == 1:
                return updates
            await bot.stop()
            return []

    monkeypatch.setattr(telegram_runner_module, "_RETRY_SECONDS", 0)
    await bot._run_client(BatchClient())  # type: ignore[arg-type]  # noqa: SLF001

    collect.assert_awaited_once()
    assert bot._state.offset == 0  # noqa: SLF001


@pytest.mark.asyncio
async def test_invalid_inbound_update_is_logged_and_acknowledged(tmp_path: Path) -> None:
    runner = _runner(tmp_path, _config(main={"bot_token": "token"}))
    collect = AsyncMock()
    runner.set_inbound_handler(collect)
    bot = runner._bots["main"]  # noqa: SLF001

    acknowledged = await bot._consume_update(  # noqa: SLF001
        _FakeClient(),
        {
            "update_id": 7,
            "message": {
                "message_id": 2,
                "chat": {"id": 123, "type": "future-chat-type"},
                "text": "hello",
            },
        },
    )

    assert acknowledged is True
    assert bot._state.offset == 8  # noqa: SLF001
    collect.assert_not_awaited()


@pytest.mark.asyncio
async def test_inbound_access_is_checked_before_contact_and_download(
    tmp_path: Path,
) -> None:
    runner = _runner(tmp_path, _config(main={"bot_token": "token"}))
    runner.set_access_controller(
        ChannelAccessController(
            ChannelAccessConfig.model_validate({"telegram": {"inbound_deny": ["tg:main:*"]}})
        )
    )
    collect = AsyncMock()
    runner.set_inbound_handler(collect)
    client = _FakeClient()

    await runner._bots["main"]._consume_update(  # noqa: SLF001
        client,
        {
            "update_id": 4,
            "message": {
                "message_id": 3,
                "message_thread_id": 9,
                "chat": {"id": -1009, "type": "supergroup", "title": "Secret"},
                "from": {"id": 77, "first_name": "Mallory"},
                "photo": [{"file_id": "photo", "file_unique_id": "unique"}],
            },
        },
    )

    collect.assert_not_awaited()
    assert client.downloads == []
    assert runner.contacts() == []
    assert client.messages[0][0::2] == (-1009, 9)


@pytest.mark.asyncio
async def test_inbound_document_exposes_file_details_to_the_model(tmp_path: Path) -> None:
    runner = _runner(tmp_path, _config(main={"bot_token": "token"}))
    events: list[IncomingEvent] = []

    async def collect(event: IncomingEvent) -> None:
        events.append(event)

    runner.set_inbound_handler(collect)
    client = _FakeClient()
    with locale_context("en"):
        await runner._bots["main"]._consume_update(  # noqa: SLF001
            client,
            {
                "update_id": 5,
                "message": {
                    "message_id": 4,
                    "chat": {
                        "id": 123,
                        "type": "private",
                        "first_name": "Alice",
                    },
                    "document": {
                        "file_id": "report-file",
                        "file_unique_id": "report-1",
                        "file_name": "quarterly-report.pdf",
                        "mime_type": "application/pdf",
                    },
                    "caption": "Please review this report",
                },
            },
        )

        blocks = build_content_blocks(events)

    assert client.downloads == ["report-file"]
    assert events[0].attachments[0].filename == "quarterly-report.pdf"
    assert isinstance(blocks, list)
    model_text = "\n".join(block["text"] for block in blocks if block.get("type") == "text")
    assert "[file] quarterly-report.pdf (application/pdf)" in model_text
    assert "Please review this report" in model_text


@pytest.mark.asyncio
async def test_channel_routes_outbound_to_selected_bot_and_topic(tmp_path: Path) -> None:
    runner = _runner(
        tmp_path,
        _config(
            work={"bot_token": "work-token"},
            home={"bot_token": "home-token"},
        ),
    )
    work_client = _FakeClient(1)
    home_client = _FakeClient(2)
    work = runner._bots["work"]  # noqa: SLF001
    home = runner._bots["home"]  # noqa: SLF001
    work._client = work_client  # noqa: SLF001
    home._client = home_client  # noqa: SLF001
    work._ready = home._ready = True  # noqa: SLF001
    assert work._state.contacts is not None  # noqa: SLF001
    assert home._state.contacts is not None  # noqa: SLF001
    work._state.contacts[-100] = TelegramContact(-100, "group", "Work group")  # noqa: SLF001
    home._state.contacts[200] = TelegramContact(200, "private", "Bob")  # noqa: SLF001
    channel = TelegramChannel(runner)

    result = await channel.send(
        CommunicateRequest(
            participant_id="tg:work:-100",
            message="hello",
            conversation_id="12",
        )
    )

    assert result.is_error is False
    assert work_client.messages == [(-100, "hello", 12)]
    assert home_client.messages == []
    assert channel.list_connections()[0].kind == "telegram:group"


@pytest.mark.asyncio
async def test_reconfigure_adds_and_removes_bot_instances(tmp_path: Path) -> None:
    runner = _runner(tmp_path, _config(main={"bot_token": "one"}))

    await runner.reconfigure(_config(work={"bot_token": "two"}))

    assert set(runner._bots) == {"work"}  # noqa: SLF001


def test_state_resets_contacts_when_token_belongs_to_different_bot(tmp_path: Path) -> None:
    path = tmp_path / "main.json"
    store = TelegramStateStore(path)
    state = TelegramState(
        bot_user_id=1,
        offset=12,
        contacts={3: TelegramContact(3, "private", "Alice")},
    )
    store.save(state)
    restored = store.load()

    assert restored.reset_for_bot(2) is True
    assert restored.offset == 0
    assert restored.contacts == {}


def test_config_supports_multiple_bots_and_custom_api_roots() -> None:
    config = _config(
        main={"bot_token": "one"},
        work={
            "bot_token": "two",
            "api_base_url": "https://telegram.example/api/",
            "local_mode": True,
        },
    )

    assert set(config.bots) == {"main", "work"}
    assert config.bots["work"] == TelegramBotConfig(
        bot_token="two",
        api_base_url="https://telegram.example/api",
        local_mode=True,
    )
    with pytest.raises(ValueError):
        _config(**{"Bad.Name": {"bot_token": "three"}})


def test_multiple_bots_and_api_roots_load_from_environment(
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv(
        "TELEGRAM__BOTS",
        json.dumps(
            {
                "main": {"bot_token": "one"},
                "work": {
                    "bot_token": "two",
                    "api_base_url": "https://telegram.example/custom",
                },
            }
        ),
    )

    config = TelegramConfig()

    assert set(config.bots) == {"main", "work"}
    assert config.bots["work"].api_base_url == "https://telegram.example/custom"


@pytest.mark.parametrize(
    ("chat_type", "zh_label", "en_label"),
    (
        ("private", "[会话：私聊]", "[Chat: private]"),
        ("group", "[会话：群聊]", "[Chat: group]"),
        ("channel", "[会话：频道]", "[Chat: channel]"),
    ),
)
def test_telegram_content_header_identifies_chat_type(
    chat_type: str,
    zh_label: str,
    en_label: str,
) -> None:
    telegram_type = "supergroup" if chat_type == "group" else chat_type
    message = {"chat": {"id": -1001, "type": telegram_type}, "text": "hello"}

    with locale_context("zh-CN"):
        assert adapter.message_content(message, None).splitlines()[0] == zh_label
    with locale_context("en"):
        assert adapter.message_content(message, None).splitlines()[0] == en_label


def test_telegram_source_uses_the_generic_localized_label() -> None:
    event = IncomingEvent(
        participant_id="tg:main:123",
        source="telegram",
        content="hello",
    )

    with locale_context("zh-CN"):
        assert format_event_text(event).startswith("[来自Telegram][tg:main:123]")
    with locale_context("en"):
        assert "from Telegram" in format_event_text(event)


def test_telegram_header_does_not_repeat_the_generic_source_label() -> None:
    message = {
        "chat": {"id": 8905877830, "type": "private"},
        "text": "/start",
    }
    with locale_context("zh-CN"):
        event = IncomingEvent(
            participant_id="tg:main:8905877830",
            source="telegram",
            content=adapter.message_content(message, None),
        )

        assert format_event_text(event) == (
            "[来自Telegram][tg:main:8905877830]的消息:\n[会话：私聊]\n/start"
        )


def test_telegram_reply_prefers_the_selected_quote() -> None:
    message = {
        "chat": {"id": -1001, "type": "supergroup"},
        "from": {
            "id": 22,
            "username": "bob",
            "first_name": "Bob",
        },
        "text": "收到",
        "quote": {"text": "use prod"},
        "reply_to_message": {
            "from": {
                "id": 11,
                "username": "alice",
                "first_name": "Alice",
            },
            "text": "please use prod tomorrow",
        },
    }

    with locale_context("zh-CN"):
        content = adapter.message_content(message, None)

    assert content == (
        "[会话：群聊]\n"
        "[发送者：Bob；ID：22；用户名：@bob]\n"
        "[引用 Alice（ID：11，用户名：@alice）]\n"
        "> use prod\n"
        "收到"
    )
    assert "please use prod tomorrow" not in content


def test_telegram_reply_falls_back_to_original_media_and_caption() -> None:
    message = {
        "chat": {"id": 123, "type": "private"},
        "text": "What is this?",
        "reply_to_message": {
            "from": {"id": 11, "first_name": "Alice"},
            "voice": {"file_id": "voice", "file_unique_id": "voice-1"},
            "caption": "status update",
        },
    }

    with locale_context("en"):
        content = adapter.message_content(message, None)

    assert content == (
        "[Chat: private]\n"
        "[Reply to Alice (ID: 11, username: -)]\n"
        "> [voice message] telegram-voice-voice-1.ogg (audio/ogg) status update\n"
        "What is this?"
    )


def test_telegram_reply_preview_is_bounded() -> None:
    message = {
        "chat": {"id": 123, "type": "private"},
        "text": "current",
        "reply_to_message": {
            "from": {"id": 11, "first_name": "Alice"},
            "text": "x" * 1200,
        },
    }

    with locale_context("en"):
        content = adapter.message_content(message, None)

    quoted_line = next(line for line in content.splitlines() if line.startswith("> "))
    assert quoted_line == f"> {'x' * 999}…"


def test_telegram_external_reply_and_forward_origins_are_visible() -> None:
    external_reply = {
        "chat": {"id": 123, "type": "private"},
        "text": "external",
        "external_reply": {
            "origin": {
                "type": "hidden_user",
                "sender_user_name": "Hidden Alice",
            },
            "photo": [{"file_id": "photo", "file_unique_id": "photo-1"}],
        },
    }
    forwarded = {
        "chat": {"id": 123, "type": "private"},
        "text": "forwarded",
        "forward_origin": {
            "type": "user",
            "sender_user": {"id": 11, "first_name": "Alice"},
        },
    }

    with locale_context("zh-CN"):
        external_content = adapter.message_content(external_reply, None)
        forwarded_content = adapter.message_content(forwarded, None)

    assert (
        "[外部引用，来源 Hidden Alice]\n> [图片] telegram-photo-photo-1.jpg（image/jpeg）"
    ) in external_content
    assert "[转发自：Alice（ID：11，用户名：-）]" in forwarded_content


@pytest.mark.parametrize(
    ("payload", "expected"),
    (
        ({"sticker": {"emoji": "🎉"}}, "[贴纸 🎉]"),
        (
            {
                "contact": {
                    "first_name": "Alice",
                    "phone_number": "+86123",
                    "user_id": 11,
                }
            },
            "[联系人：Alice；电话：+86123；用户 ID：11]",
        ),
        ({"location": {"latitude": 31.2, "longitude": 121.5}}, "[位置：31.2, 121.5]"),
        (
            {
                "venue": {
                    "title": "Office",
                    "address": "One Road",
                    "location": {"latitude": 31.2, "longitude": 121.5},
                }
            },
            "[地点：Office；地址：One Road；坐标：31.2, 121.5]",
        ),
        (
            {
                "poll": {
                    "question": "Lunch?",
                    "options": [{"text": "Rice"}, {"text": "Noodles"}],
                }
            },
            "[投票：Lunch?；选项：Rice / Noodles]",
        ),
        ({"dice": {"emoji": "🎲", "value": 6}}, "[骰子：🎲 = 6]"),
    ),
)
def test_telegram_structured_messages_have_readable_summaries(
    payload: dict,
    expected: str,
) -> None:
    message = {"chat": {"id": 123, "type": "private"}, **payload}

    with locale_context("zh-CN"):
        assert adapter.message_content(message, None) == f"[会话：私聊]\n{expected}"


def test_telegram_video_note_is_a_downloadable_attachment() -> None:
    media = adapter.media_for(
        {
            "video_note": {
                "file_id": "note",
                "file_unique_id": "note-1",
            }
        }
    )

    assert media == adapter.TelegramMedia(
        file_id="note",
        filename="telegram-video_note-note-1.mp4",
        media_type="video/mp4",
        label_key="channel.telegram.video_note",
    )


def test_telegram_document_summary_includes_filename_type_and_caption() -> None:
    message = {
        "chat": {"id": 123, "type": "private"},
        "document": {
            "file_id": "document",
            "file_unique_id": "document-1",
            "file_name": "quarterly-report.pdf",
            "mime_type": "application/pdf",
        },
        "caption": "Please review this report",
    }

    with locale_context("en"):
        content = adapter.message_content(message, adapter.media_for(message))

    assert content == (
        "[Chat: private]\n[file] quarterly-report.pdf (application/pdf)\nPlease review this report"
    )


def test_telegram_animation_wins_over_compatibility_document() -> None:
    media = adapter.media_for(
        {
            "animation": {
                "file_id": "animation",
                "file_unique_id": "animation-1",
            },
            "document": {
                "file_id": "document",
                "file_unique_id": "document-1",
            },
        }
    )

    assert media is not None
    assert media.file_id == "animation"
    assert media.label_key == "channel.telegram.animation"


def test_split_telegram_text_obeys_limit_and_prefers_newlines() -> None:
    chunks = split_telegram_text("first line\n" + "x" * 20, limit=12)

    assert chunks[0] == "first line"
    assert "".join(chunks) == "first line" + "x" * 20
    assert all(len(chunk) <= 12 for chunk in chunks)
