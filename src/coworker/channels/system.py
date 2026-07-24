from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from coworker.channels.registry import ChannelRegistry
from coworker.channels.stream import StreamChannel, StreamProfile, StreamRuntime
from coworker.core.registration import RegistrationError


@dataclass(frozen=True)
class ChannelSystem:
    """Application-level channel composition shared by tools and API adapters."""

    registry: ChannelRegistry
    stream_runtime: StreamRuntime
    _stream_channel: StreamChannel = field(repr=False)

    def register_stream_profile(self, profile: StreamProfile) -> None:
        if self.registry.is_running:
            raise RegistrationError(
                "stream profile",
                ["cannot register while the channel system is running"],
            )
        self._stream_channel.register_profile(profile)


def create_channel_system(outbox_dir: str | Path) -> ChannelSystem:
    outbox = Path(outbox_dir)
    stream = StreamRuntime(outbox, outbox.parent / "communicate_registrations.json")
    registry = ChannelRegistry()
    stream_channel = StreamChannel(stream)
    registry.register(stream_channel)
    return ChannelSystem(
        registry=registry,
        stream_runtime=stream,
        _stream_channel=stream_channel,
    )
