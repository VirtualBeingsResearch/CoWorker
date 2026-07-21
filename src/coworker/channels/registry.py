from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from difflib import SequenceMatcher

from loguru import logger

from coworker.core.types import CommunicateRequest, ToolResult
from coworker.i18n import tr

ChannelSender = Callable[[CommunicateRequest], Awaitable[ToolResult]]
ParticipantResolver = Callable[[str], str | None]

_PARTICIPANT_ID_SUGGESTION_CUTOFF = 0.75
_MAX_PARTICIPANT_ID_SUGGESTIONS = 3


@dataclass(frozen=True, slots=True)
class ChannelParticipant:
    """A canonical channel address and the shorthand aliases that resolve to it."""

    participant_id: str
    aliases: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        participant_id = self.participant_id.strip()
        if not participant_id:
            raise ValueError("channel participant_id cannot be empty")
        aliases = tuple(
            dict.fromkeys(
                alias
                for raw_alias in self.aliases
                if (alias := str(raw_alias).strip()) and alias != participant_id
            )
        )
        object.__setattr__(self, "participant_id", participant_id)
        object.__setattr__(self, "aliases", aliases)


ParticipantProvider = Callable[[], Iterable[ChannelParticipant]]


@dataclass(frozen=True, slots=True)
class CommunicationChannel:
    """Outbound routing, capabilities, and optional participant discovery for a channel."""

    prefix: str
    sender: ChannelSender
    resolver: ParticipantResolver | None = None
    participants: ParticipantProvider | None = None
    supports_extra: bool = False

    def __post_init__(self) -> None:
        if not self.prefix or self.prefix != self.prefix.strip():
            raise ValueError("channel prefix must be a non-empty string without outer whitespace")


@dataclass(frozen=True, slots=True)
class ChannelResolution:
    participant_id: str
    channel: CommunicationChannel | None
    verified: bool = False


class ParticipantIdResolutionError(ValueError):
    """Raised when a participant ID cannot be resolved safely."""


class CommunicationChannelRegistry:
    """Own channel routing, shorthand resolution, and model-facing target suggestions."""

    def __init__(self) -> None:
        self._channels: dict[str, CommunicationChannel] = {}

    def register(self, channel: CommunicationChannel) -> None:
        self._channels[channel.prefix] = channel

    def resolve(self, participant_id: str) -> ChannelResolution:
        explicit_channel = self._matching_channel(participant_id)
        if explicit_channel is not None:
            verified = any(
                participant.participant_id == participant_id
                for participant in self._participants_for(explicit_channel)
            )
            return ChannelResolution(participant_id, explicit_channel, verified)

        matches: dict[tuple[str, str], CommunicationChannel] = {}
        for channel in self._channels.values():
            if channel.resolver is not None:
                try:
                    canonical_id = channel.resolver(participant_id)
                except Exception as error:
                    raise ParticipantIdResolutionError(
                        tr(
                            "tool_result.communicate.resolver_failed",
                            prefix=channel.prefix,
                            participant=participant_id,
                            error=error,
                        )
                    ) from error
                if canonical_id is not None:
                    self._validate_canonical_id(channel, canonical_id)
                    matches[(channel.prefix, canonical_id)] = channel

            for participant in self._participants_for(channel):
                if participant_id in participant.aliases:
                    matches[(channel.prefix, participant.participant_id)] = channel

        if len(matches) == 1:
            (_, canonical_id), channel = next(iter(matches.items()))
            return ChannelResolution(canonical_id, channel, verified=True)
        if len(matches) > 1:
            options = "\n".join(
                tr("tool_result.communicate.option", id=canonical_id, prefix=prefix)
                for prefix, canonical_id in sorted(matches)
            )
            raise ParticipantIdResolutionError(
                tr(
                    "tool_result.communicate.ambiguous",
                    participant=participant_id,
                    options=options,
                )
            )
        return ChannelResolution(participant_id, None)

    def suggest(
        self,
        requested_id: str,
        resolution: ChannelResolution,
        *,
        connected_ids: Iterable[str] = (),
        limit: int = _MAX_PARTICIPANT_ID_SUGGESTIONS,
        cutoff: float = _PARTICIPANT_ID_SUGGESTION_CUTOFF,
    ) -> list[str]:
        if resolution.verified:
            return []

        connected = tuple(dict.fromkeys(str(item) for item in connected_ids))
        if requested_id in connected or resolution.participant_id in connected:
            return []

        candidates: dict[str, set[str]] = {}
        selected_prefix = resolution.channel.prefix if resolution.channel is not None else ""
        match_requested_id = (
            requested_id.removeprefix(selected_prefix)
            if selected_prefix and requested_id.startswith(selected_prefix)
            else requested_id
        )

        def add_candidate(participant_id: str, aliases: Iterable[str] = ()) -> None:
            canonical_match_key = (
                participant_id.removeprefix(selected_prefix)
                if selected_prefix and participant_id.startswith(selected_prefix)
                else participant_id
            )
            match_keys = candidates.setdefault(participant_id, {canonical_match_key})
            match_keys.update(aliases)

        for participant_id in connected:
            if resolution.channel is not None and not participant_id.startswith(
                resolution.channel.prefix
            ):
                continue
            add_candidate(participant_id)

        channels = (
            (resolution.channel,)
            if resolution.channel is not None
            else tuple(self._channels.values())
        )
        for channel in channels:
            for participant in self._participants_for(channel):
                add_candidate(participant.participant_id, participant.aliases)

        ranked: list[tuple[float, str]] = []
        for participant_id, match_keys in candidates.items():
            score = max(
                SequenceMatcher(None, match_requested_id, match_key).ratio()
                for match_key in match_keys
            )
            if score >= cutoff:
                ranked.append((score, participant_id))
        ranked.sort(key=lambda item: (-item[0], item[1]))
        return [participant_id for _, participant_id in ranked[: max(0, limit)]]

    def _matching_channel(self, participant_id: str) -> CommunicationChannel | None:
        for prefix in sorted(self._channels, key=len, reverse=True):
            if participant_id.startswith(prefix):
                return self._channels[prefix]
        return None

    def _participants_for(
        self,
        channel: CommunicationChannel,
    ) -> tuple[ChannelParticipant, ...]:
        if channel.participants is None:
            return ()
        try:
            participants = tuple(channel.participants())
        except Exception as error:
            logger.warning(
                f"Communication channel '{channel.prefix}' participant provider failed: {error}"
            )
            return ()

        valid: list[ChannelParticipant] = []
        for participant in participants:
            if not isinstance(participant, ChannelParticipant):
                logger.warning(
                    f"Communication channel '{channel.prefix}' returned an invalid participant"
                )
                continue
            if not participant.participant_id.startswith(channel.prefix):
                logger.warning(
                    f"Communication channel '{channel.prefix}' returned out-of-prefix target "
                    f"'{participant.participant_id}'"
                )
                continue
            valid.append(participant)
        return tuple(valid)

    @staticmethod
    def _validate_canonical_id(channel: CommunicationChannel, participant_id: str) -> None:
        if not participant_id.startswith(channel.prefix):
            raise ParticipantIdResolutionError(
                tr(
                    "tool_result.communicate.invalid_channel_target",
                    prefix=channel.prefix,
                    participant=participant_id,
                )
            )
