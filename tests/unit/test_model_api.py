from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi import status

from coworker.api import app as api_app
from coworker.api.model_api import setup_model_api
from coworker.channels.modelapi import (
    ConversationRegistry,
    ModelApiChannel,
    ModelApiTokenDirectory,
    TurnItem,
    TurnRegistry,
    content_text,
    message_fingerprint,
)
from coworker.core.config import ModelApiTokenConfig
from coworker.core.types import CommunicateRequest, IncomingEvent

_TOKEN = "sk-test-token-123456"
_AUTH = {"Authorization": f"Bearer {_TOKEN}"}


def _fingerprints(history: list[dict[str, str]]) -> list[str]:
    return [
        message_fingerprint(entry["role"], entry["content"]) for entry in history
    ]


def _history(*entries: tuple[str, str]) -> list[dict[str, str]]:
    return [{"role": role, "content": content} for role, content in entries]


# ---------------------------------------------------------------------------
# sessions: fingerprints and conversation stitching


def test_content_text_flattens_multimodal_blocks() -> None:
    content: list[Any] = [
        {"type": "text", "text": "hello"},
        {"type": "image_url", "image_url": {"url": "data:..."}},
        {"type": "text", "text": "world"},
    ]
    assert content_text(content) == "hello\nworld"
    assert content_text("plain") == "plain"


def test_conversation_match_creates_then_extends(tmp_path: Path) -> None:
    registry = ConversationRegistry(tmp_path / "conversations.json")
    first = _history(("user", "hi"))
    conversation_id, matched = registry.match("api:a", _fingerprints(first))
    assert matched == 0

    second = _history(("user", "hi"), ("assistant", "hello"), ("user", "how are you"))
    conversation_id_2, matched_2 = registry.match("api:b", _fingerprints(second))
    assert conversation_id_2 != conversation_id

    extended = _history(("user", "hi"), ("assistant", "hello"), ("user", "how are you"), ("assistant", "fine"))
    conversation_id_3, matched_3 = registry.match("api:b", _fingerprints(extended))
    assert conversation_id_3 == conversation_id_2
    assert matched_3 == 3


def test_conversation_match_survives_trimmed_history(tmp_path: Path) -> None:
    registry = ConversationRegistry(tmp_path / "conversations.json")
    full = _history(("user", "a"), ("assistant", "b"), ("user", "c"), ("assistant", "d"))
    conversation_id, _ = registry.match("api:a", _fingerprints(full))
    trimmed = full[-2:]
    conversation_id_2, matched = registry.match("api:a", _fingerprints(trimmed))
    assert conversation_id_2 == conversation_id
    assert matched == 2


def test_conversation_match_survives_trim_then_continue(tmp_path: Path) -> None:
    registry = ConversationRegistry(tmp_path / "conversations.json")
    full = _history(("user", "a"), ("assistant", "b"), ("user", "c"), ("assistant", "d"))
    conversation_id, _ = registry.match("api:a", _fingerprints(full))
    # The client dropped old messages and added a new one.
    continued = _history(("user", "c"), ("assistant", "d"), ("user", "e"))
    conversation_id_2, matched = registry.match("api:a", _fingerprints(continued))
    assert conversation_id_2 == conversation_id
    assert matched == 2
    # And a follow-up on the new canonical history keeps matching.
    follow_up = _history(("user", "c"), ("assistant", "d"), ("user", "e"), ("assistant", "f"))
    conversation_id_3, matched_3 = registry.match("api:a", _fingerprints(follow_up))
    assert conversation_id_3 == conversation_id
    assert matched_3 == 3


def test_conversation_divergence_starts_new_conversation(tmp_path: Path) -> None:
    registry = ConversationRegistry(tmp_path / "conversations.json")
    first = _history(("user", "tell me about cats"), ("assistant", "cats are nice"))
    conversation_id, _ = registry.match("api:a", _fingerprints(first))
    divergent = _history(("user", "tell me about cats"), ("assistant", "dogs are nice"))
    conversation_id_2, _ = registry.match("api:a", _fingerprints(divergent))
    assert conversation_id_2 != conversation_id


def test_conversation_registry_persists(tmp_path: Path) -> None:
    path = tmp_path / "conversations.json"
    registry = ConversationRegistry(path)
    history = _history(("user", "persist me"))
    conversation_id, _ = registry.match("api:a", _fingerprints(history))
    assert path.is_file()

    reloaded = ConversationRegistry(path)
    conversation_id_2, _ = reloaded.match("api:a", _fingerprints(history))
    assert conversation_id_2 == conversation_id


