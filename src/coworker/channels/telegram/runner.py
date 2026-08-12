from __future__ import annotations

import asyncio
import base64
from collections.abc import Callable
from pathlib import Path
from typing import Any

from loguru import logger

from coworker.channels.access import (
    ChannelAccessController,
    inbound_access_denied_message,
)
from coworker.channels.activity import ChannelActivityStore
from coworker.channels.base import InboundHandler
from coworker.channels.telegram import adapter
from coworker.channels.telegram.client import (
    TelegramClient,
    TelegramFileTooLargeError,
)
from coworker.channels.telegram.state import (
    TelegramContact,
    TelegramStateStore,
)
from coworker.core.config import TelegramBotConfig, TelegramConfig
from coworker.core.ids import new_compact_id
from coworker.core.types import AttachmentData, IncomingEvent
from coworker.i18n import tr

_RETRY_SECONDS = 5.0
_INLINE_BASE64_LIMIT = 10 * 1024 * 1024
_TELEGRAM_TEXT_LIMIT = 4096

TelegramClientFactory = Callable[[TelegramBotConfig], TelegramClient]


class _TelegramBotRuntime:
    """One independently configured Bot token and long-poll offset."""

    def __init__(
        self,
        instance_id: str,
        config: TelegramBotConfig,
        state_path: Path,
        attachments_dir: Path,
        activity: ChannelActivityStore,
        client_factory: TelegramClientFactory,
    ) -> None:
        self.instance_id = instance_id
        self._config = config.model_copy(deep=True)
        self._state_store = TelegramStateStore(state_path)
        self._state = self._state_store.load()
        self._attachments_dir = attachments_dir
        self._activity = activity
        self._client_factory = client_factory
        self._client: TelegramClient | None = None
        self._inbound_handler: InboundHandler | None = None
        self._access = ChannelAccessController()
        self._stop = asyncio.Event()
        self._wake = asyncio.Event()
        self._reconfigure_lock = asyncio.Lock()
        self._ready = False
        self._polling_failed = False

    @property
    def ready(self) -> bool:
        return self._ready

    def set_inbound_handler(self, handler: InboundHandler | None) -> None:
        self._inbound_handler = handler

    def set_access_controller(self, access: ChannelAccessController) -> None:
        self._access = access

    async def start(self) -> None:
        while not self._stop.is_set():
            self._wake.clear()
            if not self._is_configured():
                await self._wake.wait()
                continue
            client = self._client_factory(self._config.model_copy(deep=True))
            self._client = client
            try:
                await self._run_client(client)
            finally:
                self._ready = False
                if self._client is client:
                    self._client = None
                await client.close()

    async def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        client = self._client
        if client is not None:
            await client.close()

    async def reconfigure(self, config: TelegramBotConfig) -> None:
        async with self._reconfigure_lock:
            if self._config == config:
                return
            self._config = config.model_copy(deep=True)
            self._wake.set()
            client = self._client
            if client is not None:
                await client.close()

    def contacts(self) -> list[TelegramContact]:
        return list((self._state.contacts or {}).values())

    def contact_for_chat(self, chat_id: int) -> TelegramContact | None:
        return (self._state.contacts or {}).get(chat_id)

    async def send(
        self,
        participant_id: str,
        message: str,
        attachments: list[dict[str, Any]],
        conversation_id: str | None,
    ) -> None:
        _, chat_id = adapter.parse_participant(participant_id)
        if self.contact_for_chat(chat_id) is None:
            raise ValueError(
                tr(
                    "channel.telegram.participant_unknown",
                    participant=participant_id,
                )
            )
        client = self._client
        if client is None or not self._ready:
            raise RuntimeError(
                tr("channel.telegram.bot_unavailable", instance=self.instance_id)
            )
        thread_id = _thread_id(conversation_id)
        for chunk in split_telegram_text(message):
            await client.send_message(chat_id, chunk, thread_id)
        for attachment in attachments:
            await client.send_attachment(chat_id, attachment, thread_id)
        self._activity.record_sent(participant_id)

    async def _run_client(self, client: TelegramClient) -> None:
        try:
            bot = await client.get_me()
            bot_user_id = bot.get("id")
            if not isinstance(bot_user_id, int):
                raise ValueError(tr("channel.telegram.bot_identity_invalid"))
            if self._state.reset_for_bot(bot_user_id):
                self._state_store.save(self._state)
            self._ready = True
            if self._polling_failed:
                logger.info(
                    tr("channel.telegram.poll_recovered", instance=self.instance_id)
                )
                self._polling_failed = False
        except Exception as error:
            self._log_poll_failure(error)
            await self._wait_for_wake(_RETRY_SECONDS)
            return

        while not self._stop.is_set() and not self._wake.is_set():
            try:
                updates = await client.get_updates(
                    self._state.offset,
                    self._config.poll_timeout_seconds,
                )
                for update in updates:
                    await self._consume_update(client, update)
                if self._polling_failed:
                    logger.info(
                        tr("channel.telegram.poll_recovered", instance=self.instance_id)
                    )
                    self._polling_failed = False
            except asyncio.CancelledError:
                raise
            except Exception as error:
                if self._wake.is_set() or self._stop.is_set():
                    return
                self._ready = False
                self._log_poll_failure(error)
                if await self._wait_for_wake(_RETRY_SECONDS):
                    return
                self._ready = True

    async def _consume_update(
        self,
        client: TelegramClient,
        update: dict[str, Any],
    ) -> None:
        update_id = update.get("update_id")
        if not isinstance(update_id, int) or update_id < self._state.offset:
            return
        try:
            await self._publish_update(client, update, update_id)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            logger.warning(
                tr(
                    "channel.telegram.update_failed",
                    instance=self.instance_id,
                    update=update_id,
                    error=error,
                )
            )
        finally:
            self._state.offset = update_id + 1
            self._state_store.save(self._state)

    async def _publish_update(
        self,
        client: TelegramClient,
        update: dict[str, Any],
        update_id: int,
    ) -> None:
        message = adapter.message_for_update(update)
        if message is None:
            return
        contact = adapter.contact_for(message)
        participant_id = contact.participant_id(self.instance_id)
        thread_id = _thread_id(adapter.conversation_id_for(message))
        if not self._access.allows("telegram", "inbound", participant_id):
            await self._reject_inbound(client, participant_id, contact.chat_id, thread_id)
            return

        media = adapter.media_for(message)
        content = adapter.message_content(message, media)
        attachments: list[AttachmentData] = []
        if media is not None:
            try:
                attachments.append(await self._download_media(client, media))
            except TelegramFileTooLargeError:
                content = f"{content}\n{tr('channel.telegram.attachment_skipped', filename=media.filename)}"
            except Exception as error:
                logger.warning(
                    tr(
                        "channel.telegram.download_failed",
                        instance=self.instance_id,
                        filename=media.filename,
                        error=error,
                    )
                )
                content = f"{content}\n{tr('channel.telegram.attachment_unavailable', filename=media.filename)}"

        assert self._state.contacts is not None
        self._state.contacts[contact.chat_id] = contact
        self._activity.record_received(participant_id)
        if self._inbound_handler is None:
            logger.warning(
                tr("channel.telegram.inbound_unhandled", instance=self.instance_id)
            )
            return
        await self._inbound_handler(
            IncomingEvent(
                participant_id=participant_id,
                content=content,
                conversation_id=adapter.conversation_id_for(message),
                source="telegram",
                attachments=attachments,
                event_id=f"telegram:{self.instance_id}:{update_id}",
                metadata={"chat_type": contact.kind},
            )
        )

    async def _reject_inbound(
        self,
        client: TelegramClient,
        participant_id: str,
        chat_id: int,
        thread_id: int | None,
    ) -> None:
        logger.info(
            tr(
                "channel.access.inbound_denied",
                channel="telegram",
                participant=participant_id,
            )
        )
        self._access.traffic.record(
            direction="inbound",
            channel="telegram",
            participant_id=participant_id,
            status="denied",
            source="telegram",
            reason="policy",
        )
        try:
            await client.send_message(
                chat_id,
                inbound_access_denied_message(),
                thread_id,
            )
            self._access.traffic.record(
                direction="outbound",
                channel="telegram",
                participant_id=participant_id,
                status="sent",
                source="access_policy",
                reason="rejection_notice",
            )
        except Exception as error:
            self._access.traffic.record(
                direction="outbound",
                channel="telegram",
                participant_id=participant_id,
                status="failed",
                source="access_policy",
                reason="rejection_notice",
            )
            logger.warning(
                tr(
                    "channel.access.inbound_denied_reply_failed",
                    channel="telegram",
                    participant=participant_id,
                    error=error,
                )
            )

    async def _download_media(
        self,
        client: TelegramClient,
        media: adapter.TelegramMedia,
    ) -> AttachmentData:
        buffer = await client.download_file(media.file_id)
        filename = _safe_filename(media.filename)
        self._attachments_dir.mkdir(parents=True, exist_ok=True)
        destination = self._attachments_dir / f"{new_compact_id()}_{filename}"
        destination.write_bytes(buffer)
        inline = len(buffer) <= _INLINE_BASE64_LIMIT and (
            media.media_type.startswith("image/")
            or media.media_type == "application/pdf"
        )
        return AttachmentData(
            filename=filename,
            media_type=media.media_type,
            saved_path=str(destination),
            data=base64.b64encode(buffer).decode("ascii") if inline else None,
        )

    def _is_configured(self) -> bool:
        return bool(self._config.enabled and self._config.bot_token)

    def _log_poll_failure(self, error: Exception) -> None:
        if not self._polling_failed:
            logger.warning(
                tr(
                    "channel.telegram.poll_failed",
                    instance=self.instance_id,
                    error=error,
                )
            )
        self._polling_failed = True

    async def _wait_for_wake(self, seconds: float) -> bool:
        try:
            await asyncio.wait_for(self._wake.wait(), timeout=seconds)
            return True
        except TimeoutError:
            return False


