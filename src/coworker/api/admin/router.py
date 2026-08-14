"""Authenticated management API for the local Coworker control room."""

from __future__ import annotations

import asyncio
import base64
import binascii
import json
import os
import re
import secrets
import shutil
import time
import uuid
from collections.abc import Mapping
from datetime import date, datetime, timedelta
from functools import lru_cache
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, cast
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, Response
from loguru import logger
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from coworker.admin.configuration import (
    AdminConfigDependencies,
    AdminConfigService,
    ConfigUpdate,
    ConfigUpdateError,
    JsonObject,
    JsonValue,
)
from coworker.agent.bubble_log_index import (
    load_completed_bubble_index,
    synchronize_completed_bubble_index,
)
from coworker.agent.log_store import LogPageCursor, LogStore
from coworker.channels.traffic import (
    ChannelTrafficStore,
    TrafficDirection,
    TrafficStatus,
)
from coworker.core.config import (
    Config,
    ModelCapabilities,
    ModelCapabilitySpec,
    _deep_merge,
    effective_admin_token,
    load_admin_overrides,
)
from coworker.core.startup_intent import (
    write_bootstrap_startup_intent,
)
from coworker.desktop_updates import build_runtime_spec, provider_metadata
from coworker.i18n import capture_locale, locale_context, tr
from coworker.persona import Person, PersonAlias
from coworker.prompts.template import (
    DEFAULT_SYSTEM_PROMPT_TEMPLATE,
    SYSTEM_PROMPT_CONTENT_VARIABLES,
    SYSTEM_PROMPT_VARIABLES,
    resolve_system_prompt_template,
)

if TYPE_CHECKING:
    from coworker.agent.bubble import Bubble, BubbleStore
    from coworker.agent.loop import AgentLoop
    from coworker.agent.subconscious_mode import SubconsciousMode, SubconsciousModeLoader
    from coworker.agent.usage_stats import UsageStatsCollector
    from coworker.brain.brain import Brain
    from coworker.channels.module import ChannelModuleRegistry
    from coworker.desktop_updates import SyncService
    from coworker.memory.long_term import LongTermMemory
    from coworker.memory.short_term import ShortTermMemory
    from coworker.palaces.loader import Palace, PalaceLoader
    from coworker.persona import PersonaCard, PersonStore
    from coworker.relay import RelayClient
    from coworker.skills.loader import Skill, SkillLoader
    from coworker.tools.alarm_tools import AlarmManager
    from coworker.tools.reasoning_tools import Task, TaskStore

type ApiResponse = dict[str, object]
type ContentLoader = SkillLoader | PalaceLoader | SubconsciousModeLoader

_TERMINAL_BUBBLE_STATUSES = frozenset({"done", "error", "cancelled", "timeout"})
_BUBBLE_LOG_TAIL_BYTES = 64 * 1024

router = APIRouter(prefix="/api/admin", tags=["admin"])

_agent: AgentLoop | None = None
_brain: Brain | None = None
_config: Config | None = None
_inherited_config: Config | None = None
_alarms: AlarmManager | None = None
_skill_loader: SkillLoader | None = None
_palace_loader: PalaceLoader | None = None
_mode_loader: SubconsciousModeLoader | None = None
_desktop_update_sync: SyncService | None = None
_channel_modules: ChannelModuleRegistry | None = None
_process_started_at: datetime = datetime.now()
_admin_config_service: AdminConfigService | None = None
_relay_client: RelayClient | None = None
_person_store: PersonStore | None = None
_persona_cards: PersonaCard | None = None
_usage_stats: UsageStatsCollector | None = None
_background_tasks: set[asyncio.Task[None]] = set()
_CONTENT_TYPES = {"skills", "palaces", "subconscious"}
_SAFE_SLUG = re.compile(r"^[\w.-]{1,80}$", re.UNICODE)
_SAFE_BUBBLE_ID = re.compile(r"^bbl_[A-Za-z0-9_-]{1,160}$")
_BOOTSTRAP_MANAGED_CONFIG_PATHS = {
    "admin.token",
    "admin.config_file",
    "desktop_updates.admin_token",
    "llm.default_provider",
    "llm.default_model",
    "llm.managed_providers",
    "llm.providers_file",
    "llm.runtime_config_file",
}
_BOOTSTRAP_HIDDEN_LLM_SUFFIXES = ("_api_key", "_base_url")


class ConfigPatch(BaseModel):
    changes: JsonObject = Field(default_factory=dict)
    secrets: dict[str, str | None] = Field(default_factory=dict)
    clear_overrides: list[str] = Field(default_factory=list)


class IdentityPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = None
    personality: str | None = None
    current_location: str | None = None


class PersonAliasPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    participant_id: str = Field(min_length=1, max_length=400)
    conversation_id: str | None = None
    channel: str = Field(default="", max_length=80)
    notes: list[str] = Field(default_factory=list, max_length=20)


class PersonPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    display_name: str | None = Field(default=None, max_length=120)
    notes: list[str] | None = None
    aliases: list[PersonAliasPatch] | None = None


class PersonMergePayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    other_person_id: str = Field(min_length=1, max_length=120)


class BootstrapPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider_type: Literal["anthropic", "openai", "deepseek", "qwen", "zhipu", "minimax"]
    model: str = Field(min_length=1, max_length=120)
    api_key: str = Field(min_length=1, max_length=4096)
    base_url: str = Field(default="", max_length=2048)
    coworker_name: str = Field(default="", max_length=80)
    reconnect_proof: str = Field(default="", pattern=r"^(?:[0-9a-f]{64})?$")
    model_capabilities: ModelCapabilities | None = None
    configuration: JsonObject = Field(default_factory=dict)
    secrets: dict[str, str | None] = Field(default_factory=dict)


class SummaryModelPatch(BaseModel):
    provider: str | None = None
    model: str | None = None
    thinking: bool | None = None


class VisionModelPatch(BaseModel):
    provider: str | None = None
    model: str | None = None
    thinking: bool | None = None


class Mem0ModelPatch(BaseModel):
    """mem0 抽取 LLM 配置。provider/model 留空表示跟随主线。"""

    provider: str | None = None
    model: str | None = None
    thinking: bool | None = None


class ModelPatch(BaseModel):
    summary: SummaryModelPatch | None = None
    fallbacks: list[str] | None = None
    vision: VisionModelPatch | None = None
    mem0: Mem0ModelPatch | None = None


class SwitchModelPayload(BaseModel):
    provider: str
    model_id: str = ""


class ConfirmPayload(BaseModel):
    confirm_name: str = ""


class TaskPayload(BaseModel):
    description: str
    details: str = ""
    status: str | None = None


class MemoryPatch(BaseModel):
    content: str
    tags: list[str] | None = None


class PinnedContextPayload(BaseModel):
    label: str = Field(min_length=1, max_length=80)
    content: str = Field(min_length=1, max_length=100_000)


class AlarmPayload(BaseModel):
    trigger_at: datetime
    message: str
    repeat_seconds: int | None = Field(default=None, ge=1)


class ContentPayload(BaseModel):
    raw: str


class ContentFilePayload(BaseModel):
    content: str


class BackupRestorePayload(BaseModel):
    filename: str
    mode: Literal["full", "summarize"] = "full"
    confirm_name: str = ""


class RelayConnectPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    relay_url: str = Field(min_length=1, max_length=2048)
    pairing_code: str = Field(min_length=4, max_length=128)


def setup_admin(
    *,
    agent: AgentLoop,
    brain: Brain,
    config: Config,
    alarm_manager: AlarmManager,
    skill_loader: SkillLoader,
    palace_loader: PalaceLoader,
    mode_loader: SubconsciousModeLoader,
    long_term: LongTermMemory | None = None,
    desktop_update_sync: SyncService | None = None,
    inherited_config: Config | None = None,
    relay_client: RelayClient | None = None,
    person_store: PersonStore | None = None,
    persona_cards: PersonaCard | None = None,
    usage_stats: UsageStatsCollector | None = None,
) -> None:
    global \
        _agent, \
        _brain, \
        _config, \
        _inherited_config, \
        _alarms, \
        _skill_loader, \
        _palace_loader, \
        _mode_loader, \
        _desktop_update_sync, \
        _admin_config_service, \
        _relay_client, \
        _person_store, \
        _persona_cards, \
        _usage_stats
    _agent = agent
    _brain = brain
    _config = config
    _inherited_config = (inherited_config or config).model_copy(deep=True)
    _alarms = alarm_manager
    _skill_loader = skill_loader
    _palace_loader = palace_loader
    _mode_loader = mode_loader
    _desktop_update_sync = desktop_update_sync
    _relay_client = relay_client
    _person_store = person_store
    _persona_cards = persona_cards
    _usage_stats = usage_stats
    _admin_config_service = AdminConfigService(
        AdminConfigDependencies(
            agent=agent,
            brain=brain,
            config=config,
            inherited_config=_inherited_config,
            desktop_update_sync=desktop_update_sync,
            long_term=long_term,
        )
    )
    _admin_config_service.set_channel_modules(_channel_modules)


def setup_channel_admin(modules: ChannelModuleRegistry | None) -> None:
    global _channel_modules
    _channel_modules = modules
    if _admin_config_service is not None:
        _admin_config_service.set_channel_modules(modules)


def _require_agent() -> AgentLoop:
    if _agent is None:
        raise HTTPException(status_code=503, detail=tr("api.state.agent_not_ready"))
    return _agent


def _require_brain() -> Brain:
    if _brain is None:
        raise HTTPException(status_code=503, detail=tr("api.state.brain_not_ready"))
    return _brain


def _require_usage_stats() -> UsageStatsCollector:
    if _usage_stats is None:
        raise HTTPException(
            status_code=503,
            detail=tr("api.state.usage_stats_not_ready"),
        )
    return _usage_stats


def _require_persona() -> tuple[PersonStore, PersonaCard]:
    if _person_store is None or _persona_cards is None:
        raise HTTPException(status_code=503, detail=tr("api.admin.persona_disabled"))
    return _person_store, _persona_cards


def _require_relay_client() -> RelayClient:
    if _relay_client is None:
        raise HTTPException(
            status_code=503,
            detail=tr("api.relay.client_not_ready"),
        )
    return _relay_client


def _require_config() -> Config:
    if _config is None:
        raise HTTPException(status_code=503, detail=tr("api.state.config_not_ready"))
    return _config


def _require_admin_config_service() -> AdminConfigService:
    if _admin_config_service is None:
        raise HTTPException(status_code=503, detail=tr("api.state.config_not_ready"))
    return _admin_config_service


def _require_alarms() -> AlarmManager:
    if _alarms is None:
        raise HTTPException(status_code=503, detail=tr("api.state.alarm_manager_not_ready"))
    return _alarms


def _require_desktop_update_sync() -> SyncService:
    if _desktop_update_sync is None:
        raise HTTPException(
            status_code=503,
            detail=tr("api.state.desktop_update_sync_not_ready"),
        )
    return _desktop_update_sync


def _require_channel_modules() -> ChannelModuleRegistry:
    if _channel_modules is None:
        raise HTTPException(
            status_code=503,
            detail=tr("api.state.channel_modules_not_ready"),
        )
    return _channel_modules


def _require_task_store() -> TaskStore:
    store = _require_agent()._task_store
    if store is None:
        raise HTTPException(status_code=503, detail=tr("api.state.task_store_not_ready"))
    return store


