from __future__ import annotations

from typing import Any
from uuid import uuid4

from coworker.brain.openai_chat import OpenAIChatCompletionsProvider
from coworker.brain.openai_provider import OpenAIProvider
from coworker.brain.thinking import ThinkingEffort
from coworker.core.constants import DEFAULT_LLM_MAX_TOKENS
from coworker.core.types import LLMResponse, Message
from coworker.version import __version__

# Official model catalog served by OpenCode Go's /v1 endpoint. MiniMax/Qwen
# models use its Anthropic-compatible endpoint and are not exposed by this
# provider; the sets below cover chat.completions and Responses separately.
_CHAT_COMPLETIONS_MODELS = {
    "deepseek-v4-flash",
    "deepseek-v4-flash-vision-exp",
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

# These stable additions use the OpenAI Responses API on the same /v1 base URL.
# Newly listed experimental, contributor/region-limited, and limited-time
# entries intentionally remain dynamic-only rather than recommended here.
_RESPONSES_MODELS = {
    "gpt-5.6-luna",
    "grok-4.5",
}

_OPENCODE_GO_MODELS = _CHAT_COMPLETIONS_MODELS | _RESPONSES_MODELS

_VISION_MODELS = {
    "deepseek-v4-flash-vision-exp",
    "kimi-k3",
}

_DEEPSEEK_MODELS = {
    "deepseek-v4-flash",
    "deepseek-v4-flash-vision-exp",
    "deepseek-v4-pro",
}

# Kimi K2.6/K2.7 支持 low/medium/high；K3 只支持 high/max。
_KIMI_MEDIUM_MODELS = {"kimi-k2.6", "kimi-k2.7-code"}
_KIMI_MAX_MODELS = {"kimi-k3"}


def _new_opencode_session_id() -> str:
    return f"coworker:{uuid4()}"


def _opencode_go_headers(session_id: str) -> dict[str, str]:
    return {
        "User-Agent": f"Coworker/{__version__}",
        "x-opencode-session": session_id,
    }


def _with_opencode_headers(client: Any, session_id: str) -> Any:
    bind = getattr(client, "with_options", None)
    if not callable(bind):
        return client
    return bind(default_headers=_opencode_go_headers(session_id))


def _apply_opencode_session_header(provider: Any, kwargs: dict[str, Any]) -> None:
    session_id = getattr(provider, "_opencode_session_id", None)
    if not isinstance(session_id, str) or not session_id:
        session_id = _new_opencode_session_id()
        provider._opencode_session_id = session_id
    kwargs["extra_headers"] = _opencode_go_headers(session_id)


class _OpenCodeGoResponsesProvider(OpenAIProvider):
    """Responses adapter without registering a second public provider type."""

    provider_type = ""

    def __init__(
        self,
        api_key: str,
        base_url: str | None = None,
        name: str | None = None,
        session_id: str | None = None,
    ) -> None:
        super().__init__(api_key, base_url=base_url, name=name)
        self._opencode_session_id = session_id or _new_opencode_session_id()
        self._client = _with_opencode_headers(self._client, self._opencode_session_id)

    def _apply_reasoning(self, kwargs: dict, effort) -> None:
        _apply_opencode_session_header(self, kwargs)
        # OpenCode documents Luna's standard Responses reasoning contract.
        # Grok has no stable public effort contract through Go, so leave its
        # request shape untouched and let the gateway select its default.
        if self._current_model != "gpt-5.6-luna":
            return
        if effort == "none":
            kwargs["reasoning"] = {"effort": "none"}
        else:
            kwargs["reasoning"] = {
                "effort": effort if effort is not None else "high",
                "summary": "auto",
            }


class OpenCodeGoProvider(OpenAIChatCompletionsProvider):
    provider_type = "opencode-go"
    default_base_url = "https://opencode.ai/zen/go/v1"
    _DEFAULT_MODEL = "deepseek-v4-flash"

    def __init__(
        self,
        api_key: str,
        base_url: str | None = None,
        name: str | None = None,
    ) -> None:
        self._opencode_session_id = _new_opencode_session_id()
        resolved_base_url = self.resolve_base_url(base_url)
        super().__init__(api_key, base_url=resolved_base_url, name=name)
        self._client = _with_opencode_headers(self._client, self._opencode_session_id)
        self._responses_provider = _OpenCodeGoResponsesProvider(
            api_key,
            base_url=resolved_base_url,
            name=self.provider_name,
            session_id=self._opencode_session_id,
        )

    async def complete(
        self,
        messages: list[Message],
        system_prompt: str,
        tools: list[dict],
        max_tokens: int = DEFAULT_LLM_MAX_TOKENS,
        thinking: bool = True,
        thinking_effort: str | None = None,
    ) -> LLMResponse:
        if self._current_model in _RESPONSES_MODELS:
            return await self._responses_provider.complete(
                messages,
                system_prompt,
                tools,
                max_tokens=max_tokens,
                thinking=thinking,
                thinking_effort=thinking_effort,
            )
        return await super().complete(
            messages,
            system_prompt,
            tools,
            max_tokens=max_tokens,
            thinking=thinking,
            thinking_effort=thinking_effort,
        )

    def set_model(self, model_id: str) -> None:
        super().set_model(model_id)
        self._responses_provider.set_model(model_id)

    async def count_tokens(self, messages: list[Message], model_id: str) -> int:
        if model_id in _RESPONSES_MODELS:
            return await self._responses_provider.count_tokens(messages, model_id)
        return await super().count_tokens(messages, model_id)

    def _apply_thinking(
        self,
        kwargs: dict[str, Any],
        effort: ThinkingEffort | None,
        model_id: str,
    ) -> None:
        _apply_opencode_session_header(self, kwargs)
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
