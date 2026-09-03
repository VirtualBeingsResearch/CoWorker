"""OpenAI-compatible HTTP channel: extra tokens, waiters, and control."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import re
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from coworker.channels.base import (
    BaseChannel,
    ChannelCapabilities,
    ConnectionInfo,
)
from coworker.channels.detail_store import DetailStore, _safe
from coworker.channels.inbound import AttachmentStore
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
from coworker.core.types import AttachmentData, CommunicateRequest, IncomingEvent, ToolResult
from coworker.i18n import tr
from coworker.memory.short_term import ShortTermMemory
from coworker.persona import PersonAlias, PersonStore

if TYPE_CHECKING:
    from coworker.channels.runtime import ChannelRuntime

_END_TURN_KEYS = frozenset({"end_turn", "endTurn"})
_MAX_IMAGE_COUNT = 5
_MAX_IMAGE_BYTES = 20 * 1024 * 1024
_SUPPORTED_IMAGE_MEDIA_TYPES = frozenset(
    {"image/jpeg", "image/png", "image/gif", "image/webp"}
)
_DATA_URL_RE = re.compile(
    r"^data:(image/(?:jpeg|png|gif|webp))(?:;charset=[^;]+)?;base64,(.+)$",
    re.IGNORECASE | re.DOTALL,
)
_IMAGE_EXTENSIONS = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/gif": ".gif",
    "image/webp": ".webp",
}
# Fold per-request system / tools past this size so pinned content stays cheap.
_FOLD_THRESHOLD = 1500
_FOLD_HEAD_CHARS = 400
_PROMPT_STATE_LIMIT = 200
_ORIGIN_LIMIT = 200
_SLICE_CACHE_LIMIT = 64
_PIN_DIGEST_LEN = 16
_LABEL_DIGEST_LEN = 12


@dataclass
class _WindowPromptState:
    """Digests and pointer pin for one conversation window."""

    system_digest: str | None = None
    tools_digest: str | None = None
    pointer_pin_id: str | None = None


@dataclass
class _PromptSlice:
    """One hashed system/tools blob shared by any matching window."""

    pin_id: str
    section: str
    holders: set[tuple[str, str]] = field(default_factory=set)


def _fold_key(kind: str, text: str) -> str:
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]
    return f"{kind}-{digest}"


def _text_digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _head_lines(text: str, limit: int) -> str:
    """Leading slice of ``text`` within ``limit`` chars (whole lines preferred)."""
    kept: list[str] = []
    total = 0
    for line in text.split("\n"):
        addition = len(line) + 1
        if total + addition > limit:
            remaining = limit - total
            if remaining > 1:
                kept.append(line[: remaining - 1].rstrip() + "…")
            break
        kept.append(line)
        total += addition
    return "\n".join(kept)



def fingerprint_conversation(messages: list[dict[str, Any]]) -> str:
    """Window id from originating system plus every user message in this snapshot.

    The HTTP resolver remembers the first request's user list so later turns of
    the same chat keep this id instead of hashing newly appended user messages.
    """

    return _identity_fingerprint(
        _system_fingerprint_text(messages),
        _user_fingerprint_texts(messages),
    )


def _identity_fingerprint(system: str, users: list[str]) -> str:
    joined_users = "\n---\n".join(_canonical(text) for text in users)
    payload = f"{_canonical(system)}\n---\n{joined_users}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def _system_fingerprint_text(messages: list[dict[str, Any]]) -> str:
    for item in messages:
        if str(item.get("role") or "") == "system":
            return _message_fingerprint_text(item)
    return ""


def _user_fingerprint_texts(messages: list[dict[str, Any]]) -> list[str]:
    return [
        _message_fingerprint_text(item)
        for item in messages
        if str(item.get("role") or "") == "user"
    ]


def _role_after_origin_users(
    messages: list[dict[str, Any]],
    origin_users: tuple[str, ...],
) -> str | None:
    """Role after the first-request users, or None if the history ends there.

    ``__mismatch__`` means ``origin_users`` is not a prefix of this history.
    Few-shot assistant/tool messages between those users are skipped.
    """

    remaining = list(origin_users)
    for item in messages:
        role = str(item.get("role") or "")
        if role in {"", "system"}:
            continue
        if remaining:
            if role == "user":
                if _message_fingerprint_text(item) != remaining[0]:
                    return "__mismatch__"
                remaining.pop(0)
                continue
            if role in {"assistant", "tool"}:
                continue
            return "__mismatch__"
        return role
    if remaining:
        return "__mismatch__"
    return None


def last_user_text(messages: list[dict[str, Any]]) -> str:
    for item in reversed(messages):
        if str(item.get("role") or "") == "user":
            return _message_text(item)
    return ""


def turn_user_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """User messages that belong to the current inbound turn.

    After any assistant/tool history, only the trailing user messages are new.
    On the first request (no assistant/tool yet), every user message is new.
    Clients such as ZCode often put harness context in extra opening user
    messages before the actual question.
    """
    start = 0
    for index, item in enumerate(messages):
        role = str(item.get("role") or "")
        if role in {"assistant", "tool"}:
            start = index + 1
    return [
        item
        for item in messages[start:]
        if str(item.get("role") or "") == "user"
    ]


def turn_user_text(messages: list[dict[str, Any]]) -> str:
    texts = [_message_text(item) for item in turn_user_messages(messages)]
    return "\n\n".join(text for text in texts if text)


def first_system_text(messages: list[dict[str, Any]]) -> str:
    for item in messages:
        if str(item.get("role") or "") == "system":
            return _message_text(item)
    return ""


def last_user_message(messages: list[dict[str, Any]]) -> dict[str, Any] | None:
    for item in reversed(messages):
        if str(item.get("role") or "") == "user":
            return item
    return None


def last_user_image_attachments(messages: list[dict[str, Any]]) -> list[dict[str, str]]:
    """Parse data-URL images from the latest user message."""

    message = last_user_message(messages)
    if message is None:
        return []
    return image_attachments_from_message(message)


def turn_user_image_attachments(messages: list[dict[str, Any]]) -> list[dict[str, str]]:
    """Parse data-URL images from every user message in the current turn."""

    return image_attachments_from_messages(turn_user_messages(messages))


def image_attachments_from_message(
    message: dict[str, Any],
    *,
    start_index: int = 1,
) -> list[dict[str, str]]:
    """Parse data-URL images from one OpenAI message (user or tool)."""

    attachments: list[dict[str, str]] = []
    content = message.get("content")
    if isinstance(content, str):
        url = content.strip()
        if url.lower().startswith("data:image/"):
            attachments.append(_data_url_attachment(url, index=start_index))
        return attachments
    if not isinstance(content, list):
        return attachments
    for block in content:
        if not isinstance(block, dict):
            continue
        image_url = _image_url_from_block(block)
        if image_url is None:
            continue
        attachments.append(
            _data_url_attachment(image_url, index=start_index + len(attachments))
        )
        if len(attachments) > _MAX_IMAGE_COUNT:
            raise ValueError(
                tr("api.attachment.count_exceeded", limit=_MAX_IMAGE_COUNT)
            )
    return attachments


def image_attachments_from_messages(
    messages: list[dict[str, Any]],
) -> list[dict[str, str]]:
    attachments: list[dict[str, str]] = []
    for message in messages:
        attachments.extend(
            image_attachments_from_message(
                message, start_index=len(attachments) + 1
            )
        )
        if len(attachments) > _MAX_IMAGE_COUNT:
            raise ValueError(
                tr("api.attachment.count_exceeded", limit=_MAX_IMAGE_COUNT)
            )
    return attachments


def tool_call_ids_from_messages(messages: list[dict[str, Any]]) -> set[str]:
    """Collect assistant ``tool_calls[].id`` and tool ``tool_call_id`` values."""

    ids: set[str] = set()
    for item in messages:
        call_id = str(item.get("tool_call_id") or "").strip()
        if call_id:
            ids.add(call_id)
        raw_calls = item.get("tool_calls")
        if not isinstance(raw_calls, list):
            continue
        for call in raw_calls:
            if not isinstance(call, dict):
                continue
            found = str(call.get("id") or "").strip()
            if found:
                ids.add(found)
    return ids


def _message_text(message: dict[str, Any]) -> str:
    content = message.get("content")
    if isinstance(content, str):
        text = content.strip()
        if _DATA_URL_RE.match(text):
            return ""
        return text
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "text":
                parts.append(str(block.get("text") or ""))
            elif "text" in block and block.get("type") != "image_url":
                parts.append(str(block.get("text") or ""))
        return "\n".join(part for part in parts if part).strip()
    return ""


def _message_fingerprint_text(message: dict[str, Any]) -> str:
    text = _message_text(message)
    content = message.get("content")
    if isinstance(content, str):
        url = content.strip()
        if url.lower().startswith("data:image/"):
            return f"[image:{hashlib.sha256(url.encode('utf-8')).hexdigest()[:12]}]"
        return text
    if not isinstance(content, list):
        return text
    markers: list[str] = []
    for block in content:
        if not isinstance(block, dict):
            continue
        image_url = _image_url_from_block(block)
        if image_url is None:
            continue
        markers.append(
            f"[image:{hashlib.sha256(image_url.encode('utf-8')).hexdigest()[:12]}]"
        )
    if not markers:
        return text
    return "\n".join(part for part in [text, *markers] if part)


def _image_url_from_block(block: dict[str, Any]) -> str | None:
    block_type = str(block.get("type") or "")
    if block_type == "image_url":
        image_url = block.get("image_url")
        if isinstance(image_url, str):
            url = image_url.strip()
            return url or None
        if isinstance(image_url, dict):
            url = str(image_url.get("url") or "").strip()
            return url or None
        return None
    if block_type == "image":
        source = block.get("source")
        if not isinstance(source, dict):
            return None
        if str(source.get("type") or "") != "base64":
            return None
        media_type = str(source.get("media_type") or "").strip().lower()
        data = re.sub(r"\s+", "", str(source.get("data") or ""))
        if not media_type or not data:
            return None
        return f"data:{media_type};base64,{data}"
    return None


def _data_url_attachment(url: str, *, index: int) -> dict[str, str]:
    match = _DATA_URL_RE.match(url.strip())
    if match is None:
        raise ValueError(tr("api.openai.image_data_url_required"))
    media_type = match.group(1).lower()
    if media_type not in _SUPPORTED_IMAGE_MEDIA_TYPES:
        raise ValueError(
            tr("api.openai.image_unsupported_media", media_type=media_type)
        )
    encoded = re.sub(r"\s+", "", match.group(2))
    try:
        raw = base64.b64decode(encoded, validate=True)
    except Exception as error:
        raise ValueError(tr("api.openai.image_invalid_data")) from error
    if len(raw) > _MAX_IMAGE_BYTES:
        raise ValueError(
            tr(
                "api.attachment.too_large",
                name=f"image-{index}",
                size=len(raw),
                limit=_MAX_IMAGE_BYTES,
            )
        )
    extension = _IMAGE_EXTENSIONS[media_type]
    return {
        "filename": f"image-{index}{extension}",
        "media_type": media_type,
        "data": encoded,
    }


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
        attachments_dir: str | Path | None = None,
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
        if attachments_dir is not None:
            root = Path(attachments_dir)
            detail_root = root.parent / "openai"
        else:
            root = Path("data") / "attachments"
            detail_root = Path("data") / "openai"
        self._attachments = AttachmentStore(root)
        self._details = DetailStore(detail_root)
        self._prompt_state: OrderedDict[tuple[str, str], _WindowPromptState] = (
            OrderedDict()
        )
        self._slices: OrderedDict[tuple[str, str], _PromptSlice] = OrderedDict()
        self._origins: OrderedDict[tuple[str, str], tuple[str, ...]] = OrderedDict()
        self._short_term: ShortTermMemory | None = None

    def set_person_store(self, store: PersonStore | None) -> None:
        self._person_store = store

    def set_short_term(self, short_term: ShortTermMemory | None) -> None:
        self._short_term = short_term

    def set_native_tool_names(self, names: set[str]) -> None:
        self._native_tool_names = set(names)

    def materialize_user_images(
        self, messages: list[dict[str, Any]]
    ) -> list[AttachmentData]:
        return self.materialize_image_dicts(turn_user_image_attachments(messages))

    def materialize_image_dicts(
        self, items: list[dict[str, str]]
    ) -> list[AttachmentData]:
        return [
            self._attachments.save(item, keep_inline_data=True) for item in items
        ]

    def resolve_implicit_conversation_id(
        self,
        participant_id: str,
        messages: list[dict[str, Any]],
    ) -> str:
        """Pick a window id when the client omitted conversation_id.

        The first request hashes system plus every user message in that
        snapshot. Later turns of the same chat keep that id: newly appended
        user messages are not folded into the fingerprint.
        """
        users = _user_fingerprint_texts(messages)
        for turn in self._sessions.all_active():
            if turn.participant_id != participant_id:
                continue
            if self._request_belongs_to_turn(messages, turn):
                return turn.conversation_id
        matched = self._longest_origin_match(participant_id, users, messages)
        if matched is not None:
            key = (participant_id, matched)
            if key in self._origins:
                self._origins.move_to_end(key)
            return matched
        conversation_id = _identity_fingerprint(
            _system_fingerprint_text(messages),
            users,
        )
        self._remember_origin(participant_id, conversation_id, users)
        return conversation_id

    def _request_belongs_to_turn(
        self,
        messages: list[dict[str, Any]],
        turn: OpenAITurn,
    ) -> bool:
        pending_ids = {item.openai_id for item in turn.pending_calls()}
        if pending_ids:
            return bool(pending_ids & tool_call_ids_from_messages(messages))
        if not turn.in_flight:
            return False
        origin = self._origins.get((turn.participant_id, turn.conversation_id))
        if origin is None:
            return False
        return tuple(_user_fingerprint_texts(messages)) == origin

    def _longest_origin_match(
        self,
        participant_id: str,
        users: list[str],
        messages: list[dict[str, Any]],
    ) -> str | None:
        users_t = tuple(users)
        best_len = -1
        best_id: str | None = None
        for (pid, conversation_id), origin in self._origins.items():
            if pid != participant_id or users_t[: len(origin)] != origin:
                continue
            after = _role_after_origin_users(messages, origin)
            if after == "__mismatch__":
                continue
            if after is not None and after not in {"assistant", "tool"}:
                continue
            if len(origin) > best_len:
                best_len = len(origin)
                best_id = conversation_id
        return best_id

    def _remember_origin(
        self,
        participant_id: str,
        conversation_id: str,
        users: list[str],
    ) -> None:
        key = (participant_id, conversation_id)
        self._origins[key] = tuple(users)
        self._origins.move_to_end(key)
        while len(self._origins) > _ORIGIN_LIMIT:
            self._origins.popitem(last=False)

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
        return ChannelCapabilities(conversation_id=True, extra=True)

    def agent_instructions(self) -> str:
        return tr("prompt.channel.openai")

    @staticmethod
    def _error(content: str) -> ToolResult:
        return ToolResult(tool_call_id="", content=content, is_error=True)

    @staticmethod
    def _wants_end_turn(extra: dict[str, Any]) -> bool:
        for key in _END_TURN_KEYS:
            if key not in extra:
                continue
            value = extra[key]
            if value is True or value == 1:
                return True
            if isinstance(value, str) and value.strip().lower() in {"1", "true", "yes"}:
                return True
        return False

    async def send(self, request: CommunicateRequest) -> ToolResult:
        if request.participant_id == CONTROL_PARTICIPANT_ID:
            return await self._control(request.extra)
        conversation_id = (request.conversation_id or "").strip()
        if not conversation_id:
            active = self._sessions.in_flight_for(request.participant_id)
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
        end_turn = self._wants_end_turn(request.extra)
        if end_turn:
            if not turn.fulfill_stop(request.message):
                return self._error(tr("tool_result.communicate.openai_late"))
            self._record_sent(request.participant_id)
            return ToolResult(
                tool_call_id="",
                content=tr(
                    "tool_result.communicate.openai_ended",
                    participant=request.participant_id,
                    conversation=conversation_id,
                ),
            )
        if not turn.push_message(request.message):
            return self._error(tr("tool_result.communicate.openai_late"))
        self._record_sent(request.participant_id)
        return ToolResult(
            tool_call_id="",
            content=tr(
                "tool_result.communicate.openai_updated",
                participant=request.participant_id,
                conversation=conversation_id,
            ),
        )

    async def start_user_turn(
        self,
        *,
        participant_id: str,
        conversation_id: str,
        user_text: str,
        system_text: str,
        catalog: dict[str, dict[str, Any]],
        attachments: list[AttachmentData] | None = None,
        stream: bool = False,
    ) -> OpenAITurn:
        if self._sessions.awaiting_tools(participant_id, conversation_id):
            raise BusyError("tools")
        turn = OpenAITurn(
            participant_id=participant_id,
            conversation_id=conversation_id,
            catalog=catalog,
            timeout_seconds=self.timeout_seconds,
            stream=stream,
        )
        try:
            self._sessions.begin_user_turn(turn)
        except BusyError:
            raise
        content = self._inbound_body(
            participant_id=participant_id,
            conversation_id=conversation_id,
            user_text=user_text,
            system_text=system_text,
            catalog=catalog,
        )
        await self.publish_inbound(
            IncomingEvent(
                participant_id=participant_id,
                conversation_id=conversation_id,
                content=content,
                source="openai",
                attachments=list(attachments or ()),
            )
        )
        self.record_received(participant_id)
        return turn

    async def open_user_turn(
        self,
        *,
        participant_id: str,
        conversation_id: str,
        user_text: str,
        system_text: str,
        catalog: dict[str, dict[str, Any]],
        attachments: list[AttachmentData] | None = None,
    ) -> OpenAICompletion:
        turn = await self.start_user_turn(
            participant_id=participant_id,
            conversation_id=conversation_id,
            user_text=user_text,
            system_text=system_text,
            catalog=catalog,
            attachments=attachments,
            stream=False,
        )
        return await self._await_turn(turn)

    def settle_turn(self, turn: OpenAITurn) -> OpenAICompletion:
        """Apply session bookkeeping after a turn's completion Future is done."""
        if not turn.completion.done():
            turn.expire()
        completion = turn.completion.result()
        if completion.kind == "tool_calls" and not completion.timed_out:
            self._sessions.mark_awaiting_tools(turn)
        else:
            self._finish_http_turn(turn)
        return completion

    async def _await_turn(self, turn: OpenAITurn) -> OpenAICompletion:
        try:
            await asyncio.wait_for(turn.completion, timeout=self.timeout_seconds)
        except TimeoutError:
            turn.expire()
            self._finish_http_turn(turn)
            return OpenAICompletion(kind="stop", content="", timed_out=True)
        except Exception:
            self._finish_http_turn(turn)
            raise
        return self.settle_turn(turn)

    def _finish_http_turn(self, turn: OpenAITurn) -> None:
        self._sessions.discard(turn)
        self._release_window_pins(turn.participant_id, turn.conversation_id)

    async def start_tool_followup(
        self,
        *,
        participant_id: str,
        conversation_id: str,
        results: dict[str, str],
        stream: bool = False,
        attachments: list[AttachmentData] | None = None,
    ) -> OpenAITurn:
        pending = self._sessions.pending_tool_turn(participant_id, conversation_id)
        if pending is None:
            raise ValueError(tr("api.openai.tool_followup_unexpected"))
        body = self._tool_results_body(pending, results)
        followup = OpenAITurn(
            participant_id=participant_id,
            conversation_id=conversation_id,
            catalog=pending.catalog,
            timeout_seconds=self.timeout_seconds,
            stream=stream,
        )
        self._sessions.begin_tool_followup(followup, results)
        await self.publish_inbound(
            IncomingEvent(
                participant_id=participant_id,
                conversation_id=conversation_id,
                content=body,
                source="openai",
                attachments=list(attachments or ()),
            )
        )
        self.record_received(participant_id)
        return followup

    async def open_tool_followup(
        self,
        *,
        participant_id: str,
        conversation_id: str,
        results: dict[str, str],
        attachments: list[AttachmentData] | None = None,
    ) -> OpenAICompletion:
        followup = await self.start_tool_followup(
            participant_id=participant_id,
            conversation_id=conversation_id,
            results=results,
            stream=False,
            attachments=attachments,
        )
        return await self._await_turn(followup)

    def prepare_client_tool_batch(
        self,
        participant_id: str,
        conversation_id: str,
        count: int,
    ) -> None:
        resolved = conversation_id
        if not resolved:
            active = self._sessions.in_flight_for(participant_id)
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
            active = self._sessions.in_flight_for(participant_id)
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
        participant_id: str,
        conversation_id: str,
        user_text: str,
        system_text: str,
        catalog: dict[str, dict[str, Any]],
    ) -> str:
        state = self._prompt_state_for(participant_id, conversation_id)
        system_digest = _text_digest(system_text) if system_text else None
        self._sync_prompt_slice(
            participant_id=participant_id,
            conversation_id=conversation_id,
            kind="system",
            digest=system_digest,
            previous_digest=state.system_digest,
            render=lambda: self._system_section(system_text=system_text),
        )
        state.system_digest = system_digest
        tools_rendered = (
            json.dumps(list(catalog.values()), ensure_ascii=False, indent=2)
            if catalog
            else ""
        )
        tools_digest = _text_digest(tools_rendered) if tools_rendered else None
        self._sync_prompt_slice(
            participant_id=participant_id,
            conversation_id=conversation_id,
            kind="tools",
            digest=tools_digest,
            previous_digest=state.tools_digest,
            render=lambda: self._tools_section(
                catalog=catalog,
                rendered=tools_rendered,
            ),
        )
        state.tools_digest = tools_digest
        self._sync_pointer_pin(participant_id, conversation_id, state)
        return user_text

    def _prompt_state_for(
        self, participant_id: str, conversation_id: str
    ) -> _WindowPromptState:
        key = (participant_id, conversation_id)
        state = self._prompt_state.get(key)
        if state is None:
            state = _WindowPromptState()
            self._prompt_state[key] = state
        else:
            self._prompt_state.move_to_end(key)
        while len(self._prompt_state) > _PROMPT_STATE_LIMIT:
            (old_participant, old_conversation), old_state = (
                self._prompt_state.popitem(last=False)
            )
            self._drop_window_holds(old_participant, old_conversation, old_state)
        return state

    def _content_pin_id(self, kind: str, digest: str) -> str:
        return f"openai-req:{kind}:{digest[:_PIN_DIGEST_LEN]}"

    def _sync_prompt_slice(
        self,
        *,
        participant_id: str,
        conversation_id: str,
        kind: Literal["system", "tools"],
        digest: str | None,
        previous_digest: str | None,
        render: Callable[[], str],
    ) -> None:
        """Attach or share a content-addressed system/tools pin.

        Short-term already skips reinjecting a pin_id that is still in
        primary, so this only tracks holders and pins new digests.
        """
        holder = (participant_id, conversation_id)
        if previous_digest and previous_digest != digest:
            self._release_slice_hold(holder, kind, previous_digest)
        if not digest:
            return
        key = (kind, digest)
        slice_ = self._slices.get(key)
        if slice_ is not None and holder in slice_.holders:
            self._slices.move_to_end(key)
            return
        if slice_ is None:
            slice_ = _PromptSlice(
                pin_id=self._content_pin_id(kind, digest),
                section=render(),
            )
            self._slices[key] = slice_
        else:
            self._slices.move_to_end(key)
        already_live = bool(slice_.holders)
        slice_.holders.add(holder)
        self._evict_idle_slices()
        if already_live:
            return
        self._pin_slice(kind, slice_, digest[:_LABEL_DIGEST_LEN])

    def _pin_slice(
        self,
        kind: Literal["system", "tools"],
        slice_: _PromptSlice,
        digest: str,
    ) -> None:
        short_term = self._short_term
        if short_term is None:
            return
        label_key = (
            "channel.openai.pin_system_label"
            if kind == "system"
            else "channel.openai.pin_tools_label"
        )
        short_term.pin(
            slice_.pin_id,
            tr(label_key, digest=digest),
            slice_.section,
            system_managed=True,
        )

    def _pointer_pin_id(
        self,
        participant_id: str,
        conversation_id: str,
        system_digest: str | None,
        tools_digest: str | None,
    ) -> str:
        sys_part = (system_digest or "none")[:8]
        tools_part = (tools_digest or "none")[:8]
        return (
            f"openai-ptr:{_safe(participant_id)}:"
            f"{_safe(conversation_id)}:{sys_part}:{tools_part}"
        )

    def _sync_pointer_pin(
        self,
        participant_id: str,
        conversation_id: str,
        state: _WindowPromptState,
    ) -> None:
        """Pin a per-window pointer at the shared system/tools digests."""
        if self._short_term is None:
            return
        if not state.system_digest and not state.tools_digest:
            self._unpin_pin_ids(state.pointer_pin_id)
            state.pointer_pin_id = None
            return
        pin_id = self._pointer_pin_id(
            participant_id,
            conversation_id,
            state.system_digest,
            state.tools_digest,
        )
        if state.pointer_pin_id and state.pointer_pin_id != pin_id:
            self._unpin_pin_ids(state.pointer_pin_id)
        absent = tr("channel.openai.pin_absent")
        system_digest = (
            state.system_digest[:_LABEL_DIGEST_LEN]
            if state.system_digest
            else absent
        )
        tools_digest = (
            state.tools_digest[:_LABEL_DIGEST_LEN]
            if state.tools_digest
            else absent
        )
        self._short_term.pin(
            pin_id,
            tr(
                "channel.openai.pin_pointer_label",
                participant=participant_id,
                conversation=conversation_id,
            ),
            tr(
                "channel.openai.pin_pointer",
                system_digest=system_digest,
                tools_digest=tools_digest,
            ),
            system_managed=True,
        )
        state.pointer_pin_id = pin_id

    def _release_slice_hold(
        self,
        holder: tuple[str, str],
        kind: str,
        digest: str,
    ) -> None:
        key = (kind, digest)
        slice_ = self._slices.get(key)
        if slice_ is None:
            return
        slice_.holders.discard(holder)
        if not slice_.holders:
            self._unpin_pin_ids(slice_.pin_id)

    def _drop_window_holds(
        self,
        participant_id: str,
        conversation_id: str,
        state: _WindowPromptState,
    ) -> None:
        holder = (participant_id, conversation_id)
        self._unpin_pin_ids(state.pointer_pin_id)
        state.pointer_pin_id = None
        if state.system_digest:
            self._release_slice_hold(holder, "system", state.system_digest)
        if state.tools_digest:
            self._release_slice_hold(holder, "tools", state.tools_digest)

    def _evict_idle_slices(self) -> None:
        idle_keys = [key for key, slice_ in self._slices.items() if not slice_.holders]
        excess = len(idle_keys) - _SLICE_CACHE_LIMIT
        if excess <= 0:
            return
        for key in idle_keys[:excess]:
            self._slices.pop(key, None)

    def _release_window_pins(self, participant_id: str, conversation_id: str) -> None:
        """Drop this window's hold after the HTTP turn ends, times out, or is discarded."""
        state = self._prompt_state.pop((participant_id, conversation_id), None)
        if state is None:
            return
        self._drop_window_holds(participant_id, conversation_id, state)

    def _unpin_pin_ids(self, *pin_ids: str | None) -> None:
        short_term = self._short_term
        if short_term is None:
            return
        for pin_id in pin_ids:
            if pin_id:
                short_term.unpin(pin_id)

    def _system_section(self, *, system_text: str) -> str:
        if len(system_text) <= _FOLD_THRESHOLD:
            return tr("channel.openai.request_system", text=system_text)
        path = self._details.write_detail(_fold_key("system", system_text), system_text)
        head = _head_lines(system_text, _FOLD_HEAD_CHARS)
        return tr(
            "channel.openai.request_system_folded",
            text=head,
            path=str(path),
        )

    def _tools_section(
        self,
        *,
        catalog: dict[str, dict[str, Any]],
        rendered: str,
    ) -> str:
        if len(rendered) <= _FOLD_THRESHOLD:
            return tr("channel.openai.client_tools", tools=rendered)
        path = self._details.write_detail(_fold_key("tools", rendered), rendered)
        names = ", ".join(catalog.keys())
        return tr(
            "channel.openai.client_tools_folded",
            names=names,
            path=str(path),
        )

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