def _require_bubble_store() -> BubbleStore:
    store = _require_agent()._bubble_store
    if store is None:
        raise HTTPException(status_code=503, detail=tr("api.state.bubble_store_not_ready"))
    return store


def _admin_message_content(content: object) -> object:
    """Keep readable content blocks without returning embedded attachment bytes."""
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return str(content)
    safe: list[object] = []
    for block in content:
        if not isinstance(block, dict):
            safe.append(str(block))
            continue
        block_type = str(block.get("type") or "unknown")
        if block_type in {"text", "input_text", "output_text"}:
            safe.append({"type": block_type, "text": str(block.get("text") or "")})
        else:
            safe.append(
                {
                    key: block[key]
                    for key in ("type", "media_type", "filename", "name")
                    if key in block
                }
                or {"type": block_type}
            )
    return safe


def _admin_tool_arguments(value: object) -> object:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (TypeError, ValueError):
            return value
    if isinstance(value, dict):
        return {str(key): _admin_tool_arguments(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_admin_tool_arguments(item) for item in value]
    return value


def _admin_tool_calls(
    tool_calls: list[dict], results: Mapping[str, object]
) -> list[dict[str, object]]:
    out: list[dict[str, object]] = []
    for call in tool_calls:
        raw_function = call.get("function")
        function = raw_function if isinstance(raw_function, dict) else {}
        call_id = str(call.get("id") or "")
        item: dict[str, object] = {
            "id": call_id,
            "name": str(function.get("name") or call.get("name") or "unknown"),
            "arguments": _admin_tool_arguments(
                function.get("arguments", call.get("arguments", {}))
            ),
        }
        if call_id in results:
            item["result"] = _admin_message_content(results[call_id])
        out.append(item)
    return out


def _token() -> str:
    if _config is None:
        return ""
    return effective_admin_token(_config)


async def require_admin(
    authorization: str | None = Header(default=None),
) -> None:
    token = _token()
    if not token:
        raise HTTPException(status_code=503, detail=tr("api.auth.admin_token_unconfigured"))
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail=tr("api.auth.admin_token_missing"))
    if not secrets.compare_digest(authorization[7:], token):
        raise HTTPException(status_code=403, detail=tr("api.auth.admin_token_invalid"))


def _audit_path() -> Path:
    logs_dir = _config.agent.logs_dir if _config is not None else "data/logs"
    return Path(logs_dir) / "admin_audit.jsonl"


def _channel_traffic_path() -> Path:
    logs_dir = _config.agent.logs_dir if _config is not None else "data/logs"
    return Path(logs_dir) / "channel_traffic.jsonl"


def _audit(
    request: Request,
    action: str,
    target: str,
    result: str = "ok",
    detail: str = "",
) -> None:
    entry = {
        "ts": datetime.now().isoformat(),
        "action": action,
        "target": target,
        "result": result,
        "source": request.client.host if request.client else "unknown",
        "detail": detail[:500],
    }
    path = _audit_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry, ensure_ascii=False) + "\n")


def _confirmation_name() -> str:
    return _require_agent()._identity.name or "Coworker"


def _require_name_confirmation(name: str) -> None:
    expected = _confirmation_name()
    if name.strip() != expected:
        raise HTTPException(
            status_code=400,
            detail=tr("api.admin.confirm_name", name=expected),
        )


def _task_dict(task: Task) -> JsonObject:
    return cast(JsonObject, task.to_dict())


def _bubble_dict(bubble: Bubble) -> JsonObject:
    return {
        "id": bubble.id,
        "goal": bubble.goal,
        "status": bubble.status,
        "provider": bubble.provider,
        "model": bubble.model,
        "cycles_used": bubble.cycles_used,
        "max_cycles": bubble.max_cycles,
        "participant_id": str(getattr(bubble, "participant_id", "")),
        "conversation_id": str(getattr(bubble, "conversation_id", "")),
        "handoff_transparency": bool(getattr(bubble, "handoff_transparency", False)),
        "resume_count": _as_int(getattr(bubble, "resume_count", 0)),
        "palaces": cast(JsonValue, bubble.palaces),
        "created_at": bubble.created_at.isoformat(),
        "finished_at": bubble.finished_at.isoformat() if bubble.finished_at else None,
        "elapsed_seconds": bubble.elapsed_seconds(),
        "result": bubble.result,
        "error": bubble.error,
    }


def _bubble_logs_dir() -> Path:
    logs_dir = _config.agent.logs_dir if _config is not None else "data/logs"
    return Path(logs_dir) / "bubbles"


def _subconscious_logs_dir() -> Path:
    logs_dir = _config.agent.logs_dir if _config is not None else "data/logs"
    return Path(logs_dir) / "subconscious" / "bubbles"


def _completed_bubble_summaries(log_dir: Path) -> list[JsonObject]:
    """Load indexed terminal logs and recover any completed records missing from it."""
    index_root = log_dir.parent
    records = load_completed_bubble_index(index_root)
    if not log_dir.is_dir():
        if records:
            synchronize_completed_bubble_index(index_root, log_dir, [])
        return []

    log_paths = {path.stem: path for path in log_dir.glob("*.jsonl")}
    indexed: list[JsonObject] = [
        cast(JsonObject, record)
        for record in records or []
        if str(record.get("log_id") or "") in log_paths
    ]
    indexed_log_ids = {str(record.get("log_id") or "") for record in indexed}
    recovered: list[JsonObject] = []
    for log_id, path in log_paths.items():
        if log_id in indexed_log_ids or not _is_terminal_bubble_log(path):
            continue
        summary = _bubble_log_summary(path)
        if summary is not None:
            recovered.append(summary)
    if recovered or records is None or len(indexed) != len(records):
        synchronized = synchronize_completed_bubble_index(
            index_root,
            log_dir,
            [cast(dict[str, object], record) for record in recovered],
        )
        return [cast(JsonObject, record) for record in synchronized]
    return indexed


_INTERACTION_PAGE_SCAN_BYTES = 2 * 1024 * 1024
_INTERACTION_PREVIEW_CHARS = 480
_INTERACTION_DETAIL_STRING_CHARS = 32_000
_INTERACTION_DETAIL_ITEMS = 200
_INTERACTION_DETAIL_DEPTH = 10
_INTERACTION_TIME_RANGE_LIMIT = timedelta(days=1)


def _interaction_logs_dir() -> Path:
    logs_dir = _config.agent.logs_dir if _config is not None else "data/logs"
    return Path(logs_dir)


@lru_cache(maxsize=8)
def _interaction_log_store(logs_dir: str) -> LogStore:
    """Keep shard boundary scans cached across adjacent admin history pages."""
    return LogStore(logs_dir)


def _interaction_sequence_summary(store: LogStore) -> JsonObject:
    """Return lifetime sequence metadata from cached shard boundaries only."""
    shards = store.manifest()
    if not shards:
        return {"first": None, "latest": None, "total": 0}
    first = min(shard.seq_min for shard in shards)
    latest = max(shard.seq_max for shard in shards)
    # InteractionLogger starts at seq=0 and increments once per emitted record.
    # ``total`` deliberately reflects that lifetime numbering even if an old
    # archive was removed and ``first`` is no longer zero.
    return {"first": first, "latest": latest, "total": latest + 1}


