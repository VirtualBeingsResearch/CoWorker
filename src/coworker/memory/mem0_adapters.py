from __future__ import annotations

import copy
import threading
from collections.abc import Callable
from types import SimpleNamespace
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
from coworker.brain.opencode_go_provider import _DEEPSEEK_MODELS as _OPENCODE_GO_DEEPSEEK_MODELS
from coworker.brain.opencode_go_provider import _RESPONSES_MODELS as _OPENCODE_GO_RESPONSES_MODELS
from coworker.brain.opencode_go_provider import _opencode_go_headers
from coworker.brain.qwen_provider import _THINKING_MODELS as _QWEN_THINKING_MODELS
from coworker.brain.routing import ModelRouter, ModelTarget
from coworker.brain.tls import shared_ssl_context
from coworker.brain.zhipu_provider import _ALWAYS_THINKING_MODELS as _ZHIPU_ALWAYS_THINKING_MODELS
from coworker.brain.zhipu_provider import _THINKING_MODELS as _ZHIPU_THINKING_MODELS
from coworker.core.llm_errors import (
    FailureCategory,
    ProviderCallError,
    capture_response_usage,
    check_response_finish,
    response_failure,
)
from coworker.core.model_routing import ModelRoutingConfig


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
                "type": "enabled" if thinking or model in _ZHIPU_ALWAYS_THINKING_MODELS else "disabled",
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
    if provider == "opencode-go":
        if model not in _OPENCODE_GO_DEEPSEEK_MODELS:
            return None
        return {"thinking": {"type": "enabled" if thinking else "disabled"}}
    return None


