"""Backwards-compatible re-exports for the stream channel wire layer.

The connection pool and wire-format helpers moved to
:mod:`coworker.channels.stream`. This module keeps the historical
``coworker.api.ws`` import path working for callers (``api/app.py`` and
tests) that still import ``ConnectionPool`` / ``serialize_outbound_message``
/ ``SHUTDOWN_SENTINEL`` from here.
"""

from __future__ import annotations

from coworker.channels.stream.connection_pool import ConnectionPool
from coworker.channels.stream.wire import SHUTDOWN_SENTINEL, serialize_outbound_message

__all__ = ["ConnectionPool", "SHUTDOWN_SENTINEL", "serialize_outbound_message"]
