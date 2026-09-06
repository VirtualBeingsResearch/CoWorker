from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest

from coworker.channels.access import (
    ChannelAccessDeniedError,
    inbound_access_denied_message,
)
from coworker.channels.stream.runtime import StreamRuntime
from coworker.channels.traffic import ChannelTrafficStore
from coworker.i18n import locale_context


@pytest.mark.asyncio
async def test_reject_inbound_access_notifies_closes_and_records(tmp_path):
    traffic = ChannelTrafficStore()
    stream = StreamRuntime(
        tmp_path / "outbox",
        tmp_path / "registrations.json",
        traffic=traffic,
    )
    websocket = AsyncMock()

    with locale_context("en"):
        expected_message = inbound_access_denied_message()
        await stream.reject_inbound_access(
            websocket,
            ChannelAccessDeniedError("stream", "blocked-client"),
        )

    websocket.send_text.assert_awaited_once_with(expected_message)
    websocket.close.assert_awaited_once_with(
        code=1008,
        reason="Channel access policy denied this inbound message",
    )
    [entry] = traffic.recent(1)
    assert entry["direction"] == "outbound"
    assert entry["channel"] == "stream"
    assert entry["participant_id"] == "blocked-client"
    assert entry["status"] == "sent"
    assert entry["source"] == "access_policy"
    assert entry["reason"] == "rejection_notice"


@pytest.mark.asyncio
async def test_reject_inbound_access_still_closes_when_notice_fails(tmp_path):
    traffic = ChannelTrafficStore()
    stream = StreamRuntime(
        tmp_path / "outbox",
        tmp_path / "registrations.json",
        traffic=traffic,
    )
    websocket = AsyncMock()
    websocket.send_text.side_effect = RuntimeError("socket already closed")

    await stream.reject_inbound_access(
        websocket,
        ChannelAccessDeniedError("stream", "blocked-client"),
    )

    websocket.close.assert_awaited_once()
    [entry] = traffic.recent(1)
    assert entry["status"] == "failed"
    assert entry["reason"] == "rejection_notice"


@pytest.mark.asyncio
async def test_session_metrics_track_accept_reject_and_close(tmp_path):
    from coworker.core.metrics import MetricsRegistry

    registry = MetricsRegistry()
    stream = StreamRuntime(
        tmp_path / "outbox",
        tmp_path / "registrations.json",
        metrics=registry,
    )
    first: asyncio.Queue = asyncio.Queue()
    second: asyncio.Queue = asyncio.Queue()

    assert stream.register_session("desktop:one", first, transport="websocket") is True
    # 同一 participant 的后续连接被拒绝。
    assert stream.register_session("desktop:one", second, transport="websocket") is False

    rendered = registry.render()
    assert 'coworker_sessions_total{state="accepted",transport="websocket"} 1' in rendered
    assert 'coworker_sessions_total{state="rejected",transport="websocket"} 1' in rendered
    assert 'coworker_sessions_active{transport="websocket"} 1' in rendered

    stream.unregister_session("desktop:one", first)
    rendered = registry.render()
    assert 'coworker_sessions_total{state="closed",transport="websocket"} 1' in rendered
    assert 'coworker_sessions_active{transport="websocket"} 0' in rendered


@pytest.mark.asyncio
async def test_session_metrics_ignore_stale_unregister(tmp_path):
    from coworker.core.metrics import MetricsRegistry

    registry = MetricsRegistry()
    stream = StreamRuntime(
        tmp_path / "outbox",
        tmp_path / "registrations.json",
        metrics=registry,
    )
    live: asyncio.Queue = asyncio.Queue()
    stale: asyncio.Queue = asyncio.Queue()

    assert stream.register_session("desktop:one", live, transport="sse") is True
    # 队列不匹配的注销不会减少活跃会话。
    stream.unregister_session("desktop:one", stale)
    assert 'coworker_sessions_active{transport="sse"} 1' in registry.render()

    stream.unregister_session("desktop:one", live)
    assert 'coworker_sessions_active{transport="sse"} 0' in registry.render()
