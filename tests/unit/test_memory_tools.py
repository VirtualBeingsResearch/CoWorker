from __future__ import annotations

from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest
from loguru import logger

from coworker.memory.base import MemoryRecord
from coworker.memory.long_term import MemoryWriteResult
from coworker.memory.memory_tree import MemoryNode
from coworker.memory.short_term import ShortTermMemory
from coworker.tools.memory_tools import ManageMemoryTool, QueryMemoryTool


def _make_memory(query_results=None, write_result=None):
    memory = MagicMock()
    memory.query = AsyncMock(return_value=query_results or [])
    memory.write = AsyncMock(return_value=write_result or MemoryWriteResult(status="written", memory_id="mem-abc123"))
    memory.update = AsyncMock()
    memory.associate_tags = AsyncMock(return_value=["product", "bug"])
    memory.delete = AsyncMock()
    return memory


class _FailingLogStore:
    def recall_time_range(self, *_):
        raise AssertionError("tree summary hit should not read log store")


class _LogStore:
    def __init__(self, text: str | None = None, complete: bool = True) -> None:
        self.text = text
        self.complete = complete
        self.calls = []

    def recall_time_range(self, t0, t1):
        self.calls.append((t0, t1))
        return self.text, self.complete


def _short_term_with_node(log_store=None) -> tuple[ShortTermMemory, MemoryNode]:
    base = datetime(2026, 6, 1, 9, 0, 0)
    mem = ShortTermMemory(log_store=log_store)
    child = MemoryNode(
        level=0,
        summary="子摘要：用户确认了方案。",
        t_start=base + timedelta(minutes=10),
        t_end=base + timedelta(minutes=20),
        msg_count=2,
    )
    node = MemoryNode(
        level=1,
        summary="父摘要：讨论 query_memory 合并。",
        t_start=base,
        t_end=base + timedelta(hours=1),
        msg_count=5,
        children=[child],
    )
    mem.tree.nodes.append(node)
    return mem, node


