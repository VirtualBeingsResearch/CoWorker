from __future__ import annotations

import asyncio

import pytest

from coworker.channels.base import ConnectionInfo
from coworker.channels.stream.runtime import StreamRuntime
from coworker.core.types import CommunicateRequest
from coworker.i18n import locale_context
from coworker.tools.communicate_tool import ListConnectionTool


class _Communicate:
    def list_connections(self) -> list[ConnectionInfo]:
        return [
            ConnectionInfo(
                participant_id="wecom:single:U123",
                channel="wecom",
                kind="wecom:single",
                active=True,
                last_sent_at="2026-07-23T10:20:30+08:00",
                last_received_at="2026-07-23T10:19:00+08:00",
            )
        ]


@pytest.mark.asyncio
async def test_list_connections_shows_activity_times_without_status_label():
    result = await ListConnectionTool(_Communicate()).execute()

    assert result.is_error is False
    assert "最近发送：2026-07-23T10:20:30+08:00" in result.content
    assert "最近接收：2026-07-23T10:19:00+08:00" in result.content
    assert "[wecom:single, active]" not in result.content


@pytest.mark.asyncio
async def test_list_connections_uses_english_catalog():
    with locale_context("en"):
        result = await ListConnectionTool(_Communicate()).execute()

    assert "last sent: 2026-07-23T10:20:30+08:00" in result.content
    assert "last received: 2026-07-23T10:19:00+08:00" in result.content
    assert "最近发送" not in result.content


@pytest.mark.asyncio
async def test_stream_connection_records_send_and_receive_times(tmp_path):
    stream = StreamRuntime(tmp_path / "outbox", tmp_path / "registrations.json")
    queue: asyncio.Queue = asyncio.Queue()
    assert stream.register_ws("alice", queue)

    result = await stream.send(CommunicateRequest(participant_id="alice", message="hello"))
    stream.record_received("alice")

    assert result.is_error is False
    info = stream.list_connections()[0]
    assert info.last_sent_at is not None
    assert info.last_received_at is not None