class TelegramRunner:
    """Aggregate multiple independently configured Telegram Bot runtimes."""

    name = "telegram"

    def __init__(
        self,
        config: TelegramConfig,
        state_dir: Path,
        attachments_dir: Path,
        activity: ChannelActivityStore,
        *,
        client_factory: TelegramClientFactory | None = None,
    ) -> None:
        self._config = config.model_copy(deep=True)
        self._state_dir = state_dir
        self._attachments_dir = attachments_dir
        self._activity = activity
        self._client_factory = client_factory or _default_client_factory
        self._inbound_handler: InboundHandler | None = None
        self._access = ChannelAccessController()
        self._bots: dict[str, _TelegramBotRuntime] = {
            instance_id: self._build_bot(instance_id, bot_config)
            for instance_id, bot_config in self._config.bots.items()
        }
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._stop = asyncio.Event()
        self._started = False
        self._lock = asyncio.Lock()

    def set_inbound_handler(self, handler: InboundHandler | None) -> None:
        self._inbound_handler = handler
        for bot in self._bots.values():
            bot.set_inbound_handler(handler)

    def set_access_controller(self, access: ChannelAccessController) -> None:
        self._access = access
        for bot in self._bots.values():
            bot.set_access_controller(access)

    async def start(self) -> None:
        async with self._lock:
            self._started = True
            for instance_id in self._bots:
                self._start_bot(instance_id)
        await self._stop.wait()

    async def stop(self) -> None:
        self._stop.set()
        async with self._lock:
            await asyncio.gather(
                *(bot.stop() for bot in self._bots.values()),
                return_exceptions=True,
            )
            tasks = list(self._tasks.values())
            self._tasks.clear()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def reconfigure(self, config: TelegramConfig) -> None:
        async with self._lock:
            next_ids = set(config.bots)
            removed = set(self._bots) - next_ids
            for instance_id in removed:
                bot = self._bots.pop(instance_id)
                await bot.stop()
                task = self._tasks.pop(instance_id, None)
                if task is not None:
                    await asyncio.gather(task, return_exceptions=True)
            for instance_id, bot_config in config.bots.items():
                existing = self._bots.get(instance_id)
                if existing is None:
                    new_bot = self._build_bot(instance_id, bot_config)
                    self._bots[instance_id] = new_bot
                    if self._started and not self._stop.is_set():
                        self._start_bot(instance_id)
                else:
                    await existing.reconfigure(bot_config)
            self._config = config.model_copy(deep=True)

    async def send(
        self,
        participant_id: str,
        message: str,
        attachments: list[dict[str, Any]],
        conversation_id: str | None,
    ) -> None:
        instance_id, _ = adapter.parse_participant(participant_id)
        bot = self._bots.get(instance_id)
        if bot is None:
            raise ValueError(
                tr("channel.telegram.instance_unknown", instance=instance_id)
            )
        await bot.send(participant_id, message, attachments, conversation_id)

    def resolve_participant(self, participant_id: str) -> str | None:
        instance_hint = ""
        chat_hint = participant_id
        if ":" in participant_id:
            instance_hint, chat_hint = participant_id.split(":", maxsplit=1)
        try:
            chat_id = int(chat_hint)
        except ValueError:
            return None
        matches = [
            contact.participant_id(instance_id)
            for instance_id, bot in self._bots.items()
            if (not instance_hint or instance_id == instance_hint)
            and (contact := bot.contact_for_chat(chat_id)) is not None
        ]
        return matches[0] if len(matches) == 1 else None

    def contacts(self) -> list[tuple[str, TelegramContact, bool]]:
        return [
            (instance_id, contact, bot.ready)
            for instance_id, bot in self._bots.items()
            for contact in bot.contacts()
        ]

    def activity_for(self, participant_id: str) -> tuple[str | None, str | None]:
        return self._activity.activity_for(participant_id)

    def _build_bot(
        self,
        instance_id: str,
        config: TelegramBotConfig,
    ) -> _TelegramBotRuntime:
        bot = _TelegramBotRuntime(
            instance_id,
            config,
            self._state_dir / f"{instance_id}.json",
            self._attachments_dir,
            self._activity,
            self._client_factory,
        )
        bot.set_inbound_handler(self._inbound_handler)
        bot.set_access_controller(self._access)
        return bot

    def _start_bot(self, instance_id: str) -> None:
        if instance_id in self._tasks:
            return
        self._tasks[instance_id] = asyncio.create_task(
            self._bots[instance_id].start(),
            name=f"telegram-bot:{instance_id}",
        )


