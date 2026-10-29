from __future__ import annotations

from types import SimpleNamespace

from coworker.brain.anthropic_provider import AnthropicProvider
from coworker.brain.openai_chat import OpenAIChatCompletionsProvider
from coworker.brain.openai_provider import OpenAIProvider
from coworker.core.types import SummaryResult


def _chat_response(*, reasoning_tokens: int | None = None):
    details = (
        SimpleNamespace(reasoning_tokens=reasoning_tokens)
        if reasoning_tokens is not None
        else None
    )
    return SimpleNamespace(
        model="chat-model",
        usage=SimpleNamespace(
            prompt_tokens=100,
            completion_tokens=60,
            prompt_tokens_details=SimpleNamespace(cached_tokens=10),
            completion_tokens_details=details,
        ),
    )


def _responses_response(*, reasoning_tokens: int | None = None):
    details = (
        SimpleNamespace(reasoning_tokens=reasoning_tokens)
        if reasoning_tokens is not None
        else None
    )
    return SimpleNamespace(
        model="responses-model",
        usage=SimpleNamespace(
            input_tokens=100,
            output_tokens=60,
            input_tokens_details=SimpleNamespace(cached_tokens=10),
            output_tokens_details=details,
        ),
    )


def _anthropic_response(*, thinking_tokens: int | None = None):
    details = (
        SimpleNamespace(thinking_tokens=thinking_tokens)
        if thinking_tokens is not None
        else None
    )
    return SimpleNamespace(
        usage=SimpleNamespace(
            input_tokens=100,
            output_tokens=60,
            output_tokens_details=details,
        ),
    )


def test_chat_completions_usage_splits_reasoning_tokens():
    usage = OpenAIChatCompletionsProvider._extract_usage(
        _chat_response(reasoning_tokens=40)
    )

    assert usage == {
        "input_tokens": 100,
        "output_tokens": 60,
        "cached_tokens": 10,
        "reasoning_tokens": 40,
    }


def test_chat_completions_usage_omits_reasoning_without_details():
    usage = OpenAIChatCompletionsProvider._extract_usage(_chat_response())

    assert "reasoning_tokens" not in usage


def test_responses_usage_splits_reasoning_tokens():
    usage = OpenAIProvider._extract_usage(_responses_response(reasoning_tokens=25))

    assert usage == {
        "input_tokens": 100,
        "output_tokens": 60,
        "cached_tokens": 10,
        "reasoning_tokens": 25,
    }


def test_responses_usage_omits_reasoning_without_details():
    usage = OpenAIProvider._extract_usage(_responses_response())

    assert "reasoning_tokens" not in usage


def test_anthropic_usage_splits_thinking_tokens():
    usage = AnthropicProvider._extract_usage(_anthropic_response(thinking_tokens=45))

    assert usage == {
        "input_tokens": 100,
        "output_tokens": 60,
        "reasoning_tokens": 45,
    }


def test_anthropic_usage_omits_thinking_without_details():
    usage = AnthropicProvider._extract_usage(_anthropic_response())

    assert "reasoning_tokens" not in usage


def test_summary_result_separates_reasoning_from_content_tokens():
    result = SummaryResult(
        content="summary",
        usage={"input_tokens": 10, "output_tokens": 60, "reasoning_tokens": 40},
    )

    assert result.output_tokens == 60
    assert result.reasoning_tokens == 40
    assert result.content_output_tokens == 20


def test_summary_result_falls_back_to_total_without_reasoning_split():
    result = SummaryResult(content="summary", usage={"output_tokens": 60})

    assert result.reasoning_tokens == 0
    assert result.content_output_tokens == 60


def test_summary_result_clamps_reasoning_to_total_output():
    result = SummaryResult(
        content="summary",
        usage={"output_tokens": 30, "reasoning_tokens": 80},
    )

    assert result.content_output_tokens == 0
