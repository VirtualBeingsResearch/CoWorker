from __future__ import annotations

import asyncio
import base64
import json
import re
from collections import OrderedDict
from datetime import datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from fastapi import APIRouter, Header, HTTPException
from loguru import logger
from pydantic import BaseModel

from coworker.core.ids import new_compact_id
from coworker.core.model_config import RuntimeModelConfig, write_runtime_model_config
from coworker.core.types import AttachmentData, IncomingEvent, SummaryResult
from coworker.memory.short_term import ShortTermMemory

if TYPE_CHECKING:
    from coworker.agent.inbox_watcher import InboxWatcher
    from coworker.agent.loop import AgentLoop
    from coworker.agent.usage_stats import UsageStatsCollector
    from coworker.brain.brain import Brain

router = APIRouter()

_inbox: InboxWatcher | None = None
_agent: AgentLoop | None = None
_brain: Brain | None = None
_usage_stats: UsageStatsCollector | None = None
_attachments_dir: Path = Path("data/attachments")
_model_config_path: Path = Path("data/model_runtime_config.json")
_communication_token = ""
# Authentication is the safe baseline.  Tests and explicitly local-only
# callers can opt into development mode through ``setup(..., True)``.
_development_mode = False

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

_IMAGE_MEDIA_TYPES = {"image/jpeg", "image/png", "image/gif", "image/webp"}
_PDF_MEDIA_TYPES = {"application/pdf"}
_UNSAFE_ATTACHMENT_CHARS_RE = re.compile(r'[<>:"/\\|?*\x00-\x1f]+')
_PROFILE_README_INTERVAL = timedelta(days=14)  # 档案自述更新提醒间隔；默认两周一次
_profile_readme_last_reminded_at: datetime | None = None


def setup(
    inbox: InboxWatcher,
    agent: AgentLoop,
    brain: Brain,
    inbox_dir: str = "data/inbox",
    usage_stats: UsageStatsCollector | None = None,
    model_config_path: str | Path = "data/model_runtime_config.json",
    communication_token: str = "",
    development_mode: bool = False,
) -> None:
    global _inbox, _agent, _brain, _usage_stats, _attachments_dir, _model_config_path
    global _communication_token, _development_mode
    _inbox = inbox
    _agent = agent
    _brain = brain
    _usage_stats = usage_stats
    _model_config_path = Path(model_config_path)
    _attachments_dir = Path(inbox_dir).parent / "attachments"
    _attachments_dir.mkdir(parents=True, exist_ok=True)
    _communication_token = communication_token.strip()
    _development_mode = development_mode
    if _development_mode:
        logger.warning("Coworker communication API is running in unauthenticated development mode")


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


def verify_communication_authorization(authorization: str | None) -> None:
    if _development_mode:
        return
    if not _communication_token:
        raise HTTPException(status_code=503, detail="Communication token is not configured")
    if authorization != f"Bearer {_communication_token}":
        raise HTTPException(status_code=401, detail="Invalid communication bearer token")


class SwitchModelPayload(BaseModel):
    provider: str
    model_id: str = ""  # 省略则使用该 provider 实例配置的 default_model


class SummaryModelConfigPayload(BaseModel):
    provider: str | None = None
    model: str | None = None
    thinking: bool | None = None


class VisionModelConfigPayload(BaseModel):
    provider: str | None = None
    model: str | None = None
    thinking: bool | None = None


class ModelConfigPatchPayload(BaseModel):
    summary: SummaryModelConfigPayload | None = None
    fallbacks: list[str] | None = None
    vision: VisionModelConfigPayload | None = None


class BackfillTreePayload(BaseModel):
    max_leaves: int = 64


class RestoreBackupPayload(BaseModel):
    filename: str
    mode: Literal["full", "summarize"] = "full"


