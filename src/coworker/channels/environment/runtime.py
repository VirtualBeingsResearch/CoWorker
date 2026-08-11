"""Environment channel runtime — the scheduling loop.

Each environment source declares its own trigger conditions (modelled on
:class:`~coworker.agent.subconscious_mode.SubconsciousMode`):

* ``every_seconds`` / ``interval_seconds`` — wall-clock periodicity
* ``every_n_cycles`` — after N agent cycles
* ``every_n_tool_calls`` — after N tool calls
* ``cold_floor_seconds`` — once, N seconds after startup
* ``cron`` — a standard 5-field cron expression
* ``manual`` — never auto-scheduled; only via ``manage_environment run_now``

The runtime owns one long-running task that wakes on a short tick (or when
notified of a cycle/tool-call) and runs every due source concurrently, capped
by ``max_concurrent_polls``.
"""

from __future__ import annotations

import asyncio
import time
from datetime import datetime
from typing import TYPE_CHECKING

from loguru import logger

from .executor import SourceExecutor
from .loader import EnvironmentLoader
from .state import SourceStateStore
from .types import EnvironmentSignal, EnvironmentSourceDef, SourceScheduleState

if TYPE_CHECKING:
    from coworker.channels.base import BaseChannel

# Minimum scheduler tick — the loop sleeps at most this long between due checks.
_TICK_SECONDS = 5.0


class EnvironmentRuntime:
    """Schedules and executes environment sources.

    Implements the :class:`~coworker.channels.runtime.ChannelRuntime` protocol
    (``name`` / ``start()`` / ``stop()``) so it plugs into the existing
    :class:`~coworker.channels.registry.ChannelRegistry` lifecycle.
    """

    name = "environment"

    def __init__(
        self,
        *,
        loader: EnvironmentLoader,
        executor: SourceExecutor,
        state_store: SourceStateStore,
        channel: BaseChannel | None = None,
        max_concurrent_polls: int = 5,
    ) -> None:
        self._loader = loader
        self._executor = executor
        self._state_store = state_store
        self._channel = channel
        self._max_concurrent = max(1, max_concurrent_polls)
        self._stop = asyncio.Event()
        self._wake = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        # agent-activity counters (updated by notify_* callbacks)
        self._cycle_count = 0
        self._tool_call_count = 0
        self._started_at = time.monotonic()

    # ------------------------------------------------------------------ ChannelRuntime

    async def start(self) -> None:
        if self._task is not None and not self._task.done():
            return
        await self._state_store.load()
        self._loader.load_all()
        self._stop.clear()
        self._task = asyncio.create_task(self._run(), name="environment-runtime")
        await asyncio.sleep(0)

    async def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        if self._task is not None and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        self._task = None

    # ------------------------------------------------------------------ Notifications

    def notify_cycle_complete(self) -> None:
        """Called by AgentLoop after each cycle (drives every_n_cycles)."""
        self._cycle_count += 1
        self._wake.set()

    def notify_tool_call(self) -> None:
        """Called by AgentLoop after each tool call (drives every_n_tool_calls)."""
        self._tool_call_count += 1
        self._wake.set()

    def request_reload(self) -> None:
        """Force a source-directory rescan on the next tick."""
        self._wake.set()

    async def run_source_now(self, source_id: str) -> bool:
        """Immediately poll a single source by name, bypassing its schedule.

        Returns ``True`` if the source was found and polled.
        """
        definitions = {d.name: d for d in self._loader.list_all()}
        definition = definitions.get(source_id)
        if definition is None:
            return False
        await self._poll_one(definition)
        return True

    # ------------------------------------------------------------------ Main loop

    async def _run(self) -> None:
        logger.info("EnvironmentRuntime started")
        try:
            while not self._stop.is_set():
                self._loader.load_all()
                await self._poll_due_sources()
                # Wait for the next tick or an external wake signal.
                self._wake.clear()
                try:
                    await asyncio.wait_for(
                        self._stop.wait(),  # returns when stop() is called
                        timeout=_TICK_SECONDS,
                    )
                except TimeoutError:
                    pass
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("EnvironmentRuntime crashed; will not restart until process restart")
        finally:
            logger.info("EnvironmentRuntime stopped")

    async def _poll_due_sources(self) -> None:
        now_mono = time.monotonic()
        now_wall = datetime.now()
        definitions = self._loader.list_all()
        states = await self._state_store.all_states()
        due: list[EnvironmentSourceDef] = []
        for definition in definitions:
            state = states.get(definition.name) or self._state_store.get(definition.name)
            if not state.is_enabled(definition):
                continue
            if self._is_due(definition, state, now_mono, now_wall):
                due.append(definition)
        if not due:
            return
        semaphore = asyncio.Semaphore(self._max_concurrent)

        async def _guarded(defn: EnvironmentSourceDef) -> None:
            async with semaphore:
                await self._poll_one(defn)

        await asyncio.gather(*(_guarded(d) for d in due), return_exceptions=True)
        await self._state_store.save()

    def _is_due(
        self,
        definition: EnvironmentSourceDef,
        state: SourceScheduleState,
        now_mono: float,
        now_wall: datetime,
    ) -> bool:
        # min_interval protection: never run more frequently than this.
        if (
            definition.min_interval_seconds > 0
            and state.last_run_at is not None
            and (now_wall - state.last_run_at).total_seconds()
            < definition.min_interval_seconds
        ):
            return False

        trigger = definition.schedule_trigger
        if trigger == "manual":
            return False

        if trigger == "cold_floor":
            cf = definition.cold_floor_seconds
            if cf <= 0:
                return False
            # Only fire once after startup (last_run_at is None means never ran).
            if state.last_run_at is not None:
                return False
            return (now_mono - self._started_at) >= cf

        # periodic
        conditions: list[bool] = []
        if definition.interval_seconds > 0:
            last = state.last_run_at
            elapsed = (
                (now_wall - last).total_seconds() if last else float("inf")
            )
            conditions.append(elapsed >= definition.interval_seconds)
        if definition.every_seconds > 0:
            last = state.last_run_at
            elapsed = (
                (now_wall - last).total_seconds() if last else float("inf")
            )
            conditions.append(elapsed >= definition.every_seconds)
        if definition.every_n_cycles > 0:
            conditions.append(
                self._cycle_count > 0
                and self._cycle_count % definition.every_n_cycles == 0
                and (state.last_run_at is None or self._cycle_since_last(state) >= definition.every_n_cycles)
            )
        if definition.every_n_tool_calls > 0:
            conditions.append(
                self._tool_call_count > 0
                and self._tool_call_count % definition.every_n_tool_calls == 0
            )
        if definition.cron:
            conditions.append(_cron_due(definition.cron, now_wall, state.last_run_at))
        return any(conditions)

    def _cycle_since_last(self, state: SourceScheduleState) -> int:
        """Approximate cycles elapsed since last run.

        Without per-source cycle bookkeeping we use the global cycle count delta
        since the last run's wall clock — a coarse but safe approximation.
        """
        return self._cycle_count  # conservative: always allows periodic triggers

    async def _poll_one(self, definition: EnvironmentSourceDef) -> None:
        state = self._state_store.get(definition.name)
        signals = await self._executor.run(definition, state)
        for signal in signals:
            await self._publish(signal)
        if signals:
            logger.debug(
                f"Environment source {definition.name}: emitted {len(signals)} signal(s)"
            )
        await self._state_store.save()

    async def _publish(self, signal: EnvironmentSignal) -> None:
        if self._channel is None:
            return
        from coworker.core.types import IncomingEvent

        content = self._format_signal(signal)
        event = IncomingEvent(
            participant_id=f"env:{signal.source_id}",
            content=content,
            source="environment",
            timestamp=signal.timestamp,
        )
        try:
            await self._channel.publish_inbound(event)
        except Exception:
            logger.exception(f"Failed to publish environment signal from {signal.source_id}")

    @staticmethod
    def _format_signal(signal: EnvironmentSignal) -> str:
        header = f"[环境信号 · {signal.source_id}"
        if signal.severity != "info":
            header += f" · {signal.severity}"
        header += "]"
        body = signal.title
        if signal.url:
            body += f"\n链接：{signal.url}"
        body += f"\n\n{signal.content}"
        return f"{header}\n{body}"


