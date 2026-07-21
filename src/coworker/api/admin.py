"""Authenticated management API for the local Coworker control room."""

from __future__ import annotations

import asyncio
import base64
import binascii
import json
import re
import secrets
import shutil
from collections.abc import Mapping
from datetime import datetime
from functools import lru_cache
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, TypedDict, cast

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from pydantic import BaseModel, Field, ValidationError

from coworker.agent.log_store import LogPageCursor, LogStore
from coworker.core.config import Config, _deep_merge, load_admin_overrides
from coworker.i18n import capture_locale, locale_context, tr

if TYPE_CHECKING:
    from coworker.agent.bubble import Bubble, BubbleStore
    from coworker.agent.loop import AgentLoop
    from coworker.agent.subconscious_mode import SubconsciousMode, SubconsciousModeLoader
    from coworker.brain.brain import Brain
    from coworker.palaces.loader import Palace, PalaceLoader
    from coworker.skills.loader import Skill, SkillLoader
    from coworker.tools.alarm_tools import AlarmManager
    from coworker.tools.reasoning_tools import Task, TaskStore

type JsonValue = None | bool | int | float | str | list[JsonValue] | dict[str, JsonValue]
type JsonObject = dict[str, JsonValue]
type ApiResponse = dict[str, object]
type ContentLoader = SkillLoader | PalaceLoader | SubconsciousModeLoader

router = APIRouter(prefix="/api/admin", tags=["admin"])

_agent: AgentLoop | None = None
_brain: Brain | None = None
_config: Config | None = None
_alarms: AlarmManager | None = None
_skill_loader: SkillLoader | None = None
_palace_loader: PalaceLoader | None = None
_mode_loader: SubconsciousModeLoader | None = None
_process_started_at: datetime = datetime.now()
_pending_restart: bool = False

_SECRET_PATHS = {
    "admin.token",
    "desktop_updates.admin_token",
    "llm.anthropic_api_key",
    "llm.openai_api_key",
    "llm.deepseek_api_key",
    "llm.qwen_api_key",
    "llm.zhipu_api_key",
    "llm.minimax_api_key",
    "wecom.secret",
}
_CONTENT_TYPES = {"skills", "palaces", "subconscious"}
_SAFE_SLUG = re.compile(r"^[\w.-]{1,80}$", re.UNICODE)
_SAFE_BUBBLE_ID = re.compile(r"^bbl_[A-Za-z0-9_-]{1,160}$")
_HOT_CONFIG_PATHS = {
    "llm.max_tokens",
    "agent.idle_sleep_seconds",
    "agent.passive_mode",
    "agent.inbox_batch_max",
    "agent.bubble_max_concurrent",
    "memory.auto_recall_enabled",
    "memory.auto_recall_relevance_threshold",
    "memory.auto_recall_limit",
    "memory.recent_activity_auto_recall_enabled",
    "memory.recent_activity_auto_recall_limit",
    "memory.recent_activity_auto_recall_relevance_threshold",
}


class ConfigPatch(BaseModel):
    changes: JsonObject = Field(default_factory=dict)
    secrets: dict[str, str | None] = Field(default_factory=dict)


class BootstrapPayload(BaseModel):
    provider_type: Literal["anthropic", "openai", "deepseek", "qwen", "zhipu", "minimax"]
    model: str = Field(min_length=1, max_length=120)
    api_key: str = Field(min_length=1, max_length=4096)
    base_url: str = Field(default="", max_length=2048)
    coworker_name: str = Field(default="", max_length=80)


class SummaryModelPatch(BaseModel):
    provider: str | None = None
    model: str | None = None
    thinking: bool | None = None


class VisionModelPatch(BaseModel):
    provider: str | None = None
    model: str | None = None
    thinking: bool | None = None


class ModelPatch(BaseModel):
    summary: SummaryModelPatch | None = None
    fallbacks: list[str] | None = None
    vision: VisionModelPatch | None = None


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


