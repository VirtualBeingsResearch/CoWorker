from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from coworker.channels.registry import ChannelParticipant, CommunicationChannel
from coworker.channels.wecom.runner import WeComRunner, _split_markdown
from coworker.core.config import WeComConfig
from coworker.core.types import CommunicateRequest
from coworker.tools.communicate_tool import CommunicateTool


def _frame_single() -> dict:
    return {
        "headers": {"req_id": "r1"},
        "body": {
            "msgid": "M1",
            "chattype": "single",
            "from": {"userid": "U123"},
            "msgtype": "text",
            "text": {"content": "ping"},
        },
    }


def _make_runner(tmp_path) -> WeComRunner:
    inbox = MagicMock()
    inbox.push = AsyncMock()
    cfg = WeComConfig(enabled=True, bot_id="BID", secret="SEC")
    runner = WeComRunner(cfg=cfg, inbox=inbox, attachments_dir=tmp_path)
    runner._client = AsyncMock()
    return runner


def test_checker_returns_string_chat_type(tmp_path):
    runner = _make_runner(tmp_path)
    runner._contacts["U123"] = "single"

    assert runner.checker("U123") == "wecom:single:U123"


def test_checker_normalizes_legacy_numeric_chat_type(tmp_path):
    runner = _make_runner(tmp_path)
    runner._contacts["U123"] = 1

    assert runner.checker("U123") == "wecom:single:U123"


def test_communication_participants_expose_canonical_ids_and_aliases(tmp_path):
    runner = _make_runner(tmp_path)
    runner._contacts.update({"U123": "single", "CHATX": "group"})

    assert runner.list_communication_participants() == [
        ChannelParticipant(
            "wecom:group:CHATX",
            aliases=("CHATX", "group:CHATX"),
        ),
        ChannelParticipant(
            "wecom:single:U123",
            aliases=("U123", "single:U123"),
        ),
    ]


@pytest.mark.parametrize(
    "mistyped_id",
    ["U124", "wecom:single:U124", "wecm:single:U123"],
)
@pytest.mark.asyncio
async def test_channel_directory_suggests_known_wecom_target(tmp_path, mistyped_id):
    runner = _make_runner(tmp_path)
    runner._contacts["U123"] = "single"
    tool = CommunicateTool(str(tmp_path / "outbox"))
    tool.register_channel(
        CommunicationChannel(
            prefix="wecom:",
            sender=runner.sender,
            participants=runner.list_communication_participants,
        )
    )

    assert tool.resolve_participant_id("U123") == "wecom:single:U123"
    result = await tool.execute(participant_id=mistyped_id, message="hi")

    assert result.is_error is True
    assert "wecom:single:U123" in result.content
    assert "本次消息未发送" in result.content
    runner._client.send_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_channel_directory_does_not_block_dissimilar_full_wecom_target(tmp_path):
    runner = _make_runner(tmp_path)
    runner._contacts["U123"] = "single"
    tool = CommunicateTool(str(tmp_path / "outbox"))
    tool.register_channel(
        CommunicationChannel(
            prefix="wecom:",
            sender=runner.sender,
            participants=runner.list_communication_participants,
        )
    )

    result = await tool.execute(participant_id="wecom:single:U999", message="hi")

    assert result.is_error is False
    runner._client.send_message.assert_awaited_once()
    assert runner._client.send_message.call_args.args[0] == "U999"


def test_load_contacts_normalizes_legacy_numeric_values(tmp_path):
    contacts_path = tmp_path / "wecom_contacts.json"
    contacts_path.write_text('{"U123": 1, "CHATX": 2, "bad": 3}', encoding="utf-8")
    inbox = MagicMock()
    inbox.push = AsyncMock()
    cfg = WeComConfig(enabled=True, bot_id="BID", secret="SEC")

    runner = WeComRunner(
        cfg=cfg,
        inbox=inbox,
        attachments_dir=tmp_path,
        contacts_path=contacts_path,
    )

    assert runner._contacts == {"U123": "single", "CHATX": "group"}


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
    runner._cache_frame("wecom:single:U123", _frame_single())

    await runner.send("wecom:single:U123", "你好", [])

    runner._client.reply_stream.assert_awaited_once()
    runner._client.send_message.assert_not_called()


@pytest.mark.asyncio
async def test_send_uses_send_message_when_no_frame(tmp_path):
    runner = _make_runner(tmp_path)

    await runner.send("wecom:single:U999", "ping", [])

    runner._client.send_message.assert_awaited_once()
    args, _ = runner._client.send_message.call_args
    chatid, body = args
    assert chatid == "U999"
    assert body["msgtype"] == "markdown"
    assert body["markdown"]["content"] == "ping"
    runner._client.reply_stream.assert_not_called()


@pytest.mark.asyncio
async def test_send_chunks_long_markdown(tmp_path):
    runner = _make_runner(tmp_path)
    long_msg = ("para\n\n" + "y" * 10000 + "\n\n") * 4  # > 20480 bytes

    await runner.send("wecom:single:U777", long_msg, [])

    assert runner._client.send_message.await_count >= 2


