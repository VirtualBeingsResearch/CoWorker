from __future__ import annotations

import asyncio
import json
import re
import uuid
from collections.abc import Awaitable, Callable, Iterable
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from loguru import logger

from coworker.core.ids import new_compact_id
from coworker.core.types import CommunicateRegistration, CommunicateRequest, ToolResult
from coworker.i18n import tr
from coworker.tools.base import Tool, ToolDefinition

# 接受裸 participant_id（无前缀），返回规范化的完整 participant_id；若无法处理则返回 None
Checker = Callable[[str], "str | None"]
Sender = Callable[[CommunicateRequest], Awaitable[ToolResult]]
ConnectionListener = Callable[[], None]

_UNSAFE_OUTBOX_CHARS_RE = re.compile(r'[<>:"/\\|?*\x00-\x1f]+')
_SAFE_PARTICIPANT_CHARS_RE = re.compile(r"[^a-zA-Z0-9_.:-]+")


class ParticipantIdResolutionError(ValueError):
    """Raised when a shorthand participant ID cannot be resolved unambiguously."""


if TYPE_CHECKING:
    from coworker.core.tool_scope import ToolScope


class _BoundCommunicateTool(Tool):
    """A bubble-scoped communicator restricted to one pre-authorized recipient."""

    def __init__(
        self,
        delegate: CommunicateTool,
        participant_id: str,
        conversation_id: str = "",
        message_prefix: str = "",
        message_extra: dict[str, Any] | None = None,
    ) -> None:
        self._delegate = delegate
        self._participant_id = participant_id
        self._conversation_id = conversation_id
        self._message_prefix = message_prefix
        self._message_extra = dict(message_extra or {})

    @property
    def definition(self) -> ToolDefinition:
        base = self._delegate.definition
        properties = dict(base.parameters["properties"])
        properties["participant_id"] = {
            "type": "string",
            "description": "可选；此泡泡已固定绑定通信对象，不能改为其他对象。",
        }
        parameters = {
            **base.parameters,
            "properties": properties,
            "required": [
                name for name in base.parameters.get("required", []) if name != "participant_id"
            ],
        }
        conversation_note = f"，会话固定为 {self._conversation_id}" if self._conversation_id else ""
        return ToolDefinition(
            name=base.name,
            description=(
                f"向此泡泡绑定的通信对象发送消息（对象固定为 {self._participant_id}"
                f"{conversation_note}）。不得联系其他对象。"
            ),
            parameters=parameters,
        )

    async def execute(
        self,
        participant_id: str = "",
        message: str = "",
        conversation_id: str | None = None,
        attachments: list[dict[str, Any]] | None = None,
        extra: dict[str, Any] | None = None,
        **_,
    ) -> ToolResult:
        requested_participant = participant_id.strip() if isinstance(participant_id, str) else ""
        if requested_participant and requested_participant != self._participant_id:
            return ToolResult(
                tool_call_id="",
                content=tr("tool_result.communicate.bound_participant"),
                is_error=True,
            )

        requested_conversation = conversation_id.strip() if isinstance(conversation_id, str) else ""
        if (
            self._conversation_id
            and requested_conversation
            and (requested_conversation != self._conversation_id)
        ):
            return ToolResult(
                tool_call_id="",
                content=tr("tool_result.communicate.bound_conversation"),
                is_error=True,
            )

        outgoing_message = message
        if self._message_prefix:
            if outgoing_message:
                if not outgoing_message.startswith(self._message_prefix):
                    outgoing_message = f"{self._message_prefix}{outgoing_message}"
            elif attachments:
                outgoing_message = tr(
                    "tool_result.communicate.attachment_fallback",
                    prefix=self._message_prefix,
                )

        if extra is not None and not isinstance(extra, dict):
            return ToolResult(
                tool_call_id="",
                content=tr("tool_result.communicate.extra_object"),
                is_error=True,
            )
        outgoing_extra = dict(extra or {})
        outgoing_extra.update(self._message_extra)
        return await self._delegate.execute(
            participant_id=self._participant_id,
            message=outgoing_message,
            conversation_id=self._conversation_id or requested_conversation or None,
            attachments=attachments,
            extra=outgoing_extra,
        )


