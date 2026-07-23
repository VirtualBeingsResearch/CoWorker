"""Channel abstraction: unified inbound/outbound/lifecycle/state contract.

A :class:`Channel` is one communication transport -- the generic WS/SSE
stream, WeCom, or Desktop. :class:`ChannelHost` owns the participant_id
routing that previously lived in :class:`~coworker.tools.communicate_tool.CommunicateTool`:

* longest non-empty prefix match,
* checker auto-resolution (``Channel.resolve``) with multi-match ambiguity,
* fallback to the empty-prefix stream channel (live WS/SSE queue or outbox).

Channels are registered once and started/stopped together. The host also
aggregates :meth:`ConnectionInfo` across channels for the
``list_connections`` tool and exposes live WS/SSE participant IDs for
internal stream-lifecycle consumers.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from coworker.core.types import CommunicateRequest, ToolResult
from coworker.i18n import tr

if TYPE_CHECKING:
    from coworker.core.types import IncomingEvent


@dataclass(frozen=True)
class ConnectionInfo:
    """A reachable communication participant on some channel."""

    participant_id: str
    channel: str  # "stream" / "wecom" / "desktop"
    kind: str  # "websocket" / "sse" / "wecom:single" / "wecom:group" / "desktop:actor"
    display_name: str = ""
    active: bool = False  # online now (stream WS/SSE) vs known-reachable (wecom/desktop)
    last_sent_at: str | None = None
    last_received_at: str | None = None


class ParticipantIdResolutionError(ValueError):
    """Raised when a shorthand participant ID cannot be resolved unambiguously."""


class InlineChannel:
    """Minimal :class:`Channel` wrapping a sender callable.

    The simplest way to register a channel: a prefix, an async sender, an
    optional checker, and a static ``supports_extra`` flag. Real channels
    (desktop, wecom, stream) provide richer ``list_connections`` / lifecycle,
    but anything that just routes by prefix and sends can use this.
    """

    def __init__(
        self,
        prefix: str,
        sender: Callable[[CommunicateRequest], Awaitable[ToolResult]],
        checker: Callable[[str], str | None] | None = None,
        *,
        supports_extra: bool = False,
        name: str | None = None,
    ) -> None:
        self.name = name or prefix.rstrip(":") or "inline"
        self.participant_prefix = prefix
        self._sender = sender
        self._checker = checker
        self._supports_extra = supports_extra
        self._last_sent_at: dict[str, str] = {}
        self._last_received_at: dict[str, str] = {}

    def resolve(self, participant_id: str) -> str | None:
        return self._checker(participant_id) if self._checker is not None else None

    async def send(self, request: CommunicateRequest) -> ToolResult:
        result = await self._sender(request)
        if not result.is_error:
            self._last_sent_at[request.participant_id] = _activity_timestamp()
        return result

    def record_received(self, participant_id: str) -> None:
        self._last_received_at[participant_id] = _activity_timestamp()

    def activity_for(self, participant_id: str) -> tuple[str | None, str | None]:
        return self._last_sent_at.get(participant_id), self._last_received_at.get(participant_id)

    def supports_extra_for(self, participant_id: str) -> bool:
        return self._supports_extra

    def list_connections(self) -> list[ConnectionInfo]:
        return []

    def list_live_stream_participant_ids(self) -> list[str]:
        return []

    async def start(self) -> None:
        pass

    async def stop(self) -> None:
        pass


@runtime_checkable
class Channel(Protocol):
    """One communication transport.

    ``participant_prefix`` routes by participant_id prefix; the empty-prefix
    channel is the fallback stream channel that handles ids no other channel
    claims. ``resolve`` canonicalizes a shorthand id (the old ``checker``);
    returning ``None`` means this channel does not claim it.
    """

    name: str
    participant_prefix: str

    def resolve(self, participant_id: str) -> str | None:
        """Canonicalize a shorthand participant_id, or None if not claimed."""
        ...

    async def send(self, request: CommunicateRequest) -> ToolResult:
        """Deliver a request to this channel's participant."""
        ...

    def supports_extra_for(self, participant_id: str) -> bool:
        """Whether this channel accepts structured ``extra`` for the participant.

        Static for prefix-routed channels (desktop yes, wecom no); per-participant
        for the stream fallback (only live WS/SSE connections accept extra).
        """
        ...

    def list_connections(self) -> list[ConnectionInfo]:
        """Reachable participants on this channel."""
        ...

    def record_received(self, participant_id: str) -> None:
        """Record an inbound message for a participant."""
        ...

    def list_live_stream_participant_ids(self) -> list[str]:
        """Live participant_ids (WS/SSE stream connections); non-stream channels return []."""
        ...

    async def start(self) -> None:
        """Start background transport (no-op for channels without one)."""
        ...

    async def stop(self) -> None:
        """Stop background transport (no-op for channels without one)."""
        ...


