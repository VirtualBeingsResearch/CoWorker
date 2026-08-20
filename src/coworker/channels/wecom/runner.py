from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

from loguru import logger

from coworker.channels.access import (
    ChannelAccessController,
    inbound_access_denied_message,
)
from coworker.channels.activity import ChannelActivityStore
from coworker.channels.base import ConnectionInfo, InboundHandler
from coworker.channels.wecom import adapter
from coworker.channels.wecom.contacts import ContactsStore, normalize_chat_type
from coworker.channels.wecom.sender import WeComSender
from coworker.core.types import IncomingEvent
from coworker.i18n import tr


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
    from coworker.core.config import WeComBotConfig, WeComConfig

_FRAME_TTL = 600.0  # 10 minutes
INSTANCE_DIR_PREFIX = "wecom_contacts_"
INSTANCE_DIR_SUFFIX = ".json"


class _WeComBotRuntime:
    """One independently configured WeCom bot's WebSocket lifecycle.

    Each bot owns its own WS client, inbound frame cache, and persistent
    contact mapping, so multiple bots can be connected simultaneously and
    routed by ``instance_id``.
    """

    def __init__(
        self,
        instance_id: str,
        cfg: WeComBotConfig,
        attachments_dir: Path,
        contacts_path: Path | None,
        activity: ChannelActivityStore,
        *,
        inbound_handler: InboundHandler | None = None,
        access: ChannelAccessController | None = None,
    ) -> None:
        self.instance_id = instance_id
        self._cfg = cfg.model_copy(deep=True)
        self._attachments_dir = attachments_dir
        self._contacts_path = contacts_path
        self._activity = activity
        self._client: Any = None  # WSClient, lazy-imported
        self._frame_cache: dict[tuple[str, str], tuple[dict[str, Any], float]] = {}
        # Persistent chat_id -> chat_type ("single"/"group") mapping.
        self._contacts: dict[str, str] = ContactsStore.load(contacts_path)
        self._sender = WeComSender(lambda: self._client, self._take_fresh_frame)
        self._stop = asyncio.Event()
        self._wake = asyncio.Event()
        self._connection_blocked = False
        self._inbound_handler = inbound_handler
        self._access = access or ChannelAccessController()

    def set_inbound_handler(self, handler: InboundHandler | None) -> None:
        self._inbound_handler = handler

    def set_access_controller(self, access: ChannelAccessController) -> None:
        self._access = access

    @property
    def configured(self) -> bool:
        return bool(self._cfg.enabled and self._cfg.bot_id and self._cfg.secret)

    async def start(self) -> None:
        while not self._stop.is_set():
            self._wake.clear()
            if self._stop.is_set():
                return
            if self._connection_blocked or not self.configured:
                await self._wake.wait()
                continue
            await self._run_connection()

    async def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        await self._disconnect(self._client)

    async def reconfigure(self, cfg: WeComBotConfig) -> None:
        """Apply a new configuration for this existing bot instance."""
        if self._cfg == cfg:
            return
        self._cfg = cfg.model_copy(deep=True)
        self._connection_blocked = False
        self._frame_cache.clear()
        client = self._client
        self._wake.set()
        await self._disconnect(client)

    async def _run_connection(self) -> None:
        from wecom_aibot_sdk import WSClient

        cfg = self._cfg.model_copy(deep=True)
        client = WSClient(
            bot_id=cfg.bot_id,
            secret=cfg.secret,
            ws_url=cfg.ws_url or "",
            logger=_LoguruLogger(),
        )
        self._client = client
        self._register_handlers(client, cfg.bot_id)
        try:
            await client.connect()
        except Exception as error:
            logger.error(f"WeCom connect failed: {error}")
            self._connection_blocked = True
            await self._disconnect(client)
            if self._client is client:
                self._client = None
            return

        try:
            while not self._stop.is_set() and not self._wake.is_set():
                try:
                    await asyncio.wait_for(self._wake.wait(), timeout=60.0)
                except TimeoutError:
                    self._sweep_frames()
        finally:
            await self._disconnect(client)
            if self._client is client:
                self._client = None

    async def _disconnect(self, client: Any) -> None:
        if client is None:
            return
        try:
            await client.disconnect()
        except Exception as error:
            logger.debug(f"WeCom disconnect error: {error}")

    # ── handler registration ─────────────────────────────────────────────

    def _register_handlers(self, client: Any, bot_id: str) -> None:
        client.on(
            "authenticated",
            lambda: logger.info(
                f"WeCom authenticated bot={bot_id} instance={self.instance_id}"
            ),
        )
        client.on(
            "disconnected",
            lambda reason=None: logger.warning(
                f"WeCom disconnected instance={self.instance_id}: {reason}"
            ),
        )
        client.on("event.disconnected_event", self._on_kicked)
        for evt in ("message.text", "message.voice"):
            client.on(evt, self._on_text_like)
        for evt in ("message.image", "message.file", "message.mixed", "message.video"):
            client.on(evt, self._on_with_attachments)
        client.on("message.stream", self._on_stream_notify)

    async def _on_text_like(self, frame: dict[str, Any]) -> None:
        try:
            participant_id = adapter.participant_id_for(frame, self.instance_id)
            if not await self._allows_inbound(participant_id, frame):
                return
            collected = await adapter.collect_attachments(
                self._client,
                frame,
                self._attachments_dir,
            )
            event = adapter.frame_to_event(
                frame,
                attachments=collected.attachments,
                attachment_failure_notices=collected.failure_notices,
                instance=self.instance_id,
            )
            self._cache_frame(event.participant_id, event.conversation_id, frame)
            await self._publish_inbound(event)
        except Exception as e:
            logger.error(f"WeCom text handler error (instance={self.instance_id}): {e}")

    async def _on_with_attachments(self, frame: dict[str, Any]) -> None:
        try:
            participant_id = adapter.participant_id_for(frame, self.instance_id)
            if not await self._allows_inbound(participant_id, frame):
                return
            collected = await adapter.collect_attachments(
                self._client,
                frame,
                self._attachments_dir,
            )
            event = adapter.frame_to_event(
                frame,
                attachments=collected.attachments,
                attachment_failure_notices=collected.failure_notices,
                instance=self.instance_id,
            )
            self._cache_frame(event.participant_id, event.conversation_id, frame)
            await self._publish_inbound(event)
        except Exception as e:
            logger.error(
                f"WeCom attachment handler error (instance={self.instance_id}): {e}"
            )

    async def _on_stream_notify(self, frame: dict[str, Any]) -> None:
        stream_id = frame.get("body", {}).get("stream", {}).get("id", "?")
        logger.debug(f"WeCom stream notify instance={self.instance_id} id={stream_id}")

    async def _publish_inbound(self, event: IncomingEvent) -> None:
        if self._inbound_handler is None:
            logger.warning("Dropping WeCom inbound event: no channel handler is configured")
            return
        await self._inbound_handler(event)

    async def _allows_inbound(
        self,
        participant_id: str,
        frame: dict[str, Any],
    ) -> bool:
        if self._access.allows("wecom", "inbound", participant_id):
            return True
        logger.info(
            tr(
                "channel.access.inbound_denied",
                channel="wecom",
                participant=participant_id,
            )
        )
        self._access.traffic.record(
            direction="inbound",
            channel="wecom",
            participant_id=participant_id,
            status="denied",
            source="wecom",
            reason="policy",
        )
        try:
            if self._client is None:
                raise RuntimeError(tr("channel.access.reply_transport_unavailable"))
            from wecom_aibot_sdk import generate_req_id

            await self._client.reply_stream(
                frame,
                generate_req_id("stream"),
                inbound_access_denied_message(),
                finish=True,
            )
            self._access.traffic.record(
                direction="outbound",
                channel="wecom",
                participant_id=participant_id,
                status="sent",
                source="access_policy",
                reason="rejection_notice",
            )
        except Exception as error:
            self._access.traffic.record(
                direction="outbound",
                channel="wecom",
                participant_id=participant_id,
                status="failed",
                source="access_policy",
                reason="rejection_notice",
            )
            logger.warning(
                tr(
                    "channel.access.inbound_denied_reply_failed",
                    channel="wecom",
                    participant=participant_id,
                    error=error,
                )
            )
        return False

    async def _on_kicked(self, frame: dict[str, Any]) -> None:
        logger.warning(
            f"WeCom bot {self.instance_id} kicked by a newer connection; will not auto-reconnect"
        )
        self._connection_blocked = True
        self._wake.set()

    # ── frame cache ──────────────────────────────────────────────────────

    def _cache_frame(
        self,
        participant_id: str,
        conversation_id: str | None,
        frame: dict[str, Any],
    ) -> None:
        _, chat_type, chat_id = adapter.parse_participant(participant_id)
        self._frame_cache[(chat_id, conversation_id or "")] = (
            frame,
            time.monotonic() + _FRAME_TTL,
        )
        self._activity.record_received(participant_id)
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
    ) -> None:
        await self._sender.send(
            participant_id,
            message,
            attachments,
            conversation_id,
        )
        self._activity.record_sent(participant_id)

    def activity_for(self, participant_id: str) -> tuple[str | None, str | None]:
        return self._activity.activity_for(participant_id)

    def resolve_participant(self, participant_id: str) -> str | None:
        """若 participant_id 是已知的 chat_id，返回带实例前缀的规范化 ID。"""
        chat_type = normalize_chat_type(self._contacts.get(participant_id))
        if chat_type is None:
            return None
        return f"wecom:{self.instance_id}:{chat_type}:{participant_id}"

    def connections(self) -> list[tuple[str, str, bool, str | None, str | None]]:
        """Return (participant_id, kind, active, last_sent_at, last_received_at)."""
        now = time.monotonic()
        out: list[tuple[str, str, bool, str | None, str | None]] = []
        for chat_id, chat_type in self._contacts.items():
            active = any(
                cached_chat_id == chat_id and now < expires
                for (cached_chat_id, _), (_, expires) in self._frame_cache.items()
            )
            participant_id = f"wecom:{self.instance_id}:{chat_type}:{chat_id}"
            last_sent_at, last_received_at = self._activity.activity_for(participant_id)
            out.append(
                (
                    participant_id,
                    f"wecom:{chat_type}",
                    active,
                    last_sent_at,
                    last_received_at,
                )
            )
        return out


