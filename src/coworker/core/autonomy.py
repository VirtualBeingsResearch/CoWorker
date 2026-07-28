"""Autonomy levels and the shared model-call gate."""

from __future__ import annotations

import asyncio
from collections import Counter
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from enum import StrEnum
from typing import TypeVar

from pydantic import BaseModel

from coworker.i18n import tr

_T = TypeVar("_T")


class AutonomyLevel(StrEnum):
    SILENT = "silent"
    REACTIVE = "reactive"
    EVENT_DRIVEN = "event_driven"
    AUTONOMOUS = "autonomous"

    @property
    def rank(self) -> int:
        return {
            AutonomyLevel.SILENT: 0,
            AutonomyLevel.REACTIVE: 1,
            AutonomyLevel.EVENT_DRIVEN: 2,
            AutonomyLevel.AUTONOMOUS: 3,
        }[self]

    def allows(self, required: AutonomyLevel) -> bool:
        return self.rank >= required.rank


class AutonomyScope(StrEnum):
    MAIN = "main"
    BUBBLE = "bubble"
    SUBCONSCIOUS = "subconscious"
    SUMMARY = "summary"
    VISION = "vision"
    MEM0 = "mem0"


class AutonomyThresholds(BaseModel):
    main: AutonomyLevel = AutonomyLevel.REACTIVE
    bubble: AutonomyLevel = AutonomyLevel.REACTIVE
    subconscious: AutonomyLevel = AutonomyLevel.AUTONOMOUS
    summary: AutonomyLevel = AutonomyLevel.REACTIVE
    vision: AutonomyLevel = AutonomyLevel.REACTIVE
    mem0: AutonomyLevel = AutonomyLevel.REACTIVE

    def required_for(self, scope: AutonomyScope) -> AutonomyLevel:
        return getattr(self, scope.value)


class AutonomyBlockedError(RuntimeError):
    def __init__(
        self,
        *,
        current: AutonomyLevel,
        required: AutonomyLevel,
        scope: AutonomyScope,
    ) -> None:
        self.current = current
        self.required = required
        self.scope = scope
        super().__init__(
            tr(
                "autonomy.blocked",
                current=current.value,
                required=required.value,
                scope=scope.value,
            )
        )


class AutonomyController:
    """Hot-reloadable policy shared by every model-calling subsystem.

    Lowering the level never cancels an in-flight call. The next call observes
    the new policy, which gives the runtime a bounded, protocol-safe drain.
    """

    def __init__(
        self,
        level: AutonomyLevel,
        thresholds: AutonomyThresholds,
    ) -> None:
        self._level = level
        self._thresholds = thresholds.model_copy(deep=True)
        self._changed = asyncio.Event()
        self._in_flight: Counter[tuple[AutonomyScope, AutonomyLevel]] = Counter()

    @property
    def level(self) -> AutonomyLevel:
        return self._level

    @property
    def thresholds(self) -> AutonomyThresholds:
        return self._thresholds.model_copy(deep=True)

    @property
    def in_flight(self) -> int:
        return sum(self._in_flight.values())

    @property
    def is_draining(self) -> bool:
        return any(
            count > 0 and not self.allows(scope, trigger=trigger)
            for (scope, trigger), count in self._in_flight.items()
        )

    @property
    def change_event(self) -> asyncio.Event:
        return self._changed

    def update(
        self,
        *,
        level: AutonomyLevel | None = None,
        thresholds: AutonomyThresholds | None = None,
    ) -> None:
        if level is not None:
            self._level = level
        if thresholds is not None:
            self._thresholds = thresholds.model_copy(deep=True)
        self._signal_change()

    def _signal_change(self) -> None:
        changed = self._changed
        self._changed = asyncio.Event()
        changed.set()

    def required_for(
        self,
        scope: AutonomyScope,
        *,
        trigger: AutonomyLevel = AutonomyLevel.SILENT,
    ) -> AutonomyLevel:
        scope_required = self._thresholds.required_for(scope)
        return max((scope_required, trigger), key=lambda level: level.rank)

    def allows(
        self,
        scope: AutonomyScope,
        *,
        trigger: AutonomyLevel = AutonomyLevel.SILENT,
    ) -> bool:
        return self._level_allows(self.required_for(scope, trigger=trigger))

    def _level_allows(self, required: AutonomyLevel) -> bool:
        # Silent is a global kill switch even if a scope threshold is
        # accidentally configured to "silent".
        return self._level is not AutonomyLevel.SILENT and self._level.allows(required)

    def ensure_allowed(
        self,
        scope: AutonomyScope,
        *,
        trigger: AutonomyLevel = AutonomyLevel.SILENT,
    ) -> None:
        required = self.required_for(scope, trigger=trigger)
        if not self._level_allows(required):
            raise AutonomyBlockedError(
                current=self._level,
                required=required,
                scope=scope,
            )

    async def wait_until_allowed(
        self,
        scope: AutonomyScope,
        *,
        trigger: AutonomyLevel = AutonomyLevel.SILENT,
    ) -> None:
        while not self.allows(scope, trigger=trigger):
            changed = self._changed
            if self.allows(scope, trigger=trigger):
                return
            await changed.wait()

    async def retry_when_allowed(
        self,
        scope: AutonomyScope,
        operation: Callable[[], Awaitable[_T]],
        *,
        trigger: AutonomyLevel = AutonomyLevel.SILENT,
    ) -> _T:
        """Retry a nested model operation after a policy pause.

        Top-level loops intentionally surface :class:`AutonomyBlockedError` so
        they can release their cycle. Nested/background work uses this helper to
        pause without turning a policy transition into a user-visible failure.
        """

        while True:
            try:
                return await operation()
            except AutonomyBlockedError as error:
                if error.scope is not scope:
                    raise
                await self.wait_until_allowed(scope, trigger=trigger)

    @asynccontextmanager
    async def model_call(
        self,
        scope: AutonomyScope,
        *,
        trigger: AutonomyLevel = AutonomyLevel.SILENT,
    ) -> AsyncIterator[None]:
        self.ensure_allowed(scope, trigger=trigger)
        key = (scope, trigger)
        self._in_flight[key] += 1
        try:
            yield
        finally:
            self._in_flight[key] -= 1
            if self._in_flight[key] <= 0:
                del self._in_flight[key]
            self._signal_change()