def _runtime_local_naive(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value
    return value.astimezone().replace(tzinfo=None)


def _encode_interaction_cursor(cursor: LogPageCursor | None) -> str | None:
    if cursor is None:
        return None
    payload = {
        "p": cursor.path,
        "o": cursor.offset,
        "s": cursor.before_seq,
    }
    raw = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _decode_interaction_cursor(value: str | None) -> LogPageCursor | None:
    if not value:
        return None
    if len(value) > 512:
        raise HTTPException(status_code=400, detail=tr("api.admin.invalid_log_cursor"))
    try:
        padded = value + "=" * (-len(value) % 4)
        payload = json.loads(base64.urlsafe_b64decode(padded).decode("utf-8"))
        path = payload["p"]
        offset = payload["o"]
        before_seq = payload.get("s")
    except (binascii.Error, KeyError, TypeError, ValueError, UnicodeDecodeError):
        raise HTTPException(
            status_code=400, detail=tr("api.admin.invalid_log_cursor")
        ) from None
    if (
        not isinstance(path, str)
        or not path
        or Path(path).name != path
        or type(offset) is not int
        or offset < 0
        or (before_seq is not None and (type(before_seq) is not int or before_seq < 0))
    ):
        raise HTTPException(status_code=400, detail=tr("api.admin.invalid_log_cursor"))
    return LogPageCursor(path=path, offset=offset, before_seq=before_seq)


def _interaction_text(value: object, limit: int = _INTERACTION_PREVIEW_CHARS) -> str:
    if isinstance(value, str):
        text = value
    else:
        try:
            text = json.dumps(value, ensure_ascii=False, default=str, separators=(",", ":"))
        except (TypeError, ValueError):
            text = str(value)
    text = " ".join(text.split())
    return text if len(text) <= limit else text[:limit] + "…"


def _interaction_preview(entry: Mapping[str, object]) -> str:
    for key in ("content", "reasoning_content", "result", "goal", "message", "query", "label"):
        value = entry.get(key)
        if value not in (None, "", [], {}):
            return _interaction_text(value)
    name = entry.get("name")
    arguments = entry.get("arguments")
    if name:
        suffix = _interaction_text(arguments, 300) if arguments not in (None, "", {}, []) else ""
        return f"{name}{' · ' + suffix if suffix else ''}"
    details = {str(key): value for key, value in entry.items() if key not in {"seq", "ts", "type"}}
    return _interaction_text(details) if details else "—"


def _interaction_list_item(entry: Mapping[str, object]) -> JsonObject:
    meta: JsonObject = {}
    for key in (
        "name",
        "source",
        "participant_id",
        "provider",
        "model",
        "cycle",
        "mode",
        "trigger",
        "storage",
        "operation",
        "stop_reason",
        "is_error",
        "thinking",
        "messages_compressed",
        "duration_ms",
        "summary_calls",
        "summary_total_tokens",
    ):
        value = entry.get(key)
        if value not in (None, ""):
            meta[key] = _interaction_text(value, 120)
    seq = entry.get("seq")
    return {
        "seq": seq if isinstance(seq, int) else None,
        "ts": str(entry.get("ts") or ""),
        "type": str(entry.get("type") or "unknown"),
        "preview": _interaction_preview(entry),
        "meta": meta,
    }


def _bounded_interaction_value(value: object, state: list[bool], depth: int = 0) -> JsonValue:
    if depth >= _INTERACTION_DETAIL_DEPTH:
        state[0] = True
        return tr("api.admin.nested_truncated")
    if value is None or isinstance(value, bool | int | float):
        return cast(JsonValue, value)
    if isinstance(value, str):
        if len(value) > _INTERACTION_DETAIL_STRING_CHARS:
            state[0] = True
            return value[:_INTERACTION_DETAIL_STRING_CHARS] + tr("api.admin.field_truncated")
        return value
    if isinstance(value, list):
        items = value[:_INTERACTION_DETAIL_ITEMS]
        if len(value) > len(items):
            state[0] = True
        return [_bounded_interaction_value(item, state, depth + 1) for item in items]
    if isinstance(value, dict):
        result: dict[str, JsonValue] = {}
        for index, (key, item) in enumerate(value.items()):
            if index >= _INTERACTION_DETAIL_ITEMS:
                state[0] = True
                result["…"] = tr("api.admin.more_fields_truncated")
                break
            result[str(key)] = _bounded_interaction_value(item, state, depth + 1)
        return result
    return str(value)


def _read_bubble_log_uncached(path: str) -> list[dict[str, object]]:
    entries: list[dict[str, object]] = []
    try:
        with Path(path).open("r", encoding="utf-8") as handle:
            for line in handle:
                try:
                    entry = json.loads(line)
                except (TypeError, ValueError):
                    continue
                if isinstance(entry, dict):
                    sanitized = _admin_tool_arguments(entry)
                    if isinstance(sanitized, dict):
                        entries.append(sanitized)
    except OSError:
        return []
    return entries


@lru_cache(maxsize=512)
def _read_completed_bubble_log_cached(
    path: str, _mtime_ns: int, _size: int
) -> list[dict[str, object]]:
    return _read_bubble_log_uncached(path)


def _is_terminal_bubble_log(path: Path) -> bool:
    """Check the final metadata record without parsing a complete JSONL log."""
    try:
        with path.open("rb") as handle:
            handle.seek(0, 2)
            handle.seek(max(0, handle.tell() - _BUBBLE_LOG_TAIL_BYTES))
            tail = handle.read().decode("utf-8", errors="replace")
    except OSError:
        return False
    for line in reversed(tail.splitlines()):
        try:
            entry = json.loads(line)
        except (TypeError, ValueError):
            continue
        if not isinstance(entry, dict):
            continue
        if not entry.get("__meta__"):
            return False
        return str(entry.get("status") or "").lower() in _TERMINAL_BUBBLE_STATUSES
    return False


def _read_bubble_log_summary_uncached(
    path: str, _mtime_ns: int, _size: int
) -> JsonObject | None:
    """Read only the fields needed by the list view.

    The detail endpoint needs the fully sanitized event stream, but listing
    records should not materialize and sanitize every event in every log. In
    particular, active logs change frequently and therefore miss the detail
    cache on every request.
    """
    first: dict[str, object] | None = None
    meta: dict[str, object] | None = None
    llm_provider = ""
    llm_model = ""
    result = ""
    try:
        with Path(path).open("r", encoding="utf-8") as handle:
            for line in handle:
                try:
                    entry = json.loads(line)
                except (TypeError, ValueError):
                    continue
                if not isinstance(entry, dict):
                    continue
                if first is None:
                    first = entry
                if entry.get("__meta__"):
                    meta = entry
                if entry.get("type") == "llm_response":
                    llm_provider = llm_provider or str(entry.get("provider") or "")
                    llm_model = llm_model or str(entry.get("model") or "")
                if entry.get("type") == "tool_call" and entry.get("name") == "bubble_done":
                    arguments = entry.get("arguments")
                    if isinstance(arguments, dict) and not arguments.get("checkpoint"):
                        result = str(arguments.get("result") or result)
    except OSError:
        return None

    if meta is None:
        return None
    bubble_id = str(meta.get("id") or Path(path).stem)
    stem = Path(path).stem
    mode = stem[len(bubble_id) + 1 :] if stem.startswith(f"{bubble_id}_") else ""
    return {
        "id": bubble_id,
        "log_id": stem,
        "mode": mode,
        "goal": str(meta.get("goal") or tr("api.admin.goal_unrecorded")),
        "status": str(meta.get("status") or "done"),
        "provider": str(meta.get("provider") or llm_provider),
        "model": str(meta.get("model") or llm_model),
        "cycles_used": _as_int(meta.get("cycles_used")),
        "max_cycles": _as_int(meta.get("max_cycles")),
        "participant_id": str(meta.get("participant_id") or ""),
        "conversation_id": str(meta.get("conversation_id") or ""),
        "handoff_transparency": bool(meta.get("handoff_transparency")),
        "resume_count": _as_int(meta.get("resume_count")),
        "palaces": cast(JsonValue, meta.get("palaces") or []),
        "created_at": str((first or meta).get("ts") or ""),
        "finished_at": str(meta.get("ts") or ""),
        "elapsed_seconds": _as_float(meta.get("elapsed_seconds")),
        "result": result,
        "error": str(meta.get("error") or ""),
    }


@lru_cache(maxsize=512)
def _bubble_log_summary_cached(
    path: str, _mtime_ns: int, _size: int
) -> JsonObject | None:
    return _read_bubble_log_summary_uncached(path, _mtime_ns, _size)


def _read_bubble_log(path: Path) -> list[dict[str, object]]:
    try:
        stat = path.stat()
    except OSError:
        return []
    resolved = str(path.resolve())
    if _is_terminal_bubble_log(path):
        return _read_completed_bubble_log_cached(resolved, stat.st_mtime_ns, stat.st_size)
    return _read_bubble_log_uncached(resolved)


def _read_bubble_log_summary(path: Path) -> JsonObject | None:
    try:
        stat = path.stat()
    except OSError:
        return None
    resolved = str(path.resolve())
    if _is_terminal_bubble_log(path):
        return _bubble_log_summary_cached(resolved, stat.st_mtime_ns, stat.st_size)
    return _read_bubble_log_summary_uncached(resolved, stat.st_mtime_ns, stat.st_size)


def _as_int(value: object, default: int = 0) -> int:
    try:
        return int(cast(Any, value))
    except (TypeError, ValueError, OverflowError):
        return default


def _as_float(value: object, default: float = 0.0) -> float:
    try:
        return float(cast(Any, value))
    except (TypeError, ValueError, OverflowError):
        return default


def _bubble_log_summary(path: Path) -> JsonObject | None:
    return _read_bubble_log_summary(path)


def _bubble_snapshot(bubble: Bubble) -> dict[str, object]:
    return {
        "type": "bubble_snapshot",
        "status": bubble.status,
        "goal": bubble.goal,
        "result": bubble.result,
        "error": bubble.error,
        "participant_id": str(getattr(bubble, "participant_id", "")),
        "conversation_id": str(getattr(bubble, "conversation_id", "")),
        "handoff_transparency": bool(getattr(bubble, "handoff_transparency", False)),
        "resume_count": _as_int(getattr(bubble, "resume_count", 0)),
        "content": (
            tr("api.admin.detail_log_pending")
            if bubble.status == "running"
            else tr("api.admin.detail_log_unavailable")
        ),
        "ts": bubble.created_at.isoformat(),
    }


@router.post("/session/verify")
async def verify_session(_: None = Depends(require_admin)) -> ApiResponse:
    return {
        "ok": True,
        "name": _require_agent()._identity.name,
        "confirmation_name": _confirmation_name(),
    }


def _bootstrap_managed_config_path(
    configuration: JsonObject,
    secrets: dict[str, str | None],
) -> str | None:
    for section, value in configuration.items():
        if not isinstance(value, dict):
            continue
        for field_name in value:
            path = f"{section}.{field_name}"
            if path in _BOOTSTRAP_MANAGED_CONFIG_PATHS:
                return path
            if section == "llm" and field_name.endswith(
                _BOOTSTRAP_HIDDEN_LLM_SUFFIXES
            ):
                return path
    for path in secrets:
        if path in _BOOTSTRAP_MANAGED_CONFIG_PATHS or path in {
            "admin.token",
            "desktop_updates.admin_token",
        }:
            return path
    return None


def _server_utc_offset() -> str:
    is_dst = bool(time.daylight and time.localtime().tm_isdst)
    offset_seconds = -(time.altzone if is_dst else time.timezone)
    hours, remainder = divmod(abs(offset_seconds), 3600)
    minutes = remainder // 60
    sign = "+" if offset_seconds >= 0 else "-"
    return f"{sign}{hours:02d}:{minutes:02d}"


def _server_timezone() -> str:
    """Return the browser-parseable timezone that owns naive runtime timestamps."""

    configured = os.environ.get("TZ", "").strip().removeprefix(":")
    if configured and not configured.startswith("/"):
        try:
            ZoneInfo(configured)
        except ZoneInfoNotFoundError:
            pass
        else:
            return configured
    local_timezone = datetime.now().astimezone().tzinfo
    key = getattr(local_timezone, "key", None)
    if isinstance(key, str) and key:
        return key
    return _server_utc_offset()


def _server_timezone_description() -> str:
    """Describe the operating system timezone without creating app-level state."""

    offset = _server_utc_offset()
    hours, minutes = offset[1:].split(":", maxsplit=1)
    display_offset = (
        f"UTC{offset[0]}{int(hours)}"
        if minutes == "00"
        else f"UTC{offset[0]}{int(hours)}:{minutes}"
    )
    return f"{_server_timezone()} ({display_offset})"


@router.get("/bootstrap")
async def bootstrap_status(_: None = Depends(require_admin)) -> ApiResponse:
    """Describe whether this installation still needs its first model connection."""

    brain = _require_brain()
    snapshot = _require_admin_config_service().snapshot()
    from coworker.brain.factory import available_models, available_types

    providers: list[dict[str, object]] = []
    for provider_type in available_types():
        providers.append({"type": provider_type, "models": available_models(provider_type)})
    return {
        "required": brain.active_provider is None,
        "active_provider": brain.current_provider_name,
        "active_model": brain.current_model,
        "server_timezone": _server_timezone(),
        "server_timezone_description": _server_timezone_description(),
        "providers": providers,
        "defaults": {
            "configuration": snapshot.config,
            "secret_status": snapshot.secret_status,
        },
    }


@router.post("/bootstrap", status_code=202)
async def complete_bootstrap(
    payload: BootstrapPayload,
    request: Request,
    _: None = Depends(require_admin),
) -> ApiResponse:
    """Persist the first provider connection and restart into normal operation."""

    config_service = _require_admin_config_service()
    async with config_service.lock:
        config = _require_config()
        brain = _require_brain()
        if brain.active_provider is not None or config_service.pending_restart:
            raise HTTPException(status_code=409, detail=tr("api.admin.already_initialized"))

        from coworker.brain.factory import available_models, build_provider

        provider_type = payload.provider_type.strip()
        model = payload.model.strip()
        api_key = payload.api_key.strip()
        base_url = payload.base_url.strip()
        if not model:
            raise HTTPException(status_code=422, detail=tr("api.admin.model_required"))
        if not api_key:
            raise HTTPException(status_code=422, detail=tr("api.admin.api_key_required"))

        catalog_models = available_models(provider_type)
        custom_model = model not in catalog_models
        if custom_model and payload.model_capabilities is None:
            raise HTTPException(
                status_code=422,
                detail=tr(
                    "api.admin.custom_model_confirmation_required",
                    model=repr(model),
                    provider=provider_type,
                ),
            )
        declared_models = (
            [ModelCapabilitySpec(model=model, **payload.model_capabilities.model_dump())]
            if payload.model_capabilities is not None
            else []
        )
        provider = build_provider(
            provider_type,
            api_key,
            base_url=base_url or None,
            name=provider_type,
            default_model=model,
            model_capabilities=declared_models,
        )
        if not provider.can_use_tools(model):
            raise HTTPException(
                status_code=422,
                detail=tr(
                    "api.admin.unsupported_tool_model",
                    model=repr(model),
                    provider=provider_type,
                ),
            )

        managed_path = _bootstrap_managed_config_path(
            payload.configuration,
            payload.secrets,
        )
        if managed_path is not None:
            raise HTTPException(
                status_code=422,
                detail=tr("api.admin.bootstrap_field_managed", path=managed_path),
            )
        path = Path(config.admin.config_file)
        current_overrides = load_admin_overrides(path)
        try:
            next_overrides = config_service.prepare_overrides(
                current_overrides,
                ConfigUpdate(
                    changes=payload.configuration,
                    secrets=payload.secrets,
                ),
            )
        except ConfigUpdateError as error:
            raise HTTPException(
                status_code=error.status_code,
                detail=error.detail,
            ) from error
        connection_changes: JsonObject = {
            "llm": {
                "default_provider": provider_type,
                "default_model": model,
                "managed_providers": [
                    {
                        "name": provider_type,
                        "type": provider_type,
                        "api_key": api_key,
                        "base_url": base_url,
                        "default_model": model,
                        "model_capabilities": [
                            capability.model_dump(mode="json")
                            for capability in declared_models
                        ],
                    }
                ],
            },
        }
        next_overrides = config_service.merge_overrides(
            next_overrides,
            connection_changes,
        )
        try:
            desired = Config.model_validate(
                _deep_merge(config.model_dump(mode="json"), next_overrides)
            )
        except ValidationError as e:
            raise HTTPException(status_code=422, detail=json.loads(e.json())) from e

        agent = _require_agent()
        identity = agent._identity
        name_path: Path | None = None
        previous_name: bytes | None = None
        name_snapshot_captured = False
        reload_identity = False
        startup_intent_path: Path | None = None
        restart_pending = False
        try:
            if payload.coworker_name.strip():
                current_identity_dir = Path(identity._dir)
                identity_dir = current_identity_dir
                agent_changes = payload.configuration.get("agent")
                if isinstance(agent_changes, dict) and "identity_dir" in agent_changes:
                    identity_dir = Path(desired.agent.identity_dir)
                name_path = identity_dir / "name.txt"
                previous_name = name_path.read_bytes() if name_path.exists() else None
                name_snapshot_captured = True
                identity_dir.mkdir(parents=True, exist_ok=True)
                name_path.write_text(payload.coworker_name.strip(), encoding="utf-8")
                reload_identity = identity_dir == current_identity_dir
                if reload_identity:
                    identity.load()

            startup_intent_path = write_bootstrap_startup_intent(
                desired.memory.db_path,
                provider=provider_type,
                model=model,
                reconnect_proof=payload.reconnect_proof,
            )
            config_service.write_sparse_overrides(path, next_overrides)
            config_service.mark_restart_pending("bootstrap")
            restart_pending = True
            asyncio.get_running_loop().call_later(
                0.5, lambda: agent.request_restart(reason="bootstrap")
            )
        except Exception:
            if name_path is not None and name_snapshot_captured:
                try:
                    if previous_name is None:
                        name_path.unlink(missing_ok=True)
                    else:
                        name_path.write_bytes(previous_name)
                    if reload_identity:
                        identity.load()
                except Exception as rollback_error:
                    logger.warning(
                        f"Failed to roll back bootstrap identity name: {rollback_error}"
                    )
            if restart_pending:
                try:
                    config_service.clear_restart_pending("bootstrap")
                except Exception as rollback_error:
                    logger.warning(
                        f"Failed to clear bootstrap restart state: {rollback_error}"
                    )
            if startup_intent_path is not None:
                try:
                    startup_intent_path.unlink(missing_ok=True)
                except OSError as rollback_error:
                    logger.warning(
                        f"Failed to clear bootstrap startup intent: {rollback_error}"
                    )
            raise
        try:
            _audit(
                request,
                "bootstrap.complete",
                f"{provider_type}/{model} locale={desired.i18n.locale.value} max_tokens={desired.llm.max_tokens} passive_mode={desired.agent.passive_mode} custom_model={custom_model}",
            )
        except OSError as error:
            logger.warning(f"Failed to write bootstrap audit entry: {error}")
        return {"accepted": True, "restarting": True}


@router.get("/overview")
async def overview(_: None = Depends(require_admin)) -> ApiResponse:
    agent = _require_agent()
    brain = _require_brain()
    config = _require_config()
    tasks = agent._task_store.list() if agent._task_store else []
    bubbles = agent._bubble_store.list_active() if agent._bubble_store else []
    memory_count = await agent._long_term.count()
    stm = agent._short_term
    startup_reason = "unknown"
    try:
        instance_status = json.loads(
            (Path(config.memory.db_path) / "instance_status.json").read_text(
                encoding="utf-8"
            )
        )
        if instance_status.get("startup_reason") in {"bootstrap", "restart", "start"}:
            startup_reason = instance_status["startup_reason"]
    except (OSError, json.JSONDecodeError, TypeError):
        pass
    return {
        "status": {
            "is_running": agent.state.is_running,
            "is_sleeping": agent.state.is_sleeping,
            "provider": brain.current_provider_name,
            "model": brain.current_model,
            "cycle_count": agent.state.cycle_count,
            "started_at": _process_started_at.isoformat(),
            "startup_reason": startup_reason,
            "passive_mode": config.agent.passive_mode,
            "idle_sleep_seconds": config.agent.idle_sleep_seconds,
        },
        "counts": {
            "tasks": len(tasks),
            "active_tasks": sum(t.status in ("pending", "in_progress") for t in tasks),
            "active_bubbles": len(bubbles),
            "long_term_memories": memory_count,
            "short_term_messages": len(stm.primary),
            "alarms": len(_require_alarms().list()),
        },
        "memory": {
            "max_tokens": stm._max_tokens,
            "messages": len(stm.primary),
            "tree_nodes": len(stm.tree.nodes),
            "backfill": stm.backfill_progress,
        },
        "pending_restart": _require_admin_config_service().pending_restart,
    }


@router.get("/usage")
async def usage(
    _: None = Depends(require_admin),
    start_date: date | None = Query(default=None),
    end_date: date | None = Query(default=None),
) -> ApiResponse:
    model_prices = _require_config().llm.model_prices
    if start_date is None and end_date is None:
        return _require_usage_stats().report(model_prices=model_prices)
    selected_start = start_date or end_date
    selected_end = end_date or start_date
    if (
        selected_start is None
        or selected_end is None
        or selected_start > selected_end
    ):
        raise HTTPException(
            status_code=422,
            detail=tr("api.admin.invalid_usage_date_range"),
        )
    return _require_usage_stats().report(
        start_date=selected_start,
        end_date=selected_end,
        model_prices=model_prices,
    )


@router.get("/config")
async def get_config(_: None = Depends(require_admin)) -> ApiResponse:
    snapshot = _require_admin_config_service().snapshot()
    return {
        "config": snapshot.config,
        "effective_providers": snapshot.effective_providers,
        "secret_status": snapshot.secret_status,
        "overridden_fields": snapshot.overridden_fields,
        "hot_reloadable": snapshot.hot_reloadable,
        "override_path": snapshot.override_path,
        "pending_restart": snapshot.pending_restart,
        "sources": {
            "base": ".env / environment",
            "providers": _require_config().llm.providers_file,
            "override": snapshot.override_path,
        },
    }


@router.patch("/config")
async def patch_config(
    payload: ConfigPatch,
    request: Request,
    _: None = Depends(require_admin),
) -> ApiResponse:
    try:
        result = await _require_admin_config_service().patch(
            ConfigUpdate(
                changes=payload.changes,
                secrets=payload.secrets,
                clear_overrides=payload.clear_overrides,
            )
        )
    except ConfigUpdateError as error:
        raise HTTPException(status_code=error.status_code, detail=error.detail) from error
    _audit(
        request,
        "config.update",
        str(result.override_path),
        detail=(
            f"hot={','.join(result.applied_now)}; "
            f"restart={','.join(result.requires_restart)}"
        ),
    )
    return {
        "saved": True,
        "pending_restart": result.pending_restart,
        "applied_now": result.applied_now,
        "requires_restart": result.requires_restart,
    }


@router.get("/channels/{channel_name}/management")
async def get_channel_management(
    channel_name: str,
    _: None = Depends(require_admin),
) -> ApiResponse:
    management = _require_channel_modules().management_for(channel_name)
    if management is None:
        raise HTTPException(
            status_code=404,
            detail=tr("api.admin.channel_management_missing", channel=channel_name),
        )
    return await management.snapshot()


@router.get("/relay")
async def get_relay(_: None = Depends(require_admin)) -> ApiResponse:
    return _require_relay_client().snapshot()


@router.get("/relay/token")
async def get_relay_token(
    request: Request,
    _: None = Depends(require_admin),
) -> ApiResponse:
    _audit(request, "relay.token.read", "relay")
    snapshot = _require_relay_client().snapshot(include_token=True)
    return {"communication_token": snapshot.get("communication_token", "")}


@router.post("/relay/connect")
async def connect_relay(
    payload: RelayConnectPayload,
    request: Request,
    _: None = Depends(require_admin),
) -> ApiResponse:
    try:
        result = await _require_relay_client().enroll(
            payload.relay_url,
            payload.pairing_code,
        )
    except Exception as error:
        _audit(request, "relay.connect", "relay", result="error", detail=type(error).__name__)
        raise HTTPException(
            status_code=502,
            detail=tr("api.relay.connect_failed", error=error),
        ) from error
    _audit(request, "relay.connect", str(result.get("instance_id", "")))
    return result


@router.post("/relay/test")
async def test_relay(
    request: Request,
    _: None = Depends(require_admin),
) -> ApiResponse:
    try:
        result = await _require_relay_client().test()
    except Exception as error:
        raise HTTPException(
            status_code=502,
            detail=tr("api.relay.test_failed", error=error),
        ) from error
    _audit(request, "relay.test", "relay")
    return result


@router.post("/relay/reconnect", status_code=202)
async def reconnect_relay(
    request: Request,
    _: None = Depends(require_admin),
) -> ApiResponse:
    await _require_relay_client().reconnect()
    _audit(request, "relay.reconnect", "relay")
    return {"accepted": True}

@router.post("/relay/rotate-token")
async def rotate_relay_token(
    request: Request,
    _: None = Depends(require_admin),
) -> ApiResponse:
    try:
        result = await _require_relay_client().rotate_token()
    except Exception as error:
        _audit(
            request,
            "relay.token.rotate",
            "relay",
            result="error",
            detail=type(error).__name__,
        )
        raise HTTPException(
            status_code=502,
            detail=tr("api.relay.token_rotation_failed", error=error),
        ) from error
    _audit(request, "relay.token.rotate", str(result.get("instance_id", "")))
    return result


@router.delete("/relay", status_code=204)
async def disconnect_relay(
    request: Request,
    _: None = Depends(require_admin),
) -> Response:
    instance_id = str(_require_relay_client().snapshot().get("instance_id", ""))
    await _require_relay_client().disconnect()
    _audit(request, "relay.disconnect", instance_id or "relay")
    return Response(status_code=204)


@router.post("/channels/{channel_name}/management/{command}")
async def execute_channel_management(
    channel_name: str,
    command: str,
    payload: dict[str, object],
    request: Request,
    _: None = Depends(require_admin),
) -> ApiResponse:
    management = _require_channel_modules().management_for(channel_name)
    if management is None:
        raise HTTPException(
            status_code=404,
            detail=tr("api.admin.channel_management_missing", channel=channel_name),
        )
    try:
        result = await management.execute(command, payload)
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    except Exception as error:
        raise HTTPException(
            status_code=502,
            detail=tr("api.admin.channel_management_failed", error=error),
        ) from error
    _audit(request, f"channel.{channel_name}.{command}", channel_name)
    return result


@router.get("/desktop-updates/providers")
async def get_desktop_update_providers(_: None = Depends(require_admin)) -> ApiResponse:
    return {"providers": provider_metadata()}


@router.get("/desktop-updates/sync")
async def get_desktop_update_sync(_: None = Depends(require_admin)) -> ApiResponse:
    config = _require_config().desktop_updates
    if _desktop_update_sync is None:
        runtime = build_runtime_spec(config)
        data: dict[str, object] = {
            "enabled": runtime.enabled,
            "ready": runtime.ready,
            "readiness": runtime.readiness,
            "outcome": "idle",
            "phase": "idle",
            "source": runtime.source_summary.model_dump(mode="json") if runtime.source_summary else None,
        }
    else:
        status = await _desktop_update_sync.status()
        data = status.model_dump(mode="json")
    active = config.active_source()
    return {
        **data,
        "active_source": str(config.sync_active_source) if config.sync_active_source else None,
        "active_source_name": active.name if active else "",
        "active_source_type": active.type if active else "",
        "token_configured": bool(active.token) if active else False,
        "providers": provider_metadata(),
    }


@router.post("/desktop-updates/sync", status_code=202)
async def trigger_desktop_update_sync(
    request: Request,
    _: None = Depends(require_admin),
) -> ApiResponse:
    config = _require_config().desktop_updates
    active = config.active_source()
    if active is None:
        raise HTTPException(
            status_code=409,
            detail=tr("api.desktop.sync_disabled"),
        )
    try:
        result = await _require_desktop_update_sync().request_sync("manual")
    except RuntimeError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    _audit(
        request,
        "desktop_updates.sync.trigger",
        str(config.sync_active_source or "disabled"),
        detail=f"run_id={result['run_id']}; coalesced={result['coalesced']}",
    )
    return {"accepted": True, **result}


def _mem0_model_view(config: Config) -> dict[str, Any]:
    """模型编排页所需的 mem0 配置视图（空字符串表示跟随主线）。"""
    return {
        "provider": config.memory.mem0_llm_provider,
        "model": config.memory.mem0_llm_model,
        "thinking": config.memory.mem0_llm_thinking,
    }


@router.get("/model")
async def get_model(_: None = Depends(require_admin)) -> ApiResponse:
    snapshot = _require_brain().model_config_snapshot()
    snapshot["mem0"] = _mem0_model_view(_require_config())
    return cast(ApiResponse, snapshot)


@router.patch("/model")
async def patch_model(
    payload: ModelPatch,
    request: Request,
    _: None = Depends(require_admin),
) -> ApiResponse:
    brain = _require_brain()
    config = _require_config()
    try:
        snapshot = await brain.update_model_config(
            summary_provider=payload.summary.provider if payload.summary else None,
            summary_model=payload.summary.model if payload.summary else None,
            summary_thinking=payload.summary.thinking if payload.summary else None,
            fallbacks=payload.fallbacks,
            vision_provider=payload.vision.provider if payload.vision else None,
            vision_model=payload.vision.model if payload.vision else None,
            vision_thinking=payload.vision.thinking if payload.vision else None,
        )
        from coworker.core.model_config import RuntimeModelConfig, write_runtime_model_config

        write_runtime_model_config(
            Path(config.llm.runtime_config_file),
            RuntimeModelConfig.from_brain_snapshot(snapshot),
        )
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    if payload.mem0 is not None:
        changes: dict[str, Any] = {}
        if payload.mem0.provider is not None:
            changes["mem0_llm_provider"] = payload.mem0.provider
        if payload.mem0.model is not None:
            changes["mem0_llm_model"] = payload.mem0.model
        if payload.mem0.thinking is not None:
            changes["mem0_llm_thinking"] = payload.mem0.thinking
        if changes:
            try:
                await _require_admin_config_service().patch(
                    ConfigUpdate(changes={"memory": changes})
                )
            except ConfigUpdateError as error:
                raise HTTPException(
                    status_code=error.status_code, detail=error.detail
                ) from error
    _audit(request, "model.runtime.update", "model_config")
    response = snapshot
    response["mem0"] = _mem0_model_view(config)
    return cast(ApiResponse, response)


@router.post("/model/switch")
async def switch_model(
    payload: SwitchModelPayload,
    request: Request,
    _: None = Depends(require_admin),
) -> ApiResponse:
    brain = _require_brain()
    agent = _require_agent()
    try:
        await brain.switch_model(payload.provider, payload.model_id)
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    agent.state.current_provider = brain.current_provider_name
    agent.state.current_model = brain.current_model
    _audit(request, "model.switch", f"{brain.current_provider_name}/{brain.current_model}")
    return cast(ApiResponse, brain.model_config_snapshot())


@router.post("/restart", status_code=202)
async def restart(
    payload: ConfirmPayload,
    request: Request,
    _: None = Depends(require_admin),
) -> ApiResponse:
    _require_name_confirmation(payload.confirm_name)
    _audit(request, "runtime.restart", "coworker")
    asyncio.get_running_loop().call_later(0.25, _require_agent().request_restart)
    return {"accepted": True}


@router.post("/resume")
async def resume(
    request: Request,
    _: None = Depends(require_admin),
) -> ApiResponse:
    resumed = _require_agent().resume_from_rest()
    _audit(request, "runtime.resume", "coworker", detail=f"resumed={resumed}")
    if resumed:
        await asyncio.sleep(0)
    return {"resumed": resumed}


@router.get("/tasks")
async def list_tasks(_: None = Depends(require_admin)) -> ApiResponse:
    return {"tasks": [_task_dict(task) for task in _require_task_store().list()]}


@router.post("/tasks")
async def create_task(
    payload: TaskPayload,
    request: Request,
    _: None = Depends(require_admin),
) -> JsonObject:
    task = _require_task_store().create(payload.description.strip(), payload.details)
    _audit(request, "task.create", task.id)
    return _task_dict(task)


@router.patch("/tasks/{task_id}")
async def update_task(
    task_id: str,
    payload: TaskPayload,
    request: Request,
    _: None = Depends(require_admin),
) -> JsonObject:
    task = _require_task_store().update(
        task_id, description=payload.description, details=payload.details, status=payload.status
    )
    if task is None:
        raise HTTPException(status_code=404, detail=tr("api.admin.task_missing"))
    _audit(request, "task.update", task_id)
    return _task_dict(task)


@router.delete("/tasks/{task_id}")
async def delete_task(
    task_id: str,
    request: Request,
    _: None = Depends(require_admin),
) -> ApiResponse:
    task = _require_task_store().update(task_id, status="deleted")
    if task is None:
        raise HTTPException(status_code=404, detail=tr("api.admin.task_missing"))
    _audit(request, "task.delete", task_id)
    return {"deleted": True}


@router.get("/bubbles")
async def list_bubbles(
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    _: None = Depends(require_admin),
) -> ApiResponse:
    store = _require_bubble_store()
    agent = _require_agent()
    scheduler = getattr(agent, "_subconscious", None)
    normal_log_dir = _bubble_logs_dir()
    subconscious_log_dir = _subconscious_logs_dir()
    normal_summaries, subconscious_summaries = await asyncio.gather(
        asyncio.to_thread(_completed_bubble_summaries, normal_log_dir),
        asyncio.to_thread(_completed_bubble_summaries, subconscious_log_dir),
    )
    subconscious_ids = {
        str(summary["id"])
        for summary in subconscious_summaries
    }
    subconscious_ids.update(
        str(bubble_id)
        for bubble_id in getattr(scheduler, "_active_by_mode", {}).values()
        if bubble_id
    )
    live = [
        _bubble_dict(b)
        for b in store.list_active() + list(store._history)
        if b.id not in subconscious_ids
    ]
    by_id = {str(item["id"]): item for item in live}
    for summary in normal_summaries:
        if str(summary["id"]) not in by_id:
            by_id[str(summary["id"])] = summary
    bubbles = sorted(
        by_id.values(),
        key=lambda item: (item["status"] == "running", str(item.get("created_at") or "")),
        reverse=True,
    )
    return {
        "bubbles": bubbles[offset : offset + limit],
        "total": len(bubbles),
        "has_more": offset + limit < len(bubbles),
    }


@router.get("/bubbles/{bubble_id}/history")
async def get_bubble_history(
    bubble_id: str,
    _: None = Depends(require_admin),
) -> ApiResponse:
    if not _SAFE_BUBBLE_ID.fullmatch(bubble_id):
        raise HTTPException(status_code=404, detail=tr("api.admin.bubble_record_missing"))
    path = _bubble_logs_dir() / f"{bubble_id}.jsonl"
    if not path.is_file():
        bubble = _require_bubble_store().get(bubble_id)
        if bubble is None:
            raise HTTPException(
                status_code=404, detail=tr("api.admin.bubble_record_missing")
            )
        return {"bubble_id": bubble_id, "events": [_bubble_snapshot(bubble)]}
    return {"bubble_id": bubble_id, "events": await asyncio.to_thread(_read_bubble_log, path)}


@router.get("/subconscious")
async def list_subconscious(
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    _: None = Depends(require_admin),
) -> ApiResponse:
    by_log_id: dict[str, JsonObject] = {}
    scheduler = getattr(_require_agent(), "_subconscious", None)
    store = getattr(_require_agent(), "_bubble_store", None)
    for mode, bubble_id in getattr(scheduler, "_active_by_mode", {}).items():
        bubble = store.get(bubble_id) if store is not None and bubble_id else None
        if bubble is not None:
            log_id = f"{bubble.id}_{mode}"
            by_log_id[log_id] = {
                **_bubble_dict(bubble),
                "log_id": log_id,
                "mode": mode,
            }
    log_dir = _subconscious_logs_dir()
    for summary in await asyncio.to_thread(_completed_bubble_summaries, log_dir):
        by_log_id[str(summary["log_id"])] = summary
    items = list(by_log_id.values())
    items.sort(key=lambda item: str(item.get("created_at") or ""), reverse=True)
    return {
        "bubbles": items[offset : offset + limit],
        "total": len(items),
        "has_more": offset + limit < len(items),
    }


@router.get("/subconscious/{log_id}/history")
async def get_subconscious_history(
    log_id: str,
    _: None = Depends(require_admin),
) -> ApiResponse:
    if not _SAFE_BUBBLE_ID.fullmatch(log_id):
        raise HTTPException(
            status_code=404, detail=tr("api.admin.subconscious_record_missing")
        )
    path = _subconscious_logs_dir() / f"{log_id}.jsonl"
    if not path.is_file():
        scheduler = getattr(_require_agent(), "_subconscious", None)
        store = getattr(_require_agent(), "_bubble_store", None)
        bubble = next(
            (
                store.get(bubble_id)
                for mode, bubble_id in getattr(scheduler, "_active_by_mode", {}).items()
                if store is not None and bubble_id and f"{bubble_id}_{mode}" == log_id
            ),
            None,
        )
        if bubble is None:
            raise HTTPException(
                status_code=404, detail=tr("api.admin.subconscious_record_missing")
            )
        return {"bubble_id": log_id, "events": [_bubble_snapshot(bubble)]}
    return {"bubble_id": log_id, "events": await asyncio.to_thread(_read_bubble_log, path)}


@router.post("/bubbles/{bubble_id}/cancel")
async def cancel_bubble(
    bubble_id: str,
    request: Request,
    _: None = Depends(require_admin),
) -> JsonObject:
    bubble = _require_bubble_store().get(bubble_id)
    if bubble is None:
        raise HTTPException(status_code=404, detail=tr("api.admin.bubble_missing"))
    if bubble.is_terminal():
        raise HTTPException(status_code=409, detail=tr("api.admin.bubble_finished"))
    if bubble.task and not bubble.task.done():
        bubble.task.cancel()
        try:
            await bubble.task
        except asyncio.CancelledError:
            pass
    # BubbleLoop 的 finally 负责持久化、合并局部结果并从 active 移入 history；
    # 管理 API 不重复 mark_done，避免历史记录出现两份。
    _audit(request, "bubble.cancel", bubble_id)
    return _bubble_dict(bubble)


def _short_term_messages(stm: ShortTermMemory) -> list[dict[str, object]]:
    """Serialize the current short-term message tail for admin observation.

    Mirrors the ``messages`` section of ``/memory/short-term`` so the lightweight
    polling endpoint can stay in sync without re-serializing the tree, watermark, or
    pinned items on every refresh.
    """
    tool_results = {
        message.tool_call_id: message.content
        for message in stm.primary
        if message.role == "tool" and message.tool_call_id
    }
    paired_tool_ids = {
        str(call.get("id") or "")
        for message in stm.primary
        for call in message.tool_calls
        if call.get("id")
    }
    messages = []
    for index, message in enumerate(stm.primary):
        if message.role == "tool" and message.tool_call_id in paired_tool_ids:
            continue
        item: dict[str, object] = {
            "index": index,
            "role": message.role,
            "content": _admin_message_content(message.content),
            "timestamp": message.timestamp.isoformat(),
            "tool_calls": _admin_tool_calls(message.tool_calls, tool_results),
            "recalled_memory_ids": list(message.recalled_memory_ids),
            "source": message.source,
        }
        if message.tool_call_id:
            item["tool_call_id"] = message.tool_call_id
        if message.pin_id:
            item["pin_id"] = message.pin_id
        if message.stop_reason:
            item["stop_reason"] = message.stop_reason
        if message.reasoning_content:
            item["reasoning_content"] = message.reasoning_content
        if message.usage:
            item["usage"] = dict(message.usage)
        messages.append(item)
    return messages


@router.get("/memory/short-term")
async def get_short_term_memory(_: None = Depends(require_admin)) -> ApiResponse:
    agent = _require_agent()
    brain = _require_brain()
    stm = agent._short_term
    primary_tokens = stm.estimate_tokens(brain)
    tree_tokens = sum(node.token_estimate for node in stm.tree.nodes)
    estimated_tokens = primary_tokens + tree_tokens
    capacity = stm._max_tokens
    latest = getattr(agent.state, "last_main_response_usage", None)
    exact_tokens = int(latest.get("input_tokens", 0) or 0) if isinstance(latest, dict) else 0
    source = "provider" if exact_tokens > 0 else "estimated"
    tokens = exact_tokens if exact_tokens > 0 else estimated_tokens
    provider = (
        str(latest.get("provider") or brain.current_provider_name)
        if isinstance(latest, dict)
        else brain.current_provider_name
    )
    model = (
        str(latest.get("model") or brain.current_model)
        if isinstance(latest, dict)
        else brain.current_model
    )

    messages = _short_term_messages(stm)

    return {
        "token_watermark": {
            "tokens": tokens,
            "capacity": capacity,
            "ratio": tokens / capacity if capacity else 0,
            "source": source,
            "measured_at": (
                latest.get("measured_at")
                if source == "provider" and isinstance(latest, dict)
                else None
            ),
            "provider": provider,
            "model": model,
            "estimated_short_term_tokens": estimated_tokens,
        },
        "stats": {
            "message_count": len(stm.primary),
            "tree_node_count": len(stm.tree.nodes),
            "tree_tokens": tree_tokens,
            "pinned_count": len(stm.pinned_items),
            "thread_count": len(stm.threads),
            "tree_enabled": stm._tree_enabled,
            "compressing": stm._compressing,
        },
        "messages": messages,
        "tree": {"nodes": [node.to_dict() for node in stm.tree.nodes]},
        "pinned_items": [item.to_dict() for item in stm.pinned_items],
        "backfill": dict(stm.backfill_progress),
        "active_model": {"provider": brain.current_provider_name, "model": brain.current_model},
    }


@router.get("/memory/short-term/messages")
async def get_short_term_messages(_: None = Depends(require_admin)) -> ApiResponse:
    """Lightweight real-time view of the current short-term message tail.

    Intended for high-frequency observation polling: returns only the messages
    (no tree, token watermark, or pinned items), so the admin UI can refresh the
    tail in real time without re-serializing the full memory snapshot.
    """
    stm = _require_agent()._short_term
    return {"messages": _short_term_messages(stm)}


@router.post("/memory/pinned", status_code=201)
async def create_pinned_context(
    payload: PinnedContextPayload,
    request: Request,
    _: None = Depends(require_admin),
) -> ApiResponse:
    agent = _require_agent()
    label, content = payload.label.strip(), payload.content.strip()
    if not label or not content:
        raise HTTPException(status_code=422, detail=tr("api.admin.pin_content_required"))
    pin_id = f"admin-{secrets.token_hex(6)}"
    agent._short_term.pin(pin_id, label, content)
    snapshot_path = getattr(agent, "_snapshot_path", None)
    if snapshot_path is not None:
        agent._short_term.save_to_file(snapshot_path)
    _audit(request, "memory.pin", pin_id)
    return {"pinned": True, "pin_id": pin_id}


@router.delete("/memory/pinned/{pin_id}")
async def delete_pinned_context(
    pin_id: str,
    request: Request,
    _: None = Depends(require_admin),
) -> ApiResponse:
    agent = _require_agent()
    if not agent._short_term.unpin(pin_id):
        raise HTTPException(status_code=404, detail=tr("api.admin.pin_missing"))
    snapshot_path = getattr(agent, "_snapshot_path", None)
    if snapshot_path is not None:
        agent._short_term.save_to_file(snapshot_path)
    _audit(request, "memory.unpin", pin_id)
    return {"deleted": True}


@router.get("/memories")
async def search_memories(
    q: str = Query(min_length=1),
    category: str | None = None,
    limit: int = Query(default=20, ge=1, le=100),
    _: None = Depends(require_admin),
) -> ApiResponse:
    return {"memories": await _require_agent()._long_term.query(q, category=category, limit=limit)}


@router.patch("/memories/{memory_id}")
async def update_memory(
    memory_id: str,
    payload: MemoryPatch,
    request: Request,
    _: None = Depends(require_admin),
) -> ApiResponse:
    tags = None
    if payload.tags is not None:
        tags = list(dict.fromkeys(tag.strip() for tag in payload.tags if tag.strip()))
    await _require_agent()._long_term.update(memory_id, payload.content, tags=tags)
    _audit(request, "memory.update", memory_id)
    return {"updated": True}


@router.delete("/memories/{memory_id}")
async def delete_memory(
    memory_id: str,
    request: Request,
    _: None = Depends(require_admin),
) -> ApiResponse:
    await _require_agent()._long_term.delete(memory_id)
    _audit(request, "memory.delete", memory_id)
    return {"deleted": True}


@router.post("/memory/compress")
async def compress_memory(
    payload: ConfirmPayload,
    request: Request,
    _: None = Depends(require_admin),
) -> ApiResponse:
    _require_name_confirmation(payload.confirm_name)
    agent = _require_agent()
    compressed, saved = await agent._short_term.compress_all_now(
        _require_brain(),
        context_hint=tr("notification.admin_compress_hint"),
        agent_system_prompt=agent.current_system_prompt(),
        trigger="admin",
    )
    _audit(
        request,
        "memory.compress",
        "short_term",
        detail=f"compressed={compressed}, saved={saved}",
    )
    return {"messages_compressed": compressed, "memories_saved": saved}


@router.post("/memory/backfill", status_code=202)
async def backfill_memory(
    request: Request,
    max_leaves: int = Query(default=64, ge=1, le=512),
    _: None = Depends(require_admin),
) -> ApiResponse:
    stm = _require_agent()._short_term
    brain = _require_brain()
    if stm.backfill_progress.get("running"):
        raise HTTPException(status_code=409, detail=tr("api.admin.backfill_running"))
    stm.backfill_progress = {"running": True, "done": 0, "total": 0}
    task_locale = capture_locale()

    async def run() -> None:
        with locale_context(task_locale):
            try:
                await stm.backfill_tree_online(brain, max_leaves)
            finally:
                stm.backfill_progress["running"] = False

    task = asyncio.create_task(run(), name="admin-memory-backfill")
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)
    _audit(request, "memory.backfill", "memory_tree", detail=f"max_leaves={max_leaves}")
    return {"started": True}


