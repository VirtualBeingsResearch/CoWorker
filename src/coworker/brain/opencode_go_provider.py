from __future__ import annotations

import inspect
from typing import Any, Literal

import anthropic
import httpx
import openai
from any_llm import AnyLLM
from any_llm.exceptions import AnyLLMError

from coworker.brain.base import BaseLLMProvider
from coworker.brain.openai_provider import OpenAIProvider
from coworker.brain.tls import shared_ssl_context
from coworker.core.constants import DEFAULT_LLM_MAX_TOKENS
from coworker.core.exceptions import ProviderError
from coworker.core.types import LLMResponse, Message, ThinkingMode

_RESPONSES_MODELS = {"grok-4.5", "gpt-5.6-luna"}
_ANTHROPIC_MODELS = {
    "minimax-m3",
    "minimax-m2.7",
    "minimax-m2.5",
    "qwen3.8-max",
    "qwen3.7-max",
    "qwen3.7-plus",
    "qwen3.6-plus",
}
_CHAT_MODELS = {
    "glm-5.3",
    "glm-5.2",
    "glm-5.1",
    "kimi-k3",
    "kimi-k2.7-code",
    "kimi-k2.6",
    "deepseek-v4-pro",
    "deepseek-v4-flash",
    "mimo-v2.5",
    "mimo-v2.5-pro",
    "hy3",
}
_MODELS = _CHAT_MODELS | _RESPONSES_MODELS | _ANTHROPIC_MODELS


class OpenCodeGoProvider(OpenAIProvider):
    """OpenCode Go adapter with model-aware endpoint routing.

    Go exposes one model catalog but currently serves models through three wire
    protocols. OpenAI-compatible chat is the conservative fallback for future
    catalog entries whose protocol has not yet been added here.
    """

    provider_type = "opencode-go"
    api_dialect = "openai"
    any_llm_provider = "openai"
    default_base_url = "https://opencode.ai/zen/go/v1"
    initial_model = "deepseek-v4-pro"
    documentation_url = "https://opencode.ai/docs/go/"
    catalog_reasoning = True
    catalog_image = False
    catalog_pdf = False

    @staticmethod
    def _http_client() -> httpx.AsyncClient:
        return httpx.AsyncClient(verify=shared_ssl_context())

    def __init__(
        self,
        api_key: str,
        base_url: str | None = None,
        name: str | None = None,
    ) -> None:
        BaseLLMProvider.__init__(self, name)
        self._current_model = self.initial_model
        self._api_key = api_key
        self._api_base = self.resolve_base_url(base_url)
        self._anthropic_llm: Any | None = None
        try:
            self._llm = AnyLLM.create_openai_compatible(
                self.provider_type,
                api_base=self._api_base or self.default_base_url,
                api_key=api_key,
                http_client=self._http_client(),
            )
        except (AnyLLMError, ImportError, ValueError) as error:
            raise ProviderError(str(error)) from error
        self._client: Any = getattr(self._llm, "client", None)

    @staticmethod
    def _normalize_model_id(model_id: str) -> str:
        return model_id.removeprefix("opencode-go/")

    @classmethod
    def endpoint_dialect(cls, model_id: str) -> Literal["chat", "responses", "anthropic"]:
        normalized = cls._normalize_model_id(model_id)
        if normalized in _RESPONSES_MODELS:
            return "responses"
        if normalized in _ANTHROPIC_MODELS:
            return "anthropic"
        return "chat"

    def set_model(self, model_id: str) -> None:
        self._current_model = self._normalize_model_id(model_id)

    def list_models(self) -> list[str]:
        return sorted(_MODELS)

    async def fetch_models(self) -> list[str]:
        try:
            models = await super().fetch_models()
        except openai.OpenAIError as error:
            raise ProviderError(str(error)) from error
        return sorted({self._normalize_model_id(model) for model in models})

    def supports_tool_use(self, model_id: str) -> bool:
        return self._normalize_model_id(model_id) in _MODELS

    def supports_vision(self, model_id: str) -> bool:
        return False

    def _supports_reasoning_effort(self, model_id: str) -> bool:
        return self._normalize_model_id(model_id) in _RESPONSES_MODELS

    def _anthropic_api_base(self) -> str | None:
        if not self._api_base:
            return None
        normalized = self._api_base.rstrip("/")
        return normalized.removesuffix("/v1") or normalized

    def _get_anthropic_llm(self) -> Any:
        if self._anthropic_llm is None:
            try:
                self._anthropic_llm = AnyLLM.create(
                    "anthropic",
                    api_key=self._api_key,
                    api_base=self._anthropic_api_base(),
                    http_client=self._http_client(),
                )
            except (AnyLLMError, ImportError) as error:
                raise ProviderError(str(error)) from error
        return self._anthropic_llm

    async def complete(
        self,
        messages: list[Message],
        system_prompt: str,
        tools: list[dict],
        max_tokens: int = DEFAULT_LLM_MAX_TOKENS,
        thinking: ThinkingMode = True,
    ) -> LLMResponse:
        try:
            dialect = self.endpoint_dialect(self._current_model)
            if dialect == "responses":
                return await OpenAIProvider.complete(
                    self,
                    messages,
                    system_prompt,
                    tools,
                    max_tokens=max_tokens,
                    thinking=thinking,
                )
            llm = self._get_anthropic_llm() if dialect == "anthropic" else self._llm
            return await self._complete_with_llm(
                llm,
                messages,
                system_prompt,
                tools,
                max_tokens=max_tokens,
                thinking=thinking,
            )
        except (openai.OpenAIError, anthropic.APIError) as error:
            raise ProviderError(str(error)) from error

    async def count_tokens(self, messages: list[Message], model_id: str) -> int:
        if self.endpoint_dialect(model_id) == "responses":
            return await OpenAIProvider.count_tokens(self, messages, model_id)
        return await BaseLLMProvider.count_tokens(self, messages, model_id)

    async def close(self) -> None:
        clients = [self._client]
        if self._anthropic_llm is not None:
            clients.append(getattr(self._anthropic_llm, "client", None))
        for client in clients:
            close = getattr(client, "close", None) or getattr(client, "aclose", None)
            if close is None:
                continue
            result = close()
            if inspect.isawaitable(result):
                await result
