"""Unit tests for ChannelHost routing (Phase 1 abstraction, not yet wired)."""

from __future__ import annotations

import pytest

from coworker.channels.base import (
    ChannelHost,
    ConnectionInfo,
    ParticipantIdResolutionError,
)
from coworker.core.types import CommunicateRequest, ToolResult


class _FakeChannel:
    """Minimal Channel implementation for routing tests."""

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
        self._supports_extra = supports_extra
        self._resolver = resolver or (lambda pid: None)
        self._live = set(live)
        self.sent: list[CommunicateRequest] = []
        self.started = False
        self.stopped = False

    def resolve(self, participant_id: str) -> str | None:
        return self._resolver(participant_id)

    async def send(self, request: CommunicateRequest) -> ToolResult:
        self.sent.append(request)
        return ToolResult(tool_call_id="", content=f"sent:{self.name}")

    def supports_extra_for(self, participant_id: str) -> bool:
        if self.participant_prefix == "":
            return participant_id in self._live  # stream: live connection
        return self._supports_extra

    def list_connections(self) -> list[ConnectionInfo]:
        return [
            ConnectionInfo(
                participant_id=p,
                channel=self.name,
                kind="fake",
                active=p in self._live,
            )
            for p in self._live
        ]

    def list_connected(self) -> list[str]:
        return list(self._live) if self.participant_prefix == "" else []

    async def start(self) -> None:
        self.started = True

    async def stop(self) -> None:
        self.stopped = True


@pytest.fixture()
def host() -> ChannelHost:
    return ChannelHost()


# ------------------------------------------------------------------ prefix match


@pytest.mark.asyncio
async def test_longest_prefix_wins(host: ChannelHost) -> None:
    generic = _FakeChannel("generic", "rich:")
    specific = _FakeChannel("specific", "rich:team:")
    host.register(generic)
    host.register(specific)

    result = await host.send(CommunicateRequest(participant_id="rich:team:alice", message="hi"))

    assert not result.is_error
    assert specific.sent and not generic.sent
    assert specific.sent[0].participant_id == "rich:team:alice"


@pytest.mark.asyncio
async def test_prefix_match_bypasses_checker(host: ChannelHost) -> None:
    # Resolver only claims bare ids; a prefixed id must route by prefix untouched.
    chan = _FakeChannel(
        "chan",
        "chan:",
        resolver=lambda pid: f"chan:{pid}" if not pid.startswith("chan:") else None,
    )
    host.register(chan)

    await host.send(CommunicateRequest(participant_id="chan:alice", message="hi"))

    assert chan.sent[0].participant_id == "chan:alice"


# ------------------------------------------------------------------- checker path


@pytest.mark.asyncio
async def test_no_prefix_single_match_auto_routes(host: ChannelHost) -> None:
    chan = _FakeChannel(
        "chan",
        "chan:",
        resolver=lambda pid: f"chan:single:{pid}" if pid == "alice" else None,
    )
    host.register(chan)

    await host.send(CommunicateRequest(participant_id="alice", message="hi"))

    assert chan.sent[0].participant_id == "chan:single:alice"


def test_resolve_participant_id_expands_and_passes_through(host: ChannelHost) -> None:
    chan = _FakeChannel(
        "chan",
        "chan:",
        resolver=lambda pid: f"chan:single:{pid}" if pid == "alice" else None,
    )
    host.register(chan)

    assert host.resolve_participant_id("alice") == "chan:single:alice"
    assert host.resolve_participant_id("chan:single:alice") == "chan:single:alice"
    assert host.resolve_participant_id("unknown") == "unknown"


@pytest.mark.asyncio
async def test_no_prefix_multi_match_raises(host: ChannelHost) -> None:
    chan_a = _FakeChannel(
        "chan_a", "chan_a:", resolver=lambda pid: f"chan_a:{pid}" if pid == "alice" else None
    )
    chan_b = _FakeChannel(
        "chan_b", "chan_b:", resolver=lambda pid: f"chan_b:{pid}" if pid == "alice" else None
    )
    host.register(chan_a)
    host.register(chan_b)

    with pytest.raises(ParticipantIdResolutionError) as exc_info:
        await host.send(CommunicateRequest(participant_id="alice", message="hi"))

    message = str(exc_info.value)
    assert "多个信道" in message
    assert "chan_a:alice" in message
    assert "chan_b:alice" in message


# ------------------------------------------------------------- stream fallback


@pytest.mark.asyncio
async def test_no_prefix_no_match_falls_back_to_stream(host: ChannelHost) -> None:
    stream = _FakeChannel("stream", "")
    host.register(stream)

    await host.send(CommunicateRequest(participant_id="unknown_user", message="hello"))

    assert stream.sent and stream.sent[0].participant_id == "unknown_user"


# --------------------------------------------------------- supports_message_extra


def test_supports_extra_follows_selected_transport(host: ChannelHost) -> None:
    plain = _FakeChannel("plain", "plain:", supports_extra=False)
    rich = _FakeChannel("rich", "rich:", supports_extra=True)
    stream = _FakeChannel("stream", "", live=("stream-client",))
    host.register(plain)
    host.register(rich)
    host.register(stream)

    assert not host.supports_message_extra("plain:alice")
    assert host.supports_message_extra("rich:alice")
    assert host.supports_message_extra("stream-client")  # live stream connection
    assert not host.supports_message_extra("offline-client")


# ------------------------------------------------------------ list_connected / status


def test_list_connected_is_stream_only(host: ChannelHost) -> None:
    stream = _FakeChannel("stream", "", live=("a", "b"))
    desktop = _FakeChannel("desktop", "coworker-desktop:", live=("d1",))  # non-stream
    host.register(stream)
    host.register(desktop)

    assert sorted(host.list_connected()) == ["a", "b"]


def test_list_connections_aggregates_across_channels(host: ChannelHost) -> None:
    stream = _FakeChannel("stream", "", live=("a",))
    wecom = _FakeChannel("wecom", "wecom:")
    host.register(stream)
    host.register(wecom)

    infos = host.list_connections()
    assert [info.participant_id for info in infos] == ["a"]
    assert infos[0].channel == "stream"


# --------------------------------------------------------------------- lifecycle


@pytest.mark.asyncio
async def test_start_all_and_stop_all(host: ChannelHost) -> None:
    stream = _FakeChannel("stream", "")
    wecom = _FakeChannel("wecom", "wecom:")
    host.register(stream)
    host.register(wecom)

    await host.start_all()
    assert stream.started and wecom.started

    await host.stop_all()
    assert stream.stopped and wecom.stopped
