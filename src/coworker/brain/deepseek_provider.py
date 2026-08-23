from __future__ import annotations

from typing import Any

from coworker.brain.openai_chat import OpenAIChatCompletionsProvider
from coworker.brain.openai_chat import parse_tool_arguments as _parse_tool_arguments  # noqa: F401
from coworker.brain.thinking import ThinkingEffort

_DEEPSEEK_MODELS = {
    "deepseek-v4-flash",
    "deepseek-v4-pro",
    "deepseek-v4-flash-vision-exp",
}

# DeepSeek text models don't accept image input; only the vision-exp model does.
_VISION_MODELS = {"deepseek-v4-flash-vision-exp"}

# Models that support extended thinking; require reasoning_effort param.
_THINKING_MODELS = _DEEPSEEK_MODELS

# Official DeepSeek effort scale is low/high/max (medium/xhigh are mapped to
# high server-side); keep the same mapping explicit here.
_EFFORT_ALIASES = {"minimal": "low", "medium": "high", "xhigh": "high"}


class DeepSeekProvider(OpenAIChatCompletionsProvider):
    provider_type = "deepseek"
    default_base_url = "https://api.deepseek.com"
    _DEFAULT_MODEL = "deepseek-v4-flash"

    def _transform_message(self, message, data: dict[str, Any], model_id: str) -> dict[str, Any]:
        if message.role == "assistant" and message.content_text():
            if message.reasoning_content is None:
                data["reasoning_content"] = ""
        # reasoning_content is already included by to_dict() when present;
        # DeepSeek requires it to be echoed back after any tool call turn.
        return data

    def _apply_thinking(
        self,
        kwargs: dict[str, Any],
        effort: ThinkingEffort | None,
        model_id: str,
    ) -> None:
        if model_id not in _THINKING_MODELS:
            return
        if effort == "none":
            kwargs["extra_body"] = {"thinking": {"type": "disabled"}}
            return
        kwargs["extra_body"] = {"thinking": {"type": "enabled"}}
        if effort is not None:
            mapped = _EFFORT_ALIASES.get(effort, effort)
            if mapped in {"low", "high", "max"}:
                kwargs["reasoning_effort"] = mapped

    def list_models(self) -> list[str]:
        return sorted(_DEEPSEEK_MODELS)

    def supports_tool_use(self, model_id: str) -> bool:
        return True

    def supports_vision(self, model_id: str) -> bool:
        return model_id in _VISION_MODELS
