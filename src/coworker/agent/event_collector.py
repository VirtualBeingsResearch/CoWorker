from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any

from loguru import logger

from coworker.agent.log_store import LogStore
from coworker.channels.stream import SHUTDOWN_SENTINEL

# 进入运行日志的条目类型（用户可感知的叙事性运行事实）。其余条目——system_prompt（体量大）、
# message_tick（内部 tick）、auto_recall / palace_injection / pin_reinjected（系统注入）、
# task_reminder（噪声）——一律丢弃。后续要展示新类型，只在 _map 里加分支即可。
_FEED_TYPES = {"message_in", "thinking_start", "llm_response", "tool_call", "tool_result"}

# 各工具透传给前端运行日志的「摘要字段」：{工具名: [(字段, 脱敏, 截断长度)]}。
# 前端据此把通用的「name() 调用中」升级成按工具种别可读的一句话（读取 path、搜索 query…）。
# redact=True 的字段（自由文本，可能含人名/企微 ID）过 ID→人名替换；False 仅截断（路径/枚举/ID）。
# 只透传「让展示行可读」所需的最小字段；code/content/正文等大字段只取短预览。表外工具不透传参数。
_ARG_SPECS: dict[str, list[tuple[str, bool, int]]] = {
    "communicate": [
        ("participant_id", True, 40),
        ("conversation_id", False, 40),
        ("message", True, 80),
    ],
    "get_skill": [("skill_name", False, 60)],
    "read_file": [("path", False, 80)],
    "write_file": [("path", False, 80)],
    "list_directory": [("path", False, 80)],
    "find_files": [("pattern", False, 60), ("root", False, 60)],
    "grep_files": [("pattern", False, 60), ("path", False, 60)],
    "search_web": [("query", True, 80)],
    "fetch_url": [("url", False, 100)],
    "query_memory": [("query", True, 80), ("start", False, 40), ("end", False, 40)],
    "manage_memory": [("action", False, 20), ("content", True, 60)],
    "execute_code": [("code", True, 80)],
    "sleep": [("seconds", False, 10)],
    "task_create": [("description", True, 80)],
    "task_update": [("task_id", False, 40), ("status", False, 20)],
    "set_alarm": [("trigger_at", False, 40), ("message", True, 60)],
    "bubble_spawn": [("goal", True, 80)],
    "switch_model": [("model_id", False, 40)],
}

# 单个订阅队列的容量上限：客户端卡住时丢弃新事件而非无限堆积（前端本就只留最近 80 条）。
_QUEUE_MAXSIZE = 256


def _truncate(s: Any, n: int) -> str:
    s = s if isinstance(s, str) else ("" if s is None else str(s))
    # 截断时缀省略号，让前端展示行能直观看出内容被裁掉了（未截断则原样返回）。
    return s if len(s) <= n else s[:n] + "…"