class ChannelHost:
    """Owns channel registration, participant_id routing, and lifecycle.

    The empty-prefix channel (the stream) is the fallback: it receives any
    request no other channel claims by prefix or checker, and owns the live
    WS/SSE queue plus the outbox-file fallback.
    """

    def __init__(self) -> None:
        self._channels: list[Channel] = []
        self._fallback: Channel | None = None
        self._interceptors: list[Callable[[IncomingEvent], bool]] = []

    @property
    def channels(self) -> list[Channel]:
        return list(self._channels)

    def register(self, channel: Channel) -> None:
        self._channels.append(channel)
        if channel.participant_prefix == "":
            self._fallback = channel

    # ------------------------------------------------------------------ routing

    def resolve_participant_id(self, participant_id: str) -> str:
        """Expand a shorthand participant_id without sending (raises on ambiguity)."""
        canonical, _ = self._resolve(participant_id)
        return canonical

    def supports_message_extra(self, participant_id: str) -> bool:
        """Whether the participant's selected transport accepts structured ``extra``."""
        canonical, channel = self._resolve(participant_id)
        if channel is not None:
            return channel.supports_extra_for(canonical)
        if self._fallback is not None:
            return self._fallback.supports_extra_for(canonical)
        return False

    async def send(self, request: CommunicateRequest) -> ToolResult:
        """Route a request to the matching channel (or stream fallback)."""
        canonical, channel = self._resolve(request.participant_id)
        target = channel if channel is not None else self._fallback
        if target is None:
            return ToolResult(
                tool_call_id="",
                content=tr("tool_result.communicate.failed", error="no channel registered"),
                is_error=True,
            )
        from dataclasses import replace

        return await target.send(replace(request, participant_id=canonical))

    def _resolve(self, participant_id: str) -> tuple[str, Channel | None]:
        # 1. Longest non-empty prefix match wins.
        matched: Channel | None = None
        for channel in self._channels:
            prefix = channel.participant_prefix
            if prefix and participant_id.startswith(prefix):
                if matched is None or len(prefix) > len(matched.participant_prefix):
                    matched = channel
        if matched is not None:
            return participant_id, matched

        # 2. Checker resolution across all channels.
        resolved: dict[Channel, str] = {}
        for channel in self._channels:
            canonical = channel.resolve(participant_id)
            if canonical is not None:
                resolved[channel] = canonical
        if len(resolved) == 1:
            channel, canonical = next(iter(resolved.items()))
            return canonical, channel
        if len(resolved) > 1:
            options = "\n".join(
                tr(
                    "tool_result.communicate.option",
                    id=cid,
                    prefix=channel.participant_prefix or channel.name,
                )
                for channel, cid in resolved.items()
            )
            raise ParticipantIdResolutionError(
                tr(
                    "tool_result.communicate.ambiguous",
                    participant=participant_id,
                    options=options,
                )
            )
        return participant_id, None

    # ------------------------------------------------ connections / live streams

    def list_connections(self) -> list[ConnectionInfo]:
        """Aggregate reachable participants across all channels."""
        out: list[ConnectionInfo] = []
        for channel in self._channels:
            out.extend(channel.list_connections())
        return out

    def record_received(self, participant_id: str) -> None:
        """Record an inbound message on the channel selected for a participant."""
        _, channel = self._resolve(participant_id)
        target = channel if channel is not None else self._fallback
        if target is not None:
            target.record_received(participant_id)

    def list_live_stream_participant_ids(self) -> list[str]:
        """Return participant IDs with a currently live WS/SSE reply stream."""
        out: list[str] = []
        for channel in self._channels:
            out.extend(channel.list_live_stream_participant_ids())
        return out

    # ----------------------------------------------------------------- lifecycle

    async def start_all(self) -> None:
        for channel in self._channels:
            await channel.start()

    async def stop_all(self) -> None:
        for channel in self._channels:
            await channel.stop()

    # -------------------------------------------------------- inbox interceptors

    def add_inbox_interceptor(self, interceptor: Callable[[IncomingEvent], bool]) -> None:
        """Register an InboxWatcher interceptor (ordering is explicit, not call-order)."""
        self._interceptors.append(interceptor)

    @property
    def inbox_interceptors(self) -> list[Callable[[IncomingEvent], bool]]:
        return list(self._interceptors)


def _activity_timestamp() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")
