from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
from collections import OrderedDict
from datetime import datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from fastapi import APIRouter, Header, HTTPException, Request
from loguru import logger
from pydantic import BaseModel

from coworker.channels.access import ChannelAccessDeniedError
from coworker.channels.inbound import InboundEnvelope
from coworker.core.communication_tokens import (
    PRIMARY_TOKEN_NAME,
    participant_id_for_token_name,
    validate_token_name,
)
from coworker.core.model_config import RuntimeModelConfig, write_runtime_model_config
from coworker.core.types import IncomingEvent, SummaryResult
from coworker.i18n import capture_locale, locale_context, tr
from coworker.memory.short_term import ShortTermMemory

if TYPE_CHECKING:
    from coworker.agent.inbox_watcher import InboxWatcher
    from coworker.agent.loop import AgentLoop
    from coworker.agent.usage_stats import UsageStatsCollector
    from coworker.brain.brain import Brain
    from coworker.channels.registry import ChannelRegistry

router = APIRouter()

_inbox: InboxWatcher | None = None
_agent: AgentLoop | None = None
_brain: Brain | None = None
_usage_stats: UsageStatsCollector | None = None
_model_config_path: Path = Path("data/model_runtime_config.json")
_communication_token = ""
# 只有管理员显式设置了 API__COMMUNICATION_TOKEN 才认为通信令牌“已配置”。
# _communication_token 仍可携带管理员令牌回退值，供 Desktop 兼容校验。
_communication_token_explicit = False
_extra_communication_tokens: dict[str, str] = {}
# 搭档信道（coworker: 前缀发送方）专用的入站令牌与自身 peer id。
_coworker_inbound_token = ""
_coworker_self_id = ""
_channels: ChannelRegistry | None = None

# 已处理过的入站 desktop 消息 message_id 集合，用于对 bridge 出站"至少一次"重试做幂等去重：
# bridge 在 HTTP POST 成功但响应丢失/超时会重发同一 message_id，这里命中后直接 ack 且不再入队，
# 避免同一条消息被 agent 处理多次。按 LRU 留存最近若干条，防止无界增长。
_DESKTOP_DEDUP_LIMIT = 4096
_seen_desktop_message_ids: OrderedDict[str, None] = OrderedDict()


def _remember_desktop_message_id(message_id: str) -> bool:
    """记录入站 desktop 消息 message_id，返回 True 表示首次见到、False 表示重复。"""
    if not message_id:
        return True
    if message_id in _seen_desktop_message_ids:
        # 命中重复：挪到队尾保持 LRU 顺序。
        _seen_desktop_message_ids.move_to_end(message_id)
        return False
    _seen_desktop_message_ids[message_id] = None
    while len(_seen_desktop_message_ids) > _DESKTOP_DEDUP_LIMIT:
        _seen_desktop_message_ids.popitem(last=False)
    return True


_PROFILE_README_INTERVAL = timedelta(days=14)  # 档案自述更新提醒间隔；默认两周一次
_profile_readme_last_reminded_at: datetime | None = None


def setup(
    inbox: InboxWatcher | None,
    agent: AgentLoop,
    brain: Brain,
    inbox_dir: str = "data/inbox",
    usage_stats: UsageStatsCollector | None = None,
    model_config_path: str | Path = "data/model_runtime_config.json",
    communication_token: str = "",
    channels: ChannelRegistry | None = None,
    communication_token_explicit: bool | None = None,
    extra_communication_tokens: dict[str, str] | None = None,
    coworker_inbound_token: str = "",
    coworker_self_id: str = "",
) -> None:
    global _inbox, _agent, _brain, _usage_stats, _model_config_path
    global _communication_token, _communication_token_explicit, _channels
    global _coworker_inbound_token, _coworker_self_id
    _inbox = inbox
    _agent = agent
    _brain = brain
    _usage_stats = usage_stats
    _model_config_path = Path(model_config_path)
    _communication_token = communication_token.strip()
    _communication_token_explicit = (
        bool(_communication_token)
        if communication_token_explicit is None
        else communication_token_explicit
    )
    _channels = channels
    _coworker_inbound_token = coworker_inbound_token.strip()
    _coworker_self_id = coworker_self_id
    update_communication_token_table(extra_communication_tokens or {}, sync_store=False)