def setup_admin(
    *,
    agent: AgentLoop,
    brain: Brain,
    config: Config,
    alarm_manager: AlarmManager,
    skill_loader: SkillLoader,
    palace_loader: PalaceLoader,
    mode_loader: SubconsciousModeLoader,
) -> None:
    global \
        _agent, \
        _brain, \
        _config, \
        _alarms, \
        _skill_loader, \
        _palace_loader, \
        _mode_loader, \
        _pending_restart
    _agent = agent
    _brain = brain
    _config = config
    _alarms = alarm_manager
    _skill_loader = skill_loader
    _palace_loader = palace_loader
    _mode_loader = mode_loader
    _pending_restart = False


def _require_agent() -> AgentLoop:
    if _agent is None:
        raise HTTPException(status_code=503, detail="Agent not ready")
    return _agent


def _require_brain() -> Brain:
    if _brain is None:
        raise HTTPException(status_code=503, detail="Brain not ready")
    return _brain


def _require_config() -> Config:
    if _config is None:
        raise HTTPException(status_code=503, detail="Config not ready")
    return _config


def _require_alarms() -> AlarmManager:
    if _alarms is None:
        raise HTTPException(status_code=503, detail="Alarm manager not ready")
    return _alarms


def _require_task_store() -> TaskStore:
    store = _require_agent()._task_store
    if store is None:
        raise HTTPException(status_code=503, detail="Task store not ready")
    return store


def _require_bubble_store() -> BubbleStore:
    store = _require_agent()._bubble_store
    if store is None:
        raise HTTPException(status_code=503, detail="Bubble store not ready")
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
    # Compatibility: existing desktop update deployments already have a protected admin token.
    return _config.admin.token or _config.desktop_updates.admin_token


async def require_admin(
    authorization: str | None = Header(default=None),
) -> None:
    token = _token()
    if not token:
        raise HTTPException(status_code=503, detail="ADMIN__TOKEN 未配置")
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="缺少管理员令牌")
    if not secrets.compare_digest(authorization[7:], token):
        raise HTTPException(status_code=403, detail="管理员令牌无效")


def _audit_path() -> Path:
    logs_dir = _config.agent.logs_dir if _config is not None else "data/logs"
    return Path(logs_dir) / "admin_audit.jsonl"


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


def _set_path(data: JsonObject, dotted: str, value: JsonValue) -> None:
    parts = dotted.split(".")
    node: JsonValue = data
    for index, part in enumerate(parts[:-1]):
        if isinstance(node, dict):
            child = node.get(part)
            if child is None:
                child = [] if parts[index + 1].isdigit() else {}
                node[part] = child
        elif isinstance(node, list) and part.isdigit():
            item_index = int(part)
            if item_index >= len(node):
                raise ValueError(f"配置路径无效：{dotted}")
            child = node[item_index]
        else:
            raise ValueError(f"配置路径无效：{dotted}")
        node = child
    last = parts[-1]
    if isinstance(node, dict):
        node[last] = value
    elif isinstance(node, list) and last.isdigit() and int(last) < len(node):
        node[int(last)] = value
    else:
        raise ValueError(f"配置路径无效：{dotted}")


def _get_path(data: JsonObject, dotted: str, default: JsonValue = None) -> JsonValue:
    node: JsonValue = data
    for part in dotted.split("."):
        if isinstance(node, dict) and part in node:
            node = node[part]
        elif isinstance(node, list) and part.isdigit() and int(part) < len(node):
            node = node[int(part)]
        else:
            return default
    return node


def _remove_path(data: JsonObject, dotted: str) -> None:
    parts = dotted.split(".")
    node: JsonValue = data
    for part in parts[:-1]:
        if isinstance(node, dict) and part in node:
            node = node[part]
        elif isinstance(node, list) and part.isdigit() and int(part) < len(node):
            node = node[int(part)]
        else:
            return
    if isinstance(node, dict):
        node.pop(parts[-1], None)
    elif isinstance(node, list) and parts[-1].isdigit() and int(parts[-1]) < len(node):
        node.pop(int(parts[-1]))


def _write_json_atomic(path: Path, payload: JsonObject) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


class SecretStatus(TypedDict):
    configured: bool
    last4: str


