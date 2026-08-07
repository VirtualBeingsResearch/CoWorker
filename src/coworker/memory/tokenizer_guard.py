"""Serialize every call into the shared HuggingFace tokenizer.

sentence-transformers embeds text through a ``transformers`` fast tokenizer
whose backend is a ``tokenizers`` Rust object wrapped in a PyO3 ``RefCell``.
That object is **not thread-safe**: two threads entering it at once raise
``RuntimeError: Already borrowed``. The failure mode is a mismatch between
the two kinds of borrow the binding takes:

- ``encode_batch``/``encode`` hold a *shared* borrow for the whole call and
  release the GIL (``py.detach``) while tokenizing the batch, and
- ``set_truncation_and_padding`` then calls ``enable_truncation`` /
  ``no_truncation`` / ``enable_padding``, which take an *exclusive* borrow.

So a batch encode running on one executor thread (activity indexing) while
another thread embeds a mem0 query/write trips the exclusive borrow with
``RuntimeError: Already borrowed``.

CoWorker funnels several subsystems through one shared tokenizer:

- mem0's long-term memory (``LongTermMemory`` -> ``AsyncMemory`` -> embedder),
- the recent-activity index (``RecentActivityMemory`` reuses the same
  ``SentenceTransformer`` and its tokenizer for chunking).

This module wraps the Rust ``tokenizers.Tokenizer`` with a thin proxy that
acquires one shared module-level ``RLock`` around every operation, in the
calling thread, right at the FFI boundary -- the same approach as
:mod:`coworker.memory.chroma_guard` for the shared Chroma connection.

Notes / limitations
-------------------
- The proxy forwards attribute access as-is; only *calls* are locked. Plain
  attributes (``tokenizer.truncation``, ``tokenizer.padding`` ...) are
  returned raw.
- ``isinstance(x, tokenizers.Tokenizer)`` checks fail on the proxy; no
  subsystem in this repo relies on that.
- The proxy is installed as ``wrapper._tokenizer`` on the transformers
  wrapper, so every caller that goes through the wrapper (sentence-transformers'
  ``preprocess``, ``RecentActivityMemory``'s ``tokenizer.encode``) serializes
  on the same lock. Code holding a raw reference to the Rust object would
  bypass it.

Observability
-------------
Lock holds are the duration of one Rust tokenizer call (tokenize or a batch
tokenize, typically milliseconds up to about a second for a large batch). If a
call ever has to wait on the shared lock longer than
``COWORKER_TOKENIZER_LOCK_WARN_MS`` (default 50 ms), a ``WARNING`` is logged
with the operation name and the wait duration, so contention between a
foreground write and background indexing stays observable.
"""

from __future__ import annotations

import os
import threading
import time
from collections.abc import Callable
from typing import Any

from loguru import logger

_LOCK = threading.RLock()

# Warn when a tokenizer call had to wait on the shared lock longer than this.
_SLOW_LOCK_WARN_MS = float(os.environ.get("COWORKER_TOKENIZER_LOCK_WARN_MS", "50"))


class _TokenizerProxy:
    """Lock-guarded stand-in for a ``tokenizers.Tokenizer``."""

    def __init__(self, tokenizer: Any) -> None:
        object.__setattr__(self, "_tokenizer_inner", tokenizer)

    def __getattr__(self, name: str) -> Any:
        inner = object.__getattribute__(self, "_tokenizer_inner")
        attr = getattr(inner, name)
        if callable(attr):
            return _make_locked_callable(attr)
        return attr


def guarded_tokenizer(tokenizer: Any) -> Any:
    """Wrap a ``tokenizers.Tokenizer`` so every call serializes on the lock."""
    return _TokenizerProxy(tokenizer)


def _make_locked_callable(
    fn: Callable[..., Any],
    *,
    warn_ms: float | None = None,
) -> Callable[..., Any]:
    """Wrap ``fn`` so it runs under the shared lock, timing the wait.

    If the call had to wait on the lock longer than the warning threshold
    (``_SLOW_LOCK_WARN_MS`` unless ``warn_ms`` overrides it for tests), a
    ``WARNING`` is logged so lock contention stays observable.
    """

    def wrapped(*args: Any, **kwargs: Any) -> Any:
        started = time.monotonic()
        with _LOCK:
            wait_ms = (time.monotonic() - started) * 1000.0
            threshold = _SLOW_LOCK_WARN_MS if warn_ms is None else warn_ms
            if wait_ms > threshold:
                logger.warning(
                    "Slow tokenizer lock wait: {op} waited {wait_ms:.1f}ms "
                    "(threshold {threshold:.0f}ms)",
                    op=_op_label(fn),
                    wait_ms=wait_ms,
                    threshold=threshold,
                )
            return fn(*args, **kwargs)

    return wrapped


def _op_label(fn: Callable[..., Any]) -> str:
    owner = getattr(fn, "__self__", None)
    name = getattr(fn, "__name__", repr(fn))
    if owner is not None:
        return f"{type(owner).__name__}.{name}"
    return name
