from __future__ import annotations

from typing import Any

from coworker.brain.any_llm_provider import AnyLLMProvider, parse_tool_arguments
from coworker.brain.base import (
    pdf_attachment_fallback,
    unsupported_image_fallback,
    unsupported_video_fallback,
)
from coworker.core.types import ThinkingMode, reasoning_effort
from coworker.i18n import tr

_parse_tool_arguments = parse_tool_arguments

_QWEN_MODELS = {
    "qwen3.6-flash",
    "qwen3.6-plus",
    "qwen3.6-max-preview",
    "qwen3.7-plus",
    "qwen3.7-max",
}
_VISION_MODELS = {"qwen3.6-plus", "qwen3.7-plus"}
_VIDEO_MODELS = {"qwen3.6-plus", "qwen3.7-plus"}
_THINKING_MODELS = _QWEN_MODELS
_THINKING_BUDGETS = {
    "minimal": 1024,
    "low": 2048,
    "medium": 4096,
    "high": 8192,
    "xhigh": 16384,
    "max": 32768,
}


class QwenProvider(AnyLLMProvider):
    provider_type = "qwen"
    api_dialect = "openai"
    any_llm_provider = "dashscope"
    default_base_url = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    initial_model = "qwen-plus"

    def _completion_options(self, thinking: ThinkingMode) -> dict[str, Any]:
        effort = reasoning_effort(thinking)
        extra_body: dict[str, Any] = {"enable_thinking": effort != "none"}
        if not isinstance(thinking, bool) and effort in _THINKING_BUDGETS:
            extra_body["thinking_budget"] = _THINKING_BUDGETS[effort]
        return {
            "reasoning_effort": None,
            "extra_body": extra_body,
        }

    def list_models(self) -> list[str]:
        return sorted(_QWEN_MODELS)

    def supports_tool_use(self, model_id: str) -> bool:
        return model_id in _QWEN_MODELS

    def supports_vision(self, model_id: str) -> bool:
        return model_id in _VISION_MODELS or model_id.endswith("-plus")

    def supports_video(self, model_id: str) -> bool:
        return model_id in _VIDEO_MODELS

    def _adapt_content(
        self,
        content: str | list[dict[str, Any]],
        model_id: str,
    ) -> str | list[dict[str, Any]]:
        if isinstance(content, str):
            return content
        if not self.can_use_vision(model_id):
            return super()._adapt_content(content, model_id)
        result: list[dict[str, Any]] = []
        for block in content:
            block_type = block.get("type")
            if block_type == "image":
                source = block.get("source", {})
                if source.get("type") == "base64":
                    data_url = f"data:{source['media_type']};base64,{source['data']}"
                    result.append({"type": "image_url", "image_url": {"url": data_url}})
                else:
                    result.append({"type": "text", "text": unsupported_image_fallback()})
            elif block_type == "video":
                source = block.get("source", {})
                if self.can_use_video(model_id) and source.get("type") == "base64":
                    data_url = f"data:{source['media_type']};base64,{source['data']}"
                    result.append({"type": "video_url", "video_url": {"url": data_url}})
                else:
                    result.append({"type": "text", "text": unsupported_video_fallback()})
            elif block_type == "document":
                filename = block.get("_filename", tr("attachment_fallback.document_name"))
                result.append(
                    {
                        "type": "text",
                        "text": pdf_attachment_fallback(filename, block.get("_saved_path", "")),
                    }
                )
            else:
                result.append({key: value for key, value in block.items() if not key.startswith("_")})
        return result
