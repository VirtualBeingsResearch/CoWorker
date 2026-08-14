from __future__ import annotations

from typing import Any

from coworker.brain.any_llm_provider import AnyLLMProvider, parse_tool_arguments
from coworker.brain.base import pdf_attachment_fallback, unsupported_image_fallback
from coworker.core.types import Message, ThinkingMode, thinking_enabled
from coworker.i18n import tr

_parse_tool_arguments = parse_tool_arguments

_MINIMAX_MODELS = {"MiniMax-M3"}
_VISION_MODELS = {"MiniMax-M3"}
_THINKING_MODELS = _MINIMAX_MODELS


class MiniMaxProvider(AnyLLMProvider):
    provider_type = "minimax"
    api_dialect = "openai"
    any_llm_provider = "minimax"
    default_base_url = "https://api.minimaxi.com/v1"
    initial_model = "MiniMax-M3"

    def _message_role(self, message: Message) -> str:
        return "user" if message.role == "system" else message.role

    def _completion_options(self, thinking: ThinkingMode) -> dict[str, Any]:
        return {
            "reasoning_effort": None,
            "extra_body": {
                "reasoning_split": True,
                "thinking": "adaptive" if thinking_enabled(thinking) else "disabled",
            },
        }

    def list_models(self) -> list[str]:
        return sorted(_MINIMAX_MODELS)

    def supports_tool_use(self, model_id: str) -> bool:
        return model_id in _MINIMAX_MODELS

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
