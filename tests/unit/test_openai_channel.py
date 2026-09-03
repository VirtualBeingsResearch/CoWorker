from __future__ import annotations

import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from coworker.channels.openai.channel import (
    OpenAIChannel,
    fingerprint_conversation,
    last_user_image_attachments,
    turn_user_image_attachments,
    turn_user_text,
)
from coworker.channels.openai.tokens import ExtraTokenStore
from coworker.channels.openai.waiters import BusyError, OpenAITurn
from coworker.core.types import CommunicateRequest, IncomingEvent
from coworker.i18n import locale_context
from coworker.memory.short_term import ShortTermMemory
from coworker.persona import PersonStore
from coworker.tools.client_tool import CallClientTool
from coworker.tools.registry import ToolRegistry


def test_fingerprint_includes_every_user_in_the_snapshot() -> None:
    first = fingerprint_conversation(
        [
            {"role": "system", "content": "You are Cursor."},
            {"role": "user", "content": "hello"},
        ]
    )
    later = fingerprint_conversation(
        [
            {"role": "system", "content": "You are Cursor."},
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": "hi"},
            {"role": "user", "content": "second turn"},
        ]
    )
    assert later != first
    with_tools = fingerprint_conversation(
        [
            {"role": "system", "content": "You are Cursor."},
            {"role": "user", "content": "hello"},
        ]
    )
    assert first == with_tools
    different = fingerprint_conversation(
        [
            {"role": "system", "content": "You are Cursor."},
            {"role": "user", "content": "hello world"},
        ]
    )
    assert different != first
    opening = fingerprint_conversation(
        [
            {"role": "system", "content": "You are Cursor."},
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": "hi"},
            {"role": "user", "content": "open a.py"},
        ]
    )
    assert opening != first


def test_implicit_id_uses_all_users_from_the_first_request() -> None:
    channel = OpenAIChannel(extras=ExtraTokenStore())
    first_messages = [
        {"role": "system", "content": "You are Cursor."},
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": "hi"},
        {"role": "user", "content": "open a.py"},
    ]
    first_id = channel.resolve_implicit_conversation_id("openai:api", first_messages)
    assert first_id == fingerprint_conversation(first_messages)
    later = [
        *first_messages,
        {"role": "assistant", "content": "ok"},
        {"role": "user", "content": "and then"},
    ]
    assert channel.resolve_implicit_conversation_id("openai:api", later) == first_id
    sibling = [
        {"role": "system", "content": "You are Cursor."},
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": "hi"},
        {"role": "user", "content": "another chat"},
    ]
    sibling_id = channel.resolve_implicit_conversation_id("openai:api", sibling)
    assert sibling_id != first_id
    assert sibling_id == fingerprint_conversation(sibling)


def test_turn_user_text_keeps_every_opening_user_message() -> None:
    messages = [
        {"role": "system", "content": "You are ZCode"},
        {
            "role": "user",
            "content": "<system-reminder>\nAvailable skills.\n</system-reminder>",
        },
        {
            "role": "user",
            "content": "<system-reminder>\nToday's date is 2026-09-03.\n</system-reminder>",
        },
        {"role": "user", "content": "你知道今天是星期几吗？"},
    ]
    assert turn_user_text(messages) == (
        "<system-reminder>\nAvailable skills.\n</system-reminder>\n\n"
        "<system-reminder>\nToday's date is 2026-09-03.\n</system-reminder>\n\n"
        "你知道今天是星期几吗？"
    )


def test_turn_user_text_uses_users_after_last_assistant() -> None:
    messages = [
        {"role": "system", "content": "You are ZCode"},
        {"role": "user", "content": "skills"},
        {"role": "user", "content": "what day is it"},
        {"role": "assistant", "content": "Thursday"},
        {"role": "user", "content": "date update"},
        {"role": "user", "content": "and the timezone"},
    ]
    assert turn_user_text(messages) == "date update\n\nand the timezone"


def test_turn_user_image_attachments_collects_opening_images() -> None:
    png = (
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQ"
        "AAAABJRU5ErkJggg=="
    )
    url = f"data:image/png;base64,{png}"
    attachments = turn_user_image_attachments(
        [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "context"},
                    {"type": "image_url", "image_url": {"url": url}},
                ],
            },
            {"role": "user", "content": "what is in the image"},
        ]
    )
    assert len(attachments) == 1
    assert attachments[0]["data"] == png


def test_last_user_image_attachments_from_data_url_string() -> None:
    png = (
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQ"
        "AAAABJRU5ErkJggg=="
    )
    url = f"data:image/png;base64,{png}"
    attachments = last_user_image_attachments(
        [{"role": "user", "content": url}]
    )
    assert len(attachments) == 1
    assert attachments[0]["media_type"] == "image/png"
    assert attachments[0]["data"] == png

def test_fingerprint_includes_image_data_url() -> None:
    png = (
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQ"
        "AAAABJRU5ErkJggg=="
    )
    text_only = fingerprint_conversation(
        [{"role": "user", "content": "look"}]
    )
    with_image = fingerprint_conversation(
        [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "look"},
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:image/png;base64,{png}"},
                    },
                ],
            }
        ]
    )
    assert with_image != text_only