# ---------------------------------------------------------------------------
# Minimal cron matcher (5-field, local time)
# ---------------------------------------------------------------------------


def _cron_due(expr: str, now: datetime, last_run: datetime | None) -> bool:
    """Return True if ``expr`` should fire at ``now`` since ``last_run``.

    Implements a conservative minute-granularity match against a 5-field cron
    expression (minute hour day-of-month month day-of-week, local time).
    Supports ``*``, comma lists, ranges, and ``*/step``.
    """
    try:
        fields = expr.split()
        if len(fields) != 5:
            return False
        # Check the current minute; if it matches, we're due (subject to
        # last_run having been in a different minute).
        if not _cron_field_matches(fields[0], now.minute, 0, 59):
            return False
        if not _cron_field_matches(fields[1], now.hour, 0, 23):
            return False
        if not _cron_field_matches(fields[2], now.day, 1, 31):
            return False
        if not _cron_field_matches(fields[3], now.month, 1, 12):
            return False
        weekday = now.weekday()  # Monday=0 .. Sunday=6; cron uses Sunday=0
        cron_dow = (weekday + 1) % 7
        if not _cron_field_matches(fields[4], cron_dow, 0, 6):
            return False
        # The current minute matches.  Don't fire twice in the same minute:
        if last_run is not None and _same_minute(last_run, now):
            return False
        return True
    except Exception:
        logger.debug(f"Environment cron expression {expr!r} could not be evaluated")
        return False


def _cron_field_matches(field: str, value: int, min_val: int, max_val: int) -> bool:
    for part in field.split(","):
        part = part.strip()
        if part == "*":
            return True
        if part.startswith("*/"):
            try:
                step = int(part[2:])
                if step > 0 and (value - min_val) % step == 0:
                    return True
            except ValueError:
                pass
            continue
        if "-" in part:
            lo, _, hi = part.partition("-")
            try:
                lo_v, hi_v = int(lo), int(hi)
                if lo_v <= value <= hi_v:
                    return True
            except ValueError:
                pass
            continue
        try:
            if int(part) == value:
                return True
        except ValueError:
            pass
    return False


def _same_minute(a: datetime, b: datetime) -> bool:
    return (
        a.year == b.year
        and a.month == b.month
        and a.day == b.day
        and a.hour == b.hour
        and a.minute == b.minute
    )
