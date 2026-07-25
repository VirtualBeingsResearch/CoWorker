from __future__ import annotations

from abc import ABC, abstractmethod

from coworker.channels.base import ChannelCapabilities, ConnectionInfo
from coworker.channels.inbound import InboundEnvelope
from coworker.channels.stream.runtime import StreamRuntime
from coworker.core.types import CommunicateRequest, IncomingEvent, ToolResult


class StreamProfile(ABC):
    """Protocol behavior layered on the shared stream transport."""

    name: str
    participant_prefix: str

    @abstractmethod
    def capabilities_for(self, participant_id: str) -> ChannelCapabilities: ...

    @abstractmethod
    async def send(
        self,
        request: CommunicateRequest,
        runtime: StreamRuntime,
    ) -> ToolResult: ...

    @abstractmethod
    def normalize_inbound(
        self,
        envelope: InboundEnvelope,
        runtime: StreamRuntime,
    ) -> IncomingEvent | None: ...

    @abstractmethod
    def list_connections(self, runtime: StreamRuntime) -> list[ConnectionInfo]: ...