class AttachmentSchema(BaseModel):
    filename: str
    media_type: str
    data: str  # base64 encoded


class MessagePayload(BaseModel):
    sender_id: str
    content: str = ""
    conversation_id: str | None = None
    attachments: list[AttachmentSchema] = []
    message_id: str | None = None
    protocol_version: int | None = None
    request_id: str | None = None
    created_at: str | None = None
    type: str | None = None
    payload: dict[str, Any] | None = None
    # 搭档信道的自我宣告（回呼地址/令牌/展示名）；仅在 sender_id 以 coworker:
    # 开头时由信道消费，不进入模型上下文。
    coworker_peer: dict[str, Any] | None = None


def _bearer_token(authorization: str | None) -> str | None:
    if not authorization:
        return None
    scheme, _, remainder = authorization.partition(" ")
    if scheme.lower() != "bearer":
        return None
    token = remainder.strip()
    return token or None


def _token_matches(provided: str, expected: str) -> bool:
    if not expected:
        return False
    return hmac.compare_digest(
        hashlib.sha256(provided.encode("utf-8")).digest(),
        hashlib.sha256(expected.encode("utf-8")).digest(),
    )


def _normalized_extra_tokens(tokens: dict[str, str] | None) -> dict[str, str]:
    cleaned: dict[str, str] = {}
    for raw_name, raw_secret in (tokens or {}).items():
        try:
            name = validate_token_name(str(raw_name))
        except ValueError:
            continue
        secret = str(raw_secret or "").strip()
        if secret:
            cleaned[name] = secret
    return cleaned


def communication_token_table() -> dict[str, str]:
    table: dict[str, str] = {}
    if _communication_token:
        table[PRIMARY_TOKEN_NAME] = _communication_token
    table.update(_extra_communication_tokens)
    return table


def resolve_communication_token_name(authorization: str | None) -> str:
    provided = _bearer_token(authorization)
    if provided is None:
        raise HTTPException(
            status_code=401,
            detail=tr("api.auth.communication_token_invalid"),
        )
    table = communication_token_table()
    if not table:
        raise HTTPException(
            status_code=503,
            detail=tr("api.auth.communication_token_unconfigured"),
        )
    if _communication_token and _token_matches(provided, _communication_token):
        return PRIMARY_TOKEN_NAME
    for name, secret in _extra_communication_tokens.items():
        if _token_matches(provided, secret):
            return name
    raise HTTPException(
        status_code=401,
        detail=tr("api.auth.communication_token_invalid"),
    )


def openai_participant_id(authorization: str | None) -> str:
    return participant_id_for_token_name(resolve_communication_token_name(authorization))


def verify_communication_authorization(authorization: str | None) -> None:
    resolve_communication_token_name(authorization)


def _verify_coworker_peer_authorization(authorization: str | None) -> None:
    """Authenticate ``coworker:`` senders: the dedicated inbound token or the usual table."""
    if _coworker_inbound_token:
        provided = _bearer_token(authorization)
        if provided is not None and _token_matches(provided, _coworker_inbound_token):
            return
        if communication_token_table():
            # 主令牌/额外令牌对搭档消息同样有效；都不匹配时按无效令牌拒绝。
            verify_communication_authorization(authorization)
            return
        raise HTTPException(
            status_code=401,
            detail=tr("api.auth.communication_token_invalid"),
        )
    verify_communication_authorization(authorization)


