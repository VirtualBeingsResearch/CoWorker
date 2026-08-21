from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from coworker.brain.anthropic_provider import AnthropicProvider
from coworker.brain.openai_compatible_provider import OpenAICompatibleProvider
from coworker.core.types import Message

_TOOLS = [
    {
        "name": "sleep",
        "description": "Rest",
        "parameters": {"type": "object", "properties": {}},
    }
]


def _chat_provider() -> tuple[OpenAICompatibleProvider, AsyncMock]:
    create = AsyncMock(
        return_value=SimpleNamespace(
            model="compatible-model",
            usage=SimpleNamespace(prompt_tokens=1, completion_tokens=1),
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(
                        content="ok",
                        tool_calls=[],
                        reasoning_content=None,
                    )
                )
            ],
        )
    )
    provider = OpenAICompatibleProvider.__new__(OpenAICompatibleProvider)
    provider._current_model = "compatible-model"
    provider._client = SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=create))
    )
    return provider, create


def _anthropic_provider() -> tuple[AnthropicProvider, AsyncMock]:
    create = AsyncMock(
        return_value=SimpleNamespace(
            model="claude-sonnet-4-6",
            stop_reason="end_turn",
            usage=SimpleNamespace(input_tokens=1, output_tokens=1),
            content=[SimpleNamespace(type="text", text="ok")],
        )
    )
    provider = AnthropicProvider.__new__(AnthropicProvider)
    provider._current_model = "claude-sonnet-4-6"
    provider._client = SimpleNamespace(messages=SimpleNamespace(create=create))
    return provider, create


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("make_provider", "expected"),
    [
        (_chat_provider, "required"),
        (_anthropic_provider, {"type": "any"}),
    ],
)
async def test_tool_choice_is_required_by_default(make_provider, expected):
    provider, create = make_provider()

    await provider.complete(
        messages=[Message(role="user", content="hi")],
        system_prompt="system",
        tools=_TOOLS,
    )

    assert create.await_args.kwargs["tool_choice"] == expected


@pytest.mark.asyncio
@pytest.mark.parametrize("make_provider", [_chat_provider, _anthropic_provider])
async def test_required_tool_choice_can_be_disabled(make_provider):
    provider, create = make_provider()

    await provider.complete(
        messages=[Message(role="user", content="hi")],
        system_prompt="system",
        tools=_TOOLS,
        tool_choice_required=False,
    )

    assert "tool_choice" not in create.await_args.kwargs
