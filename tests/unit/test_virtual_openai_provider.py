from __future__ import annotations

import asyncio
import json
import socket

import pytest
import uvicorn
from fastapi.testclient import TestClient

from coworker.brain.openai_compatible_provider import OpenAICompatibleProvider
from coworker.core.types import Message
from tests.support.virtual_openai import (
    VIRTUAL_MODEL_ID,
    ScriptRunner,
    VirtualScenario,
    create_virtual_openai_app,
    load_scenario,
    parse_inbound_header,
    snapshot_messages,
)


def _user(content: str) -> Message:
    return Message(role="user", content=content)


def _openai_user(text: str) -> Message:
    return _user(
        f"[from OpenAI-compatible HTTP][openai:api][conversation:win] message:\n{text}"
    )


def test_parse_inbound_header_en_and_zh() -> None:
    participant, conversation = parse_inbound_header(
        "[from OpenAI-compatible HTTP][openai:api][conversation:win] message:\nhello"
    )
    assert participant == "openai:api"
    assert conversation == "win"
    participant, conversation = parse_inbound_header(
        "[来自OpenAI 兼容 HTTP][openai:cursor]的消息:\n你好"
    )
    assert participant == "openai:cursor"
    assert conversation == ""


def test_snapshot_classifies_structured_kinds() -> None:
    user = snapshot_messages([_openai_user("hello coworker")])
    assert user.kind == "user"
    assert user.user_text == "hello coworker"
    tools = snapshot_messages(
        [
            _openai_user(
                "Client tools for this request (call them with call_client_tool; "
                'do not treat these names as native Coworker tools):\n[{"name": "read_file"}]\n\n'
                "open a.py"
            )
        ]
    )
    assert tools.kind == "client_tools"
    assert tools.client_tools == ("read_file",)
    pinned = snapshot_messages(
        [
            Message(
                role="user",
                content=(
                    "[OpenAI client tools [abc]]\n"
                    "Client tools for this request (call them with call_client_tool; "
                    'do not treat these names as native Coworker tools):\n[{"name": "read_file"}]'
                ),
                pin_id="openai-req:tools:abc",
                source="pinned_context",
            ),
            _openai_user("open a.py"),
        ]
    )
    assert pinned.kind == "client_tools"
    assert pinned.client_tools == ("read_file",)
    assert pinned.user_text == "open a.py"
    results = snapshot_messages(
        [
            _openai_user(
                "Client tool results for this window:\ncall_1 (read_file):\nprint('hi')"
            )
        ]
    )
    assert results.kind == "client_results"
    assert results.client_results == (("call_1", "read_file", "print('hi')"),)


def test_echo_fixture_is_openai_request_response() -> None:
    scenario = load_scenario("echo")
    exchange = scenario.exchanges[0]
    assert exchange.request["messages"][0]["role"] == "user"
    assert exchange.response["object"] == "chat.completion"
    assert exchange.response["choices"][0]["finish_reason"] == "tool_calls"
    assert exchange.response["choices"][0]["message"]["tool_calls"][0]["function"]["arguments"] == {
        "extra": {"end_turn": True},
        "message": "echo: {{user_text}}",
    }
    runner = ScriptRunner(scenario)
    response, calls = runner.complete([_openai_user("hello coworker")])
    raw_arguments = response["choices"][0]["message"]["tool_calls"][0]["function"]["arguments"]
    assert response["choices"][0]["message"]["tool_calls"][0]["function"]["name"] == "communicate"
    assert isinstance(raw_arguments, str)
    arguments = json.loads(raw_arguments)
    assert arguments["message"] == "echo: hello coworker"
    assert arguments["participant_id"] == "openai:api"
    assert calls[0].arguments == arguments
    assert runner.trace[0].request["messages"][-1]["role"] == "user"
    assert runner.turn_ids == ["echo_user"]
    runner.complete(
        [
            _openai_user("hello coworker"),
            Message(role="assistant", content=""),
            Message(role="tool", content="sent"),
        ]
    )
    assert runner.turn_ids == ["echo_user", "sleep"]
    assert runner.trace[1].response["choices"][0]["message"]["tool_calls"][0]["function"]["name"] == (
        "sleep"
    )


def test_client_tool_fixture_matches_openai_tools_request() -> None:
    runner = ScriptRunner(load_scenario("client_tool_roundtrip"))
    dispatched = runner.decide(
        [
            _openai_user(
                "Client tools for this request (call them with call_client_tool; "
                'do not treat these names as native Coworker tools):\n[{"name": "read_file"}]\n\n'
                "open a.py"
            )
        ]
    )
    assert dispatched[0].name == "call_client_tool"
    assert dispatched[0].arguments["name"] == "read_file"
    assert dispatched[0].arguments["arguments"] == {"path": "a.py"}
    replied = runner.decide(
        [
            _openai_user(
                "Client tool results for this window:\ncall_1 (read_file):\nprint('hi')"
            )
        ]
    )
    assert replied[0].arguments["message"] == "tool-result: print('hi')"
    assert runner.turn_ids == ["dispatch_client_tool", "reply_after_tools"]
    assert runner.trace[0].request["messages"][-1]["content"].endswith("open a.py")


