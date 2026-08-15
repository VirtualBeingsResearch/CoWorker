from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from coworker.brain.base import BaseLLMProvider
from coworker.core.types import LLMResponse


class MockProvider(BaseLLMProvider):
    provider_name = "mock"

    def __init__(self, response: LLMResponse | None = None) -> None:
        self._current_model = "mock-model"
        self._response = response or LLMResponse(
            content="mock response",
            tool_calls=[],
            stop_reason="end_turn",
            model="mock-model",
            usage={"input_tokens": 10, "output_tokens": 5},
        )

    async def complete(
        self,
        messages,
        system_prompt,
        tools,
        max_tokens=4096,
        thinking=True,
        thinking_effort=None,
    ):
        return self._response

    def set_model(self, model_id: str) -> None:
        self._current_model = model_id

    def list_models(self) -> list[str]:
        return ["mock-model"]

    def supports_tool_use(self, model_id: str) -> bool:
        return True

    def supports_vision(self, model_id: str) -> bool:
        return True


@pytest.fixture
def mock_provider():
    return MockProvider()


@pytest.fixture
def mock_long_term():
    lt = MagicMock()
    lt.write = AsyncMock()
    lt.query = AsyncMock(return_value=[])
    lt.add_conversation = AsyncMock()
    return lt
