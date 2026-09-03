from __future__ import annotations

import pytest

from coworker.i18n import locale_context
from tests.support.openai_compat_harness import (
    authorization,
    chat_payload,
    openai_compat_harness,
)

READ_FILE_TOOL = {
    "type": "function",
    "function": {
        "name": "read_file",
        "description": "Read a file",
        "parameters": {
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
        },
    },
}


@pytest.mark.asyncio
async def test_v1_chat_completes_through_echo_scenario(tmp_path) -> None:
    with locale_context("en"):
        async with openai_compat_harness(tmp_path, scenario="echo") as harness:
            models = await harness.client.get("/v1/models", headers=authorization())
            assert models.status_code == 200
            assert models.json()["data"][0]["id"] == "coworker"
            response = await harness.client.post(
                "/v1/chat/completions",
                headers=authorization(),
                json=chat_payload("hello coworker", conversation_id="win"),
                timeout=8,
            )
            assert response.status_code == 200
            message = response.json()["choices"][0]["message"]
            assert response.json()["choices"][0]["finish_reason"] == "stop"
            assert message["content"] == "echo: hello coworker"
            assert "echo_user" in harness.provider.turn_ids
            recorded = harness.provider.trace[0]
            assert recorded.request["messages"][-1]["role"] == "user"
            assert recorded.response["choices"][0]["finish_reason"] == "tool_calls"
            assert recorded.snapshot.participant_id == "openai:api"
            assert recorded.snapshot.conversation_id == "win"


@pytest.mark.asyncio
async def test_v1_client_tool_roundtrip_uses_named_scenario(tmp_path) -> None:
    with locale_context("en"):
        async with openai_compat_harness(
            tmp_path, scenario="client_tool_roundtrip"
        ) as harness:
            first = await harness.client.post(
                "/v1/chat/completions",
                headers=authorization(),
                json=chat_payload(
                    "open a.py",
                    tools=[READ_FILE_TOOL],
                    conversation_id="win",
                ),
                timeout=8,
            )
            assert first.status_code == 200
            first_message = first.json()["choices"][0]["message"]
            assert first.json()["choices"][0]["finish_reason"] == "tool_calls"
            assert first_message["tool_calls"][0]["function"]["name"] == "read_file"
            assert harness.provider.turn_ids[0] == "dispatch_client_tool"
            assert harness.provider.trace[0].request["messages"][-1]["role"] == "user"
            assert (
                harness.provider.trace[0].response["choices"][0]["message"]["tool_calls"][0][
                    "function"
                ]["name"]
                == "call_client_tool"
            )
            call_id = first_message["tool_calls"][0]["id"]

            followup = await harness.client.post(
                "/v1/chat/completions",
                headers=authorization(),
                json={
                    "model": "coworker",
                    "conversation_id": "win",
                    "messages": [
                        {"role": "user", "content": "open a.py"},
                        first_message,
                        {
                            "role": "tool",
                            "tool_call_id": call_id,
                            "content": "print('hi')",
                        },
                    ],
                },
                timeout=8,
            )
            assert followup.status_code == 200
            assert followup.json()["choices"][0]["finish_reason"] == "stop"
            assert followup.json()["choices"][0]["message"]["content"] == (
                "tool-result: print('hi')"
            )
            assert "reply_after_tools" in harness.provider.turn_ids
            assert any(
                record.response["id"] == "chatcmpl-tool-followup"
                for record in harness.provider.trace
            )


@pytest.mark.asyncio
async def test_custom_mapping_selects_turn_by_user_text(tmp_path) -> None:
    scenario = {
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
                                            "arguments": {
                                                "message": "got-alpha",
                                                "extra": {"end_turn": True},
                                            },
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
                                            "arguments": {
                                                "message": "got-beta",
                                                "extra": {"end_turn": True},
                                            },
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
    with locale_context("en"):
        async with openai_compat_harness(tmp_path, scenario=scenario) as harness:
            response = await harness.client.post(
                "/v1/chat/completions",
                headers=authorization(),
                json=chat_payload("please run beta now", conversation_id="win"),
                timeout=8,
            )
            assert response.status_code == 200
            assert response.json()["choices"][0]["message"]["content"] == "got-beta"
            assert harness.provider.turn_ids[0] == "beta"
            assert harness.provider.trace[0].snapshot.user_text == "please run beta now"