def _masked_config() -> tuple[JsonObject, dict[str, SecretStatus], list[JsonObject]]:
    config = _require_config()
    desired_data = _deep_merge(
        config.model_dump(mode="json"),
        load_admin_overrides(config.admin.config_file),
    )
    desired = Config.model_validate(desired_data)
    data: JsonObject = desired.model_dump(mode="json")
    statuses: dict[str, SecretStatus] = {}
    for path in _SECRET_PATHS:
        value = str(_get_path(data, path, "") or "")
        statuses[path] = {"configured": bool(value), "last4": value[-4:] if value else ""}
        _set_path(data, path, "")
    llm = data.get("llm")
    providers = llm.get("managed_providers", []) if isinstance(llm, dict) else []
    if not isinstance(providers, list):
        providers = []
    for index, provider in enumerate(providers):
        if not isinstance(provider, dict):
            continue
        value = str(provider.get("api_key", "") or "")
        path = f"llm.managed_providers.{index}.api_key"
        statuses[path] = {"configured": bool(value), "last4": value[-4:] if value else ""}
        provider["api_key"] = ""

    managed_names = {spec.name for spec in desired.llm.managed_providers}
    effective_providers: list[JsonObject] = []
    for index, spec in enumerate(desired.llm.resolved_providers()):
        provider = cast(JsonObject, spec.model_dump(mode="json"))
        value = str(provider.get("api_key", "") or "")
        statuses[f"effective_providers.{index}.api_key"] = {
            "configured": bool(value),
            "last4": value[-4:] if value else "",
        }
        provider["api_key"] = ""
        provider["managed"] = spec.name in managed_names
        effective_providers.append(provider)
    return data, statuses, effective_providers


def _changed_paths(before: JsonValue, after: JsonValue, prefix: str = "") -> set[str]:
    if isinstance(before, dict) and isinstance(after, dict):
        paths: set[str] = set()
        for key in before.keys() | after.keys():
            child = f"{prefix}.{key}" if prefix else key
            paths.update(_changed_paths(before.get(key), after.get(key), child))
        return paths
    if before != after:
        return {prefix}
    return set()


def _assign_config_path(config: Config, path: str, source: Config) -> None:
    group, field = path.split(".", 1)
    setattr(getattr(config, group), field, getattr(getattr(source, group), field))


async def _apply_hot_config(
    current: Config,
    desired: Config,
    changed_paths: set[str],
) -> tuple[list[str], list[str]]:
    applied: list[str] = []
    restart = sorted(
        path
        for path in changed_paths
        if path not in _HOT_CONFIG_PATHS and not path.startswith("llm.managed_providers")
    )
    brain = _require_brain()

    if "llm.max_tokens" in changed_paths:
        brain.set_max_tokens(desired.llm.max_tokens)
        current.llm.max_tokens = desired.llm.max_tokens
        applied.append("llm.max_tokens")

    provider_changed = any(path.startswith("llm.managed_providers") for path in changed_paths)
    if provider_changed:
        from coworker.brain.factory import build_provider

        current_specs = {spec.name: spec for spec in current.llm.resolved_providers()}
        desired_specs = {spec.name: spec for spec in desired.llm.resolved_providers()}
        changed_names = {
            name
            for name in current_specs.keys() | desired_specs.keys()
            if current_specs.get(name) != desired_specs.get(name)
        }
        for name, spec in desired_specs.items():
            if name not in changed_names:
                continue
            provider = build_provider(
                spec.type,
                spec.api_key,
                base_url=spec.base_url or None,
                name=spec.name,
                default_model=spec.default_model,
            )
            await brain.upsert_provider(provider)
        removed = current_specs.keys() - desired_specs.keys()
        current.llm.managed_providers = list(desired.llm.managed_providers)
        applied.append("llm.managed_providers")
        if removed:
            restart.append("llm.managed_providers.removed")

    for path in sorted(changed_paths & _HOT_CONFIG_PATHS - {"llm.max_tokens"}):
        _assign_config_path(current, path, desired)
        applied.append(path)
        if path == "agent.bubble_max_concurrent":
            store = getattr(_require_agent(), "_bubble_store", None)
            if store is not None:
                store.max_concurrent = desired.agent.bubble_max_concurrent

    return sorted(set(applied)), sorted(set(restart))


