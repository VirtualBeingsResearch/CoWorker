from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from coworker.channels.activity import ChannelActivityStore
from coworker.channels.module import ChannelModule, ChannelModuleRegistry
from coworker.channels.registry import ChannelRegistry
from coworker.channels.stream import StreamChannel, StreamProfile, StreamRuntime
from coworker.core.registration import RegistrationError


@dataclass(frozen=True)
class ChannelSystem:
    """Application-level channel composition shared by tools and API adapters."""

    registry: ChannelRegistry
    stream_runtime: StreamRuntime
    activity: ChannelActivityStore
    modules: ChannelModuleRegistry
    _stream_channel: StreamChannel = field(repr=False)

    def register_stream_profile(self, profile: StreamProfile) -> None:
        if self.registry.is_running:
            raise RegistrationError(
                "stream profile",
                ["cannot register while the channel system is running"],
            )
        self._stream_channel.register_profile(profile)

    def install(self, module: ChannelModule) -> None:
        if self.registry.is_running:
            raise RegistrationError(
                "channel module",
                ["cannot install while the channel system is running"],
            )
        issues = self.modules.registration_issues(module)
        if issues:
            raise RegistrationError("channel module", issues)
        self.registry.register(module.channel)
        self.modules.register(module)


def create_channel_system(
    outbox_dir: str | Path,
    activity_path: str | Path | None = None,
) -> ChannelSystem:
    outbox = Path(outbox_dir)
    activity = ChannelActivityStore(activity_path)
    stream = StreamRuntime(
        outbox,
        outbox.parent / "communicate_registrations.json",
        activity,
    )
    registry = ChannelRegistry()
    modules = ChannelModuleRegistry()
    stream_channel = StreamChannel(stream)
    registry.register(stream_channel)
    return ChannelSystem(
        registry=registry,
        stream_runtime=stream,
        activity=activity,
        modules=modules,
        _stream_channel=stream_channel,
    )
