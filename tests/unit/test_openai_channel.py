from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest

from coworker.channels.openai.channel import (
    OpenAIChannel,
    fingerprint_conversation,
)
from coworker.channels.openai.tokens import ExtraTokenStore
from coworker.channels.openai.waiters import BusyError, OpenAITurn
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


@pytest.mark.asyncio
async def test_call_client_tool_does_not_wait_for_client_result() -> None:
    channel = OpenAIChannel(extras=ExtraTokenStore())
    channel.publish_inbound = AsyncMock()
    task = asyncio.create_task(
        channel.open_user_turn(
            participant_id="openai:api",
            conversation_id="win",
            user_text="open a.py",
            system_text="",
            catalog={"read_file": {"name": "read_file"}},
            originating_task="open a.py",
        )
    )
    await asyncio.sleep(0)
    channel.prepare_client_tool_batch("openai:api", "win", 2)
    with locale_context("en"):
        first = await asyncio.wait_for(
            channel.call_client_tool(
                name="read_file",
                arguments={"path": "a.py"},
                participant_id="openai:api",
                conversation_id="win",
            ),
            timeout=0.5,
        )
        second = await asyncio.wait_for(
            channel.call_client_tool(
                name="read_file",
                arguments={"path": "b.py"},
                participant_id="openai:api",
                conversation_id="win",
            ),
            timeout=0.5,
        )
    assert not first.is_error
    assert not second.is_error
    assert "Dispatched" in first.content
    assert "Dispatched" in second.content
    completion = await asyncio.wait_for(task, timeout=0.5)
    assert completion.kind == "tool_calls"
    assert len(completion.tool_calls) == 2
    assert {call.name for call in completion.tool_calls} == {"read_file"}


@pytest.mark.asyncio
async def test_call_client_tool_dispatched_zh_cn() -> None:
    channel = OpenAIChannel(extras=ExtraTokenStore())
    channel.publish_inbound = AsyncMock()
    task = asyncio.create_task(
        channel.open_user_turn(
            participant_id="openai:api",
            conversation_id="win",
            user_text="open a.py",
            system_text="",
            catalog={"read_file": {"name": "read_file"}},
            originating_task="open a.py",
        )
    )
    await asyncio.sleep(0)
    with locale_context("zh-CN"):
        result = await asyncio.wait_for(
            channel.call_client_tool(
                name="read_file",
                arguments={"path": "a.py"},
                participant_id="openai:api",
                conversation_id="win",
            ),
            timeout=0.5,
        )
    assert not result.is_error
    assert "派发" in result.content
    await asyncio.wait_for(task, timeout=0.5)


@pytest.mark.asyncio
async def test_tool_followup_arrives_as_inbound_mail() -> None:
    channel = OpenAIChannel(extras=ExtraTokenStore())
    received: list[IncomingEvent] = []

    async def capture(event: IncomingEvent) -> None:
        received.append(event)

    channel.set_inbound_handler(capture)
    task = asyncio.create_task(
        channel.open_user_turn(
            participant_id="openai:api",
            conversation_id="win",
            user_text="open a.py",
            system_text="",
            catalog={"read_file": {"name": "read_file"}},
            originating_task="open a.py",
        )
    )
    await asyncio.sleep(0)
    dispatched = await channel.call_client_tool(
        name="read_file",
        arguments={"path": "a.py"},
        participant_id="openai:api",
        conversation_id="win",
    )
    assert not dispatched.is_error
    completion = await task
    assert completion.kind == "tool_calls"
    call_id = completion.tool_calls[0].id
    followup = asyncio.create_task(
        channel.open_tool_followup(
            participant_id="openai:api",
            conversation_id="win",
            results={call_id: "print('hi')"},
        )
    )
    await asyncio.sleep(0)
    assert any("print('hi')" in event.content for event in received[1:])
    await channel.send(
        CommunicateRequest(
            participant_id="openai:api",
            conversation_id="win",
            message="done",
        )
    )
    followup_completion = await followup
    assert followup_completion.kind == "stop"
    assert followup_completion.content == "done"


@pytest.mark.asyncio
async def test_second_tool_round_accepts_accumulated_history() -> None:
    channel = OpenAIChannel(extras=ExtraTokenStore())
    channel.publish_inbound = AsyncMock()
    first_turn = asyncio.create_task(
        channel.open_user_turn(
            participant_id="openai:api",
            conversation_id="win",
            user_text="open a.py",
            system_text="",
            catalog={"read_file": {"name": "read_file"}},
            originating_task="open a.py",
        )
    )
    await asyncio.sleep(0)
    await channel.call_client_tool(
        name="read_file",
        arguments={"path": "a.py"},
        participant_id="openai:api",
        conversation_id="win",
    )
    first_completion = await first_turn
    old_id = first_completion.tool_calls[0].id
    second_turn = asyncio.create_task(
        channel.open_tool_followup(
            participant_id="openai:api",
            conversation_id="win",
            results={old_id: "first file"},
        )
    )
    await asyncio.sleep(0)
    await channel.call_client_tool(
        name="read_file",
        arguments={"path": "b.py"},
        participant_id="openai:api",
        conversation_id="win",
    )
    second_completion = await second_turn
    new_id = second_completion.tool_calls[0].id
    third_turn = asyncio.create_task(
        channel.open_tool_followup(
            participant_id="openai:api",
            conversation_id="win",
            results={old_id: "first file", new_id: "second file"},
        )
    )
    await asyncio.sleep(0)
    inbound = channel.publish_inbound.await_args.args[0]
    assert "second file" in inbound.content
    assert "first file" not in inbound.content
    await channel.send(
        CommunicateRequest(
            participant_id="openai:api",
            conversation_id="win",
            message="done",
        )
    )
    third_completion = await third_turn
    assert third_completion.kind == "stop"
    assert third_completion.content == "done"


@pytest.mark.asyncio
async def test_deliver_tool_results_ignores_ids_from_earlier_rounds() -> None:
    turn = OpenAITurn(
        participant_id="openai:api",
        conversation_id="win",
        catalog={"read_file": {"name": "read_file"}},
        timeout_seconds=5,
    )
    turn.prepare_client_calls(1)
    pending = turn.register_client_call("read_file", {"path": "a.py"})
    turn.deliver_tool_results(
        {
            "call_stale": "old result",
            pending.openai_id: "new result",
        }
    )
    with pytest.raises(ValueError):
        turn.deliver_tool_results({"call_stale": "old result"})
