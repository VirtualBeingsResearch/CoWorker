from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from typing import Any

from loguru import logger

from coworker.brain.base import BaseLLMProvider
from coworker.brain.thinking import normalize_thinking_effort
from coworker.core.constants import DEFAULT_LLM_MAX_TOKENS
from coworker.core.exceptions import ModelNotSupportedError, ProviderNotFoundError
from coworker.core.types import LLMResponse, Message, SummaryResult
from coworker.i18n import tr

_WEEKDAY_KEYS = [
    "calendar.monday",
    "calendar.tuesday",
    "calendar.wednesday",
    "calendar.thursday",
    "calendar.friday",
    "calendar.saturday",
    "calendar.sunday",
]

# 单个 Provider 远端模型目录拉取的最长等待时间；超时按失败缓存，不阻塞管理页面。
_MODEL_CATALOG_FETCH_TIMEOUT = 30.0

# 显式切换模型会打断在途主线调用；连续切换时限制重试次数，避免无限循环。
_MAX_MODEL_SWITCH_INTERRUPTS = 8


class ModelCallInterrupted(Exception):
    """An explicit model switch aborted this in-flight provider call."""

    def __init__(self, provider: str, model: str) -> None:
        super().__init__(
            tr("brain.switch.call_interrupted", provider=provider, model=model)
        )
        self.provider = provider
        self.model = model


@dataclass(eq=False)
class _InflightCall:
    """One main-model provider call that an explicit switch may abort."""

    task: asyncio.Task[Any]
    interrupted: bool = False


def _prepend_timestamps(messages: list[Message]) -> list[Message]:
    """Return copies of user messages with their own timestamp prepended.

    Uses each message's creation timestamp (not now()), so historical messages
    keep a stable prefix. Original Message objects are never mutated.
    """
    out: list[Message] = []
    for m in messages:
        if m.role != "user":
            out.append(m)
            continue
        ts = m.timestamp
        prefix = f"[{ts.strftime('%Y-%m-%d %H:%M:%S')} {tr(_WEEKDAY_KEYS[ts.weekday()])}] "
        if isinstance(m.content, str):
            new_content: str | list = prefix + m.content
        else:  # content blocks (user message with attachments)
            new_content = [{"type": "text", "text": prefix.rstrip()}, *m.content]
        out.append(replace(m, content=new_content))
    return out