def test_scenario_changed_tracks_hash(tmp_path: Path) -> None:
    registry = ConversationRegistry(tmp_path / "conversations.json")
    conversation_id, _ = registry.match("api:a", _fingerprints(_history(("user", "hi"))))
    assert registry.scenario_changed("api:a", conversation_id, "h1") is True
    assert registry.scenario_changed("api:a", conversation_id, "h1") is False
    assert registry.scenario_changed("api:a", conversation_id, "h2") is True


def test_token_directory_resolves_bearer_and_rejects_duplicates() -> None:
    directory = ModelApiTokenDirectory(
        [ModelApiTokenConfig(token=_TOKEN, display_name="Alice")]
    )
    identity = directory.resolve_authorization(f"Bearer {_TOKEN}")
    assert identity is not None
    assert identity.participant_id == "api:alice"
    assert directory.resolve_authorization("Bearer wrong-token-value") is None
    assert directory.resolve_authorization(None) is None

    with pytest.raises(ValueError):
        ModelApiTokenDirectory(
            [
                ModelApiTokenConfig(token="sk-first-token-123456", display_name="Dup"),
                ModelApiTokenConfig(token="sk-second-token-12345", display_name="dup"),
            ]
        )


def test_token_directory_falls_back_to_token_hash() -> None:
    directory = ModelApiTokenDirectory([ModelApiTokenConfig(token=_TOKEN)])
    identity = directory.resolve_authorization(f"Bearer {_TOKEN}")
    assert identity is not None
    assert identity.participant_id.startswith("api:")
    assert identity.participant_id != "api:"


# ---------------------------------------------------------------------------
# turns


async def test_turn_publish_fans_out_and_close_ends_all() -> None:
    turns = TurnRegistry()
    turn = turns.open_or_get("api:a", "conv_1")
    first = turn.attach()
    second = turn.attach()

    turn.publish(TurnItem(kind="message", text="hello"))
    assert first.qsize() == 1
    assert second.qsize() == 1
    assert turn.texts == ["hello"]

    turns.close(turn, "end_turn")
    assert turn.closed
    message_item = first.get_nowait()
    assert message_item.kind == "message"
    close_item = first.get_nowait()
    assert close_item.kind == "close"
    assert close_item.end_reason == "end_turn"
    assert second.get_nowait().kind == "message"
    assert second.get_nowait().kind == "close"

    assert turns.get("api:a", "conv_1") is None


async def test_turn_registry_single_open_turn_lookup() -> None:
    turns = TurnRegistry()
    assert turns.get("api:a", None) is None
    turn = turns.open_or_get("api:a", "conv_1")
    assert turns.get("api:a", None) is turn
    other = turns.open_or_get("api:a", "conv_2")
    assert turns.get("api:a", None) is None
    turns.close(other, "end_turn")
    assert turns.get("api:a", None) is turn


async def test_watchdog_nudges_then_closes_idle_turn() -> None:
    turns = TurnRegistry(nudge_seconds=0.05, timeout_seconds=0.15)
    nudges: list[str] = []
    timeouts: list[str] = []

    async def on_nudge(turn) -> None:
        nudges.append(turn.conversation_id)

    async def on_timeout(turn) -> None:
        timeouts.append(turn.conversation_id)

    turns.on_nudge = on_nudge
    turns.on_timeout = on_timeout
    turn = turns.open_or_get("api:a", "conv_1")
    queue = turn.attach()

    task = asyncio.create_task(turns.run_watchdog(interval=0.02))
    try:
        await asyncio.sleep(0.1)
        assert nudges == ["conv_1"]
        assert turn.closed is False
        await asyncio.sleep(0.2)
        assert turn.closed is True
        assert timeouts == ["conv_1"]
        item = queue.get_nowait()
        assert item.kind == "close"
        assert item.end_reason == "timeout"
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


# ---------------------------------------------------------------------------
# channel


def _model_api_system(tmp_path: Path) -> tuple[ModelApiChannel, list[IncomingEvent]]:
    turns = TurnRegistry()
    channel = ModelApiChannel(turns)
    events: list[IncomingEvent] = []

    async def handler(event: IncomingEvent) -> None:
        events.append(event)

    channel.set_inbound_handler(handler)
    return channel, events


async def test_channel_send_streams_and_end_turn_closes(tmp_path: Path) -> None:
    channel, _ = _model_api_system(tmp_path)
    turn = channel.turns.open_or_get("api:a", "conv_1")
    queue = turn.attach()

    result = await channel.send(
        CommunicateRequest(participant_id="api:a", message="working on it")
    )
    assert not result.is_error
    assert turn.closed is False

    result = await channel.send(
        CommunicateRequest(
            participant_id="api:a",
            message="all done",
            extra={"end_turn": True},
        )
    )
    assert not result.is_error
    assert turn.closed is True
    assert turn.end_reason == "end_turn"

    first = queue.get_nowait()
    assert first.text == "working on it"
    second = queue.get_nowait()
    assert second.text == "all done"
    close_item = queue.get_nowait()
    assert close_item.end_reason == "end_turn"


