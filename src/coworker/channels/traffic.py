"""Metadata-only Channel traffic history for local administration."""

from __future__ import annotations

import json
import threading
from collections import deque
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from loguru import logger

from coworker.i18n import tr

TrafficDirection = Literal["inbound", "outbound"]
TrafficStatus = Literal["received", "sent", "denied", "failed", "duplicate"]

_DEFAULT_MAX_BYTES = 10 * 1024 * 1024
_DEFAULT_BACKUPS = 6
_DEFAULT_MEMORY_LIMIT = 1000
_READ_CHUNK_BYTES = 64 * 1024


class ChannelTrafficStore:
    """Append bounded, message-body-free traffic metadata to JSONL."""

    def __init__(
        self,
        path: str | Path | None = None,
        *,
        max_bytes: int = _DEFAULT_MAX_BYTES,
        backups: int = _DEFAULT_BACKUPS,
        memory_limit: int = _DEFAULT_MEMORY_LIMIT,
    ) -> None:
        self._path = Path(path) if path is not None else None
        self._max_bytes = max(1, max_bytes)
        self._backups = max(0, backups)
        self._memory: deque[dict[str, str]] = deque(maxlen=max(1, memory_limit))
        self._lock = threading.Lock()
        # Cumulative (channel, direction, status) counts. Built lazily from the
        # retained JSONL the first time totals() is read, then maintained
        # incrementally by record(); query-only stores never pay the scan.
        self._totals: dict[str, dict[str, dict[str, int]]] | None = None

    @property
    def path(self) -> Path | None:
        return self._path

    def record(
        self,
        *,
        direction: TrafficDirection,
        channel: str,
        participant_id: str,
        status: TrafficStatus,
        source: str = "",
        reason: str = "",
    ) -> None:
        entry = {
            "ts": datetime.now(UTC).isoformat(),
            "direction": direction,
            "channel": str(channel)[:80],
            "participant_id": str(participant_id)[:400],
            "status": status,
            "source": str(source)[:80],
            "reason": str(reason)[:80],
        }
        with self._lock:
            self._memory.append(entry)
            self._bump_totals(entry)
            if self._path is None:
                return
            try:
                self._append(entry)
            except OSError as error:
                logger.warning(tr("channel.traffic.write_failed", error=error))

    def totals(self) -> dict[str, dict[str, dict[str, int]]]:
        """Cumulative message counts per channel, direction and status.

        Counts stay monotonic for the lifetime of this store instance. A store
        rebuilt after a restart only sees the retained traffic window (rotated
        -away backups no longer contribute), so counters may reset lower then.
        """
        with self._lock:
            totals = self._ensure_totals()
            return {
                channel: {
                    direction: dict(status_counts)
                    for direction, status_counts in directions.items()
                }
                for channel, directions in totals.items()
            }

    def _ensure_totals(self) -> dict[str, dict[str, dict[str, int]]]:
        assert self._lock.locked()
        if self._totals is None:
            totals: dict[str, dict[str, dict[str, int]]] = {}
            if self._path is not None:
                for entry in self._iter_persisted_entries():
                    _apply_totals(totals, entry)
            else:
                for entry in self._memory:
                    _apply_totals(totals, entry)
            self._totals = totals
        return self._totals

    def _bump_totals(self, entry: dict[str, str]) -> None:
        assert self._lock.locked()
        if self._totals is not None:
            _apply_totals(self._totals, entry)

    def recent(
        self,
        limit: int,
        *,
        direction: TrafficDirection | None = None,
        status: TrafficStatus | None = None,
        channel: str = "",
    ) -> list[dict[str, str]]:
        bounded_limit = max(0, limit)
        if bounded_limit == 0:
            return []
        normalized_channel = channel.strip()
        entries: list[dict[str, str]] = []
        with self._lock:
            source = (
                self._iter_persisted_newest_first()
                if self._path is not None
                else reversed(self._memory)
            )
            for entry in source:
                if direction is not None and entry.get("direction") != direction:
                    continue
                if status is not None and entry.get("status") != status:
                    continue
                if normalized_channel and entry.get("channel") != normalized_channel:
                    continue
                entries.append(entry)
                if len(entries) >= bounded_limit:
                    break
        return entries

    def _append(self, entry: dict[str, str]) -> None:
        assert self._path is not None
        line = json.dumps(entry, ensure_ascii=False, separators=(",", ":")) + "\n"
        encoded_size = len(line.encode("utf-8"))
        self._path.parent.mkdir(parents=True, exist_ok=True)
        if self._path.is_file() and self._path.stat().st_size + encoded_size > self._max_bytes:
            self._rotate()
        with self._path.open("a", encoding="utf-8") as handle:
            handle.write(line)

    def _rotate(self) -> None:
        assert self._path is not None
        if self._backups == 0:
            self._path.unlink(missing_ok=True)
            return
        oldest = self._backup_path(self._backups)
        oldest.unlink(missing_ok=True)
        for index in range(self._backups - 1, 0, -1):
            current = self._backup_path(index)
            if current.exists():
                current.replace(self._backup_path(index + 1))
        if self._path.exists():
            self._path.replace(self._backup_path(1))

    def _iter_persisted_newest_first(self) -> Iterator[dict[str, str]]:
        assert self._path is not None
        paths = [
            self._path,
            *(self._backup_path(index) for index in range(1, self._backups + 1)),
        ]
        for path in paths:
            if not path.is_file():
                continue
            try:
                for line in _iter_lines_newest_first(path):
                    try:
                        entry = json.loads(line)
                    except (json.JSONDecodeError, TypeError, UnicodeDecodeError):
                        continue
                    if isinstance(entry, dict):
                        yield {str(key): str(value) for key, value in entry.items()}
            except OSError as error:
                logger.warning(tr("channel.traffic.read_failed", error=error))

    def _iter_persisted_entries(self) -> Iterator[dict[str, str]]:
        """Yield retained entries oldest-first for totals rebuilding."""
        assert self._path is not None
        paths = [
            *(self._backup_path(index) for index in range(self._backups, 0, -1)),
            self._path,
        ]
        for path in paths:
            if not path.is_file():
                continue
            try:
                with path.open("r", encoding="utf-8") as handle:
                    for line in handle:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            entry = json.loads(line)
                        except (json.JSONDecodeError, UnicodeDecodeError):
                            continue
                        if isinstance(entry, dict):
                            yield {str(key): str(value) for key, value in entry.items()}
            except OSError as error:
                logger.warning(tr("channel.traffic.read_failed", error=error))

    def _backup_path(self, index: int) -> Path:
        assert self._path is not None
        return self._path.with_name(f"{self._path.name}.{index}")


def _iter_lines_newest_first(path: Path) -> Iterator[bytes]:
    """Yield complete lines from the end without loading a rotated log into memory."""
    with path.open("rb") as handle:
        position = handle.seek(0, 2)
        remainder = b""
        while position > 0:
            read_size = min(position, _READ_CHUNK_BYTES)
            position -= read_size
            handle.seek(position)
            parts = (handle.read(read_size) + remainder).split(b"\n")
            remainder = parts[0]
            for line in reversed(parts[1:]):
                if line:
                    yield line
        if remainder:
            yield remainder


def _apply_totals(
    totals: dict[str, dict[str, dict[str, int]]],
    entry: dict[str, str],
) -> None:
    channel = entry.get("channel") or "unknown"
    direction = entry.get("direction") or "unknown"
    status = entry.get("status") or "unknown"
    channel_totals = totals.setdefault(channel, {})
    channel_totals.setdefault(direction, {})
    counts = channel_totals[direction]
    counts[status] = counts.get(status, 0) + 1