class Brain:
    def __init__(
        self,
        default_provider: str,
        default_model: str,
        message_time_prefix: bool = True,
        max_tokens: int = DEFAULT_LLM_MAX_TOKENS,
        fallbacks: list[str] | None = None,
        thinking: bool = True,
        thinking_effort: str = "",
        summary_provider: str = "",
        summary_model: str = "",
        summary_thinking: bool = False,
        summary_thinking_effort: str = "",
        vision_provider: str = "",
        vision_model: str = "",
        vision_thinking: bool = True,
        vision_thinking_effort: str = "",
    ) -> None:
        self._providers: dict[str, BaseLLMProvider] = {}
        self._active_provider_name = default_provider
        self._active_model = default_model
        self._summary_provider_name = summary_provider
        self._summary_model = summary_model
        self._summary_thinking = summary_thinking
        self._summary_thinking_effort = normalize_thinking_effort(summary_thinking_effort) or ""
        self._vision_provider_name = vision_provider
        self._vision_model = vision_model
        self._vision_thinking = vision_thinking
        self._vision_thinking_effort = normalize_thinking_effort(vision_thinking_effort) or ""
        self._message_time_prefix = message_time_prefix
        self._max_tokens = max_tokens
        self._thinking = thinking
        self._thinking_effort = normalize_thinking_effort(thinking_effort) or ""
        self._lock = asyncio.Lock()
        self._model_switch_listener_lock = asyncio.Lock()
        # 降级链原始配置（"name" 或 "name/model"）；运行时按当前注册表解析，构造时不解析。
        self._fallbacks = list(fallbacks or [])
        # 上次切换是否由主模型失败触发（区别于用户/模型主动 switch_model），供主循环措辞使用。
        self._last_switch_was_fallback = False
        self._summary_usage_listeners: list[Callable[[LLMResponse, dict[str, Any]], None]] = []
        self._vision_usage_listeners: list[Callable[[LLMResponse, dict[str, Any]], None]] = []
        self._model_switch_listeners: list[Callable[[str, str], Awaitable[None]]] = []
        # 显式切换模型时打断在途主线调用，避免卡死的 Provider 请求拖住主循环。
        self._inflight_calls: set[_InflightCall] = set()
        self._switch_generation = 0

    @property
    def message_time_prefix(self) -> bool:
        return self._message_time_prefix

    @property
    def max_tokens(self) -> int:
        return self._max_tokens

    @property
    def thinking(self) -> bool:
        return self._thinking

    @property
    def thinking_effort(self) -> str:
        return self._thinking_effort

    def register_provider(self, provider: BaseLLMProvider) -> None:
        self._providers[provider.provider_name] = provider
        logger.info(f"Registered LLM provider: {provider.provider_name}")

    async def upsert_provider(self, provider: BaseLLMProvider) -> None:
        """Atomically add or replace a provider for subsequent model calls.

        In-flight calls keep their local provider reference. Replacing the active
        connection is rejected when it cannot serve the currently active model.
        """
        async with self._lock:
            if (
                provider.provider_name == self._active_provider_name
                and not provider.can_use_tools(self._active_model)
            ):
                raise ModelNotSupportedError(
                    tr(
                        "brain.validation.active_model_unsupported",
                        provider=provider.provider_name,
                        model=self._active_model,
                    )
                )
            self._providers[provider.provider_name] = provider
            logger.info(f"Hot-updated LLM provider: {provider.provider_name}")

    def set_max_tokens(self, value: int) -> None:
        if value <= 0:
            raise ValueError(tr("brain.validation.max_tokens_positive"))
        self._max_tokens = value

    def add_summary_usage_listener(
        self,
        fn: Callable[[LLMResponse, dict[str, Any]], None],
    ) -> None:
        self._summary_usage_listeners.append(fn)

    def add_vision_usage_listener(
        self,
        fn: Callable[[LLMResponse, dict[str, Any]], None],
    ) -> None:
        self._vision_usage_listeners.append(fn)

    def add_model_switch_listener(
        self,
        fn: Callable[[str, str], Awaitable[None]],
    ) -> None:
        """Register an async observer for persistent active-model changes."""
        self._model_switch_listeners.append(fn)

    def inherit_usage_listeners_from(self, other: Brain) -> None:
        self._summary_usage_listeners.extend(other._summary_usage_listeners)
        self._vision_usage_listeners.extend(other._vision_usage_listeners)

    def _notify_usage_listeners(
        self,
        listeners: list[Callable[[LLMResponse, dict[str, Any]], None]],
        response: LLMResponse,
        metadata: dict[str, Any],
    ) -> None:
        for fn in listeners:
            try:
                fn(response, metadata)
            except Exception as e:
                logger.warning(f"Brain usage listener raised, ignored: {e}")

    async def _notify_model_switch_listeners(self, provider: str, model: str) -> None:
        # Serialize notifications so concurrent switches cannot leave observers on
        # an older model after a newer switch has already completed.
        async with self._model_switch_listener_lock:
            if (provider, model) != (self._active_provider_name, self._active_model):
                return
            for fn in self._model_switch_listeners:
                try:
                    await fn(provider, model)
                except Exception as e:
                    logger.warning(
                        tr(
                            "brain.listeners.model_switch_failed",
                            provider=provider,
                            model=model,
                            error=e,
                        )
                    )

    def list_providers(self) -> list[str]:
        """已注册的 provider 实例名（注册表 key）。"""
        return sorted(self._providers)

    @property
    def current_provider_name(self) -> str:
        return self._active_provider_name

    @property
    def current_model(self) -> str:
        return self._active_model

    @property
    def summary_provider_name(self) -> str:
        return self._summary_provider_name

    @property
    def summary_model(self) -> str:
        return self._summary_model

    @property
    def summary_thinking(self) -> bool:
        return self._summary_thinking

    @property
    def summary_thinking_effort(self) -> str:
        return self._summary_thinking_effort

    @property
    def fallbacks(self) -> list[str]:
        return list(self._fallbacks)

    @property
    def vision_provider_name(self) -> str:
        return self._vision_provider_name

    @property
    def vision_model(self) -> str:
        return self._vision_model

    @property
    def vision_thinking(self) -> bool:
        return self._vision_thinking

    @property
    def vision_thinking_effort(self) -> str:
        return self._vision_thinking_effort

    @property
    def active_provider(self) -> BaseLLMProvider | None:
        return self._providers.get(self._active_provider_name)

    def consume_fallback_switch(self) -> bool:
        """读并复位「上次切换是否为失败降级」标志。主循环用它决定切换通知的措辞。"""
        flag = self._last_switch_was_fallback
        self._last_switch_was_fallback = False
        return flag

    @property
    def current_model_has_vision(self) -> bool:
        provider = self.active_provider
        return bool(provider and provider.can_use_vision(self._active_model))

    def _fallback_model_for(self, entry: str) -> tuple[str, str]:
        entry = entry.strip()
        if not entry:
            raise ValueError(tr("brain.validation.fallback_empty"))
        name, sep, model = entry.partition("/")
        if not name or (sep and not model) or "/" in model:
            raise ValueError(tr("brain.validation.fallback_invalid", entry=repr(entry)))
        provider = self._providers.get(name)
        if provider is None:
            raise ProviderNotFoundError(name)
        model = model or provider.default_model
        if not model:
            raise ModelNotSupportedError(
                tr("brain.validation.fallback_model_missing", provider=name)
            )
        if not provider.can_use_tools(model):
            raise ModelNotSupportedError(
                tr(
                    "brain.validation.fallback_tool_unsupported",
                    model=model,
                    provider=name,
                )
            )
        return name, model

    def _validate_fallbacks(self, fallbacks: list[str]) -> list[str]:
        normalized: list[str] = []
        for entry in fallbacks:
            text = str(entry).strip()
            self._fallback_model_for(text)
            normalized.append(text)
        return normalized

    def _validate_summary_config(self, provider_name: str, model: str) -> None:
        if not provider_name:
            return
        provider = self._providers.get(provider_name)
        if provider is None:
            raise ProviderNotFoundError(provider_name)
        if not model and not provider.default_model:
            raise ModelNotSupportedError(
                tr("brain.validation.summary_model_missing", provider=provider_name)
            )

    def _validate_vision_config(self, provider_name: str, model: str) -> None:
        if not provider_name and not model:
            return
        if not provider_name or not model:
            raise ValueError(tr("brain.validation.vision_pair"))
        provider = self._providers.get(provider_name)
        if provider is None:
            raise ProviderNotFoundError(provider_name)
        if not provider.can_use_vision(model):
            raise ModelNotSupportedError(
                tr(
                    "brain.validation.vision_unsupported",
                    model=model,
                    provider=provider_name,
                )
            )

    def model_config_snapshot(self) -> dict[str, Any]:
        return {
            "providers": self.list_providers(),
            "active": {
                "provider": self._active_provider_name,
                "model": self._active_model,
                "thinking": self._thinking,
                "thinking_effort": self._thinking_effort,
            },
            "thinking_effort": self._thinking_effort,
            "summary": {
                "provider": self._summary_provider_name,
                "model": self._summary_model,
                "thinking": self._summary_thinking,
                "thinking_effort": self._summary_thinking_effort,
            },
            "fallbacks": list(self._fallbacks),
            "vision": {
                "provider": self._vision_provider_name,
                "model": self._vision_model,
                "thinking": self._vision_thinking,
                "thinking_effort": self._vision_thinking_effort,
                "enabled": bool(self._vision_provider_name and self._vision_model),
            },
        }

    def _catalog_providers(self, provider_name: str | None = None) -> list[BaseLLMProvider]:
        if provider_name is None:
            return list(self._providers.values())
        provider = self._providers.get(provider_name)
        if provider is None:
            raise ProviderNotFoundError(provider_name)
        return [provider]

    def model_catalog_snapshot(self, provider_name: str | None = None) -> dict[str, Any]:
        """Static + cached-remote model catalogs for registered providers."""
        return {
            "providers": [
                provider.model_catalog()
                for provider in self._catalog_providers(provider_name)
            ]
        }

    async def refresh_model_catalog(self, provider_name: str | None = None) -> dict[str, Any]:
        """Fetch remote model lists for one or all registered providers."""
        providers = self._catalog_providers(provider_name)

        async def refresh_one(provider: BaseLLMProvider) -> None:
            try:
                await asyncio.wait_for(
                    provider.fetch_models(),
                    timeout=_MODEL_CATALOG_FETCH_TIMEOUT,
                )
            except Exception as e:
                provider.mark_remote_models_error(e)

        await asyncio.gather(*(refresh_one(provider) for provider in providers))
        return self.model_catalog_snapshot(provider_name)

    async def update_model_config(
        self,
        *,
        thinking_effort: str | None = None,
        summary_provider: str | None = None,
        summary_model: str | None = None,
        summary_thinking: bool | None = None,
        summary_thinking_effort: str | None = None,
        fallbacks: list[str] | None = None,
        vision_provider: str | None = None,
        vision_model: str | None = None,
        vision_thinking: bool | None = None,
        vision_thinking_effort: str | None = None,
    ) -> dict[str, Any]:
        next_thinking_effort = (
            self._thinking_effort
            if thinking_effort is None
            else normalize_thinking_effort(thinking_effort) or ""
        )
        next_summary_provider = (
            self._summary_provider_name if summary_provider is None else summary_provider.strip()
        )
        next_summary_model = self._summary_model if summary_model is None else summary_model.strip()
        next_summary_thinking = (
            self._summary_thinking if summary_thinking is None else bool(summary_thinking)
        )
        next_summary_thinking_effort = (
            self._summary_thinking_effort
            if summary_thinking_effort is None
            else normalize_thinking_effort(summary_thinking_effort) or ""
        )
        next_fallbacks = (
            self._fallbacks if fallbacks is None else self._validate_fallbacks(fallbacks)
        )
        next_vision_provider = (
            self._vision_provider_name if vision_provider is None else vision_provider.strip()
        )
        next_vision_model = self._vision_model if vision_model is None else vision_model.strip()
        next_vision_thinking = (
            self._vision_thinking if vision_thinking is None else bool(vision_thinking)
        )
        next_vision_thinking_effort = (
            self._vision_thinking_effort
            if vision_thinking_effort is None
            else normalize_thinking_effort(vision_thinking_effort) or ""
        )

        self._validate_summary_config(next_summary_provider, next_summary_model)
        self._validate_vision_config(next_vision_provider, next_vision_model)

        async with self._lock:
            self._thinking_effort = next_thinking_effort
            self._summary_provider_name = next_summary_provider
            self._summary_model = next_summary_model
            self._summary_thinking = next_summary_thinking
            self._summary_thinking_effort = next_summary_thinking_effort
            self._fallbacks = list(next_fallbacks)
            self._vision_provider_name = next_vision_provider
            self._vision_model = next_vision_model
            self._vision_thinking = next_vision_thinking
            self._vision_thinking_effort = next_vision_thinking_effort
        return self.model_config_snapshot()

    async def count_tokens(self, messages: list[Message]) -> int:
        """Count tokens for messages using the active provider's native API if available."""
        provider = self.active_provider
        if provider is None:
            from coworker.core.token_utils import estimate_content_tokens

            return sum(estimate_content_tokens(m.content) for m in messages)
        return await provider.count_tokens(messages, self._active_model)

    def _resolve_candidates(self) -> list[tuple[str, str, int]]:
        """构造降级候选链 [(provider_name, model, tries), ...]，首选为当前 active。

        按运行时 active（可能已被 switch_model 或上次降级改过）去重；fallback 项按当前
        注册表解析：未注册 / 无可用模型 / 模型不支持 tool use / 与 active 重复者跳过。
        """
        active = (self._active_provider_name, self._active_model)
        candidates: list[tuple[str, str, int]] = [(active[0], active[1], 3)]
        seen = {active}
        for entry in self._fallbacks:
            name, _, model = entry.partition("/")
            provider = self._providers.get(name)
            if provider is None:
                continue
            model = model or provider.default_model
            if not model or not provider.can_use_tools(model):
                continue
            if (name, model) in seen:
                continue
            seen.add((name, model))
            candidates.append((name, model, 2))
        return candidates

    async def _complete_interruptible(
        self,
        provider: BaseLLMProvider,
        provider_name: str,
        model: str,
        **kwargs: Any,
    ) -> LLMResponse:
        """Run one provider call in its own task so a model switch can abort it.

        The provider call must not run inside the caller's task: cancelling that
        task would cancel the whole agent turn instead of just this request.
        """
        call = _InflightCall(asyncio.ensure_future(provider.complete(**kwargs)))
        self._inflight_calls.add(call)
        try:
            return await call.task
        except asyncio.CancelledError:
            if call.interrupted:
                raise ModelCallInterrupted(provider_name, model) from None
            raise
        finally:
            self._inflight_calls.discard(call)

    def _interrupt_inflight_calls(self) -> int:
        """Abort main-model calls still waiting on the previous model.

        Bumping the generation also makes ``_attempt`` abandon retry loops and
        stale candidate lists, so the interrupted turn retries on the new model.
        """
        self._switch_generation += 1
        interrupted = 0
        for call in list(self._inflight_calls):
            if call.task.done():
                continue
            call.interrupted = True
            call.task.cancel()
            interrupted += 1
        return interrupted

    async def _attempt(
        self,
        provider_name: str,
        model: str,
        messages: list[Message],
        system_prompt: str,
        tools: list[dict],
        max_tokens: int,
        tries: int,
        thinking: bool = True,
        thinking_effort: str | None = None,
        interruptible: bool = True,
        expected_generation: int | None = None,
    ) -> LLMResponse:
        """对单个候选重试 tries 次（指数退避）。配置类错误确定性失败，不重试直接抛出。"""
        last_err: Exception | None = None
        for attempt in range(tries):
            if (
                expected_generation is not None
                and self._switch_generation != expected_generation
            ):
                raise ModelCallInterrupted(provider_name, model)
            try:
                provider = self._providers.get(provider_name)
                if not provider:
                    raise ProviderNotFoundError(provider_name)
                provider.set_model(model)
                if interruptible:
                    return await self._complete_interruptible(
                        provider,
                        provider_name,
                        model,
                        messages=messages,
                        system_prompt=system_prompt,
                        tools=tools,
                        max_tokens=max_tokens,
                        thinking=thinking,
                        thinking_effort=thinking_effort,
                    )
                return await provider.complete(
                    messages,
                    system_prompt,
                    tools,
                    max_tokens,
                    thinking=thinking,
                    thinking_effort=thinking_effort,
                )
            except (ProviderNotFoundError, ModelNotSupportedError, ModelCallInterrupted):
                raise
            except Exception as e:
                last_err = e
                if attempt < tries - 1:
                    wait = 2**attempt
                    logger.warning(
                        f"LLM call {provider_name}/{model} failed (attempt {attempt + 1}/{tries}), retrying in {wait}s: {e}"
                    )
                    await asyncio.sleep(wait)
        assert last_err is not None
        raise last_err

    async def think(
        self,
        messages: list[Message],
        system_prompt: str,
        tools: list[dict],
        max_tokens: int | None = None,
        _persist_switch: bool = True,
        _thinking_override: bool | None = None,
        _thinking_effort_override: str | None = None,
        _interruptible: bool = True,
    ) -> LLMResponse:

        if self._message_time_prefix:
            messages = _prepend_timestamps(messages)
        tokens = max_tokens if max_tokens is not None else self._max_tokens

        last_err: Exception | None = None
        interruptions = 0
        while True:
            # 与候选列表一起记录代数：切换发生在解析与调用之间时，用新 active 重新解析。
            generation = self._switch_generation
            candidates = self._resolve_candidates()
            interrupted: ModelCallInterrupted | None = None
            for idx, (name, model, tries) in enumerate(candidates):
                try:
                    response = await self._attempt(
                        name,
                        model,
                        messages,
                        system_prompt,
                        tools,
                        tokens,
                        tries,
                        thinking=(
                            self._thinking if _thinking_override is None else _thinking_override
                        ),
                        thinking_effort=(
                            self._thinking_effort
                            if _thinking_effort_override is None
                            else _thinking_effort_override
                        ),
                        interruptible=_interruptible,
                        expected_generation=generation if _interruptible else None,
                    )
                except ModelCallInterrupted as e:
                    interrupted = e
                    break
                except Exception as e:
                    last_err = e
                    logger.warning(f"Provider {name}/{model} exhausted ({tries} tries): {e}")
                    continue
                response.provider = name
                # 非首选候选成功 = 主模型已降级。_persist_switch 时把 active 切到它（停在备用，
                # 等手动切回）；summarize 等后台调用传 False，仅本次借用 fallback，不劫持主模型。
                if idx > 0 and _persist_switch:
                    async with self._lock:
                        old = (self._active_provider_name, self._active_model)
                        self._active_provider_name = name
                        self._active_model = model
                        self._last_switch_was_fallback = True
                    logger.warning(f"Fell back to {name}/{model} after {old[0]}/{old[1]} failed")
                    await self._notify_model_switch_listeners(name, model)
                return response

            if interrupted is None:
                assert last_err is not None
                raise last_err
            interruptions += 1
            if interruptions > _MAX_MODEL_SWITCH_INTERRUPTS:
                raise interrupted
            logger.info(
                tr(
                    "brain.switch.retry_with_active",
                    provider=interrupted.provider,
                    model=interrupted.model,
                )
            )

    def _resolve_summary_model(self) -> tuple[str, str] | None:
        if not self._summary_provider_name and not self._summary_model:
            return None

        provider_name = self._summary_provider_name or self._active_provider_name
        provider = self._providers.get(provider_name)
        if provider is None:
            raise ProviderNotFoundError(provider_name)

        model = self._summary_model
        if not model:
            model = provider.default_model
        if not model:
            raise ModelNotSupportedError(
                tr("brain.validation.summary_model_missing", provider=provider_name)
            )
        return provider_name, model

    async def _summary_think(
        self,
        messages: list[Message],
        system_prompt: str,
        max_tokens: int | None = None,
    ) -> LLMResponse:
        summary_target = self._resolve_summary_model()
        if summary_target is None:
            # 跟随主线：切换会打断这次摘要，think 的切换重试会在新模型上重新压缩。
            return await self.think(
                messages=messages,
                system_prompt=system_prompt,
                tools=[],
                max_tokens=max_tokens,
                _persist_switch=False,
                _thinking_override=self._summary_thinking,
                _thinking_effort_override=self._summary_thinking_effort,
            )

        provider_name, model = summary_target
        if self._message_time_prefix:
            messages = _prepend_timestamps(messages)
        response = await self._attempt(
            provider_name,
            model,
            messages,
            system_prompt,
            [],
            max_tokens if max_tokens is not None else self._max_tokens,
            tries=3,
            thinking=self._summary_thinking,
            thinking_effort=self._summary_thinking_effort,
            # 独立摘要模型不随主线切换中断：切换针对的是旧的主线模型。
            interruptible=False,
        )
        response.provider = provider_name
        return response

    async def query_with_vision(
        self,
        messages: list[Message],
        system_prompt: str = "",
        vision_provider: str = "",
        vision_model: str = "",
        max_tokens: int | None = None,
        usage_context: dict[str, Any] | None = None,
        require_video: bool = False,
    ) -> str:
        vision_provider = vision_provider or self._vision_provider_name
        vision_model = vision_model or self._vision_model
        if not vision_provider or not vision_model:
            raise RuntimeError(tr("brain.validation.vision_unconfigured"))
        provider = self._providers.get(vision_provider)
        if not provider:
            raise RuntimeError(
                tr("brain.validation.vision_provider_missing", provider=vision_provider)
            )
        if not provider.can_use_vision(vision_model):
            raise ModelNotSupportedError(
                tr(
                    "brain.validation.vision_unsupported",
                    model=vision_model,
                    provider=vision_provider,
                )
            )
        if require_video and not provider.can_use_video(vision_model):
            raise ModelNotSupportedError(
                tr(
                    "brain.validation.video_unsupported",
                    model=vision_model,
                    provider=vision_provider,
                )
            )
        provider.set_model(vision_model)
        resp = await provider.complete(
            messages,
            system_prompt,
            [],
            max_tokens if max_tokens is not None else self._max_tokens,
            thinking=self._vision_thinking,
            thinking_effort=self._vision_thinking_effort,
        )
        resp.provider = vision_provider
        self._notify_usage_listeners(
            self._vision_usage_listeners,
            resp,
            {
                "thinking": self._vision_thinking,
                "thinking_effort": self._vision_thinking_effort,
                **(usage_context or {}),
            },
        )
        return resp.content

    async def switch_model(self, provider_name: str, model_id: str = "") -> None:
        async with self._lock:
            if provider_name not in self._providers:
                raise ProviderNotFoundError(provider_name)
            provider = self._providers[provider_name]
            if not model_id:
                model_id = provider.default_model
                if not model_id:
                    raise ModelNotSupportedError(
                        tr("brain.validation.model_missing", provider=provider_name)
                    )
            if not provider.can_use_tools(model_id):
                raise ModelNotSupportedError(
                    tr(
                        "brain.validation.model_tool_unsupported",
                        model=model_id,
                        provider=provider_name,
                    )
                )
            old = (self._active_provider_name, self._active_model)
            self._active_provider_name = provider_name
            self._active_model = model_id
            logger.info(f"Switched model: {old[0]}/{old[1]} → {provider_name}/{model_id}")
        # 显式切换必须打断在途调用：旧 Provider 的请求可能永远不返回，否则主循环会一直卡着。
        interrupted = self._interrupt_inflight_calls()
        if interrupted:
            logger.info(
                tr(
                    "brain.switch.interrupted_calls",
                    count=interrupted,
                    provider=provider_name,
                    model=model_id,
                )
            )
        if old != (provider_name, model_id):
            await self._notify_model_switch_listeners(provider_name, model_id)

    @staticmethod
    def _sanitize_for_summary(content: str | list) -> str | list:
        """Strip binary data from content blocks, replacing with text placeholders."""
        if isinstance(content, str):
            return content
        sanitized = []
        for block in content:
            btype = block.get("type", "")
            if btype in ("image", "document"):
                filename = block.get("_filename", block.get("source", {}).get("media_type", btype))
                label = tr("brain.summary.image" if btype == "image" else "brain.summary.file")
                sanitized.append(
                    {
                        "type": "text",
                        "text": tr("brain.summary.attachment", kind=label, filename=filename),
                    }
                )
            else:
                sanitized.append(block)
        return sanitized

    async def summarize(
        self,
        messages: list[Message],
        context_hint: str = "",
        agent_system_prompt: str = "",
        stm_context: list[Message] | None = None,
        return_usage: bool = False,
    ) -> str | SummaryResult:
        """Compress a message slice into a plain-text summary.

        agent_system_prompt — when provided, enables subjective mode: the agent
        summarises in first-person using its own identity prompt. stm_context
        (the rendered memory-tree spine) is injected before the slice so the
        agent can contextualise old memories against its current knowledge.

        When agent_system_prompt is empty, falls back to objective mode: a
        neutral third-party description, no JSON wrapper.
        """
        sanitized_messages = []
        for message in messages:
            d = message.to_dict()
            d["content"] = self._sanitize_for_summary(d["content"])
            sanitized_messages.append(d)

        if agent_system_prompt:
            # 主观模式：模仿泡泡结构——上下文在前，指令消息在后
            # 将消息渲染为自然格式（而非 JSON），更易于第一人称回顾
            lines = []
            for d in sanitized_messages:
                role = d.get("role", "")
                content = d.get("content", "")
                if isinstance(content, list):
                    content = " ".join(
                        b.get("text", "")
                        for b in content
                        if isinstance(b, dict) and b.get("type") == "text"
                    )
                label = {"user": "[user]", "assistant": "[assistant]", "tool": "[tool_result]"}.get(
                    role, f"[{role}]"
                )
                lines.append(f"{label} {content}")
            slice_text = "\n".join(lines)

            hint_line = tr("brain.summary.context_hint", hint=context_hint) if context_hint else ""
            slice_msg = tr(
                "brain.summary.subjective_slice",
                hint=hint_line,
                content=slice_text,
            )
            instruction = tr("brain.summary.subjective_instruction")
            think_messages: list[Message] = []
            if stm_context:
                think_messages.extend(stm_context)
            think_messages.append(Message(role="system", content=slice_msg))
            think_messages.append(Message(role="user", content=instruction))
            response = await self._summary_think(
                messages=think_messages,
                system_prompt=agent_system_prompt,
            )
        else:
            # 客观模式：第三方叙述，纯文本，无 JSON
            lines = []
            for d in sanitized_messages:
                role = d.get("role", "")
                content = d.get("content", "")
                if isinstance(content, list):
                    content = " ".join(
                        b.get("text", "")
                        for b in content
                        if isinstance(b, dict) and b.get("type") == "text"
                    )
                label = {"user": "[user]", "assistant": "[assistant]", "tool": "[tool_result]"}.get(
                    role, f"[{role}]"
                )
                lines.append(f"{label} {content}")
            messages_natural = "\n".join(lines)

            prompt = tr("brain.summary.objective_prompt")
            if context_hint:
                prompt += tr("brain.summary.objective_hint", hint=context_hint)
            response = await self._summary_think(
                messages=[Message(role="user", content=messages_natural)],
                system_prompt=prompt,
            )

        self._notify_usage_listeners(
            self._summary_usage_listeners,
            response,
            {
                "context_hint": context_hint,
                "thinking": self._summary_thinking,
                "thinking_effort": self._summary_thinking_effort,
            },
        )
        if response.content and response.content.startswith("```json"):
            # 兼容部分模型喜欢加 markdown 代码块的输出
            content = response.content.removeprefix("```json").removesuffix("```").strip()
        else:
            content = response.content
        result = SummaryResult(content=content, usage=dict(response.usage or {}))
        return result if return_usage else result.content
