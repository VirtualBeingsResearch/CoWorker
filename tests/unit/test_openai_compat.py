from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from coworker.api import app as api_app
from coworker.api.openai_compat import (
    _followup_results,
    _is_image_user_followup,
    setup_openai_channel,
)
from coworker.api.routes import setup as setup_routes
from coworker.channels.openai.channel import OpenAIChannel
from coworker.channels.openai.tokens import ExtraTokenStore
from coworker.channels.openai.waiters import (
    BusyError,
    ClientToolCall,
    OpenAICompletion,
    OpenAITurn,
)
from coworker.core.types import AgentState, CommunicateRequest
from coworker.i18n import locale_context


def _client(monkeypatch, extras: dict[str, str] | None = None) -> TestClient:
    import coworker.api.routes as routes_mod

    routes_mod._communication_token = ""
    routes_mod._communication_token_explicit = False
    routes_mod._extra_communication_tokens = {}
    setup_openai_channel(None)
    api_app.set_setup_required(False)
    brain = type("Brain", (), {"state": AgentState()})()
    setup_routes(
        None,
        AsyncMock(),
        brain,
        communication_token="primary-token",
        communication_token_explicit=True,
        extra_communication_tokens=extras or {},
    )
    channel = OpenAIChannel(extras=ExtraTokenStore(extras or {}))
    channel.open_user_turn = AsyncMock(
        return_value=OpenAICompletion(kind="stop", content="hello from coworker")
    )
    channel.open_tool_followup = AsyncMock(
        return_value=OpenAICompletion(kind="stop", content="tool done")
    )
    setup_openai_channel(channel)
    monkeypatch.setattr(api_app, "_shutting_down", False)
    return TestClient(api_app.app), channel


def test_v1_models_maps_extra_token_and_rejects_unknown(monkeypatch) -> None:
    client, _ = _client(monkeypatch, extras={"cursor": "cursor-secret"})
    with locale_context("en"):
        denied = client.get("/v1/models")
        assert denied.status_code == 401
        assert denied.json()["error"]["code"] == "invalid_api_key"
        primary = client.get(
            "/v1/models",
            headers={"Authorization": "Bearer primary-token"},
        )
        extra = client.get(
            "/v1/models",
            headers={"Authorization": "Bearer cursor-secret"},
        )
    assert primary.status_code == 200
    assert extra.status_code == 200
    assert primary.json()["data"][0]["id"] == "coworker"


def test_v1_chat_uses_token_as_participant_and_fingerprint(monkeypatch) -> None:
    client, channel = _client(monkeypatch, extras={"cursor": "cursor-secret"})
    response = client.post(
        "/v1/chat/completions",
        headers={"Authorization": "Bearer cursor-secret"},
        json={
            "model": "coworker",
            "messages": [
                {"role": "system", "content": "sys"},
                {"role": "user", "content": "hello"},
            ],
        },
    )
    assert response.status_code == 200
    assert response.json()["choices"][0]["message"]["content"] == "hello from coworker"
    kwargs = channel.open_user_turn.await_args.kwargs
    assert kwargs["participant_id"] == "openai:cursor"
    assert kwargs["user_text"] == "hello"
    headered = client.post(
        "/v1/chat/completions",
        headers={
            "Authorization": "Bearer cursor-secret",
            "X-Coworker-Conversation-Id": "composer-1",
        },
        json={"messages": [{"role": "user", "content": "again"}]},
    )
    assert headered.status_code == 200
    assert channel.open_user_turn.await_args.kwargs["conversation_id"] == "composer-1"