def test_last_user_image_attachments_parse_data_url() -> None:
    png = (
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQ"
        "AAAABJRU5ErkJggg=="
    )
    attachments = last_user_image_attachments(
        [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "describe"},
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:image/png;base64,{png}"},
                    },
                ],
            }
        ]
    )
    assert len(attachments) == 1
    assert attachments[0]["media_type"] == "image/png"
    assert attachments[0]["filename"] == "image-1.png"
    assert attachments[0]["data"] == png


def test_last_user_image_attachments_reject_remote_url() -> None:
    with locale_context("en"), pytest.raises(ValueError, match="data:image"):
        last_user_image_attachments(
            [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image_url",
                            "image_url": {"url": "https://example.com/a.png"},
                        }
                    ],
                }
            ]
        )


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
        )
    await channel.send(
        CommunicateRequest(
            participant_id="openai:api",
            conversation_id="win",
            message="done",
            extra={"end_turn": True},
        )
    )
    completion = await task
    assert completion.kind == "stop"
    assert completion.content == "done"


@pytest.mark.asyncio
async def test_intermediate_communicate_concatenates_until_end_turn() -> None:
    channel = OpenAIChannel(extras=ExtraTokenStore())
    channel.publish_inbound = AsyncMock()
    task = asyncio.create_task(
        channel.open_user_turn(
            participant_id="openai:api",
            conversation_id="win",
            user_text="hi",
            system_text="",
            catalog={},
        )
    )
    await asyncio.sleep(0)
    mid = await channel.send(
        CommunicateRequest(
            participant_id="openai:api",
            conversation_id="win",
            message="one",
        )
    )
    assert not mid.is_error
    assert not task.done()
    end = await channel.send(
        CommunicateRequest(
            participant_id="openai:api",
            conversation_id="win",
            message="two",
            extra={"end_turn": True},
        )
    )
    assert not end.is_error
    completion = await task
    assert completion.content == "onetwo"


@pytest.mark.asyncio
async def test_stream_turn_emits_delta_then_stop() -> None:
    channel = OpenAIChannel(extras=ExtraTokenStore())
    channel.publish_inbound = AsyncMock()
    turn = await channel.start_user_turn(
        participant_id="openai:api",
        conversation_id="win",
        user_text="hi",
        system_text="",
        catalog={},
        stream=True,
    )
    events: list[str] = []

    async def consume() -> None:
        async for event in turn.iter_events():
            events.append(event.kind if event.kind != "delta" else f"delta:{event.content}")
        channel.settle_turn(turn)

    task = asyncio.create_task(consume())
    await channel.send(
        CommunicateRequest(
            participant_id="openai:api",
            conversation_id="win",
            message="a",
        )
    )
    await channel.send(
        CommunicateRequest(
            participant_id="openai:api",
            conversation_id="win",
            message="b",
            extra={"end_turn": True},
        )
    )
    await task
    assert events == ["delta:a", "delta:b", "stop"]


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
        )
    )
    await asyncio.sleep(0)
    assert received[0].source == "openai"
    assert received[0].participant_id == "openai:api"
    assert received[0].content == "hello"
    await channel.send(
        CommunicateRequest(
            participant_id="openai:api",
            conversation_id="win",
            message="ok",
            extra={"end_turn": True},
        )
    )
    await task


def _kind_pins(short_term: ShortTermMemory, kind: str) -> list:
    prefix = f"openai-req:{kind}:"
    return [item for item in short_term.pinned_items if item.pin_id.startswith(prefix)]


def _pointer_pins(short_term: ShortTermMemory) -> list:
    return [
        item
        for item in short_term.pinned_items
        if item.pin_id.startswith("openai-ptr:")
    ]


def test_changed_system_or_tools_update_pins(tmp_path) -> None:
    short_term = ShortTermMemory(tree_enabled=False)
    channel = OpenAIChannel(
        extras=ExtraTokenStore(),
        attachments_dir=tmp_path / "attachments",
    )
    channel.set_short_term(short_term)
    with locale_context("en"):
        channel._inbound_body(
            participant_id="openai:api",
            conversation_id="win",
            user_text="hi",
            system_text="Be brief.",
            catalog={"ping": {"name": "ping"}},
        )
        system_update = channel._inbound_body(
            participant_id="openai:api",
            conversation_id="win",
            user_text="next",
            system_text="Be thorough.",
            catalog={"ping": {"name": "ping"}},
        )
        tools_update = channel._inbound_body(
            participant_id="openai:api",
            conversation_id="win",
            user_text="later",
            system_text="Be thorough.",
            catalog={"pong": {"name": "pong", "description": "other"}},
        )
    assert "Be thorough." in _kind_pins(short_term, "system")[0].content
    assert "Be brief." not in _kind_pins(short_term, "system")[0].content
    assert "Be thorough." not in system_update
    assert '"name": "pong"' in _kind_pins(short_term, "tools")[0].content
    assert "ping" not in _kind_pins(short_term, "tools")[0].content
    assert '"name": "pong"' not in tools_update
    assert tools_update.endswith("later")


