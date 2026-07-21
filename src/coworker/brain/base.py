from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from coworker.core.constants import DEFAULT_LLM_MAX_TOKENS
from coworker.core.token_utils import estimate_content_tokens
from coworker.core.types import LLMResponse, Message
from coworker.i18n import tr


def unsupported_image_fallback() -> str:
    return tr("attachment_fallback.unsupported_image")


def unsupported_video_fallback() -> str:
    return tr("attachment_fallback.unsupported_video")


def pdf_attachment_fallback(
    filename: object,
    saved_path: object = "",
    *,
    note: str = "",
) -> str:
    saved = tr("attachment_fallback.document_saved", path=saved_path) if saved_path else ""
    return tr(
        "attachment_fallback.pdf",
        filename=filename,
        note=note,
        saved=saved,
    )


class BaseLLMProvider(ABC):
    # provider_type: 类级常量，标识 API 方言/模型表（如 "zhipu"）。
    # provider_name: 实例属性，注册表 key，默认等于 provider_type，可被构造参数 name 覆盖，
    #                从而允许同一类型注册多个命名实例（如 "zhipu-userA" / "zhipu-userB"）。
    provider_type: str = ""
    provider_name: str

    # 该实例的默认模型：switch_model 切到本实例但未指定 model_id 时使用。
    # 由工厂从 ProviderSpec.default_model 赋值；空字符串表示未配置。
    default_model: str = ""

    # 子类按 provider_type 自动登记到这里，工厂据此实例化。无需手动维护任何映射。
    _TYPE_REGISTRY: dict[str, type[BaseLLMProvider]] = {}

    def __init_subclass__(cls, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
        if cls.provider_type:
            BaseLLMProvider._TYPE_REGISTRY[cls.provider_type] = cls
            # 类级默认，等于类型名；实例化时由 __init__ 的 name 覆盖。
            # 保证即便绕过 __init__（如测试 __new__）也能读到 provider_name。
            cls.provider_name = cls.provider_type

    def __init__(self, name: str | None = None) -> None:
        self.provider_name = name or type(self).provider_type

    @abstractmethod
    async def complete(
        self,
        messages: list[Message],
        system_prompt: str,
        tools: list[dict],
        max_tokens: int = DEFAULT_LLM_MAX_TOKENS,
        thinking: bool = True,
    ) -> LLMResponse: ...

    def set_model(self, model_id: str) -> None:
        """Select the model used by the next provider request."""
        raise NotImplementedError

    @abstractmethod
    def list_models(self) -> list[str]: ...

    @abstractmethod
    def supports_tool_use(self, model_id: str) -> bool: ...

    @abstractmethod
    def supports_vision(self, model_id: str) -> bool: ...

    def supports_video(self, model_id: str) -> bool:
        """Whether the model accepts native video input blocks."""
        return False

    def estimate_content_tokens(self, content: str | list[dict[str, Any]], model_id: str) -> int:
        """Estimate token cost for content under the given model.

        Calls _adapt_content first so that images degraded to text on non-vision
        models are counted as text (short), not as large base64 payloads.
        """
        adapted = self._adapt_content(content, model_id)
        return estimate_content_tokens(adapted)

    async def count_tokens(self, messages: list[Message], model_id: str) -> int:
        """Return the token count for a list of messages.

        Default implementation uses the heuristic estimator.  Subclasses that
        have a native token-counting API (Anthropic, OpenAI) override this.
        """
        return sum(self.estimate_content_tokens(m.content, model_id) for m in messages)

    def _adapt_content(
        self, content: str | list[dict[str, Any]], model_id: str
    ) -> str | list[dict[str, Any]]:
        """Convert internal Anthropic-style content blocks to this provider's format.

        Default implementation: if model doesn't support vision, replace image/document
        blocks with descriptive text. Subclasses override for format conversion.
        """
        if isinstance(content, str):
            return content
        if self.supports_vision(model_id):
            return content
        result = []
        for block in content:
            btype = block.get("type")
            if btype == "image":
                fname = block.get("_filename", tr("attachment_fallback.image_name"))
                path = block.get("_saved_path", "")
                saved = tr("attachment_fallback.image_saved", path=path) if path else ""
                text = tr("attachment_fallback.image", filename=fname, saved=saved)
                result.append({"type": "text", "text": text})
            elif btype == "document":
                fname = block.get("_filename", tr("attachment_fallback.document_name"))
                path = block.get("_saved_path", "")
                saved = tr("attachment_fallback.document_saved", path=path) if path else ""
                text = tr("attachment_fallback.document", filename=fname, saved=saved)
                result.append({"type": "text", "text": text})
            else:
                result.append({k: v for k, v in block.items() if not k.startswith("_")})
        return result
