from __future__ import annotations

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
