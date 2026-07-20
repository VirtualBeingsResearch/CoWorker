from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from coworker.agent.incoming_content import format_event_text
from coworker.channels.wecom import adapter


def _text_single() -> dict:
    return {
        "cmd": "aibot_msg_callback",
        "headers": {"req_id": "r1"},
        "body": {
            "msgid": "M1",
            "aibotid": "AIBOTID",
            "chattype": "single",
            "from": {"userid": "U123"},
            "msgtype": "text",
            "text": {"content": "你好"},
        },
    }


def _text_group() -> dict:
    return {
        "cmd": "aibot_msg_callback",
        "headers": {"req_id": "r2"},
        "body": {
            "msgid": "M2",
            "aibotid": "AIBOTID",
            "chatid": "CHATX",
            "chattype": "group",
            "from": {"userid": "Ualice"},
            "msgtype": "text",
            "text": {"content": "@robot 帮忙"},
        },
    }


def _voice_single() -> dict:
    return {
        "cmd": "aibot_msg_callback",
        "headers": {"req_id": "r3"},
        "body": {
            "msgid": "M3",
            "aibotid": "AIBOTID",
            "chattype": "single",
            "from": {"userid": "U123"},
            "msgtype": "voice",
            "voice": {"content": "这是语音转的文字"},
        },
    }


def _image_single() -> dict:
    return {
        "cmd": "aibot_msg_callback",
        "headers": {"req_id": "r4"},
        "body": {
            "msgid": "M4",
            "aibotid": "AIBOTID",
            "chattype": "single",
            "from": {"userid": "U123"},
            "msgtype": "image",
            "image": {"url": "https://x/y", "aeskey": "AESKEY"},
        },
    }


def _mixed_group() -> dict:
    return {
        "cmd": "aibot_msg_callback",
        "headers": {"req_id": "r5"},
        "body": {
            "msgid": "M5",
            "aibotid": "AIBOTID",
            "chatid": "CHATY",
            "chattype": "group",
            "from": {"userid": "Ubob"},
            "msgtype": "mixed",
            "mixed": {
                "msg_item": [
                    {"msgtype": "text", "text": {"content": "看这个图"}},
                    {"msgtype": "image", "image": {"url": "https://x/img1", "aeskey": "K1"}},
                    {"msgtype": "text", "text": {"content": "明天讨论"}},
                ]
            },
        },
    }


def test_parse_participant_single():
    assert adapter.parse_participant("wecom:single:U123") == ("single", "U123")


def test_parse_participant_group():
    assert adapter.parse_participant("wecom:group:CHATX") == ("group", "CHATX")


def test_parse_participant_invalid():
    with pytest.raises(ValueError):
        adapter.parse_participant("rest:alice")


def test_participant_id_for_single():
    assert adapter.participant_id_for(_text_single()) == "wecom:single:U123"


def test_participant_id_for_group():
    assert adapter.participant_id_for(_text_group()) == "wecom:group:CHATX"


def test_roundtrip_single():
    frame = _text_single()
    pid = adapter.participant_id_for(frame)
    chat_type, chat_id = adapter.parse_participant(pid)
    assert chat_type == "single" and chat_id == "U123"


def test_roundtrip_group():
    frame = _text_group()
    pid = adapter.participant_id_for(frame)
    chat_type, chat_id = adapter.parse_participant(pid)
    assert chat_type == "group" and chat_id == "CHATX"


def test_frame_to_event_text_single():
    event = adapter.frame_to_event(_text_single(), attachments=[])
    assert event.participant_id == "wecom:single:U123"
    assert event.source == "wecom"
    assert event.content == "你好"
    assert format_event_text(event) == (
        "[来自企业微信][wecom:single:U123]的消息:\n你好"
    )


def test_frame_to_event_voice_uses_transcript():
    event = adapter.frame_to_event(_voice_single(), attachments=[])
    assert "这是语音转的文字" in event.content