def update_communication_token(token: str, explicit: bool | None = None) -> None:
    """Atomically replace the primary communication token used by existing ASGI routes."""

    global _communication_token, _communication_token_explicit
    _communication_token = token.strip()
    _communication_token_explicit = (
        bool(_communication_token) if explicit is None else explicit
    )


def update_communication_token_table(
    tokens: dict[str, str],
    *,
    sync_store: bool = True,
) -> None:
    """Replace extra communication tokens (not the primary Desktop/Relay token)."""

    global _extra_communication_tokens
    _extra_communication_tokens = _normalized_extra_tokens(tokens)
    if sync_store:
        from coworker.api.openai_compat import sync_extra_token_store

        sync_extra_token_store(_extra_communication_tokens)


def extra_communication_tokens() -> dict[str, str]:
    return dict(_extra_communication_tokens)


def communication_authorization_matches(authorization: str | None) -> bool:
    try:
        resolve_communication_token_name(authorization)
    except HTTPException:
        return False
    return True


def communication_token_required() -> bool:
    """Return True when an administrator explicitly configured API__COMMUNICATION_TOKEN."""

    return _communication_token_explicit


def is_authenticated_relay_request(request: Request) -> bool:
    relay = request.scope.get("state", {}).get("coworker_relay")
    return isinstance(relay, dict) and relay.get("authenticated_tunnel") is True


class SwitchModelPayload(BaseModel):
    provider: str
    model_id: str = ""  # 省略则使用该 provider 实例配置的 default_model


class SummaryModelConfigPayload(BaseModel):
    provider: str | None = None
    model: str | None = None
    thinking: bool | None = None
    thinking_effort: str | None = None


class VisionModelConfigPayload(BaseModel):
    provider: str | None = None
    model: str | None = None
    thinking: bool | None = None
    thinking_effort: str | None = None


class ModelConfigPatchPayload(BaseModel):
    thinking_effort: str | None = None
    summary: SummaryModelConfigPayload | None = None
    fallbacks: list[str] | None = None
    vision: VisionModelConfigPayload | None = None


class BackfillTreePayload(BaseModel):
    max_leaves: int = 64


class RestoreBackupPayload(BaseModel):
    filename: str
    mode: Literal["full", "summarize"] = "full"


def _model_config_response() -> dict:
    if _brain is None:
        raise HTTPException(status_code=503, detail=tr("api.state.agent_not_ready"))
    snapshot = _brain.model_config_snapshot()
    snapshot["override_path"] = str(_model_config_path)
    snapshot["persisted"] = _model_config_path.is_file()
    return snapshot


@router.post("/messages")
async def post_message(
    message: MessagePayload,
    request: Request,
    authorization: str | None = Header(default=None),
):
    if _inbox is None:
        raise HTTPException(status_code=503, detail=tr("api.state.agent_not_ready"))
    is_desktop = (
        message.sender_id.startswith("coworker-desktop:")
        or message.message_id is not None
        or message.type is not None
    )
    is_coworker_peer = message.sender_id.startswith("coworker:")
    # 普通 REST 入站同样受通信令牌保护：只有显式设置了通信令牌时，所有 /messages
    # 才必须携带 Bearer；未显式设置时保持既有行为，由回环/可信网络边界兜底。
    # 搭档信道额外接受 COWORKER__INBOUND_TOKEN；一旦设置，搭档消息必须认证。
    requires_communication_auth = (
        is_desktop
        or is_authenticated_relay_request(request)
        or _communication_token_explicit
        or (is_coworker_peer and bool(_coworker_inbound_token))
    )
    if requires_communication_auth:
        if is_coworker_peer:
            _verify_coworker_peer_authorization(authorization)
        else:
            verify_communication_authorization(authorization)
    if is_desktop:
        if message.protocol_version != 1:
            raise HTTPException(status_code=422, detail=tr("api.message.protocol_version"))
        if not message.message_id:
            raise HTTPException(
                status_code=422, detail=tr("api.message.message_id_required")
            )
        if not message.type or not message.type.startswith("desktop."):
            raise HTTPException(
                status_code=422, detail=tr("api.message.event_type_required")
            )
        if message.payload is None:
            raise HTTPException(status_code=422, detail=tr("api.message.payload_required"))
        _ensure_inbound_allowed(message.sender_id, source="desktop")
        if not _remember_desktop_message_id(message.message_id):
            # bridge 出站重试导致的重复投递：对端已经处理过这条消息，直接 ack 且不再入队，
            # 让 bridge 把 outbox 行 acknowledge 掉，避免 agent 把同一条消息处理多次。
            logger.debug(
                f"Duplicate desktop message_id {message.message_id} ignored "
                f"(sender={message.sender_id}, type={message.type})"
            )
            if _channels is not None:
                _channels.record_inbound_duplicate(message.sender_id, source="desktop")
            return {
                "message_id": message.message_id,
                "accepted": True,
                "duplicate": True,
            }
    await _push_message(message, source_is_desktop=is_desktop)
    if message.message_id:
        return {
            "message_id": message.message_id,
            "accepted": True,
            "duplicate": False,
        }
    return {
        "status": "queued",
        "sender_id": message.sender_id,
        "conversation_id": message.conversation_id,
    }