async def test_channel_send_tool_calls_closes_turn(tmp_path: Path) -> None:
    channel, _ = _model_api_system(tmp_path)
    turn = channel.turns.open_or_get("api:a", "conv_1")
    queue = turn.attach()
    tool_calls = [{"id": "call_1", "type": "function", "function": {"name": "get_weather", "arguments": "{}"}}]

    result = await channel.send(
        CommunicateRequest(
            participant_id="api:a",
            extra={"tool_calls": tool_calls},
        )
    )
    assert not result.is_error
    assert turn.closed is True
    assert turn.end_reason == "tool_calls"
    item = queue.get_nowait()
    assert item.tool_calls == tool_calls


async def test_channel_send_without_open_turn_is_not_delivered(tmp_path: Path) -> None:
    channel, _ = _model_api_system(tmp_path)
    result = await channel.send(
        CommunicateRequest(participant_id="api:a", message="anyone there?")
    )
    assert not result.is_error
    assert "api:a" in result.content


async def test_channel_send_invalid_tool_calls_is_error(tmp_path: Path) -> None:
    channel, _ = _model_api_system(tmp_path)
    channel.turns.open_or_get("api:a", "conv_1")
    result = await channel.send(
        CommunicateRequest(
            participant_id="api:a",
            extra={"tool_calls": [{"function": {"arguments": "{}"}}]},
        )
    )
    assert result.is_error


async def test_channel_nudge_and_timeout_publish_system_events(tmp_path: Path) -> None:
    channel, events = _model_api_system(tmp_path)
    turn = channel.turns.open_or_get("api:a", "conv_1")
    await channel.nudge_turn(turn)
    await channel.timeout_turn(turn)
    assert [event.source for event in events] == ["system", "system"]
    assert "conv_1" in events[0].content


# ---------------------------------------------------------------------------
# HTTP endpoints


class _EndpointHarness:
    def __init__(self, tmp_path: Path, *, enabled: bool = True) -> None:
        self.events: list[IncomingEvent] = []
        self.channel: ModelApiChannel | None = None
        directory = None
        if enabled:
            turns = TurnRegistry()
            self.channel = ModelApiChannel(turns)

            async def handler(event: IncomingEvent) -> None:
                self.events.append(event)

            self.channel.set_inbound_handler(handler)
            directory = ModelApiTokenDirectory(
                [ModelApiTokenConfig(token=_TOKEN, display_name="Alice")]
            )
        setup_model_api(
            channel=self.channel,
            directory=directory,
            conversations=ConversationRegistry(tmp_path / "conversations.json"),
        )

    def cleanup(self) -> None:
        setup_model_api(channel=None, directory=None, conversations=ConversationRegistry(None))

    async def wait_for_events(self, count: int) -> None:
        for _ in range(500):
            if len(self.events) >= count:
                return
            await asyncio.sleep(0.01)
        raise AssertionError(f"expected {count} inbound events, saw {len(self.events)}")


@pytest.fixture
def harness(tmp_path: Path):
    harness = _EndpointHarness(tmp_path)
    yield harness
    harness.cleanup()


@pytest.fixture
def disabled_harness(tmp_path: Path):
    harness = _EndpointHarness(tmp_path, enabled=False)
    yield harness
    harness.cleanup()


async def _post(client: httpx.AsyncClient, payload: dict[str, Any], headers: dict[str, str] | None = _AUTH):
    return await client.post("/v1/chat/completions", json=payload, headers=headers)


