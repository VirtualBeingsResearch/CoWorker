from __future__ import annotations

import hashlib
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

_TOOL_USE_MODELS = {
    # GPT-5.x series (current flagship and variants)
    "gpt-5.5",
    "gpt-5.5-pro",
    "gpt-5.4",
    "gpt-5.4-mini",
    "gpt-5.4-nano",
    "gpt-5.2",
    "gpt-5.2-pro",
    "gpt-5.1",
    "gpt-5.1-codex",
    "gpt-5.1-codex-max",
    "gpt-5.1-codex-mini",
    "gpt-5.1-chat-latest",
    "gpt-5",
    "gpt-5-pro",
    "gpt-5-mini",
    "gpt-5-codex",
    # GPT-4.x series
    "gpt-4.1",
    "gpt-4.1-mini",
    "gpt-4.1-nano",
    "gpt-4o",
    "gpt-4o-mini",
    # o-series (deprecated but still available)
    "o1",
    "o1-mini",
    "o1-pro",
    "o3",
    "o3-mini",
    "o3-pro",
    "o4-mini",
}

# All GPT-5.x and recent models support vision (text + image input).
_VISION_MODELS = {
    "gpt-5.5",
    "gpt-5.5-pro",
    "gpt-5.4",
    "gpt-5.4-mini",
    "gpt-5.4-nano",
    "gpt-5.2",
    "gpt-5.2-pro",
    "gpt-5.1",
    "gpt-5.1-codex",
    "gpt-5.1-codex-max",
    "gpt-5.1-codex-mini",
    "gpt-5.1-chat-latest",
    "gpt-5",
    "gpt-5-pro",
    "gpt-5-mini",
    "gpt-5-codex",
    "gpt-4.1",
    "gpt-4.1-mini",
    "gpt-4.1-nano",
    "gpt-4o",
    "gpt-4o-mini",
    "o3",
    "o3-pro",
    "o4-mini",
}

# Models that support reasoning via the Responses API reasoning param.
_REASONING_MODELS = {
    # GPT-5.x series all support configurable reasoning effort.
    "gpt-5.5",
    "gpt-5.5-pro",
    "gpt-5.4",
    "gpt-5.4-mini",
    "gpt-5.4-nano",
    "gpt-5.2",
    "gpt-5.2-pro",
    "gpt-5.1",
    "gpt-5.1-codex",
    "gpt-5.1-codex-max",
    "gpt-5.1-codex-mini",
    "gpt-5.1-chat-latest",
    "gpt-5",
    "gpt-5-pro",
    "gpt-5-mini",
    "gpt-5-codex",
    # o-series always reasons.
    "o1",
    "o1-mini",
    "o1-pro",
    "o3",
    "o3-mini",
    "o3-pro",
    "o4-mini",
}


