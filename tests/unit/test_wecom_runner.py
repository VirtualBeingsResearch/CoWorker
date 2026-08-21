from __future__ import annotations

import asyncio
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError

from coworker.channels.access import ChannelAccessController
from coworker.channels.activity import ChannelActivityStore
from coworker.channels.registry import ChannelRegistry
from coworker.channels.wecom.channel import WeComChannel
from coworker.channels.wecom.runner import WeComRunner
from coworker.channels.wecom.sender import split_markdown as _split_markdown
from coworker.core.config import ChannelAccessConfig, Config, WeComConfig
from coworker.core.types import CommunicateRequest
from coworker.i18n import locale_context


def _frame_single(request_id: str = "r1", message_id: str = "M1") -> dict:
    return {
        "headers": {"req_id": request_id},
        "body": {
            "msgid": message_id,
            "chattype": "single",
            "from": {"userid": "U123"},
            "msgtype": "text",
            "text": {"content": "ping"},
        },
    }


def _make_runner(
    tmp_path,
    *,
    bots: dict | None = None,
    contacts_path=None,
    activity=None,
) -> WeComRunner:
    cfg = WeComConfig(
        bots=bots
        or {"default": {"enabled": True, "bot_id": "BID", "secret": "SEC"}}
    )
    runner = WeComRunner(
        cfg=cfg,
        attachments_dir=tmp_path,
        contacts_path=contacts_path,
        activity=activity,
    )
    for bot in runner._bots.values():
        bot._client = AsyncMock()
    return runner


def _bot(runner: WeComRunner, instance: str = "default"):
    return runner._bots[instance]


async def _wait_until(predicate, attempts: int = 50) -> None:
    for _ in range(attempts):
        if predicate():
            return
        await asyncio.sleep(0)
    raise AssertionError("condition was not reached")


@pytest.mark.asyncio
async def test_runtime_hot_reconfigures_without_registry_replacement(tmp_path, monkeypatch):
    clients = []

    class FakeClient:
        def __init__(self, **kwargs):
            self.kwargs = kwargs
            self.handlers = {}
            self.connect_count = 0
            self.disconnect_count = 0
            clients.append(self)

        def on(self, event, handler):
            self.handlers[event] = handler

        async def connect(self):
            self.connect_count += 1

        async def disconnect(self):
            self.disconnect_count += 1

    monkeypatch.setitem(sys.modules, "wecom_aibot_sdk", SimpleNamespace(WSClient=FakeClient))
    runner = WeComRunner(
        cfg=WeComConfig(bots={}),
        attachments_dir=tmp_path,
    )
    runtime_task = asyncio.create_task(runner.start())
    await asyncio.sleep(0)
    assert clients == []

    # 启用一个实例并连接
    await runner.reconfigure(
        WeComConfig(bots={"main": {"enabled": True, "bot_id": "first", "secret": "secret"}})
    )
    await _wait_until(lambda: len(clients) == 1 and clients[0].connect_count == 1)

    bot_main = _bot(runner, "main")
    bot_main._cache_frame("wecom:main:single:U1", "request", _frame_single())

    # 改配置触发重连
    await runner.reconfigure(
        WeComConfig(bots={"main": {"enabled": True, "bot_id": "second", "secret": "next"}})
    )
    await _wait_until(lambda: len(clients) == 2 and clients[1].connect_count == 1)
    assert clients[0].disconnect_count >= 1
    assert clients[1].kwargs["bot_id"] == "second"
    assert _bot(runner, "main")._frame_cache == {}

    # 热删实例
    await runner.reconfigure(WeComConfig(bots={}))
    await _wait_until(lambda: clients[1].disconnect_count >= 1)
    await asyncio.sleep(0)
    assert runner._bots == {}

    await runner.stop()
    await runtime_task


