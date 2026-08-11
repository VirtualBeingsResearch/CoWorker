"""Channel module wiring for the environment sensing channel.

``ChannelModule`` is the registration unit that ``ChannelSystem.install``
expects: it bundles the channel, an optional management surface, and an
optional settings surface.  For the environment channel, management and
settings are lightweight stubs — real configuration happens through
source scripts and the ``manage_environment`` tool.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from coworker.channels.base import BaseChannel


@dataclass(frozen=True)
class EnvironmentModule:
    """Minimal ChannelModule implementation for the environment channel."""

    _name: str
    _channel: BaseChannel

    @property
    def name(self) -> str:
        return self._name

    @property
    def channel(self) -> BaseChannel:
        return self._channel

    @property
    def management(self) -> Any:
        return None

    @property
    def settings(self) -> Any:
        return None


def create_environment_module(channel: BaseChannel) -> EnvironmentModule:
    """Bundle the environment channel into a ChannelModule for install()."""
    return EnvironmentModule(_name=channel.name, _channel=channel)