def test_system_and_tools_are_pinned_as_system_managed(tmp_path) -> None:
    short_term = ShortTermMemory(tree_enabled=False)
    channel = OpenAIChannel(
        extras=ExtraTokenStore(),
        attachments_dir=tmp_path / "attachments",
    )
    channel.set_short_term(short_term)
    with locale_context("en"):
        body = channel._inbound_body(
            participant_id="openai:api",
            conversation_id="win",
            user_text="hi",
            system_text="Be brief.",
            catalog={"ping": {"name": "ping", "description": "ok"}},
        )
    system_pins = _kind_pins(short_term, "system")
    tools_pins = _kind_pins(short_term, "tools")
    assert len(system_pins) == 1 and len(tools_pins) == 1
    system_pin = system_pins[0]
    tools_pin = tools_pins[0]
    assert system_pin.system_managed is True
    assert tools_pin.system_managed is True
    assert "Be brief." in system_pin.content
    assert '"name": "ping"' in tools_pin.content
    assert "openai:api" not in system_pin.label
    assert "win" not in system_pin.label
    assert "openai:api" not in tools_pin.label
    assert "win" not in tools_pin.label
    assert ":system:" in system_pin.pin_id
    assert ":tools:" in tools_pin.pin_id
    pointers = _pointer_pins(short_term)
    assert len(pointers) == 1
    pointer = pointers[0]
    assert pointer.system_managed is True
    assert "openai:api" in pointer.label
    assert "win" in pointer.label
    assert system_pin.pin_id.rsplit(":", 1)[-1][:12] in pointer.content
    assert tools_pin.pin_id.rsplit(":", 1)[-1][:12] in pointer.content
    assert "Be brief." not in body
    assert '"name": "ping"' not in body
    assert body.endswith("hi")
    assert not (tmp_path / "openai" / "detail").exists()


def test_unchanged_request_does_not_rewrite_pin_content(tmp_path) -> None:
    short_term = ShortTermMemory(tree_enabled=False)
    channel = OpenAIChannel(
        extras=ExtraTokenStore(),
        attachments_dir=tmp_path / "attachments",
    )
    channel.set_short_term(short_term)
    catalog = {"ping": {"name": "ping"}}
    with locale_context("en"):
        channel._inbound_body(
            participant_id="openai:api",
            conversation_id="win",
            user_text="hi",
            system_text="Be brief.",
            catalog=catalog,
        )
        system_pin = _kind_pins(short_term, "system")[0]
        before_content = system_pin.content
        before_id = system_pin.pin_id
        channel._inbound_body(
            participant_id="openai:api",
            conversation_id="win",
            user_text="again",
            system_text="Be brief.",
            catalog=catalog,
        )
        after = _kind_pins(short_term, "system")[0]
    assert after.pin_id == before_id
    assert after.content == before_content
    assert len(_kind_pins(short_term, "system")) == 1
    assert len(_kind_pins(short_term, "tools")) == 1
    assert len(_pointer_pins(short_term)) == 1


def test_changed_system_updates_pin(tmp_path) -> None:
    short_term = ShortTermMemory(tree_enabled=False)
    channel = OpenAIChannel(
        extras=ExtraTokenStore(),
        attachments_dir=tmp_path / "attachments",
    )
    channel.set_short_term(short_term)
    with locale_context("en"):
        channel._inbound_body(
            participant_id="openai:api",
            conversation_id="win",
            user_text="hi",
            system_text="Be brief.",
            catalog={},
        )
        channel._inbound_body(
            participant_id="openai:api",
            conversation_id="win",
            user_text="next",
            system_text="Be thorough.",
            catalog={},
        )
    pin = _kind_pins(short_term, "system")[0]
    assert "Be thorough." in pin.content
    assert "Be brief." not in pin.content
    assert len(_kind_pins(short_term, "system")) == 1


def test_cleared_system_and_tools_are_unpinned(tmp_path) -> None:
    short_term = ShortTermMemory(tree_enabled=False)
    channel = OpenAIChannel(
        extras=ExtraTokenStore(),
        attachments_dir=tmp_path / "attachments",
    )
    channel.set_short_term(short_term)
    with locale_context("en"):
        channel._inbound_body(
            participant_id="openai:api",
            conversation_id="win",
            user_text="hi",
            system_text="Be brief.",
            catalog={"ping": {"name": "ping"}},
        )
        assert len(short_term.pinned_items) == 3
        channel._inbound_body(
            participant_id="openai:api",
            conversation_id="win",
            user_text="cleared",
            system_text="",
            catalog={},
        )
    assert short_term.pinned_items == []


