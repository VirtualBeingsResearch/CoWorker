"""WeCom outbound sending: message chunking, media upload, and delivery.

Extracted from ``WeComRunner``. :class:`WeComSender` holds the media cache
and takes a client getter + a fresh-frame lookup (both owned by the runner)
so it can reply via a cached inbound frame or fall back to an active push.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from coworker.channels.wecom import adapter

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


class WeComSender:
    """Outbound WeCom delivery (text chunks + media)."""

    def __init__(
        self,
        client_getter: Callable[[], Any],
        take_frame: Callable[[str], dict[str, Any] | None],
    ) -> None:
        self._get_client = client_getter
        self._take_frame = take_frame
        self._media_cache: dict[tuple[str, float], str] = {}

    async def send(
        self,
        participant_id: str,
        message: str,
        attachments: list[dict[str, Any]],
    ) -> None:
        client = self._get_client()
        if client is None:
            raise RuntimeError("WeCom client not started")
        _chat_type, chat_id = adapter.parse_participant(participant_id)
        frame = self._take_frame(chat_id)

        if message:
            from wecom_aibot_sdk import generate_req_id

            for chunk in split_markdown(message):
                if frame is not None:
                    await client.reply_stream(
                        frame, generate_req_id("stream"), chunk, finish=True
                    )
                    frame = None
                else:
                    await client.send_message(
                        chat_id,
                        {"msgtype": "markdown", "markdown": {"content": chunk}},
                    )

        for att in attachments:
            self._validate_attachment(att)
            media_id = await self._ensure_media(att)
            media_type = att["type"]
            if frame is not None:
                await client.reply_media(frame, media_type, media_id)
                frame = None
            else:
                await client.send_media_message(chat_id, media_type, media_id)

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

    async def _ensure_media(self, att: dict[str, Any]) -> str:
        client = self._get_client()
        path = Path(att["path"])
        key = (str(path.resolve()), path.stat().st_mtime)
        if key in self._media_cache:
            return self._media_cache[key]
        result = await client.upload_media(
            path.read_bytes(),
            type=att["type"],
            filename=path.name,
        )
        media_id = result.media_id if hasattr(result, "media_id") else result["media_id"]
        self._media_cache[key] = media_id
        return media_id
