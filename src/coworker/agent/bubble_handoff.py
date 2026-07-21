from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from fnmatch import fnmatchcase
from typing import TYPE_CHECKING

from loguru import logger

from coworker.core.constants import (
    DEFAULT_BUBBLE_HANDOFF_TRANSPARENCY_PARTICIPANT_MATCHES,
    DEFAULT_BUBBLE_HANDOFF_TRANSPARENCY_STREAM_TRANSPORTS,
)
from coworker.i18n import tr

if TYPE_CHECKING:
    from coworker.agent.bubble import Bubble
    from coworker.tools.communicate_tool import CommunicateTool

BUBBLE_REPLY_PREFIX = "🫧 泡泡："
BUBBLE_MESSAGE_METADATA_KEY = "bubble"
_DESKTOP_PARTICIPANT_PREFIX = "coworker-desktop:"
_SUPPORTED_STREAM_TRANSPORTS = frozenset({"websocket", "sse"})


def format_handoff_start_message(bubble_id: str, *, resumed: bool = False) -> str:
    return tr("handoff.resume" if resumed else "handoff.start", id=bubble_id)


def format_handoff_end_message(bubble_id: str) -> str:
    return tr("handoff.end", id=bubble_id)


def bubble_handoff_message_extra(
    bubble_id: str,
    *,
    phase: str,
    resumed: bool = False,
) -> dict[str, object]:
    return {
        BUBBLE_MESSAGE_METADATA_KEY: {
            "id": bubble_id,
            "kind": "handoff",
            "phase": phase,
            "resumed": resumed,
        }
    }


def bubble_reply_message_extra(bubble_id: str) -> dict[str, object]:
    return {
        BUBBLE_MESSAGE_METADATA_KEY: {
            "id": bubble_id,
            "kind": "reply",
        }
    }


@dataclass(frozen=True)
class BubbleHandoffMatcher:
    """Decide whether a Bubble handoff should be visible to the participant.

    Explicit full-ID glob matches take precedence. Desktop participant IDs that
    do not match a configured glob are excluded from the generic stream rule, so
    ``claude`` and ``codex`` remain attributed to their own clients by default.
    Other participants can opt in through a live WebSocket or SSE transport.
    """

    participant_matches: tuple[str, ...] = ()
    stream_transports: frozenset[str] = frozenset()

    @classmethod
    def from_config(
        cls,
        *,
        participant_matches: Iterable[str] | None = None,
        stream_transports: Iterable[str] | None = None,
    ) -> BubbleHandoffMatcher:
        configured_matches = (
            DEFAULT_BUBBLE_HANDOFF_TRANSPARENCY_PARTICIPANT_MATCHES
            if participant_matches is None
            else participant_matches
        )
        configured_transports = (
            DEFAULT_BUBBLE_HANDOFF_TRANSPARENCY_STREAM_TRANSPORTS
            if stream_transports is None
            else stream_transports
        )
        return cls(
            participant_matches=tuple(_normalize_participant_matches(configured_matches)),
            stream_transports=frozenset(
                transport
                for transport in configured_transports
                if transport in _SUPPORTED_STREAM_TRANSPORTS
            ),
        )

    def matches(
        self,
        participant_id: str,
        *,
        stream_transport: str | None = None,
    ) -> bool:
        """Return whether this participant should receive visible handoff notices."""
        if not participant_id:
            return False
        if any(fnmatchcase(participant_id, pattern) for pattern in self.participant_matches):
            return True
        if is_desktop_participant(participant_id):
            return False
        return stream_transport in self.stream_transports


class BubbleHandoffNotifier:
    """Deliver visible Bubble lifecycle notices without coupling them to spawning."""

    def __init__(self, communicate: CommunicateTool | None) -> None:
        self._communicate = communicate

    async def announce_started(self, bubble: Bubble, *, resumed: bool = False) -> None:
        await self._announce(
            bubble,
            message=format_handoff_start_message(bubble.id, resumed=resumed),
            extra=bubble_handoff_message_extra(
                bubble.id,
                phase="start",
                resumed=resumed,
            ),
            phase="start",
        )

    async def announce_finished(self, bubble: Bubble) -> None:
        await self._announce(
            bubble,
            message=format_handoff_end_message(bubble.id),
            extra=bubble_handoff_message_extra(bubble.id, phase="end"),
            phase="end",
        )

    async def _announce(
        self,
        bubble: Bubble,
        *,
        message: str,
        extra: dict[str, object],
        phase: str,
    ) -> None:
        if (
            not bubble.handoff_transparency
            or not bubble.participant_id
            or self._communicate is None
        ):
            return
        try:
            outgoing_extra = (
                extra if self._communicate.supports_message_extra(bubble.participant_id) else None
            )
            result = await self._communicate.execute(
                participant_id=bubble.participant_id,
                conversation_id=bubble.conversation_id or None,
                message=message,
                extra=outgoing_extra,
            )
            if result.is_error:
                logger.warning(
                    f"Bubble {bubble.id} handoff-{phase} notice failed: {result.content}"
                )
        except Exception as error:
            logger.warning(f"Bubble {bubble.id} handoff-{phase} notice raised: {error}")


def is_desktop_participant(participant_id: str) -> bool:
    return participant_id.startswith(_DESKTOP_PARTICIPANT_PREFIX)


def bubble_reply_fallback_prefix(participant_id: str) -> str:
    """Return a text fallback only for channels without guaranteed Bubble metadata UI."""
    return "" if is_desktop_participant(participant_id) else tr("handoff.reply_prefix")


def _normalize_participant_matches(matches: Iterable[str]) -> list[str]:
    normalized: list[str] = []
    for value in matches:
        if not isinstance(value, str):
            continue
        pattern = value.strip()
        if pattern and pattern not in normalized:
            normalized.append(pattern)
    return normalized