class TestQueryMemoryTool:
    def test_schema_exposes_simplified_parameters(self):
        props = QueryMemoryTool(_make_memory()).definition.parameters["properties"]

        assert "query" in props
        assert "start" in props
        assert "end" in props
        assert "node" not in props
        assert "in_tree" not in props
        assert "offset" not in props
        assert props["limit"]["default"] == 5
        assert props["limit"]["maximum"] == 10

    @pytest.mark.asyncio
    async def test_empty_results(self):
        tool = QueryMemoryTool(_make_memory([]))
        result = await tool.execute(query="test")
        assert not result.is_error
        assert "没有找到" in result.content

    @pytest.mark.asyncio
    async def test_results_include_id(self):
        records = [
            MemoryRecord(id="id-001", category="knowledge", content="Python 是一种编程语言", tags=[], timestamp=""),
            MemoryRecord(id="id-002", category="task", content="明天开会", tags=[], timestamp=""),
        ]
        tool = QueryMemoryTool(_make_memory(records))
        result = await tool.execute(query="编程")
        assert not result.is_error
        assert "id-001" in result.content
        assert "id-002" in result.content
        assert "knowledge" in result.content

    @pytest.mark.asyncio
    async def test_passes_category_and_limit(self):
        memory = _make_memory([])
        tool = QueryMemoryTool(memory)
        await tool.execute(query="q", category="task", limit=3)
        memory.query.assert_called_once_with("q", category="task", tags=None, limit=3, manual=True)

    @pytest.mark.asyncio
    async def test_passes_tags(self):
        memory = _make_memory([])
        tool = QueryMemoryTool(memory)
        await tool.execute(query="q", tags=["product", "bug"], limit=5)
        memory.query.assert_called_once_with("q", category=None, tags=["product", "bug"], limit=5, manual=True)

    @pytest.mark.asyncio
    async def test_results_include_relevance_score(self):
        records = [
            MemoryRecord(
                id="id-001",
                category="knowledge",
                content="低相关记忆",
                tags=[],
                extra={"score": 0.31},
            ),
            MemoryRecord(id="id-002", category="task", content="无分数", tags=[]),
        ]
        tool = QueryMemoryTool(_make_memory(records))
        result = await tool.execute(query="检索")
        assert not result.is_error
        assert "score=0.31" in result.content

    @pytest.mark.asyncio
    async def test_results_show_tags(self):
        records = [
            MemoryRecord(id="id-001", category="knowledge", content="内容", tags=["product", "bug"], timestamp=""),
        ]
        tool = QueryMemoryTool(_make_memory(records))
        result = await tool.execute(query="x")
        assert not result.is_error
        assert "#product" in result.content
        assert "#bug" in result.content

    @pytest.mark.asyncio
    async def test_error_propagated(self):
        memory = _make_memory()
        memory.query = AsyncMock(side_effect=RuntimeError("db error"))
        tool = QueryMemoryTool(memory)
        result = await tool.execute(query="x")
        assert not result.is_error
        assert "db error" in result.content

    @pytest.mark.asyncio
    async def test_no_args_errors(self):
        short_term, _node = _short_term_with_node()
        tool = QueryMemoryTool(_make_memory(), short_term=short_term)

        result = await tool.execute()

        assert result.is_error
        assert "需要提供 query" in result.content

    @pytest.mark.asyncio
    async def test_time_window_uses_tree_summary_without_log_store(self):
        short_term, node = _short_term_with_node(log_store=_FailingLogStore())
        tool = QueryMemoryTool(_make_memory(), short_term=short_term)

        result = await tool.execute(start=node.t_start.isoformat(), end=node.t_end.isoformat())

        assert not result.is_error
        assert "记忆摘要" in result.content
        assert "父摘要" in result.content
        assert "子摘要" in result.content

    @pytest.mark.asyncio
    async def test_time_window_falls_back_to_log_and_summarizes(self):
        store = _LogStore(text="[用户] 讨论时间窗回忆")
        short_term = ShortTermMemory(log_store=store)
        brain = MagicMock()
        brain.summarize = AsyncMock(return_value='{"summary":"日志摘要"}')
        tool = QueryMemoryTool(_make_memory(), short_term=short_term, brain=brain)

        result = await tool.execute(start="2026-06-01T09:00:00", end="2026-06-01T10:00:00")

        assert not result.is_error
        assert "原始日志回退摘要" in result.content
        assert "日志摘要" in result.content
        assert len(store.calls) == 1
        brain.summarize.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_time_window_returns_digest_when_summarize_fails(self):
        store = _LogStore(text="[用户] 摘要失败时保留原始回退")
        short_term = ShortTermMemory(log_store=store)
        brain = MagicMock()
        brain.summarize = AsyncMock(side_effect=RuntimeError("summary down"))
        tool = QueryMemoryTool(_make_memory(), short_term=short_term, brain=brain)

        result = await tool.execute(start="2026-06-01T09:00:00", end="2026-06-01T10:00:00")

        assert not result.is_error
        assert "摘要失败" in result.content
        assert "保留原始回退" in result.content

    @pytest.mark.asyncio
    async def test_query_and_time_window_are_combined(self):
        store = _LogStore(text="[用户] 提到了方案")
        short_term = ShortTermMemory(log_store=store)
        brain = MagicMock()
        brain.summarize = AsyncMock(return_value='{"summary":"时间窗聚焦摘要"}')
        tool = QueryMemoryTool(
            _make_memory([]),
            short_term=short_term,
            brain=brain,
        )

        result = await tool.execute(query="方案", start="2026-06-01T09:00:00", end="2026-06-01T10:00:00")

        assert not result.is_error
        # 时间窗聚焦回忆路径走 log_store.recall_time_range，start/end 透传
        assert store.calls
        assert store.calls[0][0].isoformat() == "2026-06-01T09:00:00"
        assert store.calls[0][1].isoformat() == "2026-06-01T10:00:00"

    @pytest.mark.asyncio
    async def test_query_combined_does_not_emit_comprehensive_summary(self):
        brain = MagicMock()
        brain.summarize = AsyncMock(return_value='{"summary":"不应出现"}')
        tool = QueryMemoryTool(
            _make_memory([
                MemoryRecord(
                    id="id-001",
                    category="task",
                    content="长期记忆证据",
                    tags=[],
                )
            ]),
            brain=brain,
        )

        result = await tool.execute(query="证据")

        assert not result.is_error
        assert "[综合摘要]" not in result.content
        assert "不应出现" not in result.content
        assert "[长期记忆]" in result.content
        brain.summarize.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_long_term_limit_is_respected(self, tmp_path):
        long_term = [
            MemoryRecord(
                id=f"long-{i}",
                category="knowledge",
                content=f"长期记忆 {i}",
                tags=[],
            )
            for i in range(8)
        ]
        tool = QueryMemoryTool(
            _make_memory(long_term),
            snapshot_dir=tmp_path,
        )

        result = await tool.execute(query="内容", limit=5)

        assert not result.is_error
        result_lines = result.content.splitlines()
        assert any(line.startswith("L1.") for line in result_lines)
        assert any(line.startswith("L5.") for line in result_lines)
        assert not any(line.startswith("L6.") for line in result_lines)

    @pytest.mark.asyncio
    async def test_combined_result_is_compact_and_full_text_is_frozen_to_file(self, tmp_path):
        full_content = "完整记忆正文" * 800
        tool = QueryMemoryTool(
            _make_memory([MemoryRecord(
                id="long-large",
                category="knowledge",
                content=full_content,
                tags=["large"],
            )]),
            snapshot_dir=tmp_path,
        )

        result = await tool.execute(query="完整记忆")

        assert not result.is_error
        assert len(result.content) <= 3_000
        assert "完整结果已冻结到" in result.content
        assert full_content not in result.content
        snapshots = list(tmp_path.glob("qmem-*.md"))
        assert len(snapshots) == 1
        frozen = snapshots[0].read_text(encoding="utf-8")
        assert full_content in frozen.replace("\n", "")
        assert max(map(len, frozen.splitlines())) <= 500

    @pytest.mark.asyncio
    async def test_reversed_time_window_is_normalized(self):
        store = _LogStore(text="[用户] 反向时间窗")
        short_term = ShortTermMemory(log_store=store)
        tool = QueryMemoryTool(_make_memory(), short_term=short_term)

        result = await tool.execute(start="2026-06-01T10:00:00", end="2026-06-01T09:00:00")

        assert not result.is_error
        assert len(store.calls) == 1
        assert store.calls[0][0] == datetime(2026, 6, 1, 9, 0, 0)
        assert store.calls[0][1] == datetime(2026, 6, 1, 10, 0, 0)

    @pytest.mark.asyncio
    async def test_time_window_requires_start_and_end(self):
        tool = QueryMemoryTool(_make_memory(), short_term=ShortTermMemory())

        result = await tool.execute(start="2026-06-01T09:00:00")

        assert result.is_error
        assert "同时提供 start 和 end" in result.content

    @pytest.mark.asyncio
    async def test_time_window_without_short_term_errors(self):
        tool = QueryMemoryTool(_make_memory())

        result = await tool.execute(start="2026-06-01T09:00:00", end="2026-06-01T10:00:00")

        assert result.is_error
        assert "未配置短期记忆" in result.content

    @pytest.mark.asyncio
    async def test_old_time_window_parameters_are_rejected(self):
        tool = QueryMemoryTool(_make_memory(), short_term=ShortTermMemory())

        result = await tool.execute(node=0)

        assert result.is_error
        assert "不再支持参数 node" in result.content