@router.get("/backups")
async def backups(_: None = Depends(require_admin)) -> ApiResponse:
    from coworker.api.routes import list_backups

    return await list_backups()


@router.post("/backups/restore")
async def restore_admin_backup(
    payload: BackupRestorePayload,
    request: Request,
    _: None = Depends(require_admin),
) -> ApiResponse:
    from coworker.api.routes import RestoreBackupPayload, restore_backup

    if payload.mode == "full":
        _require_name_confirmation(payload.confirm_name)
    result = await restore_backup(
        RestoreBackupPayload(filename=payload.filename, mode=payload.mode)
    )
    _audit(request, "backup.restore", payload.filename, detail=f"mode={payload.mode}")
    return result


@router.get("/alarms")
async def list_alarms(_: None = Depends(require_admin)) -> ApiResponse:
    return {"alarms": _require_alarms().list()}


@router.post("/alarms")
async def create_alarm(
    payload: AlarmPayload,
    request: Request,
    _: None = Depends(require_admin),
) -> ApiResponse:
    alarm_id = f"alarm_{uuid.uuid4().hex[:8]}"
    await _require_alarms().set(
        alarm_id, payload.trigger_at, payload.message, payload.repeat_seconds
    )
    _audit(request, "alarm.create", alarm_id)
    return {"id": alarm_id}