@pytest.mark.asyncio
async def test_end_turn_unpins_and_next_turn_repins(tmp_path) -> None:
    short_term = ShortTermMemory(tree_enabled=False)
    channel = OpenAIChannel(
        extras=ExtraTokenStore(),
        attachments_dir=tmp_path / "attachments",
    )
    channel.set_short_term(short_term)
    channel.publish_inbound = AsyncMock()
    catalog = {"ping": {"name": "ping"}}
    first = asyncio.create_task(
        channel.open_user_turn(
            participant_id="openai:api",
            conversation_id="win",
            user_text="one",
            system_text="Be brief.",
            catalog=catalog,
        )
    )
    await asyncio.sleep(0)
    assert len(_kind_pins(short_term, "system")) == 1
    assert len(_kind_pins(short_term, "tools")) == 1
    assert len(_pointer_pins(short_term)) == 1
    await channel.send(
        CommunicateRequest(
            participant_id="openai:api",
            conversation_id="win",
            message="ok",
            extra={"end_turn": True},
        )
    )
    await first
    assert short_term.pinned_items == []

    second = asyncio.create_task(
        channel.open_user_turn(
            participant_id="openai:api",
            conversation_id="win",
            user_text="two",
            system_text="Be brief.",
            catalog=catalog,
        )
    )
    await asyncio.sleep(0)
    assert len(_kind_pins(short_term, "system")) == 1
    assert len(_kind_pins(short_term, "tools")) == 1
    assert len(_pointer_pins(short_term)) == 1
    await channel.send(
        CommunicateRequest(
            participant_id="openai:api",
            conversation_id="win",
            message="done",
            extra={"end_turn": True},
        )
    )
    await second


@pytest.mark.asyncio
async def test_timeout_unpins_request_context(tmp_path) -> None:
    short_term = ShortTermMemory(tree_enabled=False)
    channel = OpenAIChannel(
        extras=ExtraTokenStore(),
        timeout_seconds=0.05,
        attachments_dir=tmp_path / "attachments",
    )
    channel.set_short_term(short_term)
    channel.publish_inbound = AsyncMock()
    completion = await channel.open_user_turn(
        participant_id="openai:api",
        conversation_id="win",
        user_text="hi",
        system_text="Be brief.",
        catalog={},
    )
    assert completion.timed_out is True
    assert short_term.pinned_items == []


@pytest.mark.asyncio
async def test_tool_calls_keep_request_pins(tmp_path) -> None:
    short_term = ShortTermMemory(tree_enabled=False)
    channel = OpenAIChannel(
        extras=ExtraTokenStore(),
        attachments_dir=tmp_path / "attachments",
    )
    channel.set_short_term(short_term)
    channel.publish_inbound = AsyncMock()
    task = asyncio.create_task(
        channel.open_user_turn(
            participant_id="openai:api",
            conversation_id="win",
            user_text="hi",
            system_text="Be brief.",
            catalog={"alpha": {"name": "alpha"}},
        )
    )
    await asyncio.sleep(0)
    dispatched = await channel.call_client_tool(
        participant_id="openai:api",
        conversation_id="win",
        name="alpha",
        arguments={},
    )
    assert not dispatched.is_error
    completion = await task
    assert completion.kind == "tool_calls"
    assert _kind_pins(short_term, "system")
    assert _kind_pins(short_term, "tools")
    assert _pointer_pins(short_term)


def test_pinned_system_reinjects_after_primary_drop(tmp_path) -> None:
    short_term = ShortTermMemory(tree_enabled=False)
    channel = OpenAIChannel(
        extras=ExtraTokenStore(),
        attachments_dir=tmp_path / "attachments",
    )
    channel.set_short_term(short_term)
    with locale_context("en"):
        channel._inbound_body(
            participant_id="openai:api",
            conversation_id="win",
            user_text="hi",
            system_text="Stay pinned.",
            catalog={},
        )
    reinjected = short_term.reinject_missing_pins()
    assert len(reinjected) == 2
    assert any("Stay pinned." in item.content for item in short_term.primary)
    short_term.primary.clear()
    again = short_term.reinject_missing_pins()
    assert len(again) == 2
    assert any("Stay pinned." in item.content for item in again)


@pytest.mark.asyncio
async def test_unchanged_tools_still_available_for_call_client_tool(tmp_path) -> None:
    channel = OpenAIChannel(
        extras=ExtraTokenStore(),
        attachments_dir=tmp_path / "attachments",
    )
    channel.publish_inbound = AsyncMock()
    catalog = {"alpha": {"name": "alpha"}}

    first = asyncio.create_task(
        channel.open_user_turn(
            participant_id="openai:api",
            conversation_id="win",
            user_text="one",
            system_text="rules",
            catalog=catalog,
        )
    )
    await asyncio.sleep(0)
    await channel.send(
        CommunicateRequest(
            participant_id="openai:api",
            conversation_id="win",
            message="ok",
            extra={"end_turn": True},
        )
    )
    await first

    second = asyncio.create_task(
        channel.open_user_turn(
            participant_id="openai:api",
            conversation_id="win",
            user_text="two",
            system_text="rules",
            catalog=catalog,
        )
    )
    await asyncio.sleep(0)
    event = channel.publish_inbound.await_args.args[0]
    assert "two" in event.content
    dispatched = await channel.call_client_tool(
        participant_id="openai:api",
        conversation_id="win",
        name="alpha",
        arguments={},
    )
    assert not dispatched.is_error
    completion = await second
    assert completion.kind == "tool_calls"
    assert {call.name for call in completion.tool_calls} == {"alpha"}