def test_inline_openai_request_matches_user_content() -> None:
    scenario = VirtualScenario.from_mapping(
        {
            "id": "by_text",
            "exchanges": [
                {
                    "id": "alpha",
                    "request": {"messages": [{"role": "user", "content": "alpha"}]},
                    "response": {
                        "choices": [
                            {
                                "message": {
                                    "role": "assistant",
                                    "tool_calls": [
                                        {
                                            "id": "call_alpha",
                                            "type": "function",
                                            "function": {
                                                "name": "communicate",
                                                "arguments": {"message": "got-alpha"},
                                            },
                                        }
                                    ],
                                }
                            }
                        ]
                    },
                },
                {
                    "id": "beta",
                    "request": {"messages": [{"role": "user", "content": "beta"}]},
                    "response": {
                        "choices": [
                            {
                                "message": {
                                    "role": "assistant",
                                    "tool_calls": [
                                        {
                                            "id": "call_beta",
                                            "type": "function",
                                            "function": {
                                                "name": "communicate",
                                                "arguments": {"message": "got-beta"},
                                            },
                                        }
                                    ],
                                }
                            }
                        ]
                    },
                },
            ],
        }
    )
    runner = ScriptRunner(scenario)
    first = runner.decide([_openai_user("beta task")])
    second = runner.decide([_openai_user("alpha task")])
    assert first[0].arguments["message"] == "got-beta"
    assert second[0].arguments["message"] == "got-alpha"
    assert runner.turn_ids == ["beta", "alpha"]


def test_unknown_scenario_file_lists_available() -> None:
    with pytest.raises(FileNotFoundError, match="echo"):
        load_scenario("does-not-exist")


def test_legacy_turns_schema_is_rejected() -> None:
    with pytest.raises(ValueError, match="exchanges"):
        VirtualScenario.from_mapping({"id": "bad", "turns": []})


def test_response_must_look_like_chat_completion() -> None:
    with pytest.raises(ValueError, match="response"):
        VirtualScenario.from_mapping(
            {
                "id": "bad",
                "exchanges": [{"id": "x", "request": {"messages": []}, "response": {}}],
            }
        )


def test_virtual_http_app_returns_fixture_response() -> None:
    client = TestClient(create_virtual_openai_app("echo"))
    models = client.get("/v1/models")
    assert models.status_code == 200
    assert models.json()["data"][0]["id"] == VIRTUAL_MODEL_ID
    completion = client.post(
        "/v1/chat/completions",
        json={
            "model": VIRTUAL_MODEL_ID,
            "messages": [
                {
                    "role": "user",
                    "content": (
                        "[from OpenAI-compatible HTTP][openai:api][conversation:win] "
                        "message:\nhello"
                    ),
                }
            ],
        },
    )
    assert completion.status_code == 200
    body = completion.json()
    assert body["object"] == "chat.completion"
    assert body["id"] == "chatcmpl-echo"
    message = body["choices"][0]["message"]
    assert message["tool_calls"][0]["type"] == "function"
    assert message["tool_calls"][0]["function"]["name"] == "communicate"
    raw_arguments = message["tool_calls"][0]["function"]["arguments"]
    assert isinstance(raw_arguments, str)
    arguments = json.loads(raw_arguments)
    assert arguments["message"] == "echo: hello"


@pytest.mark.asyncio
async def test_openai_compatible_provider_uses_virtual_http_server() -> None:
    app = create_virtual_openai_app("echo")
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error")
    server = uvicorn.Server(config)
    server.install_signal_handlers = False
    serve_task = asyncio.create_task(server.serve())
    try:
        for _ in range(100):
            if server.started:
                break
            await asyncio.sleep(0.02)
        assert server.started
        provider = OpenAICompatibleProvider(
            api_key="sk-test",
            base_url=f"http://127.0.0.1:{port}/v1",
            name="virtual",
        )
        provider.set_model(VIRTUAL_MODEL_ID)
        response = await provider.complete(
            messages=[
                Message(
                    role="user",
                    content=(
                        "[from OpenAI-compatible HTTP][openai:api][conversation:win] "
                        "message:\nhello"
                    ),
                )
            ],
            system_prompt="system",
            tools=[],
        )
        assert response.tool_calls[0].name == "communicate"
        assert response.tool_calls[0].arguments["message"] == "echo: hello"
        assert response.tool_calls[0].arguments["participant_id"] == "openai:api"
        assert app.state.runner.turn_ids == ["echo_user"]
        assert app.state.runner.trace[0].response["object"] == "chat.completion"
    finally:
        server.should_exit = True
        await serve_task
