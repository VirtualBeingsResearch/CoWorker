from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from coworker.brain.openai_provider import OpenAIProvider
from coworker.core.constants import DEFAULT_LLM_MAX_TOKENS
from coworker.core.types import Message


def _make_provider(model_id: str = "gpt-5.4") -> tuple[OpenAIProvider, AsyncMock]:
    create = AsyncMock()
    provider = OpenAIProvider.__new__(OpenAIProvider)
    provider._current_model = model_id
    provider._llm = SimpleNamespace(
        aresponses=create,
        client=SimpleNamespace(
            responses=SimpleNamespace(),
        ),
    )
    return provider, create


def _make_response(*, cached_tokens: int = 0, output_text: str = "ok", tool_calls=None):
    return SimpleNamespace(
        model="gpt-5.4",
        usage=SimpleNamespace(
            input_tokens=123,
            output_tokens=45,
            input_tokens_details=SimpleNamespace(cached_tokens=cached_tokens),
        ),
        output_text=output_text,
        output=[
            SimpleNamespace(
                type="function_call",
                call_id=tc["id"],
                name=tc["name"],
                arguments=tc["arguments"],
            )
            for tc in (tool_calls or [])
        ],
    )


class TestOpenAIProvider:
    @pytest.mark.asyncio
    async def test_complete_sends_prompt_cache_hints(self):
        provider, create = _make_provider("gpt-5.4")
        create.return_value = _make_response()

        tools = [
            {
                "name": "search_web",
                "description": "Search the web",
                "parameters": {"type": "object", "properties": {}},
            }
        ]
        await provider.complete(
            messages=[Message(role="user", content="hi")],
            system_prompt="You are helpful.",
            tools=tools,
        )

        kwargs = create.await_args.kwargs
        assert kwargs["prompt_cache_key"].startswith("coworker:openai:")
        assert kwargs["input_data"]
        assert kwargs["max_output_tokens"] == DEFAULT_LLM_MAX_TOKENS
        assert kwargs["instructions"] == "You are helpful."
        assert kwargs["tools"] == [{"type": "function", **tools[0]}]

    @pytest.mark.asyncio
    async def test_complete_reports_cached_tokens(self):
        provider, create = _make_provider("gpt-5.4")
        create.return_value = _make_response(cached_tokens=67)

        response = await provider.complete(
            messages=[Message(role="user", content="hi")],
            system_prompt="You are helpful.",
            tools=[],
        )

        assert response.usage["input_tokens"] == 123
        assert response.usage["output_tokens"] == 45
        assert response.usage["cached_tokens"] == 67
        assert response.content == "ok"

    @pytest.mark.asyncio
    async def test_complete_passes_dynamic_reasoning_effort(self):
        provider, create = _make_provider("gpt-5.4")
        create.return_value = _make_response()

        await provider.complete(
            messages=[Message(role="user", content="hi")],
            system_prompt="You are helpful.",
            tools=[],
            thinking="minimal",
        )

        assert create.await_args.kwargs["reasoning"] == {
            "effort": "minimal",
            "summary": "auto",
        }

    @pytest.mark.asyncio
    async def test_complete_disables_reasoning_without_summary_request(self):
        provider, create = _make_provider("gpt-5.4")
        create.return_value = _make_response()

        await provider.complete(
            messages=[Message(role="user", content="hi")],
            system_prompt="You are helpful.",
            tools=[],
            thinking="none",
        )

        assert create.await_args.kwargs["reasoning"] == {"effort": "none"}

    @pytest.mark.asyncio
    async def test_complete_reads_tool_calls_from_responses_output(self):
        provider, create = _make_provider("gpt-5.4")
        create.return_value = _make_response(
            output_text="",
            tool_calls=[
                {
                    "id": "call_1",
                    "name": "search_web",
                    "arguments": '{"query":"hello"}',
                }
            ],
        )

        response = await provider.complete(
            messages=[Message(role="user", content="hi")],
            system_prompt="You are helpful.",
            tools=[],
        )

        assert response.stop_reason == "tool_use"
        assert len(response.tool_calls) == 1
        assert response.tool_calls[0].id == "call_1"
        assert response.tool_calls[0].name == "search_web"
        assert response.tool_calls[0].arguments == {"query": "hello"}

    def test_to_responses_input_converts_tool_output_and_assistant_tool_call(self):
        provider, _ = _make_provider("gpt-5.4")
        messages = [
            Message(
                role="assistant",
                content="",
                tool_calls=[
                    {
                        "id": "call_1",
                        "type": "function",
                        "function": {
                            "name": "search_web",
                            "arguments": '{"query":"hello"}',
                        },
                    }
                ],
            ),
            Message(role="tool", content="done", tool_call_id="call_1"),
        ]

        input_items, _ = provider._to_responses_input(messages, "gpt-5.4")

        assert input_items[0] == {
            "type": "function_call",
            "call_id": "call_1",
            "name": "search_web",
            "arguments": '{"query":"hello"}',
        }
        assert input_items[1] == {
            "type": "function_call_output",
            "call_id": "call_1",
            "output": "done",
        }

    def test_to_responses_input_uses_output_text_for_plain_assistant_messages(self):
        provider, _ = _make_provider("gpt-5.4")
        messages = [Message(role="assistant", content="hello")]

        input_items, _ = provider._to_responses_input(messages, "gpt-5.4")

        assert input_items == [
            {
                "role": "assistant",
                "content": [{"type": "output_text", "text": "hello"}],
            }
        ]

    @pytest.mark.asyncio
    async def test_complete_handles_malformed_tool_call_arguments(self):
        provider, create = _make_provider("gpt-5.4")
        create.return_value = _make_response(
            output_text="",
            tool_calls=[{"id": "call_1", "name": "search_web", "arguments": "not valid json{{{"}],
        )

        response = await provider.complete(
            messages=[Message(role="user", content="hi")],
            system_prompt="You are helpful.",
            tools=[],
        )

        assert response.stop_reason == "tool_use"
        assert len(response.tool_calls) == 1
        assert "__parse_error__" in response.tool_calls[0].arguments
        assert response.tool_calls[0].arguments["__raw_arguments__"] == "not valid json{{{"
