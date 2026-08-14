from __future__ import annotations

from typing import Any

from coworker.brain.any_llm_provider import AnyLLMProvider, parse_tool_arguments
from coworker.core.types import Message, ThinkingMode, reasoning_effort

_parse_tool_arguments = parse_tool_arguments

_DEEPSEEK_MODELS = {
    "deepseek-v4-flash",
    "deepseek-v4-pro",
}
_THINKING_MODELS = _DEEPSEEK_MODELS


class DeepSeekProvider(AnyLLMProvider):
    provider_type = "deepseek"
    api_dialect = "openai"
    any_llm_provider = "deepseek"
    default_base_url = "https://api.deepseek.com"
    initial_model = "deepseek-v4-flash"

    def _message_role(self, message: Message) -> str:
        return "user" if message.role == "system" else message.role

    def _completion_options(self, thinking: ThinkingMode) -> dict[str, Any]:
        effort = reasoning_effort(thinking)
        if effort == "auto":
            return {
                "reasoning_effort": "auto",
                "extra_body": {"thinking": {"type": "enabled"}},
            }
        return {"reasoning_effort": effort}

    def _message_extra_fields(self, message: Message) -> dict[str, Any]:
        fields = super()._message_extra_fields(message)
        if message.role == "assistant" and message.content_text() and not message.reasoning_content:
            fields["reasoning_content"] = ""
        return fields

    def list_models(self) -> list[str]:
        return sorted(_DEEPSEEK_MODELS)

    def supports_tool_use(self, model_id: str) -> bool:
        return True

    def supports_vision(self, model_id: str) -> bool:
        return False
