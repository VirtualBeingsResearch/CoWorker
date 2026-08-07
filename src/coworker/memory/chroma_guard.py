"""Serialize every call into the shared ChromaDB connection.

chromadb >= 1.x routes embedded operations through a Rust core
(``chromadb_rust_bindings``) whose connection lives in a ``RefCell`` and is
**not thread-safe**: when two threads enter the same client at the same time,
the second one hits a double mutable borrow and PyO3 surfaces
``RuntimeError: Already borrowed``.

CoWorker funnels several subsystems through one shared Chroma client:

- mem0's long-term memory (``LongTermMemory`` -> ``AsyncMemory`` -> chroma),
- the recent-activity index (``RecentActivityMemory`` reuses
  ``LongTermMemory.chroma_client``).

Each subsystem has its own lock, so none of them can serialize against the
others. This module wraps a Chroma client/collection with thin proxies that
acquire one shared module-level ``RLock`` around every operation, in the
calling thread, right at the FFI boundary.

Notes / limitations
-------------------
- The proxies forward attribute access as-is; only *calls* are locked. Plain
  attributes (``collection.name``, ``collection.id`` ...) are returned raw.
- ``isinstance(x, chromadb.api.models.Collection)`` checks fail on the proxy;
  no subsystem in this repo relies on that.

Observability
-------------
Lock holds are short (single Chroma FFI calls, typically milliseconds), but if
a call ever has to wait on the shared lock longer than
``COWORKER_CHROMA_LOCK_WARN_MS`` (default 50 ms), a ``WARNING`` is logged with
the operation name and the wait duration. This makes it visible whether a
foreground write (``manage_memory``) is being queued behind background
activity-indexing Chroma calls.
"""

from __future__ import annotations

import os
import threading
import time
from collections.abc import Callable
from typing import Any

from loguru import logger

_LOCK = threading.RLock()

# Warn when a Chroma call had to wait on the shared lock longer than this.
_SLOW_LOCK_WARN_MS = float(os.environ.get("COWORKER_CHROMA_LOCK_WARN_MS", "50"))

# Client methods whose return value is a collection and must therefore be
# wrapped in a collection proxy (so operations on it stay serialized too).
_COLLECTION_RETURNING = frozenset(
    {"get_collection", "get_or_create_collection", "create_collection"}
)


class _ChromaCollectionProxy:
    """Lock-guarded stand-in for a ``chromadb`` Collection."""

    def __init__(self, collection: Any) -> None:
        object.__setattr__(self, "_chroma_inner", collection)

    def __getattr__(self, name: str) -> Any:
        inner = object.__getattribute__(self, "_chroma_inner")
        attr = getattr(inner, name)
        if callable(attr):
            return _locked_callable(attr)
        return attr


class _ChromaClientProxy:
    """Lock-guarded stand-in for a ``chromadb`` Client.

    Operations that return a collection are additionally wrapped in a
    collection proxy so downstream calls on that collection are serialized
    under the same lock.
    """

    def __init__(self, client: Any) -> None:
        object.__setattr__(self, "_chroma_inner", client)

    def __getattr__(self, name: str) -> Any:
        inner = object.__getattribute__(self, "_chroma_inner")
        attr = getattr(inner, name)
        if name in _COLLECTION_RETURNING and callable(attr):
            return _locked_collection_returning(attr)
        if callable(attr):
            return _locked_callable(attr)
        return attr


def _locked_callable(fn: Callable[..., Any]) -> Callable[..., Any]:
    return _make_locked_callable(fn)


def _locked_collection_returning(fn: Callable[..., Any]) -> Callable[..., Any]:
    return _make_locked_callable(fn, transform=_ChromaCollectionProxy)


def _make_locked_callable(
    fn: Callable[..., Any],
    *,
    transform: Callable[[Any], Any] | None = None,
    warn_ms: float | None = None,
) -> Callable[..., Any]:
    """Wrap ``fn`` so it runs under the shared lock, timing the wait.

    If the call had to wait on the lock longer than the warning threshold (the
    module-level ``_SLOW_LOCK_WARN_MS`` unless ``warn_ms`` overrides it for
    tests), a ``WARNING`` is logged so lock contention stays observable.
    ``transform`` optionally rewrites the result (used to wrap returned
    collections in a proxy).
    """

    def wrapped(*args: Any, **kwargs: Any) -> Any:
        started = time.monotonic()
        with _LOCK:
            wait_ms = (time.monotonic() - started) * 1000.0
            threshold = _SLOW_LOCK_WARN_MS if warn_ms is None else warn_ms
            if wait_ms > threshold:
                logger.warning(
                    "Slow Chroma lock wait: {op} waited {wait_ms:.1f}ms "
                    "(threshold {threshold:.0f}ms)",
                    op=_op_label(fn),
                    wait_ms=wait_ms,
                    threshold=threshold,
                )
            result = fn(*args, **kwargs)
        if transform is not None:
            return transform(result)
        return result

    return wrapped


def _op_label(fn: Callable[..., Any]) -> str:
    owner = getattr(fn, "__self__", None)
    name = getattr(fn, "__name__", repr(fn))
    if owner is not None:
        return f"{type(owner).__name__}.{name}"
    return name


def guarded_chroma_client(client: Any) -> Any:
    """Wrap a Chroma client so every operation serializes on the shared lock."""
    return _ChromaClientProxy(client)


def guarded_chroma_collection(collection: Any) -> Any:
    """Wrap a Chroma collection so every operation serializes on the shared lock."""
    return _ChromaCollectionProxy(collection)
