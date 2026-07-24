"""Folded-prompt detail store for the Desktop stream profile.

When a rendered desktop prompt exceeds the fold threshold, its full text is
persisted here (keyed by request_id / message_id) so the coworker can
``read_file`` it on demand instead of carrying the whole block in context.

Extracted from :class:`DesktopRegistry` (which now composes this) so the
registry is left with actor-state + pinned-context responsibilities only.
"""

from __future__ import annotations

import time
from pathlib import Path

from loguru import logger

_DETAIL_SUBDIR = "detail"
_DETAIL_MAX_FILES = 200
_DETAIL_MAX_AGE_SECONDS = 7 * 24 * 3600


def _safe(value: str) -> str:
    safe = "".join(ch if ch.isalnum() or ch in "_.-" else "-" for ch in value)
    return safe.strip(".-") or "unknown"


class DetailStore:
    def __init__(self, root: str | Path) -> None:
        self._dir = Path(root)

    def detail_path(self, key: str) -> Path:
        return self._dir / _DETAIL_SUBDIR / f"{_safe(key)}.txt"

    def write_detail(self, key: str, text: str) -> Path:
        """Persist a folded prompt's full text for lazy ``read_file`` retrieval.

        The dispatcher folds long rendered blocks and writes the full content
        here keyed by ``request_id``/``message_id``; the inline prompt keeps a
        head summary plus a pointer to the returned path.
        """
        destination = self.detail_path(key)
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_suffix(".tmp")
        temporary.write_text(text, encoding="utf-8")
        temporary.replace(destination)
        self.prune()
        # Absolute path so the coworker's `read_file` resolves regardless of CWD.
        return destination.resolve()

    def prune(self) -> None:
        directory = self._dir / _DETAIL_SUBDIR
        if not directory.is_dir():
            return
        try:
            files = [path for path in directory.iterdir() if path.is_file()]
        except OSError as error:
            logger.warning(f"Failed to list desktop detail dir {directory}: {error}")
            return
        cutoff = time.time() - _DETAIL_MAX_AGE_SECONDS
        expired = [path for path in files if self._mtime(path) < cutoff]
        expired_set = set(expired)
        for path in expired:
            self._unlink(path)
        survivors = [path for path in files if path not in expired_set]
        excess = len(survivors) - _DETAIL_MAX_FILES
        if excess <= 0:
            return
        survivors.sort(key=self._mtime)
        for path in survivors[:excess]:
            self._unlink(path)

    @staticmethod
    def _mtime(path: Path) -> float:
        try:
            return path.stat().st_mtime
        except OSError:
            return float("inf")

    @staticmethod
    def _unlink(path: Path) -> None:
        try:
            path.unlink()
        except OSError as error:
            logger.warning(f"Failed to prune desktop detail file {path}: {error}")
