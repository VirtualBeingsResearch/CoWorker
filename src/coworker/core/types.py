from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal

from coworker.core.token_utils import estimate_content_tokens, estimate_text_tokens

__all__ = ["estimate_content_tokens", "estimate_text_tokens"]


@dataclass
class AttachmentData:
    filename: str
    media_type: str
    saved_path: str
    data: str | None = None  # base64, only for image/pdf content blocks


@dataclass
class Message:
    role: Literal["user", "assistant", "system", "tool"]
    content: str | list[dict[str, Any]]
    timestamp: datetime = field(default_factory=lambda:datetime.now())
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    tool_call_id: str | None = None
    reasoning_content: str | None = None
    stop_reason: str | None = None
    recalled_memory_ids: list[str] = field(default_factory=list)
    pin_id: str | None = None
    source: str | None = None
    usage: dict[str, int] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {"role": self.role, "content": self.content}
        if self.tool_calls:
            d["tool_calls"] = self.tool_calls
        if self.tool_call_id:
            d["tool_call_id"] = self.tool_call_id
        if self.reasoning_content:
            d["reasoning_content"] = self.reasoning_content
        if self.recalled_memory_ids:
            d["recalled_memory_ids"] = self.recalled_memory_ids
        if self.pin_id:
            d["pin_id"] = self.pin_id
        return d

    def content_text(self) -> str:
        if isinstance(self.content, str):
            return self.content
        parts = [b.get("text", "") for b in self.content if b.get("type") == "text"]
        return " ".join(parts)


@dataclass
class PinnedItem:
    pin_id: str
    label: str
    content: str
    file_path: str | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now())

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "pin_id": self.pin_id,
            "label": self.label,
            "content": self.content,
            "created_at": self.created_at.isoformat(),
        }
        if self.file_path:
            d["file_path"] = self.file_path
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> PinnedItem:
        return cls(
            pin_id=d["pin_id"],
            label=d["label"],
            content=d["content"],
            file_path=d.get("file_path"),
            created_at=datetime.fromisoformat(d.get("created_at", datetime.now().isoformat())),
        )


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any]


@dataclass
class ToolResult:
    tool_call_id: str
    content: str
    is_error: bool = False
    recalled_memory_ids: list[str] = field(default_factory=list)
    content_blocks: list[dict[str, Any]] | None = None


@dataclass(frozen=True)
class CommunicateRequest:
    participant_id: str
    message: str = ""
    conversation_id: str | None = None
    attachments: list[dict[str, Any]] = field(default_factory=list)
    extra: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "participant_id": self.participant_id,
            "message": self.message,
        }
        if self.conversation_id:
            d["conversation_id"] = self.conversation_id
        if self.attachments:
            d["attachments"] = self.attachments
        if self.extra:
            d["extra"] = self.extra
        return d


@dataclass
class CommunicateRegistration:
    registration_id: str
    participant_id: str
    kind: str
    client_id: str
    display_name: str
    created_at: str
    last_registered_at: str
    metadata: dict[str, Any]

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> CommunicateRegistration:
        metadata = data.get("metadata")
        return cls(
            registration_id=str(data.get("registration_id") or uuid.uuid4().hex),
            participant_id=str(data.get("participant_id") or ""),
            kind=str(data.get("kind") or ""),
            client_id=str(data.get("client_id") or ""),
            display_name=str(data.get("display_name") or ""),
            created_at=str(data.get("created_at") or datetime.now().isoformat()),
            last_registered_at=str(
                data.get("last_registered_at") or datetime.now().isoformat()
            ),
            metadata=metadata if isinstance(metadata, dict) else {},
        )

    def to_dict(self, *, active: bool) -> dict[str, Any]:
        return {
            "registration_id": self.registration_id,
            "participant_id": self.participant_id,
            "kind": self.kind,
            "client_id": self.client_id,
            "display_name": self.display_name,
            "active": active,
            "created_at": self.created_at,
            "last_registered_at": self.last_registered_at,
            "metadata": self.metadata,
        }