def test_frame_to_event_group_includes_chatid_and_userid():
    event = adapter.frame_to_event(_text_group(), attachments=[])
    assert event.participant_id == "wecom:group:CHATX"
    assert event.content == "[发送者 userid=Ualice]\n@robot 帮忙"
    assert format_event_text(event) == (
        "[来自企业微信][wecom:group:CHATX]的消息:\n"
        "[发送者 userid=Ualice]\n@robot 帮忙"
    )


def test_frame_to_event_mixed_concats_text_items():
    event = adapter.frame_to_event(_mixed_group(), attachments=[])
    assert "看这个图" in event.content
    assert "明天讨论" in event.content


@pytest.mark.asyncio
async def test_collect_attachments_image(tmp_path):
    client = AsyncMock()
    client.download_file = AsyncMock(return_value={"buffer": b"\x89PNG-fake-bytes", "filename": "shot.png"})
    atts = await adapter.collect_attachments(client, _image_single(), tmp_path)
    assert len(atts) == 1
    assert atts[0].filename == "shot.png"
    assert atts[0].media_type == "image/png"
    # small image inlined as base64
    assert atts[0].data is not None
    client.download_file.assert_awaited_once_with("https://x/y", "AESKEY")


@pytest.mark.asyncio
async def test_collect_attachments_mixed_only_images(tmp_path):
    client = AsyncMock()
    client.download_file = AsyncMock(return_value={"buffer": b"\x89PNG-fake", "filename": "m.png"})
    atts = await adapter.collect_attachments(client, _mixed_group(), tmp_path)
    assert len(atts) == 1
    client.download_file.assert_awaited_once_with("https://x/img1", "K1")


def _reply_text_single() -> dict:
    return {
        "cmd": "aibot_msg_callback",
        "headers": {"req_id": "r6"},
        "body": {
            "msgid": "M6",
            "aibotid": "AIBOTID",
            "chattype": "single",
            "from": {"userid": "U123"},
            "msgtype": "text",
            "text": {"content": "好的"},
            "msgquote": {
                "msgtype": "text",
                "text": {"content": "请帮我总结一下"},
                "from_userid": "U456",
            },
        },
    }


def _reply_image_single() -> dict:
    return {
        "cmd": "aibot_msg_callback",
        "headers": {"req_id": "r7"},
        "body": {
            "msgid": "M7",
            "aibotid": "AIBOTID",
            "chattype": "single",
            "from": {"userid": "U123"},
            "msgtype": "text",
            "text": {"content": "这图是什么"},
            "msgquote": {
                "msgtype": "image",
                "from_userid": "U456",
            },
        },
    }


def test_frame_to_event_reply_text_with_from_userid():
    event = adapter.frame_to_event(_reply_text_single(), attachments=[])
    assert '[引用 U456: "请帮我总结一下"]' in event.content
    assert "好的" in event.content


def test_frame_to_event_reply_text_without_from_userid():
    frame = _reply_text_single()
    del frame["body"]["msgquote"]["from_userid"]
    event = adapter.frame_to_event(frame, attachments=[])
    assert '[引用: "请帮我总结一下"]' in event.content


def test_frame_to_event_reply_image_with_from_userid():
    event = adapter.frame_to_event(_reply_image_single(), attachments=[])
    assert "[引用 U456 的图片]" in event.content
    assert "这图是什么" in event.content


def test_frame_to_event_reply_voice_shows_transcript():
    frame = {
        "cmd": "aibot_msg_callback",
        "headers": {"req_id": "r8"},
        "body": {
            "msgid": "M8",
            "aibotid": "AIBOTID",
            "chattype": "single",
            "from": {"userid": "U123"},
            "msgtype": "text",
            "text": {"content": "明白了"},
            "msgquote": {
                "msgtype": "voice",
                "voice": {"content": "请帮我处理这件事"},
                "from_userid": "U456",
            },
        },
    }
    event = adapter.frame_to_event(frame, attachments=[])
    assert '[引用 U456: "请帮我处理这件事"]' in event.content


