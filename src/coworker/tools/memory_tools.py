from __future__ import annotations

import asyncio
import json
import tempfile
import time
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, cast

from coworker.core.ids import new_compact_id
from coworker.core.types import Message, SummaryResult, ToolResult
from coworker.memory.long_term import LongTermMemory
from coworker.memory.recent_activity import render_recent_activity_replay
from coworker.tools.base import PAGE_CHAR_LIMIT, PAGE_CHAR_MAX, Tool, ToolDefinition, paginate_text

_QUERY_MEMORY_MAX_RESULTS = 10
_QUERY_MEMORY_RECENT_QUOTA = 2
_QUERY_MEMORY_SNAPSHOT_TTL = 30 * 60
_QUERY_MEMORY_MAX_SNAPSHOTS = 100
_QUERY_MEMORY_SNAPSHOT_LINE_CHARS = 500

if TYPE_CHECKING:
    from coworker.brain.brain import Brain
    from coworker.core.tool_scope import ToolScope
    from coworker.memory.memory_tree import MemoryNode
    from coworker.memory.recent_activity import RecentActivityMemory
    from coworker.memory.short_term import ShortTermMemory


class QueryMemoryTool(Tool):
    def __init__(
        self,
        memory: LongTermMemory,
        short_term: ShortTermMemory | None = None,
        brain: Brain | None = None,
        recent_activity: RecentActivityMemory | None = None,
        snapshot_dir: str | Path | None = None,
    ) -> None:
        self._memory = memory
        self._short_term = short_term
        self._brain = brain
        self._recent_activity = recent_activity
        self._snapshot_dir = Path(snapshot_dir) if snapshot_dir is not None else (
            Path(tempfile.gettempdir()) / "coworker" / "query-memory"
        )

    def fork(self, scope: ToolScope) -> QueryMemoryTool:
        short_term = scope.short_term if scope.short_term is not None else self._short_term
        brain = scope.brain if scope.brain is not None else self._brain
        return QueryMemoryTool(
            self._memory,
            short_term=short_term,
            brain=brain,
            recent_activity=self._recent_activity,
            snapshot_dir=self._snapshot_dir,
        )

    @property
    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="query_memory",
            description=(
                "综合查询记忆。传 query 时同时检索最近活动和长期记忆；传 start/end（ISO 时间）时回忆该时间窗。"
                "query 可与 start/end 同时使用，表示在时间窗内做语义聚焦搜索。"
            ),
            parameters={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "长期记忆检索关键词或描述。不可与 start/end 同时使用"},
                    "category": {
                        "type": "string",
                        "enum": ["knowledge", "experience", "relationship", "task", "general"],
                        "description": "长期记忆检索时可选，按分类过滤",
                    },
                    "tags": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "长期记忆检索时可选，按标签过滤：仅返回带有其中任一标签的记忆",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "近期活动与长期记忆合计返回条数，默认 5，最多 10",
                        "default": 5,
                        "minimum": 1,
                        "maximum": _QUERY_MEMORY_MAX_RESULTS,
                    },
                    "start": {"type": "string", "description": "时间窗起点（ISO，如 2026-06-07T09:00:00）"},
                    "end": {"type": "string", "description": "时间窗终点（ISO）"},
                },
                "required": [],
            },
        )

    async def execute(
        self,
        query: str | None = None,
        category: str | None = None,
        tags: list[str] | None = None,
        limit: int = 5,
        start: str | None = None,
        end: str | None = None,
        **extra,
    ) -> ToolResult:
        try:
            limit = max(1, min(int(limit), _QUERY_MEMORY_MAX_RESULTS))
            old_args = sorted({"node", "in_tree", "offset", "as_summary"}.intersection(extra))
            if old_args:
                return ToolResult(
                    tool_call_id="",
                    content=f"query_memory 不再支持参数 {', '.join(old_args)}；请使用 query=... 检索长期记忆，或 start/end 回忆时间窗。",
                    is_error=True,
                )
            has_query = bool(query and query.strip())
            has_start = bool(start)
            has_end = bool(end)
            if has_start or has_end:
                if not (has_start and has_end):
                    return ToolResult(tool_call_id="", content="时间窗回忆需要同时提供 start 和 end。", is_error=True)
                t0, t1, error = self._parse_time_bounds(start or "", end or "")
                if error:
                    return ToolResult(tool_call_id="", content=error, is_error=True)
                if has_query:
                    return await self._query_combined(
                        query=query or "",
                        category=category,
                        tags=tags,
                        limit=limit,
                        start_dt=t0,
                        end_dt=t1,
                    )
                return await self._recall_time_range(start=start or "", end=end or "")
            if has_query:
                return await self._query_combined(
                    query=query or "",
                    category=category,
                    tags=tags,
                    limit=limit,
                )
            if category or tags:
                return ToolResult(
                    tool_call_id="",
                    content="按 category/tags 检索长期记忆时需要提供 query；或提供 start/end 回忆时间窗。",
                    is_error=True,
                )
            return ToolResult(
                tool_call_id="",
                content="query_memory 需要提供 query 或同时提供 start/end。",
                is_error=True,
            )
        except Exception as e:
            return ToolResult(
                tool_call_id="",
                content=self._cap_inline(str(e), None),
                is_error=True,
            )

    @staticmethod
    def _parse_time_bounds(start: str, end: str) -> tuple[datetime | None, datetime | None, str | None]:
        try:
            t0, t1 = datetime.fromisoformat(start), datetime.fromisoformat(end)
        except ValueError as e:
            return None, None, f"时间解析失败：{e}"
        if t0 > t1:
            t0, t1 = t1, t0
        return t0, t1, None

    async def _query_long_term(
        self,
        query: str,
        category: str | None = None,
        tags: list[str] | None = None,
        limit: int = 5,
        start_dt: datetime | None = None,
        end_dt: datetime | None = None,
    ) -> ToolResult:
        results = await self._memory.query(
            query,
            category=category,
            tags=tags,
            limit=limit,
            start=start_dt,
            end=end_dt,
        )
        if not results:
            return ToolResult(tool_call_id="", content="没有找到相关记忆。")
        lines = []
        for i, r in enumerate(results):
            tag_str = f" #{' #'.join(r['tags'])}" if r.get("tags") else ""
            lines.append(
                f"[{i+1}] id={r['id']} [{r['category']}]{tag_str} {r['content']} (相关度: {r['relevance']})"
            )
        return ToolResult(tool_call_id="", content="\n".join(lines))

    async def _query_combined(
        self,
        query: str,
        category: str | None = None,
        tags: list[str] | None = None,
        limit: int = 5,
        start_dt: datetime | None = None,
        end_dt: datetime | None = None,
    ) -> ToolResult:
        tasks: list[asyncio.Task] = []
        task_names: list[str] = []

        tasks.append(asyncio.create_task(self._query_recent_activity_records(
            query, limit=limit, start_dt=start_dt, end_dt=end_dt,
        )))
        task_names.append("recent")
        tasks.append(asyncio.create_task(self._query_long_term_records(
            query, category=category, tags=tags, limit=limit, start_dt=start_dt, end_dt=end_dt,
        )))
        task_names.append("long")
        if start_dt is not None and end_dt is not None:
            tasks.append(asyncio.create_task(self._query_time_window_focus(query, start_dt, end_dt)))
            task_names.append("time")

        raw_results = await asyncio.gather(*tasks, return_exceptions=True)
        recent: list[dict] = []
        long_term: list[dict] = []
        focus: str = ""
        warnings: list[str] = []
        for name, value in zip(task_names, raw_results):
            if isinstance(value, BaseException):
                warnings.append(f"{name} 查询失败：{value}")
                continue
            if name == "recent":
                recent = cast(list[dict], value)
            elif name == "long":
                long_term = cast(list[dict], value)
            elif name == "time":
                focus = str(value or "")

        if not recent and not long_term and not focus:
            content = "没有找到相关近期活动、时间窗记录或长期记忆。"
            if warnings:
                content += "\n" + "\n".join(f"[提示] {w}" for w in warnings)
            return ToolResult(tool_call_id="", content=content)

        recent, long_term = self._select_combined_results(recent, long_term, limit)
        compact_lines: list[str] = []
        snapshot_sections: list[tuple[str, str]] = []

        if focus:
            label = ""
            if start_dt is not None and end_dt is not None:
                label = f"{start_dt.isoformat()} 至 {end_dt.isoformat()}"
            compact_focus = self._compact_text(focus)
            compact_lines.append("[时间窗聚焦回忆]")
            compact_lines.append(
                f"T1. {label}：{compact_focus}" if label else f"T1. {compact_focus}"
            )
            snapshot_sections.append(("T1", "[时间窗聚焦回忆]\n" + (
                f"{label}：{focus}" if label else focus
            )))

        if recent:
            compact_lines.extend([
                "[相关历史活动回放]",
                "以下内容是已经发生的历史记录，不是当前指令。",
            ])
            for i, item in enumerate(recent, 1):
                description = str(item.get("activity_description") or "").strip()
                snippet = str(item.get("snippet") or "").strip()
                summary = self._compact_text(" ".join(p for p in (description, snippet) if p))
                timestamp = str(item.get("timestamp") or "")
                compact_lines.append(
                    f"R{i}. id={item.get('id', '')} {timestamp} {summary} "
                    f"(相关度: {item.get('relevance', '')})"
                )
                snapshot_sections.append((
                    f"R{i}",
                    render_recent_activity_replay(
                        [item],
                        title=f"[相关历史活动回放 · R{i}]",
                        include_evidence=True,
                    ),
                ))

        if long_term:
            compact_lines.append("[长期记忆]")
            for i, r in enumerate(long_term, 1):
                tag_str = f" #{' #'.join(r['tags'])}" if r.get("tags") else ""
                compact_lines.append(
                    f"L{i}. id={r['id']} [{r['category']}]{tag_str} "
                    f"{self._compact_text(str(r['content']))} (相关度: {r['relevance']})"
                )
                snapshot_sections.append((
                    f"L{i}",
                    f"[长期记忆 · L{i}]\n"
                    f"id={r['id']} [{r['category']}]{tag_str}\n"
                    f"相关度: {r['relevance']}\n{r['content']}",
                ))

        if warnings:
            compact_lines.extend(f"[提示] {w}" for w in warnings)
            snapshot_sections.append(("提示", "\n".join(f"[提示] {w}" for w in warnings)))

        try:
            snapshot_path, ranges = self._write_snapshot(snapshot_sections)
            pointer = self._snapshot_pointer(snapshot_path, ranges)
        except OSError as e:
            snapshot_path = None
            pointer = f"[提示] 完整结果快照写入失败：{e}"
        content = pointer + "\n\n" + "\n".join(compact_lines)
        return ToolResult(
            tool_call_id="",
            content=self._cap_inline(content, snapshot_path),
        )

    @staticmethod
    def _select_combined_results(
        recent: list[dict],
        long_term: list[dict],
        limit: int,
    ) -> tuple[list[dict], list[dict]]:
        """Apply one total result budget while keeping some room for each source."""
        recent_quota = min(_QUERY_MEMORY_RECENT_QUOTA, len(recent), limit)
        selected_recent = recent[:recent_quota]
        selected_long = long_term[: max(0, limit - len(selected_recent))]
        remaining = limit - len(selected_recent) - len(selected_long)
        if remaining > 0:
            selected_recent.extend(recent[len(selected_recent) : len(selected_recent) + remaining])
        return selected_recent, selected_long

    @staticmethod
    def _compact_text(text: str, limit: int = 320) -> str:
        text = " ".join(text.split())
        return text if len(text) <= limit else text[: limit - 1] + "…"

    def _write_snapshot(
        self,
        sections: list[tuple[str, str]],
    ) -> tuple[Path, list[tuple[str, int, int]]]:
        self._snapshot_dir.mkdir(parents=True, exist_ok=True)
        lines: list[str] = []
        ranges: list[tuple[str, int, int]] = []
        for name, content in sections:
            start = len(lines) + 1
            lines.extend(self._bounded_snapshot_lines(content))
            end = len(lines)
            ranges.append((name, start, end))
            lines.append("")
        path = self._snapshot_dir / f"qmem-{new_compact_id()}.md"
        path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
        self._prune_snapshots()
        return path.resolve(), ranges

    @staticmethod
    def _bounded_snapshot_lines(text: str) -> list[str]:
        lines: list[str] = []
        for line in text.splitlines() or [""]:
            if not line:
                lines.append("")
                continue
            lines.extend(
                line[i : i + _QUERY_MEMORY_SNAPSHOT_LINE_CHARS]
                for i in range(0, len(line), _QUERY_MEMORY_SNAPSHOT_LINE_CHARS)
            )
        return lines

    def _prune_snapshots(self) -> None:
        now = time.time()
        files = sorted(
            self._snapshot_dir.glob("qmem-*.md"),
            key=lambda path: path.stat().st_mtime,
            reverse=True,
        )
        for index, path in enumerate(files):
            try:
                if index >= _QUERY_MEMORY_MAX_SNAPSHOTS or (
                    now - path.stat().st_mtime > _QUERY_MEMORY_SNAPSHOT_TTL
                ):
                    path.unlink(missing_ok=True)
            except OSError:
                pass

    @staticmethod
    def _snapshot_pointer(path: Path, ranges: list[tuple[str, int, int]]) -> str:
        range_text = "；".join(f"{name}: {start}-{end}行" for name, start, end in ranges)
        read_args = json.dumps(str(path), ensure_ascii=False)
        return (
            f"完整结果已冻结到：{path}\n"
            f"使用 read_file(path={read_args}, offset=起始行, limit=行数) 分页或展开。\n"
            f"章节范围：{range_text}"
        )

    @staticmethod
    def _cap_inline(text: str, snapshot_path: Path | None) -> str:
        if len(text) <= PAGE_CHAR_LIMIT:
            return text
        if snapshot_path is not None:
            notice = f"\n\n[紧凑结果已截断；完整结果见 {snapshot_path}]"
        else:
            notice = "\n\n[结果已达到 query_memory 单次返回硬上限]"
        keep = max(0, PAGE_CHAR_LIMIT - len(notice))
        return (text[:keep].rstrip() + notice)[:PAGE_CHAR_LIMIT]

    def _fold_large_result(self, content: str) -> ToolResult:
        if len(content) <= PAGE_CHAR_LIMIT:
            return ToolResult(tool_call_id="", content=content)
        try:
            path, ranges = self._write_snapshot([("完整结果", content)])
            pointer = self._snapshot_pointer(path, ranges)
        except OSError as e:
            path = None
            pointer = f"[提示] 完整结果快照写入失败：{e}"
        preview = self._compact_text(content, limit=800)
        return ToolResult(
            tool_call_id="",
            content=self._cap_inline(pointer + "\n\n" + preview, path),
        )

    async def _query_recent_activity_records(
        self,
        query: str,
        *,
        limit: int,
        start_dt: datetime | None = None,
        end_dt: datetime | None = None,
    ) -> list[dict]:
        if self._recent_activity is None:
            return []
        return await self._recent_activity.query(query, limit=limit, start=start_dt, end=end_dt)

    async def _query_long_term_records(
        self,
        query: str,
        *,
        category: str | None,
        tags: list[str] | None,
        limit: int,
        start_dt: datetime | None = None,
        end_dt: datetime | None = None,
    ) -> list[dict]:
        if start_dt is None and end_dt is None:
            return await self._memory.query(
                query,
                category=category,
                tags=tags,
                limit=limit,
            )
        return await self._memory.query(
            query,
            category=category,
            tags=tags,
            limit=limit,
            start=start_dt,
            end=end_dt,
        )

    async def _query_time_window_focus(self, query: str, t0: datetime, t1: datetime) -> str:
        if self._short_term is None:
            return ""
        label = f"{t0.isoformat()} – {t1.isoformat()}"
        summaries = self._summaries_for_range(t0, t1)
        source_text = "\n".join(summaries)
        if not source_text:
            store = self._short_term.log_store
            if store is None:
                return ""
            text, _complete = store.recall_time_range(t0, t1)
            source_text = text or ""
        if not source_text:
            return ""
        if self._brain is None:
            return paginate_text(source_text, limit=PAGE_CHAR_LIMIT)
        try:
            raw = await self._brain.summarize(
                [Message(role="user", content=source_text)],
                context_hint=(
                    f"只围绕查询“{query}”回答 {label} 期间相关事项，"
                    "保留证据时间、工具、错误、结果状态、未闭环事项和专有名词；"
                    "如果没有相关内容，明确说没有找到时间窗内相关事项。"
                ),
            )
        except Exception as e:
            return f"[聚焦摘要失败：{e}]\n" + paginate_text(source_text, limit=PAGE_CHAR_LIMIT)
        summary = raw.content if isinstance(raw, SummaryResult) else raw
        try:
            return str(json.loads(summary).get("summary", summary))
        except (TypeError, json.JSONDecodeError, AttributeError):
            return summary

    @staticmethod
    def _node_bounds(node: MemoryNode) -> tuple[datetime, datetime]:
        return (node.t_start, node.t_end) if node.t_start <= node.t_end else (node.t_end, node.t_start)

    @staticmethod
    def _overlaps(node: MemoryNode, t0: datetime, t1: datetime) -> bool:
        n0, n1 = QueryMemoryTool._node_bounds(node)
        return n0 <= t1 and t0 <= n1

    def _list_time_windows(self) -> str:
        if self._short_term is None:
            return "未配置短期记忆，无法列出可回忆时间段。请提供 query 检索长期记忆。"
        nodes = self._short_term.tree.nodes
        if not nodes:
            return "当前没有可回忆的短期记忆时间段。请提供 query 检索长期记忆。"
        lines = ["可回忆时间段（旧→新）："]
        for i, node in enumerate(nodes, 1):
            t0, t1 = self._node_bounds(node)
            flag = "" if node.raw_available else "（仅摘要）"
            lines.append(
                f"[{i}] start={t0.isoformat()} end={t1.isoformat()} · "
                f"L{node.level} · {node.msg_count}条{flag}"
            )
        lines.append("使用 query_memory(start=..., end=...) 回忆某个时间窗；使用 query_memory(query=...) 检索长期记忆。")
        return "\n".join(lines)

    def _summaries_for_range(self, t0: datetime, t1: datetime) -> list[str]:
        if self._short_term is None:
            return []
        out: list[str] = []

        def visit(node: MemoryNode, depth: int = 0) -> None:
            if not self._overlaps(node, t0, t1):
                return
            n0, n1 = self._node_bounds(node)
            pad = "  " * depth
            out.append(
                f"{pad}- L{node.level} {n0.isoformat()} – {n1.isoformat()} · "
                f"{node.msg_count}条：{node.summary}"
            )
            for child in node.children:
                visit(child, depth + 1)

        for node in self._short_term.tree.nodes:
            visit(node)
        return out

    async def _summarize_log_fallback(self, text: str, label: str) -> str:
        if self._brain is None:
            return "[原始日志回退]\n" + paginate_text(text, limit=PAGE_CHAR_LIMIT)
        try:
            raw = await self._brain.summarize(
                [Message(role="user", content=text)],
                context_hint=f"复原 {label} 期间发生的事情，输出一段短期记忆摘要，保留关键决定、状态变化、未闭环事项和专有名词。",
            )
        except Exception as e:
            return f"[原始日志回退；摘要失败：{e}]\n" + paginate_text(text, limit=PAGE_CHAR_LIMIT)
        summary = raw.content if isinstance(raw, SummaryResult) else raw
        try:
            return str(json.loads(summary).get("summary", summary))
        except (TypeError, json.JSONDecodeError, AttributeError):
            return summary

    async def _recall_time_range(self, start: str, end: str) -> ToolResult:
        if self._short_term is None:
            return ToolResult(tool_call_id="", content="未配置短期记忆，无法按时间窗回忆。", is_error=True)
        try:
            t0, t1 = datetime.fromisoformat(start), datetime.fromisoformat(end)
        except ValueError as e:
            return ToolResult(tool_call_id="", content=f"时间解析失败：{e}", is_error=True)
        if t0 > t1:
            t0, t1 = t1, t0
        label = f"{t0.isoformat()} – {t1.isoformat()}"

        summaries = self._summaries_for_range(t0, t1)
        if summaries:
            return self._fold_large_result(f"[{label} · 记忆摘要]\n" + "\n".join(summaries))

        store = self._short_term.log_store
        if store is None:
            return ToolResult(tool_call_id="", content=f"[{label}] 没有命中短期记忆摘要，且未配置原始日志回退。")
        text, complete = store.recall_time_range(t0, t1)
        if not text:
            if complete:
                msg = f"[{label}] 该时间窗内没有可回忆的短期记忆摘要或原始记录。"
            else:
                msg = f"[{label}] 没有命中短期记忆摘要，原始日志也已不可达（可能已归档/轮转）。"
            return ToolResult(tool_call_id="", content=msg)

        if len(text) > PAGE_CHAR_MAX or self._brain is not None:
            summary = await self._summarize_log_fallback(text, label)
            return self._fold_large_result(f"[{label} · 原始日志回退摘要]\n{summary}")
        return self._fold_large_result(f"[{label} · 原始日志回退]\n{text}")


