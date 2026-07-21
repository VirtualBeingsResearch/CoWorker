from __future__ import annotations

import json

import openai
from loguru import logger

from coworker.brain.base import BaseLLMProvider
from coworker.brain.tls import shared_ssl_context
from coworker.core.constants import DEFAULT_LLM_MAX_TOKENS
from coworker.core.exceptions import ProviderError
from coworker.core.types import LLMResponse, Message, ToolCall


def _parse_tool_arguments(raw: str, tool_name: str) -> dict:
    try:
        return json.loads(raw)
    except json.JSONDecodeError as e:
        logger.warning(f"Failed to parse tool call arguments for '{tool_name}': {raw!r}")
        return {"__parse_error__": str(e), "__raw_arguments__": raw}

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

# Qwen3 models support extended thinking via enable_thinking extra_body param.
_THINKING_MODELS = _QWEN_MODELS

_DEFAULT_BASE_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1"


class QwenProvider(BaseLLMProvider):
    provider_type = "qwen"

    def __init__(self, api_key: str, base_url: str | None = None, name: str | None = None) -> None:
        super().__init__(name)
        self._client = openai.AsyncOpenAI(
            api_key=api_key,
            base_url=base_url or _DEFAULT_BASE_URL,
            http_client=openai.DefaultAsyncHttpxClient(verify=shared_ssl_context()),
        )
        self._current_model = "qwen-plus"

    @staticmethod
    def _extract_usage(response) -> dict[str, int]:
        usage = getattr(response, "usage", None)
        return {
            "input_tokens": getattr(usage, "prompt_tokens", 0) or 0,
            "output_tokens": getattr(usage, "completion_tokens", 0) or 0,
            "cached_tokens": (
                getattr(
                    getattr(usage, "prompt_tokens_details", None),
                    "cached_tokens",
                    0,
                )
                if usage
                else 0
            ),
        }

    async def complete(
        self,
        messages: list[Message],
        system_prompt: str,
        tools: list[dict],
        max_tokens: int = DEFAULT_LLM_MAX_TOKENS,
        thinking: bool = True,
    ) -> LLMResponse:
        api_messages: list[dict] = [{"role": "system", "content": system_prompt}]
        for m in messages:
            d = m.to_dict()
            if m.role == "user":
                d["content"] = self._adapt_content(m.content, self._current_model)
            api_messages.append(d)

        kwargs: dict = {
            "model": self._current_model,
            "max_tokens": max_tokens,
            "messages": api_messages,
        }
        if tools:
            kwargs["tools"] = [{"type": "function", "function": t} for t in tools]
        if self._current_model in _THINKING_MODELS and thinking:
            kwargs["extra_body"] = {"enable_thinking": True}
        elif not thinking:
            kwargs["extra_body"] = {"enable_thinking": False}

        try:
            response = await self._client.chat.completions.create(**kwargs)
        except openai.APIError as e:
            raise ProviderError(str(e)) from e

        choice = response.choices[0]
        msg = choice.message

        tool_calls: list[ToolCall] = []
        if msg.tool_calls:
            for tc in msg.tool_calls:
                tool_calls.append(
                    ToolCall(
                        id=tc.id,
                        name=tc.function.name,
                        arguments=_parse_tool_arguments(tc.function.arguments, tc.function.name),
                    )
                )

        reasoning_content: str | None = getattr(msg, "reasoning_content", None) or None

        return LLMResponse(
            content=msg.content or "",
            tool_calls=tool_calls,
            stop_reason="tool_use" if tool_calls else "end_turn",
            model=response.model,
            usage=self._extract_usage(response),
            reasoning_content=reasoning_content,
        )

    def set_model(self, model_id: str) -> None:
        self._current_model = model_id

    def list_models(self) -> list[str]:
        return sorted(_QWEN_MODELS)

    def supports_tool_use(self, model_id: str) -> bool:
        return model_id in _QWEN_MODELS

    def supports_vision(self, model_id: str) -> bool:
        return model_id in _VISION_MODELS or model_id.endswith("-plus")

    def supports_video(self, model_id: str) -> bool:
        return model_id in _VIDEO_MODELS

    def _adapt_content(self, content, model_id):
        if isinstance(content, str):
            return content
        if not self.supports_vision(model_id):
            return super()._adapt_content(content, model_id)
        result = []
        for block in content:
            btype = block.get("type")
            if btype == "image":
                src = block.get("source", {})
                if src.get("type") == "base64":
                    data_url = f"data:{src['media_type']};base64,{src['data']}"
                    result.append({"type": "image_url", "image_url": {"url": data_url}})
                else:
                    result.append({"type": "text", "text": "[图片附件 — 不支持的图片格式]"})
            elif btype == "video":
                src = block.get("source", {})
                if self.supports_video(model_id) and src.get("type") == "base64":
                    data_url = f"data:{src['media_type']};base64,{src['data']}"
                    result.append({"type": "video_url", "video_url": {"url": data_url}})
                else:
                    result.append({
                        "type": "text",
                        "text": "[视频附件 — 当前模型不支持原生视频输入]",
                    })
            elif btype == "document":
                fname = block.get("_filename", "文档")
                path = block.get("_saved_path", "")
                text = f"[PDF 附件: {fname}"
                if path:
                    text += f"，已保存至 {path}，可使用工具读取"
                text += "]"
                result.append({"type": "text", "text": text})
            else:
                result.append({k: v for k, v in block.items() if not k.startswith("_")})
        return result
