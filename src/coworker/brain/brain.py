from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from contextvars import ContextVar
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Literal

from loguru import logger

from coworker.brain.base import BaseLLMProvider
from coworker.brain.routing import ModelRouter, ModelTarget, RoutedResult, RoutingBudget
from coworker.brain.thinking import normalize_thinking_effort
from coworker.core.constants import DEFAULT_LLM_MAX_TOKENS
from coworker.core.exceptions import ModelNotSupportedError, ProviderNotFoundError
from coworker.core.llm_errors import ModelRoutingUnavailable
from coworker.core.model_config import RuntimePreferenceState
from coworker.core.model_routing import ModelRoutingConfig
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

# 模型调用的协程归属。只有主循环（"main"）发起的调用会随管理端暂停中断，
# 后台任务（Bubble、回溯等）保持运行。
ModelCallOwner = Literal["main", "bubble", "background"]

_MODEL_CALL_OWNER: ContextVar[ModelCallOwner] = ContextVar(
    "coworker_model_call_owner", default="background"
)


def set_model_call_owner(owner: ModelCallOwner) -> None:
    """Tag the model calls issued from the current task and its children."""
    _MODEL_CALL_OWNER.set(owner)


class ModelCallInterrupted(Exception):
    """An explicit model switch aborted this in-flight provider call."""

    def __init__(self, provider: str, model: str) -> None:
        super().__init__(
            tr("brain.switch.call_interrupted", provider=provider, model=model)
        )
        self.provider = provider
        self.model = model


class ModelCallSuspended(Exception):
    """An admin pause aborted this provider call; the turn must not retry."""

    def __init__(self, provider: str, model: str) -> None:
        super().__init__(
            tr("brain.pause.call_suspended", provider=provider, model=model)
        )
        self.provider = provider
        self.model = model


