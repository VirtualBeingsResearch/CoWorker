from __future__ import annotations

import asyncio
import hashlib
import json
import os
import secrets
import sqlite3
from collections import Counter
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

from loguru import logger

from coworker.core.autonomy import AutonomyLevel
from coworker.core.ids import new_compact_id
from coworker.core.types import AttachmentData, IncomingEvent
from coworker.i18n import tr

_IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".webp"}
_PDF_EXTS = {".pdf"}
_TEXT_EXTS = {
    ".txt", ".csv", ".py", ".js", ".ts", ".json", ".yaml", ".yml",
    ".md", ".xml", ".html", ".css", ".sh",
}

_MEDIA_TYPES: dict[str, str] = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".pdf": "application/pdf",
    ".mp4": "video/mp4",
    ".mov": "video/quicktime",
    ".avi": "video/x-msvideo",
    ".webm": "video/webm",
    ".mkv": "video/x-matroska",
    ".flv": "video/x-flv",
    ".wmv": "video/x-ms-wmv",
    ".txt": "text/plain",
    ".csv": "text/csv",
    ".py": "text/x-python",
    ".js": "text/javascript",
    ".ts": "text/typescript",
    ".json": "application/json",
    ".yaml": "application/yaml",
    ".yml": "application/yaml",
    ".xml": "text/xml",
    ".html": "text/html",
    ".css": "text/css",
    ".sh": "text/x-sh",
}


def _guess_media_type(suffix: str) -> str:
    return _MEDIA_TYPES.get(suffix.lower(), "application/octet-stream")


def _copy_private_file(source: Path, destination: Path) -> None:
    """Copy an attachment without ever exposing a broad-permission destination."""
    descriptor = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with source.open("rb") as source_file, os.fdopen(descriptor, "wb") as destination_file:
            descriptor = -1
            while chunk := source_file.read(1024 * 1024):
                destination_file.write(chunk)
    except BaseException:
        if descriptor >= 0:
            os.close(descriptor)
        destination.unlink(missing_ok=True)
        raise