def test_long_system_is_folded_to_detail_file(tmp_path) -> None:
    short_term = ShortTermMemory(tree_enabled=False)
    channel = OpenAIChannel(
        extras=ExtraTokenStore(),
        attachments_dir=tmp_path / "attachments",
    )
    channel.set_short_term(short_term)
    system_text = ("You are a careful assistant. " * 80).strip()
    assert len(system_text) > 1500
    with locale_context("en"):
        body = channel._inbound_body(
            participant_id="openai:api",
            conversation_id="win",
            user_text="hi",
            system_text=system_text,
            catalog={},
        )
    pin = _kind_pins(short_term, "system")[0]
    assert "folded" in pin.content.lower()
    assert "read_file" in pin.content
    assert system_text not in pin.content
    assert system_text not in body
    assert body.endswith("hi")
    detail_dir = tmp_path / "openai" / "detail"
    files = list(detail_dir.glob("*.txt"))
    assert len(files) == 1
    assert files[0].read_text(encoding="utf-8") == system_text
    assert str(files[0].resolve()) in pin.content


def test_long_tools_fold_keeps_names_and_catalog(tmp_path) -> None:
    short_term = ShortTermMemory(tree_enabled=False)
    channel = OpenAIChannel(
        extras=ExtraTokenStore(),
        attachments_dir=tmp_path / "attachments",
    )
    channel.set_short_term(short_term)
    catalog = {
        "alpha": {
            "name": "alpha",
            "description": "A" * 800,
            "parameters": {"type": "object", "properties": {"q": {"type": "string"}}},
        },
        "beta": {
            "name": "beta",
            "description": "B" * 800,
            "parameters": {"type": "object", "properties": {"x": {"type": "number"}}},
        },
    }
    rendered_len = len(
        json.dumps(list(catalog.values()), ensure_ascii=False, indent=2)
    )
    assert rendered_len > 1500
    with locale_context("en"):
        body = channel._inbound_body(
            participant_id="openai:api",
            conversation_id="win",
            user_text="hi",
            system_text="",
            catalog=catalog,
        )
    pin = _kind_pins(short_term, "tools")[0]
    assert "alpha" in pin.content and "beta" in pin.content
    assert "folded" in pin.content.lower() or "Full schemas are folded" in pin.content
    assert '"description":' not in pin.content
    assert body.endswith("hi")
    files = list((tmp_path / "openai" / "detail").glob("*.txt"))
    assert len(files) == 1
    assert "alpha" in files[0].read_text(encoding="utf-8")


def test_matching_windows_share_one_pin_each(tmp_path) -> None:
    short_term = ShortTermMemory(tree_enabled=False)
    channel = OpenAIChannel(
        extras=ExtraTokenStore(),
        attachments_dir=tmp_path / "attachments",
    )
    channel.set_short_term(short_term)
    catalog = {"ping": {"name": "ping"}}
    with locale_context("en"):
        first = channel._inbound_body(
            participant_id="openai:api",
            conversation_id="win-a",
            user_text="one",
            system_text="Be brief.",
            catalog=catalog,
        )
        second = channel._inbound_body(
            participant_id="openai:api",
            conversation_id="win-b",
            user_text="two",
            system_text="Be brief.",
            catalog=catalog,
        )
    assert len(_kind_pins(short_term, "system")) == 1
    assert len(_kind_pins(short_term, "tools")) == 1
    pointers = _pointer_pins(short_term)
    assert len(pointers) == 2
    labels = pointers[0].label + pointers[1].label
    assert "Be brief." not in first
    assert "Be brief." not in second
    assert "win-a" in labels
    assert "win-b" in labels


def test_divergent_window_gets_its_own_pin(tmp_path) -> None:
    short_term = ShortTermMemory(tree_enabled=False)
    channel = OpenAIChannel(
        extras=ExtraTokenStore(),
        attachments_dir=tmp_path / "attachments",
    )
    channel.set_short_term(short_term)
    with locale_context("en"):
        channel._inbound_body(
            participant_id="openai:api",
            conversation_id="win-a",
            user_text="one",
            system_text="Be brief.",
            catalog={},
        )
        channel._inbound_body(
            participant_id="openai:api",
            conversation_id="win-b",
            user_text="two",
            system_text="Be thorough.",
            catalog={},
        )
    pins = _kind_pins(short_term, "system")
    assert len(pins) == 2
    contents = {item.content for item in pins}
    assert any("Be brief." in text for text in contents)
    assert any("Be thorough." in text for text in contents)
    assert len(_pointer_pins(short_term)) == 2