@pytest.mark.asyncio
async def test_send_with_attachment_uses_reply_media_when_frame(tmp_path):
    runner = _make_runner(tmp_path)
    runner._cache_frame("wecom:single:U123", _frame_single())

    runner._client.upload_media = AsyncMock(return_value={"media_id": "MID-1"})

    img = tmp_path / "a.png"
    img.write_bytes(b"\x89PNGfake-data" * 100)
    await runner.send("wecom:single:U123", "", [{"type": "image", "path": str(img)}])

    runner._client.upload_media.assert_awaited_once()
    runner._client.reply_media.assert_awaited_once()
    args, _ = runner._client.reply_media.call_args
    assert args[1] == "image"
    assert args[2] == "MID-1"


@pytest.mark.asyncio
async def test_send_attachment_after_text_uses_send_media_message(tmp_path):
    """frame 被首条 text 消耗后，attachment 走主动推送。"""
    runner = _make_runner(tmp_path)
    runner._cache_frame("wecom:single:U123", _frame_single())
    runner._client.upload_media = AsyncMock(return_value={"media_id": "MID-2"})

    f = tmp_path / "doc.pdf"
    f.write_bytes(b"\x25\x50\x44\x46-fake" * 100)
    await runner.send(
        "wecom:single:U123",
        "see file",
        [{"type": "file", "path": str(f)}],
    )

    runner._client.reply_stream.assert_awaited_once()
    runner._client.send_media_message.assert_awaited_once()


@pytest.mark.asyncio
async def test_ensure_media_caches_by_path_and_mtime(tmp_path):
    runner = _make_runner(tmp_path)
    runner._client.upload_media = AsyncMock(return_value={"media_id": "MID-X"})

    img = tmp_path / "a.png"
    img.write_bytes(b"\x89PNGfake-data" * 100)
    a = await runner._ensure_media({"type": "image", "path": str(img)})
    b = await runner._ensure_media({"type": "image", "path": str(img)})
    assert a == b == "MID-X"
    runner._client.upload_media.assert_awaited_once()


def test_validate_attachment_rejects_oversize(tmp_path):
    runner = _make_runner(tmp_path)
    big = tmp_path / "big.png"
    # 11MB > image limit 10MB
    big.write_bytes(b"\x00" * (11 * 1024 * 1024))
    with pytest.raises(ValueError):
        runner._validate_attachment({"type": "image", "path": str(big)})


def test_validate_attachment_rejects_unknown_type(tmp_path):
    runner = _make_runner(tmp_path)
    f = tmp_path / "a.png"
    f.write_bytes(b"hello-world-bytes")
    with pytest.raises(ValueError):
        runner._validate_attachment({"type": "weird", "path": str(f)})


def test_take_fresh_frame_returns_none_after_expiry(tmp_path, monkeypatch):
    runner = _make_runner(tmp_path)
    runner._cache_frame("wecom:single:U1", _frame_single())
    # Advance monotonic past TTL
    import coworker.channels.wecom.runner as runner_mod
    base = runner._frame_cache["U1"][1]
    monkeypatch.setattr(runner_mod.time, "monotonic", lambda: base + 1)
    assert runner._take_fresh_frame("U1") is None


def test_take_fresh_frame_pops_value(tmp_path):
    runner = _make_runner(tmp_path)
    runner._cache_frame("wecom:single:U1", _frame_single())
    f = runner._take_fresh_frame("U1")
    assert f is not None
    # second call returns None (popped)
    assert runner._take_fresh_frame("U1") is None


@pytest.mark.asyncio
async def test_sender_returns_tool_result(tmp_path):
    runner = _make_runner(tmp_path)
    result = await runner.sender(
        CommunicateRequest(participant_id="wecom:single:U777", message="hi")
    )
    assert result.is_error is False
    assert "wecom:single:U777" in result.content


@pytest.mark.asyncio
async def test_sender_catches_errors(tmp_path):
    runner = _make_runner(tmp_path)
    runner._client.send_message = AsyncMock(side_effect=RuntimeError("boom"))
    result = await runner.sender(
        CommunicateRequest(participant_id="wecom:single:U777", message="hi")
    )
    assert result.is_error is True
    assert "boom" in result.content


@pytest.mark.asyncio
async def test_sender_rejects_unsupported_request_fields(tmp_path):
    runner = _make_runner(tmp_path)

    conversation_result = await runner.sender(
        CommunicateRequest(
            participant_id="wecom:single:U777",
            message="hi",
            conversation_id="thr_1",
        )
    )
    extra_result = await runner.sender(
        CommunicateRequest(
            participant_id="wecom:single:U777",
            message="hi",
            extra={"mode": "plan"},
        )
    )

    assert conversation_result.is_error is True
    assert conversation_result.content.startswith("消息发送失败：")
    assert "不支持 conversation_id" in conversation_result.content
    assert extra_result.is_error is True
    assert extra_result.content.startswith("消息发送失败：")
    assert "不支持 extra" in extra_result.content
