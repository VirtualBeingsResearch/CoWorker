"""OpenAI-compatible HTTP channel: extra tokens, waiters, and control."""

from __future__ import annotations

import asyncio
import hashlib
import json
from typing import TYPE_CHECKING, Any

from coworker.channels.base import (
    BaseChannel,
    ChannelCapabilities,
    ConnectionInfo,
)
from coworker.channels.openai.tokens import ExtraTokenStore
from coworker.channels.openai.waiters import (
    BusyError,
    OpenAICompletion,
    OpenAISessionTable,
    OpenAITurn,
)
from coworker.core.communication_tokens import (
    CONTROL_PARTICIPANT_ID,
    PRIMARY_PARTICIPANT_ID,
    PRIMARY_TOKEN_NAME,
    participant_id_for_token_name,
    token_name_from_participant,
    validate_token_name,
)
from coworker.core.types import CommunicateRequest, IncomingEvent, ToolResult
from coworker.i18n import tr
from coworker.persona import PersonAlias, PersonStore

if TYPE_CHECKING:
    from coworker.channels.runtime import ChannelRuntime


def fingerprint_conversation(messages: list[dict[str, Any]]) -> str:
    """Stable window id from originating system + first user text."""

    first_system = ""
    first_user = ""
    for item in messages:
        role = str(item.get("role") or "")
        text = _message_text(item)
        if role == "system" and not first_system:
            first_system = text
        elif role == "user" and not first_user:
            first_user = text
        if first_system and first_user:
            break
    payload = f"{_canonical(first_system)}\n---\n{_canonical(first_user)}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def last_user_text(messages: list[dict[str, Any]]) -> str:
    for item in reversed(messages):
        if str(item.get("role") or "") == "user":
            return _message_text(item)
    return ""


def first_system_text(messages: list[dict[str, Any]]) -> str:
    for item in messages:
        if str(item.get("role") or "") == "system":
            return _message_text(item)
    return ""


def _message_text(message: dict[str, Any]) -> str:
    content = message.get("content")
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "text":
                parts.append(str(block.get("text") or ""))
            elif "text" in block:
                parts.append(str(block.get("text") or ""))
        return "\n".join(part for part in parts if part).strip()
    return ""


def _canonical(text: str) -> str:
    return "\n".join(line.rstrip() for line in text.strip().splitlines())


def catalog_from_tools(tools: list[dict[str, Any]] | None) -> dict[str, dict[str, Any]]:
    catalog: dict[str, dict[str, Any]] = {}
    for item in tools or []:
        if not isinstance(item, dict):
            continue
        function = item.get("function") if item.get("type") == "function" else item
        if not isinstance(function, dict):
            continue
        name = str(function.get("name") or "").strip()
        if name:
            catalog[name] = function
    return catalog


