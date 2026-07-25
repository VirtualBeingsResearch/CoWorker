from __future__ import annotations

import json

import openai
from loguru import logger

from coworker.brain.base import BaseLLMProvider
from coworker.brain.tls import shared_ssl_context
from coworker.core.constants import DEFAULT_LLM_MAX_TOKENS
from coworker.core.exceptions import ProviderError
from coworker.core.types import LLMResponse, Message, ToolCall


def _parse_tool_arguments(raw: str, tool_name: str) -> dict:
    try:
        return json.loads(raw)
    except json.JSONDecodeError as e:
        logger.warning(f"Failed to parse tool call arguments for '{tool_name}': {raw!r}")
        return {"__parse_error__": str(e), "__raw_arguments__": raw}

_DEEPSEEK_MODELS = {
    "deepseek-v4-flash",
    "deepseek-v4-pro",
}

# Models that support extended thinking; require reasoning_effort param.
_THINKING_MODELS = _DEEPSEEK_MODELS


class DeepSeekProvider(BaseLLMProvider):
    provider_type = "deepseek"
    api_dialect = "openai"
    default_base_url = "https://api.deepseek.com"

    def __init__(self, api_key: str, base_url: str | None = None, name: str | None = None) -> None:
        super().__init__(name)
        self._client = openai.AsyncOpenAI(
            api_key=api_key,
            base_url=self.resolve_base_url(base_url),
            http_client=openai.DefaultAsyncHttpxClient(verify=shared_ssl_context()),
        )
        self._current_model = "deepseek-v4-flash"

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

    async def complete(
        self,
        messages: list[Message],
        system_prompt: str,
        tools: list[dict],
        max_tokens: int = DEFAULT_LLM_MAX_TOKENS,
        thinking: bool = True,
    ) -> LLMResponse:
        api_messages: list[dict] = [{"role": "system", "content": system_prompt}]
        for m in messages:
            d = m.to_dict()
            if m.role == "user":
                d["content"] = self._adapt_content(m.content, self._current_model)
            if m.role == "system":
                d["role"] = "user"
            if m.role == "assistant" and m.content_text():
                if m.reasoning_content is None:
                    d["reasoning_content"] = ""
            # reasoning_content is already included by to_dict() when present;
            # DeepSeek requires it to be echoed back after any tool call turn.
            api_messages.append(d)

        kwargs: dict = {
            "model": self._current_model,
            "max_tokens": max_tokens,
            "messages": api_messages,
        }
        if tools:
            kwargs["tools"] = [{"type": "function", "function": t} for t in tools]
        if self._current_model in _THINKING_MODELS and thinking:
            kwargs["extra_body"] = {"thinking": {"type": "enabled"}}
        elif not thinking:
            kwargs["extra_body"] = {"thinking": {"type": "disabled"}}

        try:
            response = await self._client.chat.completions.create(**kwargs)
        except openai.APIError as e:
            raise ProviderError(str(e)) from e

        choice = response.choices[0]
        msg = choice.message

        tool_calls: list[ToolCall] = []
        if msg.tool_calls:
            for tc in msg.tool_calls:
                tool_calls.append(
                    ToolCall(
                        id=tc.id,
                        name=tc.function.name,
                        arguments=_parse_tool_arguments(tc.function.arguments, tc.function.name),
                    )
                )

        # reasoning_content is only present on thinking-mode responses.
        reasoning_content: str | None = getattr(msg, "reasoning_content", None) or None

        return LLMResponse(
            content=msg.content or "",
            tool_calls=tool_calls,
            stop_reason="tool_use" if tool_calls else "end_turn",
            model=response.model,
            usage=self._extract_usage(response),
            reasoning_content=reasoning_content,
        )

    def set_model(self, model_id: str) -> None:
        self._current_model = model_id

    def list_models(self) -> list[str]:
        return sorted(_DEEPSEEK_MODELS)

    def supports_tool_use(self, model_id: str) -> bool:
        return True

    def supports_vision(self, model_id: str) -> bool:
        return False  # DeepSeek text models don't support vision