class CommunicateTool(Tool):
    def __init__(self, outbox_dir: str) -> None:
        self._outbox = Path(outbox_dir)
        self._registrations_path = self._outbox.parent / "communicate_registrations.json"
        self._ws_connections: dict[str, asyncio.Queue] = {}
        self._stream_transports: dict[str, str] = {}
        self._senders: dict[str, Sender] = {}
        self._checkers: dict[str, Checker] = {}
        self._extra_capable_senders: set[str] = set()
        self._connection_listeners: list[ConnectionListener] = []

    def add_connection_listener(self, listener: ConnectionListener) -> None:
        self._connection_listeners.append(listener)

    def fork(self, scope: ToolScope) -> Tool:
        participant_id = str(getattr(scope, "communicate_participant_id", "")).strip()
        if not participant_id:
            return self
        conversation_id = str(getattr(scope, "communicate_conversation_id", "")).strip()
        message_prefix = str(getattr(scope, "communicate_message_prefix", ""))
        message_extra = getattr(scope, "communicate_message_extra", {})
        return _BoundCommunicateTool(
            self,
            participant_id,
            conversation_id,
            message_prefix,
            message_extra if isinstance(message_extra, dict) else {},
        )

    def register_ws(
        self,
        participant_id: str,
        queue: asyncio.Queue,
        *,
        transport: str = "websocket",
    ) -> bool:
        """Register an outbound queue for a participant.

        The first live connection owns the participant_id. Later SSE/WS
        connections using the same id are rejected instead of replacing it.
        """
        if transport not in {"websocket", "sse"}:
            raise ValueError(f"unsupported stream transport: {transport}")
        if participant_id in self._ws_connections:
            return False
        self._ws_connections[participant_id] = queue
        self._stream_transports[participant_id] = transport
        self._notify_connection_listeners()
        return True

    def unregister_ws(self, participant_id: str, queue: asyncio.Queue | None = None) -> None:
        # 身份守卫：传了 queue 时，仅当注册表里当前就是这个 queue 才删除。
        # 防止 SSE 与 WS 用同一 participant_id 时互相误删对方的 queue，
        # 也修复 WS 旧连接断开误删新连接的潜在竞态。
        if queue is not None and self._ws_connections.get(participant_id) is not queue:
            return
        if participant_id not in self._ws_connections:
            return
        self._ws_connections.pop(participant_id, None)
        self._stream_transports.pop(participant_id, None)
        self._notify_connection_listeners()

    def outbound_queue(self, participant_id: str) -> asyncio.Queue | None:
        return self._ws_connections.get(participant_id)

    def live_stream_transport(self, participant_id: str) -> str | None:
        """Return the transport of a participant's current outbound reply stream."""
        return self._stream_transports.get(participant_id)

    def has_live_stream_connection(
        self,
        participant_id: str,
        *,
        transports: Iterable[str] | None = None,
    ) -> bool:
        """Whether a participant has a live matching WebSocket or SSE reply stream."""
        transport = self.live_stream_transport(participant_id)
        if transport is None:
            return False
        return transports is None or transport in set(transports)

    def register_sender(
        self,
        prefix: str,
        sender: Sender,
        checker: Checker | None = None,
        *,
        supports_extra: bool = False,
    ) -> None:
        """注册一个按 participant_id 前缀路由的发送器（如 wecom: → 企微 runner.sender）。
        可选传入 checker：当 participant_id 无前缀时，用于判断该信道能否处理并返回规范化 ID。
        supports_extra 表示该发送器能消费结构化 extra；默认关闭，避免系统元数据使纯文本信道拒绝消息。
        """
        self._senders[prefix] = sender
        if checker is not None:
            self._checkers[prefix] = checker
        if supports_extra:
            self._extra_capable_senders.add(prefix)
        else:
            self._extra_capable_senders.discard(prefix)

    def supports_message_extra(self, participant_id: str) -> bool:
        """Whether the participant's selected transport accepts structured ``extra``."""
        canonical_id, sender_prefix = self._resolve_participant_id(participant_id)
        if sender_prefix is not None:
            return sender_prefix in self._extra_capable_senders
        return canonical_id in self._ws_connections

    def resolve_participant_id(self, participant_id: str) -> str:
        """Expand a shorthand participant ID without sending a message.

        Full IDs and IDs that no checker recognizes are returned unchanged.  A
        shorthand that matches more than one channel is rejected in the same
        way as :meth:`execute`, so callers such as ``bubble_spawn`` cannot bind
        an ambiguous recipient.
        """
        canonical_id, _ = self._resolve_participant_id(participant_id)
        return canonical_id

    def _resolve_participant_id(self, participant_id: str) -> tuple[str, str | None]:
        for prefix in sorted(self._senders, key=len, reverse=True):
            if participant_id.startswith(prefix):
                return participant_id, prefix

        resolved: dict[str, str] = {}
        for prefix, checker in self._checkers.items():
            canonical = checker(participant_id)
            if canonical is not None:
                resolved[prefix] = canonical
        if len(resolved) == 1:
            prefix, canonical_id = next(iter(resolved.items()))
            return canonical_id, prefix
        if len(resolved) > 1:
            options = "\n".join(
                tr("tool_result.communicate.option", id=cid, prefix=p)
                for p, cid in resolved.items()
            )
            raise ParticipantIdResolutionError(
                tr(
                    "tool_result.communicate.ambiguous",
                    participant=participant_id,
                    options=options,
                )
            )
        return participant_id, None

    @property
    def definition(self) -> ToolDefinition:
        description = (
            "向指定通信对象发送消息。participant_id 表示对象；conversation_id "
            "表示同一对象下的某段会话。attachments 和 extra 的支持情况由目标信道决定。"
        )
        return ToolDefinition(
            name="communicate",
            description=description,
            parameters={
                "type": "object",
                "properties": {
                    "participant_id": {"type": "string", "description": "接收方的 ID"},
                    "message": {"type": "string", "description": "要发送的消息内容"},
                    "conversation_id": {
                        "type": "string",
                        "description": "同一 participant_id 下的会话 ID。",
                    },
                    "attachments": {
                        "type": "array",
                        "description": "可选附件；具体支持情况由目标信道决定。",
                        "items": {
                            "type": "object",
                            "properties": {
                                "type": {
                                    "type": "string",
                                    "description": "附件类型：image / file",
                                    "enum": ["image", "file"],
                                },
                                "filename": {
                                    "type": "string",
                                    "description": "可选展示文件名；默认使用 path 的文件名",
                                },
                                "media_type": {
                                    "type": "string",
                                    "description": "可选 MIME 类型；默认按文件扩展名推断",
                                },
                                "path": {
                                    "type": "string",
                                    "description": "本地文件绝对或相对路径",
                                },
                            },
                            "required": ["path"],
                        },
                    },
                    "extra": {
                        "type": "object",
                        "description": "低频信道扩展；具体白名单由目标信道决定。",
                    },
                },
                "required": ["participant_id"],
            },
        )

    def list_connected(self) -> list[str]:
        return list(self._ws_connections.keys())

    def _notify_connection_listeners(self) -> None:
        for listener in list(self._connection_listeners):
            try:
                listener()
            except Exception as error:
                logger.warning(f"Communicate connection listener raised, ignored: {error}")

    def register_participant(
        self,
        *,
        kind: str,
        client_id: str,
        display_name: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        kind = kind.strip()
        client_id = client_id.strip()
        if not kind:
            raise ValueError("kind is required")
        if not client_id:
            raise ValueError("client_id is required")

        registrations = self._load_registrations()
        now = datetime.now().isoformat()
        reusable = next(
            (
                item
                for item in registrations
                if item.kind == kind
                and item.client_id == client_id
                and item.participant_id not in self._ws_connections
            ),
            None,
        )
        if reusable is not None:
            reusable.display_name = display_name or reusable.display_name
            reusable.last_registered_at = now
            reusable.metadata = metadata or reusable.metadata
            self._save_registrations(registrations)
            return reusable.to_dict(active=False)

        registration = CommunicateRegistration(
            registration_id=uuid.uuid4().hex,
            participant_id=self._next_participant_id(kind, client_id, registrations),
            kind=kind,
            client_id=client_id,
            display_name=display_name or client_id,
            created_at=now,
            last_registered_at=now,
            metadata=metadata or {},
        )
        registrations.append(registration)
        self._save_registrations(registrations)
        return registration.to_dict(active=False)

    def list_registrations(self) -> list[dict[str, Any]]:
        return [
            item.to_dict(active=item.participant_id in self._ws_connections)
            for item in self._load_registrations()
        ]

    def registration_records(self) -> list[CommunicateRegistration]:
        return self._load_registrations()

    def delete_registration(self, registration_id: str) -> dict[str, Any]:
        registrations = self._load_registrations()
        for index, item in enumerate(registrations):
            if item.registration_id != registration_id:
                continue
            if item.participant_id in self._ws_connections:
                raise RuntimeError("registration is active; stop the connection before deleting it")
            removed = registrations.pop(index)
            self._save_registrations(registrations)
            return removed.to_dict(active=False)
        raise KeyError(registration_id)

    def _next_participant_id(
        self,
        kind: str,
        client_id: str,
        registrations: list[CommunicateRegistration],
    ) -> str:
        used = {item.participant_id for item in registrations}
        prefix = _SAFE_PARTICIPANT_CHARS_RE.sub("-", kind).strip(".:-") or "unknown"
        client_segment = _SAFE_PARTICIPANT_CHARS_RE.sub("-", client_id).strip(".:-") or "unknown"
        if prefix == "coworker-desktop":
            # Desktop metadata already stores desktop_id/coworker_id.  Keep only
            # the actor segment required by handoff matching instead of copying
            # the full client_id into every model-visible participant ID.
            client_parts = client_segment.split(":")
            actor_segment = client_parts[1] if len(client_parts) > 1 else "unknown"
            base = f"{prefix}:d:{actor_segment}"
        else:
            base = prefix
        while True:
            candidate = f"{base}:{new_compact_id(entropy_bytes=6)}"
            if candidate not in used and candidate not in self._ws_connections:
                return candidate

    def _load_registrations(self) -> list[CommunicateRegistration]:
        try:
            data = json.loads(self._registrations_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return []
        except Exception as error:
            logger.warning(f"Failed to read communicate registrations: {error}")
            return []
        if not isinstance(data, list):
            return []
        return [CommunicateRegistration.from_dict(item) for item in data if isinstance(item, dict)]

    def _save_registrations(self, registrations: list[CommunicateRegistration]) -> None:
        self._registrations_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._registrations_path.with_name(f".{self._registrations_path.name}.tmp")
        tmp.write_text(
            json.dumps(
                [item.to_dict(active=False) for item in registrations if item.participant_id],
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        tmp.replace(self._registrations_path)

    async def execute(
        self,
        participant_id: str,
        message: str = "",
        conversation_id: str | None = None,
        attachments: list[dict[str, Any]] | None = None,
        extra: dict[str, Any] | None = None,
        **_,
    ) -> ToolResult:
        attachments = attachments or []
        extra = extra or {}
        if not isinstance(extra, dict):
            return ToolResult(
                tool_call_id="",
                content=tr("tool_result.communicate.extra_object"),
                is_error=True,
            )

        try:
            participant_id, sender_prefix = self._resolve_participant_id(participant_id)
        except ParticipantIdResolutionError as error:
            return ToolResult(tool_call_id="", content=str(error), is_error=True)

        request = CommunicateRequest(
            participant_id=participant_id,
            message=message,
            conversation_id=conversation_id,
            attachments=attachments,
            extra=extra,
        )
        if sender_prefix is not None:
            return await self._senders[sender_prefix](request)

        try:
            if participant_id in self._ws_connections:
                await self._ws_connections[participant_id].put(request)
                return ToolResult(
                    tool_call_id="",
                    content=tr(
                        "tool_result.communicate.websocket_sent",
                        participant=participant_id,
                    ),
                )
            if conversation_id:
                return ToolResult(
                    tool_call_id="",
                    content=tr("tool_result.communicate.conversation_unsupported"),
                    is_error=True,
                )
            if extra:
                return ToolResult(
                    tool_call_id="",
                    content=tr("tool_result.communicate.extra_unsupported"),
                    is_error=True,
                )
            if attachments:
                return ToolResult(
                    tool_call_id="",
                    content=tr("tool_result.communicate.attachments_unsupported"),
                    is_error=True,
                )
            if not message:
                return ToolResult(
                    tool_call_id="",
                    content=tr("tool_result.communicate.message_empty"),
                    is_error=True,
                )

            self._outbox.mkdir(parents=True, exist_ok=True)
            ts = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
            safe_participant_id = (
                _UNSAFE_OUTBOX_CHARS_RE.sub("-", participant_id).strip(" .-") or "unknown"
            )
            out_file = self._outbox / f"{ts}_{safe_participant_id}.md"
            out_file.write_text(message, encoding="utf-8")

            logger.debug(f"No active WS for {participant_id}, message written to outbox only")

            return ToolResult(
                tool_call_id="",
                content=tr("tool_result.communicate.fallback_saved", path=out_file),
            )
        except Exception as e:
            return ToolResult(
                tool_call_id="",
                content=tr("tool_result.communicate.failed", error=e),
                is_error=True,
            )


class ListWSConnectionsTool(Tool):
    def __init__(self, communicate: CommunicateTool) -> None:
        self._communicate = communicate

    @property
    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="list_ws_connections",
            description="列出当前通过 WebSocket 连接的所有参与者 ID",
            parameters={
                "type": "object",
                "properties": {},
                "required": [],
            },
        )

    async def execute(self, **_) -> ToolResult:
        connected = self._communicate.list_connected()
        if not connected:
            return ToolResult(tool_call_id="", content=tr("tool_result.communicate.no_websocket"))
        return ToolResult(tool_call_id="", content="\n".join(connected))