@dataclass(eq=False)
class _InflightCall:
    """One main-model provider call that a switch or a pause may abort."""

    task: asyncio.Task[RoutedResult[LLMResponse]]
    owner: ModelCallOwner
    abort: str | None = None  # "switch" | "suspend"


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
        routing: ModelRoutingConfig | None = None,
        router: ModelRouter | None = None,
        policy_parent: Brain | None = None,
        routing_parent: Brain | None = None,
        routing_role: str = "main",
    ) -> None:
        self.router = router or ModelRouter()
        self._providers = self.router.providers
        self._policy_parent = policy_parent
        self._routing_parent = routing_parent or policy_parent
        self._routing_config = routing or ModelRoutingConfig()
        self._routing_role = routing_role
        self._preference_revision = 0
        self._runtime_config_path: Path | None = None
        self._last_used: dict[str, Any] | None = None
        self._preference_state = RuntimePreferenceState()
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
        self.preference_changed = asyncio.Event()
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
        # 管理端暂停期间拒绝新的主线模型调用，直到主循环恢复。
        self._main_calls_suspended = False

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
        self.router.register(provider)
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
            self.router.register(provider)
            self.preference_changed.set()
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
        return self._policy_parent.current_provider_name if self._policy_parent else self._active_provider_name

    @property
    def current_model(self) -> str:
        return self._policy_parent.current_model if self._policy_parent else self._active_model

    @property
    def routing_config(self) -> ModelRoutingConfig:
        return self._routing_parent.routing_config if self._routing_parent else self._routing_config

    @property
    def preference_revision(self) -> tuple[int, ...]:
        parent = self._policy_parent.preference_revision if self._policy_parent else ()
        return (*parent, self._preference_revision)

    def configure_persistence(self, runtime_path: Path, health_path: Path) -> None:
        self._runtime_config_path = runtime_path
        self.router.configure_persistence(health_path)

    @property
    def preference_pending(self) -> bool:
        return self.router.preference_pending

    def restore_preference_state(self, state: RuntimePreferenceState) -> None:
        self._preference_state = state.model_copy(deep=True)
        if self._routing_parent is None and self._routing_role == "main":
            self.router.preference_pending = state.status == "pending"
        if state.status == "pending":
            self._active_provider_name = ""
            self._active_model = ""

    def require_preference_confirmation(
        self, state: RuntimePreferenceState, *, persistence_path: Path
    ) -> None:
        snapshot = self.model_config_snapshot()
        snapshot["preferred"] = None
        snapshot["preference"] = state.model_dump()
        self._persist_config(snapshot, persistence_path)
        self.restore_preference_state(state)

    def _persist_config(self, snapshot: dict[str, Any], path: Path | None = None) -> None:
        path = path or self._runtime_config_path
        if path is not None:
            from coworker.core.model_config import RuntimeModelConfig, write_runtime_model_config

            write_runtime_model_config(path, RuntimeModelConfig.from_brain_snapshot(snapshot))

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
        return self._policy_parent.fallbacks if self._policy_parent else list(self._fallbacks)

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
        return self._providers.get(self.current_provider_name)

    def consume_fallback_switch(self) -> bool:
        """读并复位「上次切换是否为失败降级」标志。主循环用它决定切换通知的措辞。"""
        flag = self._last_switch_was_fallback
        self._last_switch_was_fallback = False
        return flag

    @property
    def current_model_has_vision(self) -> bool:
        provider = self.active_provider
        return bool(provider and provider.can_use_vision(self.current_model))

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
                "provider": self.current_provider_name,
                "model": self.current_model,
                "thinking": self._thinking,
                "thinking_effort": self._thinking_effort,
            },
            "preferred": None if self.preference_pending else {"provider": self.current_provider_name, "model": self.current_model},
            "preference": self._preference_state.model_dump(),
            "last_used": self._last_used,
            "routing": self.routing_config.model_dump(),
            "health": self.router.snapshot(),
            "thinking_effort": self._thinking_effort,
            "summary": {
                "provider": self._summary_provider_name,
                "model": self._summary_model,
                "thinking": self._summary_thinking,
                "thinking_effort": self._summary_thinking_effort,
            },
            "fallbacks": self.fallbacks,
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
        routing: ModelRoutingConfig | dict[str, Any] | None = None,
        persistence_path: Path | None = None,
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
        next_routing = self.routing_config if routing is None else ModelRoutingConfig.model_validate(
            {**self.routing_config.model_dump(), **routing} if isinstance(routing, dict) else routing
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
            snapshot = self.model_config_snapshot()
            snapshot.update(
                thinking_effort=next_thinking_effort,
                routing=next_routing.model_dump(),
                summary={"provider": next_summary_provider, "model": next_summary_model, "thinking": next_summary_thinking, "thinking_effort": next_summary_thinking_effort},
                fallbacks=list(next_fallbacks),
                vision={"provider": next_vision_provider, "model": next_vision_model, "thinking": next_vision_thinking, "thinking_effort": next_vision_thinking_effort},
            )
            self._persist_config(snapshot, persistence_path)
            self._routing_config = next_routing
            self._preference_revision += 1
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
            self.preference_changed.set()
        return self.model_config_snapshot()

    async def count_tokens(self, messages: list[Message]) -> int:
        """Count tokens for messages using the active provider's native API if available."""
        provider = self.active_provider
        if provider is None:
            from coworker.core.token_utils import estimate_content_tokens

            return sum(estimate_content_tokens(m.content) for m in messages)
        return await provider.count_tokens(messages, self.current_model)

    def _interrupt_inflight_calls(self) -> int:
        """Invalidate old selections and cancel their routes, including retry sleeps."""
        self._switch_generation += 1
        interrupted = 0
        for call in list(self._inflight_calls):
            if call.task.done() or call.abort is not None:
                continue
            call.abort = "switch"
            call.task.cancel()
            interrupted += 1
        return interrupted

    def suspend_main_calls(self) -> int:
        """Park main-owned calls without affecting Bubble or background owners."""
        self._main_calls_suspended = True
        cancelled = 0
        for call in list(self._inflight_calls):
            if call.owner != "main" or call.task.done():
                continue
            # Suspension takes precedence over a switch; do not cancel cleanup twice.
            was_cancelled = call.abort is not None
            call.abort = "suspend"
            if not was_cancelled:
                call.task.cancel()
                cancelled += 1
        return cancelled

    def resume_main_calls(self) -> None:
        self._main_calls_suspended = False

    def _check_call_control(self, owner: ModelCallOwner, generation: int) -> None:
        if owner == "main" and self._main_calls_suspended:
            raise ModelCallSuspended(self.current_provider_name, self.current_model)
        if generation != self._switch_generation:
            raise ModelCallInterrupted(self.current_provider_name, self.current_model)

    async def _run_interruptible_route(
        self,
        execute: Callable[[], Awaitable[RoutedResult[LLMResponse]]],
        generation: int,
    ) -> RoutedResult[LLMResponse]:
        owner = _MODEL_CALL_OWNER.get()
        self._check_call_control(owner, generation)
        call = _InflightCall(asyncio.ensure_future(execute()), owner)
        self._inflight_calls.add(call)
        try:
            result = await call.task
            # A response may win the cancellation race; never commit stale work.
            self._check_call_control(owner, generation)
            return result
        except asyncio.CancelledError:
            caller = asyncio.current_task()
            if caller is not None and caller.cancelling():
                raise  # External shutdown/restart must not turn into another request.
            if call.abort == "suspend" or owner == "main" and self._main_calls_suspended:
                raise ModelCallSuspended(self.current_provider_name, self.current_model) from None
            if call.abort == "switch":
                raise ModelCallInterrupted(self.current_provider_name, self.current_model) from None
            raise
        except Exception:
            # Selection/pause may race with an already completed failing request.
            self._check_call_control(owner, generation)
            raise
        finally:
            self._inflight_calls.discard(call)

    def routing_targets(
        self,
        provider_name: str = "",
        model: str = "",
        *,
        allow_fallback: bool = True,
        require_tools: bool = True,
    ) -> list[ModelTarget]:
        """Resolve an immutable priority list; health filtering happens at execution."""
        self.router.ensure_preference_ready()
        name = provider_name or self.current_provider_name
        provider = self._providers.get(name)
        selected_model = model or (self.current_model if not provider_name else provider.default_model if provider else "")
        pairs = [(name, selected_model)]
        if allow_fallback:
            for entry in self.fallbacks:
                fallback_name, _, fallback_model = entry.partition("/")
                fallback_provider = self._providers.get(fallback_name)
                if fallback_provider is not None:
                    pairs.append((fallback_name, fallback_model or fallback_provider.default_model))
        seen: set[tuple[str, str]] = set()
        targets: list[ModelTarget] = []
        for candidate_name, candidate_model in pairs:
            target = self.router.target(candidate_name, candidate_model)
            if target is None or not candidate_model or (candidate_name, candidate_model) in seen:
                continue
            if require_tools and not target.provider.can_use_tools(candidate_model):
                continue
            seen.add((candidate_name, candidate_model))
            targets.append(target)
        if not targets:
            raise ProviderNotFoundError(name)
        return targets

    async def _execute(
        self,
        targets: list[ModelTarget],
        messages: list[Message],
        system_prompt: str,
        tools: list[dict],
        max_tokens: int,
        *,
        thinking: bool,
        thinking_effort: str,
        role: str,
        record_execution: bool = False,
        generation: int | None = None,
        budget: RoutingBudget | None = None,
    ) -> LLMResponse:
        revision = self.preference_revision
        preferred = (self.current_provider_name, self.current_model)
        config = self.routing_config

        async def call(target: ModelTarget, timeout: float) -> LLMResponse:
            return await target.provider.complete_for_model(
                target.model, messages, system_prompt, tools, max_tokens,
                thinking=thinking, thinking_effort=thinking_effort,
            )

        async def execute() -> RoutedResult[LLMResponse]:
            return await self.router.execute_async(
                targets, config, call, role=role, budget=budget,
                usage_scope=self._routing_role if self._routing_role in {"bubble", "subconscious"} else role,
            )

        result = await execute() if generation is None else await self._run_interruptible_route(execute, generation)
        response = result.value
        response.provider = result.target.name
        response.routing = {"preferred_provider": preferred[0], "preferred_model": preferred[1], "role": role, "attempts": result.attempts}
        if record_execution and self.preference_revision == revision:
            self._last_used = {"provider": result.target.name, "model": result.target.model, "fallback": (result.target.name, result.target.model) != preferred, "attempts": result.attempts}
        if (result.target.name, result.target.model) != preferred and record_execution:
            logger.info(tr("brain.routing.fallback_used", provider=result.target.name, model=result.target.model, preferred_provider=preferred[0], preferred_model=preferred[1]))
        return response

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

        blocks = [block for message in messages if isinstance(message.content, list) for block in message.content if isinstance(block, dict)]
        requires_vision = self.current_model_has_vision and any(block.get("type") == "image" for block in blocks)
        budget = self.router.new_budget(self.routing_config)
        interruptions = 0
        while True:
            generation = self._switch_generation if _interruptible else None
            targets = self.routing_targets(require_tools=bool(tools))
            if requires_vision:
                targets = [target for target in targets if target.provider.can_use_vision(target.model)]
            try:
                return await self._execute(
                    targets, messages, system_prompt, tools, tokens,
                    thinking=self._thinking if _thinking_override is None else _thinking_override,
                    thinking_effort=self._thinking_effort if _thinking_effort_override is None else _thinking_effort_override,
                    role=self._routing_role if _persist_switch else "summary",
                    record_execution=_persist_switch,
                    generation=generation,
                    budget=budget,
                )
            except ModelCallInterrupted:
                interruptions += 1
                if interruptions > _MAX_MODEL_SWITCH_INTERRUPTS:
                    # Repeated operator actions must not enter destructive cycle recovery.
                    raise ModelRoutingUnavailable(budget.attempts) from None
                logger.info(tr("brain.switch.retry_with_active", provider=self.current_provider_name, model=self.current_model))

    def _resolve_summary_model(self) -> tuple[str, str] | None:
        if not self._summary_provider_name and not self._summary_model:
            return None

        provider_name = self._summary_provider_name or self.current_provider_name
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
        response = await self._execute(
            self.routing_targets(provider_name, model, allow_fallback=False, require_tools=False),
            messages,
            system_prompt,
            [],
            max_tokens if max_tokens is not None else self._max_tokens,
            thinking=self._summary_thinking,
            thinking_effort=self._summary_thinking_effort,
            role="summary",
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
        resp = await self._execute(
            self.routing_targets(vision_provider, vision_model, allow_fallback=False, require_tools=False),
            messages,
            system_prompt,
            [],
            max_tokens if max_tokens is not None else self._max_tokens,
            thinking=self._vision_thinking,
            thinking_effort=self._vision_thinking_effort,
            role="vision",
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

    async def switch_model(
        self, provider_name: str, model_id: str = "", *,
        persistence_path: Path | None = None,
        preference_source: Literal["explicit", "startup"] = "explicit",
    ) -> None:
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
            snapshot = self.model_config_snapshot()
            snapshot["preferred"] = {"provider": provider_name, "model": model_id}
            selection = RuntimePreferenceState.explicit()
            selection.source = preference_source
            snapshot["preference"] = selection.model_dump()
            self._persist_config(snapshot, persistence_path)
            if self._policy_parent is not None:
                self._fallbacks = self.fallbacks
                self._policy_parent = None
            self._active_provider_name = provider_name
            self._active_model = model_id
            self.restore_preference_state(selection)
            self._preference_revision += 1
            self._last_used = None
            self.router.reset(provider_name, model_id)
            self.preference_changed.set()
            logger.info(f"Switched model: {old[0]}/{old[1]} → {provider_name}/{model_id}")
        # Explicit selection cancels a route without rewriting fallback health.
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