@router.delete("/alarms/{alarm_id}")
async def cancel_alarm(
    alarm_id: str,
    request: Request,
    _: None = Depends(require_admin),
) -> ApiResponse:
    if not _require_alarms().cancel(alarm_id):
        raise HTTPException(status_code=404, detail=tr("api.admin.alarm_missing"))
    _audit(request, "alarm.cancel", alarm_id)
    return {"cancelled": True}


@router.get("/interactions")
async def get_interaction_history(
    limit: int = Query(default=100, ge=1, le=200),
    cursor: str | None = Query(default=None, max_length=512),
    event_type: str | None = Query(default=None, max_length=120),
    q: str = Query(default="", max_length=500),
    seq_start: int | None = Query(default=None, ge=0),
    seq_end: int | None = Query(default=None, ge=0),
    start_time: datetime | None = Query(default=None),
    end_time: datetime | None = Query(default=None),
    _: None = Depends(require_admin),
) -> ApiResponse:
    """Page through every interactions.jsonl shard without loading history at once.

    The first page starts at the newest record (or jumps directly to an
    requested sequence interval). Each following cursor moves toward birth
    across rotated ``interactions-000001.jsonl`` shards. Searching is
    deliberately byte-budgeted; a rare match may need several pages, but no
    single admin request can scan the whole lifetime log.
    """
    if seq_start is not None and seq_end is not None and seq_start > seq_end:
        raise HTTPException(status_code=400, detail=tr("api.admin.invalid_seq_range"))
    if (start_time is None) != (end_time is None):
        raise HTTPException(
            status_code=422,
            detail=tr("api.admin.incomplete_log_time_range"),
        )
    if start_time is not None and end_time is not None:
        try:
            time_span = end_time - start_time
        except TypeError:
            time_span = timedelta(days=-1)
        if time_span < timedelta(0):
            raise HTTPException(
                status_code=422,
                detail=tr("api.admin.invalid_log_time_range"),
            )
        if time_span > _INTERACTION_TIME_RANGE_LIMIT:
            raise HTTPException(
                status_code=422,
                detail=tr("api.admin.log_time_range_too_large"),
            )
    needle = q.strip().casefold()
    selected_type = (event_type or "").strip()

    # Interaction logs historically store runtime-local naive timestamps.  A
    # browser sends absolute instants, so convert them back to that legacy
    # storage clock before using LogStore's ISO string range index.
    log_start_time: datetime | None = None
    log_end_time: datetime | None = None

    store = _interaction_log_store(str(_interaction_logs_dir().resolve()))
    sequence = _interaction_sequence_summary(store)
    effective_seq_start = seq_start
    effective_seq_end = seq_end
    time_range: JsonObject | None = None
    if start_time is not None and end_time is not None:
        log_start_time = _runtime_local_naive(start_time)
        log_end_time = _runtime_local_naive(end_time)
        time_range = {
            "start_time": start_time.isoformat(),
            "end_time": end_time.isoformat(),
        }
        ranged_entries, _complete = store.read_time_range(log_start_time, log_end_time)
        ranged_sequences: list[int] = []
        for entry in ranged_entries:
            try:
                ranged_sequences.append(int(entry.get("seq", -1)))
            except (TypeError, ValueError, OverflowError):
                continue
        ranged_sequences = [seq for seq in ranged_sequences if seq >= 0]
        if not ranged_sequences:
            return {
                "events": [],
                "next_cursor": None,
                "has_more": False,
                "scanned_bytes": 0,
                "sequence": sequence,
                "time_range": time_range,
            }
        time_seq_start = min(ranged_sequences)
        time_seq_end = max(ranged_sequences)
        effective_seq_start = max(
            value for value in (effective_seq_start, time_seq_start) if value is not None
        )
        effective_seq_end = min(
            value for value in (effective_seq_end, time_seq_end) if value is not None
        )
        if effective_seq_start > effective_seq_end:
            return {
                "events": [],
                "next_cursor": None,
                "has_more": False,
                "scanned_bytes": 0,
                "sequence": sequence,
                "time_range": time_range,
            }

    start_time_iso = log_start_time.isoformat() if log_start_time is not None else None
    end_time_iso = log_end_time.isoformat() if log_end_time is not None else None

    def matches(entry: dict[str, Any]) -> bool:
        if effective_seq_start is not None or effective_seq_end is not None:
            try:
                entry_seq = int(entry.get("seq", -1))
            except (TypeError, ValueError, OverflowError):
                return False
            if (effective_seq_start is not None and entry_seq < effective_seq_start) or (
                effective_seq_end is not None and entry_seq > effective_seq_end
            ):
                return False
        if start_time_iso is not None and end_time_iso is not None:
            entry_time = str(entry.get("ts") or "")
            if not start_time_iso <= entry_time <= end_time_iso:
                return False
        if selected_type and str(entry.get("type") or "") != selected_type:
            return False
        if not needle:
            return True
        try:
            searchable = json.dumps(entry, ensure_ascii=False, default=str)
        except (TypeError, ValueError):
            searchable = str(entry)
        return needle in searchable.casefold()

    page = store.read_history_page(
        limit=limit,
        cursor=_decode_interaction_cursor(cursor),
        match=matches
        if (
            selected_type
            or needle
            or effective_seq_start is not None
            or effective_seq_end is not None
            or time_range is not None
        )
        else None,
        max_scan_bytes=_INTERACTION_PAGE_SCAN_BYTES,
        seq_start=effective_seq_start,
        seq_end=effective_seq_end,
    )
    response: ApiResponse = {
        "events": [_interaction_list_item(entry) for entry in page.entries],
        "next_cursor": _encode_interaction_cursor(page.cursor),
        "has_more": page.has_more,
        "scanned_bytes": page.scanned_bytes,
        "sequence": sequence,
    }
    if time_range is not None:
        response["time_range"] = time_range
    return response


