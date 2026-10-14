"""Background attachment delivery shared by channels that upload files."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from loguru import logger

from coworker.core.types import IncomingEvent, ToolResult
from coworker.i18n import bind_locale, tr

_UPLOAD_SOURCE = "channel_upload"


@dataclass(frozen=True)
class UploadBatchStart:
    """What communicate finished before attachment bytes continue in the background."""

    text_sent: bool
    started: tuple[str, ...]
    rejected: tuple[tuple[str, str], ...]


class UploadTracker:
    """Owns the background tasks created by one channel runtime."""

    def __init__(self) -> None:
        self._tasks: set[asyncio.Task[None]] = set()

    def schedule(self, operation: Callable[[], Awaitable[None]], *, name: str) -> None:
        task = asyncio.create_task(bind_locale(operation), name=name)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def drain(self) -> None:
        while self._tasks:
            await asyncio.gather(*tuple(self._tasks))

    async def cancel(self) -> None:
        tasks = tuple(self._tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._tasks.clear()


def upload_tool_result(
    *,
    participant: str,
    text_sent: bool,
    started: tuple[str, ...],
    rejected: tuple[tuple[str, str], ...],
    sent_key: str,
    failed_key: str,
) -> ToolResult:
    """Describe an immediate communicate result for text plus background uploads."""
    if not text_sent and not started:
        detail = rejection_details(rejected) or tr("channel.upload.failed")
        return ToolResult(
            tool_call_id="",
            content=tr(failed_key, error=detail),
            is_error=True,
        )
    parts: list[str] = []
    if text_sent:
        parts.append(tr(sent_key, participant=participant))
    if started:
        parts.append(
            tr(
                "tool_result.communicate.upload_started",
                participant=participant,
                filenames=", ".join(started),
            )
        )
    if rejected:
        parts.append(
            tr(
                "tool_result.communicate.upload_rejected",
                details=rejection_details(rejected),
            )
        )
    return ToolResult(tool_call_id="", content="\n".join(parts))


def rejection_details(rejected: tuple[tuple[str, str], ...]) -> str:
    return "; ".join(f"{name} ({error})" for name, error in rejected)


def safe_upload_error(error: Exception) -> str:
    """Return an agent-visible reason that does not carry URLs or credentials."""
    message = str(error)
    if isinstance(error, (FileNotFoundError, ValueError)) and "://" not in message:
        return message
    if isinstance(error, TimeoutError):
        return tr("channel.upload.timed_out")
    logger.warning(tr("channel.upload.failed_type", error_type=type(error).__name__))
    return tr("channel.upload.failed")


async def push_upload_status(
    handler: Callable[[IncomingEvent], Awaitable[None]] | None,
    participant_id: str,
    conversation_id: str | None,
    notice: str,
    *,
    label: str,
) -> None:
    """Tell the agent an upload finished without presenting it as a human message."""
    if handler is None:
        return
    try:
        await handler(
            IncomingEvent(
                participant_id=participant_id,
                content=tr("channel.upload.status", label=label, notice=notice),
                conversation_id=conversation_id,
                source=_UPLOAD_SOURCE,
            )
        )
    except Exception as error:
        logger.warning(
            tr("channel.upload.status_delivery_failed", error_type=type(error).__name__)
        )