class TestManageMemoryTool:
    # ── write ──────────────────────────────────────────────────────────────
    @pytest.mark.asyncio
    async def test_write_returns_id(self):
        memory = _make_memory()
        tool = ManageMemoryTool(memory)
        result = await tool.execute(action="write", content="新知识", category="knowledge")
        assert not result.is_error
        assert "mem-abc123" in result.content
        assert result.recalled_memory_ids == ["mem-abc123"]
        memory.write.assert_called_once_with("新知识", category="knowledge", tags=[])

    @pytest.mark.asyncio
    async def test_write_empty_reports_not_stored(self):
        memory = _make_memory(write_result=MemoryWriteResult(status="empty"))
        tool = ManageMemoryTool(memory)
        result = await tool.execute(action="write", content="无可提取内容")
        assert not result.is_error
        assert result.recalled_memory_ids == []
        assert "未写入" in result.content

    @pytest.mark.asyncio
    async def test_write_with_tags(self):
        memory = _make_memory()
        tool = ManageMemoryTool(memory)
        await tool.execute(action="write", content="c", category="general", tags=["a", "b"])
        memory.write.assert_called_once_with("c", category="general", tags=["a", "b"])

    @pytest.mark.asyncio
    async def test_write_missing_content(self):
        tool = ManageMemoryTool(_make_memory())
        result = await tool.execute(action="write", category="general")
        assert result.is_error
        assert "content" in result.content

    # ── update ─────────────────────────────────────────────────────────────
    @pytest.mark.asyncio
    async def test_update_success(self):
        memory = _make_memory()
        tool = ManageMemoryTool(memory)
        result = await tool.execute(action="update", memory_id="id-001", content="修正内容")
        assert not result.is_error
        assert "id-001" in result.content
        memory.update.assert_called_once_with("id-001", "修正内容")

    @pytest.mark.asyncio
    async def test_update_missing_memory_id(self):
        tool = ManageMemoryTool(_make_memory())
        result = await tool.execute(action="update", content="新内容")
        assert result.is_error
        assert "memory_id" in result.content

    @pytest.mark.asyncio
    async def test_update_missing_content(self):
        tool = ManageMemoryTool(_make_memory())
        result = await tool.execute(action="update", memory_id="id-001")
        assert result.is_error
        assert "content" in result.content

    # ── associate ────────────────────────────────────────────────────────────
    @pytest.mark.asyncio
    async def test_associate_success(self):
        memory = _make_memory()
        tool = ManageMemoryTool(memory)
        result = await tool.execute(action="associate", memory_id="id-001", tags=["bug"])
        assert not result.is_error
        assert "id-001" in result.content
        memory.associate_tags.assert_called_once_with("id-001", ["bug"])

    @pytest.mark.asyncio
    async def test_associate_missing_memory_id(self):
        tool = ManageMemoryTool(_make_memory())
        result = await tool.execute(action="associate", tags=["bug"])
        assert result.is_error
        assert "memory_id" in result.content

    @pytest.mark.asyncio
    async def test_associate_missing_tags(self):
        tool = ManageMemoryTool(_make_memory())
        result = await tool.execute(action="associate", memory_id="id-001")
        assert result.is_error
        assert "tags" in result.content

    # ── delete ─────────────────────────────────────────────────────────────
    @pytest.mark.asyncio
    async def test_delete_success(self):
        memory = _make_memory()
        tool = ManageMemoryTool(memory)
        result = await tool.execute(action="delete", memory_id="id-002")
        assert not result.is_error
        assert "id-002" in result.content
        memory.delete.assert_called_once_with("id-002")

    @pytest.mark.asyncio
    async def test_delete_missing_memory_id(self):
        tool = ManageMemoryTool(_make_memory())
        result = await tool.execute(action="delete")
        assert result.is_error
        assert "memory_id" in result.content

    # ── error handling ──────────────────────────────────────────────────────
    @pytest.mark.asyncio
    async def test_unknown_action(self):
        tool = ManageMemoryTool(_make_memory())
        result = await tool.execute(action="upsert")
        assert result.is_error
        assert "upsert" in result.content

    @pytest.mark.asyncio
    async def test_exception_returned_and_logged_with_traceback(self):
        memory = _make_memory()
        memory.write = AsyncMock(side_effect=RuntimeError("storage full"))
        tool = ManageMemoryTool(memory)

        records = []
        sink_id = logger.add(lambda message: records.append(message.record.copy()), level="ERROR")
        try:
            result = await tool.execute(
                action="write",
                content="private memory content",
                category="general",
            )
        finally:
            logger.remove(sink_id)

        assert result.is_error
        assert "storage full" in result.content
        failure = next(
            record for record in records if record["message"].startswith("manage_memory failed")
        )
        assert failure["level"].name == "ERROR"
        assert "action='write'" in failure["message"]
        assert "category='general'" in failure["message"]
        assert "private memory content" not in failure["message"]
        assert failure["exception"] is not None
        assert str(failure["exception"].value) == "storage full"
