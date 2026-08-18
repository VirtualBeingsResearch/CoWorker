from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal, Protocol, runtime_checkable

UsageListener = Callable[[dict[str, Any]], None]


@dataclass(frozen=True, slots=True)
class MemoryRecord:
    """A single long-term memory entry as seen by Coworker.

    ``timestamp`` is the original source timestamp when available; it is stored as
    an ISO-8601 string to keep the backend contract JSON-friendly.
    """

    id: str
    content: str
    category: str = "general"
    tags: list[str] = field(default_factory=list)
    timestamp: str | None = None


@dataclass(frozen=True, slots=True)
class MemoryWriteResult:
    status: Literal["written", "empty"]
    memory_id: str = ""


@dataclass(frozen=True, slots=True)
class MemoryQuery:
    text: str
    category: str | None = None
    tags: list[str] | None = None
    limit: int = 10
    start: datetime | None = None
    end: datetime | None = None


class MemoryBackendConfig(Protocol):
    """Structural protocol for backend reconfiguration payloads.

    ``LongTermLLMConfig`` satisfies this protocol; backend implementations should
    not depend on mem0-specific config types.
    """

    provider: str
    api_dialect: str
    api_key: str
    model: str
    base_url: str
    thinking: bool


@runtime_checkable
class LongTermMemoryBackend(Protocol):
    """Backend-agnostic long-term memory service contract."""

    async def initialize(self) -> None: ...

    def is_ready(self) -> bool: ...

    async def write(
        self,
        content: str,
        *,
        category: str,
        tags: list[str],
        source_timestamp: datetime | None = None,
    ) -> MemoryWriteResult: ...

    async def query(self, params: MemoryQuery) -> list[MemoryRecord]: ...

    async def update(
        self,
        memory_id: str,
        content: str,
        *,
        tags: list[str] | None = None,
    ) -> None: ...

    async def delete(self, memory_id: str) -> None: ...

    async def associate_tags(self, memory_id: str, tags: list[str]) -> list[str]: ...

    async def reconfigure(self, config: MemoryBackendConfig) -> None: ...

    def add_usage_listener(self, listener: UsageListener) -> None: ...

    async def count(self) -> int: ...