@pytest.mark.asyncio
async def test_kicked_runtime_waits_for_new_configuration(tmp_path, monkeypatch):
    clients = []

    class FakeClient:
        def __init__(self, **kwargs):
            self.handlers = {}
            clients.append(self)

        def on(self, event, handler):
            self.handlers[event] = handler

        async def connect(self):
            return None

        async def disconnect(self):
            return None

    monkeypatch.setitem(sys.modules, "wecom_aibot_sdk", SimpleNamespace(WSClient=FakeClient))
    runner = WeComRunner(
        cfg=WeComConfig(bots={"main": {"enabled": True, "bot_id": "first", "secret": "secret"}}),
        attachments_dir=tmp_path,
    )
    runtime_task = asyncio.create_task(runner.start())
    await _wait_until(lambda: len(clients) == 1)

    await _bot(runner, "main")._on_kicked({})
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert len(clients) == 1
    assert not runner._stop.is_set()

    await runner.reconfigure(
        WeComConfig(bots={"main": {"enabled": True, "bot_id": "second", "secret": "next"}})
    )
    await _wait_until(lambda: len(clients) == 2)

    await runner.stop()
    await runtime_task


def test_resolver_returns_string_chat_type(tmp_path):
    runner = _make_runner(tmp_path)
    _bot(runner)._contacts["U123"] = "single"

    assert runner.resolve_participant("U123") == "wecom:default:single:U123"


def test_resolver_normalizes_legacy_numeric_chat_type(tmp_path):
    runner = _make_runner(tmp_path)
    _bot(runner)._contacts["U123"] = 1

    assert runner.resolve_participant("U123") == "wecom:default:single:U123"


def test_resolver_prefers_instance_hint(tmp_path):
    runner = _make_runner(
        tmp_path,
        bots={
            "main": {"bot_id": "b1", "secret": "s1"},
            "work": {"bot_id": "b2", "secret": "s2"},
        },
    )
    # 同一 chat 同时出现在两个实例时，不带提示会解析为 None（存在歧义）
    _bot(runner, "main")._contacts["U9"] = "single"
    _bot(runner, "work")._contacts["U9"] = "single"
    assert runner.resolve_participant("U9") is None
    # 带实例提示则明确归属
    assert runner.resolve_participant("work:U9") == "wecom:work:single:U9"


def test_resolver_unique_across_instances(tmp_path):
    runner = _make_runner(
        tmp_path,
        bots={
            "main": {"bot_id": "b1", "secret": "s1"},
            "work": {"bot_id": "b2", "secret": "s2"},
        },
    )
    _bot(runner, "main")._contacts["U9"] = "single"
    assert runner.resolve_participant("U9") == "wecom:main:single:U9"


def test_load_contacts_normalizes_legacy_numeric_values(tmp_path):
    # 旧版单实例联系人文件，应一次性迁移到 default 实例（含数字归一化）
    legacy = tmp_path / "wecom_contacts.json"
    legacy.write_text('{"U123": 1, "CHATX": 2, "bad": 3}', encoding="utf-8")

    runner = WeComRunner(
        cfg=WeComConfig(bots={"default": {"bot_id": "BID", "secret": "SEC"}}),
        attachments_dir=tmp_path,
        contacts_path=legacy,
    )

    assert _bot(runner, "default")._contacts == {"U123": "single", "CHATX": "group"}
    assert (tmp_path / "wecom_contacts_default.json").exists()


def test_load_contacts_does_not_migrate_non_default_instance(tmp_path):
    legacy = tmp_path / "wecom_contacts.json"
    legacy.write_text('{"U123": "single"}', encoding="utf-8")
    runner = _make_runner(tmp_path, contacts_path=legacy)
    # default 迁移
    assert _bot(runner, "default")._contacts == {"U123": "single"}
    # 再建一个非 default 实例，不应从旧文件迁移内容
    runner2 = WeComRunner(
        cfg=WeComConfig(bots={"work": {"bot_id": "b2", "secret": "s2"}}),
        attachments_dir=tmp_path,
        contacts_path=legacy,
    )
    assert _bot(runner2, "work")._contacts == {}


def test_wecom_config_folds_legacy_flat_into_default():
    cfg = WeComConfig(enabled=True, bot_id="BID", secret="SEC", ws_url="wss://x/ws")
    assert set(cfg.bots) == {"default"}
    bot = cfg.bots["default"]
    assert bot.enabled is True
    assert bot.bot_id == "BID"
    assert bot.secret == "SEC"
    assert bot.ws_url == "wss://x/ws"