class RuntimeEventCollector:
    """运行日志的唯一事件采集器：作为 InteractionLogger 的监听者接住每条日志条目，
    按类型做初级处理（脱敏 + 截断 + 丢弃噪声），再扇出给所有 SSE 订阅者。

    取代旧的、散落在 loop / inbox / communicate / skill 各处手工维护的 push_event 埋点——
    现在只有「日志写盘」这一个 tap 点，事件流与持久化日志天然一致、且能在重连时历史回放。
    """

    def __init__(self, log_store: LogStore, redact: Callable[[str], str]) -> None:
        self._log_store = log_store
        # 文本脱敏（企微 ID→人名）；复用 AgentState._replace_ids，避免把事件发射耦合回 state。
        self._redact = redact
        self._subscribers: list[asyncio.Queue] = []

    # ---- 订阅者注册表 ----------------------------------------------------

    def register(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=_QUEUE_MAXSIZE)
        self._subscribers.append(q)
        return q

    def unregister(self, q: asyncio.Queue) -> None:
        if q in self._subscribers:
            self._subscribers.remove(q)

    def subscribers(self) -> list[asyncio.Queue]:
        return list(self._subscribers)

    def shutdown(self) -> None:
        """关闭时唤醒所有订阅者：塞入哨兵让阻塞在 queue.get() 的 SSE 生成器立即收尾、释放连接。"""
        for q in self.subscribers():
            try:
                q.put_nowait(SHUTDOWN_SENTINEL)
            except asyncio.QueueFull:
                pass

    # ---- InteractionLogger 监听回调 --------------------------------------

    def on_entry(self, entry: dict) -> None:
        """作为 InteractionLogger.add_listener 回调（与日志写盘同步、同一事件循环）。
        映射为展示事件后非阻塞扇出；任何异常都不应回溯影响日志写入。"""
        ev = self._map(entry)
        if ev is None:
            return
        for q in self._subscribers:
            try:
                q.put_nowait(ev)
            except asyncio.QueueFull:
                pass  # 客户端消费不过来：丢弃，不阻塞主循环

    # ---- 历史回放 --------------------------------------------------------

    def recent(
        self,
        limit: int,
        days: int | float | None = None,
        tail_lines: int | None = None,
    ) -> list[dict]:
        """从持久化日志取最近 limit 条展示事件，供新连接回放历史上下文。
        ``tail_lines`` 非空时只从原始日志尾部读最多 N 行，再按 ``days`` 做轻量过滤。
        与实时流共用同一 _map（DRY）。"""
        if limit <= 0:
            return []
        try:
            if tail_lines is not None:
                entries, _complete = self._log_store.read_tail(tail_lines)
                entries = self._filter_recent_days(entries, days)
            elif days is None:
                entries, _complete = self._log_store.read_all()
            else:
                entries, _complete = self._log_store.read_recent_days(days)
        except Exception as e:
            if tail_lines is not None:
                source = f"read_tail({tail_lines})"
            else:
                source = "read_all" if days is None else f"read_recent_days({days})"
            logger.warning(f"RuntimeEventCollector.recent {source} failed: {e}")
            return []
        out = [ev for e in entries if (ev := self._map(e)) is not None]
        return out[-limit:]

    @staticmethod
    def _filter_recent_days(entries: list[dict], days: int | float | None) -> list[dict]:
        if days is None:
            return entries
        if days <= 0:
            return []
        now = datetime.now()
        cutoff = (now - timedelta(days=days)).isoformat()
        end = now.isoformat()
        return [e for e in entries if cutoff <= str(e.get("ts", "")) <= end]

    # ---- 按类型初级处理 --------------------------------------------------

    def _map(self, entry: dict) -> dict | None:
        t = entry.get("type")
        if t not in _FEED_TYPES:
            return None
        base = {"seq": entry.get("seq"), "ts": entry.get("ts"), "type": t}

        if t == "message_in":
            base["participant_id"] = self._redact(str(entry.get("participant_id", "")))
            if entry.get("conversation_id"):
                base["conversation_id"] = str(entry.get("conversation_id"))
            base["source"] = entry.get("source")
            base["content"] = self._redact(_truncate(entry.get("content"), 80))
            return base

        if t == "thinking_start":
            base["cycle"] = entry.get("cycle")
            if "thinking" in entry:
                base["thinking"] = bool(entry.get("thinking"))
            return base

        if t == "llm_response":
            base["content"] = self._redact(_truncate(entry.get("content"), 120))
            if "thinking" in entry:
                base["thinking"] = bool(entry.get("thinking"))
            return base

        if t == "tool_call":
            base["id"] = entry.get("id")
            base["name"] = entry.get("name")
            args = entry.get("arguments")
            name = entry.get("name")
            spec = _ARG_SPECS.get(name) if isinstance(name, str) else None
            if spec and isinstance(args, dict):
                base["arguments"] = self._redact_args(spec, args)
            return base

        if t == "tool_result":
            base["id"] = entry.get("id")
            base["name"] = entry.get("name")
            base["is_error"] = bool(entry.get("is_error"))
            base["content"] = self._redact(_truncate(entry.get("content"), 100))
            return base

        return None

    def _redact_args(self, spec: list[tuple[str, bool, int]], args: dict) -> dict:
        """按 _ARG_SPECS 裁剪工具参数：只保留展示行所需字段。

        逐字段截断，并对自由文本做 ID→人名替换。
        """
        out: dict[str, str] = {}
        for field, redact, maxlen in spec:
            if args.get(field) is None:
                continue
            val = _truncate(args.get(field), maxlen)
            out[field] = self._redact(val) if redact else val
        return out