class WeComRunner:
    """Aggregate multiple independently configured WeCom bot runtimes."""

    name = "wecom"

    def __init__(
        self,
        cfg: WeComConfig,
        attachments_dir: Path,
        contacts_path: Path | None = None,
        activity: ChannelActivityStore | None = None,
    ) -> None:
        self._cfg = cfg.model_copy(deep=True)
        self._attachments_root = attachments_dir
        self._contacts_path = contacts_path
        self._activity = activity or ChannelActivityStore()
        self._inbound_handler: InboundHandler | None = None
        self._access = ChannelAccessController()
        self._bots: dict[str, _WeComBotRuntime] = {
            instance_id: self._build_bot(instance_id, bot_cfg)
            for instance_id, bot_cfg in self._cfg.bots.items()
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

    async def reconfigure(self, cfg: WeComConfig) -> None:
        """Apply WeCom settings without replacing the registered channel runtime."""
        async with self._lock:
            if self._cfg == cfg:
                return
            next_ids = set(cfg.bots)
            removed = set(self._bots) - next_ids
            for instance_id in removed:
                bot = self._bots.pop(instance_id)
                await bot.stop()
                task = self._tasks.pop(instance_id, None)
                if task is not None:
                    await asyncio.gather(task, return_exceptions=True)
            for instance_id, bot_cfg in cfg.bots.items():
                existing = self._bots.get(instance_id)
                if existing is None:
                    new_bot = self._build_bot(instance_id, bot_cfg)
                    self._bots[instance_id] = new_bot
                    if self._started and not self._stop.is_set():
                        self._start_bot(instance_id)
                else:
                    await existing.reconfigure(bot_cfg)
            self._cfg = cfg.model_copy(deep=True)

    async def send(
        self,
        participant_id: str,
        message: str,
        attachments: list[dict[str, Any]],
        conversation_id: str | None = None,
    ) -> None:
        instance_id, _, _ = adapter.parse_participant(participant_id)
        bot = self._bots.get(instance_id)
        if bot is None:
            raise ValueError(
                tr("channel.wecom.instance_unknown", instance=instance_id)
            )
        await bot.send(participant_id, message, attachments, conversation_id)

    def resolve_participant(self, participant_id: str) -> str | None:
        instance_hint = ""
        chat_hint = participant_id
        if ":" in participant_id:
            potential_inst, rest = participant_id.split(":", maxsplit=1)
            if potential_inst in self._bots:
                instance_hint = potential_inst
                chat_hint = rest
        matches: list[str] = []
        for instance_id, bot in self._bots.items():
            if instance_hint and instance_id != instance_hint:
                continue
            canonical = bot.resolve_participant(chat_hint)
            if canonical is not None:
                matches.append(canonical)
        return matches[0] if len(matches) == 1 else None

    def activity_for(self, participant_id: str) -> tuple[str | None, str | None]:
        """Return the latest successful outbound and inbound times for a chat."""
        return self._activity.activity_for(participant_id)

    def list_connections(self) -> list[ConnectionInfo]:
        out: list[ConnectionInfo] = []
        for bot in self._bots.values():
            for (
                participant_id,
                kind,
                active,
                last_sent_at,
                last_received_at,
            ) in bot.connections():
                out.append(
                    ConnectionInfo(
                        participant_id=participant_id,
                        channel="wecom",
                        kind=kind,
                        active=active,
                        last_sent_at=last_sent_at,
                        last_received_at=last_received_at,
                    )
                )
        return out

    def _build_bot(
        self,
        instance_id: str,
        cfg: WeComBotConfig,
    ) -> _WeComBotRuntime:
        bot = _WeComBotRuntime(
            instance_id,
            cfg,
            attachments_dir=self._attachments_root / instance_id,
            contacts_path=_instance_contacts_path(self._contacts_path, instance_id),
            activity=self._activity,
            inbound_handler=self._inbound_handler,
            access=self._access,
        )
        return bot

    def _start_bot(self, instance_id: str) -> None:
        if instance_id in self._tasks:
            return
        self._tasks[instance_id] = asyncio.create_task(
            self._bots[instance_id].start(),
            name=f"wecom-bot:{instance_id}",
        )


def _instance_contacts_path(base: Path | None, instance_id: str) -> Path | None:
    """Return the per-instance contacts file path derived from the legacy base."""
    if base is None:
        return None
    return base.parent / f"{INSTANCE_DIR_PREFIX}{instance_id}{INSTANCE_DIR_SUFFIX}"
