"""Channel base class and shared extension defaults."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Any

from coworker.channels.inbound import InboundEnvelope
from coworker.channels.runtime import DEFAULT_RUNTIME, ChannelRuntime
from coworker.core.types import CommunicateRequest, IncomingEvent, ToolResult

InboundHandler = Callable[[IncomingEvent], Awaitable[Any]]


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


@dataclass(frozen=True)
class ChannelCapabilities:
    """Optional outbound fields accepted by a channel."""

    conversation_id: bool = False
    attachments: bool = False
    extra: bool = False

    def filter(
        self, request: CommunicateRequest
    ) -> tuple[CommunicateRequest, tuple[str, ...]]:
        omitted: list[str] = []
        conversation_id = request.conversation_id
        attachments = request.attachments
        extra = request.extra
        if conversation_id and not self.conversation_id:
            omitted.append("conversation_id")
            conversation_id = None
        if attachments and not self.attachments:
            omitted.append("attachments")
            attachments = []
        if extra and not self.extra:
            omitted.append("extra")
            extra = {}
        if not omitted:
            return request, ()
        return (
            replace(
                request,
                conversation_id=conversation_id,
                attachments=attachments,
                extra=extra,
            ),
            tuple(omitted),
        )


class BaseChannel(ABC):
    """Default implementation for the non-transport parts of a Channel."""

    name = ""
    participant_prefix = ""

    def __init__(
        self,
        *,
        runtime: ChannelRuntime | None = None,
        capabilities: ChannelCapabilities | None = None,
    ) -> None:
        self._runtime = runtime or DEFAULT_RUNTIME
        self._capabilities = capabilities or ChannelCapabilities()
        self._last_sent_at: dict[str, str] = {}
        self._last_received_at: dict[str, str] = {}
        self._inbound_handler: InboundHandler | None = None

    @classmethod
    def from_sender(
        cls,
        prefix: str,
        sender: Callable[[CommunicateRequest], Awaitable[ToolResult]],
        resolver: Callable[[str], str | None] | None = None,
        *,
        capabilities: ChannelCapabilities | None = None,
        name: str | None = None,
        runtime: ChannelRuntime | None = None,
    ) -> BaseChannel:
        """Build a minimal outbound channel from an async sender."""
        return _SenderChannel(
            prefix,
            sender,
            resolver,
            capabilities=capabilities,
            name=name,
            runtime=runtime,
        )

    @property
    def runtime(self) -> ChannelRuntime:
        return self._runtime

    def resolve(self, participant_id: str) -> str | None:
        return None

    def set_inbound_handler(self, handler: InboundHandler | None) -> None:
        self._inbound_handler = handler

    async def publish_inbound(self, event: IncomingEvent) -> None:
        if self._inbound_handler is None:
            raise RuntimeError(f"channel {self.name} has no inbound handler")
        await self._inbound_handler(event)

    async def receive_raw(self, envelope: InboundEnvelope) -> None:
        raise NotImplementedError(f"channel {self.name} does not accept raw inbound payloads")

    @abstractmethod
    async def send(self, request: CommunicateRequest) -> ToolResult:
        """Deliver a request to this channel."""

    def record_received(self, participant_id: str) -> None:
        self._last_received_at[participant_id] = _activity_timestamp()

    def activity_for(self, participant_id: str) -> tuple[str | None, str | None]:
        return self._last_sent_at.get(participant_id), self._last_received_at.get(participant_id)

    def capabilities_for(self, participant_id: str) -> ChannelCapabilities:
        return self._capabilities

    def list_connections(self) -> list[ConnectionInfo]:
        return []

    def _record_sent(self, participant_id: str) -> None:
        self._last_sent_at[participant_id] = _activity_timestamp()


class _SenderChannel(BaseChannel):
    """Private adapter backing :meth:`BaseChannel.from_sender`."""

    def __init__(
        self,
        prefix: str,
        sender: Callable[[CommunicateRequest], Awaitable[ToolResult]],
        resolver: Callable[[str], str | None] | None = None,
        *,
        capabilities: ChannelCapabilities | None = None,
        name: str | None = None,
        runtime: ChannelRuntime | None = None,
    ) -> None:
        super().__init__(runtime=runtime, capabilities=capabilities)
        self.name = name or prefix.rstrip(":") or "inline"
        self.participant_prefix = prefix
        self._sender = sender
        self._resolver = resolver

    def resolve(self, participant_id: str) -> str | None:
        return self._resolver(participant_id) if self._resolver is not None else None

    async def send(self, request: CommunicateRequest) -> ToolResult:
        result = await self._sender(request)
        if not result.is_error:
            self._record_sent(request.participant_id)
        return result

def _activity_timestamp() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")