def test_wecom_config_empty_has_no_bots():
    assert WeComConfig().bots == {}


def test_wecom_config_prefers_explicit_bots_over_legacy_flat():
    cfg = WeComConfig(bot_id="x", bots={"main": {"bot_id": "y"}})

    assert set(cfg.bots) == {"main"}
    assert cfg.bots["main"].bot_id == "y"
    assert cfg.bot_id == ""


def test_config_loads_legacy_wecom_flat_env_without_conflict(monkeypatch):
    monkeypatch.setenv("WECOM__ENABLED", "true")
    monkeypatch.setenv("WECOM__BOT_ID", "BID")
    monkeypatch.setenv("WECOM__SECRET", "SEC")
    monkeypatch.setenv("WECOM__WS_URL", "wss://x/ws")

    cfg = Config()

    assert set(cfg.wecom.bots) == {"default"}
    assert cfg.wecom.bots["default"].bot_id == "BID"
    assert cfg.wecom.bots["default"].secret == "SEC"
    assert cfg.wecom.bots["default"].ws_url == "wss://x/ws"


def test_config_prefers_explicit_wecom_bots_over_legacy_flat_in_merged_dict():
    cfg = Config.model_validate(
        {
            "wecom": {
                "enabled": True,
                "bot_id": "old",
                "secret": "old-secret",
                "ws_url": "wss://old/ws",
                "bots": {
                    "main": {
                        "enabled": True,
                        "bot_id": "new",
                        "secret": "new-secret",
                        "ws_url": "",
                    }
                },
            }
        }
    )

    assert set(cfg.wecom.bots) == {"main"}
    assert cfg.wecom.bots["main"].bot_id == "new"
    assert cfg.wecom.bot_id == ""


def test_wecom_config_rejects_invalid_instance_id():
    with pytest.raises(ValidationError):
        WeComConfig(bots={"Bad!": {"bot_id": "y"}})


def test_split_markdown_single_paragraph_under_limit():
    text = "hello world"
    assert _split_markdown(text) == [text]


def test_split_markdown_paragraph_break():
    para = "x" * 10000
    big = "\n\n".join([para, para, para])  # ~30k bytes
    chunks = _split_markdown(big, max_bytes=15000)
    assert len(chunks) >= 2
    # 没有任何块超出限制
    for c in chunks:
        assert len(c.encode("utf-8")) <= 15000


def test_split_markdown_hard_split_oversize_paragraph():
    huge = "x" * 50000
    chunks = _split_markdown(huge, max_bytes=15000)
    assert len(chunks) >= 4
    for c in chunks:
        assert len(c.encode("utf-8")) <= 15000


@pytest.mark.asyncio
async def test_send_uses_reply_stream_when_frame_cached(tmp_path):
    runner = _make_runner(tmp_path)
    bot = _bot(runner)
    bot._cache_frame("wecom:default:single:U123", "r1", _frame_single())

    await runner.send("wecom:default:single:U123", "你好", [])

    bot._client.reply_stream.assert_awaited_once()
    bot._client.send_message.assert_not_called()
    sent_at, received_at = runner.activity_for("wecom:default:single:U123")
    assert sent_at is not None
    assert received_at is not None


@pytest.mark.asyncio
async def test_single_inbound_frame_supports_reply_without_conversation_id(tmp_path):
    runner = _make_runner(tmp_path)
    bot = _bot(runner)
    handler = AsyncMock()
    runner.set_inbound_handler(handler)
    frame = _frame_single()

    await bot._on_text_like(frame)
    await runner.send("wecom:default:single:U123", "reply", [])

    handler.assert_awaited_once()
    event = handler.await_args.args[0]
    assert event.participant_id == "wecom:default:single:U123"
    assert event.conversation_id is None
    assert event.content == "ping"
    assert bot._client.reply_stream.await_args.args[0] is frame


