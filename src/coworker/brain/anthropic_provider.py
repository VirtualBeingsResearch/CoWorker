from __future__ import annotations

import json
from typing import cast

import anthropic

from coworker.brain.base import BaseLLMProvider
from coworker.brain.tls import shared_ssl_context
from coworker.core.constants import DEFAULT_LLM_MAX_TOKENS
from coworker.core.exceptions import ProviderError
from coworker.core.types import LLMResponse, Message, ToolCall

_TOOL_USE_MODELS = {
    "claude-fable-5",
    "claude-mythos-preview",
    "claude-opus-4-8",
    "claude-opus-4-7",
    "claude-sonnet-4-6",
    "claude-haiku-4-5-20251001",
    "claude-opus-4-5",
    "claude-sonnet-4-5",
    "claude-haiku-4-5",
}


class AnthropicProvider(BaseLLMProvider):
    provider_type = "anthropic"
    api_dialect = "anthropic"

    def __init__(self, api_key: str, base_url: str | None = None, name: str | None = None) -> None:
        super().__init__(name)
        kwargs: dict = {
            "api_key": api_key,
            "http_client": anthropic.DefaultAsyncHttpxClient(
                verify=shared_ssl_context(),
            ),
        }
        if base_url:
            kwargs["base_url"] = base_url
        self._client = anthropic.AsyncAnthropic(**kwargs)

    def _build_api_messages(self, messages: list[Message]) -> list[dict]:
        """Convert internal messages to Anthropic API format.

        Anthropic uses content blocks for tool_use/tool_result instead of the
        OpenAI-style tool_calls field and role="tool" messages.
        """
        api_messages: list[dict] = []
        i = 0
        while i < len(messages):
            m = messages[i]
            if m.role == "system":
                api_messages.append({
                    "role": "user",
                    "content": m.content
                })
                i += 1
                continue

            if m.role == "assistant" and m.tool_calls:
                content: list[dict] = []
                if m.content:
                    content.append({"type": "text", "text": m.content})
                for tc in m.tool_calls:
                    content.append({
                        "type": "tool_use",
                        "id": tc["id"],
                        "name": tc["function"]["name"],
                        "input": json.loads(tc["function"]["arguments"]),
                    })
                api_messages.append({"role": "assistant", "content": content})
                i += 1

            elif m.role == "tool":
                tool_results: list[dict] = []
                while i < len(messages) and messages[i].role == "tool":
                    tm = messages[i]
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": tm.tool_call_id,
                        "content": tm.content if isinstance(tm.content, list) else [{"type": "text", "text": tm.content}],
                    })
                    i += 1
                api_messages.append({"role": "user", "content": tool_results})

            else:
                d = m.to_dict()
                if m.role == "user":
                    d["content"] = self._adapt_content(m.content, self._current_model)
                api_messages.append(d)
                i += 1

        return api_messages

    async def complete(
        self,
        messages: list[Message],
        system_prompt: str,
        tools: list[dict],
        max_tokens: int = DEFAULT_LLM_MAX_TOKENS,
        thinking: bool = True,
    ) -> LLMResponse:
        api_messages = self._build_api_messages(messages)
        try:
            kwargs: dict = {
                "model": self._current_model,
                "max_tokens": max_tokens,
                "system": system_prompt,
                "messages": api_messages,
            }
            if tools:
                kwargs["tools"] = tools
            if thinking:
                kwargs["thinking"] = {"type": "adaptive"}
            else:
                kwargs["thinking"] = {"type": "disabled"}
            response = await self._client.messages.create(**kwargs)
        except anthropic.APIError as e:
            raise ProviderError(str(e)) from e

        tool_calls: list[ToolCall] = []
        text_parts: list[str] = []

        for block in response.content:
            if block.type == "text":
                text_parts.append(block.text)
            elif block.type == "tool_use":
                tool_calls.append(
                    ToolCall(id=block.id, name=block.name, arguments=block.input)
                )

        return LLMResponse(
            content="\n".join(text_parts),
            tool_calls=tool_calls,
            stop_reason=response.stop_reason or "end_turn",
            model=response.model,
            usage={
                "input_tokens": response.usage.input_tokens,
                "output_tokens": response.usage.output_tokens,
            },
        )

    def set_model(self, model_id: str) -> None:
        self._current_model = model_id

    def list_models(self) -> list[str]:
        return sorted(_TOOL_USE_MODELS)

    def supports_tool_use(self, model_id: str) -> bool:
        return True

    def supports_vision(self, model_id: str) -> bool:
        return True  # all Claude 3/4 models support vision

    def _adapt_content(self, content, model_id):
        if isinstance(content, str):
            return content
        return [{k: v for k, v in block.items() if not k.startswith("_")} for block in content]

    async def count_tokens(self, messages: list[Message], model_id: str) -> int:
        try:
            api_messages = self._build_api_messages(messages)
            result = await self._client.messages.count_tokens(
                model=model_id,
                messages=cast(list[anthropic.types.MessageParam], api_messages),
            )
            return result.input_tokens
        except Exception:
            return await super().count_tokens(messages, model_id)
