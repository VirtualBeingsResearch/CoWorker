from __future__ import annotations

from typing import Any

from loguru import logger

from coworker.brain.openai_chat import OpenAIChatCompletionsProvider
from coworker.brain.openai_chat import parse_tool_arguments as _parse_tool_arguments  # noqa: F401
from coworker.brain.thinking import ThinkingEffort

_ZHIPU_MODELS = {
    "glm-4.5-air",
    "glm-4.7",
    "glm-5",
    "glm-5v-turbo",
    "glm-5.1",
    "glm-5.2",
    "glm-5.3",
}

_VISION_MODELS = {
    "glm-4v",
    "glm-4v-plus",
    "glm-4v-flash",
    "glm-5v-turbo",
}

# GLM-Z1/5 系列支持 extended thinking。GLM-5.3 不允许关闭思考。
_THINKING_MODELS = _ZHIPU_MODELS
_ALWAYS_THINKING_MODELS = {"glm-5.3"}

# reasoning_effort 仅 GLM-5.2+ 支持。GLM-5.3 只接受 low/high/max；
# GLM-5.2 的 medium/low 会被服务端映射为 high，xhigh 映射为 max。
_EFFORT_MODELS = {"glm-5.2", "glm-5.3"}


def _thinking_body(enabled: bool) -> dict[str, Any]:
    return {
        "thinking": {
            "type": "enabled" if enabled else "disabled",
            "clear_thinking": False,
        }
    }


def _mapped_effort(model_id: str, effort: ThinkingEffort) -> str:
    if model_id == "glm-5.3":
        return {"none": "low", "minimal": "low", "low": "low", "medium": "high",
                "high": "high", "xhigh": "max", "max": "max"}[effort]
    return {"minimal": "disabled", "low": "high", "medium": "high",
            "xhigh": "max"}.get(effort, effort)


class ZhipuProvider(OpenAIChatCompletionsProvider):
    provider_type = "zhipu"
    default_base_url = "https://open.bigmodel.cn/api/paas/v4/"
    _DEFAULT_MODEL = "glm-5.1"

    def _apply_thinking(
        self,
        kwargs: dict[str, Any],
        effort: ThinkingEffort | None,
        model_id: str,
    ) -> None:
        if model_id not in _THINKING_MODELS:
            return
        if model_id in _EFFORT_MODELS and effort is not None:
            mapped = _mapped_effort(model_id, effort)
            if mapped == "disabled":
                kwargs["extra_body"] = _thinking_body(False)
                return
            kwargs["extra_body"] = _thinking_body(True)
            kwargs["reasoning_effort"] = mapped
            return
        if effort == "none":
            if model_id in _ALWAYS_THINKING_MODELS:
                logger.warning(
                    f"Model {model_id} always thinks; ignoring disabled thinking request"
                )
                kwargs["extra_body"] = _thinking_body(True)
                return
            kwargs["extra_body"] = _thinking_body(False)
            return
        kwargs["extra_body"] = _thinking_body(True)

    def list_models(self) -> list[str]:
        return sorted(_ZHIPU_MODELS)

    def supports_tool_use(self, model_id: str) -> bool:
        return True

    def supports_vision(self, model_id: str) -> bool:
        return model_id in _VISION_MODELS