@pytest.mark.asyncio
async def test_text_inbound_downloads_quoted_attachment(tmp_path):
    runner = _make_runner(tmp_path)
    bot = _bot(runner)
    handler = AsyncMock()
    runner.set_inbound_handler(handler)
    frame = _frame_single()
    frame["body"]["quote"] = {
        "msgtype": "file",
        "file": {
            "url": "https://x/quoted-file",
            "aeskey": "QF",
            "name": "report.pdf",
        },
    }
    bot._client.download_file = AsyncMock(
        return_value={"buffer": b"%PDF-quoted", "filename": "report.pdf"}
    )

    await bot._on_text_like(frame)

    handler.assert_awaited_once()
    event = handler.await_args.args[0]
    assert [(att.filename, att.media_type) for att in event.attachments] == [
        ("report.pdf", "application/pdf")
    ]
    bot._client.download_file.assert_awaited_once_with(
        "https://x/quoted-file",
        "QF",
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("locale", "quote_notice", "failure_notice"),
    [
        (
            "zh-CN",
            '[引用的文件 "report.pdf"]',
            '[引用附件 文件 "report.pdf" 下载失败]',
        ),
        (
            "en",
            '[quoted file "report.pdf"]',
            '[failed to download quoted attachment file "report.pdf"]',
        ),
    ],
)
async def test_text_inbound_identifies_quoted_attachment_download_failure(
    tmp_path,
    locale,
    quote_notice,
    failure_notice,
):
    runner = _make_runner(tmp_path)
    bot = _bot(runner)
    handler = AsyncMock()
    runner.set_inbound_handler(handler)
    frame = _frame_single()
    frame["body"]["quote"] = {
        "msgtype": "file",
        "file": {
            "url": "https://secret.example/quoted-file",
            "aeskey": "SECRET-AES-KEY",
            "name": "report.pdf",
        },
    }
    bot._client.download_file = AsyncMock(
        side_effect=RuntimeError("sensitive transport detail")
    )

    with locale_context(locale):
        await bot._on_text_like(frame)

    handler.assert_awaited_once()
    event = handler.await_args.args[0]
    assert event.attachments == []
    assert quote_notice in event.content
    assert failure_notice in event.content
    assert "https://secret.example" not in event.content
    assert "SECRET-AES-KEY" not in event.content
    assert "sensitive transport detail" not in event.content


@pytest.mark.asyncio
async def test_text_inbound_access_is_checked_before_quoted_attachment_download(
    tmp_path,
):
    runner = _make_runner(tmp_path)
    bot = _bot(runner)
    handler = AsyncMock()
    runner.set_inbound_handler(handler)
    runner.set_access_controller(
        ChannelAccessController(
            ChannelAccessConfig.model_validate(
                {"wecom": {"inbound_deny": ["wecom:default:single:U123"]}}
            )
        )
    )
    frame = _frame_single()
    frame["body"]["quote"] = {
        "msgtype": "file",
        "file": {"url": "https://x/quoted-file", "aeskey": "QF"},
    }

    await bot._on_text_like(frame)

    bot._client.download_file.assert_not_awaited()
    handler.assert_not_awaited()


@pytest.mark.asyncio
async def test_inbound_access_is_checked_before_attachment_download_and_cache(
    tmp_path,
    monkeypatch,
):
    runner = _make_runner(tmp_path)
    bot = _bot(runner)
    handler = AsyncMock()
    collect_attachments = AsyncMock(return_value=[])
    monkeypatch.setattr(
        "coworker.channels.wecom.adapter.collect_attachments",
        collect_attachments,
    )
    runner.set_inbound_handler(handler)
    runner.set_access_controller(
        ChannelAccessController(
            ChannelAccessConfig.model_validate(
                {"wecom": {"inbound_deny": ["wecom:default:single:U123"]}}
            )
        )
    )
    frame = _frame_single()
    frame["body"]["msgtype"] = "image"

    await bot._on_with_attachments(frame)

    collect_attachments.assert_not_awaited()
    handler.assert_not_awaited()
    bot._client.reply_stream.assert_awaited_once()
    assert bot._client.reply_stream.await_args.args[0] is frame
    assert "拒绝" in bot._client.reply_stream.await_args.args[2]
    assert bot._client.reply_stream.await_args.kwargs["finish"] is True
    assert bot._frame_cache == {}
    assert bot._contacts == {}
    assert runner.activity_for("wecom:default:single:U123") == (None, None)
    assert [entry["status"] for entry in runner._access.traffic.recent(2)] == [  # noqa: SLF001
        "sent",
        "denied",
    ]