def _ensure_inbound_allowed(participant_id: str, *, source: str = "") -> None:
    if _channels is None:
        raise HTTPException(status_code=503, detail=tr("api.state.agent_not_ready"))
    try:
        _channels.ensure_inbound_allowed(participant_id, source=source)
    except ChannelAccessDeniedError as error:
        raise HTTPException(status_code=403, detail=str(error)) from error


async def _push_message(message: MessagePayload, *, source_is_desktop: bool) -> None:
    if _channels is None:
        raise HTTPException(status_code=503, detail=tr("api.state.agent_not_ready"))
    try:
        await _channels.receive_raw(
            InboundEnvelope(
                participant_id=message.sender_id,
                source="desktop" if source_is_desktop else "rest",
                payload=message.model_dump(),
            )
        )
    except ChannelAccessDeniedError as error:
        raise HTTPException(status_code=403, detail=str(error)) from error


def _public_status_payload(authenticated: bool = False) -> dict[str, Any]:
    """管理员配置了通信令牌但请求未认证时返回的基础状态：只暴露生命周期。"""

    auth = {
        "communication_token_configured": True,
        "authenticated": authenticated,
    }
    if _agent is None:
        return {"status": "not_started", **auth}
    s = _agent.state
    if s.is_sleeping:
        state = "sleeping"
    elif s.is_running:
        state = "running"
    else:
        state = "idle"
    return {
        "status": state,
        "is_running": s.is_running,
        "is_sleeping": s.is_sleeping,
        "setup_mode": s.setup_mode,
        **auth,
    }


def _full_status_payload(auth: dict[str, Any] | None = None) -> dict[str, Any]:
    if _agent is None:
        return {"status": "not_started", **(auth or {})}
    s = _agent.state
    payload: dict[str, Any] = {
        "is_running": s.is_running,
        "is_sleeping": s.is_sleeping,
        "provider": s.current_provider,
        "model": s.current_model,
        "cycle_count": s.cycle_count,
        "setup_mode": s.setup_mode,
    }
    if _coworker_self_id:
        payload["coworker_self_id"] = _coworker_self_id
    if auth:
        payload.update(auth)
    if _brain is not None:
        payload["providers"] = _brain.list_providers()
        payload["model_config"] = _model_config_response()
    if _usage_stats is not None:
        payload["usage_stats"] = _usage_stats.snapshot()
    return payload


@router.get("/status")
async def get_status(
    request: Request,
    authorization: str | None = Header(default=None),
):
    if not _communication_token_explicit:
        # 管理员没有显式设置通信令牌时，保持与引入认证前一致：直接返回完整快照。
        return _full_status_payload()
    authenticated = communication_authorization_matches(authorization)
    if not authenticated:
        # 已配置令牌但未认证时保持 /status 可用，只降级为基础信息。
        return _public_status_payload()
    return _full_status_payload(
        {"communication_token_configured": True, "authenticated": True}
    )


