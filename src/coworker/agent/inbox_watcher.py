from __future__ import annotations

import asyncio
import base64
import secrets
import shutil
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

from loguru import logger

from coworker.core.ids import new_compact_id
from coworker.core.types import AttachmentData, IncomingEvent

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


class InboxWatcher:
    def __init__(self, inbox_dir: str, poll_interval: float = 2.0) -> None:
        self._inbox = Path(inbox_dir)
        self._processed = self._inbox / "processed"
        self._attachments = self._inbox.parent / "attachments"
        self._poll_interval = poll_interval
        self._queue: asyncio.Queue[IncomingEvent] = asyncio.Queue()
        self._running = False
        self._message_event = asyncio.Event()
        self._stop_event = asyncio.Event()
        self._interceptors: list[Callable[[IncomingEvent], bool]] = []

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
        event_id = secrets.token_hex(8)
        event.event_id = event_id
        for interceptor in self._interceptors:
            if interceptor(event):
                return event_id
        await self._queue.put(event)
        self._message_event.set()
        return event_id

    def cancel(self, event_id: str) -> None:
        """直接从队列中移除对应事件；事件不在队列中则静默忽略。"""
        remaining: list[IncomingEvent] = []
        while not self._queue.empty():
            try:
                e = self._queue.get_nowait()
                if e.event_id != event_id:
                    remaining.append(e)
            except asyncio.QueueEmpty:
                break
        for e in remaining:
            self._queue.put_nowait(e)
        if self._queue.empty():
            self._message_event.clear()

    async def get_pending(self) -> list[IncomingEvent]:
        events: list[IncomingEvent] = []
        while not self._queue.empty():
            try:
                events.append(self._queue.get_nowait())
            except asyncio.QueueEmpty:
                break
        if self._queue.empty():
            self._message_event.clear()
        return events

    async def start(self) -> None:
        self._inbox.mkdir(parents=True, exist_ok=True)
        self._processed.mkdir(parents=True, exist_ok=True)
        self._attachments.mkdir(parents=True, exist_ok=True)
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
        )
        await self.push(event)
        dest = self._processed / path.name
        path.rename(dest)
        logger.debug(f"Inbox: received message from {sender_id}")

    async def _process_binary_attachment(self, path: Path) -> None:
        sender_id = self._extract_sender(path.stem)
        suffix = path.suffix.lower()
        media_type = _guess_media_type(suffix)
        raw = path.read_bytes()
        data_b64 = base64.b64encode(raw).decode("ascii")

        dest = self._attachments / f"{new_compact_id()}_{path.name}"
        shutil.copy2(path, dest)
        path.unlink(missing_ok=True)

        att = AttachmentData(
            filename=path.name,
            media_type=media_type,
            saved_path=str(dest),
            data=data_b64,
        )
        event = IncomingEvent(
            participant_id=sender_id,
            content="",
            timestamp=datetime.now(),
            source="file",
            attachments=[att],
        )
        await self._queue.put(event)
        self._message_event.set()
        logger.debug(f"Inbox: received attachment {path.name} from {sender_id}")

    async def _process_other_attachment(self, path: Path) -> None:
        sender_id = self._extract_sender(path.stem)
        suffix = path.suffix.lower()
        media_type = _guess_media_type(suffix)

        dest = self._attachments / f"{new_compact_id()}_{path.name}"
        shutil.copy2(path, dest)
        path.unlink(missing_ok=True)

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
        )
        await self._queue.put(event)
        self._message_event.set()
        logger.debug(f"Inbox: received file attachment {path.name} from {sender_id}")

    @staticmethod
    def _extract_sender(stem: str) -> str:
        parts = stem.split("_", 2)
        return parts[2] if len(parts) >= 3 else "unknown"