def test_channel_lists_latest_activity_times(tmp_path):
    runner = _make_runner(tmp_path)
    bot = _bot(runner)
    bot._contacts["U123"] = "single"
    bot._cache_frame("wecom:default:single:U123", "r1", _frame_single())

    info = WeComChannel(runner).list_connections()[0]

    assert info.participant_id == "wecom:default:single:U123"
    assert info.active is True
    assert info.last_sent_at is None
    assert info.last_received_at is not None


def test_channel_lists_connections_across_instances(tmp_path):
    runner = _make_runner(
        tmp_path,
        bots={
            "main": {"bot_id": "b1", "secret": "s1"},
            "work": {"bot_id": "b2", "secret": "s2"},
        },
    )
    _bot(runner, "main")._contacts["U1"] = "single"
    _bot(runner, "work")._contacts["G1"] = "group"

    infos = WeComChannel(runner).list_connections()
    ids = {info.participant_id for info in infos}
    assert ids == {"wecom:main:single:U1", "wecom:work:group:G1"}


def test_activity_times_survive_runner_restart(tmp_path):
    activity_path = tmp_path / "channel_activity.json"
    first = WeComRunner(
        cfg=WeComConfig(bots={"default": {"bot_id": "BID", "secret": "SEC"}}),
        attachments_dir=tmp_path,
        activity=ChannelActivityStore(activity_path),
    )
    _bot(first)._cache_frame("wecom:default:single:U123", "r1", _frame_single())

    restored = WeComRunner(
        cfg=WeComConfig(bots={"default": {"bot_id": "BID", "secret": "SEC"}}),
        attachments_dir=tmp_path,
        activity=ChannelActivityStore(activity_path),
    )

    assert restored.activity_for("wecom:default:single:U123") == first.activity_for(
        "wecom:default:single:U123"
    )


@pytest.mark.asyncio
async def test_send_uses_send_message_when_no_frame(tmp_path):
    runner = _make_runner(tmp_path)
    bot = _bot(runner)

    await runner.send("wecom:default:single:U999", "ping", [])

    bot._client.send_message.assert_awaited_once()
    args, _ = bot._client.send_message.call_args
    chatid, body = args
    assert chatid == "U999"
    assert body["msgtype"] == "markdown"
    assert body["markdown"]["content"] == "ping"
    bot._client.reply_stream.assert_not_called()


@pytest.mark.asyncio
async def test_send_chunks_long_markdown(tmp_path):
    runner = _make_runner(tmp_path)
    bot = _bot(runner)
    long_msg = ("para\n\n" + "y" * 10000 + "\n\n") * 4  # > 20480 bytes

    await runner.send("wecom:default:single:U777", long_msg, [])

    assert bot._client.send_message.await_count >= 2


@pytest.mark.asyncio
async def test_send_with_attachment_uses_reply_media_when_frame(tmp_path):
    runner = _make_runner(tmp_path)
    bot = _bot(runner)
    bot._cache_frame("wecom:default:single:U123", "r1", _frame_single())

    bot._client.upload_media = AsyncMock(return_value={"media_id": "MID-1"})

    img = tmp_path / "a.png"
    img.write_bytes(b"\x89PNGfake-data" * 100)
    await runner.send("wecom:default:single:U123", "", [{"type": "image", "path": str(img)}])

    bot._client.upload_media.assert_awaited_once()
    bot._client.reply_media.assert_awaited_once()
    args, _ = bot._client.reply_media.call_args
    assert args[1] == "image"
    assert args[2] == "MID-1"


