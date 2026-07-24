"""Unit tests for channel routing and shared runtime orchestration."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from coworker.channels.base import ConnectionInfo, ParticipantIdResolutionError
from coworker.channels.registry import ChannelRegistry
from coworker.core.types import CommunicateRequest, IncomingEvent, ToolResult


class _FakeChannel:
    def __init__(
        self,
        name: str,
        prefix: str,
        *,
        supports_extra: bool = False,
        resolver=None,
        live: tuple[str, ...] = (),
    ) -> None:
        self.name = name
        self.participant_prefix = prefix
        self.runtime = self
        self._supports_extra = supports_extra
        self._resolver = resolver or (lambda participant_id: None)
        self._live = set(live)
        self.sent: list[CommunicateRequest] = []
        self.started = False
        self.stopped = False

    def set_inbound_handler(self, handler) -> None:
        self.inbound_handler = handler

    def resolve(self, participant_id: str) -> str | None:
        return self._resolver(participant_id)

    async def send(self, request: CommunicateRequest) -> ToolResult:
        self.sent.append(request)
        return ToolResult(tool_call_id="", content=f"sent:{self.name}")

    def supports_extra_for(self, participant_id: str) -> bool:
        if self.participant_prefix == "":
            return participant_id in self._live
        return self._supports_extra

    def list_connections(self) -> list[ConnectionInfo]:
        return [
            ConnectionInfo(
                participant_id=participant_id,
                channel=self.name,
                kind="fake",
                active=participant_id in self._live,
            )
            for participant_id in self._live
        ]

    async def start(self) -> None:
        self.started = True

    async def stop(self) -> None:
        self.stopped = True


@pytest.fixture()
def registry() -> ChannelRegistry:
    return ChannelRegistry()


@pytest.mark.asyncio
async def test_longest_prefix_wins(registry: ChannelRegistry) -> None:
    generic = _FakeChannel("generic", "rich:")
    specific = _FakeChannel("specific", "rich:team:")
    registry.register(generic)
    registry.register(specific)

    result = await registry.send(
        CommunicateRequest(participant_id="rich:team:alice", message="hi")
    )

    assert not result.is_error
    assert specific.sent and not generic.sent
    assert specific.sent[0].participant_id == "rich:team:alice"


@pytest.mark.asyncio
async def test_prefix_match_bypasses_checker(registry: ChannelRegistry) -> None:
    channel = _FakeChannel(
        "channel",
        "channel:",
        resolver=lambda participant_id: (
            f"channel:{participant_id}" if not participant_id.startswith("channel:") else None
        ),
    )
    registry.register(channel)

    await registry.send(CommunicateRequest(participant_id="channel:alice", message="hi"))

    assert channel.sent[0].participant_id == "channel:alice"


@pytest.mark.asyncio
async def test_no_prefix_single_match_auto_routes(registry: ChannelRegistry) -> None:
    channel = _FakeChannel(
        "channel",
        "channel:",
        resolver=lambda participant_id: (
            f"channel:single:{participant_id}" if participant_id == "alice" else None
        ),
    )
    registry.register(channel)

    await registry.send(CommunicateRequest(participant_id="alice", message="hi"))

    assert channel.sent[0].participant_id == "channel:single:alice"


def test_resolve_participant_id_expands_and_passes_through(
    registry: ChannelRegistry,
) -> None:
    channel = _FakeChannel(
        "channel",
        "channel:",
        resolver=lambda participant_id: (
            f"channel:single:{participant_id}" if participant_id == "alice" else None
        ),
    )
    registry.register(channel)

    assert registry.resolve_participant_id("alice") == "channel:single:alice"
    assert registry.resolve_participant_id("channel:single:alice") == "channel:single:alice"
    assert registry.resolve_participant_id("unknown") == "unknown"


@pytest.mark.asyncio
async def test_no_prefix_multi_match_raises(registry: ChannelRegistry) -> None:
    channel_a = _FakeChannel(
        "channel_a",
        "channel_a:",
        resolver=lambda participant_id: (
            f"channel_a:{participant_id}" if participant_id == "alice" else None
        ),
    )
    channel_b = _FakeChannel(
        "channel_b",
        "channel_b:",
        resolver=lambda participant_id: (
            f"channel_b:{participant_id}" if participant_id == "alice" else None
        ),
    )
    registry.register(channel_a)
    registry.register(channel_b)

    with pytest.raises(ParticipantIdResolutionError) as error:
        await registry.send(CommunicateRequest(participant_id="alice", message="hi"))

    message = str(error.value)
    assert "多个信道" in message
    assert "channel_a:alice" in message
    assert "channel_b:alice" in message


@pytest.mark.asyncio
async def test_no_prefix_no_match_falls_back_to_stream(registry: ChannelRegistry) -> None:
    stream = _FakeChannel("stream", "")
    registry.register(stream)

    await registry.send(CommunicateRequest(participant_id="unknown_user", message="hello"))

    assert stream.sent and stream.sent[0].participant_id == "unknown_user"


def test_supports_extra_follows_selected_transport(registry: ChannelRegistry) -> None:
    plain = _FakeChannel("plain", "plain:")
    rich = _FakeChannel("rich", "rich:", supports_extra=True)
    stream = _FakeChannel("stream", "", live=("stream-client",))
    registry.register(plain)
    registry.register(rich)
    registry.register(stream)

    assert not registry.supports_message_extra("plain:alice")
    assert registry.supports_message_extra("rich:alice")
    assert registry.supports_message_extra("stream-client")
    assert not registry.supports_message_extra("offline-client")


def test_list_connections_aggregates_across_channels(registry: ChannelRegistry) -> None:
    stream = _FakeChannel("stream", "", live=("a",))
    wecom = _FakeChannel("wecom", "wecom:")
    registry.register(stream)
    registry.register(wecom)

    connections = registry.list_connections()

    assert [connection.participant_id for connection in connections] == ["a"]
    assert connections[0].channel == "stream"


@pytest.mark.asyncio
async def test_inbound_events_are_delivered_by_the_registry() -> None:
    registry = ChannelRegistry()
    handler = AsyncMock()
    registry.set_inbound_handler(handler)

    event = IncomingEvent(participant_id="alice", content="hello")
    await registry.publish_inbound(event)

    handler.assert_awaited_once_with(event)


@pytest.mark.asyncio
async def test_shared_runtime_starts_and_stops_once(registry: ChannelRegistry) -> None:
    stream = _FakeChannel("stream", "")
    desktop = _FakeChannel("desktop", "coworker-desktop:")
    desktop.runtime = stream.runtime
    registry.register(stream)
    registry.register(desktop)

    await registry.start()
    await registry.stop()

    assert stream.started
    assert stream.stopped
    assert not desktop.started
    assert not desktop.stopped


def test_duplicate_fallback_is_rejected(registry: ChannelRegistry) -> None:
    registry.register(_FakeChannel("stream", ""))

    with pytest.raises(ValueError, match="fallback channel already registered"):
        registry.register(_FakeChannel("other", ""))
