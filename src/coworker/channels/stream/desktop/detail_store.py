"""Backward-compatible re-export of the shared :class:`DetailStore`."""

from __future__ import annotations

from coworker.channels.detail_store import (  # noqa: F401
    _DETAIL_MAX_AGE_SECONDS,
    _DETAIL_MAX_FILES,
    _DETAIL_SUBDIR,
    DetailStore,
    _safe,
)

__all__ = [
    "DetailStore",
    "_DETAIL_MAX_AGE_SECONDS",
    "_DETAIL_MAX_FILES",
    "_DETAIL_SUBDIR",
    "_safe",
]