@router.get("/api/debug/tasks")
async def get_debug_tasks():
    """运行时查看事件循环里仍存活的 asyncio task（排查卡死/无法退出用）。

    waiting_at 指出每个 task 当前挂在哪一行 await——卡住时一眼可见元凶。
    """
    from coworker.core.diagnostics import task_snapshot

    snapshot = task_snapshot()
    return {
        "total": len(snapshot),
        "pending": sum(1 for t in snapshot if not t["done"]),
        "tasks": snapshot,
    }


@router.post("/switch_model")
async def switch_model(payload: SwitchModelPayload):
    if _brain is None:
        raise HTTPException(status_code=503, detail=tr("api.state.agent_not_ready"))
    try:
        await _brain.switch_model(payload.provider, payload.model_id)
        return {
            "status": "switched",
            "provider": payload.provider,
            "model_id": _brain.current_model,
        }
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e)) from e


@router.get("/model_config")
async def get_model_config():
    return _model_config_response()


@router.patch("/model_config")
async def patch_model_config(payload: ModelConfigPatchPayload):
    if _brain is None:
        raise HTTPException(status_code=503, detail=tr("api.state.agent_not_ready"))

    try:
        snapshot = await _brain.update_model_config(
            thinking_effort=payload.thinking_effort,
            summary_provider=payload.summary.provider if payload.summary else None,
            summary_model=payload.summary.model if payload.summary else None,
            summary_thinking=payload.summary.thinking if payload.summary else None,
            summary_thinking_effort=payload.summary.thinking_effort if payload.summary else None,
            fallbacks=payload.fallbacks,
            vision_provider=payload.vision.provider if payload.vision else None,
            vision_model=payload.vision.model if payload.vision else None,
            vision_thinking=payload.vision.thinking if payload.vision else None,
            vision_thinking_effort=payload.vision.thinking_effort if payload.vision else None,
        )
        runtime = RuntimeModelConfig.from_brain_snapshot(snapshot)
        write_runtime_model_config(_model_config_path, runtime)
        return _model_config_response()
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e)) from e


@router.post("/backfill_tree")
async def backfill_tree(payload: BackfillTreePayload):
    """在线从原始日志全史重建多尺度记忆树（运维触发，对模型零 token 成本）。

    后台异步运行（重建会消耗较多 LLM 调用，立即返回不阻塞 HTTP）；安全性由
    ShortTermMemory.backfill_tree_online 保证（临时树构建 + 压缩锁内原子替换，
    与主循环并发安全）。完成后记日志并向 inbox 推送一条系统消息。
    """
    if _agent is None or _brain is None:
        raise HTTPException(status_code=503, detail=tr("api.state.agent_not_ready"))
    stm = _agent._short_term
    if stm.log_store is None:
        raise HTTPException(
            status_code=400, detail=tr("api.backfill.log_store_unconfigured")
        )
    if stm.backfill_progress.get("running"):
        raise HTTPException(
            status_code=409,
            detail=tr("api.backfill.already_running"),
        )
    # 同步占位 running=True：让 GET 在 POST 返回后立刻看到进行中，并堵住并发重复触发的窗口
    # （检查→置位之间无 await，端点协程不让出）。_run 的 finally 与 _populate_tree 均会复位。
    stm.backfill_progress = {"running": True, "done": 0, "total": 0}
    task_locale = capture_locale()

    async def _run() -> None:
        with locale_context(task_locale):
            try:
                n = await stm.backfill_tree_online(_brain, payload.max_leaves)
                if n == 0:
                    msg = tr("notification.backfill_empty")
                else:
                    msg = tr(
                        "notification.backfill_done",
                        leaves=n,
                        nodes=len(stm.tree.nodes),
                    )
                logger.info(f"[backfill-online] {msg}")
            except Exception as e:
                msg = tr("notification.backfill_failed", error=e)
                logger.error(f"[backfill-online] {msg}")
            finally:
                stm.backfill_progress["running"] = False
            if _inbox is not None:
                await _inbox.push(
                    IncomingEvent(participant_id="system", content=msg, source="system")
                )

    task = asyncio.create_task(_run())
    task.add_done_callback(lambda t: t.exception() if not t.cancelled() else None)
    return {
        "status": "started",
        "max_leaves": payload.max_leaves,
        "note": tr("api.backfill.started_note"),
    }


