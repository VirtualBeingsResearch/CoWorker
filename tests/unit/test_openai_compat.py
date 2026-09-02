from __future__ import annotations

from unittest.mock import AsyncMock

from fastapi.testclient import TestClient

from coworker.api import app as api_app
from coworker.api.openai_compat import setup_openai_channel
from coworker.api.routes import setup as setup_routes
from coworker.channels.openai.channel import OpenAIChannel
from coworker.channels.openai.tokens import ExtraTokenStore
from coworker.channels.openai.waiters import BusyError, ClientToolCall, OpenAICompletion
from coworker.core.types import AgentState
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


def test_v1_stream_emits_whole_completion(monkeypatch) -> None:
    client, _ = _client(monkeypatch)
    response = client.post(
        "/v1/chat/completions",
        headers={"Authorization": "Bearer primary-token"},
        json={
            "stream": True,
            "messages": [{"role": "user", "content": "hello"}],
        },
    )
    assert response.status_code == 200
    assert "text/event-stream" in response.headers["content-type"]
    body = response.text
    assert "hello from coworker" in body
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


def test_extra_token_authenticates_messages_without_changing_sender(monkeypatch) -> None:
    _client(monkeypatch, extras={"cursor": "cursor-secret"})
    import coworker.api.routes as routes_mod

    assert routes_mod.resolve_communication_token_name("Bearer cursor-secret") == "cursor"
    assert routes_mod.openai_participant_id("Bearer cursor-secret") == "openai:cursor"
    assert routes_mod.resolve_communication_token_name("Bearer primary-token") == "api"

