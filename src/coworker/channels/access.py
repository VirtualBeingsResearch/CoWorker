"""Participant allow/deny policy shared by all communication Channels."""

from __future__ import annotations

from fnmatch import fnmatchcase
from typing import Literal

from coworker.core.config import ChannelAccessConfig
from coworker.i18n import tr

AccessDirection = Literal["inbound", "outbound"]


class ChannelAccessDeniedError(PermissionError):
    """Raised when a raw inbound message is rejected by Channel policy."""

    def __init__(self, channel: str, participant_id: str) -> None:
        self.channel = channel
        self.participant_id = participant_id
        super().__init__(channel, participant_id)

    def __str__(self) -> str:
        return tr(
            "api.message.channel_access_denied",
            channel=self.channel,
            participant=self.participant_id,
        )


class ChannelAccessController:
    """Evaluate a live :class:`ChannelAccessConfig` without transport coupling."""

    def __init__(self, config: ChannelAccessConfig | None = None) -> None:
        # The config object is deliberately shared with the running Config. Admin
        # hot-apply replaces ``root`` atomically so registered Channels see the
        # next policy without being restarted or re-registered.
        self._config = config if config is not None else ChannelAccessConfig()

    @property
    def config(self) -> ChannelAccessConfig:
        return self._config

    def allows(
        self,
        channel: str,
        direction: AccessDirection,
        participant_id: str,
    ) -> bool:
        rules = self._config.root.get(channel)
        if rules is None:
            return True
        allow = getattr(rules, f"{direction}_allow")
        deny = getattr(rules, f"{direction}_deny")
        if self._matches(participant_id, deny):
            return False
        return not allow or self._matches(participant_id, allow)

    @staticmethod
    def _matches(participant_id: str, patterns: list[str]) -> bool:
        return any(fnmatchcase(participant_id, pattern) for pattern in patterns)