def _require_name_confirmation(name: str) -> None:
    expected = _require_agent()._identity.name or "未命名"
    if name.strip() != expected:
        raise HTTPException(status_code=400, detail=f"请输入 Coworker 名称“{expected}”以确认")


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


_INTERACTION_PAGE_SCAN_BYTES = 2 * 1024 * 1024
_INTERACTION_PREVIEW_CHARS = 480
_INTERACTION_DETAIL_STRING_CHARS = 32_000
_INTERACTION_DETAIL_ITEMS = 200
_INTERACTION_DETAIL_DEPTH = 10


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
        raise HTTPException(status_code=400, detail="日志游标无效")
    try:
        padded = value + "=" * (-len(value) % 4)
        payload = json.loads(base64.urlsafe_b64decode(padded).decode("utf-8"))
        path = payload["p"]
        offset = payload["o"]
        before_seq = payload.get("s")
    except (binascii.Error, KeyError, TypeError, ValueError, UnicodeDecodeError):
        raise HTTPException(status_code=400, detail="日志游标无效") from None
    if (
        not isinstance(path, str)
        or not path
        or Path(path).name != path
        or type(offset) is not int
        or offset < 0
        or (before_seq is not None and (type(before_seq) is not int or before_seq < 0))
    ):
        raise HTTPException(status_code=400, detail="日志游标无效")
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
        "operation",
        "stop_reason",
        "is_error",
        "thinking",
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
        return "…（嵌套内容已截断）"
    if value is None or isinstance(value, bool | int | float):
        return cast(JsonValue, value)
    if isinstance(value, str):
        if len(value) > _INTERACTION_DETAIL_STRING_CHARS:
            state[0] = True
            return value[:_INTERACTION_DETAIL_STRING_CHARS] + "…（字段已截断）"
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
                result["…"] = "更多字段已截断"
                break
            result[str(key)] = _bounded_interaction_value(item, state, depth + 1)
        return result
    return str(value)


@lru_cache(maxsize=512)
def _read_bubble_log_cached(path: str, _mtime_ns: int, _size: int) -> list[dict[str, object]]:
    entries: list[dict[str, object]] = []
    try:
        for line in Path(path).read_text(encoding="utf-8").splitlines():
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


def _read_bubble_log(path: Path) -> list[dict[str, object]]:
    try:
        stat = path.stat()
    except OSError:
        return []
    return _read_bubble_log_cached(str(path.resolve()), stat.st_mtime_ns, stat.st_size)


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
    entries = _read_bubble_log(path)
    meta = next((entry for entry in reversed(entries) if entry.get("__meta__")), None)
    if meta is None:
        return None
    first = entries[0] if entries else meta
    bubble_id = str(meta.get("id") or path.stem)
    mode = path.stem[len(bubble_id) + 1 :] if path.stem.startswith(f"{bubble_id}_") else ""
    result = ""
    max_cycles = 0
    for entry in entries:
        if entry.get("type") == "message_in" and not max_cycles:
            match = re.search(r"最多执行\s*(\d+)\s*轮", str(entry.get("content") or ""))
            max_cycles = int(match.group(1)) if match else 0
        if entry.get("type") == "tool_call" and entry.get("name") == "bubble_done":
            arguments = entry.get("arguments")
            if isinstance(arguments, dict) and not arguments.get("checkpoint"):
                result = str(arguments.get("result") or result)
    return {
        "id": bubble_id,
        "log_id": path.stem,
        "mode": mode,
        "goal": str(meta.get("goal") or "目标未记录"),
        "status": str(meta.get("status") or "done"),
        "provider": str(meta.get("provider") or ""),
        "model": str(meta.get("model") or ""),
        "cycles_used": _as_int(meta.get("cycles_used")),
        "max_cycles": _as_int(meta.get("max_cycles"), max_cycles),
        "participant_id": str(meta.get("participant_id") or ""),
        "conversation_id": str(meta.get("conversation_id") or ""),
        "handoff_transparency": bool(meta.get("handoff_transparency")),
        "resume_count": _as_int(meta.get("resume_count")),
        "palaces": cast(JsonValue, meta.get("palaces") or []),
        "created_at": str(first.get("ts") or ""),
        "finished_at": str(meta.get("ts") or ""),
        "elapsed_seconds": _as_float(meta.get("elapsed_seconds")),
        "result": result,
        "error": str(meta.get("error") or ""),
    }


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
            "详细日志尚未写入，刷新后可重试。"
            if bubble.status == "running"
            else "该条历史未保留详细日志，当前显示内存快照。"
        ),
        "ts": bubble.created_at.isoformat(),
    }


