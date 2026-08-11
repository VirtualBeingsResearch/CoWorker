"""Persist per-source schedule state (cursor, fingerprints, run history).

State is kept in a single JSON file (``data/environment/state.json``) keyed by
source name.  Writes are atomic (temp file + ``os.replace``) mirroring
``PersonStore``.
"""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
from pathlib import Path

from loguru import logger

from .types import SourceScheduleState


def _atomic_write_text(path: Path, content: str) -> None:
    """Atomically replace ``path`` with ``content`` (tmp + os.replace)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=path.parent,
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


class SourceStateStore:
    """Thread-safe-ish (asyncio.Lock) JSON store for source schedule state."""

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)
        self._states: dict[str, SourceScheduleState] = {}
        self._lock = asyncio.Lock()
        self._loaded = False

    async def load(self) -> dict[str, SourceScheduleState]:
        async with self._lock:
            if self._loaded:
                return dict(self._states)
            self._loaded = True
            if not self._path.is_file():
                return dict(self._states)
            try:
                raw = json.loads(self._path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                logger.warning(f"Environment state file {self._path} unreadable: {exc}")
                return dict(self._states)
            if not isinstance(raw, dict):
                return dict(self._states)
            self._states = {
                name: SourceScheduleState.from_dict(data if isinstance(data, dict) else {})
                for name, data in raw.items()
            }
            return dict(self._states)

    def get(self, source_id: str) -> SourceScheduleState:
        """Return the state for ``source_id``, creating an empty one if absent."""
        if source_id not in self._states:
            self._states[source_id] = SourceScheduleState()
        return self._states[source_id]

    async def save(self) -> None:
        async with self._lock:
            payload = {name: state.to_dict() for name, state in self._states.items()}
            try:
                _atomic_write_text(self._path, json.dumps(payload, indent=2, ensure_ascii=False))
            except OSError as exc:
                logger.warning(f"Failed to persist environment state: {exc}")

    async def all_states(self) -> dict[str, SourceScheduleState]:
        async with self._lock:
            return dict(self._states)
