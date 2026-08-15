from __future__ import annotations

from typing import Any

from coworker.brain.openai_chat import OpenAIChatCompletionsProvider
from coworker.brain.openai_chat import parse_tool_arguments as _parse_tool_arguments  # noqa: F401
from coworker.brain.thinking import ThinkingEffort

_QWEN_MODELS = {
    "qwen3.6-flash",
    "qwen3.6-plus",
    "qwen3.6-max-preview",
    "qwen3.7-plus",
    "qwen3.7-max",
}

_VISION_MODELS = {
    "qwen3.6-plus",
    "qwen3.7-plus",
}

_VIDEO_MODELS = {
    "qwen3.6-plus",
    "qwen3.7-plus",
}

# Qwen3 系列支持 extended thinking，通过 enable_thinking extra_body 开关；
# 3.6+ 同时支持 reasoning_effort（官方档位为 low/medium/xhigh）。
_THINKING_MODELS = _QWEN_MODELS

# Qwen 官方档位没有 high/max；high 及更强统一映射到 xhigh。
_EFFORT_ALIASES = {"minimal": "low", "high": "xhigh", "max": "xhigh"}


class QwenProvider(OpenAIChatCompletionsProvider):
    provider_type = "qwen"
    default_base_url = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    _DEFAULT_MODEL = "qwen-plus"

    def _apply_thinking(
        self,
        kwargs: dict[str, Any],
        effort: ThinkingEffort | None,
        model_id: str,
    ) -> None:
        if model_id not in _THINKING_MODELS:
            return
        if effort == "none":
            kwargs["extra_body"] = {"enable_thinking": False}
            return
        extra_body: dict[str, Any] = {"enable_thinking": True}
        if effort is not None:
            extra_body["reasoning_effort"] = _EFFORT_ALIASES.get(effort, effort)
        kwargs["extra_body"] = extra_body

    def list_models(self) -> list[str]:
        return sorted(_QWEN_MODELS)

    def supports_tool_use(self, model_id: str) -> bool:
        return model_id in _QWEN_MODELS

    def supports_vision(self, model_id: str) -> bool:
        return model_id in _VISION_MODELS or model_id.endswith("-plus")

    def supports_video(self, model_id: str) -> bool:
        return model_id in _VIDEO_MODELS