@router.get("/interactions/{seq}")
async def get_interaction_detail(
    seq: int,
    _: None = Depends(require_admin),
) -> ApiResponse:
    """Fetch one expanded record only when an administrator asks to inspect it."""
    if seq < 0:
        raise HTTPException(status_code=404, detail=tr("api.admin.log_missing"))
    store = _interaction_log_store(str(_interaction_logs_dir().resolve()))
    entries, _complete = store.read_seq_range(seq, seq)
    entry = next((item for item in entries if item.get("seq") == seq), None)
    if entry is None:
        raise HTTPException(status_code=404, detail=tr("api.admin.log_missing"))
    state = [False]
    return {
        "entry": _bounded_interaction_value(entry, state),
        "truncated": state[0],
    }


@router.get("/diagnostics/tasks")
async def diagnostic_tasks(_: None = Depends(require_admin)) -> ApiResponse:
    from coworker.core.diagnostics import task_snapshot

    tasks = task_snapshot()
    return {"total": len(tasks), "pending": sum(not t["done"] for t in tasks), "tasks": tasks}


@router.get("/audit")
async def audit_log(
    limit: int = Query(default=100, ge=1, le=500),
    _: None = Depends(require_admin),
) -> ApiResponse:
    path = _audit_path()
    if not path.is_file():
        return {"entries": []}
    lines = path.read_text(encoding="utf-8").splitlines()[-limit:]
    entries = []
    for line in lines:
        try:
            entries.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return {"entries": list(reversed(entries))}


