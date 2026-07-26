"""Public channel development API."""

from coworker.channels.activity import ChannelActivityStore
from coworker.channels.base import BaseChannel, ChannelCapabilities
from coworker.channels.module import (
    ChannelManagement,
    ChannelModule,
    ChannelModuleRegistry,
    ChannelSettings,
)
from coworker.channels.registry import ChannelRegistry
from coworker.channels.runtime import ChannelRuntime, InlineRuntime
from coworker.channels.stream import StreamProfile
from coworker.channels.system import ChannelSystem, create_channel_system
from coworker.core.registration import RegistrationError

__all__ = [
    "BaseChannel",
    "ChannelActivityStore",
    "ChannelCapabilities",
    "ChannelManagement",
    "ChannelModule",
    "ChannelModuleRegistry",
    "ChannelRegistry",
    "ChannelRuntime",
    "ChannelSettings",
    "ChannelSystem",
    "InlineRuntime",
    "RegistrationError",
    "StreamProfile",
    "create_channel_system",
]
