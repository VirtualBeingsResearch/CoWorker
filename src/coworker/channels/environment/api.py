"""The API surface injected into inline environment sources.

An inline source is plain Python that defines ``poll(ctx)``.  ``ctx`` is a
:class:`SourceContext` — the source's handle to the host: it can emit signals,
persist a cursor between polls, check dedup state, log, and share an
``httpx.AsyncClient``.

This object is deliberately small and explicit.  Each method maps to a concept
sources actually need; we avoid exposing host internals (brain, memory, tool
registry) to keep the source contract stable and auditable.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

from loguru import logger

from .types import EnvironmentSignal, SourceScheduleState

if TYPE_CHECKING:
    import httpx


EmitCallback = Callable[[EnvironmentSignal], None]


class SourceContext:
    """Host-provided API for an inline environment source.

    A fresh instance is created for each poll.  ``state`` is shared across
    polls for the same source, so cursors and fingerprints persist.
    """

    def __init__(
        self,
        *,
        source_id: str,
        config: dict[str, Any],
        state: SourceScheduleState,
        http: httpx.AsyncClient | None = None,
        emit: EmitCallback | None = None,
        logger: Any = None,
    ) -> None:
        self.source_id = source_id
        self.config = config
        self.http = http
        self._state = state
        self._emit = emit or (lambda _signal: None)
        self._logger = logger

    @property
    def logger(self) -> Any:
        """A loguru logger bound to this source, lazily created."""
        if self._logger is not None:
            return self._logger
        return logger.bind(source=self.source_id)

    # --- signal emission ---------------------------------------------------

    def emit_signal(
        self,
        *,
        title: str,
        content: str,
        fingerprint: str,
        url: str | None = None,
        severity: str = "info",
    ) -> bool:
        """Emit one environment signal.

        Returns ``True`` if the signal was accepted (fingerprint not seen
        before), ``False`` if it was deduplicated.  Sources may use the return
        value to decide whether to update their cursor eagerly.
        """
        if not fingerprint:
            raise ValueError("emit_signal requires a non-empty fingerprint")
        if fingerprint in self._state.known_fingerprints:
            return False
        signal = EnvironmentSignal(
            source_id=self.source_id,
            title=title,
            content=content,
            fingerprint=fingerprint,
            url=url,
            severity=severity,  # type: ignore[arg-type]
        )
        self._state.known_fingerprints.add(fingerprint)
        self._emit(signal)
        return True

    # --- cursor / dedup state ---------------------------------------------

    def get_cursor(self) -> str | None:
        """Return the cursor saved by the previous successful poll."""
        return self._state.cursor

    def set_cursor(self, cursor: str) -> None:
        """Persist a cursor for the next poll.

        Sources decide the cursor's meaning — typically a timestamp, an ETag,
        or the id of the last-seen item.
        """
        self._state.cursor = cursor

    def is_known(self, fingerprint: str) -> bool:
        """Check whether a fingerprint has already been emitted."""
        return fingerprint in self._state.known_fingerprints


async def poll_with_context(
    poll_fn: Callable[..., Any],
    ctx: SourceContext,
) -> Awaitable[None] | None:
    """Invoke a source's ``poll`` function, awaiting if it is async.

    ``poll_fn`` may be ``def poll(ctx)`` or ``async def poll(ctx)``.  We detect
    coroutines at call time so sources can choose either signature freely.
    """
    result = poll_fn(ctx)
    if __import__("asyncio").iscoroutine(result):
        await result
    return None