def _save_attachment(
    att: AttachmentSchema, *, keep_inline_data: bool = True
) -> AttachmentData:
    raw = base64.b64decode(att.data)
    leaf = re.split(r"[\\/]+", att.filename)[-1].strip(" .")
    filename = _UNSAFE_ATTACHMENT_CHARS_RE.sub("-", leaf).strip(" .-") or "attachment"
    dest = _attachments_dir / f"{new_compact_id()}_{filename}"
    dest.write_bytes(raw)
    keep_data = keep_inline_data and (
        att.media_type in _IMAGE_MEDIA_TYPES or att.media_type in _PDF_MEDIA_TYPES
    )
    return AttachmentData(
        filename=filename,
        media_type=att.media_type,
        saved_path=str(dest),
        data=att.data if keep_data else None,
    )


def _model_config_response() -> dict:
    if _brain is None:
        raise HTTPException(status_code=503, detail="Agent not ready")
    snapshot = _brain.model_config_snapshot()
    snapshot["override_path"] = str(_model_config_path)
    snapshot["persisted"] = _model_config_path.is_file()
    return snapshot


@router.post("/messages")
async def post_message(message: MessagePayload, authorization: str | None = Header(default=None)):
    if _inbox is None:
        raise HTTPException(status_code=503, detail="Agent not ready")
    if message.sender_id.startswith(("codex:", "local:", "codex-bridge:")):
        raise HTTPException(status_code=422, detail="legacy Codex Bridge messages are unsupported")
    is_desktop = (
        message.sender_id.startswith("coworker-desktop:")
        or message.message_id is not None
        or message.type is not None
    )
    if is_desktop:
        verify_communication_authorization(authorization)
        if message.protocol_version != 1:
            raise HTTPException(status_code=422, detail="protocol_version must be 1")
        if not message.message_id:
            raise HTTPException(status_code=422, detail="message_id is required")
        if not message.type or not message.type.startswith("desktop."):
            raise HTTPException(status_code=422, detail="desktop event type is required")
        if message.payload is None:
            raise HTTPException(status_code=422, detail="desktop payload is required")
        if not _remember_desktop_message_id(message.message_id):
            # bridge 出站重试导致的重复投递：对端已经处理过这条消息，直接 ack 且不再入队，
            # 让 bridge 把 outbox 行 acknowledge 掉，避免 agent 把同一条消息处理多次。
            logger.debug(
                f"Duplicate desktop message_id {message.message_id} ignored "
                f"(sender={message.sender_id}, type={message.type})"
            )
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


async def _push_message(message: MessagePayload, *, source_is_desktop: bool) -> None:
    inbox = _inbox
    if inbox is None:
        raise HTTPException(status_code=503, detail="Agent not ready")
    content = message.content
    attachment_schemas = list(message.attachments)
    if source_is_desktop:
        desktop_payload = message.payload or {}
        if message.type == "desktop.thread.event":
            raw_message = desktop_payload.get("message")
            content = raw_message if isinstance(raw_message, str) else ""
            raw_attachments = desktop_payload.get("attachments")
            if isinstance(raw_attachments, list):
                attachment_schemas.extend(
                    AttachmentSchema.model_validate(item) for item in raw_attachments
                )
        else:
            content = json.dumps(
                _desktop_envelope(message),
                ensure_ascii=False,
                separators=(",", ":"),
            )
    # Desktop envelopes can contain inline attachments. Persist their files at
    # the boundary and pass only local references onward so base64 data never
    # enters the agent's generic short-term context.
    attachments = [
        _save_attachment(a, keep_inline_data=not source_is_desktop)
        for a in attachment_schemas
    ]
    source: Literal["coworker_desktop", "rest"] = (
        "coworker_desktop" if source_is_desktop else "rest"
    )
    event = IncomingEvent(
        participant_id=message.sender_id,
        content=content,
        conversation_id=message.conversation_id,
        timestamp=datetime.now(),
        source=source,
        attachments=attachments,
    )
    await inbox.push(event)


