from __future__ import annotations

from typing import Any

from coworker.brain.any_llm_provider import AnyLLMProvider, parse_tool_arguments
from coworker.brain.base import pdf_attachment_fallback, unsupported_image_fallback
from coworker.core.types import ThinkingMode, thinking_enabled
from coworker.i18n import tr

_parse_tool_arguments = parse_tool_arguments

_ZHIPU_MODELS = {
    "glm-4.5-air",
    "glm-4.7",
    "glm-5",
    "glm-5v-turbo",
    "glm-5.1",
}
_VISION_MODELS = {"glm-4v", "glm-4v-plus", "glm-4v-flash", "glm-5v-turbo"}
_THINKING_MODELS = {"glm-5.1", "glm-5", "glm-5v-turbo"}


class ZhipuProvider(AnyLLMProvider):
    provider_type = "zhipu"
    api_dialect = "openai"
    any_llm_provider = "zai"
    default_base_url = "https://open.bigmodel.cn/api/paas/v4/"
    initial_model = "glm-5.1"

    def _completion_options(self, thinking: ThinkingMode) -> dict[str, Any]:
        return {
            "reasoning_effort": None,
            "extra_body": {
                "thinking": {
                    "type": "enabled" if thinking_enabled(thinking) else "disabled",
                    "clear_thinking": False,
                }
            },
        }

    def list_models(self) -> list[str]:
        return sorted(_ZHIPU_MODELS)

    def supports_tool_use(self, model_id: str) -> bool:
        return True

    def supports_vision(self, model_id: str) -> bool:
        return model_id in _VISION_MODELS

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