def test_v1_chat_keeps_every_opening_user_message(monkeypatch) -> None:
    client, channel = _client(monkeypatch)
    response = client.post(
        "/v1/chat/completions",
        headers={"Authorization": "Bearer primary-token"},
        json={
            "messages": [
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
        },
    )
    assert response.status_code == 200
    kwargs = channel.open_user_turn.await_args.kwargs
    assert kwargs["user_text"] == (
        "<system-reminder>\nAvailable skills.\n</system-reminder>\n\n"
        "<system-reminder>\nToday's date is 2026-09-03.\n</system-reminder>\n\n"
        "你知道今天是星期几吗？"
    )
    assert kwargs["system_text"] == "You are ZCode"
    later = client.post(
        "/v1/chat/completions",
        headers={"Authorization": "Bearer primary-token"},
        json={
            "messages": [
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
                {"role": "assistant", "content": "星期四"},
                {"role": "user", "content": "<system-reminder>\nStill Thursday.\n</system-reminder>"},
                {"role": "user", "content": "那现在几点？"},
            ]
        },
    )
    assert later.status_code == 200
    later_kwargs = channel.open_user_turn.await_args.kwargs
    assert later_kwargs["user_text"] == (
        "<system-reminder>\nStill Thursday.\n</system-reminder>\n\n那现在几点？"
    )


def test_v1_setup_required_is_json_503(monkeypatch) -> None:
    _client(monkeypatch)
    api_app.set_setup_required(True)
    try:
        with locale_context("en"):
            response = TestClient(api_app.app).get(
                "/v1/models",
                headers={"Authorization": "Bearer primary-token"},
            )
        assert response.status_code == 503
        assert response.json()["error"]["code"] == "setup_required"
        assert "html" not in response.headers.get("content-type", "").lower()
    finally:
        api_app.set_setup_required(False)


def test_v1_stream_emits_delta_chunks(monkeypatch) -> None:
    import asyncio

    import httpx

    import coworker.api.routes as routes_mod

    routes_mod._communication_token = ""
    routes_mod._communication_token_explicit = False
    routes_mod._extra_communication_tokens = {}
    setup_openai_channel(None)
    api_app.set_setup_required(False)
    brain = type("Brain", (), {"state": AgentState()})()
    setup_routes(
        None,
        AsyncMock(),
        brain,
        communication_token="primary-token",
        communication_token_explicit=True,
        extra_communication_tokens={},
    )
    channel = OpenAIChannel(extras=ExtraTokenStore(), timeout_seconds=5)
    channel.publish_inbound = AsyncMock()
    setup_openai_channel(channel)
    monkeypatch.setattr(api_app, "_shutting_down", False)

    async def _run() -> str:
        transport = httpx.ASGITransport(app=api_app.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:

            async def producer() -> None:
                for _ in range(100):
                    turns = channel.sessions().in_flight_for("openai:api")
                    if turns:
                        break
                    await asyncio.sleep(0.01)
                else:
                    raise AssertionError("turn did not start")
                turn = turns[0]
                await channel.send(
                    CommunicateRequest(
                        participant_id="openai:api",
                        conversation_id=turn.conversation_id,
                        message="alpha",
                    )
                )
                await channel.send(
                    CommunicateRequest(
                        participant_id="openai:api",
                        conversation_id=turn.conversation_id,
                        message="beta",
                        extra={"end_turn": True},
                    )
                )

            task = asyncio.create_task(producer())
            async with client.stream(
                "POST",
                "/v1/chat/completions",
                headers={"Authorization": "Bearer primary-token"},
                json={
                    "stream": True,
                    "messages": [{"role": "user", "content": "hello"}],
                },
            ) as response:
                assert response.status_code == 200
                assert "text/event-stream" in response.headers["content-type"]
                body = "".join([part async for part in response.aiter_text()])
            await task
            return body

    body = asyncio.run(_run())
    assert "chat.completion.chunk" in body
    assert "alpha" in body
    assert "beta" in body
    assert "finish_reason" in body
    assert "data: [DONE]" in body


def test_revoke_stops_extra_token(monkeypatch) -> None:
    client, _ = _client(monkeypatch, extras={"cursor": "cursor-secret"})
    import coworker.api.routes as routes_mod

    routes_mod.update_communication_token_table({})
    denied = client.get(
        "/v1/models",
        headers={"Authorization": "Bearer cursor-secret"},
    )
    assert denied.status_code == 401
    still_primary = client.get(
        "/v1/models",
        headers={"Authorization": "Bearer primary-token"},
    )
    assert still_primary.status_code == 200


def test_v1_overlapping_turn_is_409(monkeypatch) -> None:
    client, channel = _client(monkeypatch)
    channel.open_user_turn = AsyncMock(side_effect=BusyError("turn"))
    with locale_context("en"):
        response = client.post(
            "/v1/chat/completions",
            headers={"Authorization": "Bearer primary-token"},
            json={"messages": [{"role": "user", "content": "hello"}]},
        )
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "conflict"


def test_v1_timeout_returns_timeout_text(monkeypatch) -> None:
    client, channel = _client(monkeypatch)
    channel.open_user_turn = AsyncMock(
        return_value=OpenAICompletion(kind="stop", content="", timed_out=True)
    )
    with locale_context("en"):
        response = client.post(
            "/v1/chat/completions",
            headers={"Authorization": "Bearer primary-token"},
            json={"messages": [{"role": "user", "content": "hello"}]},
        )
    assert response.status_code == 200
    body = response.json()["choices"][0]["message"]["content"]
    assert "timed out" in body.lower()


def test_v1_tool_calls_keep_client_function_name(monkeypatch) -> None:
    client, channel = _client(monkeypatch)
    channel.open_user_turn = AsyncMock(
        return_value=OpenAICompletion(
            kind="tool_calls",
            tool_calls=(
                ClientToolCall(
                    id="call_1",
                    name="read_file",
                    arguments='{"path":"a.py"}',
                ),
            ),
        )
    )
    response = client.post(
        "/v1/chat/completions",
        headers={"Authorization": "Bearer primary-token"},
        json={"messages": [{"role": "user", "content": "open a.py"}]},
    )
    assert response.status_code == 200
    message = response.json()["choices"][0]["message"]
    assert message["content"] is None
    assert message["tool_calls"][0]["function"]["name"] == "read_file"
    assert response.json()["choices"][0]["finish_reason"] == "tool_calls"


def test_v1_tool_followup_uses_only_trailing_tool_messages(monkeypatch) -> None:
    client, channel = _client(monkeypatch)
    response = client.post(
        "/v1/chat/completions",
        headers={"Authorization": "Bearer primary-token"},
        json={
            "messages": [
                {"role": "user", "content": "hello"},
                {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "call_old",
                            "type": "function",
                            "function": {"name": "read_file", "arguments": "{}"},
                        }
                    ],
                },
                {"role": "tool", "tool_call_id": "call_old", "content": "old"},
                {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "call_new",
                            "type": "function",
                            "function": {"name": "read_file", "arguments": "{}"},
                        }
                    ],
                },
                {"role": "tool", "tool_call_id": "call_new", "content": "new"},
            ]
        },
    )
    assert response.status_code == 200
    kwargs = channel.open_tool_followup.await_args.kwargs
    assert kwargs["results"] == {"call_new": "new"}