def test_matching_windows_share_folded_detail_file(tmp_path) -> None:
    short_term = ShortTermMemory(tree_enabled=False)
    channel = OpenAIChannel(
        extras=ExtraTokenStore(),
        attachments_dir=tmp_path / "attachments",
    )
    channel.set_short_term(short_term)
    system_text = ("You are a careful assistant. " * 80).strip()
    assert len(system_text) > 1500
    with locale_context("en"):
        first = channel._inbound_body(
            participant_id="openai:api",
            conversation_id="win-a",
            user_text="one",
            system_text=system_text,
            catalog={},
        )
        second = channel._inbound_body(
            participant_id="openai:api",
            conversation_id="win-b",
            user_text="two",
            system_text=system_text,
            catalog={},
        )
    files = list((tmp_path / "openai" / "detail").glob("*.txt"))
    assert len(files) == 1
    assert files[0].read_text(encoding="utf-8") == system_text
    pins = _kind_pins(short_term, "system")
    assert len(pins) == 1
    path = str(files[0].resolve())
    assert path in pins[0].content
    assert path not in first
    assert path not in second
    assert first.endswith("one")
    assert second.endswith("two")


def test_matching_clients_share_pins(tmp_path) -> None:
    short_term = ShortTermMemory(tree_enabled=False)
    channel = OpenAIChannel(
        extras=ExtraTokenStore(),
        attachments_dir=tmp_path / "attachments",
    )
    channel.set_short_term(short_term)
    with locale_context("en"):
        first = channel._inbound_body(
            participant_id="openai:api",
            conversation_id="win",
            user_text="one",
            system_text="Be brief.",
            catalog={},
        )
        second = channel._inbound_body(
            participant_id="openai:cursor",
            conversation_id="win",
            user_text="two",
            system_text="Be brief.",
            catalog={},
        )
    pins = _kind_pins(short_term, "system")
    assert len(pins) == 1
    assert "Be brief." in pins[0].content
    assert "Be brief." not in first
    assert "Be brief." not in second
    pointers = _pointer_pins(short_term)
    assert len(pointers) == 2
    digest = pins[0].pin_id.rsplit(":", 1)[-1][:12]
    assert digest in pointers[0].content
    assert digest in pointers[1].content


def test_matching_clients_share_folded_detail_file(tmp_path) -> None:
    short_term = ShortTermMemory(tree_enabled=False)
    channel = OpenAIChannel(
        extras=ExtraTokenStore(),
        attachments_dir=tmp_path / "attachments",
    )
    channel.set_short_term(short_term)
    system_text = ("You are a careful assistant. " * 80).strip()
    assert len(system_text) > 1500
    with locale_context("en"):
        first = channel._inbound_body(
            participant_id="openai:api",
            conversation_id="win",
            user_text="one",
            system_text=system_text,
            catalog={},
        )
        second = channel._inbound_body(
            participant_id="openai:cursor",
            conversation_id="win",
            user_text="two",
            system_text=system_text,
            catalog={},
        )
    files = list((tmp_path / "openai" / "detail").glob("*.txt"))
    assert len(files) == 1
    pins = _kind_pins(short_term, "system")
    assert len(pins) == 1
    path = str(files[0].resolve())
    assert path in pins[0].content
    assert path not in first
    assert path not in second


@pytest.mark.asyncio
async def test_releasing_one_window_keeps_shared_pin(tmp_path) -> None:
    short_term = ShortTermMemory(tree_enabled=False)
    channel = OpenAIChannel(
        extras=ExtraTokenStore(),
        attachments_dir=tmp_path / "attachments",
    )
    channel.set_short_term(short_term)
    channel.publish_inbound = AsyncMock()
    catalog = {"ping": {"name": "ping"}}
    first = asyncio.create_task(
        channel.open_user_turn(
            participant_id="openai:api",
            conversation_id="win-a",
            user_text="one",
            system_text="Be brief.",
            catalog=catalog,
        )
    )
    await asyncio.sleep(0)
    second = asyncio.create_task(
        channel.open_user_turn(
            participant_id="openai:api",
            conversation_id="win-b",
            user_text="two",
            system_text="Be brief.",
            catalog=catalog,
        )
    )
    await asyncio.sleep(0)
    assert len(_kind_pins(short_term, "system")) == 1
    assert len(_kind_pins(short_term, "tools")) == 1
    assert len(_pointer_pins(short_term)) == 2
    await channel.send(
        CommunicateRequest(
            participant_id="openai:api",
            conversation_id="win-a",
            message="ok",
            extra={"end_turn": True},
        )
    )
    await first
    assert len(_kind_pins(short_term, "system")) == 1
    assert len(_kind_pins(short_term, "tools")) == 1
    assert len(_pointer_pins(short_term)) == 1
    await channel.send(
        CommunicateRequest(
            participant_id="openai:api",
            conversation_id="win-b",
            message="done",
            extra={"end_turn": True},
        )
    )
    await second
    assert short_term.pinned_items == []


