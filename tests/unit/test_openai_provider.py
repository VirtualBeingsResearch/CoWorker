from __future__ import annotations

import json
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import openai
import pytest

from coworker.brain.openai_provider import OpenAIProvider
from coworker.core.constants import DEFAULT_LLM_MAX_TOKENS
from coworker.core.types import Message
from coworker.i18n import locale_context, tr


def _make_provider(model_id: str = "gpt-5.4") -> tuple[OpenAIProvider, AsyncMock]:
    create = AsyncMock()
    provider = OpenAIProvider.__new__(OpenAIProvider)
    provider._current_model = model_id
    provider._client = SimpleNamespace(
        responses=SimpleNamespace(
            create=create,
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
    @pytest.mark.parametrize("locale", ["en", "zh-CN"])
    @pytest.mark.parametrize("system_count", [1, 2])
    @pytest.mark.asyncio
    async def test_complete_preserves_bubble_identity_and_history_system_messages(
        self, locale, system_count
    ):
        provider, create = _make_provider("gpt-6.1-sol")
        create.return_value = _make_response()
        with locale_context(locale):
            identity = tr(
                "bubble.identity",
                id="bbl_test",
                goal="保留中文目标 / preserve this goal",
                max_cycles=3,
                external=tr("bubble.unbound"),
            )
        messages = [
            Message(role="system", content=identity),
            Message(role="user", content="First question"),
            Message(role="assistant", content="Previous answer"),
        ]
        expected_input = [
            {"role": "user", "content": [{"type": "input_text", "text": identity}]},
            {"role": "user", "content": [{"type": "input_text", "text": "First question"}]},
            {
                "role": "assistant",
                "content": [{"type": "output_text", "text": "Previous answer"}],
            },
        ]
        if system_count == 2:
            messages.append(
                Message(role="system", content=[{"type": "text", "text": "Later context"}])
            )
            expected_input.append(
                {"role": "user", "content": [{"type": "input_text", "text": "Later context"}]}
            )
        messages.append(Message(role="user", content="Continue"))
        expected_input.append(
            {"role": "user", "content": [{"type": "input_text", "text": "Continue"}]}
        )
        original_messages = deepcopy(messages)

        await provider.complete(messages, system_prompt="Main instructions", tools=[])

        kwargs = create.await_args.kwargs
        assert kwargs["instructions"] == "Main instructions"
        assert kwargs["input"] == expected_input
        assert messages == original_messages

    @pytest.mark.asyncio
    async def test_count_tokens_uses_the_same_history_as_complete(self):
        provider, create = _make_provider()
        create.return_value = _make_response()
        count = AsyncMock(return_value=SimpleNamespace(input_tokens=37))
        provider._client.responses.input_tokens = SimpleNamespace(count=count)
        messages = [
            Message(role="system", content="First context"),
            Message(role="user", content="Question"),
            Message(role="system", content=[{"type": "text", "text": "Later context"}]),
        ]

        await provider.complete(messages, system_prompt="Main instructions", tools=[])
        tokens = await provider.count_tokens(messages, "gpt-5.4")

        assert tokens == 37
        count.assert_awaited_once_with(
            model="gpt-5.4",
            input=[
                {"role": "user", "content": [{"type": "input_text", "text": "First context"}]},
                {"role": "user", "content": [{"type": "input_text", "text": "Question"}]},
                {"role": "user", "content": [{"type": "input_text", "text": "Later context"}]},
            ],
        )
        assert count.await_args.kwargs["input"] == create.await_args.kwargs["input"]

    @pytest.mark.asyncio
    async def test_tool_round_trip_keeps_summary_without_sending_reasoning_references(self):
        payloads = []

        def handle(request: httpx.Request) -> httpx.Response:
            payloads.append(json.loads(request.content))
            if len(payloads) == 1:
                output = [
                    {
                        "type": "reasoning",
                        "id": "rs_provider_test",
                        "summary": [{"type": "summary_text", "text": "A displayed summary"}],
                    },
                    {
                        "type": "function_call",
                        "id": "fc_provider_test",
                        "call_id": "call_test",
                        "name": "lookup",
                        "arguments": '{"query":"target"}',
                    },
                ]
            else:
                output = [{
                    "type": "message",
                    "id": "msg_provider_test",
                    "role": "assistant",
                    "status": "completed",
                    "content": [{"type": "output_text", "text": "Done", "annotations": []}],
                }]
            return httpx.Response(200, json={
                "id": f"resp_test_{len(payloads)}",
                "object": "response",
                "created_at": 0,
                "status": "completed",
                "model": "gpt-6.1-sol",
                "output": output,
                "usage": {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15},
            })

        async with openai.AsyncOpenAI(
            api_key="test-only",
            base_url="https://probe.invalid/v1",
            max_retries=0,
            http_client=httpx.AsyncClient(transport=httpx.MockTransport(handle)),
        ) as client:
            provider, _ = _make_provider("gpt-6.1-sol")
            provider._client = client
            messages = [Message(role="user", content="Look up the target")]
            tools = [{"name": "lookup", "parameters": {"type": "object", "properties": {}}}]
            first = await provider.complete(messages, system_prompt="Main instructions", tools=tools)
            assert first.reasoning_content == "A displayed summary"
            call = first.tool_calls[0]
            messages.extend([
                Message(
                    role="assistant",
                    content=first.content,
                    reasoning_content=first.reasoning_content,
                    tool_calls=[{
                        "id": call.id,
                        "type": "function",
                        "function": {"name": call.name, "arguments": json.dumps(call.arguments)},
                    }],
                ),
                Message(role="tool", content="Found the target", tool_call_id=call.id),
            ])

            second = await provider.complete(messages, system_prompt="Main instructions", tools=tools)

        assert second.content == "Done"
        assert messages[1].reasoning_content == "A displayed summary"
        assert len(payloads) == 2
        assert payloads[1]["input"] == [
            {"role": "user", "content": [{"type": "input_text", "text": "Look up the target"}]},
            {
                "type": "function_call",
                "call_id": "call_test",
                "name": "lookup",
                "arguments": json.dumps({"query": "target"}),
            },
            {"type": "function_call_output", "call_id": "call_test", "output": "Found the target"},
        ]
        assert payloads[1]["instructions"] == "Main instructions"
        assert payloads[1]["reasoning"] == {"effort": "high", "summary": "auto"}

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
        assert kwargs["max_output_tokens"] == DEFAULT_LLM_MAX_TOKENS
        assert kwargs["instructions"] == "You are helpful."
        assert kwargs["tools"] == [{"type": "function", **tools[0]}]
        assert kwargs["reasoning"] == {"effort": "high", "summary": "auto"}

    @pytest.mark.asyncio
    async def test_complete_passes_configured_thinking_effort(self):
        provider, create = _make_provider("gpt-5.4")
        create.return_value = _make_response()

        await provider.complete(
            messages=[Message(role="user", content="hi")],
            system_prompt="You are helpful.",
            tools=[],
            thinking_effort="xhigh",
        )

        assert create.await_args.kwargs["reasoning"] == {"effort": "xhigh", "summary": "auto"}

    @pytest.mark.asyncio
    async def test_complete_disables_thinking_for_false_flag(self):
        provider, create = _make_provider("gpt-5.4")
        create.return_value = _make_response()

        await provider.complete(
            messages=[Message(role="user", content="hi")],
            system_prompt="You are helpful.",
            tools=[],
            thinking=False,
            thinking_effort="high",
        )

        assert create.await_args.kwargs["reasoning"] == {"effort": "none"}

    @pytest.mark.asyncio
    async def test_gpt6_astra_maps_disabled_thinking_to_low(self):
        provider, create = _make_provider("gpt-6-astra")
        create.return_value = _make_response()

        await provider.complete(
            messages=[Message(role="user", content="hi")],
            system_prompt="You are helpful.",
            tools=[],
            thinking=False,
        )

        assert create.await_args.kwargs["reasoning"] == {"effort": "low", "summary": "auto"}

    @pytest.mark.asyncio
    async def test_gpt56_sol_keeps_none_and_drops_minimal(self):
        provider, create = _make_provider("gpt-5.6-sol")
        create.return_value = _make_response()

        await provider.complete(
            messages=[Message(role="user", content="hi")],
            system_prompt="You are helpful.",
            tools=[],
            thinking=False,
        )
        assert create.await_args.kwargs["reasoning"] == {"effort": "none"}

        await provider.complete(
            messages=[Message(role="user", content="hi")],
            system_prompt="You are helpful.",
            tools=[],
            thinking_effort="minimal",
        )
        assert create.await_args.kwargs["reasoning"] == {"effort": "low", "summary": "auto"}

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

        input_items = provider._to_responses_input(messages, "gpt-5.4")

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

    @pytest.mark.parametrize("reasoning_content", [None, "A prior summary"])
    def test_to_responses_input_uses_output_text_for_plain_assistant_messages(
        self, reasoning_content
    ):
        provider, _ = _make_provider("gpt-5.4")
        messages = [Message(role="assistant", content="hello", reasoning_content=reasoning_content)]

        input_items = provider._to_responses_input(messages, "gpt-5.4")

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


