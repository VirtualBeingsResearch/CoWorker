"""Process-wide virtual clock that fast-forwards only while the instance is idle.

Wall clock is driven by ``time-machine``. ``time.monotonic`` (and therefore
``asyncio`` timers) share the same offset. Selector waits jump only when no
executor work, outbound HTTP, or child process is in flight.

Outbound HTTP includes both ``httpx.AsyncClient`` and the OpenAI SDK's
``httpx2.AsyncClient``. Wrapping only ``httpx`` lets a model call sit on the
proactor while idle sleeps jump past the SDK timeout.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

import httpx
import time_machine

_REAL_MONOTONIC = time.monotonic
real_monotonic = _REAL_MONOTONIC


def _async_http_client_classes() -> list[Any]:
    """Async HTTP clients whose in-flight ``send`` must block virtual jumps."""
    classes: list[Any] = [httpx.AsyncClient]
    try:
        import httpx2
    except ImportError:
        return classes
    client = getattr(httpx2, "AsyncClient", None)
    if client is not None and client is not httpx.AsyncClient:
        classes.append(client)
    return classes


@dataclass
class Jump:
    at: str
    seconds: float
    pending: str


class VirtualClock:
    def __init__(self) -> None:
        self._traveller: Any = None
        self._offset = 0.0
        self.executor = 0
        self.http = 0
        self.procs = 0
        self.jumps: list[Jump] = []
        self._orig_http_sends: list[tuple[Any, Any]] = []
        self._orig_exec: Any = None
        self._orig_shell: Any = None
        self._orig_rie: Any = None
        self._orig_select: Any = None
        self._selector: Any = None
        self._loop: Any = None
        self._proc_tasks: list[asyncio.Task[None]] = []
        self._holds = 0

    def monotonic(self) -> float:
        return _REAL_MONOTONIC() + self._offset

    @property
    def busy(self) -> bool:
        return self.executor > 0 or self.http > 0 or self.procs > 0 or self._holds > 0

    @contextmanager
    def hold(self) -> Iterator[None]:
        """Block jumps while waiting on real-world setup (API up, parent SSE)."""
        self._holds += 1
        try:
            yield
        finally:
            self._holds = max(0, self._holds - 1)

    def advance(self, seconds: float, *, pending: str = "") -> None:
        if seconds <= 0:
            return
        self._offset += seconds
        if self._traveller is not None:
            self._traveller.shift(timedelta(seconds=seconds))
        if seconds >= 1.0:
            self.jumps.append(
                Jump(at=datetime.now().isoformat(), seconds=round(seconds, 3), pending=pending)
            )

    def install(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop
        selector = getattr(loop, "_selector", None)
        if selector is None:
            raise RuntimeError("event loop has no selector to wrap")
        original_select = selector.select

        def select(timeout: float | None = None) -> Any:
            if timeout is None or timeout <= 0 or self.busy:
                return original_select(timeout)
            events = original_select(0)
            if events:
                return events
            self.advance(float(timeout), pending="selector")
            return original_select(0)

        self._selector = selector
        self._orig_select = original_select
        selector.select = select

        original_rie = loop.run_in_executor
        self._orig_rie = original_rie

        def run_in_executor(executor: Any, func: Any, *args: Any) -> Any:
            future = original_rie(executor, func, *args)
            self.executor += 1
            future.add_done_callback(lambda _f: self._release("executor"))
            return future

        setattr(loop, "run_in_executor", run_in_executor)
        self._wrap_http_clients()
        self._wrap_subprocess()

    def _release(self, kind: str) -> None:
        if kind == "executor":
            self.executor = max(0, self.executor - 1)
        elif kind == "http":
            self.http = max(0, self.http - 1)
        elif kind == "proc":
            self.procs = max(0, self.procs - 1)

    def restore(self) -> None:
        if self._selector is not None and self._orig_select is not None:
            self._selector.select = self._orig_select
        if self._loop is not None and self._orig_rie is not None:
            setattr(self._loop, "run_in_executor", self._orig_rie)
        for cls, original in self._orig_http_sends:
            setattr(cls, "send", original)
        self._orig_http_sends.clear()
        if self._orig_exec is not None:
            setattr(asyncio, "create_subprocess_exec", self._orig_exec)
        if self._orig_shell is not None:
            setattr(asyncio, "create_subprocess_shell", self._orig_shell)

    def _wrap_http_clients(self) -> None:
        for cls in _async_http_client_classes():
            original = cls.send

            async def send(
                client: Any,
                request: Any,
                *args: Any,
                _original: Any = original,
                **kwargs: Any,
            ) -> Any:
                self.http += 1
                try:
                    return await _original(client, request, *args, **kwargs)
                finally:
                    self._release("http")

            self._orig_http_sends.append((cls, original))
            setattr(cls, "send", send)

    def _wrap_subprocess(self) -> None:
        original_exec = asyncio.create_subprocess_exec
        original_shell = asyncio.create_subprocess_shell
        self._orig_exec = original_exec
        self._orig_shell = original_shell

        async def _track(factory: Any, *args: Any, **kwargs: Any) -> Any:
            proc = await factory(*args, **kwargs)
            self.procs += 1
            self._proc_tasks.append(asyncio.create_task(self._await_proc(proc)))
            return proc

        async def create_exec(*args: Any, **kwargs: Any) -> Any:
            return await _track(original_exec, *args, **kwargs)

        async def create_shell(*args: Any, **kwargs: Any) -> Any:
            return await _track(original_shell, *args, **kwargs)

        setattr(asyncio, "create_subprocess_exec", create_exec)
        setattr(asyncio, "create_subprocess_shell", create_shell)

    async def _await_proc(self, proc: Any) -> None:
        try:
            await proc.wait()
        finally:
            self._release("proc")


@contextmanager
def install_clock(start: datetime) -> Iterator[VirtualClock]:
    clock = VirtualClock()
    original = time.monotonic
    with time_machine.travel(start, tick=True) as traveller:
        clock._traveller = traveller
        setattr(time, "monotonic", clock.monotonic)
        try:
            yield clock
        finally:
            setattr(time, "monotonic", original)