@pytest.mark.asyncio
async def test_releasing_one_client_keeps_shared_pin(tmp_path) -> None:
    short_term = ShortTermMemory(tree_enabled=False)
    channel = OpenAIChannel(
        extras=ExtraTokenStore(),
        attachments_dir=tmp_path / "attachments",
    )
    channel.set_short_term(short_term)
    channel.publish_inbound = AsyncMock()
    catalog = {"ping": {"name": "ping"}}
    first = asyncio.create_task(
        channel.open_user_turn(
            participant_id="openai:api",
            conversation_id="win",
            user_text="one",
            system_text="Be brief.",
            catalog=catalog,
        )
    )
    await asyncio.sleep(0)
    second = asyncio.create_task(
        channel.open_user_turn(
            participant_id="openai:cursor",
            conversation_id="win",
            user_text="two",
            system_text="Be brief.",
            catalog=catalog,
        )
    )
    await asyncio.sleep(0)
    assert len(_kind_pins(short_term, "system")) == 1
    assert len(_kind_pins(short_term, "tools")) == 1
    assert len(_pointer_pins(short_term)) == 2
    await channel.send(
        CommunicateRequest(
            participant_id="openai:api",
            conversation_id="win",
            message="ok",
            extra={"end_turn": True},
        )
    )
    await first
    assert len(_kind_pins(short_term, "system")) == 1
    assert len(_kind_pins(short_term, "tools")) == 1
    assert len(_pointer_pins(short_term)) == 1
    await channel.send(
        CommunicateRequest(
            participant_id="openai:cursor",
            conversation_id="win",
            message="done",
            extra={"end_turn": True},
        )
    )
    await second
    assert short_term.pinned_items == []


@pytest.mark.asyncio
async def test_next_turn_reuses_content_addressed_pin_id(tmp_path) -> None:
    short_term = ShortTermMemory(tree_enabled=False)
    channel = OpenAIChannel(
        extras=ExtraTokenStore(),
        attachments_dir=tmp_path / "attachments",
    )
    channel.set_short_term(short_term)
    channel.publish_inbound = AsyncMock()
    catalog = {"ping": {"name": "ping"}}
    first = asyncio.create_task(
        channel.open_user_turn(
            participant_id="openai:api",
            conversation_id="win",
            user_text="one",
            system_text="Be brief.",
            catalog=catalog,
        )
    )
    await asyncio.sleep(0)
    system_id = _kind_pins(short_term, "system")[0].pin_id
    tools_id = _kind_pins(short_term, "tools")[0].pin_id
    pointer_id = _pointer_pins(short_term)[0].pin_id
    await channel.send(
        CommunicateRequest(
            participant_id="openai:api",
            conversation_id="win",
            message="ok",
            extra={"end_turn": True},
        )
    )
    await first
    second = asyncio.create_task(
        channel.open_user_turn(
            participant_id="openai:api",
            conversation_id="win",
            user_text="two",
            system_text="Be brief.",
            catalog=catalog,
        )
    )
    await asyncio.sleep(0)
    assert _kind_pins(short_term, "system")[0].pin_id == system_id
    assert _kind_pins(short_term, "tools")[0].pin_id == tools_id
    assert _pointer_pins(short_term)[0].pin_id == pointer_id
    await channel.send(
        CommunicateRequest(
            participant_id="openai:api",
            conversation_id="win",
            message="done",
            extra={"end_turn": True},
        )
    )
    await second


@pytest.mark.asyncio
async def test_folded_tools_still_dispatch_via_call_client_tool(tmp_path) -> None:
    short_term = ShortTermMemory(tree_enabled=False)
    channel = OpenAIChannel(
        extras=ExtraTokenStore(),
        attachments_dir=tmp_path / "attachments",
    )
    channel.set_short_term(short_term)
    channel.publish_inbound = AsyncMock()
    catalog = {
        "alpha": {
            "name": "alpha",
            "description": "A" * 1600,
            "parameters": {"type": "object"},
        }
    }
    with locale_context("en"):
        task = asyncio.create_task(
            channel.open_user_turn(
                participant_id="openai:api",
                conversation_id="win",
                user_text="hi",
                system_text="",
                catalog=catalog,
            )
        )
        await asyncio.sleep(0)
        event = channel.publish_inbound.await_args.args[0]
        pin = _kind_pins(short_term, "tools")[0]
    assert event.content == "hi"
    assert "alpha" in pin.content
    assert '"description":' not in pin.content
    dispatched = await channel.call_client_tool(
        participant_id="openai:api",
        conversation_id="win",
        name="alpha",
        arguments={"q": 1},
    )
    assert not dispatched.is_error
    completion = await task
    assert completion.kind == "tool_calls"
    assert {call.name for call in completion.tool_calls} == {"alpha"}