@router.post("/session/verify")
async def verify_session(_: None = Depends(require_admin)) -> ApiResponse:
    return {"ok": True, "name": _require_agent()._identity.name}


@router.get("/bootstrap")
async def bootstrap_status(_: None = Depends(require_admin)) -> ApiResponse:
    """Describe whether this installation still needs its first model connection."""

    brain = _require_brain()
    from coworker.brain.factory import available_models, available_types

    providers: list[dict[str, object]] = []
    for provider_type in available_types():
        providers.append({"type": provider_type, "models": available_models(provider_type)})
    return {
        "required": brain.active_provider is None,
        "active_provider": brain.current_provider_name,
        "active_model": brain.current_model,
        "providers": providers,
    }


@router.post("/bootstrap", status_code=202)
async def complete_bootstrap(
    payload: BootstrapPayload,
    request: Request,
    _: None = Depends(require_admin),
) -> ApiResponse:
    """Persist the first provider connection and restart into normal operation."""

    global _pending_restart
    config = _require_config()
    brain = _require_brain()
    if brain.active_provider is not None:
        raise HTTPException(status_code=409, detail="初始化已完成，请在运行设置中修改模型连接")

    from coworker.brain.factory import build_provider

    provider_type = payload.provider_type.strip()
    model = payload.model.strip()
    api_key = payload.api_key.strip()
    base_url = payload.base_url.strip()
    provider = build_provider(
        provider_type,
        api_key,
        base_url=base_url or None,
        name=provider_type,
        default_model=model,
    )
    if not provider.supports_tool_use(model):
        raise HTTPException(
            status_code=422,
            detail=f"模型 {model!r} 不在 {provider_type} 的可用工具模型列表中",
        )

    path = Path(config.admin.config_file)
    current_overrides = load_admin_overrides(path)
    changes: JsonObject = {
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
                }
            ],
        },
        "memory": {"mem0_llm_provider": provider_type, "mem0_llm_model": model},
    }
    next_overrides = _deep_merge(current_overrides, changes)
    try:
        Config.model_validate(_deep_merge(config.model_dump(mode="json"), next_overrides))
    except ValidationError as e:
        raise HTTPException(status_code=422, detail=json.loads(e.json())) from e

    _write_json_atomic(path, next_overrides)
    if payload.coworker_name.strip():
        identity = _require_agent()._identity
        identity._dir.mkdir(parents=True, exist_ok=True)
        (identity._dir / "name.txt").write_text(payload.coworker_name.strip(), encoding="utf-8")
        identity.load()

    _pending_restart = True
    _audit(request, "bootstrap.complete", f"{provider_type}/{model}")
    asyncio.get_running_loop().call_later(0.5, _require_agent().request_restart)
    return {"accepted": True, "restarting": True}


