from __future__ import annotations

import asyncio
import inspect
import json
from typing import Any, Literal, cast

import httpx
from any_llm import AnyLLM
from any_llm.exceptions import AnyLLMError
from loguru import logger

from coworker.brain.base import (
    BaseLLMProvider,
    pdf_attachment_fallback,
    unsupported_image_fallback,
    unsupported_video_fallback,
)
from coworker.brain.tls import shared_ssl_context
from coworker.core.constants import DEFAULT_LLM_MAX_TOKENS
from coworker.core.exceptions import ProviderError
from coworker.core.types import LLMResponse, Message, ThinkingMode, ToolCall, reasoning_effort
from coworker.i18n import tr


def parse_tool_arguments(raw: str, tool_name: str) -> dict[str, Any]:
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as error:
        logger.warning(f"Failed to parse tool call arguments for '{tool_name}': {raw!r}")
        return {"__parse_error__": str(error), "__raw_arguments__": raw}
    return value if isinstance(value, dict) else {"value": value}


class AnyLLMProvider(BaseLLMProvider):
    """Coworker's provider contract backed by an Any-LLM provider instance."""

    any_llm_provider: str = ""
    initial_model: str = ""

    def __init__(
        self,
        api_key: str,
        base_url: str | None = None,
        name: str | None = None,
    ) -> None:
        super().__init__(name)
        self._current_model = self.initial_model
        client_options = self._any_llm_client_options()
        try:
            self._llm = AnyLLM.create(
                self.any_llm_provider,
                api_key=api_key,
                api_base=self.resolve_base_url(base_url),
                **client_options,
            )
        except (AnyLLMError, ImportError) as error:
            raise ProviderError(str(error)) from error
        self._client: Any = getattr(self._llm, "client", None)

    def _any_llm_client_options(self) -> dict[str, Any]:
        return {"http_client": httpx.AsyncClient(verify=shared_ssl_context())}

    def set_model(self, model_id: str) -> None:
        self._current_model = model_id

    async def fetch_models(self) -> list[str]:
        """Fetch model IDs from the provider without invoking a model."""

        if not self._llm.SUPPORTS_LIST_MODELS:
            return []
        try:
            models = await asyncio.wait_for(self._llm.alist_models(), timeout=15.0)
        except (AnyLLMError, TimeoutError) as error:
            raise ProviderError(str(error)) from error
        return sorted(
            {
                model_id
                for model in models
                if (model_id := str(getattr(model, "id", "") or "").strip())
            }
        )

    async def close(self) -> None:
        close = getattr(self._client, "close", None)
        if close is None:
            close = getattr(self._client, "aclose", None)
        if close is None:
            return
        result = close()
        if inspect.isawaitable(result):
            await result

    def _message_role(self, message: Message) -> str:
        return message.role

    def _message_extra_fields(self, message: Message) -> dict[str, Any]:
        if message.reasoning_content:
            return {"reasoning_content": message.reasoning_content}
        return {}

    def _build_messages(self, messages: list[Message], system_prompt: str) -> list[dict[str, Any]]:
        api_messages: list[dict[str, Any]] = [{"role": "system", "content": system_prompt}]
        for message in messages:
            content = (
                self._adapt_content(message.content, self._current_model)
                if message.role == "user"
                else message.content
            )
            converted: dict[str, Any] = {
                "role": self._message_role(message),
                "content": content,
            }
            if message.tool_calls:
                converted["tool_calls"] = message.tool_calls
            if message.tool_call_id:
                converted["tool_call_id"] = message.tool_call_id
            converted.update(self._message_extra_fields(message))
            api_messages.append(converted)
        return api_messages

    @staticmethod
    def _to_tools(tools: list[dict]) -> list[dict[str, Any]]:
        return [{"type": "function", "function": tool} for tool in tools]

    def _completion_options(self, thinking: ThinkingMode) -> dict[str, Any]:
        return {"reasoning_effort": reasoning_effort(thinking)}

    @staticmethod
    def _extract_usage(response: Any) -> dict[str, int]:
        usage = getattr(response, "usage", None)
        details = getattr(usage, "prompt_tokens_details", None)
        return {
            "input_tokens": getattr(usage, "prompt_tokens", 0) or 0,
            "output_tokens": getattr(usage, "completion_tokens", 0) or 0,
            "cached_tokens": getattr(details, "cached_tokens", 0) or 0,
        }

    @staticmethod
    def _extract_reasoning(message: Any) -> str | None:
        reasoning = getattr(message, "reasoning", None)
        if isinstance(reasoning, str):
            return reasoning or None
        content = getattr(reasoning, "content", None)
        if isinstance(content, str) and content:
            return content
        legacy = getattr(message, "reasoning_content", None)
        return legacy if isinstance(legacy, str) and legacy else None

    @staticmethod
    def _extract_tool_calls(message: Any) -> list[ToolCall]:
        tool_calls: list[ToolCall] = []
        for tool_call in getattr(message, "tool_calls", None) or []:
            function = getattr(tool_call, "function", None)
            name = str(getattr(function, "name", "") or "")
            tool_calls.append(
                ToolCall(
                    id=str(getattr(tool_call, "id", "") or ""),
                    name=name,
                    arguments=parse_tool_arguments(
                        str(getattr(function, "arguments", "") or "{}"),
                        name,
                    ),
                )
            )
        return tool_calls

    async def complete(
        self,
        messages: list[Message],
        system_prompt: str,
        tools: list[dict],
        max_tokens: int = DEFAULT_LLM_MAX_TOKENS,
        thinking: ThinkingMode = True,
    ) -> LLMResponse:
        return await self._complete_with_llm(
            self._llm,
            messages,
            system_prompt,
            tools,
            max_tokens=max_tokens,
            thinking=thinking,
        )

    async def _complete_with_llm(
        self,
        llm: Any,
        messages: list[Message],
        system_prompt: str,
        tools: list[dict],
        *,
        max_tokens: int,
        thinking: ThinkingMode,
    ) -> LLMResponse:
        """Run a normalized completion with an explicitly selected Any-LLM client."""

        try:
            response = await llm.acompletion(
                model=self._current_model,
                messages=cast(Any, self._build_messages(messages, system_prompt)),
                tools=self._to_tools(tools) if tools else None,
                max_tokens=max_tokens,
                **self._completion_options(thinking),
            )
        except AnyLLMError as error:
            raise ProviderError(str(error)) from error

        choice = response.choices[0]
        message = choice.message
        tool_calls = self._extract_tool_calls(message)
        finish_reason = getattr(choice, "finish_reason", "stop")
        stop_reason: Literal["end_turn", "tool_use", "max_tokens"] = (
            "tool_use"
            if tool_calls
            else "max_tokens"
            if finish_reason == "length"
            else "end_turn"
        )
        return LLMResponse(
            content=getattr(message, "content", "") or "",
            tool_calls=tool_calls,
            stop_reason=stop_reason,
            model=getattr(response, "model", self._current_model) or self._current_model,
            usage=self._extract_usage(response),
            reasoning_content=self._extract_reasoning(message),
        )