@pytest.mark.asyncio
async def test_open_user_turn_publishes_image_attachments(tmp_path) -> None:
    png = (
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQ"
        "AAAABJRU5ErkJggg=="
    )
    channel = OpenAIChannel(
        extras=ExtraTokenStore(),
        attachments_dir=tmp_path / "attachments",
    )
    received: list[IncomingEvent] = []

    async def capture(event: IncomingEvent) -> None:
        received.append(event)

    channel.set_inbound_handler(capture)
    attachments = channel.materialize_user_images(
        [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "what is this"},
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:image/png;base64,{png}"},
                    },
                ],
            }
        ]
    )
    task = asyncio.create_task(
        channel.open_user_turn(
            participant_id="openai:api",
            conversation_id="win",
            user_text="what is this",
            system_text="",
            catalog={},
            attachments=attachments,
        )
    )
    await asyncio.sleep(0)
    assert len(received) == 1
    assert len(received[0].attachments) == 1
    att = received[0].attachments[0]
    assert att.media_type == "image/png"
    assert att.data == png
    assert Path(att.saved_path).read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"
    await channel.send(
        CommunicateRequest(
            participant_id="openai:api",
            conversation_id="win",
            message="a pixel",
            extra={"end_turn": True},
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
            extra={"end_turn": True},
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
            extra={"end_turn": True},
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


@pytest.mark.asyncio
async def test_shared_prefix_window_is_not_blocked_by_sibling_tool_wait() -> None:
    channel = OpenAIChannel(extras=ExtraTokenStore())
    received: list[IncomingEvent] = []

    async def capture(event: IncomingEvent) -> None:
        received.append(event)

    channel.set_inbound_handler(capture)
    prefix = [
        {"role": "system", "content": "You are Cursor."},
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": "hi"},
    ]
    first_id = channel.resolve_implicit_conversation_id(
        "openai:api",
        [*prefix, {"role": "user", "content": "open a.py"}],
    )
    first_turn = asyncio.create_task(
        channel.open_user_turn(
            participant_id="openai:api",
            conversation_id=first_id,
            user_text="open a.py",
            system_text="You are Cursor.",
            catalog={"read_file": {"name": "read_file"}},
        )
    )
    await asyncio.sleep(0)
    await channel.call_client_tool(
        name="read_file",
        arguments={"path": "a.py"},
        participant_id="openai:api",
        conversation_id=first_id,
    )
    first_completion = await first_turn
    assert first_completion.kind == "tool_calls"
    assert channel.sessions().awaiting_tools("openai:api", first_id)

    sibling_messages = [*prefix, {"role": "user", "content": "another chat"}]
    sibling_id = channel.resolve_implicit_conversation_id(
        "openai:api",
        sibling_messages,
    )
    assert sibling_id != first_id
    sibling_turn = asyncio.create_task(
        channel.open_user_turn(
            participant_id="openai:api",
            conversation_id=sibling_id,
            user_text="another chat",
            system_text="You are Cursor.",
            catalog={},
        )
    )
    await asyncio.sleep(0)
    await channel.send(
        CommunicateRequest(
            participant_id="openai:api",
            conversation_id=sibling_id,
            message="ok",
            extra={"end_turn": True},
        )
    )
    sibling_completion = await sibling_turn
    assert sibling_completion.kind == "stop"
    assert sibling_completion.content == "ok"
    assert channel.sessions().awaiting_tools("openai:api", first_id)

    followup_id = channel.resolve_implicit_conversation_id(
        "openai:api",
        [
            *prefix,
            {"role": "user", "content": "open a.py"},
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": first_completion.tool_calls[0].id,
                        "type": "function",
                        "function": {"name": "read_file", "arguments": "{}"},
                    }
                ],
            },
            {
                "role": "tool",
                "tool_call_id": first_completion.tool_calls[0].id,
                "content": "print('hi')",
            },
        ],
    )
    assert followup_id == first_id


@pytest.mark.asyncio
async def test_tool_followup_publishes_image_attachments(tmp_path) -> None:
    png = (
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQ"
        "AAAABJRU5ErkJggg=="
    )
    channel = OpenAIChannel(
        extras=ExtraTokenStore(),
        attachments_dir=tmp_path / "attachments",
    )
    received: list[IncomingEvent] = []

    async def capture(event: IncomingEvent) -> None:
        received.append(event)

    channel.set_inbound_handler(capture)
    task = asyncio.create_task(
        channel.open_user_turn(
            participant_id="openai:api",
            conversation_id="win",
            user_text="load the screenshot",
            system_text="",
            catalog={"read_image": {"name": "read_image"}},
        )
    )
    await asyncio.sleep(0)
    dispatched = await channel.call_client_tool(
        name="read_image",
        arguments={"path": "shot.png"},
        participant_id="openai:api",
        conversation_id="win",
    )
    assert not dispatched.is_error
    completion = await task
    call_id = completion.tool_calls[0].id
    attachments = channel.materialize_image_dicts(
        last_user_image_attachments(
            [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image_url",
                            "image_url": {"url": f"data:image/png;base64,{png}"},
                        }
                    ],
                }
            ]
        )
    )
    followup = asyncio.create_task(
        channel.open_tool_followup(
            participant_id="openai:api",
            conversation_id="win",
            results={call_id: ""},
            attachments=attachments,
        )
    )
    await asyncio.sleep(0)
    inbound = received[-1]
    assert inbound.attachments
    assert inbound.attachments[0].media_type == "image/png"
    assert inbound.attachments[0].data == png
    await channel.send(
        CommunicateRequest(
            participant_id="openai:api",
            conversation_id="win",
            message="a pixel",
            extra={"end_turn": True},
        )
    )
    followup_completion = await followup
    assert followup_completion.kind == "stop"