def split_telegram_text(
    text: str,
    limit: int = _TELEGRAM_TEXT_LIMIT,
) -> list[str]:
    remaining = text
    chunks: list[str] = []
    while remaining:
        if len(remaining) <= limit:
            chunks.append(remaining)
            break
        boundary = remaining.rfind("\n", 0, limit + 1)
        if boundary <= 0:
            boundary = limit
        chunks.append(remaining[:boundary])
        remaining = remaining[boundary:]
        if remaining.startswith("\n"):
            remaining = remaining[1:]
    return chunks


def _default_client_factory(config: TelegramBotConfig) -> TelegramClient:
    return TelegramClient(
        config.bot_token,
        config.api_base_url,
        local_mode=config.local_mode,
    )


def _thread_id(value: str | None) -> int | None:
    if value is None:
        return None
    try:
        thread_id = int(value)
    except ValueError as error:
        raise ValueError(
            tr("channel.telegram.conversation_invalid", conversation=value)
        ) from error
    if thread_id <= 0:
        raise ValueError(
            tr("channel.telegram.conversation_invalid", conversation=value)
        )
    return thread_id


def _safe_filename(value: str) -> str:
    name = Path(value).name or "telegram-attachment"
    if len(name) <= 180:
        return name
    suffix = Path(name).suffix[:20]
    stem_limit = 180 - len(suffix)
    return f"{Path(name).stem[:stem_limit]}{suffix}"