@dataclass
class LLMResponse:
    content: str
    tool_calls: list[ToolCall]
    stop_reason: Literal["end_turn", "tool_use", "max_tokens"]
    model: str
    usage: dict[str, int]
    reasoning_content: str | None = None
    provider: str = ""


@dataclass
class SummaryResult:
    content: str
    usage: dict[str, int] = field(default_factory=dict)

    @property
    def output_tokens(self) -> int:
        try:
            return int(self.usage.get("output_tokens", 0) or 0)
        except (TypeError, ValueError):
            return 0


@dataclass
class IncomingEvent:
    participant_id: str
    content: str
    conversation_id: str | None = None
    timestamp: datetime = field(default_factory=lambda: datetime.now())
    source: Literal[
        "file",
        "rest",
        "websocket",
        "alarm",
        "system",
        "wecom",
        "code_job",
        "bubble",
        "codex",
        "coworker_desktop",
        "task_reminder",
    ] = "file"
    attachments: list[AttachmentData] = field(default_factory=list)
    event_id: str | None = None


@dataclass
class ConversationThread:
    participant_id: str
    messages: list[Message] = field(default_factory=list)
    summary: str = ""
    summary_message_count: int = 0
    last_active: datetime = field(default_factory=lambda: datetime.now())

    def add(self, message: Message) -> None:
        self.messages.append(message)
        self.last_active = datetime.now()

    def estimate_tokens(self) -> int:
        total = 0
        for m in self.messages:
            total += estimate_content_tokens(m.content)
        return total

    def to_dict(self) -> dict[str, Any]:
        return {
            "participant_id": self.participant_id,
            "messages": [{**m.to_dict(), "source": m.source} for m in self.messages],
            "summary": self.summary,
            "summary_message_count": self.summary_message_count,
            "last_active": self.last_active.isoformat(),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ConversationThread:
        thread = cls(participant_id=data["participant_id"])
        thread.summary = data.get("summary", "")
        thread.summary_message_count = data.get("summary_message_count", 0)
        thread.last_active = datetime.fromisoformat(data["last_active"])
        for m in data["messages"]:
            thread.messages.append(
                Message(
                    role=m["role"],
                    content=m["content"],
                    tool_calls=m.get("tool_calls", []),
                    tool_call_id=m.get("tool_call_id"),
                    reasoning_content=m.get("reasoning_content"),
                    recalled_memory_ids=m.get("recalled_memory_ids", []),
                    source=m.get("source"),
                )
            )
        return thread


@dataclass
class AgentState:
    is_running: bool = False
    is_sleeping: bool = False
    tick: bool = False
    setup_mode: bool = False
    current_provider: str = ""
    current_model: str = ""
    cycle_count: int = 0
    last_active: datetime | None = None
    restart_requested: bool = False
    restart_reason: str = ""
    last_main_response_usage: dict[str, Any] | None = None
    tool_call_counts: dict = field(default_factory=dict)
    skill_load_counts: dict = field(default_factory=dict)
    # 企微 ID → 人名缓存
    _wecom_names: dict = field(default_factory=dict)
    _wecom_loaded: bool = False

    def _load_wecom_names(self):
        """从 wecom_contacts.json 加载 ID→名字映射"""
        if self._wecom_loaded:
            return
        try:
            import json
            from pathlib import Path
            # 从工作目录找 wecom_contacts.json
            for base in [Path.cwd(), Path(__file__).resolve().parent]:
                for _ in range(5):
                    p = base / ".coworker" / "data" / "wecom_contacts.json"
                    if p.exists():
                        contacts = json.loads(p.read_text(encoding="utf-8"))
                        for wid, info in contacts.items():
                            if isinstance(info, dict) and "name" in info:
                                self._wecom_names[wid] = info["name"]
                        break
                    base = base.parent
                else:
                    continue
                break
        except Exception:
            pass
        self._wecom_loaded = True

    def _replace_ids(self, text: str) -> str:
        """将企微 ID 替换为人名"""
        self._load_wecom_names()
        for wid, name in self._wecom_names.items():
            if wid in text:
                text = text.replace(wid, name)
        text = text.replace("wecom:single:", "").replace("wecom:group:", "")
        return text