@router.get("/overview")
async def overview(_: None = Depends(require_admin)) -> ApiResponse:
    agent = _require_agent()
    brain = _require_brain()
    tasks = agent._task_store.list() if agent._task_store else []
    bubbles = agent._bubble_store.list_active() if agent._bubble_store else []
    memory_count = await agent._long_term.count()
    stm = agent._short_term
    return {
        "status": {
            "is_running": agent.state.is_running,
            "is_sleeping": agent.state.is_sleeping,
            "provider": brain.current_provider_name,
            "model": brain.current_model,
            "cycle_count": agent.state.cycle_count,
            "started_at": _process_started_at.isoformat(),
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
        "pending_restart": _pending_restart,
    }


@router.get("/config")
async def get_config(_: None = Depends(require_admin)) -> ApiResponse:
    config = _require_config()
    data, statuses, effective_providers = _masked_config()
    return {
        "config": data,
        "effective_providers": effective_providers,
        "secret_status": statuses,
        "hot_reloadable": sorted(_HOT_CONFIG_PATHS | {"llm.managed_providers"}),
        "override_path": config.admin.config_file,
        "pending_restart": _pending_restart,
        "sources": {
            "base": ".env / environment",
            "providers": config.llm.providers_file,
            "override": config.admin.config_file,
        },
    }


@router.patch("/config")
async def patch_config(
    payload: ConfigPatch,
    request: Request,
    _: None = Depends(require_admin),
) -> ApiResponse:
    global _pending_restart
    config = _require_config()
    path = Path(config.admin.config_file)
    current_overrides = load_admin_overrides(path)
    safe_changes = json.loads(json.dumps(payload.changes))
    # GET 返回的密钥字段固定为空；普通表单保存不得把这些空串误解释为清除。
    # 替换/清除密钥只能通过显式的 ``secrets`` 通道完成。
    for secret_path in _SECRET_PATHS:
        _remove_path(safe_changes, secret_path)
    next_overrides = _deep_merge(current_overrides, safe_changes)
    effective: JsonObject = config.model_dump(mode="json")

    for secret_path, value in payload.secrets.items():
        if secret_path not in _SECRET_PATHS and not re.fullmatch(
            r"llm\.managed_providers\.\d+\.api_key", secret_path
        ):
            raise HTTPException(status_code=400, detail=f"不可写的密钥路径：{secret_path}")
        _set_path(next_overrides, secret_path, value or "")

    # Blank managed-provider keys preserve only the previous Admin overlay key.
    # External providers remain owned by .env/providers.json and are never copied here.
    managed = _get_path(next_overrides, "llm.managed_providers")
    if isinstance(managed, list):
        old = {p.name: p for p in config.llm.managed_providers}
        for item in managed:
            if not isinstance(item, dict):
                continue
            name = item.get("name")
            if isinstance(name, str) and not item.get("api_key") and name in old:
                item["api_key"] = old[name].api_key

    try:
        before_config = Config.model_validate(_deep_merge(effective, current_overrides))
        desired_config = Config.model_validate(_deep_merge(effective, next_overrides))
    except ValidationError as e:
        raise HTTPException(status_code=422, detail=json.loads(e.json())) from e
    changed_paths = _changed_paths(
        before_config.model_dump(mode="json"),
        desired_config.model_dump(mode="json"),
    )
    try:
        applied_now, requires_restart = await _apply_hot_config(
            config,
            desired_config,
            changed_paths,
        )
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"运行时应用失败：{e}") from e
    _write_json_atomic(path, next_overrides)
    _pending_restart = _pending_restart or bool(requires_restart)
    _audit(
        request,
        "config.update",
        str(path),
        detail=f"hot={','.join(applied_now)}; restart={','.join(requires_restart)}",
    )
    return {
        "saved": True,
        "pending_restart": _pending_restart,
        "applied_now": applied_now,
        "requires_restart": requires_restart,
    }


@router.get("/model")
async def get_model(_: None = Depends(require_admin)) -> ApiResponse:
    return cast(ApiResponse, _require_brain().model_config_snapshot())


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
    _audit(request, "model.runtime.update", "model_config")
    return cast(ApiResponse, snapshot)


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
        raise HTTPException(status_code=404, detail="任务不存在")
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
        raise HTTPException(status_code=404, detail="任务不存在")
    _audit(request, "task.delete", task_id)
    return {"deleted": True}


