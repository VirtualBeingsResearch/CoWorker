"""WeCom outbound message chunking, media upload, and delivery."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from loguru import logger

from coworker.channels.outbound_upload import UploadBatchStart, safe_upload_error
from coworker.channels.progress import attachment_display_name
from coworker.channels.wecom import adapter
from coworker.i18n import tr

_MEDIA_LIMITS = {
    "image": 10 * 1024 * 1024,
    "file": 20 * 1024 * 1024,
    "voice": 2 * 1024 * 1024,
    "video": 10 * 1024 * 1024,
}
_MARKDOWN_MAX_BYTES = 20480


def split_markdown(text: str, max_bytes: int = _MARKDOWN_MAX_BYTES) -> list[str]:
    if len(text.encode("utf-8")) <= max_bytes:
        return [text]
    chunks: list[str] = []
    buf = ""
    for para in text.split("\n\n"):
        candidate = f"{buf}\n\n{para}" if buf else para
        if len(candidate.encode("utf-8")) <= max_bytes:
            buf = candidate
            continue
        if buf:
            chunks.append(buf)
        if len(para.encode("utf-8")) > max_bytes:
            # Hard split a single oversize paragraph by characters until each piece fits.
            piece = ""
            for ch in para:
                if len((piece + ch).encode("utf-8")) > max_bytes:
                    chunks.append(piece)
                    piece = ch
                else:
                    piece += ch
            buf = piece
        else:
            buf = para
    if buf:
        chunks.append(buf)
    return chunks


@dataclass(frozen=True)
class PendingWeComMedia:
    """One attachment whose bytes are uploaded after communicate returns."""

    attachment: dict[str, Any]
    chat_id: str
    frame: dict[str, Any] | None
    filename: str


class WeComSender:
    """Outbound WeCom delivery (text chunks + media)."""

    def __init__(
        self,
        client_getter: Callable[[], Any],
        take_frame: Callable[[str, str | None], dict[str, Any] | None],
    ) -> None:
        self._get_client = client_getter
        self._take_frame = take_frame
        self._media_cache: dict[tuple[str, float], str] = {}

    async def send(
        self,
        participant_id: str,
        message: str,
        attachments: list[dict[str, Any]],
        conversation_id: str | None = None,
        *,
        reply_frame: dict[str, Any] | None = None,
        reply_stream_id: str | None = None,
    ) -> tuple[UploadBatchStart, tuple[PendingWeComMedia, ...]]:
        client = self._get_client()
        if client is None:
            raise RuntimeError("WeCom client not started")
        _instance, chat_type, chat_id = adapter.parse_participant(participant_id)
        frame: dict[str, Any] | None
        stream_id: str | None
        if reply_frame is not None:
            frame = reply_frame
            stream_id = reply_stream_id
        else:
            frame = (
                self._take_frame(chat_id, conversation_id)
                if chat_type == "single" or conversation_id is not None
                else None
            )
            stream_id = None

        text_sent = False
        if message:
            from wecom_aibot_sdk import generate_req_id

            for chunk in split_markdown(message):
                if frame is not None:
                    await client.reply_stream(
                        frame,
                        stream_id or generate_req_id("stream"),
                        chunk,
                        finish=True,
                    )
                    frame = None
                    stream_id = None
                    text_sent = True
                else:
                    await client.send_message(
                        chat_id,
                        {"msgtype": "markdown", "markdown": {"content": chunk}},
                    )
                    text_sent = True
        elif reply_frame is not None and frame is not None:
            from wecom_aibot_sdk import generate_req_id

            await client.reply_stream(
                frame,
                stream_id or generate_req_id("stream"),
                "",
                finish=True,
            )
            frame = None

        pending: list[PendingWeComMedia] = []
        rejected: list[tuple[str, str]] = []
        held_frame = frame
        for att in attachments:
            try:
                self._validate_attachment(att)
            except Exception as error:
                rejected.append((attachment_display_name(att), safe_upload_error(error)))
                continue
            path = Path(att["path"])
            pending.append(
                PendingWeComMedia(
                    attachment=att,
                    chat_id=chat_id,
                    frame=held_frame,
                    filename=path.name,
                )
            )
            held_frame = None
        return (
            UploadBatchStart(
                text_sent=text_sent,
                started=tuple(item.filename for item in pending),
                rejected=tuple(rejected),
            ),
            tuple(pending),
        )

    async def deliver_media(
        self,
        item: PendingWeComMedia,
        *,
        timeout: float,
        on_terminal: Callable[[str], Awaitable[None]],
    ) -> None:
        """Upload one prepared attachment. Only a failure is posted in the chat."""
        try:
            media_id = await self._ensure_media(item.attachment, timeout=timeout)
            media_type = item.attachment["type"]
            client = self._get_client()
            if client is None:
                raise RuntimeError("WeCom client not started")
            if item.frame is not None:
                await client.reply_media(item.frame, media_type, media_id)
            else:
                await client.send_media_message(item.chat_id, media_type, media_id)
        except Exception as error:
            notice = tr(
                "channel.upload.failed_notice",
                filename=item.filename,
                error=safe_upload_error(error),
            )
            try:
                await self._send_notice(item.chat_id, notice)
            except Exception as delivery_error:
                logger.warning(
                    tr(
                        "channel.upload.status_delivery_failed",
                        error_type=type(delivery_error).__name__,
                    )
                )
        else:
            notice = tr("channel.upload.succeeded_notice", filename=item.filename)
        await on_terminal(notice)

    async def _send_notice(self, chat_id: str, text: str) -> None:
        client = self._get_client()
        if client is None:
            raise RuntimeError("WeCom client not started")
        await client.send_message(
            chat_id,
            {"msgtype": "markdown", "markdown": {"content": text}},
        )

    def _validate_attachment(self, att: dict[str, Any]) -> None:
        media_type = att.get("type")
        if media_type not in _MEDIA_LIMITS:
            raise ValueError(f"unsupported attachment type: {media_type!r}")
        path = Path(att["path"])
        if not path.is_file():
            raise FileNotFoundError(f"attachment not found: {path}")
        size = path.stat().st_size
        if size < 5:
            raise ValueError(f"attachment too small (<5 bytes): {path}")
        limit = _MEDIA_LIMITS[media_type]
        if size > limit:
            raise ValueError(
                f"attachment {path.name} ({size} bytes) exceeds {media_type} limit {limit}"
            )

    async def _ensure_media(self, att: dict[str, Any], *, timeout: float = 60.0) -> str:
        client = self._get_client()
        path = Path(att["path"])
        key = (str(path.resolve()), path.stat().st_mtime)
        if key in self._media_cache:
            return self._media_cache[key]
        payload = await asyncio.to_thread(path.read_bytes)
        result = await asyncio.wait_for(
            client.upload_media(
                payload,
                type=att["type"],
                filename=path.name,
            ),
            timeout=timeout,
        )
        media_id = result.media_id if hasattr(result, "media_id") else result["media_id"]
        self._media_cache[key] = media_id
        return media_id
