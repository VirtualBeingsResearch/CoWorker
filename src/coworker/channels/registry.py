"""Channel registration, routing, and runtime orchestration."""

from __future__ import annotations

import asyncio
from dataclasses import replace

from loguru import logger

from coworker.channels.base import (
    Channel,
    ConnectionInfo,
    InboundHandler,
    ParticipantIdResolutionError,
)
from coworker.channels.inbound import InboundEnvelope
from coworker.channels.runtime import ChannelRuntime
from coworker.core.types import CommunicateRequest, IncomingEvent, ToolResult
from coworker.i18n import tr


class ChannelRegistry:
    """Compose channels while leaving mutable transport state in their runtimes."""

    def __init__(self) -> None:
        self._channels: list[Channel] = []
        self._fallback: Channel | None = None
        self._inbound_handler: InboundHandler | None = None
        self._runtime_tasks: dict[int, asyncio.Task[None]] = {}

    @property
    def channels(self) -> list[Channel]:
        return list(self._channels)

    def register(self, channel: Channel) -> None:
        if channel in self._channels:
            raise ValueError(f"channel already registered: {channel.name}")
        if channel.participant_prefix == "" and self._fallback is not None:
            raise ValueError("fallback channel already registered")
        self._channels.append(channel)
        channel.set_inbound_handler(self._inbound_handler)
        if channel.participant_prefix == "":
            self._fallback = channel

    def set_inbound_handler(self, handler: InboundHandler | None) -> None:
        self._inbound_handler = handler
        for channel in self._channels:
            channel.set_inbound_handler(handler)

    async def publish_inbound(self, event: IncomingEvent) -> None:
        if self._inbound_handler is None:
            raise RuntimeError("no inbound handler registered")
        await self._inbound_handler(event)

    async def receive_raw(self, envelope: InboundEnvelope) -> None:
        _, channel = self._resolve(envelope.participant_id)
        target = channel if channel is not None else self._fallback
        if target is None:
            raise RuntimeError("no channel registered for inbound message")
        await target.receive_raw(envelope)

    def resolve_participant_id(self, participant_id: str) -> str:
        canonical, _ = self._resolve(participant_id)
        return canonical

    def supports_message_extra(self, participant_id: str) -> bool:
        canonical, channel = self._resolve(participant_id)
        target = channel if channel is not None else self._fallback
        return target.supports_extra_for(canonical) if target is not None else False

    async def send(self, request: CommunicateRequest) -> ToolResult:
        canonical, channel = self._resolve(request.participant_id)
        target = channel if channel is not None else self._fallback
        if target is None:
            return ToolResult(
                tool_call_id="",
                content=tr("tool_result.communicate.failed", error="no channel registered"),
                is_error=True,
            )
        return await target.send(replace(request, participant_id=canonical))

    def list_connections(self) -> list[ConnectionInfo]:
        connections: list[ConnectionInfo] = []
        for channel in self._channels:
            connections.extend(channel.list_connections())
        return connections

    def record_received(self, participant_id: str) -> None:
        _, channel = self._resolve(participant_id)
        target = channel if channel is not None else self._fallback
        if target is not None:
            target.record_received(participant_id)

    def list_live_stream_participant_ids(self) -> list[str]:
        participant_ids: list[str] = []
        for runtime in self._runtimes():
            list_live = getattr(runtime, "list_live_stream_participant_ids", None)
            if list_live is not None:
                participant_ids.extend(list_live())
        return participant_ids

    async def start(self) -> None:
        """Start every unique runtime once, including runtimes shared by profiles."""
        if self._runtime_tasks:
            return
        for runtime in self._runtimes():
            task = asyncio.create_task(runtime.start(), name=f"channel-runtime:{runtime.name}")
            task.add_done_callback(self._report_runtime_exit)
            self._runtime_tasks[id(runtime)] = task
        await asyncio.sleep(0)

    async def stop(self) -> None:
        """Stop every unique runtime and wait for its background task."""
        runtimes = self._runtimes()
        for runtime in reversed(runtimes):
            await runtime.stop()
        tasks = list(self._runtime_tasks.values())
        self._runtime_tasks.clear()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    def _resolve(self, participant_id: str) -> tuple[str, Channel | None]:
        matched = self._longest_prefix_match(participant_id)
        if matched is not None:
            return participant_id, matched

        resolved: dict[Channel, str] = {}
        for channel in self._channels:
            canonical = channel.resolve(participant_id)
            if canonical is not None:
                resolved[channel] = canonical
        if len(resolved) == 1:
            channel, canonical = next(iter(resolved.items()))
            return canonical, channel
        if len(resolved) > 1:
            raise ParticipantIdResolutionError(
                tr(
                    "tool_result.communicate.ambiguous",
                    participant=participant_id,
                    options=self._resolution_options(resolved),
                )
            )
        return participant_id, None

    def _longest_prefix_match(self, participant_id: str) -> Channel | None:
        matched: Channel | None = None
        for channel in self._channels:
            prefix = channel.participant_prefix
            if prefix and participant_id.startswith(prefix):
                if matched is None or len(prefix) > len(matched.participant_prefix):
                    matched = channel
        return matched

    @staticmethod
    def _resolution_options(resolved: dict[Channel, str]) -> str:
        return "\n".join(
            tr(
                "tool_result.communicate.option",
                id=canonical,
                prefix=channel.participant_prefix or channel.name,
            )
            for channel, canonical in resolved.items()
        )

    def _runtimes(self) -> list[ChannelRuntime]:
        runtimes: list[ChannelRuntime] = []
        seen: set[int] = set()
        for channel in self._channels:
            identity = id(channel.runtime)
            if identity not in seen:
                seen.add(identity)
                runtimes.append(channel.runtime)
        return runtimes

    @staticmethod
    def _report_runtime_exit(task: asyncio.Task[None]) -> None:
        if task.cancelled():
            return
        error = task.exception()
        if error is not None:
            logger.error(f"Channel runtime exited with error: {error}")
