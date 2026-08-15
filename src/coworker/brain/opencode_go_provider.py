from __future__ import annotations

from typing import Any

from coworker.brain.openai_chat import OpenAIChatCompletionsProvider
from coworker.brain.thinking import ThinkingEffort

# Official OpenAI-compatible model catalog served by OpenCode Go's
# https://opencode.ai/zen/go/v1 endpoint. MiniMax/Qwen 模型在该订阅里走
# Anthropic 兼容端点，不列入这里的 chat.completions 目录。
_OPENCODE_GO_MODELS = {
    "deepseek-v4-flash",
    "deepseek-v4-pro",
    "kimi-k2.5",
    "kimi-k2.6",
    "kimi-k2.7-code",
    "kimi-k3",
    "glm-5",
    "glm-5.1",
    "glm-5.2",
    "glm-5.3",
    "mimo-v2-pro",
    "mimo-v2-omni",
    "mimo-v2.5",
    "mimo-v2.5-pro",
    "hy3",
    "hy3-preview",
}

_VISION_MODELS = {
    "kimi-k3",
}

_DEEPSEEK_MODELS = {
    "deepseek-v4-flash",
    "deepseek-v4-pro",
}

# Kimi K2.6/K2.7 支持 low/medium/high；K3 只支持 high/max。
_KIMI_MEDIUM_MODELS = {"kimi-k2.6", "kimi-k2.7-code"}
_KIMI_MAX_MODELS = {"kimi-k3"}


class OpenCodeGoProvider(OpenAIChatCompletionsProvider):
    provider_type = "opencode-go"
    default_base_url = "https://opencode.ai/zen/go/v1"
    _DEFAULT_MODEL = "deepseek-v4-flash"

    def _apply_thinking(
        self,
        kwargs: dict[str, Any],
        effort: ThinkingEffort | None,
        model_id: str,
    ) -> None:
        if model_id in _DEEPSEEK_MODELS:
            if effort == "none":
                kwargs["extra_body"] = {"thinking": {"type": "disabled"}}
                return
            kwargs["extra_body"] = {"thinking": {"type": "enabled"}}
            if effort is not None:
                kwargs["reasoning_effort"] = {
                    "minimal": "low",
                    "low": "low",
                    "medium": "high",
                    "high": "high",
                    "xhigh": "high",
                    "max": "max",
                }[effort]
            return

        if model_id in _KIMI_MEDIUM_MODELS and effort is not None and effort != "none":
            kwargs["reasoning_effort"] = {
                "minimal": "low",
                "low": "low",
                "medium": "medium",
                "high": "high",
                "xhigh": "high",
                "max": "high",
            }[effort]
            return

        if model_id in _KIMI_MAX_MODELS and effort is not None and effort != "none":
            kwargs["reasoning_effort"] = "max" if effort in {"xhigh", "max"} else "high"
            return

        # GLM/MiMo/HY 等模型经 OpenCode Go 中转时未公开稳定档位，保持请求
        # 原样交给服务端默认值，避免发送服务端拒绝的参数。

    def list_models(self) -> list[str]:
        return sorted(_OPENCODE_GO_MODELS)

    def supports_tool_use(self, model_id: str) -> bool:
        return model_id in _OPENCODE_GO_MODELS

    def supports_vision(self, model_id: str) -> bool:
        return model_id in _VISION_MODELS
