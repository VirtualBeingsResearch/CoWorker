from __future__ import annotations

import json

import pytest

from coworker.channels.access import (
    ChannelAccessController,
    ChannelAccessDeniedError,
)
from coworker.channels.base import BaseChannel, ConnectionInfo
from coworker.channels.inbound import InboundEnvelope
from coworker.channels.registry import ChannelRegistry
from coworker.core.config import ChannelAccessConfig, Config
from coworker.core.types import CommunicateRequest, ToolResult
from coworker.i18n import locale_context


class _RecordingChannel(BaseChannel):
    name = "sample"
    participant_prefix = "sample:"

    def __init__(self) -> None:
        super().__init__()
        self.inbound: list[InboundEnvelope] = []
        self.outbound: list[CommunicateRequest] = []

    async def receive_raw(self, envelope: InboundEnvelope) -> None:
        self.inbound.append(envelope)

    async def send(self, request: CommunicateRequest) -> ToolResult:
        self.outbound.append(request)
        return ToolResult(tool_call_id="", content="sent")

    def resolve(self, participant_id: str) -> str | None:
        return "sample:allowed" if participant_id == "allowed" else None

    def list_connections(self) -> list[ConnectionInfo]:
        return [
            ConnectionInfo(participant_id="sample:allowed", channel=self.name, kind="test"),
            ConnectionInfo(participant_id="sample:blocked", channel=self.name, kind="test"),
        ]


def _access(**rules: list[str]) -> ChannelAccessConfig:
    return ChannelAccessConfig.model_validate({"sample": rules})


def test_channel_access_config_keeps_direct_shape_and_normalizes_patterns() -> None:
    config = Config.model_validate(
        {
            "channel_access": {
                " sample ": {
                    "inbound_allow": [" sample:* ", "sample:*", ""],
                }
            }
        }
    )

    assert config.channel_access.root["sample"].inbound_allow == ["sample:*"]
    assert config.model_dump(mode="json")["channel_access"] == {
        "sample": {
            "inbound_allow": ["sample:*"],
            "inbound_deny": [],
            "outbound_allow": [],
            "outbound_deny": [],
        }
    }


def test_channel_access_loads_direct_json_from_environment(monkeypatch, tmp_path) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv(
        "CHANNEL_ACCESS",
        json.dumps({"wecom": {"outbound_deny": ["wecom:group:private-*"]}}),
    )

    config = Config()

    assert config.channel_access.root["wecom"].outbound_deny == [
        "wecom:group:private-*"
    ]


def test_deny_precedes_allow_and_empty_allow_is_unrestricted() -> None:
    controller = ChannelAccessController(
        _access(
            inbound_allow=["sample:*"],
            inbound_deny=["sample:blocked"],
            outbound_deny=["sample:private-*"],
        )
    )

    assert controller.allows("sample", "inbound", "sample:allowed")
    assert not controller.allows("sample", "inbound", "sample:blocked")
    assert controller.allows("sample", "outbound", "sample:public")
    assert not controller.allows("sample", "outbound", "sample:private-one")
    assert controller.allows("other", "inbound", "other:any")
    assert controller.allows("sample", "inbound", "sample:User1")
    assert not controller.allows("sample", "inbound", "Sample:User1")


@pytest.mark.asyncio
async def test_registry_enforces_canonical_outbound_and_filters_connections() -> None:
    access = ChannelAccessController(
        _access(
            outbound_allow=["sample:allowed", "sample:blocked"],
            outbound_deny=["sample:blocked"],
        )
    )
    registry = ChannelRegistry(access)
    channel = _RecordingChannel()
    registry.register(channel)

    allowed = await registry.send(CommunicateRequest(participant_id="allowed", message="hi"))
    blocked = await registry.send(
        CommunicateRequest(participant_id="sample:blocked", message="hi")
    )
    with locale_context("en"):
        blocked_en = await registry.send(
            CommunicateRequest(participant_id="sample:blocked", message="hi")
        )

    assert not allowed.is_error
    assert channel.outbound == [
        CommunicateRequest(participant_id="sample:allowed", message="hi")
    ]
    assert blocked.is_error
    assert "信道访问策略" in blocked.content
    assert "Channel access policy" in blocked_en.content
    assert [entry["status"] for entry in access.traffic.recent(10)] == [
        "denied",
        "denied",
        "sent",
    ]
    assert [item.participant_id for item in registry.list_connections()] == [
        "sample:allowed"
    ]


@pytest.mark.asyncio
async def test_registry_rejects_inbound_before_transport_processing() -> None:
    access = ChannelAccessController(_access(inbound_deny=["sample:allowed"]))
    registry = ChannelRegistry(access)
    channel = _RecordingChannel()
    registry.register(channel)
    envelope = InboundEnvelope(
        participant_id="allowed",
        source="rest",
        payload={"content": "blocked"},
    )

    with pytest.raises(ChannelAccessDeniedError):
        await registry.receive_raw(envelope)

    assert channel.inbound == []
    entry = access.traffic.recent(1)[0]
    assert entry["ts"]
    assert {key: value for key, value in entry.items() if key != "ts"} == {
        "direction": "inbound",
        "channel": "sample",
        "participant_id": "sample:allowed",
        "status": "denied",
        "source": "rest",
        "reason": "policy",
    }


def test_controller_observes_hot_replacement_of_shared_config() -> None:
    config = ChannelAccessConfig()
    controller = ChannelAccessController(config)
    assert controller.allows("sample", "outbound", "sample:blocked")

    config.root = _access(outbound_deny=["sample:blocked"]).root

    assert not controller.allows("sample", "outbound", "sample:blocked")
