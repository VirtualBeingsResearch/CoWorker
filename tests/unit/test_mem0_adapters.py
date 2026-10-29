from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

from mem0.configs.llms.anthropic import AnthropicConfig
from mem0.utils.factory import LlmFactory

from coworker.memory.backends.mem0 import Mem0Backend
from coworker.memory.mem0_adapters import (
    CoworkerAnthropicLLM,
    CoworkerOpenAIConfig,
    CoworkerOpenAILLM,
    register_mem0_adapters,
)


def test_openai_adapter_ignores_openrouter_environment(monkeypatch) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "unrelated-openrouter-key")

    llm = CoworkerOpenAILLM(
        {
            "api_key": "coworker-key",
            "model": "vendor-model",
            "openai_base_url": "https://llm.example.test/v1",
        }
    )

    assert str(llm.client.base_url) == "https://llm.example.test/v1/"
    assert llm.config.model == "vendor-model"


def test_openai_config_holds_coworker_thinking_fields() -> None:
    config = CoworkerOpenAIConfig(
        model="deepseek-v4-pro",
        api_key="k",
        thinking=True,
        coworker_provider="deepseek",
    )

    assert config.thinking is True
    assert config.coworker_provider == "deepseek"


def test_anthropic_adapter_applies_custom_base_url() -> None:
    llm = CoworkerAnthropicLLM(
        {
            "api_key": "coworker-key",
            "model": "vendor-model",
            "anthropic_base_url": "https://anthropic.example.test",
        }
    )

    assert str(llm.client.base_url) == "https://anthropic.example.test"
    assert llm.config.model == "vendor-model"


def test_adapters_replace_mem0_supported_provider_implementations() -> None:
    register_mem0_adapters()

    assert LlmFactory.provider_to_class["openai"] == (
        "coworker.memory.mem0_adapters.CoworkerOpenAILLM",
        CoworkerOpenAIConfig,
    )
    assert LlmFactory.provider_to_class["anthropic"] == (
        "coworker.memory.mem0_adapters.CoworkerAnthropicLLM",
        AnthropicConfig,
    )


def _make_openai_response(content: str = "{}") -> MagicMock:
    message = MagicMock()
    message.content = content
    choice = MagicMock()
    choice.message = message
    response = MagicMock()
    response.choices = [choice]
    return response


def _mock_create(llm: CoworkerOpenAILLM, content: str = "{}") -> MagicMock:
    create = MagicMock(return_value=_make_openai_response(content))
    llm.client.chat.completions.create = create
    return create


def test_generate_response_injects_disabled_thinking_for_deepseek() -> None:
    llm = CoworkerOpenAILLM(
        {
            "api_key": "k",
            "model": "deepseek-v4-pro",
            "coworker_provider": "deepseek",
            "thinking": False,
        }
    )
    create = _mock_create(llm)

    llm.generate_response(messages=[{"role": "user", "content": "hi"}])

    kwargs = create.call_args.kwargs
    assert kwargs["extra_body"] == {"thinking": {"type": "disabled"}}


def test_generate_response_injects_enabled_thinking_for_qwen() -> None:
    llm = CoworkerOpenAILLM(
        {
            "api_key": "k",
            "model": "qwen3.6-flash",
            "coworker_provider": "qwen",
            "thinking": True,
        }
    )
    create = _mock_create(llm)

    llm.generate_response(messages=[{"role": "user", "content": "hi"}])

    kwargs = create.call_args.kwargs
    assert kwargs["extra_body"] == {"enable_thinking": True}


def test_generate_response_skips_extra_body_for_non_thinking_model() -> None:
    llm = CoworkerOpenAILLM(
        {
            "api_key": "k",
            "model": "deepseek-chat",
            "coworker_provider": "deepseek",
            "thinking": False,
        }
    )
    create = _mock_create(llm)

    llm.generate_response(messages=[{"role": "user", "content": "hi"}])

    assert "extra_body" not in create.call_args.kwargs


def test_extract_response_reasoning_from_openai_style_message() -> None:
    response = SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(reasoning_content="think first"))]
    )

    assert Mem0Backend._extract_response_reasoning(response) == "think first"


def test_extract_response_reasoning_from_anthropic_thinking_blocks() -> None:
    response = SimpleNamespace(
        content=[
            SimpleNamespace(type="thinking", thinking="first"),
            SimpleNamespace(type="text", text="answer"),
            SimpleNamespace(type="thinking", thinking="second"),
        ]
    )

    assert Mem0Backend._extract_response_reasoning(response) == "first\nsecond"


def test_extract_response_reasoning_returns_none_without_thinking() -> None:
    assert Mem0Backend._extract_response_reasoning(SimpleNamespace()) is None


def test_generate_response_skips_extra_body_for_unknown_provider() -> None:
    llm = CoworkerOpenAILLM(
        {
            "api_key": "k",
            "model": "some-model",
            "coworker_provider": "openai",
            "thinking": False,
        }
    )
    create = _mock_create(llm)

    llm.generate_response(messages=[{"role": "user", "content": "hi"}])

    assert "extra_body" not in create.call_args.kwargs