class OpenAIChannel(BaseChannel):
    """1:1 OpenAI-compat addresses plus ``openai:control`` token issuance."""

    name = "openai"
    participant_prefix = "openai:"
    requires_known_participant = True

    def __init__(
        self,
        *,
        extras: ExtraTokenStore,
        runtime: ChannelRuntime | None = None,
        person_store: PersonStore | None = None,
        timeout_seconds: float = 180,
        native_tool_names: set[str] | None = None,
    ) -> None:
        super().__init__(
            runtime=runtime,
            capabilities=ChannelCapabilities(conversation_id=True, extra=True),
        )
        self._extras = extras
        self._sessions = OpenAISessionTable()
        self._person_store = person_store
        self.timeout_seconds = timeout_seconds
        self._native_tool_names = set(native_tool_names or ())

    def set_person_store(self, store: PersonStore | None) -> None:
        self._person_store = store

    def set_native_tool_names(self, names: set[str]) -> None:
        self._native_tool_names = set(names)

    def extras(self) -> ExtraTokenStore:
        return self._extras

    def sessions(self) -> OpenAISessionTable:
        return self._sessions

    def resolve(self, participant_id: str) -> str | None:
        if participant_id == CONTROL_PARTICIPANT_ID:
            return participant_id
        name = token_name_from_participant(participant_id)
        if name == PRIMARY_TOKEN_NAME:
            return PRIMARY_PARTICIPANT_ID
        if name and self._extras.has(name):
            return participant_id_for_token_name(name)
        return None

    def known_participant_ids(self) -> set[str]:
        ids = {CONTROL_PARTICIPANT_ID, PRIMARY_PARTICIPANT_ID}
        ids.update(participant_id_for_token_name(name) for name in self._extras.names())
        return ids

    def list_connections(self) -> list[ConnectionInfo]:
        connections = [
            ConnectionInfo(
                participant_id=CONTROL_PARTICIPANT_ID,
                channel=self.name,
                kind="openai:control",
                display_name=tr("channel.openai.control_name"),
                active=True,
            ),
            ConnectionInfo(
                participant_id=PRIMARY_PARTICIPANT_ID,
                channel=self.name,
                kind="openai:token",
                display_name=tr("channel.openai.primary_name"),
                active=True,
            ),
        ]
        for name in self._extras.names():
            participant_id = participant_id_for_token_name(name)
            sent_at, received_at = self.activity_for(participant_id)
            connections.append(
                ConnectionInfo(
                    participant_id=participant_id,
                    channel=self.name,
                    kind="openai:token",
                    display_name=tr("channel.openai.token_name", name=name),
                    active=True,
                    last_sent_at=sent_at,
                    last_received_at=received_at,
                )
            )
        return connections

    def capabilities_for(self, participant_id: str) -> ChannelCapabilities:
        if participant_id == CONTROL_PARTICIPANT_ID:
            return ChannelCapabilities(extra=True)
        return ChannelCapabilities(conversation_id=True)

    def agent_instructions(self) -> str:
        return tr("prompt.channel.openai")

    @staticmethod
    def _error(content: str) -> ToolResult:
        return ToolResult(tool_call_id="", content=content, is_error=True)

    async def send(self, request: CommunicateRequest) -> ToolResult:
        if request.participant_id == CONTROL_PARTICIPANT_ID:
            return await self._control(request.extra)
        conversation_id = (request.conversation_id or "").strip()
        if not conversation_id:
            active = self._in_flight_for(request.participant_id)
            if len(active) == 1:
                conversation_id = active[0].conversation_id
            elif len(active) > 1:
                return self._error(tr("tool_result.communicate.openai_conversation_required"))
            else:
                return self._error(
                    tr(
                        "tool_result.communicate.openai_no_waiter",
                        participant=request.participant_id,
                    )
                )
        turn = self._sessions.get_active(request.participant_id, conversation_id)
        if turn is None or not turn.in_flight:
            return self._error(
                tr(
                    "tool_result.communicate.openai_no_waiter",
                    participant=request.participant_id,
                )
            )
        if not turn.fulfill_stop(request.message):
            return self._error(tr("tool_result.communicate.openai_late"))
        self._record_sent(request.participant_id)
        return ToolResult(
            tool_call_id="",
            content=tr(
                "tool_result.communicate.openai_sent",
                participant=request.participant_id,
                conversation=conversation_id,
            ),
        )

    def _in_flight_for(self, participant_id: str) -> list[OpenAITurn]:
        found: list[OpenAITurn] = []
        for turn in self._sessions.in_flight_for(participant_id):
            found.append(turn)
        return found

    async def open_user_turn(
        self,
        *,
        participant_id: str,
        conversation_id: str,
        user_text: str,
        system_text: str,
        catalog: dict[str, dict[str, Any]],
        originating_task: str,
    ) -> OpenAICompletion:
        if self._sessions.awaiting_tools(participant_id, conversation_id):
            raise BusyError("tools")
        turn = OpenAITurn(
            participant_id=participant_id,
            conversation_id=conversation_id,
            catalog=catalog,
            timeout_seconds=self.timeout_seconds,
        )
        try:
            self._sessions.begin_user_turn(turn)
        except BusyError:
            raise
        content = self._inbound_body(
            user_text=user_text,
            system_text=system_text,
            catalog=catalog,
            originating_task=originating_task,
        )
        await self.publish_inbound(
            IncomingEvent(
                participant_id=participant_id,
                conversation_id=conversation_id,
                content=content,
                source="openai",
            )
        )
        self.record_received(participant_id)
        return await self._await_turn(turn)

    async def _await_turn(self, turn: OpenAITurn) -> OpenAICompletion:
        try:
            completion = await asyncio.wait_for(
                turn.completion, timeout=self.timeout_seconds
            )
        except TimeoutError:
            turn.expire()
            self._sessions.discard(turn)
            return OpenAICompletion(kind="stop", content="", timed_out=True)
        except Exception:
            self._sessions.discard(turn)
            raise
        if completion.timed_out:
            self._sessions.discard(turn)
            return completion
        if completion.kind == "tool_calls":
            self._sessions.mark_awaiting_tools(turn)
        else:
            self._sessions.discard(turn)
        return completion

    async def open_tool_followup(
        self,
        *,
        participant_id: str,
        conversation_id: str,
        results: dict[str, str],
    ) -> OpenAICompletion:
        pending = self._sessions.pending_tool_turn(participant_id, conversation_id)
        if pending is None:
            raise ValueError(tr("api.openai.tool_followup_unexpected"))
        body = self._tool_results_body(pending, results)
        followup = OpenAITurn(
            participant_id=participant_id,
            conversation_id=conversation_id,
            catalog=pending.catalog,
            timeout_seconds=self.timeout_seconds,
        )
        self._sessions.begin_tool_followup(followup, results)
        await self.publish_inbound(
            IncomingEvent(
                participant_id=participant_id,
                conversation_id=conversation_id,
                content=body,
                source="openai",
            )
        )
        self.record_received(participant_id)
        return await self._await_turn(followup)

    def prepare_client_tool_batch(
        self,
        participant_id: str,
        conversation_id: str,
        count: int,
    ) -> None:
        resolved = conversation_id
        if not resolved:
            active = self._in_flight_for(participant_id)
            if len(active) == 1:
                resolved = active[0].conversation_id
            else:
                return
        turn = self._sessions.get_active(participant_id, resolved)
        if turn is not None and turn.in_flight:
            turn.prepare_client_calls(count)

    async def call_client_tool(
        self,
        *,
        name: str,
        arguments: dict[str, Any],
        participant_id: str,
        conversation_id: str | None,
    ) -> ToolResult:
        if not conversation_id:
            active = self._in_flight_for(participant_id)
            if len(active) == 1:
                conversation_id = active[0].conversation_id
            else:
                return ToolResult(
                    tool_call_id="",
                    content=tr("tool_result.client_tool.conversation_required"),
                    is_error=True,
                )
        turn = self._sessions.get_active(participant_id, conversation_id)
        if turn is None or not turn.in_flight:
            return ToolResult(
                tool_call_id="",
                content=tr("tool_result.client_tool.no_waiter"),
                is_error=True,
            )
        if name in self._native_tool_names:
            return ToolResult(
                tool_call_id="",
                content=tr("tool_result.client_tool.native_name", name=name),
                is_error=True,
            )
        try:
            pending = turn.register_client_call(name, arguments)
        except ValueError as error:
            return ToolResult(tool_call_id="", content=str(error), is_error=True)
        if turn.expected_client_calls <= 0:
            turn.flush_tool_calls()
        return ToolResult(
            tool_call_id="",
            content=tr(
                "tool_result.client_tool.dispatched",
                name=name,
                call_id=pending.openai_id,
            ),
        )

    def _tool_results_body(self, turn: OpenAITurn, results: dict[str, str]) -> str:
        items = [
            tr(
                "channel.openai.tool_result_item",
                call_id=item.openai_id,
                name=item.name,
                content=results.get(item.openai_id, ""),
            )
            for item in turn.pending_calls()
        ]
        return tr("channel.openai.tool_results", items="\n\n".join(items))

    def _inbound_body(
        self,
        *,
        user_text: str,
        system_text: str,
        catalog: dict[str, dict[str, Any]],
        originating_task: str,
    ) -> str:
        parts: list[str] = []
        if originating_task:
            parts.append(
                tr("channel.openai.originating_task", task=originating_task[:240])
            )
        if system_text:
            parts.append(tr("channel.openai.request_system", text=system_text))
        if catalog:
            rendered = json.dumps(list(catalog.values()), ensure_ascii=False, indent=2)
            parts.append(tr("channel.openai.client_tools", tools=rendered))
        parts.append(user_text)
        return "\n\n".join(part for part in parts if part)

    async def _control(self, extra: dict[str, Any]) -> ToolResult:
        action = str(extra.get("action") or "").strip()
        if action == "issue":
            return await self._issue(extra)
        if action == "rotate":
            return await self._rotate(extra)
        if action == "revoke":
            return await self._revoke(extra)
        if action == "list":
            return self._list_tokens()
        return self._error(
            tr("tool_result.communicate.openai_control_action", action=action)
        )

    async def _issue(self, extra: dict[str, Any]) -> ToolResult:
        try:
            name = validate_token_name(str(extra.get("name") or ""))
        except ValueError as error:
            return self._error(str(error))
        if self._extras.has(name):
            return self._error(tr("tool_result.communicate.openai_token_exists", name=name))
        try:
            secret = await self._extras.issue(name)
        except ValueError as error:
            return self._error(str(error))
        participant_id = participant_id_for_token_name(name)
        bind_note = await self._maybe_bind(extra, participant_id)
        return ToolResult(
            tool_call_id="",
            content=tr(
                "tool_result.communicate.openai_issued",
                name=name,
                participant=participant_id,
                token=secret,
                bind=bind_note,
            ),
        )

    async def _rotate(self, extra: dict[str, Any]) -> ToolResult:
        try:
            name = validate_token_name(str(extra.get("name") or ""))
        except ValueError as error:
            return self._error(str(error))
        try:
            secret = await self._extras.rotate(name)
        except (KeyError, ValueError):
            return self._error(tr("tool_result.communicate.openai_token_missing", name=name))
        return ToolResult(
            tool_call_id="",
            content=tr(
                "tool_result.communicate.openai_rotated",
                name=name,
                participant=participant_id_for_token_name(name),
                token=secret,
            ),
        )

    async def _revoke(self, extra: dict[str, Any]) -> ToolResult:
        try:
            name = validate_token_name(str(extra.get("name") or ""))
        except ValueError as error:
            return self._error(str(error))
        try:
            await self._extras.revoke(name)
        except (KeyError, ValueError):
            return self._error(tr("tool_result.communicate.openai_token_missing", name=name))
        return ToolResult(
            tool_call_id="",
            content=tr(
                "tool_result.communicate.openai_revoked",
                name=name,
                participant=participant_id_for_token_name(name),
            ),
        )

    def _list_tokens(self) -> ToolResult:
        names = self._extras.names()
        if not names:
            return ToolResult(
                tool_call_id="",
                content=tr("tool_result.communicate.openai_token_list_empty"),
            )
        lines = [
            f"- {name}: {participant_id_for_token_name(name)}" for name in names
        ]
        return ToolResult(
            tool_call_id="",
            content=tr(
                "tool_result.communicate.openai_token_list",
                items="\n".join(lines),
            ),
        )

    async def _maybe_bind(self, extra: dict[str, Any], participant_id: str) -> str:
        person_id = str(extra.get("person_id") or "").strip()
        name = str(extra.get("person") or "").strip()
        if not person_id and not name:
            return tr("tool_result.communicate.openai_unbound")
        store = self._person_store
        if store is None:
            return tr("tool_result.communicate.openai_persona_disabled")
        created = False
        if person_id:
            person = store.get(person_id)
            if person is None:
                return tr("tool_result.persona.person_missing", person_id=person_id)
        else:
            person = store.find_by_name(name)
            if person is None:
                person = store.create(display_name=name)
                created = True
        store.bind_alias(
            person.person_id,
            PersonAlias(
                participant_id=participant_id,
                channel=self.name,
            ),
        )
        display = person.display_name or person.person_id
        if created:
            return tr(
                "tool_result.communicate.openai_bound_created",
                name=display,
                person_id=person.person_id,
                participant=participant_id,
            )
        return tr(
            "tool_result.communicate.openai_bound",
            name=display,
            person_id=person.person_id,
            participant=participant_id,
        )
