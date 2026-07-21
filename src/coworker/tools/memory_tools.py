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
from coworker.i18n import tr
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
        self._snapshot_dir = (
            Path(snapshot_dir)
            if snapshot_dir is not None
            else (Path(tempfile.gettempdir()) / "coworker" / "query-memory")
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
                    "query": {
                        "type": "string",
                        "description": "长期记忆检索关键词或描述。不可与 start/end 同时使用",
                    },
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
                    "start": {
                        "type": "string",
                        "description": "时间窗起点（ISO，如 2026-06-07T09:00:00）",
                    },
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
                    content=tr("memory.query.legacy_args", arguments=", ".join(old_args)),
                    is_error=True,
                )
            has_query = bool(query and query.strip())
            has_start = bool(start)
            has_end = bool(end)
            if has_start or has_end:
                if not (has_start and has_end):
                    return ToolResult(
                        tool_call_id="",
                        content=tr("memory.query.needs_range"),
                        is_error=True,
                    )
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
                    content=tr("memory.query.filter_needs_query"),
                    is_error=True,
                )
            return ToolResult(
                tool_call_id="",
                content=tr("memory.query.needs_input"),
                is_error=True,
            )
        except Exception as e:
            return ToolResult(
                tool_call_id="",
                content=self._cap_inline(str(e), None),
                is_error=True,
            )

    @staticmethod
    def _parse_time_bounds(
        start: str, end: str
    ) -> tuple[datetime | None, datetime | None, str | None]:
        try:
            t0, t1 = datetime.fromisoformat(start), datetime.fromisoformat(end)
        except ValueError as e:
            return None, None, tr("memory.query.time_parse_failed", error=e)
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
            return ToolResult(tool_call_id="", content=tr("memory.query.none"))
        lines = []
        for i, r in enumerate(results):
            tag_str = f" #{' #'.join(r['tags'])}" if r.get("tags") else ""
            lines.append(
                f"[{i + 1}] id={r['id']} [{r['category']}]{tag_str} {r['content']} "
                f"({tr('memory.query.relevance', value=r['relevance'])})"
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

        tasks.append(
            asyncio.create_task(
                self._query_recent_activity_records(
                    query,
                    limit=limit,
                    start_dt=start_dt,
                    end_dt=end_dt,
                )
            )
        )
        task_names.append("recent")
        tasks.append(
            asyncio.create_task(
                self._query_long_term_records(
                    query,
                    category=category,
                    tags=tags,
                    limit=limit,
                    start_dt=start_dt,
                    end_dt=end_dt,
                )
            )
        )
        task_names.append("long")
        if start_dt is not None and end_dt is not None:
            tasks.append(
                asyncio.create_task(self._query_time_window_focus(query, start_dt, end_dt))
            )
            task_names.append("time")

        raw_results = await asyncio.gather(*tasks, return_exceptions=True)
        recent: list[dict] = []
        long_term: list[dict] = []
        focus: str = ""
        warnings: list[str] = []
        for name, value in zip(task_names, raw_results):
            if isinstance(value, BaseException):
                warnings.append(tr("memory.query.query_failed", source=name, error=value))
                continue
            if name == "recent":
                recent = cast(list[dict], value)
            elif name == "long":
                long_term = cast(list[dict], value)
            elif name == "time":
                focus = str(value or "")

        if not recent and not long_term and not focus:
            content = tr("memory.query.combined_none")
            if warnings:
                content += "\n" + "\n".join(tr("memory.query.hint", message=w) for w in warnings)
            return ToolResult(tool_call_id="", content=content)

        recent, long_term = self._select_combined_results(recent, long_term, limit)
        compact_lines: list[str] = []
        snapshot_sections: list[tuple[str, str]] = []

        if focus:
            label = ""
            if start_dt is not None and end_dt is not None:
                label = tr(
                    "memory.query.range",
                    start=start_dt.isoformat(),
                    end=end_dt.isoformat(),
                )
            compact_focus = self._compact_text(focus)
            compact_lines.append(tr("memory.query.focus_title"))
            compact_lines.append(
                f"T1. {label}: {compact_focus}" if label else f"T1. {compact_focus}"
            )
            snapshot_sections.append(
                (
                    "T1",
                    tr("memory.query.focus_title")
                    + "\n"
                    + (f"{label}: {focus}" if label else focus),
                )
            )

        if recent:
            compact_lines.extend(
                [
                    tr("memory.query.recent_title"),
                    tr("memory.query.recent_intro"),
                ]
            )
            for i, item in enumerate(recent, 1):
                description = str(item.get("activity_description") or "").strip()
                snippet = str(item.get("snippet") or "").strip()
                summary = self._compact_text(" ".join(p for p in (description, snippet) if p))
                timestamp = str(item.get("timestamp") or "")
                compact_lines.append(
                    f"R{i}. id={item.get('id', '')} {timestamp} {summary} "
                    f"({tr('memory.query.relevance', value=item.get('relevance', ''))})"
                )
                snapshot_sections.append(
                    (
                        f"R{i}",
                        render_recent_activity_replay(
                            [item],
                            title=tr("memory.query.recent_snapshot_title", id=f"R{i}"),
                            include_evidence=True,
                        ),
                    )
                )

        if long_term:
            compact_lines.append(tr("memory.query.long_title"))
            for i, r in enumerate(long_term, 1):
                tag_str = f" #{' #'.join(r['tags'])}" if r.get("tags") else ""
                compact_lines.append(
                    f"L{i}. id={r['id']} [{r['category']}]{tag_str} "
                    f"{self._compact_text(str(r['content']))} "
                    f"({tr('memory.query.relevance', value=r['relevance'])})"
                )
                snapshot_sections.append(
                    (
                        f"L{i}",
                        tr("memory.query.long_snapshot_title", id=f"L{i}") + "\n"
                        f"id={r['id']} [{r['category']}]{tag_str}\n"
                        f"{tr('memory.query.relevance', value=r['relevance'])}\n{r['content']}",
                    )
                )

        if warnings:
            compact_lines.extend(tr("memory.query.hint", message=w) for w in warnings)
            snapshot_sections.append(
                (
                    "note",
                    "\n".join(tr("memory.query.hint", message=w) for w in warnings),
                )
            )

        try:
            snapshot_path, ranges = self._write_snapshot(snapshot_sections)
            pointer = self._snapshot_pointer(snapshot_path, ranges)
        except OSError as e:
            snapshot_path = None
            pointer = tr("memory.query.snapshot_failed", error=e)
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
        range_text = tr("memory.query.range_separator").join(
            tr("memory.query.line_range", name=name, start=start, end=end)
            for name, start, end in ranges
        )
        read_args = json.dumps(str(path), ensure_ascii=False)
        return tr(
            "memory.query.snapshot_pointer",
            path=path,
            arguments=read_args,
            ranges=range_text,
        )

    @staticmethod
    def _cap_inline(text: str, snapshot_path: Path | None) -> str:
        if len(text) <= PAGE_CHAR_LIMIT:
            return text
        if snapshot_path is not None:
            notice = "\n\n" + tr("memory.query.compact_truncated", path=snapshot_path)
        else:
            notice = "\n\n" + tr("memory.query.hard_limit")
        keep = max(0, PAGE_CHAR_LIMIT - len(notice))
        return (text[:keep].rstrip() + notice)[:PAGE_CHAR_LIMIT]

    def _fold_large_result(self, content: str) -> ToolResult:
        if len(content) <= PAGE_CHAR_LIMIT:
            return ToolResult(tool_call_id="", content=content)
        try:
            path, ranges = self._write_snapshot([(tr("memory.query.complete_result"), content)])
            pointer = self._snapshot_pointer(path, ranges)
        except OSError as e:
            path = None
            pointer = tr("memory.query.snapshot_failed", error=e)
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
        label = tr("memory.query.range", start=t0.isoformat(), end=t1.isoformat())
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
                context_hint=tr("memory.query.focus_hint", query=query, range=label),
            )
        except Exception as e:
            return (
                tr("memory.query.focus_failed", error=e)
                + "\n"
                + paginate_text(source_text, limit=PAGE_CHAR_LIMIT)
            )
        summary = raw.content if isinstance(raw, SummaryResult) else raw
        try:
            return str(json.loads(summary).get("summary", summary))
        except (TypeError, json.JSONDecodeError, AttributeError):
            return summary

    @staticmethod
    def _node_bounds(node: MemoryNode) -> tuple[datetime, datetime]:
        return (
            (node.t_start, node.t_end) if node.t_start <= node.t_end else (node.t_end, node.t_start)
        )

    @staticmethod
    def _overlaps(node: MemoryNode, t0: datetime, t1: datetime) -> bool:
        n0, n1 = QueryMemoryTool._node_bounds(node)
        return n0 <= t1 and t0 <= n1

    def _list_time_windows(self) -> str:
        if self._short_term is None:
            return tr("memory.query.periods_unconfigured")
        nodes = self._short_term.tree.nodes
        if not nodes:
            return tr("memory.query.periods_empty")
        lines = [tr("memory.query.periods_title")]
        for i, node in enumerate(nodes, 1):
            t0, t1 = self._node_bounds(node)
            flag = "" if node.raw_available else tr("memory.query.summary_only")
            lines.append(
                f"[{i}] "
                + tr(
                    "memory.query.period_line",
                    start=t0.isoformat(),
                    end=t1.isoformat(),
                    level=node.level,
                    count=node.msg_count,
                    flag=flag,
                )
            )
        lines.append(tr("memory.query.periods_instruction"))
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
                + tr(
                    "memory.query.summary_line",
                    count=node.msg_count,
                    summary=node.summary,
                )
            )
            for child in node.children:
                visit(child, depth + 1)

        for node in self._short_term.tree.nodes:
            visit(node)
        return out

    async def _summarize_log_fallback(self, text: str, label: str) -> str:
        if self._brain is None:
            return (
                tr("memory.query.raw_fallback") + "\n" + paginate_text(text, limit=PAGE_CHAR_LIMIT)
            )
        try:
            raw = await self._brain.summarize(
                [Message(role="user", content=text)],
                context_hint=tr("memory.query.restore_hint", range=label),
            )
        except Exception as e:
            return (
                tr("memory.query.raw_summary_failed", error=e)
                + "\n"
                + paginate_text(text, limit=PAGE_CHAR_LIMIT)
            )
        summary = raw.content if isinstance(raw, SummaryResult) else raw
        try:
            return str(json.loads(summary).get("summary", summary))
        except (TypeError, json.JSONDecodeError, AttributeError):
            return summary

    async def _recall_time_range(self, start: str, end: str) -> ToolResult:
        if self._short_term is None:
            return ToolResult(
                tool_call_id="",
                content=tr("memory.query.recall_unconfigured"),
                is_error=True,
            )
        try:
            t0, t1 = datetime.fromisoformat(start), datetime.fromisoformat(end)
        except ValueError as e:
            return ToolResult(
                tool_call_id="",
                content=tr("memory.query.time_parse_failed", error=e),
                is_error=True,
            )
        if t0 > t1:
            t0, t1 = t1, t0
        label = tr("memory.query.range", start=t0.isoformat(), end=t1.isoformat())

        summaries = self._summaries_for_range(t0, t1)
        if summaries:
            return self._fold_large_result(
                tr("memory.query.summary_header", range=label) + "\n" + "\n".join(summaries)
            )

        store = self._short_term.log_store
        if store is None:
            return ToolResult(
                tool_call_id="", content=tr("memory.query.summary_missing", range=label)
            )
        text, complete = store.recall_time_range(t0, t1)
        if not text:
            if complete:
                msg = tr("memory.query.records_missing", range=label)
            else:
                msg = tr("memory.query.records_archived", range=label)
            return ToolResult(tool_call_id="", content=msg)

        if len(text) > PAGE_CHAR_MAX or self._brain is not None:
            summary = await self._summarize_log_fallback(text, label)
            return self._fold_large_result(
                tr("memory.query.raw_summary_header", range=label) + "\n" + summary
            )
        return self._fold_large_result(tr("memory.query.raw_header", range=label) + "\n" + text)


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
                    return ToolResult(
                        tool_call_id="",
                        content=tr("memory.query.write_needs_content"),
                        is_error=True,
                    )
                new_id = await self._memory.write(content, category=category, tags=tags or [])
                return ToolResult(
                    tool_call_id="",
                    content=tr("memory.query.remembered", id=new_id),
                    recalled_memory_ids=[new_id] if new_id else [],
                )
            elif action == "update":
                if not memory_id:
                    return ToolResult(
                        tool_call_id="",
                        content=tr("memory.query.update_needs_id"),
                        is_error=True,
                    )
                if not content:
                    return ToolResult(
                        tool_call_id="",
                        content=tr("memory.query.update_needs_content"),
                        is_error=True,
                    )
                await self._memory.update(memory_id, content)
                return ToolResult(tool_call_id="", content=tr("memory.query.updated", id=memory_id))
            elif action == "associate":
                if not memory_id:
                    return ToolResult(
                        tool_call_id="",
                        content=tr("memory.query.associate_needs_id"),
                        is_error=True,
                    )
                if not tags:
                    return ToolResult(
                        tool_call_id="",
                        content=tr("memory.query.associate_needs_tags"),
                        is_error=True,
                    )
                merged = await self._memory.associate_tags(memory_id, tags)
                return ToolResult(
                    tool_call_id="",
                    content=tr("memory.query.associated", tags=merged, id=memory_id),
                )
            elif action == "delete":
                if not memory_id:
                    return ToolResult(
                        tool_call_id="",
                        content=tr("memory.query.delete_needs_id"),
                        is_error=True,
                    )
                await self._memory.delete(memory_id)
                return ToolResult(tool_call_id="", content=tr("memory.query.deleted", id=memory_id))
            else:
                return ToolResult(
                    tool_call_id="",
                    content=tr("tool_result.common.unknown_action", action=action),
                    is_error=True,
                )
        except Exception as e:
            return ToolResult(tool_call_id="", content=str(e), is_error=True)
