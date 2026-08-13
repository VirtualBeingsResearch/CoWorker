from __future__ import annotations

from datetime import timedelta
from pathlib import Path
from typing import Any

from telegram import Bot

from coworker.i18n import tr

DEFAULT_API_BASE_URL = "https://api.telegram.org"
DEFAULT_POLL_TIMEOUT_SECONDS = 30.0
MAX_DOWNLOAD_BYTES = 20 * 1024 * 1024
MAX_UPLOAD_BYTES = 50 * 1024 * 1024


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
    ) -> None:
        root = api_base_url.rstrip("/")
        self._bot = bot or Bot(
            token=bot_token.strip(),
            base_url=f"{root}/bot",
            base_file_url=f"{root}/file/bot",
            local_mode=local_mode,
        )
        self._local_mode = local_mode
        self._initialized = False

    async def close(self) -> None:
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
    ) -> None:
        await self._bot.send_message(
            chat_id=chat_id,
            text=text,
            message_thread_id=message_thread_id,
        )

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
        if size > MAX_UPLOAD_BYTES:
            raise TelegramFileTooLargeError(
                tr(
                    "channel.telegram.attachment_too_large",
                    filename=path.name,
                    size=size,
                    limit=MAX_UPLOAD_BYTES,
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
        *,
        max_bytes: int = MAX_DOWNLOAD_BYTES,
    ) -> bytes:
        telegram_file = await self._bot.get_file(file_id)
        if (
            isinstance(telegram_file.file_size, int)
            and telegram_file.file_size > max_bytes
        ):
            raise TelegramFileTooLargeError(
                tr(
                    "channel.telegram.download_too_large",
                    size=telegram_file.file_size,
                    limit=max_bytes,
                )
            )
        buffer = await telegram_file.download_as_bytearray(
            read_timeout=60.0,
            write_timeout=60.0,
        )
        if len(buffer) > max_bytes:
            raise TelegramFileTooLargeError(
                tr(
                    "channel.telegram.download_too_large",
                    size=len(buffer),
                    limit=max_bytes,
                )
            )
        return bytes(buffer)