@router.get("/channel-traffic")
async def channel_traffic(
    limit: int = Query(default=300, ge=1, le=1000),
    direction: TrafficDirection | None = Query(default=None),
    status: TrafficStatus | None = Query(default=None),
    channel: str = Query(default="", max_length=80),
    _: None = Depends(require_admin),
) -> ApiResponse:
    store = ChannelTrafficStore(_channel_traffic_path())
    entries = await asyncio.to_thread(
        store.recent,
        limit,
        direction=direction,
        status=status,
        channel=channel,
    )
    return {"entries": entries}


@router.get("/identity")
async def get_identity(_: None = Depends(require_admin)) -> ApiResponse:
    identity = _require_agent()._identity
    return {
        "name": identity.name,
        "personality": identity.personality,
        "current_location": identity.current_location,
    }


@router.get("/system-prompt")
async def get_system_prompt(
    response: Response,
    _: None = Depends(require_admin),
) -> ApiResponse:
    agent = _require_agent()
    prompt = agent.current_system_prompt()
    section_previews = agent.current_system_prompt_sections()
    config = _require_config()
    inherited_config = _inherited_config or config
    snapshot = _require_admin_config_service().snapshot()
    agent_snapshot = snapshot.config.get("agent")
    desired_template_value = (
        str(agent_snapshot.get("system_prompt_template") or "")
        if isinstance(agent_snapshot, dict)
        else ""
    )
    active_template = resolve_system_prompt_template(
        config.agent.system_prompt_template
    )
    desired_template = resolve_system_prompt_template(desired_template_value)
    response.headers["Cache-Control"] = "no-store"
    return {
        "content": prompt,
        "characters": len(prompt),
        "lines": len(prompt.splitlines()),
        "active_template": active_template,
        "desired_template": desired_template,
        "inherited_template": resolve_system_prompt_template(
            inherited_config.agent.system_prompt_template
        ),
        "default_template": DEFAULT_SYSTEM_PROMPT_TEMPLATE,
        "variables": list(SYSTEM_PROMPT_VARIABLES),
        "content_variables": list(SYSTEM_PROMPT_CONTENT_VARIABLES),
        "section_previews": section_previews,
        "overridden": "agent.system_prompt_template" in snapshot.overridden_fields,
        "prompt_pending_restart": active_template != desired_template,
    }


@router.put("/identity")
async def put_identity(
    payload: IdentityPatch,
    request: Request,
    _: None = Depends(require_admin),
) -> ApiResponse:
    agent = _require_agent()
    identity = agent._identity
    identity.update(payload.model_dump(exclude_none=True))
    agent.refresh_system_prompt()
    _audit(request, "identity.update", identity.name or "unnamed")
    return await get_identity()


def _person_payload(person: Person) -> dict[str, object]:
    return {
        "person_id": person.person_id,
        "display_name": person.display_name,
        "notes": list(person.notes),
        "aliases": [alias.to_dict() for alias in person.aliases],
        "created_at": person.created_at,
        "updated_at": person.updated_at,
    }


def _aliases_from_patch(aliases: list[PersonAliasPatch]) -> list[PersonAlias]:
    return [
        PersonAlias(
            participant_id=a.participant_id,
            conversation_id=a.conversation_id,
            channel=a.channel,
            notes=list(a.notes),
        )
        for a in aliases
    ]


@router.get("/persons")
async def list_persons(_: None = Depends(require_admin)) -> ApiResponse:
    store, _cards = _require_persona()
    return {"persons": [_person_payload(p) for p in store.all_persons()]}


@router.post("/persons")
async def create_person(
    payload: PersonPatch,
    request: Request,
    _: None = Depends(require_admin),
) -> ApiResponse:
    store, _cards = _require_persona()
    person = store.create(
        display_name=payload.display_name or "",
        notes=payload.notes or [],
        aliases=_aliases_from_patch(payload.aliases or []),
    )
    _audit(request, "person.create", person.person_id)
    return _person_payload(person)


@router.get("/persons/{person_id}")
async def get_person(
    person_id: str,
    _: None = Depends(require_admin),
) -> ApiResponse:
    store, _cards = _require_persona()
    person = store.get(person_id)
    if person is None:
        raise HTTPException(
            status_code=404,
            detail=tr("api.admin.person_missing", person_id=person_id),
        )
    return _person_payload(person)


@router.patch("/persons/{person_id}")
async def patch_person(
    person_id: str,
    payload: PersonPatch,
    request: Request,
    _: None = Depends(require_admin),
) -> ApiResponse:
    store, _cards = _require_persona()
    if store.get(person_id) is None:
        raise HTTPException(
            status_code=404,
            detail=tr("api.admin.person_missing", person_id=person_id),
        )
    updated = store.update(
        person_id,
        display_name=payload.display_name,
        notes=payload.notes,
        aliases=_aliases_from_patch(payload.aliases) if payload.aliases is not None else None,
    )
    _audit(request, "person.update", person_id)
    return _person_payload(updated)  # type: ignore[arg-type]


@router.delete("/persons/{person_id}")
async def delete_person(
    person_id: str,
    request: Request,
    _: None = Depends(require_admin),
) -> ApiResponse:
    store, _cards = _require_persona()
    if not store.delete(person_id):
        raise HTTPException(
            status_code=404,
            detail=tr("api.admin.person_missing", person_id=person_id),
        )
    _audit(request, "person.delete", person_id)
    return {"deleted": True}


