from __future__ import annotations

from mem0.configs.llms.anthropic import AnthropicConfig
from mem0.configs.llms.openai import OpenAIConfig
from mem0.utils.factory import LlmFactory

from coworker.memory.mem0_adapters import (
    CoworkerAnthropicLLM,
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
        OpenAIConfig,
    )
    assert LlmFactory.provider_to_class["anthropic"] == (
        "coworker.memory.mem0_adapters.CoworkerAnthropicLLM",
        AnthropicConfig,
    )