@pytest.mark.asyncio
async def test_send_attachment_after_text_uses_send_media_message(tmp_path):
    """frame 被首条 text 消耗后，attachment 走主动推送。"""
    runner = _make_runner(tmp_path)
    bot = _bot(runner)
    bot._cache_frame("wecom:default:single:U123", "r1", _frame_single())
    bot._client.upload_media = AsyncMock(return_value={"media_id": "MID-2"})

    f = tmp_path / "doc.pdf"
    f.write_bytes(b"\x25\x50\x44\x46-fake" * 100)
    await runner.send(
        "wecom:default:single:U123",
        "see file",
        [{"type": "file", "path": str(f)}],
    )

    bot._client.reply_stream.assert_awaited_once()
    bot._client.send_media_message.assert_awaited_once()


@pytest.mark.asyncio
async def test_send_routes_to_correct_instance(tmp_path):
    runner = _make_runner(
        tmp_path,
        bots={
            "main": {"bot_id": "b1", "secret": "s1"},
            "work": {"bot_id": "b2", "secret": "s2"},
        },
    )
    main_bot = _bot(runner, "main")
    work_bot = _bot(runner, "work")

    await runner.send("wecom:main:single:U1", "hello main", [])
    await runner.send("wecom:work:single:U2", "hello work", [])

    main_bot._client.send_message.assert_awaited_once()
    work_bot._client.send_message.assert_awaited_once()
    assert main_bot._client.send_message.await_args.args[1]["markdown"]["content"] == "hello main"
    assert work_bot._client.send_message.await_args.args[1]["markdown"]["content"] == "hello work"


@pytest.mark.asyncio
async def test_send_unknown_instance_raises(tmp_path):
    runner = _make_runner(tmp_path)
    with pytest.raises(ValueError):
        await runner.send("wecom:nope:single:U1", "hi", [])


@pytest.mark.asyncio
async def test_ensure_media_caches_by_path_and_mtime(tmp_path):
    runner = _make_runner(tmp_path)
    bot = _bot(runner)
    bot._client.upload_media = AsyncMock(return_value={"media_id": "MID-X"})

    img = tmp_path / "a.png"
    img.write_bytes(b"\x89PNGfake-data" * 100)
    a = await bot._sender._ensure_media({"type": "image", "path": str(img)})
    b = await bot._sender._ensure_media({"type": "image", "path": str(img)})
    assert a == b == "MID-X"
    bot._client.upload_media.assert_awaited_once()


def test_validate_attachment_rejects_oversize(tmp_path):
    runner = _make_runner(tmp_path)
    big = tmp_path / "big.png"
    # 11MB > image limit 10MB
    big.write_bytes(b"\x00" * (11 * 1024 * 1024))
    with pytest.raises(ValueError):
        _bot(runner)._sender._validate_attachment({"type": "image", "path": str(big)})


def test_validate_attachment_rejects_unknown_type(tmp_path):
    runner = _make_runner(tmp_path)
    f = tmp_path / "a.png"
    f.write_bytes(b"hello-world-bytes")
    with pytest.raises(ValueError):
        _bot(runner)._sender._validate_attachment({"type": "weird", "path": str(f)})


def test_take_fresh_frame_returns_none_after_expiry(tmp_path, monkeypatch):
    runner = _make_runner(tmp_path)
    bot = _bot(runner)
    bot._cache_frame("wecom:default:single:U1", "r1", _frame_single())
    # Advance monotonic past TTL
    import coworker.channels.wecom.runner as runner_mod
    base = bot._frame_cache[("U1", "r1")][1]
    monkeypatch.setattr(runner_mod.time, "monotonic", lambda: base + 1)
    assert bot._take_fresh_frame("U1", "r1") is None


def test_take_fresh_frame_pops_value(tmp_path):
    runner = _make_runner(tmp_path)
    bot = _bot(runner)
    bot._cache_frame("wecom:default:single:U1", "r1", _frame_single())
    f = bot._take_fresh_frame("U1", "r1")
    assert f is not None
    # second call returns None (popped)
    assert bot._take_fresh_frame("U1", "r1") is None


