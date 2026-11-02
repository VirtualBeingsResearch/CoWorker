from __future__ import annotations

import asyncio
import json
import math
import random
import threading
import time
from collections import deque
from collections.abc import Awaitable, Callable, Generator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, TypeVar
from uuid import uuid4

from loguru import logger

from coworker.brain.base import BaseLLMProvider
from coworker.core.llm_errors import (
    FailureCategory,
    ModelPreferencePending,
    ModelRoutingUnavailable,
    ProviderCallError,
    normalize_provider_error,
)
from coworker.core.model_routing import ModelRoutingConfig
from coworker.i18n import tr

T = TypeVar("T")
HealthKey = tuple[str, str, str]


@dataclass(frozen=True)
class ModelTarget:
    name: str
    model: str
    provider: BaseLLMProvider
    revision: str


@dataclass
class _Health:
    state: str = "closed"
    epoch: int = 0
    failures: deque[float] = field(default_factory=deque)
    retry_at: float = 0
    cooldown: float = 0
    successes: int = 0
    probing: bool = False
    error: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class _Permit:
    target: ModelTarget
    epochs: tuple[tuple[HealthKey, int], ...]


@dataclass(frozen=True)
class _Attempt:
    target: ModelTarget
    timeout: float


@dataclass(frozen=True)
class _Delay:
    seconds: float


@dataclass
class RoutedResult[T]:
    value: T
    target: ModelTarget
    attempts: list[dict[str, Any]]


@dataclass
class RoutingBudget:
    """One logical request's limits and history, retained across explicit switches."""

    deadline: float
    max_attempts: int
    used: int = 0
    routing_id: str = field(default_factory=lambda: uuid4().hex)
    attempts: list[dict[str, Any]] = field(default_factory=list)