@router.post("/persons/{person_id}/merge")
async def merge_person(
    person_id: str,
    payload: PersonMergePayload,
    request: Request,
    _: None = Depends(require_admin),
) -> ApiResponse:
    store, _cards = _require_persona()
    keep = store.get(person_id)
    drop = store.get(payload.other_person_id)
    if keep is None or drop is None or person_id == payload.other_person_id:
        raise HTTPException(status_code=400, detail=tr("api.admin.person_merge_invalid"))
    merged = store.merge(person_id, payload.other_person_id)
    if merged is None:
        raise HTTPException(status_code=400, detail=tr("api.admin.person_merge_invalid"))
    _audit(request, "person.merge", person_id, detail=payload.other_person_id)
    return _person_payload(merged)


@router.get("/persons/{person_id}/card")
async def get_person_card(
    person_id: str,
    _: None = Depends(require_admin),
) -> ApiResponse:
    store, cards = _require_persona()
    person = store.get(person_id)
    if person is None:
        raise HTTPException(
            status_code=404,
            detail=tr("api.admin.person_missing", person_id=person_id),
        )
    return {"person_id": person_id, "content": cards.render(person)}


def _content_loader(kind: str) -> ContentLoader:
    loader = {
        "skills": _skill_loader,
        "palaces": _palace_loader,
        "subconscious": _mode_loader,
    }[kind]
    if loader is None:
        raise HTTPException(
            status_code=503,
            detail=tr("api.state.loader_not_ready", kind=kind),
        )
    return loader


def _content_filename(kind: str) -> str:
    return {"skills": "SKILL.md", "palaces": "PALACE.md", "subconscious": "MODE.md"}[kind]


def _content_path(kind: str, slug: str) -> Path:
    if kind not in _CONTENT_TYPES or not _SAFE_SLUG.fullmatch(slug) or slug in (".", ".."):
        raise HTTPException(
            status_code=400, detail=tr("api.admin.content_identity_invalid")
        )
    loader = _content_loader(kind)
    root = Path(loader._dir).resolve()
    path = (root / slug / _content_filename(kind)).resolve()
    if root not in path.parents:
        raise HTTPException(status_code=400, detail=tr("api.admin.content_path_invalid"))
    return path


_EDITABLE_CONTENT_SUFFIXES = {
    ".md",
    ".txt",
    ".py",
    ".js",
    ".mjs",
    ".cjs",
    ".ts",
    ".tsx",
    ".jsx",
    ".json",
    ".yaml",
    ".yml",
    ".toml",
    ".ini",
    ".cfg",
    ".sh",
    ".ps1",
    ".bat",
    ".css",
    ".html",
    ".xml",
    ".sql",
    ".csv",
}
_MAX_CONTENT_FILE_BYTES = 1_000_000


def _content_directory(kind: str, slug: str) -> Path:
    return _content_path(kind, slug).parent


def _content_file_path(kind: str, slug: str, relative: str) -> Path:
    root = _content_directory(kind, slug).resolve()
    normalized = relative.replace("\\", "/").strip("/")
    parts = normalized.split("/") if normalized else []
    if not parts or any(part in ("", ".", "..") for part in parts):
        raise HTTPException(status_code=400, detail=tr("api.admin.file_path_invalid"))
    path = (root / normalized).resolve()
    if root not in path.parents:
        raise HTTPException(status_code=400, detail=tr("api.admin.file_outside_root"))
    if path.suffix.lower() not in _EDITABLE_CONTENT_SUFFIXES:
        raise HTTPException(
            status_code=415, detail=tr("api.admin.file_type_unsupported")
        )
    return path


def _content_files(kind: str, slug: str) -> list[JsonObject]:
    root = _content_directory(kind, slug)
    if not root.is_dir():
        return []
    files: list[JsonObject] = []
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.is_symlink() or "__pycache__" in path.parts:
            continue
        try:
            resolved = path.resolve()
            if root.resolve() not in resolved.parents:
                continue
            stat = path.stat()
        except OSError:
            continue
        relative = path.relative_to(root).as_posix()
        files.append(
            {
                "path": relative,
                "name": path.name,
                "size_bytes": stat.st_size,
                "updated_at": datetime.fromtimestamp(stat.st_mtime).isoformat(),
                "editable": path.suffix.lower() in _EDITABLE_CONTENT_SUFFIXES
                and stat.st_size <= _MAX_CONTENT_FILE_BYTES,
                "primary": relative == _content_filename(kind),
            }
        )
    files.sort(key=lambda item: (not bool(item["primary"]), str(item["path"]).casefold()))
    return files


@router.get("/content/{kind}")
async def list_content(
    kind: Literal["skills", "palaces", "subconscious"],
    _: None = Depends(require_admin),
) -> ApiResponse:
    loader = _content_loader(kind)
    loader.load_all()
    root = Path(loader._dir)
    items = []
    if root.is_dir():
        for directory in sorted(p for p in root.iterdir() if p.is_dir() and p.name != "archived"):
            path = directory / _content_filename(kind)
            if path.is_file():
                parsed, warning = loader._parse(path)
                metadata: dict[str, object]
                if kind == "skills" and parsed is not None:
                    skill = cast("Skill", parsed)
                    summary = skill.description
                    metadata = {"version": skill.version}
                elif kind == "palaces" and parsed is not None:
                    palace = cast("Palace", parsed)
                    summary = palace.when_to_attach
                    metadata = {
                        "critical_skills": palace.critical_skills,
                        "related_skills": palace.related_skills,
                        "memory_tags": palace.memory_tags,
                    }
                elif kind == "subconscious" and parsed is not None:
                    mode = cast("SubconsciousMode", parsed)
                    summary = mode.purpose or mode.goal
                    metadata = {
                        "enabled": mode.enabled,
                        "protected": mode.protected,
                        "trigger": mode.trigger,
                    }
                else:
                    summary = ""
                    metadata = {}
                stat = path.stat()
                items.append(
                    {
                        "id": directory.name,
                        "path": str(path),
                        "raw": path.read_text(encoding="utf-8"),
                        "name": parsed.name if parsed is not None else directory.name,
                        "summary": summary,
                        "valid": parsed is not None,
                        "warning": warning or "",
                        "metadata": metadata,
                        "size_bytes": stat.st_size,
                        "updated_at": datetime.fromtimestamp(stat.st_mtime).isoformat(),
                        "files": _content_files(kind, directory.name),
                    }
                )
    return {"items": items}


@router.put("/content/{kind}/{slug}")
async def put_content(
    kind: Literal["skills", "palaces", "subconscious"],
    slug: str,
    payload: ContentPayload,
    request: Request,
    _: None = Depends(require_admin),
) -> ApiResponse:
    path = _content_path(kind, slug)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(payload.raw.rstrip() + "\n", encoding="utf-8")
    loader = _content_loader(kind)
    parsed, warning = loader._parse(tmp)
    if parsed is None:
        tmp.unlink(missing_ok=True)
        raise HTTPException(
            status_code=422,
            detail=warning or tr("api.admin.content_format_invalid"),
        )
    tmp.replace(path)
    loader.load_all()
    _audit(request, f"content.{kind}.save", slug)
    return {"saved": True, "path": str(path)}


@router.get("/content/{kind}/{slug}/files")
async def list_content_files(
    kind: Literal["skills", "palaces", "subconscious"],
    slug: str,
    _: None = Depends(require_admin),
) -> ApiResponse:
    directory = _content_directory(kind, slug)
    if not directory.is_dir():
        raise HTTPException(status_code=404, detail=tr("api.admin.content_root_missing"))
    return {"files": _content_files(kind, slug)}


@router.get("/content/{kind}/{slug}/files/{file_path:path}")
async def get_content_file(
    kind: Literal["skills", "palaces", "subconscious"],
    slug: str,
    file_path: str,
    _: None = Depends(require_admin),
) -> ApiResponse:
    path = _content_file_path(kind, slug, file_path)
    if not path.is_file() or path.is_symlink():
        raise HTTPException(status_code=404, detail=tr("api.admin.file_missing"))
    if path.stat().st_size > _MAX_CONTENT_FILE_BYTES:
        raise HTTPException(status_code=413, detail=tr("api.admin.file_too_large"))
    try:
        content = path.read_text(encoding="utf-8")
    except UnicodeDecodeError as e:
        raise HTTPException(status_code=415, detail=tr("api.admin.file_not_utf8")) from e
    return {"path": file_path, "content": content}


@router.put("/content/{kind}/{slug}/files/{file_path:path}")
async def put_content_file(
    kind: Literal["skills", "palaces", "subconscious"],
    slug: str,
    file_path: str,
    payload: ContentFilePayload,
    request: Request,
    _: None = Depends(require_admin),
) -> ApiResponse:
    path = _content_file_path(kind, slug, file_path)
    encoded = payload.content.encode("utf-8")
    if len(encoded) > _MAX_CONTENT_FILE_BYTES:
        raise HTTPException(status_code=413, detail=tr("api.admin.file_too_large"))
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_bytes(encoded)
    primary = path.name == _content_filename(kind) and path.parent == _content_directory(kind, slug)
    if primary:
        parsed, warning = _content_loader(kind)._parse(tmp)
        if parsed is None:
            tmp.unlink(missing_ok=True)
            raise HTTPException(
                status_code=422,
                detail=warning or tr("api.admin.content_format_invalid"),
            )
    tmp.replace(path)
    if primary:
        _content_loader(kind).load_all()
    _audit(request, f"content.{kind}.file.save", f"{slug}/{file_path}")
    return {"saved": True, "path": str(path)}


@router.delete("/content/{kind}/{slug}/files/{file_path:path}")
async def delete_content_file(
    kind: Literal["skills", "palaces", "subconscious"],
    slug: str,
    file_path: str,
    request: Request,
    _: None = Depends(require_admin),
) -> ApiResponse:
    path = _content_file_path(kind, slug, file_path)
    if path.name == _content_filename(kind) and path.parent == _content_directory(kind, slug):
        raise HTTPException(
            status_code=409,
            detail=tr("api.admin.main_file_delete_forbidden"),
        )
    if not path.is_file() or path.is_symlink():
        raise HTTPException(status_code=404, detail=tr("api.admin.file_missing"))
    path.unlink()
    parent = path.parent
    root = _content_directory(kind, slug)
    while parent != root:
        try:
            parent.rmdir()
        except OSError:
            break
        parent = parent.parent
    _audit(request, f"content.{kind}.file.delete", f"{slug}/{file_path}")
    return {"deleted": True}


@router.delete("/content/{kind}/{slug}")
async def delete_content(
    kind: Literal["skills", "palaces", "subconscious"],
    slug: str,
    request: Request,
    _: None = Depends(require_admin),
) -> ApiResponse:
    path = _content_path(kind, slug)
    if kind == "subconscious" and path.is_file():
        mode_loader = _mode_loader
        if mode_loader is None:
            raise HTTPException(
                status_code=503,
                detail=tr("api.state.loader_not_ready", kind="subconscious"),
            )
        parsed, _warning = mode_loader._parse(path)
        if parsed and parsed.protected:
            raise HTTPException(
                status_code=409,
                detail=tr("api.admin.protected_mode_delete_forbidden"),
            )
    if not path.is_file():
        raise HTTPException(status_code=404, detail=tr("api.admin.content_missing"))
    shutil.rmtree(path.parent)
    _content_loader(kind).load_all()
    _audit(request, f"content.{kind}.delete", slug)
    return {"deleted": True}