def _desktop_envelope(message: MessagePayload) -> dict[str, Any]:
    envelope: dict[str, Any] = {
        "protocol_version": message.protocol_version,
        "message_id": message.message_id,
        "created_at": message.created_at,
        "type": message.type,
        "payload": message.payload,
    }
    if message.request_id is not None:
        envelope["request_id"] = message.request_id
    if message.conversation_id is not None:
        envelope["conversation_id"] = message.conversation_id
    return envelope


@router.get("/status")
async def get_status():
    if _agent is None:
        return {"status": "not_started"}
    s = _agent.state
    payload = {
        "is_running": s.is_running,
        "is_sleeping": s.is_sleeping,
        "provider": s.current_provider,
        "model": s.current_model,
        "cycle_count": s.cycle_count,
    }
    if _brain is not None:
        payload["providers"] = _brain.list_providers()
        payload["model_config"] = _model_config_response()
    if _usage_stats is not None:
        payload["usage_stats"] = _usage_stats.snapshot()
    return payload


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
        raise HTTPException(status_code=503, detail="Agent not ready")
    try:
        await _brain.switch_model(payload.provider, payload.model_id)
        return {
            "status": "switched",
            "provider": payload.provider,
            "model_id": _brain.current_model,
        }
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/model_config")
async def get_model_config():
    return _model_config_response()


@router.patch("/model_config")
async def patch_model_config(payload: ModelConfigPatchPayload):
    if _brain is None:
        raise HTTPException(status_code=503, detail="Agent not ready")

    try:
        snapshot = await _brain.update_model_config(
            summary_provider=payload.summary.provider if payload.summary else None,
            summary_model=payload.summary.model if payload.summary else None,
            summary_thinking=payload.summary.thinking if payload.summary else None,
            fallbacks=payload.fallbacks,
            vision_provider=payload.vision.provider if payload.vision else None,
            vision_model=payload.vision.model if payload.vision else None,
            vision_thinking=payload.vision.thinking if payload.vision else None,
        )
        runtime = RuntimeModelConfig.from_brain_snapshot(snapshot)
        write_runtime_model_config(_model_config_path, runtime)
        return _model_config_response()
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/backfill_tree")
async def backfill_tree(payload: BackfillTreePayload):
    """在线从原始日志全史重建多尺度记忆树（运维触发，对模型零 token 成本）。

    后台异步运行（重建会消耗较多 LLM 调用，立即返回不阻塞 HTTP）；安全性由
    ShortTermMemory.backfill_tree_online 保证（临时树构建 + 压缩锁内原子替换，
    与主循环并发安全）。完成后记日志并向 inbox 推送一条系统消息。
    """
    if _agent is None or _brain is None:
        raise HTTPException(status_code=503, detail="Agent not ready")
    stm = _agent._short_term
    if stm.log_store is None:
        raise HTTPException(status_code=400, detail="未配置原始日志寻址层，无法回溯")
    if stm.backfill_progress.get("running"):
        raise HTTPException(
            status_code=409,
            detail="回溯已在进行中，请用 GET /backfill_tree 查看进度",
        )
    # 同步占位 running=True：让 GET 在 POST 返回后立刻看到进行中，并堵住并发重复触发的窗口
    # （检查→置位之间无 await，端点协程不让出）。_run 的 finally 与 _populate_tree 均会复位。
    stm.backfill_progress = {"running": True, "done": 0, "total": 0}

    async def _run() -> None:
        try:
            n = await stm.backfill_tree_online(_brain, payload.max_leaves)
            if n == 0:
                msg = "记忆树回溯完成：无可回溯的历史内容。"
            else:
                msg = (
                    f"记忆树回溯完成：从原始日志重建，生成 {n} 个叶子，"
                    f"脊柱 {len(stm.tree.nodes)} 节点。"
                )
            logger.info(f"[backfill-online] {msg}")
        except Exception as e:
            msg = f"记忆树回溯失败：{e}"
            logger.error(f"[backfill-online] {msg}")
        finally:
            stm.backfill_progress["running"] = False  # 兜底复位（含 tree 关闭等早退路径）
        if _inbox is not None:
            await _inbox.push(IncomingEvent(participant_id="system", content=msg, source="system"))

    task = asyncio.create_task(_run())
    task.add_done_callback(lambda t: t.exception() if not t.cancelled() else None)
    return {"status": "started", "max_leaves": payload.max_leaves,
            "note": "后台重建记忆树，用 GET /backfill_tree 查看进度，完成后记日志并推送系统消息"}


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


