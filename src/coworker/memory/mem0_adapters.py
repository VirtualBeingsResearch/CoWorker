from __future__ import annotations

from typing import Any, cast

import anthropic
import openai
from mem0.configs.llms.anthropic import AnthropicConfig
from mem0.configs.llms.openai import OpenAIConfig
from mem0.llms.anthropic import AnthropicLLM
from mem0.llms.openai import OpenAILLM
from mem0.utils.factory import LlmFactory

from coworker.brain.deepseek_provider import _THINKING_MODELS as _DEEPSEEK_THINKING_MODELS
from coworker.brain.minimax_provider import _THINKING_MODELS as _MINIMAX_THINKING_MODELS
from coworker.brain.qwen_provider import _THINKING_MODELS as _QWEN_THINKING_MODELS
from coworker.brain.tls import shared_ssl_context
from coworker.brain.zhipu_provider import _THINKING_MODELS as _ZHIPU_THINKING_MODELS


class CoworkerOpenAIConfig(OpenAIConfig):
    """OpenAI-compatible mem0 config with Coworker thinking pass-through fields.

    ``thinking``（bool，默认 False）与 ``coworker_provider``（coworker 侧 provider 类型，
    如 deepseek/qwen/zhipu/minimax）由 ``LongTermLLMConfig.as_mem0_config`` 写入，
    供 :class:`CoworkerOpenAILLM` 决定注入哪种思考参数。
    """

    def __init__(
        self,
        thinking: bool = False,
        coworker_provider: str = "",
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self.thinking = thinking
        self.coworker_provider = coworker_provider


def _thinking_extra_body(provider: str, model: str, thinking: bool) -> dict[str, Any] | None:
    """按 coworker provider 类型构造 mem0 抽取请求的 thinking extra_body。

    仅当模型属于该 provider 的思考模型集合时注入（镜像 brain 各 provider 的
    ``complete`` 行为，避免给非思考模型发送未知参数）。返回 None 表示无需注入。
    """
    if provider == "deepseek":
        if model not in _DEEPSEEK_THINKING_MODELS:
            return None
        return {"thinking": {"type": "enabled" if thinking else "disabled"}}
    if provider == "qwen":
        if model not in _QWEN_THINKING_MODELS:
            return None
        return {"enable_thinking": thinking}
    if provider == "zhipu":
        if model not in _ZHIPU_THINKING_MODELS:
            return None
        return {
            "thinking": {
                "type": "enabled" if thinking else "disabled",
                "clear_thinking": False,
            }
        }
    if provider == "minimax":
        if model not in _MINIMAX_THINKING_MODELS:
            return None
        return {
            "reasoning_split": True,
            "thinking": "adaptive" if thinking else "disabled",
        }
    return None


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
    def __init__(
        self,
        config: CoworkerOpenAIConfig | dict[str, Any] | None = None,
    ) -> None:
        if config is None:
            config = CoworkerOpenAIConfig()
        elif isinstance(config, dict):
            config = CoworkerOpenAIConfig(**config)
        super().__init__(config)
        previous_client = cast(openai.OpenAI, getattr(self, "client"))
        self.client = openai.OpenAI(
            api_key=self.config.api_key,
            base_url=self.config.openai_base_url,
            http_client=openai.DefaultHttpxClient(verify=shared_ssl_context()),
        )
        previous_client.close()

    def generate_response(
        self,
        messages,
        response_format=None,
        tools=None,
        tool_choice: str = "auto",
        **kwargs,
    ):
        # mem0 原生只转发 reasoning_effort，不转发 thinking/extra_body；
        # 这里按 coworker provider 类型把思考参数并入 extra_body 再委托父类。
        body = _thinking_extra_body(
            getattr(self.config, "coworker_provider", ""),
            getattr(self.config, "model", ""),
            bool(getattr(self.config, "thinking", False)),
        )
        if body:
            kwargs["extra_body"] = {**(kwargs.get("extra_body") or {}), **body}
        return super().generate_response(
            messages=messages,
            response_format=response_format,
            tools=tools,
            tool_choice=tool_choice,
            **kwargs,
        )


def register_mem0_adapters() -> None:
    LlmFactory.register_provider(
        "anthropic",
        "coworker.memory.mem0_adapters.CoworkerAnthropicLLM",
        AnthropicConfig,
    )
    LlmFactory.register_provider(
        "openai",
        "coworker.memory.mem0_adapters.CoworkerOpenAILLM",
        CoworkerOpenAIConfig,
    )
