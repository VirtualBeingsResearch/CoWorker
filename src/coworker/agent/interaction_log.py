from __future__ import annotations

import hashlib
import json
import threading
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from coworker.core.types import AttachmentData, ToolCall


class InteractionLogger:
    """Append-only interaction logger with optional size-based shard rotation.

    ``rotation_bytes=0`` keeps the historical single-file behavior.  When enabled,
    completed shards are named ``<stem>-000001<suffix>``, while ``log_path`` always
    remains the active file.  This is the layout consumed by :class:`LogStore`.
    """

    def __init__(self, log_path: str | Path, rotation_bytes: int = 0) -> None:
        self._path = Path(log_path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._rotation_bytes = max(0, rotation_bytes)
        self._last_system_prompt_hash: str | None = None
        # 单调递增的条目序号，作为 LogStore / 记忆块树寻址原始日志的稳定主键
        # （ts 是裸 datetime.now()，非单调、可重复，不能当地址）。
        # 序号持久化到 sidecar，跨重启续号，避免重复序号破坏区间查找。
        self._seq_path = self._path.with_suffix(self._path.suffix + ".seq")
        self._seq = self._load_seq()
        self._lock = threading.Lock()
        # 唯一的事件 tap：每条写入的日志条目都会同步广播给已注册的监听者
        # （运行日志 SSE 采集器据此实时扇出）。取代旧的散落式 push_event 埋点。
        self._listeners: list[Callable[[dict], None]] = []

    def add_listener(self, fn: Callable[[dict], None]) -> None:
        """注册一个监听者：每条日志条目写盘后会以该条目 dict 同步回调它。
        回调内异常被吞掉（best-effort），绝不影响日志写入本身。"""
        self._listeners.append(fn)

    def _load_seq(self) -> int:
        try:
            return int(self._seq_path.read_text(encoding="utf-8").strip())
        except Exception:
            return 0

    def _persist_seq(self) -> None:
        try:
            self._seq_path.write_text(str(self._seq), encoding="utf-8")
        except Exception as e:  # best-effort：sidecar 写失败不应中断日志记录
            from loguru import logger
            logger.warning(f"Failed to persist interaction-log seq to {self._seq_path}: {e}")

    def last_seq(self) -> int:
        """The seq that will be assigned to the NEXT entry (i.e. current counter)."""
        return self._seq

    def _next_archive_path(self) -> Path:
        """Return an unused, monotonically numbered archive path.

        We scan at rotation time rather than persisting a second counter.  That keeps
        upgrades from a legacy single file and manually restored archives safe: an
        existing ``interactions-000001.jsonl`` is never overwritten.
        """
        prefix = f"{self._path.stem}-"
        suffix = self._path.suffix
        highest = 0
        for candidate in self._path.parent.glob(f"{prefix}*{suffix}"):
            if not candidate.is_file():
                continue
            number = candidate.stem[len(prefix) :]
            if number.isdecimal():
                highest = max(highest, int(number))
        return self._path.with_name(f"{prefix}{highest + 1:06d}{suffix}")

    def _rotate_before_write(self, incoming_bytes: int) -> None:
        """Move a full active file aside before appending a new record.

        A single record may itself be larger than the threshold; it is still written
        whole into an otherwise empty shard so JSONL records are never split.
        """
        if self._rotation_bytes <= 0:
            return
        try:
            current_size = self._path.stat().st_size
        except FileNotFoundError:
            return
        except OSError as e:
            from loguru import logger

            logger.warning(f"Failed to stat interaction log {self._path} for rotation: {e}")
            return
        if current_size == 0 or current_size + incoming_bytes <= self._rotation_bytes:
            return

        archive_path = self._next_archive_path()
        try:
            # rename is an atomic same-directory move on supported filesystems.  The
            # destination is chosen above to be unused, so prior history is retained.
            self._path.rename(archive_path)
        except OSError as e:
            # Logging must remain best-effort: if an external reader temporarily holds
            # the file open (notably on Windows), retain the record in the active shard
            # and retry rotation before the next write instead of dropping it.
            from loguru import logger

            logger.warning(
                f"Failed to rotate interaction log {self._path} to {archive_path}: {e}"
            )

    def _write(self, entry: dict) -> None:
        with self._lock:
            entry["seq"] = self._seq
            entry["ts"] = datetime.now().isoformat()
            self._seq += 1
            record = (json.dumps(entry, ensure_ascii=False) + "\n").encode("utf-8")
            self._rotate_before_write(len(record))
            # Write bytes so the rotation byte limit has identical meaning on Windows
            # and POSIX (text-mode newline conversion would otherwise add a byte).
            with self._path.open("ab") as f:
                f.write(record)
            self._persist_seq()
            # 写盘后再广播给监听者：监听者只读、且任何异常都不应回溯影响已落盘的日志。
            for fn in self._listeners:
                try:
                    fn(entry)
                except Exception as e:
                    from loguru import logger
                    logger.warning(f"InteractionLogger listener raised, ignored: {e}")

    def log_system_prompt(self, system_prompt: str) -> None:
        h = hashlib.md5(system_prompt.encode()).hexdigest()
        if h == self._last_system_prompt_hash:
            return
        self._last_system_prompt_hash = h
        self._write({"type": "system_prompt", "content": system_prompt})

    def log_thinking_start(
        self,
        cycle: int,
        thinking: bool | None = None,
    ) -> None:
        """一轮推理的起点（brain.think() 调用前）。

        与 llm_response（推理终点）配对，让运行日志能呈现完整生命周期。
        不进 digest（见 LogStore._DIGEST_TYPES）。
        """
        entry = {"type": "thinking_start", "cycle": cycle}
        if thinking is not None:
            entry["thinking"] = thinking
        self._write(entry)

    def log_message_tick(self, content: str):
        self._write({
            "type": "message_tick",
            "content": content
        })

    def log_message_in(
        self,
        participant_id: str,
        content: str,
        source: str,
        attachments: list[AttachmentData] | None = None,
        conversation_id: str | None = None,
    ) -> None:
        entry: dict = {
            "type": "message_in",
            "participant_id": participant_id,
            "source": source,
            "content": content,
        }
        if conversation_id:
            entry["conversation_id"] = conversation_id
        if attachments:
            entry["files"] = [
                {"filename": a.filename, "media_type": a.media_type, "saved_path": a.saved_path}
                for a in attachments
            ]
        self._write(entry)

    def log_llm_response(
        self,
        reasoning_content: str | None,
        content: str,
        tool_calls: list[ToolCall],
        stop_reason: str,
        model: str,
        usage: dict,
        provider: str = "unknown",
        thinking: bool | None = None,
    ) -> None:
        entry: dict[str, Any] = {
            "type": "llm_response",
            "provider": provider,
            "model": model,
            "reasoning_content": reasoning_content,
            "content": content,
            "tool_calls": [
                {"id": tc.id, "name": tc.name, "arguments": tc.arguments}
                for tc in tool_calls
            ],
            "stop_reason": stop_reason,
            "usage": usage,
        }
        if thinking is not None:
            entry["thinking"] = thinking
        self._write(entry)

    def log_summary_llm_response(
        self,
        *,
        provider: str,
        model: str,
        usage: dict,
        context_hint: str = "",
    ) -> None:
        entry = {
            "type": "summary_llm_response",
            "provider": provider,
            "model": model,
            "usage": usage,
        }
        if context_hint:
            entry["context_hint"] = context_hint[:200]
        self._write(entry)

    def log_vision_llm_response(
        self,
        *,
        provider: str,
        model: str,
        usage: dict,
        label: str = "",
    ) -> None:
        entry = {
            "type": "vision_llm_response",
            "provider": provider,
            "model": model,
            "usage": usage,
        }
        if label:
            entry["label"] = label[:200]
        self._write(entry)

    def log_mem0_llm_response(
        self,
        *,
        provider: str,
        model: str,
        usage: dict,
        usage_source: str = "",
        operation: str = "",
    ) -> None:
        entry = {
            "type": "mem0_llm_response",
            "provider": provider,
            "model": model,
            "usage": usage,
        }
        if usage_source:
            entry["usage_source"] = usage_source[:40]
        if operation:
            entry["operation"] = operation[:80]
        self._write(entry)

    def log_tool_call(self, id: str, name: str, arguments: dict) -> None:
        self._write(
            {"type": "tool_call", "id": id, "name": name, "arguments": arguments}
        )

    def log_tool_result(self, id: str, name: str, content: str, is_error: bool) -> None:
        self._write(
            {
                "type": "tool_result",
                "id": id,
                "name": name,
                "content": content,
                "is_error": is_error,
            }
        )

    def log_task_reminder(self, tasks: list[dict], source: str) -> None:
        self._write({"type": "task_reminder", "source": source, "tasks": tasks})

    def log_pin_reinjected(self, pins: list[dict]) -> None:
        self._write({"type": "pin_reinjected", "pins": pins})

    def log_subconscious_spawned(self, mode: str, bubble_id: str, goal: str) -> None:
        self._write({
            "type": "subconscious_spawned",
            "mode": mode,
            "bubble_id": bubble_id,
            "goal": goal,
        })

    def log_subconscious_done(
        self, mode: str, bubble_id: str, result: str, cycles: int, elapsed_s: float
    ) -> None:
        self._write({
            "type": "subconscious_done",
            "mode": mode,
            "bubble_id": bubble_id,
            "result": result[:200],
            "cycles_used": cycles,
            "elapsed_s": round(elapsed_s, 1),
        })

    def log_palace_injection(
        self,
        palaces: list[str],
        tags: list[str],
        critical_skills: list[str],
        related_skills: list[str],
        recalled: list[dict],
    ) -> None:
        self._write({
            "type": "palace_injection",
            "palaces": palaces,
            "tags": tags,
            "critical_skills": critical_skills,
            "related_skills": related_skills,
            "recalled": [
                {"id": m["id"], "category": m["category"],
                 "relevance": m["relevance"], "content": m["content"]}
                for m in recalled
            ],
        })

    def log_auto_recall(self, query: str, memories: list[dict]) -> None:
        self._write(
            {
                "type": "auto_recall",
                "query": query,
                "memories": [
                    {
                        "id": m["id"],
                        "category": m["category"],
                        "relevance": m["relevance"],
                        "content": m["content"],
                    }
                    for m in memories
                ],
            }
        )