class OpenAIProvider(BaseLLMProvider):
    provider_type = "openai"

    def __init__(self, api_key: str, base_url: str | None = None, name: str | None = None) -> None:
        super().__init__(name)
        self._client = openai.AsyncOpenAI(
            api_key=api_key,
            base_url=base_url,
            http_client=openai.DefaultAsyncHttpxClient(verify=shared_ssl_context()),
        )
        self._current_model = "gpt-4o"

    def supports_vision(self, model_id: str) -> bool:
        return model_id in _VISION_MODELS

    def _build_prompt_cache_key(
        self,
        system_prompt: str,
        tools: list[dict],
    ) -> str:
        payload = json.dumps(
            {
                "provider": self.provider_name,
                "model": self._current_model,
                "system_prompt": system_prompt,
                "tools": tools,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
        return f"coworker:openai:{digest}"

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
                    result.append({"type": "image_url", "image_url": data_url})
                else:
                    result.append(
                        {"type": "text", "text": "[图片附件 — 不支持的图片格式]"}
                    )
            elif btype == "document":
                fname = block.get("_filename", "文档")
                path = block.get("_saved_path", "")
                text = f"[PDF 附件: {fname} — OpenAI 不原生支持 PDF"
                if path:
                    text += f"，已保存至 {path}，可使用工具读取"
                text += "]"
                result.append({"type": "text", "text": text})
            else:
                result.append({k: v for k, v in block.items() if not k.startswith("_")})
        return result

    @staticmethod
    def _stringify_content(content) -> str:
        return content if isinstance(content, str) else json.dumps(content, ensure_ascii=False)

    @staticmethod
    def _response_text_block_type(role: str) -> str:
        return "output_text" if role == "assistant" else "input_text"

    def _to_responses_content_blocks(
        self,
        content: str | list[dict],
        role: str,
        model_id: str,
    ) -> list[dict]:
        adapted = self._adapt_content(content, model_id)
        text_block_type = self._response_text_block_type(role)
        if isinstance(adapted, str):
            return [{"type": text_block_type, "text": adapted}]

        blocks: list[dict] = []
        for block in adapted:
            if block.get("type") == "text":
                blocks.append({"type": text_block_type, "text": block["text"]})
            elif block.get("type") == "image_url":
                blocks.append({"type": "input_image", "image_url": block["image_url"]})
            else:
                blocks.append({"type": text_block_type, "text": self._stringify_content(block)})
        return blocks

    def _to_responses_tools(self, tools: list[dict]) -> list[dict]:
        result: list[dict] = []
        for tool in tools:
            result.append({
                "type": "function",
                "name": tool["name"],
                "description": tool.get("description", ""),
                "parameters": tool.get("parameters", {"type": "object", "properties": {}}),
            })
        return result

    @staticmethod
    def _extract_reasoning_content(response) -> str | None:
        parts = []
        for item in getattr(response, "output", []) or []:
            if getattr(item, "type", "") != "reasoning":
                continue
            for s in getattr(item, "summary", []) or []:
                text = getattr(s, "text", "") or ""
                if text:
                    parts.append(text)
        return "\n".join(parts) if parts else None

    @staticmethod
    def _parse_responses_tool_calls(response) -> list[ToolCall]:
        tool_calls: list[ToolCall] = []
        for item in getattr(response, "output", []) or []:
            if getattr(item, "type", "") != "function_call":
                continue
            name = getattr(item, "name", "")
            tool_calls.append(
                ToolCall(
                    id=getattr(item, "call_id", ""),
                    name=name,
                    arguments=_parse_tool_arguments(getattr(item, "arguments", "") or "{}", name),
                )
            )
        return tool_calls

    @staticmethod
    def _extract_usage(response) -> dict[str, int]:
        usage = getattr(response, "usage", None)
        return {
            "input_tokens": getattr(usage, "input_tokens", 0) or 0,
            "output_tokens": getattr(usage, "output_tokens", 0) or 0,
            "cached_tokens": (
                getattr(
                    getattr(usage, "input_tokens_details", None),
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
        try:
            input_items, _ = self._to_responses_input(messages, self._current_model)
            kwargs: dict = {
                "model": self._current_model,
                "input": input_items,
                "instructions": system_prompt,
                "max_output_tokens": max_tokens,
                "prompt_cache_key": self._build_prompt_cache_key(system_prompt, tools),
            }
            if tools:
                kwargs["tools"] = self._to_responses_tools(tools)
            if self._current_model in _REASONING_MODELS and thinking:
                kwargs["reasoning"] = {"effort": "high", "summary": "auto"}
            elif not thinking:
                kwargs["reasoning"] = {"effort": "none"}
            response = await self._client.responses.create(**kwargs)
        except openai.APIError as e:
            raise ProviderError(str(e)) from e

        tool_calls = self._parse_responses_tool_calls(response)
        usage = self._extract_usage(response)
        reasoning_content = self._extract_reasoning_content(response)

        return LLMResponse(
            content=getattr(response, "output_text", "") or "",
            tool_calls=tool_calls,
            stop_reason="tool_use" if tool_calls else "end_turn",
            model=getattr(response, "model", self._current_model),
            usage=usage,
            reasoning_content=reasoning_content,
        )

    def _to_responses_input(
        self, messages: list[Message], model_id: str
    ) -> tuple[list[dict], str]:
        """Convert conversation history to Responses API input items."""
        instructions = ""
        input_items: list[dict] = []
        for m in messages:
            if m.role == "system":
                instructions = m.content if isinstance(m.content, str) else ""
                continue
            if m.role == "tool":
                input_items.append(
                    {
                        "type": "function_call_output",
                        "call_id": m.tool_call_id or "",
                        "output": self._stringify_content(m.content),
                    }
                )
                continue
            if m.role == "assistant":
                if m.reasoning_content:
                    rc_id = "rs_" + hashlib.sha256(m.reasoning_content.encode()).hexdigest()[:24]
                    input_items.append({
                        "type": "reasoning",
                        "id": rc_id,
                        "summary": [{"type": "summary_text", "text": m.reasoning_content}],
                    })
                if m.content:
                    input_items.append({
                        "role": "assistant",
                        "content": self._to_responses_content_blocks(m.content, m.role, model_id),
                    })
                for tc in m.tool_calls:
                    function = tc.get("function", {})
                    input_items.append({
                        "type": "function_call",
                        "call_id": tc.get("id", ""),
                        "name": function.get("name", ""),
                        "arguments": function.get("arguments", "{}"),
                    })
                continue
            input_items.append({
                "role": "user",
                "content": self._to_responses_content_blocks(m.content, m.role, model_id),
            })
        return input_items, instructions

    async def count_tokens(self, messages: list[Message], model_id: str) -> int:
        try:
            input_items, instructions = self._to_responses_input(messages, model_id)
            kwargs: dict = {"model": model_id, "input": input_items}
            if instructions:
                kwargs["instructions"] = instructions
            result = await self._client.responses.input_tokens.count(**kwargs)
            return result.input_tokens
        except Exception:
            return await super().count_tokens(messages, model_id)

    def set_model(self, model_id: str) -> None:
        self._current_model = model_id

    def list_models(self) -> list[str]:
        return sorted(_TOOL_USE_MODELS)

    def supports_tool_use(self, model_id: str) -> bool:
        return model_id in _TOOL_USE_MODELS
