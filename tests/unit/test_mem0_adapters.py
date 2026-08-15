from __future__ import annotations

from unittest.mock import MagicMock

from mem0.configs.llms.anthropic import AnthropicConfig
from mem0.utils.factory import LlmFactory

from coworker.memory.mem0_adapters import (
    CoworkerAnthropicLLM,
    CoworkerAnyLLMConfig,
    CoworkerAnyLLMLLM,
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
    assert LlmFactory.provider_to_class["coworker_any_llm"] == (
        "coworker.memory.mem0_adapters.CoworkerAnyLLMLLM",
        CoworkerAnyLLMConfig,
    )


def test_any_llm_adapter_normalizes_response_without_network(monkeypatch) -> None:
    function = MagicMock(name="function")
    function.name = "lookup"
    function.arguments = '{"query":"hello"}'
    tool_call = MagicMock()
    tool_call.function = function
    message = MagicMock()
    message.content = ""
    message.tool_calls = [tool_call]
    response = MagicMock()
    response.choices = [MagicMock(message=message)]
    response.usage.prompt_tokens = 8
    response.usage.completion_tokens = 3
    response.usage.prompt_tokens_details.cached_tokens = 2
    client = MagicMock()
    client.SUPPORTS_COMPLETION_REASONING = True
    client.completion.return_value = response
    create = MagicMock(return_value=client)
    monkeypatch.setattr("coworker.memory.mem0_adapters.AnyLLM.create", create)

    llm = CoworkerAnyLLMLLM(
        {
            "coworker_provider": "openrouter",
            "api_key": "offline-key",
            "api_base": "https://gateway.example.test/v1",
            "model": "vendor-model",
            "thinking": True,
        }
    )
    result = llm.generate_response(
        messages=[{"role": "user", "content": "hello"}],
        tools=[{"type": "function", "function": {"name": "lookup"}}],
    )

    create.assert_called_once_with(
        "openrouter",
        api_key="offline-key",
        api_base="https://gateway.example.test/v1",
    )
    assert client.completion.call_args.kwargs["reasoning_effort"] == "high"
    assert result == {
        "content": "",
        "tool_calls": [{"name": "lookup", "arguments": {"query": "hello"}}],
    }
    assert llm._coworker_last_usage == {
        "input_tokens": 8,
        "output_tokens": 3,
        "cached_tokens": 2,
    }


def test_any_llm_adapter_omits_reasoning_for_unsupported_provider(monkeypatch) -> None:
    message = MagicMock(content="ok")
    response = MagicMock(choices=[MagicMock(message=message)], usage=None)
    client = MagicMock(SUPPORTS_COMPLETION_REASONING=False)
    client.completion.return_value = response
    monkeypatch.setattr(
        "coworker.memory.mem0_adapters.AnyLLM.create",
        MagicMock(return_value=client),
    )
    llm = CoworkerAnyLLMLLM(
        {"coworker_provider": "openrouter", "model": "vendor-model"}
    )

    assert llm.generate_response([{"role": "user", "content": "hello"}]) == "ok"
    assert client.completion.call_args.kwargs["reasoning_effort"] is None


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
