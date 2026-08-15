from __future__ import annotations

from typing import Any

from coworker.brain.openai_chat import OpenAIChatCompletionsProvider
from coworker.brain.openai_chat import parse_tool_arguments as _parse_tool_arguments  # noqa: F401
from coworker.brain.thinking import ThinkingEffort

_MINIMAX_MODELS = {
    "MiniMax-M3",
}

_VISION_MODELS = {
    "MiniMax-M3",
}

# MiniMax-M3 支持 reasoning_split + adaptive/disabled 两态；effort 档位只
# 决定开/关，没有更细的强度旋钮。
_THINKING_MODELS = _MINIMAX_MODELS


class MiniMaxProvider(OpenAIChatCompletionsProvider):
    provider_type = "minimax"
    default_base_url = "https://api.minimaxi.com/v1"
    _DEFAULT_MODEL = "MiniMax-M3"

    def _apply_thinking(
        self,
        kwargs: dict[str, Any],
        effort: ThinkingEffort | None,
        model_id: str,
    ) -> None:
        if model_id not in _THINKING_MODELS:
            return
        kwargs["extra_body"] = {
            "reasoning_split": True,
            "thinking": "disabled" if effort == "none" else "adaptive",
        }

    def list_models(self) -> list[str]:
        return sorted(_MINIMAX_MODELS)

    def supports_tool_use(self, model_id: str) -> bool:
        return model_id in _MINIMAX_MODELS

    def supports_vision(self, model_id: str) -> bool:
        return model_id in _VISION_MODELS
