from __future__ import annotations

from coworker.agent.incoming_content import format_event_text
from coworker.application import _full_compression_completion_event
from coworker.i18n import locale_context


def test_tool_full_compression_builds_inbox_notice():
    event = {
        "trigger": "tool",
        "mode": "full",
        "messages_compressed": 12,
        "duration_ms": 2500,
    }

    with locale_context("en"):
        notice = _full_compression_completion_event(event)

    assert notice is not None
    assert notice.participant_id == "system"
    assert notice.source == "memory_compression"
    assert "12 short-term messages" in notice.content
    assert "2.5 seconds" in notice.content


def test_automatic_and_admin_compression_do_not_build_notice():
    for trigger, mode in (
        ("automatic", "incremental"),
        ("automatic", "full"),
        ("admin", "full"),
    ):
        assert _full_compression_completion_event({
            "trigger": trigger,
            "mode": mode,
            "messages_compressed": 3,
            "duration_ms": 1000,
        }) is None


def test_notice_renders_without_generic_source_wrapper():
    event = {"trigger": "tool", "mode": "full", "messages_compressed": 2, "duration_ms": 500}

    with locale_context("en"):
        notice = _full_compression_completion_event(event)

    assert notice is not None
    assert format_event_text(notice) == notice.content


def test_notice_localizes_and_tolerates_missing_fields():
    with locale_context("zh-CN"):
        notice = _full_compression_completion_event({"trigger": "tool", "mode": "full"})

    assert notice is not None
    assert "0 条" in notice.content
    assert "0.0 秒" in notice.content
