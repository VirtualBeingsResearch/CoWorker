from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from coworker.channels.registry import ChannelRegistry
from coworker.channels.stream import StreamChannel, StreamRuntime


@dataclass(frozen=True)
class ChannelSystem:
    """Application-level channel composition shared by tools and API adapters."""

    registry: ChannelRegistry
    stream_runtime: StreamRuntime


def create_channel_system(outbox_dir: str | Path) -> ChannelSystem:
    outbox = Path(outbox_dir)
    stream = StreamRuntime(outbox, outbox.parent / "communicate_registrations.json")
    registry = ChannelRegistry()
    registry.register(StreamChannel(stream))
    return ChannelSystem(registry=registry, stream_runtime=stream)
