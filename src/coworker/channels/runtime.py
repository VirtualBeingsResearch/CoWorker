from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class ChannelRuntime(Protocol):
    """Stateful execution environment shared by one or more channel profiles."""

    name: str

    async def start(self) -> None:
        """Run the transport until it stops."""
        ...

    async def stop(self) -> None:
        """Request transport shutdown."""
        ...


class InlineRuntime:
    """No-op runtime for channels backed by an injected sender."""

    name = "inline"

    async def start(self) -> None:
        pass

    async def stop(self) -> None:
        pass


DEFAULT_RUNTIME = InlineRuntime()