class CoworkerAnthropicLLM(AnthropicLLM):
    def __init__(self, config: AnthropicConfig | dict[str, Any] | None = None) -> None:
        super().__init__(config)
        previous_client = cast(anthropic.Anthropic, getattr(self, "client"))
        client_config: dict[str, Any] = {
            "api_key": self.config.api_key,
            "max_retries": 0,
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
            max_retries=0,
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


class RoutedMem0LLM:
    """Route mem0's synchronous SDK calls with the same process-wide breakers.

    AsyncMemory dispatches generate_response in a worker thread. No asyncio locks
    or event-loop blocking bridge is used here. Each attempt keeps a local SDK
    facade; request timeouts/usage never mutate a concurrently used client.
    """

    def __init__(
        self,
        initial_llm: Any,
        router: ModelRouter,
        targets: Callable[[], list[ModelTarget]],
        policy: Callable[[], ModelRoutingConfig],
        factory: Callable[[ModelTarget], Any],
        usage: Callable[[Any], dict[str, int] | None],
        reasoning: Callable[[Any], str | None] | None = None,
    ) -> None:
        self.config = initial_llm.config
        self._router = router
        self._targets = targets
        self._policy = policy
        self._factory = factory
        self._usage = usage
        self._reasoning = reasoning
        self._clients: dict[tuple[str, str, str], Any] = {}
        self._lock = threading.Lock()
        self._local = threading.local()

    @property
    def _coworker_last_usage(self) -> dict[str, int] | None:
        return getattr(self._local, "usage", None)

    @_coworker_last_usage.setter
    def _coworker_last_usage(self, value: dict[str, int] | None) -> None:
        self._local.usage = value

    @property
    def _coworker_last_usage_source(self) -> str | None:
        return getattr(self._local, "usage_source", None)

    @_coworker_last_usage_source.setter
    def _coworker_last_usage_source(self, value: str | None) -> None:
        self._local.usage_source = value

    @property
    def _coworker_last_provider(self) -> str | None:
        return getattr(self._local, "provider", None)

    @property
    def _coworker_last_model(self) -> str | None:
        return getattr(self._local, "model", None)

    @property
    def _coworker_last_reasoning(self) -> str | None:
        return getattr(self._local, "reasoning", None)

    @_coworker_last_reasoning.setter
    def _coworker_last_reasoning(self, value: str | None) -> None:
        self._local.reasoning = value

    def generate_response(self, *args: Any, **kwargs: Any) -> Any:
        self._local.provider = None
        self._local.model = None

        def call(target: ModelTarget, timeout: float) -> Any:
            self._coworker_last_reasoning = None
            key = (target.name, target.model, target.revision)
            with self._lock:
                if key not in self._clients:
                    self._clients[key] = self._factory(target)
                llm = copy.copy(self._clients[key])
            sdk = llm.client

            def tracked_create(create: Callable[..., Any], *create_args: Any, **create_kwargs: Any) -> Any:
                response = create(*create_args, **create_kwargs, timeout=timeout)
                self._coworker_last_usage = self._usage(response)
                self._coworker_last_usage_source = "provider" if self._coworker_last_usage is not None else None
                if self._reasoning is not None:
                    self._coworker_last_reasoning = self._reasoning(response)
                with capture_response_usage(response):
                    choices = getattr(response, "choices", None)
                    if target.provider.api_dialect != "anthropic":
                        if not choices:
                            raise ProviderCallError(FailureCategory.INVALID_RESPONSE)
                        check_response_finish(getattr(choices[0], "finish_reason", None))
                        refusal = getattr(choices[0].message, "refusal", None)
                        if isinstance(refusal, str) and refusal:
                            check_response_finish("refusal")
                    check_response_finish(getattr(response, "stop_reason", None))
                return response

            if target.provider.api_dialect == "anthropic":
                def create_messages(*create_args: Any, **create_kwargs: Any) -> Any:
                    return tracked_create(sdk.messages.create, *create_args, **create_kwargs)
                llm.client = SimpleNamespace(messages=SimpleNamespace(create=create_messages))
            else:
                def create_chat(*create_args: Any, **create_kwargs: Any) -> Any:
                    if target.provider.provider_type == "opencode-go":
                        create_kwargs["extra_headers"] = _opencode_go_headers(getattr(target.provider, "_opencode_session_id", ""))
                        if target.model in _OPENCODE_GO_RESPONSES_MODELS:
                            return self._responses_chat(sdk, target, timeout, create_kwargs, bool(getattr(llm.config, "thinking", False)))
                    return tracked_create(sdk.chat.completions.create, *create_args, **create_kwargs)
                llm.client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create_chat)))
            return llm.generate_response(*args, **kwargs)

        result = self._router.execute_sync(self._targets(), self._policy(), call, role="long_term")
        self._local.provider = result.target.name
        self._local.model = result.target.model
        return result.value

    def _responses_chat(self, sdk: Any, target: ModelTarget, timeout: float, chat: dict[str, Any], thinking: bool) -> Any:
        """Keep mem0's Chat parser while using Go's actual Responses-only endpoint."""
        inputs = []
        for message in chat["messages"]:
            if message["role"] == "tool":
                inputs.append({"type": "function_call_output", "call_id": message["tool_call_id"], "output": message.get("content", "")})
                continue
            if message.get("content"):
                inputs.append({"role": message["role"], "content": message["content"]})
            for tool in message.get("tool_calls") or []:
                inputs.append({"type": "function_call", "call_id": tool["id"], **tool["function"]})
        options: dict[str, Any] = {"model": target.model, "input": inputs, "timeout": timeout, "extra_headers": chat["extra_headers"]}
        limit = chat.get("max_completion_tokens") or chat.get("max_tokens")
        if limit:
            options["max_output_tokens"] = limit
        if chat.get("response_format"):
            options["text"] = {"format": chat["response_format"]}
        if chat.get("tools"):
            options["tools"] = [{"type": "function", **tool["function"]} for tool in chat["tools"]]
            options["tool_choice"] = chat.get("tool_choice", "auto")
        if target.model == "gpt-5.6-luna":
            options["reasoning"] = {"effort": "high" if thinking else "none"}
        response = sdk.responses.create(**options)
        self._coworker_last_usage = self._usage(response)
        self._coworker_last_usage_source = "provider" if self._coworker_last_usage is not None else None
        if self._reasoning is not None:
            self._coworker_last_reasoning = self._reasoning(response)
        with capture_response_usage(response):
            check_response_finish(getattr(getattr(response, "incomplete_details", None), "reason", None))
            if getattr(response, "status", None) == "failed":
                raise response_failure(getattr(response, "error", None), target.provider.provider_type)
            for item in getattr(response, "output", None) or []:
                if getattr(item, "type", None) == "message":
                    for part in getattr(item, "content", None) or []:
                        if getattr(part, "type", None) == "refusal":
                            check_response_finish("refusal")
        tool_calls = []
        for item in getattr(response, "output", None) or []:
            if getattr(item, "type", None) == "function_call":
                tool_calls.append(SimpleNamespace(function=SimpleNamespace(name=item.name, arguments=item.arguments)))
        message = SimpleNamespace(content=getattr(response, "output_text", "") or "", tool_calls=tool_calls)
        return SimpleNamespace(choices=[SimpleNamespace(message=message)])


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
