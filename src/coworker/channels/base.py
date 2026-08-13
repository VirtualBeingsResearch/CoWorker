"""Channel base class and shared extension defaults."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from typing import Any

from loguru import logger

from coworker.channels.access import ChannelAccessController
from coworker.channels.activity import ChannelActivityStore
from coworker.channels.inbound import InboundEnvelope
from coworker.channels.runtime import DEFAULT_RUNTIME, ChannelRuntime
from coworker.core.types import CommunicateRequest, IncomingEvent, ToolResult
from coworker.i18n import tr

InboundHandler = Callable[[IncomingEvent], Awaitable[Any]]


@dataclass(frozen=True)
class ConnectionInfo:
    """A reachable communication participant on some channel."""

    participant_id: str
    channel: str  # "stream" / "wecom" / "weixin" / "telegram" / "desktop"
    kind: str  # transport-specific connection kind
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
    requires_known_participant = False

    def __init__(
        self,
        *,
        runtime: ChannelRuntime | None = None,
        capabilities: ChannelCapabilities | None = None,
        activity: ChannelActivityStore | None = None,
    ) -> None:
        self._runtime = runtime or DEFAULT_RUNTIME
        self._capabilities = capabilities or ChannelCapabilities()
        self._activity = activity or ChannelActivityStore()
        self._inbound_handler: InboundHandler | None = None
        self._access = ChannelAccessController()

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
        activity: ChannelActivityStore | None = None,
    ) -> BaseChannel:
        """Build a minimal outbound channel from an async sender."""
        return _SenderChannel(
            prefix,
            sender,
            resolver,
            capabilities=capabilities,
            name=name,
            runtime=runtime,
            activity=activity,
        )

    @property
    def runtime(self) -> ChannelRuntime:
        return self._runtime

    def resolve(self, participant_id: str) -> str | None:
        return None

    def supports_extra(
        self,
        participant_id: str,
        extra: dict[str, Any] | None = None,
    ) -> bool:
        return self.capabilities_for(participant_id).extra

    def set_inbound_handler(self, handler: InboundHandler | None) -> None:
        self._inbound_handler = handler

    def set_access_controller(self, access: ChannelAccessController) -> None:
        self._access = access

    def access_channel_for(self, participant_id: str) -> str:
        """Return the policy namespace for a participant handled by this Channel."""
        return self.name

    async def publish_inbound(self, event: IncomingEvent) -> None:
        if self._inbound_handler is None:
            raise RuntimeError(f"channel {self.name} has no inbound handler")
        access_channel = self.access_channel_for(event.participant_id)
        if not self._access.allows(access_channel, "inbound", event.participant_id):
            self._access.traffic.record(
                direction="inbound",
                channel=access_channel,
                participant_id=event.participant_id,
                status="denied",
                source=event.source,
                reason="policy",
            )
            logger.info(
                tr(
                    "channel.access.inbound_denied",
                    channel=access_channel,
                    participant=event.participant_id,
                )
            )
            return
        try:
            await self._inbound_handler(event)
        except Exception as error:
            self._access.traffic.record(
                direction="inbound",
                channel=access_channel,
                participant_id=event.participant_id,
                status="failed",
                source=event.source,
                reason=type(error).__name__,
            )
            raise
        self._access.traffic.record(
            direction="inbound",
            channel=access_channel,
            participant_id=event.participant_id,
            status="received",
            source=event.source,
        )

    async def receive_raw(self, envelope: InboundEnvelope) -> None:
        raise NotImplementedError(f"channel {self.name} does not accept raw inbound payloads")

    @abstractmethod
    async def send(self, request: CommunicateRequest) -> ToolResult:
        """Deliver a request to this channel."""

    def agent_instructions(self) -> str:
        """Describe stable model-facing behavior exposed by this channel."""

        return ""

    def record_received(self, participant_id: str) -> None:
        self._activity.record_received(participant_id)

    def activity_for(self, participant_id: str) -> tuple[str | None, str | None]:
        return self._activity.activity_for(participant_id)

    def capabilities_for(self, participant_id: str) -> ChannelCapabilities:
        return self._capabilities

    def list_connections(self) -> list[ConnectionInfo]:
        return []

    def known_participant_ids(self) -> set[str]:
        return {
            connection.participant_id
            for connection in self.list_connections()
            if connection.participant_id
        }

    def _record_sent(self, participant_id: str) -> None:
        self._activity.record_sent(participant_id)


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
        activity: ChannelActivityStore | None = None,
    ) -> None:
        super().__init__(
            runtime=runtime,
            capabilities=capabilities,
            activity=activity,
        )
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
