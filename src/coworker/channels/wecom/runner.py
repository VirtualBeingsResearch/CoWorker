from __future__ import annotations

import asyncio
import time
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from loguru import logger

from coworker.channels.base import InboundHandler
from coworker.channels.wecom import adapter
from coworker.channels.wecom.contacts import ContactsStore, normalize_chat_type
from coworker.channels.wecom.sender import WeComSender
from coworker.core.types import IncomingEvent


class _LoguruLogger:
    """把 SDK 的 debug/info/warn/error 调用转发到项目的 loguru。

    SDK 期望的协议：四个方法，签名 (self, message: str, *args: Any) -> None。
    DefaultLogger 默认 print 到 stdout、UTC 时间、AiBotSDK 前缀，与项目日志格式不一致。
    """

    def __init__(self, prefix: str = "wecom") -> None:
        self._prefix = prefix

    def _fmt(self, message: str, args: tuple[Any, ...]) -> str:
        if args:
            extra = " ".join(str(a) for a in args)
            return f"[{self._prefix}] {message} {extra}".rstrip()
        return f"[{self._prefix}] {message}"

    def debug(self, message: str, *args: Any) -> None:
        logger.opt(depth=1).debug(self._fmt(message, args))

    def info(self, message: str, *args: Any) -> None:
        logger.opt(depth=1).info(self._fmt(message, args))

    def warn(self, message: str, *args: Any) -> None:
        logger.opt(depth=1).warning(self._fmt(message, args))

    def error(self, message: str, *args: Any) -> None:
        logger.opt(depth=1).error(self._fmt(message, args))


if TYPE_CHECKING:
    from coworker.core.config import WeComConfig

_FRAME_TTL = 600.0  # 10 minutes