class InboxWatcher:
    def __init__(
        self,
        inbox_dir: str,
        poll_interval: float = 2.0,
        pending_path: str | Path | None = None,
    ) -> None:
        self._inbox = Path(inbox_dir)
        self._processed = self._inbox / "processed"
        self._attachments = self._inbox.parent / "attachments"
        self._poll_interval = poll_interval
        self._pending: list[IncomingEvent] = []
        self._pending_ids: set[str] = set()
        self._wake_counts: Counter[AutonomyLevel] = Counter()
        self._running = False
        self._message_event = asyncio.Event()
        self._stop_event = asyncio.Event()
        self._interceptors: list[Callable[[IncomingEvent], bool]] = []
        self._pending_path = Path(pending_path) if pending_path is not None else None
        self._pending_db: sqlite3.Connection | None = None
        self._initialize_pending_store()
        self._load_pending()

    def set_interceptor(self, interceptor: Callable[[IncomingEvent], bool] | None) -> None:
        """Replace all inbound interceptors (backwards-compatible single-hook API)."""
        self._interceptors = [interceptor] if interceptor is not None else []

    def add_interceptor(self, interceptor: Callable[[IncomingEvent], bool]) -> None:
        """Append an inbound interceptor.

        Interceptors run in registration order; returning True consumes the
        event and prevents later interceptors and the main inbox from seeing it.
        """
        self._interceptors.append(interceptor)

    @property
    def message_event(self) -> asyncio.Event:
        return self._message_event

    @property
    def poll_interval(self) -> float:
        return self._poll_interval

    @poll_interval.setter
    def poll_interval(self, value: float) -> None:
        self._poll_interval = value

    async def push(self, event: IncomingEvent) -> str:
        event_id = event.event_id or secrets.token_hex(8)
        event.event_id = event_id
        # Attachments are already persisted by every channel boundary. Keeping a
        # second base64 copy in a paused L0/L1 queue can exhaust RAM; content
        # blocks reload it lazily from saved_path when the event is processed.
        for attachment in event.attachments:
            if attachment.saved_path and Path(attachment.saved_path).is_file():
                attachment.data = None
        for interceptor in self._interceptors:
            if interceptor(event):
                return event_id
        if event_id in self._pending_ids:
            return event_id
        if not self._persist_event(event):
            return event_id
        self._pending.append(event)
        self._pending_ids.add(event_id)
        if event.wake_level is not None:
            self._wake_counts[event.wake_level] += 1
        self._message_event.set()
        return event_id

    def cancel(self, event_id: str) -> None:
        """直接从队列中移除对应事件；事件不在队列中则静默忽略。"""
        self._delete_events([event_id])
        if not self._pending:
            self._message_event.clear()

    async def peek_pending(self, limit: int | None = None) -> list[IncomingEvent]:
        events = self._pending if limit is None else self._pending[:limit]
        return list(events)

    async def peek_claimable(
        self,
        predicate: Callable[[IncomingEvent], bool],
        limit: int,
    ) -> list[IncomingEvent]:
        """Select a policy-eligible batch without letting blocked FIFO heads starve it.

        Selection preserves queue order among eligible events.  If buffered,
        non-wakeable notices fill the front of a small batch, one slot is
        reserved for the oldest eligible wakeable event so the activation that
        caused this cycle is always actually consumed.
        """

        if limit <= 0:
            return []
        eligible = [event for event in self._pending if predicate(event)]
        selected = eligible[:limit]
        oldest_wakeable = next(
            (event for event in eligible if event.wake_level is not None),
            None,
        )
        if oldest_wakeable is not None and oldest_wakeable not in selected:
            selected[-1] = oldest_wakeable
        return list(selected)

    async def acknowledge(self, event_ids: list[str]) -> None:
        self._delete_events(event_ids)
        if not self._pending:
            self._message_event.clear()

    async def get_pending(self) -> list[IncomingEvent]:
        """Compatibility helper for consumers that intentionally drain the queue."""
        events = await self.peek_pending()
        await self.acknowledge(
            [event.event_id for event in events if event.event_id is not None]
        )
        return events

    @property
    def pending_count(self) -> int:
        return len(self._pending)

    def pending_events(self) -> list[IncomingEvent]:
        return list(self._pending)

    def pending_wake_levels(self) -> set[AutonomyLevel]:
        return {level for level, count in self._wake_counts.items() if count > 0}

    def pending_by_wake_level(self) -> dict[AutonomyLevel, int]:
        return {
            level: count
            for level, count in self._wake_counts.items()
            if count > 0
        }

    @property
    def buffered_pending_count(self) -> int:
        return max(0, len(self._pending) - sum(self._wake_counts.values()))

    def has_source(self, source: str) -> bool:
        return any(event.source == source for event in self.pending_events())

    def acknowledge_non_wakeable(self) -> None:
        self._message_event.clear()

    def _initialize_pending_store(self) -> None:
        path = self._pending_path
        if path is None:
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.exists():
            descriptor = os.open(path, os.O_CREAT | os.O_WRONLY, 0o600)
            os.close(descriptor)
        connection = sqlite3.connect(path)
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=FULL")
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS pending_events (
                sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                event_id TEXT NOT NULL UNIQUE,
                payload TEXT NOT NULL
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS invalid_pending_events (
                sequence INTEGER,
                event_id TEXT,
                payload TEXT NOT NULL,
                error TEXT NOT NULL,
                quarantined_at TEXT NOT NULL
            )
            """
        )
        connection.commit()
        self._pending_db = connection
        self._secure_store_files()

    def _load_pending(self) -> None:
        connection = self._pending_db
        if connection is None:
            return
        invalid: list[tuple[int, str, str, str]] = []
        try:
            rows = connection.execute(
                "SELECT sequence, event_id, payload FROM pending_events ORDER BY sequence"
            ).fetchall()
            for sequence, event_id, payload in rows:
                try:
                    item = json.loads(payload)
                    if not isinstance(item, dict):
                        raise ValueError("pending event payload must be a JSON object")
                    event = IncomingEvent.from_dict(item)
                    event.event_id = str(event_id)
                    self._pending.append(event)
                    self._pending_ids.add(str(event_id))
                    if event.wake_level is not None:
                        self._wake_counts[event.wake_level] += 1
                except Exception as error:
                    invalid.append((int(sequence), str(event_id), str(payload), str(error)))
            if invalid:
                with connection:
                    connection.executemany(
                        """
                        INSERT INTO invalid_pending_events
                            (sequence, event_id, payload, error, quarantined_at)
                        VALUES (?, ?, ?, ?, ?)
                        """,
                        [
                            (*item, datetime.now().isoformat())
                            for item in invalid
                        ],
                    )
                    connection.executemany(
                        "DELETE FROM pending_events WHERE sequence = ?",
                        [(item[0],) for item in invalid],
                    )
                logger.warning(
                    tr(
                        "pending_events.quarantined",
                        count=len(invalid),
                        path=self._pending_path,
                    )
                )
            if self._pending:
                self._message_event.set()
        except Exception as error:
            message = tr(
                "pending_events.load_failed",
                path=self._pending_path,
                error=error,
            )
            logger.error(message)
            raise RuntimeError(message) from error

    def _persist_event(self, event: IncomingEvent) -> bool:
        connection = self._pending_db
        if connection is None:
            return True
        payload = json.dumps(
            event.to_dict(include_attachment_data=False),
            ensure_ascii=False,
            separators=(",", ":"),
        )
        with connection:
            cursor = connection.execute(
                "INSERT OR IGNORE INTO pending_events (event_id, payload) VALUES (?, ?)",
                (event.event_id, payload),
            )
        self._secure_store_files()
        return cursor.rowcount > 0

    def _delete_events(self, event_ids: list[str]) -> None:
        if not event_ids:
            return
        targets = set(event_ids)
        connection = self._pending_db
        if connection is not None:
            with connection:
                connection.executemany(
                    "DELETE FROM pending_events WHERE event_id = ?",
                    [(event_id,) for event_id in targets],
                )
            self._secure_store_files()
        for event in self._pending:
            if event.event_id in targets and event.wake_level is not None:
                self._wake_counts[event.wake_level] -= 1
                if self._wake_counts[event.wake_level] <= 0:
                    del self._wake_counts[event.wake_level]
        self._pending = [
            event for event in self._pending if event.event_id not in targets
        ]
        self._pending_ids.difference_update(targets)

    def _secure_store_files(self) -> None:
        path = self._pending_path
        if path is None or os.name == "nt":
            return
        for candidate in (path, Path(f"{path}-wal"), Path(f"{path}-shm")):
            if candidate.exists():
                candidate.chmod(0o600)

    @staticmethod
    def _file_event_id(path: Path) -> str:
        stat = path.stat()
        identity = f"{path.name}\0{stat.st_size}\0{stat.st_mtime_ns}".encode()
        return f"file:{hashlib.sha256(identity).hexdigest()}"

    async def start(self) -> None:
        self._inbox.mkdir(parents=True, exist_ok=True)
        self._processed.mkdir(parents=True, exist_ok=True)
        self._attachments.mkdir(parents=True, exist_ok=True, mode=0o700)
        if os.name != "nt":
            self._attachments.chmod(0o700)
        self._running = True
        self._stop_event.clear()
        logger.info(f"InboxWatcher started, polling {self._inbox}")
        while self._running:
            await self._poll()
            # 可被 stop() 立即打断的轮询间隔：关闭时无需空等满一个 poll_interval。
            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=self._poll_interval)
            except TimeoutError:
                pass
        logger.info("InboxWatcher stopped")

    def stop(self) -> None:
        self._running = False
        self._stop_event.set()

    async def _poll(self) -> None:
        for path in sorted(self._inbox.iterdir()):
            if not path.is_file() or path == self._inbox / ".gitkeep":
                continue
            suffix = path.suffix.lower()
            try:
                if suffix == ".md":
                    await self._process_md(path)
                elif suffix in _IMAGE_EXTS or suffix in _PDF_EXTS:
                    await self._process_binary_attachment(path)
                else:
                    await self._process_other_attachment(path)
            except Exception as e:
                logger.error(f"Failed to process inbox file {path}: {e}")

    async def _process_md(self, path: Path) -> None:
        sender_id = self._extract_sender(path.stem)
        content = path.read_text(encoding="utf-8").strip()
        if not content:
            path.unlink(missing_ok=True)
            return
        event = IncomingEvent(
            participant_id=sender_id,
            content=content,
            timestamp=datetime.now(),
            source="file",
            event_id=self._file_event_id(path),
        )
        await self.push(event)
        dest = self._processed / path.name
        path.rename(dest)
        logger.debug(f"Inbox: received message from {sender_id}")

    async def _process_binary_attachment(self, path: Path) -> None:
        sender_id = self._extract_sender(path.stem)
        suffix = path.suffix.lower()
        media_type = _guess_media_type(suffix)
        event_id = self._file_event_id(path)

        dest = self._attachments / f"{new_compact_id()}_{path.name}"
        _copy_private_file(path, dest)

        att = AttachmentData(
            filename=path.name,
            media_type=media_type,
            saved_path=str(dest),
            data=None,
        )
        event = IncomingEvent(
            participant_id=sender_id,
            content="",
            timestamp=datetime.now(),
            source="file",
            attachments=[att],
            event_id=event_id,
        )
        await self.push(event)
        path.unlink(missing_ok=True)
        logger.debug(f"Inbox: received attachment {path.name} from {sender_id}")

    async def _process_other_attachment(self, path: Path) -> None:
        sender_id = self._extract_sender(path.stem)
        suffix = path.suffix.lower()
        media_type = _guess_media_type(suffix)
        event_id = self._file_event_id(path)

        dest = self._attachments / f"{new_compact_id()}_{path.name}"
        _copy_private_file(path, dest)

        att = AttachmentData(
            filename=path.name,
            media_type=media_type,
            saved_path=str(dest),
            data=None,
        )
        event = IncomingEvent(
            participant_id=sender_id,
            content="",
            timestamp=datetime.now(),
            source="file",
            attachments=[att],
            event_id=event_id,
        )
        await self.push(event)
        path.unlink(missing_ok=True)
        logger.debug(f"Inbox: received file attachment {path.name} from {sender_id}")

    @staticmethod
    def _extract_sender(stem: str) -> str:
        parts = stem.split("_", 2)
        return parts[2] if len(parts) >= 3 else "unknown"
