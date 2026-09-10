"""Inbound thinking placeholders and reply-timeout reminders."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from loguru import logger

from coworker.core.types import (
    CommunicateRequest,
    IncomingEvent,
    Message,
    ToolResult,
)
from coworker.i18n import tr

if TYPE_CHECKING:
    from coworker.channels.base import BaseChannel
    from coworker.channels.registry import ChannelRegistry
    from coworker.core.config import Config
    from coworker.memory.short_term import ShortTermMemory

# 开占位是拦在入站链路上的外呼：上限之内失败只是没有占位，
# 超时则说明通道 API 不健康，不能让上一条消息一直等它。
_OPEN_TIMEOUT_SECONDS = 10.0


@dataclass
class ProgressTransport:
    """Channel-owned handles needed to overwrite or close a placeholder."""

    channel: str
    participant_id: str = ""
    frame: dict[str, Any] | None = None
    stream_id: str | None = None
    telegram_chat_id: int | None = None
    telegram_message_id: int | None = None


@dataclass
class ProgressPlaceholder:
    participant_id: str
    conversation_id: str | None
    opened_at: float
    transport: ProgressTransport
    reminded: bool = False


@dataclass(frozen=True)
class ProgressTailDelivery:
    """How far a placeholder reply's remaining content got.

    Counts cover only what the caller asked this delivery to send: the first
    text chunk already went into the placeholder itself.
    """

    chunks_sent: int = 0
    chunks_total: int = 0
    attachments_sent: int = 0
    attachments_total: int = 0
    error: str = ""

    @property
    def complete(self) -> bool:
        return not self.error


_PREVIEW_LIMIT = 60


def partial_delivery_note(
    *,
    sent_key: str,
    participant_id: str,
    chunks: list[str],
    attachments: list[dict[str, Any]],
    delivery: ProgressTailDelivery,
) -> str:
    """Describe exactly where a partial reply stopped, for the model to resume from.

    A bare "chunk 4 of 6" is not something the model can act on: it never sees
    how its own text was split. Naming the first unsent chunk (or attachment)
    by content is what makes "resend only the rest" possible.
    """
    return tr(
        sent_key,
        participant=participant_id,
        chunks_sent=delivery.chunks_sent + 1,
        chunks_total=delivery.chunks_total + 1,
        attachments_sent=delivery.attachments_sent,
        attachments_total=delivery.attachments_total,
        cut=cut_point_note(
            chunks=chunks,
            attachments=attachments,
            delivery=delivery,
        ),
        error=delivery.error,
    )


def cut_point_note(
    *,
    chunks: list[str],
    attachments: list[dict[str, Any]],
    delivery: ProgressTailDelivery,
) -> str:
    """Name the first piece of content that never reached the recipient."""
    if delivery.chunks_sent < delivery.chunks_total:
        first_missing_chunk = chunks[delivery.chunks_sent + 1]
        return tr(
            "tool_result.communicate.cut_at_text",
            preview=_preview(first_missing_chunk),
        )
    first_missing_attachment = attachments[delivery.attachments_sent]
    return tr(
        "tool_result.communicate.cut_at_attachment",
        filename=attachment_display_name(first_missing_attachment),
    )


def attachment_display_name(attachment: dict[str, Any]) -> str:
    raw = str(attachment.get("filename") or attachment.get("path") or "")
    return Path(raw).name or str(attachment.get("type") or "attachment")


def _preview(text: str, limit: int = _PREVIEW_LIMIT) -> str:
    collapsed = " ".join(text.split())
    if len(collapsed) <= limit:
        return collapsed
    return f"{collapsed[:limit]}…"


class ChannelProgressCoordinator:
    """Track one live inbound placeholder per direct conversation."""

    def __init__(self, registry: ChannelRegistry, config: Config) -> None:
        self._registry = registry
        self._config = config
        self._is_setup: Callable[[], bool] = lambda: False
        self._placeholders: dict[str, ProgressPlaceholder] = {}
        # 同一对象的开占位必须串行（通道可能并发分发入站），键与占位同量级。
        self._locks: dict[str, asyncio.Lock] = {}

    def set_setup_predicate(self, predicate: Callable[[], bool]) -> None:
        self._is_setup = predicate

    async def on_inbound(self, event: IncomingEvent) -> None:
        """Give one inbound message a placeholder; never fail the message itself.

        占位是尽力而为的装饰：解析对象、开占位、结束旧占位的任何失败都只写日志，
        调用方的入站处理链必须照常拿到这条消息。
        """
        if not self._progress_allowed():
            return
        try:
            async with self._lock_for(event.participant_id):
                await self._open_placeholder(event)
        except Exception as error:
            logger.warning(
                f"channel progress skipped participant={event.participant_id}: {error}"
            )

    async def _open_placeholder(self, event: IncomingEvent) -> None:
        canonical, channel = self._target_channel(event.participant_id)
        if channel is None or not channel.capabilities_for(canonical).progress:
            return
        event = _with_participant(event, canonical)
        existing = self._placeholders.get(event.participant_id)
        if existing is not None:
            await self._close(existing, channel, tr("channel.progress.replaced"))
            self._discard(existing)
        text = tr("channel.progress.thinking")
        try:
            transport = await asyncio.wait_for(
                channel.open_progress(event, text),
                timeout=_OPEN_TIMEOUT_SECONDS,
            )
        except Exception as error:
            logger.warning(
                f"channel progress open failed participant={event.participant_id}: {error}"
            )
            return
        if transport is None:
            return
        self._placeholders[event.participant_id] = ProgressPlaceholder(
            participant_id=event.participant_id,
            conversation_id=event.conversation_id,
            opened_at=time.monotonic(),
            transport=transport,
        )

    def _lock_for(self, participant_id: str) -> asyncio.Lock:
        lock = self._locks.get(participant_id)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[participant_id] = lock
        return lock

    async def overwrite_or_send(
        self,
        request: CommunicateRequest,
        send: Callable[[CommunicateRequest], Awaitable[ToolResult]],
        channel: BaseChannel,
    ) -> ToolResult:
        placeholder = self._match(request)
        if placeholder is None:
            return await send(request)
        try:
            result = await channel.overwrite_progress(placeholder.transport, request)
        except Exception as error:
            logger.warning(
                f"channel progress overwrite failed participant={request.participant_id}: {error}"
            )
            self._discard(placeholder)
            return await send(request)
        if result.is_error:
            # 通道报告正文没能写进占位，由它自己收拾那条占位；这里只退回普通发送。
            self._discard(placeholder)
            return await send(request)
        self._discard(placeholder)
        return result

    def consume_due_reminders(
        self,
        *,
        participant_id: str | None = None,
        claimed: Callable[[ProgressPlaceholder], bool] | None = None,
    ) -> list[tuple[str, int]]:
        timeout = self._config.agent.channel_progress_reply_reminder_seconds
        if not self._config.agent.channel_progress_enabled or timeout <= 0:
            return []
        now = time.monotonic()
        due: list[tuple[str, int]] = []
        for placeholder in self._placeholders.values():
            if placeholder.reminded:
                continue
            if participant_id is not None and placeholder.participant_id != participant_id:
                continue
            if claimed is not None and claimed(placeholder):
                continue
            elapsed = int(now - placeholder.opened_at)
            if elapsed < timeout:
                continue
            placeholder.reminded = True
            due.append((placeholder.participant_id, elapsed))
        return due

    def inject_reply_reminders(
        self,
        short_term: ShortTermMemory,
        *,
        participant_id: str | None = None,
        claimed: Callable[[ProgressPlaceholder], bool] | None = None,
    ) -> list[str]:
        injected: list[str] = []
        for pid, seconds in self.consume_due_reminders(
            participant_id=participant_id,
            claimed=claimed,
        ):
            content = tr("loop.reply_reminder", participant=pid, seconds=seconds)
            message = Message(role="user", content=content, source="system_reminder")
            short_term.primary.append(message)
            injected.append(content)
        return injected

    async def expire_unanswered(
        self,
        short_term: ShortTermMemory | None = None,
        *,
        participant_id: str | None = None,
        claimed: Callable[[ProgressPlaceholder], bool] | None = None,
    ) -> list[str]:
        """Gracefully close placeholders that nobody answered in time.

        占位不能永远停在「正在思考中…」：超过时限就换成中性说明并结束，对方至少
        知道这条消息还没被丢掉。结束同样要回报模型——否则它会以为占位还在等着
        自己覆盖，或者不知道这条消息至今没回。
        """
        timeout = self._config.agent.channel_progress_timeout_seconds
        if not self._config.agent.channel_progress_enabled or timeout <= 0:
            return []
        expired: list[str] = []
        for placeholder in self._due_for_expiry(
            timeout,
            participant_id=participant_id,
            claimed=claimed,
        ):
            await self._close_placeholder(placeholder, tr("channel.progress.expired"))
            if short_term is None:
                continue
            content = tr(
                "loop.placeholder_expired",
                participant=placeholder.participant_id,
                seconds=int(time.monotonic() - placeholder.opened_at),
                notice=tr("channel.progress.expired"),
            )
            short_term.primary.append(
                Message(role="user", content=content, source="system_reminder")
            )
            expired.append(content)
        return expired

    async def close_all_placeholders(self) -> None:
        """Close every live placeholder before the process stops.

        进程退出不会自动替对方结束占位，重启后内存里的占位也没了：不管的话那条
        「正在思考中…」就永久留在对方那里，再也不会有人覆盖它。
        """
        for placeholder in list(self._placeholders.values()):
            await self._close_placeholder(placeholder, tr("channel.progress.expired"))

    def _due_for_expiry(
        self,
        timeout: int,
        *,
        participant_id: str | None,
        claimed: Callable[[ProgressPlaceholder], bool] | None,
    ) -> list[ProgressPlaceholder]:
        now = time.monotonic()
        return [
            placeholder
            for placeholder in list(self._placeholders.values())
            if (participant_id is None or placeholder.participant_id == participant_id)
            and not (claimed is not None and claimed(placeholder))
            and now - placeholder.opened_at >= timeout
        ]

    async def _close_placeholder(
        self,
        placeholder: ProgressPlaceholder,
        text: str,
    ) -> None:
        try:
            _, channel = self._target_channel(placeholder.participant_id)
        except Exception as error:
            logger.warning(
                f"channel progress expiry could not resolve "
                f"participant={placeholder.participant_id}: {error}"
            )
            channel = None
        if channel is not None:
            try:
                await asyncio.wait_for(
                    self._close(placeholder, channel, text),
                    timeout=_OPEN_TIMEOUT_SECONDS,
                )
            except Exception as error:
                logger.warning(
                    f"channel progress expiry close failed "
                    f"participant={placeholder.participant_id}: {error}"
                )
        self._discard(placeholder)

    def live_placeholders(self) -> list[ProgressPlaceholder]:
        return list(self._placeholders.values())

    def _progress_allowed(self) -> bool:
        return (
            self._config.agent.channel_progress_enabled
            and not self._config.agent.paused
            and not self._is_setup()
        )

    def _match(self, request: CommunicateRequest) -> ProgressPlaceholder | None:
        # 一个对象只保留一条占位：入站已经按对象串行，同一时刻最多只有一个活跃窗口。
        return self._placeholders.get(request.participant_id)

    def _discard(self, placeholder: ProgressPlaceholder) -> None:
        # 按对象身份核验：结束/覆盖都跨着 await，期间可能已经来了新消息并换了占位，
        # 直接按 participant_id 删除会把新占位一起删掉，让它永远等不到覆盖。
        if self._placeholders.get(placeholder.participant_id) is placeholder:
            self._placeholders.pop(placeholder.participant_id, None)

    def _target_channel(
        self,
        participant_id: str,
    ) -> tuple[str, BaseChannel | None]:
        canonical, channel = self._registry._resolve(participant_id)
        target = channel if channel is not None else self._registry._fallback
        return canonical, target

    async def _close(
        self,
        placeholder: ProgressPlaceholder,
        channel: BaseChannel,
        text: str,
    ) -> None:
        try:
            await channel.close_progress(placeholder.transport, text)
        except Exception as error:
            logger.warning(
                f"channel progress close failed participant={placeholder.participant_id}: {error}"
            )


def _with_participant(event: IncomingEvent, participant_id: str) -> IncomingEvent:
    if event.participant_id == participant_id:
        return event
    event.participant_id = participant_id
    return event
