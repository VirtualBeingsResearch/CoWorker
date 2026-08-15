from __future__ import annotations

import json
from typing import Any

import openai
from loguru import logger

from coworker.brain.base import (
    BaseLLMProvider,
    pdf_attachment_fallback,
    unsupported_image_fallback,
    unsupported_video_fallback,
)
from coworker.brain.thinking import ThinkingEffort, resolve_effort
from coworker.brain.tls import shared_ssl_context
from coworker.core.constants import DEFAULT_LLM_MAX_TOKENS
from coworker.core.exceptions import ProviderError
from coworker.core.types import LLMResponse, Message, ToolCall
from coworker.i18n import tr


def parse_tool_arguments(raw: str, tool_name: str) -> dict[str, Any]:
    """Parse a tool-call arguments string without killing the turn on bad JSON."""
    try:
        return json.loads(raw)
    except json.JSONDecodeError as e:
        logger.warning(f"Failed to parse tool call arguments for '{tool_name}': {raw!r}")
        return {"__parse_error__": str(e), "__raw_arguments__": raw}


class OpenAIChatCompletionsProvider(BaseLLMProvider):
    """Shared adapter for OpenAI-compatible ``chat.completions`` providers.

    Subclasses declare their catalog/capabilities and a
    :meth:`_apply_thinking` mapping; message conversion, usage extraction,
    tool-call parsing, and error translation live here once instead of being
    copied into every provider.
    """

    api_dialect = "openai"
    _DEFAULT_MODEL = ""

    def __init__(self, api_key: str, base_url: str | None = None, name: str | None = None) -> None:
        super().__init__(name)
        self._client = openai.AsyncOpenAI(
            api_key=api_key,
            base_url=self.resolve_base_url(base_url),
            http_client=openai.DefaultAsyncHttpxClient(verify=shared_ssl_context()),
        )
        self._current_model = self._DEFAULT_MODEL

    # -- request building hooks ------------------------------------------------

    def _transform_message(
        self,
        message: Message,
        data: dict[str, Any],
        model_id: str,
    ) -> dict[str, Any] | None:
        """Per-provider message rewrite hook; ``None`` drops the message."""
        return data

    def _build_api_messages(
        self,
        messages: list[Message],
        system_prompt: str,
        model_id: str,
    ) -> list[dict[str, Any]]:
        api_messages: list[dict[str, Any]] = [
            {"role": "system", "content": system_prompt}
        ]
        for message in messages:
            data = self._transform_message(message, message.to_dict(), model_id)
            if data is None:
                continue
            if message.role == "user":
                data["content"] = self._adapt_content(message.content, model_id)
            elif message.role == "system":
                data["role"] = "user"
            api_messages.append(data)
        return api_messages

    @staticmethod
    def _build_tools(tools: list[dict]) -> list[dict]:
        return [{"type": "function", "function": tool} for tool in tools]

    def _apply_thinking(
        self,
        kwargs: dict[str, Any],
        effort: ThinkingEffort | None,
        model_id: str,
    ) -> None:
        """Inject provider-specific thinking parameters into ``kwargs``.

        ``effort`` is already resolved (``none`` means disabled; ``None`` means
        provider default). The default deliberately injects nothing so generic
        OpenAI-compatible endpoints never receive unknown fields.
        """

    # -- content adaptation ---------------------------------------------------

    def _adapt_content(self, content, model_id):
        if isinstance(content, str):
            return content
        if not self.can_use_vision(model_id):
            return super()._adapt_content(content, model_id)
        result: list[dict[str, Any]] = []
        for block in content:
            block_type = block.get("type")
            source = block.get("source", {})
            if block_type == "image":
                if source.get("type") == "base64":
                    data_url = f"data:{source['media_type']};base64,{source['data']}"
                    result.append({"type": "image_url", "image_url": {"url": data_url}})
                else:
                    result.append({"type": "text", "text": unsupported_image_fallback()})
            elif block_type == "video":
                if self.can_use_video(model_id) and source.get("type") == "base64":
                    data_url = f"data:{source['media_type']};base64,{source['data']}"
                    result.append({"type": "video_url", "video_url": {"url": data_url}})
                else:
                    result.append({"type": "text", "text": unsupported_video_fallback()})
            elif block_type == "document":
                filename = block.get("_filename", tr("attachment_fallback.document_name"))
                text = pdf_attachment_fallback(filename, block.get("_saved_path", ""))
                result.append({"type": "text", "text": text})
            else:
                result.append({k: v for k, v in block.items() if not k.startswith("_")})
        return result

    # -- response parsing -----------------------------------------------------

    @staticmethod
    def _extract_usage(response) -> dict[str, int]:
        usage = getattr(response, "usage", None)
        return {
            "input_tokens": getattr(usage, "prompt_tokens", 0) or 0,
            "output_tokens": getattr(usage, "completion_tokens", 0) or 0,
            "cached_tokens": (
                getattr(
                    getattr(usage, "prompt_tokens_details", None),
                    "cached_tokens",
                    0,
                )
                if usage
                else 0
            ),
        }

    @staticmethod
    def _extract_reasoning_content(message) -> str | None:
        reasoning = getattr(message, "reasoning_content", None)
        if isinstance(reasoning, str) and reasoning:
            return reasoning
        reasoning = getattr(message, "reasoning", None)
        return reasoning if isinstance(reasoning, str) and reasoning else None

    @staticmethod
    def _extract_tool_calls(message) -> list[ToolCall]:
        tool_calls: list[ToolCall] = []
        for tool_call in getattr(message, "tool_calls", None) or []:
            function = getattr(tool_call, "function", None)
            name = getattr(function, "name", "") if function else ""
            arguments = getattr(function, "arguments", "") if function else ""
            tool_calls.append(
                ToolCall(
                    id=getattr(tool_call, "id", ""),
                    name=name,
                    arguments=parse_tool_arguments(arguments, name),
                )
            )
        return tool_calls

    def _build_response(self, response, message, tool_calls) -> LLMResponse:
        return LLMResponse(
            content=getattr(message, "content", "") or "",
            tool_calls=tool_calls,
            stop_reason="tool_use" if tool_calls else "end_turn",
            model=response.model,
            usage=self._extract_usage(response),
            reasoning_content=self._extract_reasoning_content(message),
        )

    # -- complete -------------------------------------------------------------

    async def complete(
        self,
        messages: list[Message],
        system_prompt: str,
        tools: list[dict],
        max_tokens: int = DEFAULT_LLM_MAX_TOKENS,
        thinking: bool = True,
        thinking_effort: str | None = None,
    ) -> LLMResponse:
        effort = resolve_effort(thinking, thinking_effort)
        api_messages = self._build_api_messages(messages, system_prompt, self._current_model)
        kwargs: dict[str, Any] = {
            "model": self._current_model,
            "max_tokens": max_tokens,
            "messages": api_messages,
        }
        if tools:
            kwargs["tools"] = self._build_tools(tools)
        self._apply_thinking(kwargs, effort, self._current_model)

        try:
            response = await self._client.chat.completions.create(**kwargs)
        except openai.APIError as e:
            raise ProviderError(str(e)) from e

        choice = response.choices[0]
        message = choice.message
        tool_calls = self._extract_tool_calls(message)
        return self._build_response(response, message, tool_calls)

    def set_model(self, model_id: str) -> None:
        self._current_model = model_id