async def test_models_requires_valid_token(harness: _EndpointHarness) -> None:
    transport = httpx.ASGITransport(app=api_app.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        denied = await client.get("/v1/models", headers={"Authorization": "Bearer nope"})
        assert denied.status_code == status.HTTP_401_UNAUTHORIZED
        allowed = await client.get("/v1/models", headers=_AUTH)
        assert allowed.status_code == status.HTTP_200_OK
        assert allowed.json()["data"][0]["id"] == "coworker"


async def test_completions_disabled_returns_503(disabled_harness: _EndpointHarness) -> None:
    transport = httpx.ASGITransport(app=api_app.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await _post(
            client, {"messages": [{"role": "user", "content": "hi"}]}
        )
        assert response.status_code == status.HTTP_503_SERVICE_UNAVAILABLE


async def test_non_stream_completion_collects_whole_turn(
    harness: _EndpointHarness,
) -> None:
    assert harness.channel is not None
    transport = httpx.ASGITransport(app=api_app.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        pending = asyncio.create_task(
            _post(
                client,
                {
                    "messages": [
                        {"role": "system", "content": "You are a helpful tester."},
                        {"role": "user", "content": "compute 2+2"},
                    ]
                },
            )
        )
        await harness.wait_for_events(1)
        participant = harness.events[0].participant_id
        conversation_id = harness.events[0].conversation_id
        assert participant == "api:alice"
        assert harness.events[0].source == "model_api"
        assert conversation_id

        await harness.channel.send(
            CommunicateRequest(
                participant_id=participant,
                conversation_id=conversation_id,
                message="thinking",
            )
        )
        await harness.channel.send(
            CommunicateRequest(
                participant_id=participant,
                conversation_id=conversation_id,
                message="the answer is 4",
                extra={"end_turn": True},
            )
        )
        response = await pending

    assert response.status_code == status.HTTP_200_OK
    body = response.json()
    assert body["choices"][0]["finish_reason"] == "stop"
    assert body["choices"][0]["message"]["content"] == "thinking\n\nthe answer is 4"
    assert body["coworker_end_reason"] == "end_turn"
    assert body["usage"]["completion_tokens"] > 0


async def test_stream_completion_emits_chunks_and_done(harness: _EndpointHarness) -> None:
    assert harness.channel is not None
    transport = httpx.ASGITransport(app=api_app.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        pending = asyncio.create_task(
            _post(
                client,
                {"messages": [{"role": "user", "content": "hi"}], "stream": True},
            )
        )
        await harness.wait_for_events(1)
        participant = harness.events[0].participant_id
        conversation_id = harness.events[0].conversation_id
        await harness.channel.send(
            CommunicateRequest(
                participant_id=participant,
                conversation_id=conversation_id,
                message="hello there",
                extra={"end_turn": True},
            )
        )
        response = await pending

    assert response.status_code == status.HTTP_200_OK
    body = response.text
    assert body.endswith("data: [DONE]\n\n")
    chunks = [
        json.loads(line[len("data: ") :])
        for line in body.splitlines()
        if line.startswith("data: ") and line != "data: [DONE]"
    ]
    assert chunks[0]["choices"][0]["delta"] == {"role": "assistant", "content": ""}
    assert chunks[1]["choices"][0]["delta"] == {"content": "hello there"}
    final = chunks[-1]
    assert final["choices"][0]["finish_reason"] == "stop"
    assert final["coworker_end_reason"] == "end_turn"


async def test_followup_request_joins_open_turn(harness: _EndpointHarness) -> None:
    assert harness.channel is not None
    transport = httpx.ASGITransport(app=api_app.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        first = asyncio.create_task(
            _post(client, {"messages": [{"role": "user", "content": "start a task"}]})
        )
        await harness.wait_for_events(1)
        participant = harness.events[0].participant_id
        conversation_id = harness.events[0].conversation_id

        second = asyncio.create_task(
            _post(
                client,
                {
                    "messages": [
                        {"role": "user", "content": "start a task"},
                        {"role": "user", "content": "also send a report"},
                    ]
                },
            )
        )
        await harness.wait_for_events(2)
        assert harness.events[1].conversation_id == conversation_id
        assert "also send a report" in harness.events[1].content

        await harness.channel.send(
            CommunicateRequest(
                participant_id=participant,
                conversation_id=conversation_id,
                message="both done",
                extra={"end_turn": True},
            )
        )
        first_response = await first
        second_response = await second

    assert first_response.status_code == status.HTTP_200_OK
    assert second_response.status_code == status.HTTP_200_OK
    assert first_response.json()["choices"][0]["message"]["content"] == "both done"
    assert second_response.json()["choices"][0]["message"]["content"] == "both done"


async def test_tool_calls_reply_returns_openai_tool_calls(harness: _EndpointHarness) -> None:
    assert harness.channel is not None
    tools = [
        {
            "type": "function",
            "function": {
                "name": "get_weather",
                "parameters": {"type": "object", "properties": {}},
            },
        }
    ]
    transport = httpx.ASGITransport(app=api_app.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        pending = asyncio.create_task(
            _post(
                client,
                {"messages": [{"role": "user", "content": "weather?"}], "tools": tools},
            )
        )
        await harness.wait_for_events(1)
        assert "get_weather" in harness.events[0].content
        participant = harness.events[0].participant_id
        conversation_id = harness.events[0].conversation_id
        tool_calls = [
            {
                "id": "call_1",
                "type": "function",
                "function": {"name": "get_weather", "arguments": "{\"city\":\"Paris\"}"},
            }
        ]
        await harness.channel.send(
            CommunicateRequest(
                participant_id=participant,
                conversation_id=conversation_id,
                extra={"tool_calls": tool_calls},
            )
        )
        response = await pending

    body = response.json()
    assert body["choices"][0]["finish_reason"] == "tool_calls"
    assert body["choices"][0]["message"]["tool_calls"] == tool_calls
    assert body["coworker_end_reason"] == "tool_calls"
