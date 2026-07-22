"""DesktopChannel: the CoWorker Desktop transport as a Channel.

Wraps :class:`DesktopCommunicateSender` (outbound) and
:class:`DesktopRegistry` (actor state for ``list_connections``). Inbound
desktop envelopes are still normalized by :class:`DesktopDispatcher`; this
channel only owns outbound routing + connection visibility.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from coworker.channels.base import ConnectionInfo, InlineChannel
from coworker.channels.desktop.communicate_sender import DESKTOP_PREFIX

if TYPE_CHECKING:
    from coworker.channels.desktop.communicate_sender import DesktopCommunicateSender
    from coworker.channels.desktop.registry import DesktopRegistry


class DesktopChannel(InlineChannel):
    """CoWorker Desktop outbound channel (prefix ``coworker-desktop:``)."""

    def __init__(self, sender: DesktopCommunicateSender, registry: DesktopRegistry) -> None:
        super().__init__(
            DESKTOP_PREFIX,
            sender.send,
            supports_extra=True,
            name="desktop",
        )
        self._registry = registry

    def list_connections(self) -> list[ConnectionInfo]:
        # The registry prunes actors whose participant_id is no longer live, so
        # every actor here is a currently-connected desktop participant.
        return [
            ConnectionInfo(
                participant_id=state.participant_id,
                channel="desktop",
                kind=f"desktop:actor:{state.actor_id}",
                display_name=state.display_name,
                active=True,
            )
            for state in self._registry.actors.values()
        ]