@router.get("/bubbles")
async def list_bubbles(
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    _: None = Depends(require_admin),
) -> ApiResponse:
    store = _require_bubble_store()
    subconscious_dir = _subconscious_logs_dir()
    live = [
        _bubble_dict(b)
        for b in store.list_active() + list(store._history)
        if not any(subconscious_dir.glob(f"{b.id}_*.jsonl"))
    ]
    by_id = {str(item["id"]): item for item in live}
    log_dir = _bubble_logs_dir()
    if log_dir.is_dir():
        for path in sorted(log_dir.glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True):
            if path.stem in by_id:
                continue
            summary = _bubble_log_summary(path)
            if summary is not None:
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
        raise HTTPException(status_code=404, detail="Bubble 记录不存在")
    path = _bubble_logs_dir() / f"{bubble_id}.jsonl"
    if not path.is_file():
        bubble = _require_bubble_store().get(bubble_id)
        if bubble is None:
            raise HTTPException(status_code=404, detail="Bubble 记录不存在")
        return {"bubble_id": bubble_id, "events": [_bubble_snapshot(bubble)]}
    return {"bubble_id": bubble_id, "events": _read_bubble_log(path)}


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
    if log_dir.is_dir():
        for path in log_dir.glob("*.jsonl"):
            summary = _bubble_log_summary(path)
            if summary is not None:
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
        raise HTTPException(status_code=404, detail="潜意识记录不存在")
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
            raise HTTPException(status_code=404, detail="潜意识记录不存在")
        return {"bubble_id": log_id, "events": [_bubble_snapshot(bubble)]}
    return {"bubble_id": log_id, "events": _read_bubble_log(path)}


@router.post("/bubbles/{bubble_id}/cancel")
async def cancel_bubble(
    bubble_id: str,
    request: Request,
    _: None = Depends(require_admin),
) -> JsonObject:
    bubble = _require_bubble_store().get(bubble_id)
    if bubble is None:
        raise HTTPException(status_code=404, detail="Bubble 不存在")
    if bubble.is_terminal():
        raise HTTPException(status_code=409, detail="Bubble 已结束")
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


@router.post("/memory/pinned", status_code=201)
async def create_pinned_context(
    payload: PinnedContextPayload,
    request: Request,
    _: None = Depends(require_admin),
) -> ApiResponse:
    agent = _require_agent()
    label, content = payload.label.strip(), payload.content.strip()
    if not label or not content:
        raise HTTPException(status_code=422, detail="标题和内容不能为空")
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
        raise HTTPException(status_code=404, detail="固定上下文不存在")
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
        agent_system_prompt=agent._prompt_builder.build(),
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
        raise HTTPException(status_code=409, detail="记忆树回溯正在进行")
    stm.backfill_progress = {"running": True, "done": 0, "total": 0}
    task_locale = capture_locale()

    async def run() -> None:
        with locale_context(task_locale):
            try:
                await stm.backfill_tree_online(brain, max_leaves)
            finally:
                stm.backfill_progress["running"] = False

    asyncio.create_task(run(), name="admin-memory-backfill")
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
    import uuid

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
        raise HTTPException(status_code=404, detail="闹钟不存在")
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
        raise HTTPException(status_code=400, detail="起始序列不能大于结束序列")
    needle = q.strip().casefold()
    selected_type = (event_type or "").strip()

    def matches(entry: dict[str, Any]) -> bool:
        if seq_start is not None or seq_end is not None:
            try:
                entry_seq = int(entry.get("seq", -1))
            except (TypeError, ValueError, OverflowError):
                return False
            if (seq_start is not None and entry_seq < seq_start) or (
                seq_end is not None and entry_seq > seq_end
            ):
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

    store = _interaction_log_store(str(_interaction_logs_dir().resolve()))
    sequence = _interaction_sequence_summary(store)
    page = store.read_history_page(
        limit=limit,
        cursor=_decode_interaction_cursor(cursor),
        match=matches
        if selected_type or needle or seq_start is not None or seq_end is not None
        else None,
        max_scan_bytes=_INTERACTION_PAGE_SCAN_BYTES,
        seq_start=seq_start,
        seq_end=seq_end,
    )
    return {
        "events": [_interaction_list_item(entry) for entry in page.entries],
        "next_cursor": _encode_interaction_cursor(page.cursor),
        "has_more": page.has_more,
        "scanned_bytes": page.scanned_bytes,
        "sequence": sequence,
    }


@router.get("/interactions/{seq}")
async def get_interaction_detail(
    seq: int,
    _: None = Depends(require_admin),
) -> ApiResponse:
    """Fetch one expanded record only when an administrator asks to inspect it."""
    if seq < 0:
        raise HTTPException(status_code=404, detail="日志记录不存在")
    store = _interaction_log_store(str(_interaction_logs_dir().resolve()))
    entries, _complete = store.read_seq_range(seq, seq)
    entry = next((item for item in entries if item.get("seq") == seq), None)
    if entry is None:
        raise HTTPException(status_code=404, detail="日志记录不存在")
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