class ModelRouter:
    """One process-local registry and breaker shared by all Brains and mem0 threads.

    Selection preference belongs to callers, never to this health registry. A
    successful fallback cannot rewrite it. Network I/O never holds the state lock.
    The same generator drives async and synchronous callers, including cancellation
    cleanup, so background traffic cannot bypass cooling or recovery leases.
    """

    def __init__(
        self,
        *,
        clock: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], float] = time.time,
    ) -> None:
        self.providers: dict[str, BaseLLMProvider] = {}
        self._lock = threading.RLock()
        self._clock = clock
        self._wall_clock = wall_clock
        self._health: dict[HealthKey, _Health] = {}
        self._revisions: dict[str, str] = {}
        self._state_path: Path | None = None
        self._listeners: list[Callable[[dict[str, Any]], None]] = []
        self.preference_pending = False

    def ensure_preference_ready(self) -> None:
        if self.preference_pending:
            raise ModelPreferencePending()

    def add_listener(self, listener: Callable[[dict[str, Any]], None]) -> None:
        self._listeners.append(listener)

    def _emit(self, role: str, entry: dict[str, Any]) -> None:
        for listener in self._listeners:
            try:
                listener({"role": role, **entry})
            except Exception:
                # Observability must not cause a successful generation to be retried.
                logger.warning(tr("brain.routing.listener_failed"))

    def register(self, provider: BaseLLMProvider) -> None:
        with self._lock:
            name = provider.provider_name
            if self.providers.get(name) is provider:
                return
            revision = getattr(provider, "connection_revision", "") or str(id(provider))
            self.providers[name] = provider
            self._revisions[name] = revision
            self._health = {
                key: state
                for key, state in self._health.items()
                if key[0] != name or key[2] == revision
            }

    def target(self, name: str, model: str) -> ModelTarget | None:
        with self._lock:
            provider = self.providers.get(name)
            if provider is None:
                return None
            return ModelTarget(name, model, provider, self._revisions[name])

    @staticmethod
    def _keys(target: ModelTarget) -> tuple[HealthKey, HealthKey]:
        return (target.name, "", target.revision), (target.name, target.model, target.revision)

    def _available(self, target: ModelTarget) -> bool:
        with self._lock:
            if self._revisions.get(target.name) != target.revision:
                return False
            for key in self._keys(target):
                state = self._health.get(key)
                if state is not None and (
                    state.state == "blocked"
                    or state.probing
                    or state.state == "open"
                    and self._clock() < state.retry_at
                ):
                    return False
            return True

    def _acquire(self, target: ModelTarget) -> _Permit | None:
        with self._lock:
            if not self._available(target):
                return None
            entries = [(key, self._health.setdefault(key, _Health())) for key in self._keys(target)]
            for _, state in entries:
                if state.state == "open":
                    state.state = "half_open"
                    state.epoch += 1
                if state.state == "half_open":
                    state.probing = True
            return _Permit(target, tuple((key, state.epoch) for key, state in entries))

    def _release(self, permit: _Permit) -> None:
        with self._lock:
            for key, epoch in permit.epochs:
                state = self._health.get(key)
                if state is not None and state.epoch == epoch:
                    state.probing = False

    def _success(self, permit: _Permit, config: ModelRoutingConfig) -> None:
        with self._lock:
            if self._revisions.get(permit.target.name) != permit.target.revision:
                return
            changed = False
            for key, epoch in permit.epochs:
                state = self._health.get(key)
                if state is None or state.epoch != epoch:
                    continue
                if state.state == "half_open":
                    state.successes += 1
                    changed = True
                    if state.successes < config.recovery_successes:
                        continue
                    state.state = "closed"
                    state.probing = False
                    state.epoch += 1
                    state.cooldown = 0
                    state.successes = 0
                    state.error = {}
                if state.state == "closed":
                    state.failures.clear()
            if changed:
                self._save_locked()

    def _failure(
        self, permit: _Permit, error: ProviderCallError, config: ModelRoutingConfig
    ) -> None:
        if not error.allow_fallback or error.category == FailureCategory.UNKNOWN:
            return
        with self._lock:
            if self._revisions.get(permit.target.name) != permit.target.revision:
                return
            key = self._keys(permit.target)[0 if error.connection_scope else 1]
            state = self._health[key]
            if state.epoch != dict(permit.epochs)[key]:
                return
            now = self._clock()
            state.error = error.as_dict()
            state.failures.append(now)
            while state.failures and state.failures[0] < now - config.failure_window_seconds:
                state.failures.popleft()
            if error.blocked:
                state.state = "blocked"
            elif error.category == FailureCategory.QUOTA:
                state.state = "open"
                state.cooldown = (
                    error.retry_after
                    if error.retry_after is not None
                    else config.quota_cooldown_seconds
                )
            elif error.category == FailureCategory.RATE_LIMIT:
                state.state = "open"
                state.cooldown = (
                    error.retry_after
                    if error.retry_after is not None
                    else config.rate_limit_cooldown_seconds
                )
            elif (
                state.state == "half_open"
                or len(state.failures) >= config.failure_threshold
                or error.retry_after
            ):
                state.state = "open"
                state.cooldown = min(
                    config.max_cooldown_seconds, max(config.cooldown_seconds, state.cooldown * 2)
                )
                state.cooldown = max(state.cooldown, error.retry_after or 0)
            else:
                return
            state.retry_at = now + state.cooldown
            state.successes = 0
            state.probing = False
            state.epoch += 1
            self._save_locked()

    def reset(self, name: str, model: str = "") -> None:
        """An explicit retry permits verification; it never declares a target healthy."""
        with self._lock:
            for key, state in self._health.items():
                if key[0] == name and (not model or key[1] in {"", model}):
                    state.failures.clear()
                    if state.state == "closed":
                        state.error = {}
                        state.epoch += 1
                        continue
                    state.state = "open"
                    state.retry_at = self._clock()
                    state.probing = False
                    state.successes = 0
                    state.epoch += 1
            self._save_locked()

    def snapshot(self) -> list[dict[str, Any]]:
        with self._lock:
            return [
                {
                    "provider": name,
                    "model": model,
                    "connection_revision": revision,
                    "state": state.state,
                    "retry_at": None
                    if state.state == "blocked"
                    else self._wall_clock() + max(0, state.retry_at - self._clock()),
                    "cooldown_seconds": state.cooldown,
                    "recovery_successes": state.successes,
                    "probing": state.probing,
                    "error": dict(state.error),
                }
                for (name, model, revision), state in self._health.items()
                if state.state != "closed"
            ]

    def configure_persistence(self, path: Path) -> None:
        with self._lock:
            self._state_path = path
            if not path.exists():
                return
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
                if not isinstance(payload, list):
                    raise ValueError("invalid routing state")
                restored: dict[HealthKey, _Health] = {}
                for item in payload:
                    name, model, revision = (
                        item["provider"],
                        item["model"],
                        item["connection_revision"],
                    )
                    if self._revisions.get(name) != revision or item["state"] not in {
                        "blocked",
                        "open",
                        "half_open",
                    }:
                        continue
                    retry_at = item.get("retry_at")
                    retry_at = float(retry_at) if retry_at is not None else self._wall_clock()
                    cooldown = float(item.get("cooldown_seconds", 0))
                    if not math.isfinite(retry_at) or not math.isfinite(cooldown) or cooldown < 0:
                        raise ValueError("invalid routing deadline")
                    delay = max(0, retry_at - self._wall_clock())
                    # In-flight probes do not survive a restart. Validate again on demand.
                    restored[(name, model, revision)] = _Health(
                        state="blocked" if item["state"] == "blocked" else "open",
                        retry_at=self._clock() + delay,
                        cooldown=cooldown,
                        error=item.get("error", {}) if isinstance(item.get("error"), dict) else {},
                    )
                self._health.update(restored)
            except (OSError, ValueError, KeyError, TypeError, AttributeError):
                logger.warning(tr("brain.routing.state_load_failed"))

    def _save_locked(self) -> None:
        if self._state_path is None:
            return
        try:
            self._state_path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self._state_path.with_suffix(".tmp")
            temporary.write_text(json.dumps(self.snapshot(), ensure_ascii=False), encoding="utf-8")
            temporary.replace(self._state_path)
        except OSError:
            logger.warning(tr("brain.routing.state_save_failed"))

    def _retry_after(self, targets: list[ModelTarget]) -> float:
        with self._lock:
            now = self._clock()
            delays = []
            for target in targets:
                if self._revisions.get(target.name) != target.revision:
                    continue
                states = [self._health[key] for key in self._keys(target) if key in self._health]
                if any(state.state == "blocked" for state in states):
                    continue
                # Both scopes must permit a call; the earliest usable candidate
                # is the minimum of these per-target maximum deadlines.
                delay = max(
                    (state.retry_at - now for state in states if state.state == "open"),
                    default=0,
                )
                if any(state.probing for state in states):
                    delay = max(delay, 30)
                if delay > 0:
                    delays.append(delay)
            return min(delays, default=30)

    def _skip_details(self, target: ModelTarget) -> dict[str, Any]:
        with self._lock:
            if self._revisions.get(target.name) != target.revision:
                return {"reason": "connection_replaced"}
            for key in self._keys(target):
                state = self._health.get(key)
                if state is not None and state.state != "closed":
                    return {
                        "reason": "probe_in_flight" if state.probing else state.state,
                        **state.error,
                    }
            return {"reason": "unavailable"}

    def new_budget(self, config: ModelRoutingConfig) -> RoutingBudget:
        return RoutingBudget(self._clock() + config.request_timeout_seconds, config.max_attempts)

    def _route(
        self,
        targets: list[ModelTarget],
        config: ModelRoutingConfig,
        role: str,
        usage_scope: str,
        budget: RoutingBudget | None = None,
    ) -> Generator[_Attempt | _Delay, Any, RoutedResult[Any]]:
        self.ensure_preference_ready()
        budget = budget or self.new_budget(config)
        attempts = budget.attempts
        routing_id = budget.routing_id
        deadline = min(budget.deadline, self._clock() + config.request_timeout_seconds)
        max_attempts = min(budget.max_attempts, config.max_attempts)
        for index, target in enumerate(targets):
            if budget.used >= max_attempts or self._clock() >= deadline:
                break
            permit = self._acquire(target)
            if permit is None:
                attempts.append(
                    {
                        "routing_id": routing_id,
                        "provider": target.name,
                        "model": target.model,
                        "outcome": "skipped",
                        **self._skip_details(target),
                    }
                )
                self._emit(role, attempts[-1])
                continue
            last_error: ProviderCallError | None = None
            try:
                for attempt in range(config.attempts_per_target):
                    if attempt:
                        # Another caller may have opened a shared breaker while
                        # this request slept. Never retry using its old lease.
                        self._release(permit)
                        renewed = self._acquire(target)
                        if renewed is None:
                            break
                        permit = renewed
                    remaining = deadline - self._clock()
                    if budget.used >= max_attempts or remaining <= 0:
                        break
                    # Reserve time for later candidates instead of spending the entire
                    # logical deadline retrying the first target.
                    later = sum(self._available(candidate) for candidate in targets[index + 1 :])
                    timeout = min(config.attempt_timeout_seconds, remaining / (1 + later))
                    budget.used += 1
                    entry: dict[str, Any] = {
                        "routing_id": routing_id,
                        "provider": target.name,
                        "model": target.model,
                        "attempt": attempt + 1,
                    }
                    attempts.append(entry)
                    try:
                        value = yield _Attempt(target, timeout)
                    except GeneratorExit:
                        entry["outcome"] = "cancelled"
                        self._emit(role, entry)
                        raise
                    except Exception as error:
                        failure = normalize_provider_error(
                            error, target.provider.provider_type, now=self._wall_clock()
                        )
                        if failure is None:
                            raise
                        last_error = failure
                        server_wait = failure.retry_after is not None and failure.retry_after > 0
                        if server_wait and failure.allow_fallback:
                            # Publish before potentially slow logging/listeners:
                            # synchronous memory workers share this registry too.
                            self._failure(permit, failure, config)
                            last_error = None  # Already counted this logical failure.
                        entry.update(outcome="failed", **failure.as_dict())
                        entry["usage_scope"] = usage_scope or role
                        entry["usage_source"] = "unavailable"
                        if failure.usage is not None:
                            entry.update(
                                usage=dict(failure.usage),
                                usage_source="provider",
                                usage_model=failure.usage_model or target.model,
                            )
                        self._emit(role, entry)
                        logger.warning(
                            tr(
                                "brain.routing.attempt_failed",
                                role=role,
                                provider=target.name,
                                model=target.model,
                                category=failure.category.value,
                                code=failure.vendor_code or failure.status_code or "-",
                            )
                        )
                        if not failure.allow_fallback:
                            raise failure from None
                        later = sum(
                            self._available(candidate) for candidate in targets[index + 1 :]
                        )
                        if (
                            not failure.retryable
                            or server_wait
                            or attempt + 1 >= config.attempts_per_target
                            or budget.used + later >= max_attempts
                        ):
                            break
                        delay = config.retry_delay_seconds * (2**attempt) * random.uniform(1, 1.25)
                        if self._clock() + delay >= deadline:
                            break
                        yield _Delay(delay)
                    else:
                        entry["outcome"] = "success"
                        self._emit(role, entry)
                        self._success(permit, config)
                        return RoutedResult(value, target, attempts)
                if last_error is not None:
                    # Count a failed logical request once, not each internal retry.
                    self._failure(permit, last_error, config)
            finally:
                self._release(permit)
        raise ModelRoutingUnavailable(attempts, self._retry_after(targets)) from None

    async def execute_async(
        self,
        targets: list[ModelTarget],
        config: ModelRoutingConfig,
        call: Callable[[ModelTarget, float], Awaitable[T]],
        *,
        role: str,
        usage_scope: str = "",
        budget: RoutingBudget | None = None,
    ) -> RoutedResult[T]:
        engine = self._route(targets, config, role, usage_scope, budget)
        try:
            action = next(engine)
            while True:
                try:
                    if isinstance(action, _Delay):
                        await asyncio.sleep(action.seconds)
                        value = None
                    else:
                        value = await asyncio.wait_for(
                            call(action.target, action.timeout), timeout=action.timeout
                        )
                except Exception as error:
                    action = engine.throw(error)
                else:
                    action = engine.send(value)
        except StopIteration as completed:
            return completed.value
        finally:
            # Also releases a half-open lease on cancellation or unexpected bugs.
            engine.close()

    def execute_sync(
        self,
        targets: list[ModelTarget],
        config: ModelRoutingConfig,
        call: Callable[[ModelTarget, float], T],
        *,
        role: str,
        usage_scope: str = "",
    ) -> RoutedResult[T]:
        engine = self._route(targets, config, role, usage_scope)
        try:
            action = next(engine)
            while True:
                try:
                    if isinstance(action, _Delay):
                        time.sleep(action.seconds)
                        value = None
                    else:
                        # Sync callers set this timeout on their SDK request.
                        value = call(action.target, action.timeout)
                except Exception as error:
                    action = engine.throw(error)
                else:
                    action = engine.send(value)
        except StopIteration as completed:
            return completed.value
        finally:
            engine.close()