class WeComRunner:
    """WeCom WebSocket lifecycle + inbound handlers.

    Outbound delivery is delegated to :class:`WeComSender`; contact persistence
    to :class:`ContactsStore`. This class owns the WS client, the inbound frame
    cache, and the channel-facing resolver/sender adapters. Normalized
    inbound events are emitted through the handler installed by ``WeComChannel``.
    """

    name = "wecom"

    def __init__(
        self,
        cfg: WeComConfig,
        attachments_dir: Path,
        contacts_path: Path | None = None,
    ) -> None:
        self._cfg = cfg
        self._attachments_dir = attachments_dir
        self._contacts_path = contacts_path
        self._client: Any = None  # WSClient, lazy-imported
        self._frame_cache: dict[tuple[str, str], tuple[dict[str, Any], float]] = {}
        self._last_sent_at: dict[str, str] = {}
        self._last_received_at: dict[str, str] = {}
        # Persistent chat_id -> chat_type ("single"/"group") mapping.
        self._contacts: dict[str, str] = ContactsStore.load(self._contacts_path)
        self._sender = WeComSender(lambda: self._client, self._take_fresh_frame)
        self._stop = asyncio.Event()
        self._kicked = False
        self._inbound_handler: InboundHandler | None = None

    def set_inbound_handler(self, handler: InboundHandler | None) -> None:
        self._inbound_handler = handler

    async def start(self) -> None:
        from wecom_aibot_sdk import WSClient

        self._client = WSClient(
            bot_id=self._cfg.bot_id,
            secret=self._cfg.secret,
            ws_url=self._cfg.ws_url or "",
            logger=_LoguruLogger(),
        )
        self._register_handlers()
        try:
            await self._client.connect()
        except Exception as e:
            logger.error(f"WeCom connect failed: {e}")
            return

        # Periodically drop expired frame cache entries; exit when stop is signaled.
        try:
            while not self._stop.is_set() and not self._kicked:
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=60.0)
                except TimeoutError:
                    pass
                self._sweep_frames()
        finally:
            await self.stop()

    async def stop(self) -> None:
        self._stop.set()
        if self._client is not None:
            try:
                await self._client.disconnect()
            except Exception as e:
                logger.debug(f"WeCom disconnect error: {e}")

    # ── handler registration ─────────────────────────────────────────────

    def _register_handlers(self) -> None:
        c = self._client
        c.on("authenticated", lambda: logger.info(f"WeCom authenticated bot={self._cfg.bot_id}"))
        c.on("disconnected", lambda reason=None: logger.warning(f"WeCom disconnected: {reason}"))
        c.on("event.disconnected_event", self._on_kicked)
        for evt in ("message.text", "message.voice"):
            c.on(evt, self._on_text_like)
        for evt in ("message.image", "message.file", "message.mixed", "message.video"):
            c.on(evt, self._on_with_attachments)
        c.on("message.stream", self._on_stream_notify)

    async def _on_text_like(self, frame: dict[str, Any]) -> None:
        try:
            event = adapter.frame_to_event(frame, attachments=[])
            self._cache_frame(event.participant_id, event.conversation_id, frame)
            await self._publish_inbound(event)
        except Exception as e:
            logger.error(f"WeCom text handler error: {e}")

    async def _on_with_attachments(self, frame: dict[str, Any]) -> None:
        try:
            atts = await adapter.collect_attachments(self._client, frame, self._attachments_dir)
            event = adapter.frame_to_event(frame, attachments=atts)
            self._cache_frame(event.participant_id, event.conversation_id, frame)
            await self._publish_inbound(event)
        except Exception as e:
            logger.error(f"WeCom attachment handler error: {e}")

    async def _on_stream_notify(self, frame: dict[str, Any]) -> None:
        stream_id = frame.get("body", {}).get("stream", {}).get("id", "?")
        logger.debug(f"WeCom stream notify id={stream_id}")

    async def _publish_inbound(self, event: IncomingEvent) -> None:
        if self._inbound_handler is None:
            logger.warning("Dropping WeCom inbound event: no channel handler is configured")
            return
        await self._inbound_handler(event)

    async def _on_kicked(self, frame: dict[str, Any]) -> None:
        logger.warning("WeCom kicked by a newer connection; will not auto-reconnect")
        self._kicked = True
        self._stop.set()

    # ── frame cache ──────────────────────────────────────────────────────

    def _cache_frame(
        self,
        participant_id: str,
        conversation_id: str | None,
        frame: dict[str, Any],
    ) -> None:
        chat_type, chat_id = adapter.parse_participant(participant_id)
        self._frame_cache[(chat_id, conversation_id or "")] = (
            frame,
            time.monotonic() + _FRAME_TTL,
        )
        self._last_received_at[chat_id] = datetime.now().astimezone().isoformat(timespec="seconds")
        if self._contacts.get(chat_id) != chat_type:
            self._contacts[chat_id] = chat_type
            ContactsStore.save(self._contacts_path, self._contacts)

    def _take_fresh_frame(
        self,
        chat_id: str,
        conversation_id: str | None,
    ) -> dict[str, Any] | None:
        if conversation_id:
            item = self._frame_cache.pop((chat_id, conversation_id), None)
        else:
            matching_keys = [key for key in self._frame_cache if key[0] == chat_id]
            latest_key = max(
                matching_keys,
                key=lambda key: self._frame_cache[key][1],
                default=None,
            )
            item = self._frame_cache.pop(latest_key, None) if latest_key else None
        if item is None:
            return None
        frame, expires = item
        if time.monotonic() >= expires:
            return None
        return frame

    def _sweep_frames(self) -> None:
        now = time.monotonic()
        expired = [
            key for key, (_, expires) in self._frame_cache.items() if expires <= now
        ]
        for key in expired:
            self._frame_cache.pop(key, None)

    # ── outbound ─────────────────────────────────────────────────────────

    async def send(
        self,
        participant_id: str,
        message: str,
        attachments: list[dict[str, Any]],
        conversation_id: str | None = None,
        mentioned_list: list[str] | None = None,
    ) -> None:
        await self._sender.send(
            participant_id,
            message,
            attachments,
            conversation_id,
            mentioned_list,
        )
        _, chat_id = adapter.parse_participant(participant_id)
        self._last_sent_at[chat_id] = datetime.now().astimezone().isoformat(timespec="seconds")

    def activity_for(self, participant_id: str) -> tuple[str | None, str | None]:
        """Return the latest successful outbound and inbound times for a chat."""
        _, chat_id = adapter.parse_participant(participant_id)
        return self._last_sent_at.get(chat_id), self._last_received_at.get(chat_id)

    def resolve_participant(self, participant_id: str) -> str | None:
        """若 participant_id 是已知的 WeCom chat_id，返回带前缀的规范化 ID；否则返回 None。"""
        chat_type = normalize_chat_type(self._contacts.get(participant_id))
        if chat_type is None:
            return None
        return f"wecom:{chat_type}:{participant_id}"