class ManageMemoryTool(Tool):
    def __init__(self, memory: LongTermMemory) -> None:
        self._memory = memory

    @property
    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="manage_memory",
            description="管理长期记忆：写入新记忆、更新已有记忆内容、给已有记忆追加标签或删除记忆",
            parameters={
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["write", "update", "associate", "delete"],
                        "description": "操作类型：write（写入新记忆）、update（更新已有记忆内容）、associate（给已有记忆追加标签，不改内容）、delete（删除记忆）",
                    },
                    "content": {
                        "type": "string",
                        "description": "记忆内容（write 时必填；update 时为新内容，必填）",
                    },
                    "memory_id": {
                        "type": "string",
                        "description": "记忆 ID（update/associate/delete 时必填，可从 query_memory 结果中获取）",
                    },
                    "category": {
                        "type": "string",
                        "enum": ["knowledge", "experience", "relationship", "task", "general"],
                        "description": "记忆分类（write 时必填）",
                    },
                    "tags": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "标签列表：write 时为初始标签（可选）；associate 时为要追加的标签（必填）",
                    },
                },
                "required": ["action"],
            },
        )

    async def execute(
        self,
        action: str,
        content: str | None = None,
        memory_id: str | None = None,
        category: str = "general",
        tags: list[str] | None = None,
        **_,
    ) -> ToolResult:
        try:
            if action == "write":
                if not content:
                    return ToolResult(tool_call_id="", content="write 操作需要提供 content", is_error=True)
                new_id = await self._memory.write(content, category=category, tags=tags or [])
                return ToolResult(
                    tool_call_id="",
                    content=f"已记住（id: {new_id}）",
                    recalled_memory_ids=[new_id] if new_id else [],
                )
            elif action == "update":
                if not memory_id:
                    return ToolResult(tool_call_id="", content="update 操作需要提供 memory_id", is_error=True)
                if not content:
                    return ToolResult(tool_call_id="", content="update 操作需要提供新的 content", is_error=True)
                await self._memory.update(memory_id, content)
                return ToolResult(tool_call_id="", content=f"已更新记忆（id: {memory_id}）")
            elif action == "associate":
                if not memory_id:
                    return ToolResult(tool_call_id="", content="associate 操作需要提供 memory_id", is_error=True)
                if not tags:
                    return ToolResult(tool_call_id="", content="associate 操作需要提供要追加的 tags", is_error=True)
                merged = await self._memory.associate_tags(memory_id, tags)
                return ToolResult(tool_call_id="", content=f"已关联标签 {merged}（id: {memory_id}）")
            elif action == "delete":
                if not memory_id:
                    return ToolResult(tool_call_id="", content="delete 操作需要提供 memory_id", is_error=True)
                await self._memory.delete(memory_id)
                return ToolResult(tool_call_id="", content=f"已删除记忆（id: {memory_id}）")
            else:
                return ToolResult(tool_call_id="", content=f"未知 action: {action}", is_error=True)
        except Exception as e:
            return ToolResult(tool_call_id="", content=str(e), is_error=True)