def _make_media_reply(qtype: str, payload: dict) -> dict:
    return {
        "cmd": "aibot_msg_callback",
        "headers": {"req_id": "r10"},
        "body": {
            "msgid": "M10",
            "aibotid": "AIBOTID",
            "chattype": "single",
            "from": {"userid": "U123"},
            "msgtype": "text",
            "text": {"content": "看看"},
            "msgquote": {"msgtype": qtype, qtype: payload, "from_userid": "U456"},
        },
    }


def test_frame_to_event_reply_image_with_url():
    frame = _make_media_reply("image", {"url": "https://cdn.example.com/img.jpg", "aeskey": "K"})
    event = adapter.frame_to_event(frame, attachments=[])
    assert "[引用 U456 的图片: https://cdn.example.com/img.jpg]" in event.content


def test_frame_to_event_reply_file_with_name_and_url():
    frame = _make_media_reply("file", {"url": "https://cdn.example.com/doc.pdf", "aeskey": "K", "name": "report.pdf"})
    event = adapter.frame_to_event(frame, attachments=[])
    assert '[引用 U456 的文件 "report.pdf": https://cdn.example.com/doc.pdf]' in event.content


def test_frame_to_event_reply_video_with_url():
    frame = _make_media_reply("video", {"url": "https://cdn.example.com/clip.mp4", "aeskey": "K"})
    event = adapter.frame_to_event(frame, attachments=[])
    assert "[引用 U456 的视频: https://cdn.example.com/clip.mp4]" in event.content


def test_frame_to_event_reply_self_quote():
    frame = {
        "cmd": "aibot_msg_callback",
        "headers": {"req_id": "r11"},
        "body": {
            "msgid": "M11",
            "aibotid": "BOTID",
            "chattype": "single",
            "from": {"userid": "U123"},
            "msgtype": "text",
            "text": {"content": "没错"},
            "msgquote": {
                "msgtype": "text",
                "text": {"content": "我是这么说的"},
                "from_userid": "BOTID",
            },
        },
    }
    event = adapter.frame_to_event(frame, attachments=[])
    assert '[引用自己的消息: "我是这么说的"]' in event.content


def test_frame_to_event_reply_mixed_with_images():
    frame = {
        "cmd": "aibot_msg_callback",
        "headers": {"req_id": "r12"},
        "body": {
            "msgid": "M12",
            "aibotid": "AIBOTID",
            "chattype": "single",
            "from": {"userid": "U123"},
            "msgtype": "text",
            "text": {"content": "好的"},
            "msgquote": {
                "msgtype": "mixed",
                "mixed": {
                    "msg_item": [
                        {"msgtype": "text", "text": {"content": "看这两张图"}},
                        {"msgtype": "image", "image": {"url": "https://x/1"}},
                        {"msgtype": "image", "image": {"url": "https://x/2"}},
                    ]
                },
                "from_userid": "U456",
            },
        },
    }
    event = adapter.frame_to_event(frame, attachments=[])
    assert '[引用 U456: "看这两张图"（含 2 张图片）]' in event.content


def test_frame_to_event_reply_long_text_is_truncated():
    long_text = "a" * 200
    frame = {
        "cmd": "aibot_msg_callback",
        "headers": {"req_id": "r9"},
        "body": {
            "msgid": "M9",
            "aibotid": "AIBOTID",
            "chattype": "single",
            "from": {"userid": "U123"},
            "msgtype": "text",
            "text": {"content": "回复"},
            "msgquote": {
                "msgtype": "text",
                "text": {"content": long_text},
            },
        },
    }
    event = adapter.frame_to_event(frame, attachments=[])
    assert "..." in event.content
    quoted = event.content[event.content.index("[引用"):event.content.index("]") + 1]
    assert len(quoted) < len(long_text)


@pytest.mark.asyncio
async def test_collect_attachments_text_returns_empty(tmp_path):
    client = AsyncMock()
    atts = await adapter.collect_attachments(client, _text_single(), tmp_path)
    assert atts == []
    client.download_file.assert_not_called()