def test_v1_tool_followup_passes_consecutive_trailing_tools(monkeypatch) -> None:
    client, channel = _client(monkeypatch)
    response = client.post(
        "/v1/chat/completions",
        headers={"Authorization": "Bearer primary-token"},
        json={
            "messages": [
                {"role": "user", "content": "hello"},
                {"role": "tool", "tool_call_id": "call_old", "content": "old"},
                {"role": "tool", "tool_call_id": "call_new", "content": "new"},
            ]
        },
    )
    assert response.status_code == 200
    kwargs = channel.open_tool_followup.await_args.kwargs
    assert kwargs["results"] == {"call_old": "old", "call_new": "new"}


def test_v1_tool_followup_passes_image_parts(monkeypatch, tmp_path) -> None:
    client, channel = _client(monkeypatch)
    channel._attachments = type(channel._attachments)(tmp_path / "attachments")
    response = client.post(
        "/v1/chat/completions",
        headers={"Authorization": "Bearer primary-token"},
        json={
            "messages": [
                {"role": "user", "content": "look at the screenshot"},
                {
                    "role": "tool",
                    "tool_call_id": "call_img",
                    "content": [
                        {"type": "text", "text": "shot.png"},
                        {
                            "type": "image_url",
                            "image_url": {
                                "url": f"data:image/png;base64,{_PNG_B64}"
                            },
                        },
                    ],
                },
            ]
        },
    )
    assert response.status_code == 200
    kwargs = channel.open_tool_followup.await_args.kwargs
    assert kwargs["results"] == {"call_img": "shot.png"}
    assert len(kwargs["attachments"]) == 1
    assert kwargs["attachments"][0].media_type == "image/png"
    assert kwargs["attachments"][0].data == _PNG_B64


def test_v1_tool_followup_passes_data_url_string(monkeypatch, tmp_path) -> None:
    client, channel = _client(monkeypatch)
    channel._attachments = type(channel._attachments)(tmp_path / "attachments")
    response = client.post(
        "/v1/chat/completions",
        headers={"Authorization": "Bearer primary-token"},
        json={
            "messages": [
                {"role": "user", "content": "look at the screenshot"},
                {
                    "role": "tool",
                    "tool_call_id": "call_img",
                    "content": f"data:image/png;base64,{_PNG_B64}",
                },
            ]
        },
    )
    assert response.status_code == 200
    kwargs = channel.open_tool_followup.await_args.kwargs
    assert kwargs["results"] == {"call_img": ""}
    assert len(kwargs["attachments"]) == 1
    assert kwargs["attachments"][0].data == _PNG_B64