@router.get("/profile")
async def get_profile():
    """Agent 基础信息：身份、最早记忆时间戳。"""
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
    if _inbox is not None and readme_needs_update and reminder_due:
        await _inbox.push(IncomingEvent(
            participant_id="system",
            content=(
                "[档案自述提醒] 请用 write_file 生成或更新 "
                f"`{readme_path.as_posix()}`，作为 /profile 状态页展示的模型自述。"
                f"建议 200 字以内；超过 {_PROFILE_README_INTERVAL.days} 天未更新时会再次提醒。"
            ),
            source="system",
        ))
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
        "goals": identity.goals or None,
        "current_location": identity.current_location or None,
        "earliest_log_ts": earliest_log_ts,
        "readme": readme,
    }


@router.get("/backups")
async def list_backups() -> dict[str, object]:
    """列出应急备份（AgentLoop 连续错误时写入的完整短期记忆快照），供运维查看与恢复。"""
    backup_dir = _backup_dir()
    if backup_dir is None:
        raise HTTPException(status_code=503, detail="Agent not ready")
    out = []
    for p in sorted(backup_dir.glob(f"{_BACKUP_PREFIX}*.json"), reverse=True):
        item: dict = {"filename": p.name, "timestamp": None, "message_count": None}
        ts_part = p.stem[len(_BACKUP_PREFIX):]
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
        raise HTTPException(status_code=503, detail="Agent not ready")

    name = payload.filename
    # 路径穿越防护：必须是裸文件名、符合命名前后缀、解析后仍落在备份目录内。
    if ("/" in name or "\\" in name or ".." in name
            or not name.startswith(_BACKUP_PREFIX) or not name.endswith(".json")):
        raise HTTPException(status_code=400, detail="非法备份文件名")
    path = backup_dir / name
    if path.resolve().parent != backup_dir.resolve() or not path.is_file():
        raise HTTPException(status_code=404, detail="备份文件不存在")

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        raise HTTPException(status_code=400, detail=f"备份文件读取失败：{e}")

    restored = ShortTermMemory.parse_primary(data)
    if not restored:
        raise HTTPException(status_code=400, detail="备份为空，拒绝恢复（避免清空会话）")

    stm = _agent._short_term  # type: ignore[union-attr]  # _backup_dir 已确保 _agent 非 None

    if payload.mode == "summarize":
        try:
            raw = await _brain.summarize(restored, context_hint=f"应急备份 {name} 内容复原")
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"摘要失败：{e}")
        summary_text = raw.content if isinstance(raw, SummaryResult) else raw
        try:
            summary = json.loads(summary_text).get("summary", summary_text)
        except (json.JSONDecodeError, AttributeError):
            summary = summary_text
        if _inbox is not None:
            await _inbox.push(IncomingEvent(
                participant_id="system",
                content=f"[应急备份恢复·摘要] {name}（{len(restored)} 条）：\n{summary}",
                source="system",
            ))
        return {"status": "restored", "mode": "summarize",
                "message_count": len(restored), "summary": summary}

    # mode == "full"：整盘引用替换（单次赋值，GIL 下原子）+ 修尾不完整 tool 链。
    stm.primary = restored
    removed = stm.cleanup_incomplete_tool_calls()
    if _inbox is not None:
        await _inbox.push(IncomingEvent(
            participant_id="system",
            content=(f"[系统通知] 已从应急备份 {name} 恢复 {len(stm.primary)} 条消息，"
                     f"上下文可能再次接近上限，必要时请压缩。"),
            source="system",
        ))
    return {"status": "restored", "mode": "full",
            "message_count": len(stm.primary), "removed_dangling": removed}
