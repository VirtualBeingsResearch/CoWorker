from __future__ import annotations

from typing import Any, cast

import anthropic
import openai
from mem0.configs.llms.anthropic import AnthropicConfig
from mem0.configs.llms.openai import OpenAIConfig
from mem0.llms.anthropic import AnthropicLLM
from mem0.llms.openai import OpenAILLM
from mem0.utils.factory import LlmFactory

from coworker.brain.tls import shared_ssl_context


class CoworkerAnthropicLLM(AnthropicLLM):
    def __init__(self, config: AnthropicConfig | dict[str, Any] | None = None) -> None:
        super().__init__(config)
        previous_client = cast(anthropic.Anthropic, getattr(self, "client"))
        client_config: dict[str, Any] = {
            "api_key": self.config.api_key,
            "http_client": anthropic.DefaultHttpxClient(verify=shared_ssl_context()),
        }
        if self.config.anthropic_base_url:
            client_config["base_url"] = self.config.anthropic_base_url
        self.client = anthropic.Anthropic(**client_config)
        previous_client.close()


class CoworkerOpenAILLM(OpenAILLM):
    def __init__(self, config: OpenAIConfig | dict[str, Any] | None = None) -> None:
        super().__init__(config)
        previous_client = cast(openai.OpenAI, getattr(self, "client"))
        self.client = openai.OpenAI(
            api_key=self.config.api_key,
            base_url=self.config.openai_base_url,
            http_client=openai.DefaultHttpxClient(verify=shared_ssl_context()),
        )
        previous_client.close()


def register_mem0_adapters() -> None:
    LlmFactory.register_provider(
        "anthropic",
        "coworker.memory.mem0_adapters.CoworkerAnthropicLLM",
        AnthropicConfig,
    )
    LlmFactory.register_provider(
        "openai",
        "coworker.memory.mem0_adapters.CoworkerOpenAILLM",
        OpenAIConfig,
    )
