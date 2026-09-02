from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest

from coworker.channels.openai.channel import (
    OpenAIChannel,
    fingerprint_conversation,
)
from coworker.channels.openai.tokens import ExtraTokenStore
from coworker.channels.openai.waiters import BusyError
from coworker.core.types import CommunicateRequest, IncomingEvent
from coworker.i18n import locale_context
from coworker.persona import PersonStore
from coworker.tools.client_tool import CallClientTool
from coworker.tools.registry import ToolRegistry


def test_fingerprint_ignores_tools_and_later_users() -> None:
    first = fingerprint_conversation(
        [
            {"role": "system", "content": "You are Cursor."},
            {"role": "user", "content": "hello"},
        ]
    )
    same = fingerprint_conversation(
        [
            {"role": "system", "content": "You are Cursor."},
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": "hi"},
            {"role": "user", "content": "second turn"},
        ]
    )
    with_tools = fingerprint_conversation(
        [
            {"role": "system", "content": "You are Cursor."},
            {"role": "user", "content": "hello"},
        ]
    )
    assert first == same == with_tools
    different = fingerprint_conversation(
        [
            {"role": "system", "content": "You are Cursor."},
            {"role": "user", "content": "hello world"},
        ]
    )
    assert different != first


@pytest.mark.asyncio
async def test_control_cannot_touch_primary_and_lists_without_secrets() -> None:
    extras = ExtraTokenStore()
    channel = OpenAIChannel(extras=extras)
    with locale_context("en"):
        refused = await channel.send(
            CommunicateRequest(
                participant_id="openai:control",
                extra={"action": "issue", "name": "api"},
            )
        )
        assert refused.is_error
        issued = await channel.send(
            CommunicateRequest(
                participant_id="openai:control",
                extra={"action": "issue", "name": "cursor"},
            )
        )
        assert not issued.is_error
        assert "cwct_v1_" in issued.content
        listed = await channel.send(
            CommunicateRequest(
                participant_id="openai:control",
                extra={"action": "list"},
            )
        )
        assert not listed.is_error
        assert "openai:cursor" in listed.content
        assert "cwct_v1_" not in listed.content
        connections = {item.participant_id for item in channel.list_connections()}
        assert "openai:control" in connections
        assert "openai:api" in connections
        assert "openai:cursor" in connections


@pytest.mark.asyncio
async def test_issue_can_bind_immediately(tmp_path) -> None:
    extras = ExtraTokenStore()
    store = PersonStore(tmp_path / "persons.json")
    channel = OpenAIChannel(extras=extras, person_store=store)
    result = await channel.send(
        CommunicateRequest(
            participant_id="openai:control",
            extra={"action": "issue", "name": "cursor", "person": "Ada"},
        )
    )
    assert not result.is_error
    person = store.find_by_participant("openai:cursor")
    assert person is not None
    assert person.display_name == "Ada"
    assert person.aliases[0].conversation_id is None


@pytest.mark.asyncio
async def test_issue_without_person_stays_unbound(tmp_path) -> None:
    extras = ExtraTokenStore()
    store = PersonStore(tmp_path / "persons.json")
    channel = OpenAIChannel(extras=extras, person_store=store)
    result = await channel.send(
        CommunicateRequest(
            participant_id="openai:control",
            extra={"action": "issue", "name": "cursor"},
        )
    )
    assert not result.is_error
    assert store.find_by_participant("openai:cursor") is None


@pytest.mark.asyncio
async def test_revoke_removes_address_but_leaves_persona_alias(tmp_path) -> None:
    extras = ExtraTokenStore()
    store = PersonStore(tmp_path / "persons.json")
    channel = OpenAIChannel(extras=extras, person_store=store)
    await channel.send(
        CommunicateRequest(
            participant_id="openai:control",
            extra={"action": "issue", "name": "cursor", "person": "Ada"},
        )
    )
    await channel.send(
        CommunicateRequest(
            participant_id="openai:control",
            extra={"action": "revoke", "name": "cursor"},
        )
    )
    assert channel.resolve("openai:cursor") is None
    assert store.find_by_participant("openai:cursor") is not None


@pytest.mark.asyncio
async def test_overlapping_turn_is_busy() -> None:
    channel = OpenAIChannel(extras=ExtraTokenStore())
    channel.publish_inbound = AsyncMock()
    task = asyncio.create_task(
        channel.open_user_turn(
            participant_id="openai:api",
            conversation_id="win",
            user_text="hi",
            system_text="",
            catalog={},
            originating_task="hi",
        )
    )
    await asyncio.sleep(0)
    with pytest.raises(BusyError):
        await channel.open_user_turn(
            participant_id="openai:api",
            conversation_id="win",
            user_text="again",
            system_text="",
            catalog={},
            originating_task="again",
        )
    await channel.send(
        CommunicateRequest(
            participant_id="openai:api",
            conversation_id="win",
            message="done",
        )
    )
    completion = await task
    assert completion.kind == "stop"
    assert completion.content == "done"


@pytest.mark.asyncio
async def test_late_communicate_does_not_fulfill() -> None:
    channel = OpenAIChannel(extras=ExtraTokenStore(), timeout_seconds=0.05)
    channel.publish_inbound = AsyncMock()
    completion = await channel.open_user_turn(
        participant_id="openai:api",
        conversation_id="win",
        user_text="hi",
        system_text="",
        catalog={},
        originating_task="hi",
    )
    assert completion.timed_out is True
    late = await channel.send(
        CommunicateRequest(
            participant_id="openai:api",
            conversation_id="win",
            message="too late",
        )
    )
    assert late.is_error


def test_call_client_tool_is_registered_but_client_schemas_are_not() -> None:
    channel = OpenAIChannel(extras=ExtraTokenStore())
    registry = ToolRegistry()
    registry.register(CallClientTool(channel))
    names = {schema["name"] for schema in registry.get_schemas()}
    assert names == {"call_client_tool"}
    scoped = registry.scoped(object())
    scoped_names = {schema["name"] for schema in scoped.get_schemas()}
    assert scoped_names == {"call_client_tool"}


@pytest.mark.asyncio
async def test_inbound_event_uses_openai_source() -> None:
    channel = OpenAIChannel(extras=ExtraTokenStore())
    received: list[IncomingEvent] = []

    async def capture(event: IncomingEvent) -> None:
        received.append(event)

    channel.set_inbound_handler(capture)
    task = asyncio.create_task(
        channel.open_user_turn(
            participant_id="openai:api",
            conversation_id="win",
            user_text="hello",
            system_text="be brief",
            catalog={"read_file": {"name": "read_file"}},
            originating_task="hello",
        )
    )
    await asyncio.sleep(0)
    assert received[0].source == "openai"
    assert received[0].participant_id == "openai:api"
    assert "call_client_tool" in received[0].content or "read_file" in received[0].content
    await channel.send(
        CommunicateRequest(
            participant_id="openai:api",
            conversation_id="win",
            message="ok",
        )
    )
    await task