@pytest.mark.asyncio
async def test_send_uses_frame_matching_conversation_id(tmp_path):
    runner = _make_runner(tmp_path)
    bot = _bot(runner)
    first = _frame_single("r1", "M1")
    second = _frame_single("r2", "M2")
    bot._cache_frame("wecom:default:single:U123", "r1", first)
    bot._cache_frame("wecom:default:single:U123", "r2", second)

    await runner.send("wecom:default:single:U123", "reply first", [], "r1")

    assert bot._client.reply_stream.await_args.args[0] is first
    assert ("U123", "r2") in bot._frame_cache


@pytest.mark.asyncio
async def test_missing_conversation_frame_never_replies_to_another_frame(tmp_path):
    runner = _make_runner(tmp_path)
    bot = _bot(runner)
    bot._cache_frame("wecom:default:single:U123", "r2", _frame_single("r2", "M2"))

    await runner.send("wecom:default:single:U123", "late reply", [], "r1")

    bot._client.reply_stream.assert_not_called()
    bot._client.send_message.assert_awaited_once()
    assert ("U123", "r2") in bot._frame_cache


@pytest.mark.asyncio
async def test_group_send_without_conversation_id_stays_proactive(tmp_path):
    runner = _make_runner(tmp_path)
    bot = _bot(runner)
    bot._cache_frame("wecom:default:group:TEAM", "r1", _frame_single("r1", "M1"))

    await runner.send("wecom:default:group:TEAM", "announcement", [])

    bot._client.reply_stream.assert_not_called()
    bot._client.send_message.assert_awaited_once()
    assert ("TEAM", "r1") in bot._frame_cache


@pytest.mark.asyncio
async def test_sender_returns_tool_result(tmp_path):
    runner = _make_runner(tmp_path)
    channel = WeComChannel(runner)
    result = await channel.send(
        CommunicateRequest(participant_id="wecom:default:single:U777", message="hi")
    )
    assert result.is_error is False
    assert "wecom:default:single:U777" in result.content


@pytest.mark.asyncio
async def test_sender_catches_errors(tmp_path):
    runner = _make_runner(tmp_path)
    bot = _bot(runner)
    bot._client.send_message = AsyncMock(side_effect=RuntimeError("boom"))
    channel = WeComChannel(runner)
    result = await channel.send(
        CommunicateRequest(participant_id="wecom:default:single:U777", message="hi")
    )
    assert result.is_error is True
    assert "boom" in result.content
    assert runner.activity_for("wecom:default:single:U777")[0] is None


@pytest.mark.asyncio
async def test_channel_omits_unsupported_extra_without_changing_message(tmp_path):
    runner = _make_runner(tmp_path)
    bot = _bot(runner)
    bot._contacts["TEAM"] = "group"
    registry = ChannelRegistry()
    registry.register(WeComChannel(runner))

    result = await registry.send(
        CommunicateRequest(
            participant_id="wecom:default:group:TEAM",
            message="请看这里",
            conversation_id="thr_1",
            extra={"mentioned_list": ["alice"], "mode": "plan"},
        )
    )

    assert not result.is_error
    assert "不支持 extra" in result.content
    assert "这些字段未被传递" in result.content
    assert "不支持 conversation_id" not in result.content
    _, body = bot._client.send_message.await_args.args
    assert body == {
        "msgtype": "markdown",
        "markdown": {"content": "请看这里"},
    }


@pytest.mark.asyncio
async def test_channel_uses_native_stream_reply_when_extra_is_omitted(tmp_path):
    runner = _make_runner(tmp_path)
    bot = _bot(runner)
    bot._contacts["TEAM"] = "group"
    bot._cache_frame("wecom:default:group:TEAM", "r1", _frame_single("r1"))
    registry = ChannelRegistry()
    registry.register(WeComChannel(runner))

    result = await registry.send(
        CommunicateRequest(
            participant_id="wecom:default:group:TEAM",
            message="请看这里",
            conversation_id="r1",
            extra={"mentioned_list": ["alice"]},
        )
    )

    assert not result.is_error
    assert "不支持 extra" in result.content
    bot._client.reply_stream.assert_awaited_once()
    assert bot._client.reply_stream.await_args.args[2] == "请看这里"
    bot._client.reply.assert_not_awaited()