class DynamicAnyLLMProvider(AnyLLMProvider):
    """Conservative adapter for an Any-LLM provider without Coworker overrides."""

    provider_type = ""
    initial_model = ""

    def __init__(
        self,
        provider_key: str,
        api_key: str,
        base_url: str | None = None,
        name: str | None = None,
        client_dialect: str = "any_llm",
        supports_reasoning: bool = False,
    ) -> None:
        self.provider_type = provider_key
        self.any_llm_provider = provider_key
        self.api_dialect = "any_llm"
        self._client_dialect = client_dialect
        self._supports_reasoning = supports_reasoning
        super().__init__(api_key, base_url=base_url, name=name or provider_key)

    def _any_llm_client_options(self) -> dict[str, Any]:
        if self._client_dialect in {"openai", "anthropic"}:
            return super()._any_llm_client_options()
        return {}

    def list_models(self) -> list[str]:
        return []

    def _completion_options(self, thinking: ThinkingMode) -> dict[str, Any]:
        if not self._supports_reasoning:
            return {"reasoning_effort": None}
        return super()._completion_options(thinking)

    def supports_tool_use(self, model_id: str) -> bool:
        return False

    def supports_vision(self, model_id: str) -> bool:
        return False

    def _adapt_content(
        self,
        content: str | list[dict[str, Any]],
        model_id: str,
    ) -> str | list[dict[str, Any]]:
        if isinstance(content, str):
            return content
        if not self.can_use_vision(model_id):
            return super()._adapt_content(content, model_id)

        result: list[dict[str, Any]] = []
        for block in content:
            block_type = block.get("type")
            if block_type == "image":
                source = block.get("source", {})
                if source.get("type") == "base64":
                    data_url = f"data:{source['media_type']};base64,{source['data']}"
                    result.append({"type": "image_url", "image_url": {"url": data_url}})
                else:
                    result.append({"type": "text", "text": unsupported_image_fallback()})
            elif block_type == "video":
                source = block.get("source", {})
                if self.can_use_video(model_id) and source.get("type") == "base64":
                    data_url = f"data:{source['media_type']};base64,{source['data']}"
                    result.append({"type": "video_url", "video_url": {"url": data_url}})
                else:
                    result.append({"type": "text", "text": unsupported_video_fallback()})
            elif block_type == "document":
                filename = block.get("_filename", tr("attachment_fallback.document_name"))
                result.append(
                    {
                        "type": "text",
                        "text": pdf_attachment_fallback(filename, block.get("_saved_path", "")),
                    }
                )
            else:
                result.append(
                    {key: value for key, value in block.items() if not key.startswith("_")}
                )
        return result
