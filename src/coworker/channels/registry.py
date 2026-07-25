"""Channel registration, routing, and runtime orchestration."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace

from loguru import logger

from coworker.channels.base import (
    BaseChannel,
    ConnectionInfo,
    InboundHandler,
    ParticipantIdResolutionError,
)
from coworker.channels.inbound import InboundEnvelope
from coworker.channels.runtime import ChannelRuntime
from coworker.core.registration import RegistrationError
from coworker.core.types import CommunicateRequest, ToolResult
from coworker.i18n import tr

_PARTICIPANT_SUGGESTION_DISTANCE = 4


@dataclass(frozen=True)
class PreparedChannelAction:
    request: CommunicateRequest
    result_note: str = ""


ChannelActionHandler = Callable[
    [CommunicateRequest],
    Awaitable[PreparedChannelAction | ToolResult],
]


class ChannelRegistry:
    """Compose channels while leaving mutable transport state in their runtimes."""

    def __init__(self) -> None:
        self._channels: list[BaseChannel] = []
        self._fallback: BaseChannel | None = None
        self._inbound_handler: InboundHandler | None = None
        self._runtime_tasks: dict[int, asyncio.Task[None]] = {}
        self._action_handlers: dict[str, ChannelActionHandler] = {}

    def register(self, channel: BaseChannel) -> None:
        issues = self._registration_issues(channel)
        if issues:
            raise RegistrationError("channel", issues)
        try:
            channel.set_inbound_handler(self._inbound_handler)
        except Exception as error:
            raise RegistrationError(
                "channel",
                [f"set_inbound_handler failed: {error}"],
            ) from error
        self._channels.append(channel)
        if channel.participant_prefix == "":
            self._fallback = channel

    @property
    def is_running(self) -> bool:
        return bool(self._runtime_tasks)

    def register_action(
        self,
        channel: str,
        handler: ChannelActionHandler,
    ) -> None:
        if not channel.strip():
            raise ValueError("channel action name is required")
        if channel in self._action_handlers:
            raise ValueError(f"channel action is already registered: {channel}")
        self._action_handlers[channel] = handler

    def set_inbound_handler(self, handler: InboundHandler | None) -> None:
        self._inbound_handler = handler
        for channel in self._channels:
            channel.set_inbound_handler(handler)

    async def receive_raw(self, envelope: InboundEnvelope) -> None:
        _, channel = self._resolve(envelope.participant_id)
        target = channel if channel is not None else self._fallback
        if target is None:
            raise RuntimeError("no channel registered for inbound message")
        await target.receive_raw(envelope)

    def resolve_participant_id(self, participant_id: str) -> str:
        canonical, _ = self._resolve(participant_id)
        return canonical

    def supports_message_extra(
        self,
        participant_id: str,
        extra: dict[str, object] | None = None,
    ) -> bool:
        canonical, channel = self._resolve(participant_id)
        target = channel if channel is not None else self._fallback
        return target.supports_extra(canonical, extra) if target is not None else False

    async def send(self, request: CommunicateRequest) -> ToolResult:
        canonical, channel = self._resolve(request.participant_id)
        target = channel if channel is not None else self._fallback
        if target is None:
            return ToolResult(
                tool_call_id="",
                content=tr("tool_result.communicate.no_channel", participant=request.participant_id),
                is_error=True,
            )
        validation_error = self._participant_validation_error(
            requested=request.participant_id,
            canonical=canonical,
            target=target,
        )
        if validation_error is not None:
            return validation_error
        prepared = await self._prepare_action(
            replace(request, participant_id=canonical)
        )
        if isinstance(prepared, ToolResult):
            return prepared
        outbound, omitted = target.capabilities_for(canonical).filter(prepared.request)
        result = await target.send(outbound)
        if result.is_error:
            return result
        notices = [prepared.result_note] if prepared.result_note else []
        if not omitted:
            return (
                replace(result, content=f"{result.content}\n{prepared.result_note}")
                if prepared.result_note
                else result
            )
        notice_key = (
            "tool_result.communicate.unsupported_message_only"
            if self._contains_only_message(outbound)
            else "tool_result.communicate.unsupported_omitted"
        )
        notices.append(tr(notice_key, fields=", ".join(omitted)))
        return replace(result, content=f"{result.content}\n" + "\n".join(notices))

    async def _prepare_action(
        self,
        request: CommunicateRequest,
    ) -> PreparedChannelAction | ToolResult:
        raw_action = request.extra.get("channel_action")
        if raw_action is None:
            return PreparedChannelAction(request)
        if not isinstance(raw_action, dict):
            return ToolResult(
                tool_call_id="",
                content=tr("tool_result.communicate.channel_action_invalid"),
                is_error=True,
            )
        channel = str(raw_action.get("channel") or "").strip()
        handler = self._action_handlers.get(channel)
        if handler is None:
            return ToolResult(
                tool_call_id="",
                content=tr(
                    "tool_result.communicate.channel_action_unknown",
                    channel=channel,
                ),
                is_error=True,
            )
        return await handler(request)

    def list_connections(self) -> list[ConnectionInfo]:
        connections: list[ConnectionInfo] = []
        for channel in self._channels:
            connections.extend(channel.list_connections())
        return connections

    def _participant_validation_error(
        self,
        *,
        requested: str,
        canonical: str,
        target: BaseChannel,
    ) -> ToolResult | None:
        if not target.requires_known_participant:
            return None

        known_ids = sorted(
            {
                participant_id
                for channel in self._channels
                for participant_id in channel.known_participant_ids()
            }
        )
        if canonical in known_ids:
            return None

        suggestions = self._similar_participant_ids(requested, known_ids)
        if suggestions:
            content = tr(
                "tool_result.communicate.participant_similar",
                participant=requested,
                max_distance=_PARTICIPANT_SUGGESTION_DISTANCE,
                options="\n".join(f"  - {participant_id}" for participant_id in suggestions),
            )
        else:
            content = tr(
                "tool_result.communicate.participant_unknown",
                participant=requested,
            )
        return ToolResult(tool_call_id="", content=content, is_error=True)

    @staticmethod
    def _similar_participant_ids(
        participant_id: str,
        known_ids: list[str],
    ) -> list[str]:
        ranked: list[tuple[int, str]] = []
        for known_id in known_ids:
            aliases = {known_id, known_id.rsplit(":", maxsplit=1)[-1]}
            distances = [
                distance
                for alias in aliases
                if (
                    distance := _bounded_edit_distance(
                        participant_id,
                        alias,
                        _PARTICIPANT_SUGGESTION_DISTANCE,
                    )
                )
                is not None
            ]
            if distances:
                ranked.append((min(distances), known_id))
        return [known_id for _, known_id in sorted(ranked)]

    def record_received(self, participant_id: str) -> None:
        _, channel = self._resolve(participant_id)
        target = channel if channel is not None else self._fallback
        if target is not None:
            target.record_received(participant_id)

    async def start(self) -> None:
        """Start every unique runtime once, including runtimes shared by profiles."""
        if self._runtime_tasks:
            return
        for runtime in self._runtimes():
            task = asyncio.create_task(runtime.start(), name=f"channel-runtime:{runtime.name}")
            task.add_done_callback(self._report_runtime_exit)
            self._runtime_tasks[id(runtime)] = task
        await asyncio.sleep(0)
        failures = [
            task
            for task in self._runtime_tasks.values()
            if task.done() and not task.cancelled() and task.exception() is not None
        ]
        if failures:
            issues = [
                f"{task.get_name().removeprefix('channel-runtime:')}: {task.exception()}"
                for task in failures
            ]
            await self.stop()
            details = "\n".join(f"  - {issue}" for issue in issues)
            raise RuntimeError(
                f"channel runtime startup failed with {len(issues)} "
                f"{'issue' if len(issues) == 1 else 'issues'}:\n{details}"
            )

    async def stop(self) -> None:
        """Stop every unique runtime and wait for its background task."""
        if not self._runtime_tasks:
            return
        runtimes = self._runtimes()
        for runtime in reversed(runtimes):
            await runtime.stop()
        tasks = list(self._runtime_tasks.values())
        self._runtime_tasks.clear()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    def _resolve(self, participant_id: str) -> tuple[str, BaseChannel | None]:
        matched = self._longest_prefix_match(participant_id)
        if matched is not None:
            return participant_id, matched

        resolved: dict[BaseChannel, str] = {}
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

    def _longest_prefix_match(self, participant_id: str) -> BaseChannel | None:
        matched: BaseChannel | None = None
        for channel in self._channels:
            prefix = channel.participant_prefix
            if prefix and participant_id.startswith(prefix):
                if matched is None or len(prefix) > len(matched.participant_prefix):
                    matched = channel
        return matched

    @staticmethod
    def _resolution_options(resolved: dict[BaseChannel, str]) -> str:
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

    def _registration_issues(self, channel: BaseChannel) -> list[str]:
        issues: list[str] = []
        if self.is_running:
            issues.append("cannot register while the registry is running")

        name = getattr(channel, "name", None)
        if not isinstance(name, str):
            issues.append("name must be a string")
        elif not name.strip():
            issues.append("name is required")
        elif name != name.strip():
            issues.append("name must not have surrounding whitespace")

        prefix = getattr(channel, "participant_prefix", None)
        if not isinstance(prefix, str):
            issues.append("participant_prefix must be a string")
        elif prefix and (not prefix.strip() or prefix != prefix.strip()):
            issues.append(
                "participant_prefix must be empty or contain no surrounding whitespace"
            )

        runtime = getattr(channel, "runtime", None)
        if not isinstance(runtime, ChannelRuntime):
            issues.append("runtime must implement ChannelRuntime")
        if not isinstance(channel, BaseChannel):
            issues.append("channel must inherit BaseChannel")

        if any(existing is channel for existing in self._channels):
            issues.append("the same channel instance is already registered")
        if isinstance(name, str) and any(
            existing.name == name for existing in self._channels
        ):
            issues.append(f"name {name!r} is already registered")
        if isinstance(prefix, str):
            if prefix == "" and self._fallback is not None:
                issues.append(
                    f"fallback channel is already registered as {self._fallback.name!r}"
                )
            elif prefix and any(
                existing.participant_prefix == prefix for existing in self._channels
            ):
                issues.append(f"participant_prefix {prefix!r} is already registered")
        return issues

    @staticmethod
    def _contains_only_message(request: CommunicateRequest) -> bool:
        return bool(request.message) and not (
            request.conversation_id or request.attachments or request.extra
        )

    @staticmethod
    def _report_runtime_exit(task: asyncio.Task[None]) -> None:
        if task.cancelled():
            return
        error = task.exception()
        if error is not None:
            logger.error(f"Channel runtime exited with error: {error}")


def _bounded_edit_distance(left: str, right: str, limit: int) -> int | None:
    if abs(len(left) - len(right)) > limit:
        return None
    if len(left) > len(right):
        left, right = right, left

    previous = list(range(len(left) + 1))
    for right_index, right_character in enumerate(right, start=1):
        current = [right_index]
        for left_index, left_character in enumerate(left, start=1):
            current.append(
                min(
                    current[-1] + 1,
                    previous[left_index] + 1,
                    previous[left_index - 1]
                    + (left_character != right_character),
                )
            )
        if min(current) > limit:
            return None
        previous = current

    distance = previous[-1]
    return distance if distance <= limit else None
