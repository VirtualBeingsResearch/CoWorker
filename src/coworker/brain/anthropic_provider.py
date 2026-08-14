from __future__ import annotations

from typing import Any

from coworker.brain.any_llm_provider import AnyLLMProvider, parse_tool_arguments
from coworker.core.types import Message

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


class AnthropicProvider(AnyLLMProvider):
    provider_type = "anthropic"
    api_dialect = "anthropic"
    any_llm_provider = "anthropic"
    initial_model = "claude-sonnet-4-6"

    def list_models(self) -> list[str]:
        return sorted(_TOOL_USE_MODELS)

    def supports_tool_use(self, model_id: str) -> bool:
        return True

    def supports_vision(self, model_id: str) -> bool:
        return True

    def _message_role(self, message: Message) -> str:
        return "user" if message.role == "system" else message.role

    def _adapt_content(
        self,
        content: str | list[dict[str, Any]],
        model_id: str,
    ) -> str | list[dict[str, Any]]:
        if isinstance(content, str):
            return content
        if not self.can_use_vision(model_id):
            return super()._adapt_content(content, model_id)
        return [
            {key: value for key, value in block.items() if not key.startswith("_")}
            for block in content
        ]

    def _build_count_messages(self, messages: list[Message]) -> list[dict[str, Any]]:
        """Convert history to Anthropic's native token-counting message shape."""

        api_messages: list[dict[str, Any]] = []
        index = 0
        while index < len(messages):
            message = messages[index]
            if message.role == "system":
                api_messages.append({"role": "user", "content": message.content})
                index += 1
                continue

            if message.role == "assistant" and message.tool_calls:
                content: list[dict[str, Any]] = []
                if message.content:
                    content.append({"type": "text", "text": message.content})
                for tool_call in message.tool_calls:
                    function = tool_call["function"]
                    raw_arguments = function.get("arguments", "{}")
                    arguments = (
                        raw_arguments
                        if isinstance(raw_arguments, dict)
                        else parse_tool_arguments(str(raw_arguments), function.get("name", ""))
                    )
                    content.append(
                        {
                            "type": "tool_use",
                            "id": tool_call["id"],
                            "name": function["name"],
                            "input": arguments,
                        }
                    )
                api_messages.append({"role": "assistant", "content": content})
                index += 1
                continue

            if message.role == "tool":
                tool_results: list[dict[str, Any]] = []
                while index < len(messages) and messages[index].role == "tool":
                    tool_message = messages[index]
                    result_content = (
                        tool_message.content
                        if isinstance(tool_message.content, list)
                        else [{"type": "text", "text": tool_message.content}]
                    )
                    tool_results.append(
                        {
                            "type": "tool_result",
                            "tool_use_id": tool_message.tool_call_id,
                            "content": result_content,
                        }
                    )
                    index += 1
                api_messages.append({"role": "user", "content": tool_results})
                continue

            message_content = (
                self._adapt_content(message.content, self._current_model)
                if message.role == "user"
                else message.content
            )
            api_messages.append({"role": message.role, "content": message_content})
            index += 1

        return api_messages

    async def count_tokens(self, messages: list[Message], model_id: str) -> int:
        try:
            result = await self._client.messages.count_tokens(
                model=model_id,
                messages=self._build_count_messages(messages),
            )
            return result.input_tokens
        except Exception:
            return await super().count_tokens(messages, model_id)