@router.get("/identity")
async def get_identity(_: None = Depends(require_admin)) -> ApiResponse:
    identity = _require_agent()._identity
    return {
        "name": identity.name,
        "personality": identity.personality,
        "goals": identity.goals,
        "life_story": identity.life_story,
        "current_location": identity.current_location,
    }


@router.put("/identity")
async def put_identity(
    payload: dict[str, str],
    request: Request,
    _: None = Depends(require_admin),
) -> ApiResponse:
    identity = _require_agent()._identity
    mapping = {
        "name": "name.txt",
        "personality": "personality.md",
        "goals": "goals.md",
        "life_story": "life_story.md",
        "current_location": "current_location.txt",
    }
    identity._dir.mkdir(parents=True, exist_ok=True)
    for key, filename in mapping.items():
        if key in payload:
            (identity._dir / filename).write_text(str(payload[key]).strip(), encoding="utf-8")
    identity.load()
    _audit(request, "identity.update", identity.name or "unnamed")
    return await get_identity()


def _content_loader(kind: str) -> ContentLoader:
    loader = {
        "skills": _skill_loader,
        "palaces": _palace_loader,
        "subconscious": _mode_loader,
    }[kind]
    if loader is None:
        raise HTTPException(status_code=503, detail=f"{kind} loader not ready")
    return loader


def _content_filename(kind: str) -> str:
    return {"skills": "SKILL.md", "palaces": "PALACE.md", "subconscious": "MODE.md"}[kind]


def _content_path(kind: str, slug: str) -> Path:
    if kind not in _CONTENT_TYPES or not _SAFE_SLUG.fullmatch(slug) or slug in (".", ".."):
        raise HTTPException(status_code=400, detail="内容类型或名称无效")
    loader = _content_loader(kind)
    root = Path(loader._dir).resolve()
    path = (root / slug / _content_filename(kind)).resolve()
    if root not in path.parents:
        raise HTTPException(status_code=400, detail="内容路径无效")
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
        raise HTTPException(status_code=400, detail="文件路径无效")
    path = (root / normalized).resolve()
    if root not in path.parents:
        raise HTTPException(status_code=400, detail="文件路径超出能力目录")
    if path.suffix.lower() not in _EDITABLE_CONTENT_SUFFIXES:
        raise HTTPException(status_code=415, detail="该文件类型不支持在线编辑")
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
        raise HTTPException(status_code=422, detail=warning or "内容格式无效")
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
        raise HTTPException(status_code=404, detail="能力目录不存在")
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
        raise HTTPException(status_code=404, detail="文件不存在")
    if path.stat().st_size > _MAX_CONTENT_FILE_BYTES:
        raise HTTPException(status_code=413, detail="文件超过 1 MB，无法在线编辑")
    try:
        content = path.read_text(encoding="utf-8")
    except UnicodeDecodeError as e:
        raise HTTPException(status_code=415, detail="该文件不是 UTF-8 文本") from e
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
        raise HTTPException(status_code=413, detail="文件超过 1 MB，无法在线编辑")
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_bytes(encoded)
    primary = path.name == _content_filename(kind) and path.parent == _content_directory(kind, slug)
    if primary:
        parsed, warning = _content_loader(kind)._parse(tmp)
        if parsed is None:
            tmp.unlink(missing_ok=True)
            raise HTTPException(status_code=422, detail=warning or "内容格式无效")
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
        raise HTTPException(status_code=409, detail="主定义文件不能在文件树中删除")
    if not path.is_file() or path.is_symlink():
        raise HTTPException(status_code=404, detail="文件不存在")
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
            raise HTTPException(status_code=503, detail="subconscious loader not ready")
        parsed, _warning = mode_loader._parse(path)
        if parsed and parsed.protected:
            raise HTTPException(status_code=409, detail="受保护的潜意识模式不可删除")
    if not path.is_file():
        raise HTTPException(status_code=404, detail="内容不存在")
    shutil.rmtree(path.parent)
    _content_loader(kind).load_all()
    _audit(request, f"content.{kind}.delete", slug)
    return {"deleted": True}