@pytest.mark.asyncio
async def test_image_user_message_is_treated_as_tool_followup() -> None:
    turn = OpenAITurn(
        participant_id="openai:api",
        conversation_id="win",
        catalog={"read_image": {"name": "read_image"}},
        timeout_seconds=5,
    )
    turn.prepare_client_calls(1)
    pending = turn.register_client_call("read_image", {"path": "shot.png"})
    messages = [
        {"role": "user", "content": "load the screenshot"},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": pending.openai_id,
                    "type": "function",
                    "function": {"name": "read_image", "arguments": "{}"},
                }
            ],
        },
        {
            "role": "user",
            "content": [
                {
                    "type": "image_url",
                    "image_url": {"url": f"data:image/png;base64,{_PNG_B64}"},
                }
            ],
        },
    ]
    assert _is_image_user_followup(messages, turn)
    results, images = _followup_results(messages, turn)
    assert results == {pending.openai_id: ""}
    assert len(images) == 1
    assert images[0]["data"] == _PNG_B64


def test_extra_token_authenticates_messages_without_changing_sender(monkeypatch) -> None:
    _client(monkeypatch, extras={"cursor": "cursor-secret"})
    import coworker.api.routes as routes_mod

    assert routes_mod.resolve_communication_token_name("Bearer cursor-secret") == "cursor"
    assert routes_mod.openai_participant_id("Bearer cursor-secret") == "openai:cursor"
    assert routes_mod.resolve_communication_token_name("Bearer primary-token") == "api"


_PNG_B64 = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQ"
    "AAAABJRU5ErkJggg=="
)


def test_v1_chat_accepts_image_url_data_url(monkeypatch, tmp_path) -> None:
    client, channel = _client(monkeypatch)
    channel._attachments = type(channel._attachments)(tmp_path / "attachments")
    response = client.post(
        "/v1/chat/completions",
        headers={"Authorization": "Bearer primary-token"},
        json={
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "what is in the image"},
                        {
                            "type": "image_url",
                            "image_url": {
                                "url": f"data:image/png;base64,{_PNG_B64}"
                            },
                        },
                    ],
                }
            ]
        },
    )
    assert response.status_code == 200
    kwargs = channel.open_user_turn.await_args.kwargs
    assert kwargs["user_text"] == "what is in the image"
    assert len(kwargs["attachments"]) == 1
    assert kwargs["attachments"][0].media_type == "image/png"
    assert kwargs["attachments"][0].data == _PNG_B64


def test_v1_chat_accepts_image_only_user_message(monkeypatch, tmp_path) -> None:
    client, channel = _client(monkeypatch)
    channel._attachments = type(channel._attachments)(tmp_path / "attachments")
    response = client.post(
        "/v1/chat/completions",
        headers={"Authorization": "Bearer primary-token"},
        json={
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image_url",
                            "image_url": {
                                "url": f"data:image/png;base64,{_PNG_B64}"
                            },
                        }
                    ],
                }
            ]
        },
    )
    assert response.status_code == 200
    kwargs = channel.open_user_turn.await_args.kwargs
    assert kwargs["user_text"] == ""
    assert len(kwargs["attachments"]) == 1


def test_v1_chat_rejects_remote_image_url(monkeypatch) -> None:
    client, _ = _client(monkeypatch)
    with locale_context("en"):
        response = client.post(
            "/v1/chat/completions",
            headers={"Authorization": "Bearer primary-token"},
            json={
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "image_url",
                                "image_url": {
                                    "url": "https://example.com/photo.png"
                                },
                            }
                        ],
                    }
                ]
            },
        )
    assert response.status_code == 400
    assert "data:image" in response.json()["error"]["message"]


def test_v1_accepts_authenticated_relay_tunnel(monkeypatch) -> None:
    client, channel = _client(monkeypatch, extras={"cursor": "cursor-secret"})

    async def with_relay(scope, receive, send):
        if scope["type"] == "http":
            scope.setdefault("state", {})["coworker_relay"] = {
                "authenticated_tunnel": True,
                "e2ee": True,
            }
        await api_app.app(scope, receive, send)

    relay_client = TestClient(with_relay)
    models = relay_client.get(
        "/v1/models",
        headers={"Authorization": "Bearer cursor-secret"},
    )
    assert models.status_code == 200
    assert models.json()["data"][0]["id"] == "coworker"
    chat = relay_client.post(
        "/v1/chat/completions",
        headers={"Authorization": "Bearer primary-token"},
        json={"messages": [{"role": "user", "content": "via relay"}]},
    )
    assert chat.status_code == 200
    assert channel.open_user_turn.await_args.kwargs["participant_id"] == "openai:api"


