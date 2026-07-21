from __future__ import annotations

import pytest

from coworker.channels.registry import (
    ChannelParticipant,
    CommunicationChannel,
    CommunicationChannelRegistry,
    ParticipantIdResolutionError,
)
from coworker.core.types import CommunicateRequest, ToolResult


async def _sender(_request: CommunicateRequest) -> ToolResult:
    return ToolResult(tool_call_id="", content="ok")


def _channel(
    prefix: str,
    *participants: ChannelParticipant,
) -> CommunicationChannel:
    return CommunicationChannel(
        prefix=prefix,
        sender=_sender,
        participants=lambda: participants,
    )


def test_directory_alias_resolves_to_canonical_participant_id():
    registry = CommunicationChannelRegistry()
    registry.register(
        _channel(
            "wecom:",
            ChannelParticipant("wecom:single:U123", aliases=("U123", "single:U123")),
        )
    )

    resolution = registry.resolve("U123")

    assert resolution.participant_id == "wecom:single:U123"
    assert resolution.channel is not None
    assert resolution.verified is True


def test_same_alias_across_directories_is_ambiguous():
    registry = CommunicationChannelRegistry()
    registry.register(
        _channel("alpha:", ChannelParticipant("alpha:alice", aliases=("alice",)))
    )
    registry.register(
        _channel("beta:", ChannelParticipant("beta:alice", aliases=("alice",)))
    )

    with pytest.raises(ParticipantIdResolutionError) as raised:
        registry.resolve("alice")

    assert "多个信道" in str(raised.value)
    assert "alpha:alice" in str(raised.value)
    assert "beta:alice" in str(raised.value)


def test_suggestions_use_aliases_and_stay_with_explicit_channel():
    registry = CommunicationChannelRegistry()
    alpha = _channel(
        "alpha:",
        ChannelParticipant("alpha:alice", aliases=("alice",)),
    )
    registry.register(alpha)
    registry.register(
        _channel("beta:", ChannelParticipant("beta:alixe", aliases=("alixe",)))
    )

    resolution = registry.resolve("alpha:alixe")
    suggestions = registry.suggest("alpha:alixe", resolution)

    assert suggestions == ["alpha:alice"]


def test_replacing_channel_drops_previous_resolver_and_directory():
    registry = CommunicationChannelRegistry()
    registry.register(
        CommunicationChannel(
            prefix="chan:",
            sender=_sender,
            resolver=lambda participant_id: f"chan:{participant_id}",
            participants=lambda: [
                ChannelParticipant("chan:alice", aliases=("alice",))
            ],
        )
    )
    registry.register(CommunicationChannel(prefix="chan:", sender=_sender))

    resolution = registry.resolve("alice")

    assert resolution.participant_id == "alice"
    assert resolution.channel is None
    assert resolution.verified is False
