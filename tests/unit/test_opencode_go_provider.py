from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from coworker.brain.opencode_go_provider import (
    OpenCodeGoProvider,
    _OpenCodeGoResponsesProvider,
)
from coworker.core.types import LLMResponse, Message


@pytest.mark.asyncio
async def test_responses_model_dispatches_to_responses_adapter():
    provider = OpenCodeGoProvider.__new__(OpenCodeGoProvider)
    provider._current_model = "gpt-5.6-luna"
    expected = LLMResponse(
        content="ok",
        tool_calls=[],
        stop_reason="end_turn",
        model="gpt-5.6-luna",
        usage={},
    )
    provider._responses_provider = MagicMock()
    provider._responses_provider.complete = AsyncMock(return_value=expected)
    messages = [Message(role="user", content="hello")]

    result = await provider.complete(
        messages,
        "system",
        [],
        thinking_effort="low",
    )

    assert result is expected
    provider._responses_provider.complete.assert_awaited_once_with(
        messages,
        "system",
        [],
        max_tokens=8192,
        thinking=True,
        thinking_effort="low",
    )


def test_set_model_updates_both_wire_adapters():
    provider = OpenCodeGoProvider.__new__(OpenCodeGoProvider)
    provider._current_model = "deepseek-v4-flash"
    provider._responses_provider = MagicMock()

    provider.set_model("grok-4.5")

    assert provider._current_model == "grok-4.5"
    provider._responses_provider.set_model.assert_called_once_with("grok-4.5")


def test_deepseek_vision_model_uses_image_url_content():
    provider = OpenCodeGoProvider.__new__(OpenCodeGoProvider)
    content = [
        {
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": "image/jpeg",
                "data": "abc",
            },
            "_filename": "photo.jpg",
        }
    ]

    result = provider._adapt_content(content, "deepseek-v4-flash-vision-exp")

    assert provider.supports_vision("deepseek-v4-flash-vision-exp") is True
    assert result == [
        {
            "type": "image_url",
            "image_url": {"url": "data:image/jpeg;base64,abc"},
        }
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("model_id", "thinking", "expected_reasoning"),
    [
        ("gpt-5.6-luna", True, {"effort": "low", "summary": "auto"}),
        ("grok-4.5", False, None),
    ],
)
async def test_responses_models_use_their_supported_reasoning_shape(
    model_id, thinking, expected_reasoning
):
    create = AsyncMock(
        return_value=SimpleNamespace(
            model=model_id,
            usage=None,
            output_text="ok",
            output=[],
        )
    )
    adapter = _OpenCodeGoResponsesProvider.__new__(_OpenCodeGoResponsesProvider)
    adapter.provider_name = "opencode-go"
    adapter._current_model = model_id
    adapter._client = SimpleNamespace(
        responses=SimpleNamespace(create=create),
    )

    await adapter.complete(
        [Message(role="user", content="hello")],
        "system",
        [],
        thinking=thinking,
        thinking_effort="low",
    )

    kwargs = create.await_args.kwargs
    assert kwargs["model"] == model_id
    if expected_reasoning is None:
        assert "reasoning" not in kwargs
    else:
        assert kwargs["reasoning"] == expected_reasoning