@router.get("/backfill_tree")
async def backfill_tree_status():
    """查询在线回溯进度：{running, done, total}。done/total 为已处理/总块数。"""
    if _agent is None:
        return {"status": "not_started"}
    return _agent._short_term.backfill_progress


_BACKUP_PREFIX = "emergency_backup_"


def _backup_dir() -> Path | None:
    """应急备份所在目录（= 短期记忆快照的同级目录）。Agent 未就绪时返回 None。"""
    if _agent is None or _agent._snapshot_path is None:
        return None
    return _agent._snapshot_path.parent


def resolve_backup_path(backup_dir: Path, name: str) -> Path:
    """Resolve one emergency backup without allowing traversal or other files."""
    if (
        "/" in name
        or "\\" in name
        or ".." in name
        or not name.startswith(_BACKUP_PREFIX)
        or not name.endswith(".json")
    ):
        raise HTTPException(status_code=400, detail=tr("api.backup.invalid_filename"))
    path = backup_dir / name
    if path.resolve().parent != backup_dir.resolve() or not path.is_file():
        raise HTTPException(status_code=404, detail=tr("api.backup.missing"))
    return path


@router.get("/profile")
async def get_profile(authorization: str | None = Header(default=None)):
    """Agent 基础信息：身份、最早记忆时间戳。"""
    if _communication_token_explicit:
        # 显式设置通信令牌后，profile 与完整 status 同权保护，避免未认证访问触发
        # 档案自述更新提醒等副作用。
        verify_communication_authorization(authorization)
    global _profile_readme_last_reminded_at
    if _agent is None:
        return {"status": "not_started"}

    identity = _agent._identity
    stm = _agent._short_term
    identity_dir = getattr(identity, "_dir", "data/identity")
    identity_dir = identity_dir if isinstance(identity_dir, (str, Path)) else "data/identity"
    readme_path = Path(identity_dir) / "profile.md"
    readme: str | None = None
    readme_needs_update = True
    try:
        updated_at = datetime.fromtimestamp(readme_path.stat().st_mtime)
        readme = readme_path.read_text(encoding="utf-8").strip() or None
        readme_needs_update = not readme or datetime.now() - updated_at >= _PROFILE_README_INTERVAL
    except OSError:
        readme_needs_update = True
    now = datetime.now()
    reminder_due = (
        _profile_readme_last_reminded_at is None
        or now - _profile_readme_last_reminded_at >= _PROFILE_README_INTERVAL
    )
    # The identity page loads this endpoint immediately. During first-run
    # setup there is no configured model that can handle a profile update, so
    # do not enqueue a system message that would wake the otherwise-idle loop.
    if (
        _inbox is not None
        and not _agent.state.setup_mode
        and _agent.state.cycle_count > 0
        and readme_needs_update
        and reminder_due
    ):
        await _inbox.push(
            IncomingEvent(
                participant_id="system",
                content=tr(
                    "notification.profile_reminder",
                    path=readme_path.as_posix(),
                    max_chars=200,
                    days=_PROFILE_README_INTERVAL.days,
                ),
                source="system",
            )
        )
        _profile_readme_last_reminded_at = now

    # 最早日志时间：LogStore manifest 第一个分片的 ts_min
    earliest_log_ts: str | None = None
    if stm.log_store is not None:
        try:
            shards = stm.log_store.manifest()
            if shards:
                earliest_log_ts = shards[0].ts_min or None
        except Exception:
            pass

    return {
        "name": identity.name or None,
        "is_initialized": identity.is_initialized,
        "personality": identity.personality or None,
        "current_location": identity.current_location or None,
        "earliest_log_ts": earliest_log_ts,
        "readme": readme,
    }


