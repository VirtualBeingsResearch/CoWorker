from __future__ import annotations

import asyncio
from datetime import timedelta
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlsplit, urlunsplit

import httpx
from telegram import Bot

from coworker.i18n import tr

DEFAULT_API_BASE_URL = "https://api.telegram.org"
DEFAULT_POLL_TIMEOUT_SECONDS = 30.0
MAX_DOWNLOAD_BYTES = 20 * 1024 * 1024
MAX_UPLOAD_BYTES = 50 * 1024 * 1024
_DOWNLOAD_CHUNK_BYTES = 1024 * 1024


class TelegramFileTooLargeError(ValueError):
    """An inbound or outbound file exceeded the channel's safety limit."""


class TelegramClient:
    """Coworker-facing adapter over :class:`python-telegram-bot`'s async Bot."""

    def __init__(
        self,
        bot_token: str,
        api_base_url: str = DEFAULT_API_BASE_URL,
        *,
        local_mode: bool = False,
        bot: Bot | None = None,
        max_download_bytes: int = MAX_DOWNLOAD_BYTES,
        max_upload_bytes: int = MAX_UPLOAD_BYTES,
    ) -> None:
        root = api_base_url.rstrip("/")
        self._bot = bot or Bot(
            token=bot_token.strip(),
            base_url=f"{root}/bot",
            base_file_url=f"{root}/file/bot",
            local_mode=local_mode,
        )
        self._local_mode = local_mode
        self._max_download_bytes = max_download_bytes
        self._max_upload_bytes = max_upload_bytes
        self._initialized = False
        self._file_http: httpx.AsyncClient | None = None

    async def close(self) -> None:
        if self._file_http is not None:
            await self._file_http.aclose()
            self._file_http = None
        if not self._initialized:
            return
        self._initialized = False
        await self._bot.shutdown()

    async def get_me(self) -> dict[str, Any]:
        if not self._initialized:
            await self._bot.initialize()
            self._initialized = True
        return self._bot.bot.to_dict()

    async def get_updates(
        self,
        offset: int,
        timeout_seconds: float,
    ) -> list[dict[str, Any]]:
        updates = await self._bot.get_updates(
            offset=offset,
            timeout=timedelta(seconds=timeout_seconds),
            allowed_updates=("message", "channel_post"),
            read_timeout=timeout_seconds + 10.0,
        )
        return [update.to_dict() for update in updates]

    async def send_message(
        self,
        chat_id: int,
        text: str,
        message_thread_id: int | None = None,
    ) -> int:
        result = await self._bot.send_message(
            chat_id=chat_id,
            text=text,
            message_thread_id=message_thread_id,
        )
        message_id = getattr(result, "message_id", None)
        if not isinstance(message_id, int):
            raise RuntimeError("Telegram send_message returned no message_id")
        return message_id

    async def edit_message_text(
        self,
        chat_id: int,
        message_id: int,
        text: str,
    ) -> None:
        await self._bot.edit_message_text(
            text=text,
            chat_id=chat_id,
            message_id=message_id,
        )

    async def delete_message(self, chat_id: int, message_id: int) -> None:
        await self._bot.delete_message(chat_id=chat_id, message_id=message_id)

    async def send_attachment(
        self,
        chat_id: int,
        attachment: dict[str, Any],
        message_thread_id: int | None = None,
    ) -> None:
        attachment_type = str(attachment.get("type") or "file")
        if attachment_type not in {"image", "file"}:
            raise ValueError(
                tr("channel.telegram.attachment_type_invalid", type=attachment_type)
            )
        path = Path(str(attachment.get("path") or ""))
        if not path.is_file():
            raise FileNotFoundError(
                tr("channel.telegram.attachment_missing", path=path)
            )
        size = path.stat().st_size
        if size > self._max_upload_bytes:
            raise TelegramFileTooLargeError(
                tr(
                    "channel.telegram.attachment_too_large",
                    filename=path.name,
                    size=size,
                    limit=self._max_upload_bytes,
                )
            )
        filename = Path(str(attachment.get("filename") or path.name)).name

        async def send(source: Any) -> None:
            if attachment_type == "image":
                await self._bot.send_photo(
                    chat_id=chat_id,
                    photo=source,
                    filename=filename,
                    message_thread_id=message_thread_id,
                    read_timeout=60.0,
                    write_timeout=60.0,
                )
            else:
                await self._bot.send_document(
                    chat_id=chat_id,
                    document=source,
                    filename=filename,
                    message_thread_id=message_thread_id,
                    read_timeout=60.0,
                    write_timeout=60.0,
                )

        if self._local_mode:
            await send(path)
        else:
            with path.open("rb") as source:
                await send(source)

    async def download_file(
        self,
        file_id: str,
        destination: Path,
        *,
        max_bytes: int | None = None,
    ) -> int:
        """Stream the file to ``destination`` and return its size in bytes.

        On any failure the partial file at ``destination`` is removed before
        the error propagates.
        """

        limit = self._max_download_bytes if max_bytes is None else max_bytes
        destination.parent.mkdir(parents=True, exist_ok=True)
        total = -1
        # 调用方已经把 destination 预留成一个空文件，所以取文件信息与各项校验
        # 都必须留在 try 内，否则早失败会把那个空文件留在附件目录里。
        try:
            telegram_file = await self._bot.get_file(file_id)
            if (
                isinstance(telegram_file.file_size, int)
                and telegram_file.file_size > limit
            ):
                raise TelegramFileTooLargeError(
                    tr(
                        "channel.telegram.download_too_large",
                        size=telegram_file.file_size,
                        limit=limit,
                    )
                )
            file_path = str(telegram_file.file_path or "")
            if not file_path:
                raise RuntimeError(tr("channel.telegram.file_path_missing"))
            if _is_local_file(file_path):
                total = await asyncio.to_thread(
                    _copy_capped, Path(file_path), destination, limit
                )
            else:
                total = await self._download_to(
                    _encoded_file_url(file_path),
                    destination,
                    limit,
                )
        finally:
            if total < 0:
                destination.unlink(missing_ok=True)
        return total

    async def _download_to(self, url: str, destination: Path, limit: int) -> int:
        total = 0
        async with self._file_http_client().stream("GET", url) as response:
            if response.status_code != 200:
                raise RuntimeError(
                    tr(
                        "channel.telegram.download_http_failed",
                        status=response.status_code,
                    )
                )
            with destination.open("wb") as handle:
                async for chunk in response.aiter_bytes(_DOWNLOAD_CHUNK_BYTES):
                    total += len(chunk)
                    if total > limit:
                        raise TelegramFileTooLargeError(
                            tr(
                                "channel.telegram.download_too_large",
                                size=total,
                                limit=limit,
                            )
                        )
                    handle.write(chunk)
        return total

    def _file_http_client(self) -> httpx.AsyncClient:
        if self._file_http is None:
            self._file_http = httpx.AsyncClient(
                timeout=httpx.Timeout(connect=10.0, read=60.0, write=60.0, pool=60.0),
                follow_redirects=True,
            )
        return self._file_http


def _is_local_file(path: str) -> bool:
    """Mirror python-telegram-bot's local Bot API Server path detection."""

    try:
        return Path(path).is_file()
    except (OSError, ValueError):
        return False


def _copy_capped(source: Path, destination: Path, limit: int) -> int:
    total = 0
    with source.open("rb") as src, destination.open("wb") as dst:
        while chunk := src.read(_DOWNLOAD_CHUNK_BYTES):
            total += len(chunk)
            if total > limit:
                raise TelegramFileTooLargeError(
                    tr("channel.telegram.download_too_large", size=total, limit=limit)
                )
            dst.write(chunk)
    return total


def _encoded_file_url(file_path: str) -> str:
    parts = urlsplit(file_path)
    return urlunsplit(
        (parts.scheme, parts.netloc, quote(parts.path), parts.query, parts.fragment)
    )
