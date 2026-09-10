"""Inbound thinking placeholders and reply-timeout reminders."""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from loguru import logger

from coworker.core.types import CommunicateRequest, IncomingEvent, ToolResult
from coworker.i18n import tr

if TYPE_CHECKING:
    from coworker.channels.base import BaseChannel
    from coworker.channels.registry import ChannelRegistry
    from coworker.core.config import Config
    from coworker.memory.short_term import ShortTermMemory


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
    speaker_id: str | None
    conversation_id: str | None
    is_group: bool
    opened_at: float
    transport: ProgressTransport
    reminded: bool = False


class ChannelProgressCoordinator:
    """Track live inbound placeholders and match them to communicate/rest."""

    def __init__(self, registry: ChannelRegistry, config: Config) -> None:
        self._registry = registry
        self._config = config
        self._is_setup: Callable[[], bool] = lambda: False
        self._placeholders: dict[tuple[str, ...], ProgressPlaceholder] = {}

    def set_setup_predicate(self, predicate: Callable[[], bool]) -> None:
        self._is_setup = predicate

    async def on_inbound(self, event: IncomingEvent) -> None:
        if not self._progress_allowed():
            return
        canonical, channel = self._target_channel(event.participant_id)
        if channel is None or not channel.capabilities_for(canonical).progress:
            return
        event = _with_participant(event, canonical)
        key = self._key(event, channel)
        existing = self._placeholders.get(key)
        if existing is not None:
            await self._close(existing, channel, tr("channel.progress.replaced"))
            self._discard(existing)
        text = tr("channel.progress.thinking")
        try:
            transport = await channel.open_progress(event, text)
        except Exception as error:
            logger.warning(
                f"channel progress open failed participant={event.participant_id}: {error}"
            )
            return
        if transport is None:
            return
        self._placeholders[key] = ProgressPlaceholder(
            participant_id=event.participant_id,
            speaker_id=event.speaker_id,
            conversation_id=event.conversation_id,
            is_group=channel.progress_is_group(event),
            opened_at=time.monotonic(),
            transport=transport,
        )

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
            await self._close(placeholder, channel, "")
            self._discard(placeholder)
            return await send(request)
        if result.is_error:
            await self._close(placeholder, channel, "")
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
        from coworker.core.types import Message

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

    def live_placeholders(self) -> list[ProgressPlaceholder]:
        return list(self._placeholders.values())

    def _progress_allowed(self) -> bool:
        return (
            self._config.agent.channel_progress_enabled
            and not self._config.agent.paused
            and not self._is_setup()
        )

    def _key(self, event: IncomingEvent, channel: BaseChannel) -> tuple[str, ...]:
        if channel.progress_is_group(event):
            return ("group", event.participant_id, event.speaker_id or "")
        return ("dm", event.participant_id)

    def _match(self, request: CommunicateRequest) -> ProgressPlaceholder | None:
        live = [
            placeholder
            for placeholder in self._placeholders.values()
            if placeholder.participant_id == request.participant_id
        ]
        if not live:
            return None
        if request.conversation_id:
            hits = [
                placeholder
                for placeholder in live
                if placeholder.conversation_id == request.conversation_id
            ]
            if hits:
                return max(hits, key=lambda item: item.opened_at)
        return max(live, key=lambda item: item.opened_at)

    def _placeholder_key(self, placeholder: ProgressPlaceholder) -> tuple[str, ...]:
        if placeholder.is_group:
            return ("group", placeholder.participant_id, placeholder.speaker_id or "")
        return ("dm", placeholder.participant_id)

    def _discard(self, placeholder: ProgressPlaceholder) -> None:
        self._placeholders.pop(self._placeholder_key(placeholder), None)

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