@router.get("/backups")
async def list_backups() -> dict[str, object]:
    """列出应急备份（AgentLoop 连续错误时写入的完整短期记忆快照），供运维查看与恢复。"""
    backup_dir = _backup_dir()
    if backup_dir is None:
        raise HTTPException(status_code=503, detail=tr("api.state.agent_not_ready"))
    out = []
    for p in sorted(backup_dir.glob(f"{_BACKUP_PREFIX}*.json"), reverse=True):
        item: dict = {"filename": p.name, "timestamp": None, "message_count": None}
        ts_part = p.stem[len(_BACKUP_PREFIX) :]
        try:
            item["timestamp"] = datetime.strptime(ts_part, "%Y%m%d_%H%M%S").isoformat()
        except ValueError:
            pass
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            item["message_count"] = len(data.get("primary", []))
        except Exception:
            pass  # 单个损坏备份不应让整个列表失败
        out.append(item)
    return {"backups": out}


@router.post("/backups/restore")
async def restore_backup(payload: RestoreBackupPayload) -> dict[str, object]:
    """从指定应急备份恢复短期记忆。

    mode="full"：整盘替换当前 primary（修掉尾部不完整 tool 链），主循环下个周期接管。
    mode="summarize"：把备份内容摘要后经 inbox 注入，让 agent 以低 token 成本重新吸收。
    """
    backup_dir = _backup_dir()
    if backup_dir is None or _brain is None:
        raise HTTPException(status_code=503, detail=tr("api.state.agent_not_ready"))

    name = payload.filename
    path = resolve_backup_path(backup_dir, name)

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        raise HTTPException(
            status_code=400, detail=tr("api.backup.read_failed", error=e)
        ) from e

    restored = ShortTermMemory.parse_primary(data)
    if not restored:
        raise HTTPException(status_code=400, detail=tr("api.backup.empty"))

    stm = _agent._short_term  # type: ignore[union-attr]  # _backup_dir 已确保 _agent 非 None

    if payload.mode == "summarize":
        try:
            raw = await _brain.summarize(
                restored,
                context_hint=tr("notification.backup_restore_hint", name=name),
            )
        except Exception as e:
            raise HTTPException(
                status_code=500, detail=tr("api.backup.summary_failed", error=e)
            ) from e
        summary_text = raw.content if isinstance(raw, SummaryResult) else raw
        try:
            summary = json.loads(summary_text).get("summary", summary_text)
        except (json.JSONDecodeError, AttributeError):
            summary = summary_text
        if _inbox is not None:
            await _inbox.push(
                IncomingEvent(
                    participant_id="system",
                    content=tr(
                        "notification.backup_summary",
                        name=name,
                        count=len(restored),
                        summary=summary,
                    ),
                    source="system",
                )
            )
        return {
            "status": "restored",
            "mode": "summarize",
            "message_count": len(restored),
            "summary": summary,
        }

    # mode == "full"：整盘引用替换（单次赋值，GIL 下原子）+ 修尾不完整 tool 链。
    stm.primary = restored
    removed = stm.cleanup_incomplete_tool_calls()
    if _inbox is not None:
        await _inbox.push(
            IncomingEvent(
                participant_id="system",
                content=tr("notification.backup_full", name=name, count=len(stm.primary)),
                source="system",
            )
        )
    return {
        "status": "restored",
        "mode": "full",
        "message_count": len(stm.primary),
        "removed_dangling": removed,
    }
