from __future__ import annotations

import asyncio
import json
import re
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock

import pytest

from coworker.core.types import (
    AgentState,
    CommunicateRequest,
    Message,
    ToolCall,
    ToolResult,
)
from coworker.memory.short_term import ShortTermMemory
from coworker.tools.base import Tool, ToolDefinition
from coworker.tools.communicate_tool import CommunicateTool
from coworker.tools.file_tools import (
    FindFilesTool,
    GrepFilesTool,
    ListDirectoryTool,
    ReadFileTool,
    WriteFileTool,
)
from coworker.tools.reasoning_tools import (
    Task,
    TaskCreateTool,
    TaskGetTool,
    TaskListTool,
    TaskStore,
    TaskUpdateTool,
    format_task_times,
)
from coworker.tools.registry import ToolRegistry
from coworker.tools.system_tools import ClearShortTermMemoryTool, GetContextTool, SleepTool
from coworker.tools.vision_tools import VisualAnalysisTool
from coworker.tools.web_tools import FetchURLTool, SearchWebTool


class EchoTool(Tool):
    @property
    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="echo",
            description="echo input",
            parameters={
                "type": "object",
                "properties": {"text": {"type": "string"}},
                "required": ["text"],
            },
        )

    async def execute(self, text: str, **_) -> ToolResult:
        return ToolResult(tool_call_id="", content=text)


class BrokenTool(Tool):
    @property
    def definition(self) -> ToolDefinition:
        return ToolDefinition(name="broken", description="always fails", parameters={})

    async def execute(self, **_) -> ToolResult:
        raise RuntimeError("intentional failure")


class TextModelOnlyTool(Tool):
    text_model_only = True

    @property
    def definition(self) -> ToolDefinition:
        return ToolDefinition(name="vision_only", description="", parameters={})

    async def execute(self, **_) -> ToolResult:
        return ToolResult(tool_call_id="", content="")


class TestToolRegistry:
    def test_register_and_list(self):
        registry = ToolRegistry()
        registry.register(EchoTool())
        assert "echo" in registry.list_names()

    def test_get_schemas(self):
        registry = ToolRegistry()
        registry.register(EchoTool())
        schemas = registry.get_schemas()
        assert len(schemas) == 1
        assert schemas[0]["name"] == "echo"
        assert "parameters" in schemas[0]

    def test_get_schemas_hides_text_model_only_from_vision_model(self):
        registry = ToolRegistry()
        registry.register(EchoTool())
        registry.register(TextModelOnlyTool())
        schemas = registry.get_schemas(model_has_vision=True)
        names = [s["name"] for s in schemas]
        assert "echo" in names
        assert "vision_only" not in names

    def test_get_schemas_shows_text_model_only_for_text_model(self):
        registry = ToolRegistry()
        registry.register(EchoTool())
        registry.register(TextModelOnlyTool())
        schemas = registry.get_schemas(model_has_vision=False)
        names = [s["name"] for s in schemas]
        assert "echo" in names
        assert "vision_only" in names

    def test_get_schemas_default_hides_text_model_only(self):
        registry = ToolRegistry()
        registry.register(TextModelOnlyTool())
        assert registry.get_schemas() == []

    @pytest.mark.asyncio
    async def test_execute_success(self):
        registry = ToolRegistry()
        registry.register(EchoTool())
        call = ToolCall(id="1", name="echo", arguments={"text": "hello"})
        result = await registry.execute(call)
        assert result.content == "hello"
        assert not result.is_error

    @pytest.mark.asyncio
    async def test_execute_unknown_tool(self):
        registry = ToolRegistry()
        call = ToolCall(id="1", name="nonexistent", arguments={})
        result = await registry.execute(call)
        assert result.is_error
        assert "Unknown tool" in result.content

    @pytest.mark.asyncio
    async def test_execute_tool_exception(self):
        registry = ToolRegistry()
        registry.register(BrokenTool())
        call = ToolCall(id="1", name="broken", arguments={})
        result = await registry.execute(call)
        assert result.is_error
        assert "intentional failure" in result.content


class TestToolRegistryIntercepts:
    def test_intercept_does_not_affect_schemas(self):
        registry = ToolRegistry()
        registry.register(EchoTool())
        intercepted = registry.intercept({"echo": "blocked here"})
        # intercepts act only at execute time — tool stays visible in schemas
        assert [s["name"] for s in intercepted.get_schemas()] == ["echo"]
        assert [s["name"] for s in registry.get_schemas()] == ["echo"]

    @pytest.mark.asyncio
    async def test_intercept_blocks_execute_with_reason(self):
        registry = ToolRegistry()
        registry.register(EchoTool())
        intercepted = registry.intercept({"echo": "潜意识不可用"})
        result = await intercepted.execute(ToolCall(id="1", name="echo", arguments={"text": "hi"}))
        assert result.is_error
        assert result.content == "潜意识不可用"

    @pytest.mark.asyncio
    async def test_non_intercepted_tool_still_executes(self):
        registry = ToolRegistry()
        registry.register(EchoTool())
        intercepted = registry.intercept({"other": "blocked"})
        result = await intercepted.execute(ToolCall(id="1", name="echo", arguments={"text": "hi"}))
        assert not result.is_error
        assert result.content == "hi"

    def test_intercept_merges_existing(self):
        registry = ToolRegistry(intercepts={"a": "ra"})
        merged = registry.intercept({"b": "rb"})
        assert merged._intercepts == {"a": "ra", "b": "rb"}

    @pytest.mark.asyncio
    async def test_scoped_carries_intercepts(self):
        registry = ToolRegistry()
        registry.register(EchoTool())
        intercepted = registry.intercept({"echo": "blocked"})
        scoped = intercepted.scoped(scope=None)  # EchoTool is stateless; fork() returns self
        # schema unaffected; interception is enforced at execute time
        assert [s["name"] for s in scoped.get_schemas()] == ["echo"]
        result = await scoped.execute(ToolCall(id="1", name="echo", arguments={"text": "hi"}))
        assert result.is_error
        assert result.content == "blocked"


class TestReadFileTool:
    @pytest.mark.asyncio
    async def test_read_existing_file(self, tmp_path):
        f = tmp_path / "test.txt"
        f.write_text("hello world", encoding="utf-8")
        tool = ReadFileTool()
        result = await tool.execute(path=str(f))
        assert result.content == "hello world"
        assert not result.is_error

    @pytest.mark.asyncio
    async def test_read_missing_file(self, tmp_path):
        tool = ReadFileTool()
        result = await tool.execute(path=str(tmp_path / "missing.txt"))
        assert result.is_error

    @pytest.mark.asyncio
    async def test_read_with_offset(self, tmp_path):
        f = tmp_path / "long.txt"
        f.write_text("\n".join(f"line{i}" for i in range(1, 11)) + "\n", encoding="utf-8")
        tool = ReadFileTool()
        result = await tool.execute(path=str(f), offset=4)
        assert not result.is_error
        assert result.content == "line4\nline5\nline6\nline7\nline8\nline9\nline10\n"

    @pytest.mark.asyncio
    async def test_read_with_limit(self, tmp_path):
        f = tmp_path / "long.txt"
        f.write_text("\n".join(f"line{i}" for i in range(1, 11)) + "\n", encoding="utf-8")
        tool = ReadFileTool()
        result = await tool.execute(path=str(f), limit=3)
        assert not result.is_error
        assert result.content == "line1\nline2\nline3\n"

    @pytest.mark.asyncio
    async def test_read_with_offset_and_limit(self, tmp_path):
        f = tmp_path / "long.txt"
        f.write_text("\n".join(f"line{i}" for i in range(1, 11)) + "\n", encoding="utf-8")
        tool = ReadFileTool()
        result = await tool.execute(path=str(f), offset=3, limit=2)
        assert not result.is_error
        assert result.content == "line3\nline4\n"

    @pytest.mark.asyncio
    async def test_read_offset_beyond_eof(self, tmp_path):
        f = tmp_path / "short.txt"
        f.write_text("a\nb\n", encoding="utf-8")
        tool = ReadFileTool()
        result = await tool.execute(path=str(f), offset=100)
        assert not result.is_error
        assert result.content == ""


class TestWriteFileTool:
    @pytest.mark.asyncio
    async def test_write_creates_file(self, tmp_path):
        target = tmp_path / "out.txt"
        tool = WriteFileTool()
        result = await tool.execute(path=str(target), content="written")
        assert not result.is_error
        assert target.read_text(encoding="utf-8") == "written"

    @pytest.mark.asyncio
    async def test_write_creates_parent_dirs(self, tmp_path):
        target = tmp_path / "a" / "b" / "out.txt"
        tool = WriteFileTool()
        await tool.execute(path=str(target), content="deep")
        assert target.exists()

    @pytest.mark.asyncio
    async def test_write_append(self, tmp_path):
        target = tmp_path / "append.txt"
        tool = WriteFileTool()
        await tool.execute(path=str(target), content="first")
        await tool.execute(path=str(target), content="second", append=True)
        assert target.read_text(encoding="utf-8") == "firstsecond"

    @pytest.mark.asyncio
    async def test_write_overwrites_by_default(self, tmp_path):
        target = tmp_path / "overwrite.txt"
        target.write_text("old", encoding="utf-8")
        tool = WriteFileTool()
        await tool.execute(path=str(target), content="new")
        assert target.read_text(encoding="utf-8") == "new"


class TestListDirectoryTool:
    @pytest.mark.asyncio
    async def test_lists_files_and_dirs(self, tmp_path):
        (tmp_path / "subdir").mkdir()
        (tmp_path / "a.txt").write_text("x", encoding="utf-8")
        (tmp_path / "b.py").write_text("y", encoding="utf-8")
        tool = ListDirectoryTool()
        result = await tool.execute(path=str(tmp_path))
        assert not result.is_error
        assert "subdir/" in result.content
        assert "a.txt" in result.content
        assert "b.py" in result.content

    @pytest.mark.asyncio
    async def test_hides_dotfiles_by_default(self, tmp_path):
        (tmp_path / ".hidden").write_text("h", encoding="utf-8")
        (tmp_path / "visible.txt").write_text("v", encoding="utf-8")
        tool = ListDirectoryTool()
        result = await tool.execute(path=str(tmp_path))
        assert ".hidden" not in result.content
        assert "visible.txt" in result.content

    @pytest.mark.asyncio
    async def test_shows_dotfiles_when_requested(self, tmp_path):
        (tmp_path / ".hidden").write_text("h", encoding="utf-8")
        tool = ListDirectoryTool()
        result = await tool.execute(path=str(tmp_path), show_hidden=True)
        assert ".hidden" in result.content

    @pytest.mark.asyncio
    async def test_error_on_missing_path(self, tmp_path):
        tool = ListDirectoryTool()
        result = await tool.execute(path=str(tmp_path / "nonexistent"))
        assert result.is_error

    @pytest.mark.asyncio
    async def test_error_on_file_path(self, tmp_path):
        f = tmp_path / "file.txt"
        f.write_text("x", encoding="utf-8")
        tool = ListDirectoryTool()
        result = await tool.execute(path=str(f))
        assert result.is_error

    @pytest.mark.asyncio
    async def test_default_path_is_cwd(self):
        tool = ListDirectoryTool()
        result = await tool.execute()
        assert not result.is_error


class TestFindFilesTool:
    @pytest.mark.asyncio
    async def test_finds_by_extension(self, tmp_path):
        (tmp_path / "a.py").write_text("", encoding="utf-8")
        (tmp_path / "b.py").write_text("", encoding="utf-8")
        (tmp_path / "c.txt").write_text("", encoding="utf-8")
        tool = FindFilesTool()
        result = await tool.execute(pattern="*.py", root=str(tmp_path))
        assert not result.is_error
        assert "a.py" in result.content
        assert "b.py" in result.content
        assert "c.txt" not in result.content

    @pytest.mark.asyncio
    async def test_finds_recursively(self, tmp_path):
        sub = tmp_path / "deep" / "nested"
        sub.mkdir(parents=True)
        (sub / "target.json").write_text("{}", encoding="utf-8")
        tool = FindFilesTool()
        result = await tool.execute(pattern="*.json", root=str(tmp_path))
        assert not result.is_error
        assert "target.json" in result.content

    @pytest.mark.asyncio
    async def test_no_match_returns_message(self, tmp_path):
        tool = FindFilesTool()
        result = await tool.execute(pattern="*.xyz", root=str(tmp_path))
        assert not result.is_error
        assert "未找到" in result.content

    @pytest.mark.asyncio
    async def test_max_results_respected(self, tmp_path):
        for i in range(10):
            (tmp_path / f"f{i}.txt").write_text("", encoding="utf-8")
        tool = FindFilesTool()
        result = await tool.execute(pattern="*.txt", root=str(tmp_path), max_results=3)
        assert not result.is_error
        assert "已达结果上限" in result.content

    @pytest.mark.asyncio
    async def test_error_on_missing_root(self, tmp_path):
        tool = FindFilesTool()
        result = await tool.execute(pattern="*.py", root=str(tmp_path / "nonexistent"))
        assert result.is_error

    @pytest.mark.asyncio
    async def test_max_scan_truncates(self, tmp_path, monkeypatch):
        import coworker.tools.file_tools as ft

        monkeypatch.setattr(ft, "_FIND_MAX_SCAN", 2)
        for i in range(5):
            (tmp_path / f"f{i}.txt").write_text("", encoding="utf-8")
        tool = FindFilesTool()
        result = await tool.execute(pattern="*.txt", root=str(tmp_path))
        assert not result.is_error
        assert "已扫描" in result.content


class TestGrepFilesTool:
    @pytest.mark.asyncio
    async def test_finds_match_in_file(self, tmp_path):
        f = tmp_path / "a.txt"
        f.write_text("hello world\nfoo bar\n", encoding="utf-8")
        tool = GrepFilesTool()
        result = await tool.execute(pattern="hello", path=str(f))
        assert not result.is_error
        assert "hello world" in result.content
        assert "foo bar" not in result.content

    @pytest.mark.asyncio
    async def test_shows_line_numbers(self, tmp_path):
        f = tmp_path / "a.txt"
        f.write_text("line1\nline2\nline3\n", encoding="utf-8")
        tool = GrepFilesTool()
        result = await tool.execute(pattern="line2", path=str(f))
        assert ":2>" in result.content

    @pytest.mark.asyncio
    async def test_searches_directory_recursively(self, tmp_path):
        (tmp_path / "x.py").write_text("import os\n", encoding="utf-8")
        sub = tmp_path / "sub"
        sub.mkdir()
        (sub / "y.py").write_text("import sys\n", encoding="utf-8")
        tool = GrepFilesTool()
        result = await tool.execute(pattern="import", path=str(tmp_path))
        assert "x.py" in result.content
        assert "y.py" in result.content

    @pytest.mark.asyncio
    async def test_file_pattern_filter(self, tmp_path):
        (tmp_path / "a.py").write_text("needle\n", encoding="utf-8")
        (tmp_path / "b.txt").write_text("needle\n", encoding="utf-8")
        tool = GrepFilesTool()
        result = await tool.execute(pattern="needle", path=str(tmp_path), file_pattern="*.py")
        assert "a.py" in result.content
        assert "b.txt" not in result.content

    @pytest.mark.asyncio
    async def test_ignore_case(self, tmp_path):
        f = tmp_path / "a.txt"
        f.write_text("Hello World\n", encoding="utf-8")
        tool = GrepFilesTool()
        result = await tool.execute(pattern="hello", path=str(f), ignore_case=True)
        assert "Hello World" in result.content

    @pytest.mark.asyncio
    async def test_context_lines(self, tmp_path):
        f = tmp_path / "a.txt"
        f.write_text("before\nmatch\nafter\n", encoding="utf-8")
        tool = GrepFilesTool()
        result = await tool.execute(pattern="match", path=str(f), context_lines=1)
        assert "before" in result.content
        assert "after" in result.content

    @pytest.mark.asyncio
    async def test_no_match_returns_message(self, tmp_path):
        f = tmp_path / "a.txt"
        f.write_text("nothing here\n", encoding="utf-8")
        tool = GrepFilesTool()
        result = await tool.execute(pattern="xyz123", path=str(f))
        assert not result.is_error
        assert "未找到" in result.content

    @pytest.mark.asyncio
    async def test_max_matches_respected(self, tmp_path):
        f = tmp_path / "a.txt"
        f.write_text("\n".join(["hit"] * 20), encoding="utf-8")
        tool = GrepFilesTool()
        result = await tool.execute(pattern="hit", path=str(f), max_matches=5)
        assert "已达上限" in result.content

    @pytest.mark.asyncio
    async def test_invalid_regex_returns_error(self, tmp_path):
        f = tmp_path / "a.txt"
        f.write_text("x\n", encoding="utf-8")
        tool = GrepFilesTool()
        result = await tool.execute(pattern="[invalid", path=str(f))
        assert result.is_error
        assert "正则表达式错误" in result.content

    @pytest.mark.asyncio
    async def test_error_on_missing_path(self, tmp_path):
        tool = GrepFilesTool()
        result = await tool.execute(pattern="x", path=str(tmp_path / "nonexistent"))
        assert result.is_error


class TestGetContextTool:
    def _make_tool(self) -> tuple[GetContextTool, AgentState]:
        brain = MagicMock()
        brain.current_provider_name = "anthropic"
        brain.current_model = "claude-sonnet-4-6"
        short_term = ShortTermMemory()
        state = AgentState(cycle_count=5)
        return GetContextTool(brain, short_term, state), state

    @pytest.mark.asyncio
    async def test_returns_current_time(self):
        tool, _ = self._make_tool()
        result = await tool.execute()
        assert not result.is_error
        assert "当前时间" in result.content
        assert re.search(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}", result.content)

    @pytest.mark.asyncio
    async def test_returns_cycle_count(self):
        tool, state = self._make_tool()
        state.cycle_count = 42
        result = await tool.execute()
        assert "42" in result.content

    @pytest.mark.asyncio
    async def test_returns_model_info(self):
        tool, _ = self._make_tool()
        result = await tool.execute()
        assert "anthropic/claude-sonnet-4-6" in result.content

    @pytest.mark.asyncio
    async def test_returns_primary_message_count(self):
        brain = MagicMock()
        brain.current_provider_name = "anthropic"
        brain.current_model = "claude-sonnet-4-6"
        short_term = ShortTermMemory()
        short_term.primary.append(Message(role="user", content="hi"))
        short_term.primary.append(Message(role="assistant", content="hello"))
        state = AgentState()
        tool = GetContextTool(brain, short_term, state)
        result = await tool.execute()
        assert "短期记忆：2" in result.content

    @pytest.mark.asyncio
    async def test_message_count_zero_when_empty(self):
        tool, _ = self._make_tool()
        result = await tool.execute()
        assert "短期记忆：0" in result.content

    @pytest.mark.asyncio
    async def test_cycle_count_reflects_live_state(self):
        tool, state = self._make_tool()
        state.cycle_count = 99
        result = await tool.execute()
        assert "99" in result.content


class TestTaskStore:
    def test_create_and_get(self, tmp_path):
        store = TaskStore(tmp_path / "tasks.json")
        task = store.create("修复登录 Bug")
        assert len(task.id) == 8
        assert task.status == "pending"
        assert task.details == ""
        assert task.created_at
        assert task.updated_at == task.created_at
        fetched = store.get(task.id)
        assert fetched is not None and fetched.description == "修复登录 Bug"

    def test_create_persists_to_file(self, tmp_path):
        import json as _json
        p = tmp_path / "tasks.json"
        store = TaskStore(p)
        store.create("任务A")
        assert p.exists()
        data = _json.loads(p.read_text(encoding="utf-8"))
        assert len(data["tasks"]) == 1
        assert data["tasks"][0]["details"] == ""
        assert data["tasks"][0]["created_at"]
        assert data["tasks"][0]["updated_at"] == data["tasks"][0]["created_at"]

    def test_load_restores_on_init(self, tmp_path):
        p = tmp_path / "tasks.json"
        s1 = TaskStore(p)
        t = s1.create("任务B")
        s2 = TaskStore(p)
        assert s2.get(t.id) is not None

    def test_update_status(self, tmp_path):
        store = TaskStore(tmp_path / "tasks.json")
        t = store.create("任务")
        updated = store.update(t.id, status="in_progress")
        assert updated is not None and updated.status == "in_progress"

    def test_update_refreshes_updated_at(self, tmp_path, monkeypatch):
        timestamps = iter(["2026-01-01T10:00:00+08:00", "2026-01-02T11:00:00+08:00"])
        monkeypatch.setattr("coworker.tools.reasoning_tools._now_iso", lambda: next(timestamps))
        store = TaskStore(tmp_path / "tasks.json")
        t = store.create("任务")

        updated = store.update(t.id, status="in_progress")

        assert updated is not None
        assert updated.created_at == "2026-01-01T10:00:00+08:00"
        assert updated.updated_at == "2026-01-02T11:00:00+08:00"

    def test_update_description(self, tmp_path):
        store = TaskStore(tmp_path / "tasks.json")
        t = store.create("旧描述")
        store.update(t.id, description="新描述")
        assert store.get(t.id).description == "新描述"  # type: ignore[union-attr]

    def test_create_with_details(self, tmp_path):
        store = TaskStore(tmp_path / "tasks.json")
        task = store.create("任务", details="## Goal\n完成它\n")
        assert task.details == "## Goal\n完成它\n"

    def test_load_legacy_task_without_details(self, tmp_path):
        p = tmp_path / "tasks.json"
        p.write_text(
            json.dumps(
                {"tasks": [{"id": "abc12345", "description": "旧任务", "status": "pending"}]},
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        store = TaskStore(p)
        task = store.get("abc12345")
        assert task is not None
        assert task.details == ""
        assert task.created_at
        assert task.updated_at == task.created_at

        data = json.loads(p.read_text(encoding="utf-8"))
        assert data["tasks"][0]["created_at"] == task.created_at
        assert data["tasks"][0]["updated_at"] == task.updated_at

    def test_update_details_replace_append_and_default(self, tmp_path):
        store = TaskStore(tmp_path / "tasks.json")
        task = store.create("任务")

        store.update(task.id, details="A")
        assert store.get(task.id).details == "A"  # type: ignore[union-attr]

        store.update(task.id, details="B", details_update_mode="append")
        assert store.get(task.id).details == "A\nB"  # type: ignore[union-attr]

        store.update(task.id, details="C", details_update_mode="replace")
        assert store.get(task.id).details == "C"  # type: ignore[union-attr]

    def test_update_details_patch(self, tmp_path):
        store = TaskStore(tmp_path / "tasks.json")
        task = store.create("任务", details="## Goal\nold\n## Next\none\n")
        patch = """--- details
+++ details
@@ -1,4 +1,4 @@
 ## Goal
-old
+new
 ## Next
 one
"""

        store.update(task.id, details=patch, details_update_mode="patch")

        assert store.get(task.id).details == "## Goal\nnew\n## Next\none\n"  # type: ignore[union-attr]

    def test_update_details_patch_multiple_hunks(self, tmp_path):
        store = TaskStore(tmp_path / "tasks.json")
        task = store.create("任务", details="a\nb\nc\nd\n")
        patch = """@@ -1,2 +1,2 @@
 a
-b
+B
@@ -4,1 +4,2 @@
 d
+e
"""

        store.update(task.id, details=patch, details_update_mode="patch")

        assert store.get(task.id).details == "a\nB\nc\nd\ne\n"  # type: ignore[union-attr]

    def test_update_details_patch_empty_document(self, tmp_path):
        store = TaskStore(tmp_path / "tasks.json")
        task = store.create("任务")
        patch = """@@ -0,0 +1,2 @@
+first
+second
"""

        store.update(task.id, details=patch, details_update_mode="patch")

        assert store.get(task.id).details == "first\nsecond\n"  # type: ignore[union-attr]

    def test_update_details_patch_failure_is_atomic(self, tmp_path):
        store = TaskStore(tmp_path / "tasks.json")
        task = store.create("任务", details="a\nb\n")
        patch = """@@ -1,2 +1,2 @@
 a
-missing
+new
"""

        with pytest.raises(ValueError):
            store.update(task.id, details=patch, details_update_mode="patch")

        assert store.get(task.id).details == "a\nb\n"  # type: ignore[union-attr]

    def test_update_details_patch_failure_does_not_mutate_other_fields(self, tmp_path):
        store = TaskStore(tmp_path / "tasks.json")
        task = store.create("旧描述", details="a\nb\n")
        patch = """@@ -1,2 +1,2 @@
 a
-missing
+new
"""

        with pytest.raises(ValueError):
            store.update(
                task.id,
                status="in_progress",
                description="新描述",
                details=patch,
                details_update_mode="patch",
            )

        current = store.get(task.id)
        assert current is not None
        assert current.status == "pending"
        assert current.description == "旧描述"
        assert current.details == "a\nb\n"

    def test_update_details_patch_rejects_multi_file(self, tmp_path):
        store = TaskStore(tmp_path / "tasks.json")
        task = store.create("任务", details="a\n")
        patch = """--- details
+++ details
@@ -1,1 +1,1 @@
-a
+b
--- other
+++ other
@@ -1,1 +1,1 @@
-x
+y
"""

        with pytest.raises(ValueError):
            store.update(task.id, details=patch, details_update_mode="patch")

        assert store.get(task.id).details == "a\n"  # type: ignore[union-attr]

    def test_delete_removes_task(self, tmp_path):
        store = TaskStore(tmp_path / "tasks.json")
        t1 = store.create("A")
        t2 = store.create("B")
        store.update(t1.id, status="deleted")
        tasks = store.list()
        assert len(tasks) == 1
        assert tasks[0].id == t2.id
        assert store.get(t1.id) is None

    def test_delete_persists(self, tmp_path):
        p = tmp_path / "tasks.json"
        s1 = TaskStore(p)
        t = s1.create("A")
        s1.update(t.id, status="deleted")
        s2 = TaskStore(p)
        assert s2.get(t.id) is None

    def test_update_nonexistent_returns_none(self, tmp_path):
        store = TaskStore(tmp_path / "tasks.json")
        assert store.update("nonexistent", status="completed") is None

    def test_corrupt_file_does_not_crash(self, tmp_path):
        p = tmp_path / "tasks.json"
        p.write_text("{ invalid json", encoding="utf-8")
        store = TaskStore(p)
        assert store.list() == []

    def test_create_nested_directories(self, tmp_path):
        store = TaskStore(tmp_path / "nested" / "deep" / "tasks.json")
        store.create("A")
        assert (tmp_path / "nested" / "deep" / "tasks.json").exists()

    def test_format_task_times_shows_relative_and_absolute_dates(self):
        task = Task(
            id="abc12345",
            description="任务",
            created_at="2026-05-10T09:00:00+08:00",
            updated_at="2026-07-07T08:30:00+08:00",
        )
        now = datetime.fromisoformat("2026-07-07T10:30:00+08:00")

        assert format_task_times(task, now=now) == (
            "创建于 1 个月前（2026-05-10） / 修改于 2 小时前"
        )

    def test_format_task_times_hides_absolute_date_for_nearby_days(self):
        task = Task(
            id="abc12345",
            description="任务",
            created_at="2026-06-08T09:00:00+08:00",
            updated_at="2026-06-07T09:00:00+08:00",
        )
        now = datetime.fromisoformat("2026-07-07T10:30:00+08:00")

        assert format_task_times(task, now=now) == (
            "创建于 29 天前 / 修改于 1 个月前（2026-06-07）"
        )


class TestTaskCreateTool:
    @pytest.mark.asyncio
    async def test_creates_task_returns_id(self, tmp_path):
        store = TaskStore(tmp_path / "tasks.json")
        result = await TaskCreateTool(store).execute(description="修复 Bug")
        assert not result.is_error
        assert "pending" in result.content
        assert len(store.list()) == 1

    @pytest.mark.asyncio
    async def test_creates_task_with_details(self, tmp_path):
        store = TaskStore(tmp_path / "tasks.json")
        result = await TaskCreateTool(store).execute(description="修复 Bug", details="细节")
        assert not result.is_error
        assert "has_details=true" in result.content
        assert store.list()[0].details == "细节"

    def test_definition_name(self, tmp_path):
        assert TaskCreateTool(TaskStore(tmp_path / "t.json")).definition.name == "task_create"

    def test_description_required(self, tmp_path):
        schema = TaskCreateTool(TaskStore(tmp_path / "t.json")).definition.parameters
        assert "description" in schema["required"]
        assert "details" in schema["properties"]


class TestTaskGetTool:
    @pytest.mark.asyncio
    async def test_get_existing_task(self, tmp_path):
        store = TaskStore(tmp_path / "tasks.json")
        task = store.create("修复登录 Bug")
        result = await TaskGetTool(store).execute(task_id=task.id)
        assert not result.is_error
        assert "修复登录 Bug" in result.content
        assert task.id in result.content
        assert "details:" in result.content
        assert "time:" in result.content
        assert "created_at:" in result.content
        assert "updated_at:" in result.content

    @pytest.mark.asyncio
    async def test_get_existing_task_shows_details(self, tmp_path):
        store = TaskStore(tmp_path / "tasks.json")
        task = store.create("修复登录 Bug", details="## Next\n继续")
        result = await TaskGetTool(store).execute(task_id=task.id)
        assert not result.is_error
        assert "has_details: true" in result.content
        assert "## Next" in result.content

    @pytest.mark.asyncio
    async def test_get_nonexistent_returns_error(self, tmp_path):
        store = TaskStore(tmp_path / "tasks.json")
        result = await TaskGetTool(store).execute(task_id="badid")
        assert result.is_error


class TestTaskListTool:
    @pytest.mark.asyncio
    async def test_empty_returns_placeholder(self, tmp_path):
        store = TaskStore(tmp_path / "tasks.json")
        result = await TaskListTool(store).execute()
        assert not result.is_error
        assert "任务列表为空" in result.content

    @pytest.mark.asyncio
    async def test_deleted_task_not_in_list(self, tmp_path):
        store = TaskStore(tmp_path / "tasks.json")
        t1 = store.create("A")
        t2 = store.create("B")
        store.update(t1.id, status="deleted")
        result = await TaskListTool(store).execute()
        assert t2.id in result.content
        assert t1.id not in result.content

    @pytest.mark.asyncio
    async def test_shows_task_description(self, tmp_path):
        store = TaskStore(tmp_path / "tasks.json")
        store.create("实现登录功能")
        result = await TaskListTool(store).execute()
        assert "实现登录功能" in result.content
        assert "创建于" in result.content
        assert "修改于" in result.content

    @pytest.mark.asyncio
    async def test_marks_details_without_showing_them(self, tmp_path):
        store = TaskStore(tmp_path / "tasks.json")
        store.create("实现登录功能", details="SECRET DETAILS")
        result = await TaskListTool(store).execute()
        assert "has_details=true" in result.content
        assert "SECRET DETAILS" not in result.content


class TestTaskUpdateTool:
    @pytest.mark.asyncio
    async def test_update_status(self, tmp_path):
        store = TaskStore(tmp_path / "tasks.json")
        t = store.create("A")
        result = await TaskUpdateTool(store).execute(task_id=t.id, status="in_progress")
        assert not result.is_error
        assert store.get(t.id).status == "in_progress"  # type: ignore[union-attr]

    @pytest.mark.asyncio
    async def test_update_description(self, tmp_path):
        store = TaskStore(tmp_path / "tasks.json")
        t = store.create("旧描述")
        result = await TaskUpdateTool(store).execute(task_id=t.id, description="新描述")
        assert not result.is_error
        assert store.get(t.id).description == "新描述"  # type: ignore[union-attr]

    @pytest.mark.asyncio
    async def test_update_details_default_replace(self, tmp_path):
        store = TaskStore(tmp_path / "tasks.json")
        t = store.create("任务")
        result = await TaskUpdateTool(store).execute(task_id=t.id, details="正文")
        assert not result.is_error
        assert "has_details=true" in result.content
        assert store.get(t.id).details == "正文"  # type: ignore[union-attr]

    @pytest.mark.asyncio
    async def test_update_details_append(self, tmp_path):
        store = TaskStore(tmp_path / "tasks.json")
        t = store.create("任务", details="A")
        result = await TaskUpdateTool(store).execute(
            task_id=t.id,
            details="B",
            details_update_mode="append",
        )
        assert not result.is_error
        assert store.get(t.id).details == "A\nB"  # type: ignore[union-attr]

    @pytest.mark.asyncio
    async def test_update_details_patch(self, tmp_path):
        store = TaskStore(tmp_path / "tasks.json")
        t = store.create("任务", details="a\nb\n")
        patch = """@@ -1,2 +1,2 @@
 a
-b
+B
"""
        result = await TaskUpdateTool(store).execute(
            task_id=t.id,
            details=patch,
            details_update_mode="patch",
        )
        assert not result.is_error
        assert store.get(t.id).details == "a\nB\n"  # type: ignore[union-attr]

    @pytest.mark.asyncio
    async def test_update_details_patch_error_keeps_original(self, tmp_path):
        store = TaskStore(tmp_path / "tasks.json")
        t = store.create("任务", details="a\nb\n")
        patch = """@@ -1,2 +1,2 @@
 a
-missing
+B
"""
        result = await TaskUpdateTool(store).execute(
            task_id=t.id,
            details=patch,
            details_update_mode="patch",
        )
        assert result.is_error
        assert store.get(t.id).details == "a\nb\n"  # type: ignore[union-attr]

    @pytest.mark.asyncio
    async def test_update_nonexistent_returns_error(self, tmp_path):
        store = TaskStore(tmp_path / "tasks.json")
        result = await TaskUpdateTool(store).execute(task_id="bad", status="completed")
        assert result.is_error



class TestSleepTool:
    @pytest.mark.asyncio
    async def test_sleeps_full_duration_without_inbox(self, monkeypatch):
        """fork 后无 inbox：睡满指定秒数"""
        slept: list[float] = []

        async def fake_sleep(delay):
            slept.append(delay)

        monkeypatch.setattr("coworker.tools.system_tools.asyncio.sleep", fake_sleep)
        tool = SleepTool(None)
        result = await tool.execute(seconds=5)
        assert not result.is_error
        assert "Slept for 5s" in result.content
        assert slept == [5]

    @pytest.mark.asyncio
    async def test_zero_seconds_rejected_when_not_passive(self):
        """非 passive 模式下 sleep(0) 不进入无限等待，返回引导提示"""
        inbox = MagicMock()
        inbox.message_event = asyncio.Event()
        config = MagicMock()
        config.agent.passive_mode = False
        tool = SleepTool(inbox, config=config)
        result = await tool.execute(seconds=0)
        assert not result.is_error
        assert "sleep(N)" in result.content
        assert "不会进入等待" in result.content

    def test_definition_passive_advertises_zero(self):
        """passive 模式下工具介绍说明 sleep(0) 可无限等待"""
        config = MagicMock()
        config.agent.passive_mode = True
        tool = SleepTool(None, config=config)
        assert "传 0" in tool.definition.description

    def test_definition_active_omits_zero(self):
        """active 模式下工具介绍不提 sleep(0)"""
        config = MagicMock()
        config.agent.passive_mode = False
        tool = SleepTool(None, config=config)
        assert "传 0" not in tool.definition.description

    @pytest.mark.asyncio
    async def test_wakes_early_when_message_arrives(self):
        event = asyncio.Event()
        inbox = MagicMock()
        inbox.message_event = event

        async def set_event_soon():
            await asyncio.sleep(0.05)
            event.set()

        asyncio.create_task(set_event_soon())
        tool = SleepTool(inbox)
        result = await asyncio.wait_for(tool.execute(seconds=60), timeout=5.0)
        assert not result.is_error
        assert "提前" in result.content

    @pytest.mark.asyncio
    async def test_returns_normal_message_on_timeout(self, monkeypatch):
        """有 inbox + seconds>0：超时返回正常消息"""
        event = asyncio.Event()
        inbox = MagicMock()
        inbox.message_event = event

        async def fake_wait_for(coro, timeout):
            coro.close()
            raise TimeoutError

        monkeypatch.setattr("coworker.tools.system_tools.asyncio.wait_for", fake_wait_for)
        tool = SleepTool(inbox)
        result = await tool.execute(seconds=10)
        assert not result.is_error
        assert "Slept for 10s" in result.content

    @pytest.mark.asyncio
    async def test_zero_seconds_sleeps_until_event_when_passive(self):
        """passive 模式下 sleep(0)：无超时，休眠直到外部信息唤醒"""
        event = asyncio.Event()
        inbox = MagicMock()
        inbox.message_event = event
        config = MagicMock()
        config.agent.passive_mode = True

        async def set_event_soon():
            await asyncio.sleep(0.05)
            event.set()

        asyncio.create_task(set_event_soon())
        tool = SleepTool(inbox, config=config)
        result = await asyncio.wait_for(tool.execute(seconds=0), timeout=5.0)
        assert not result.is_error
        assert "提前" in result.content

    @pytest.mark.asyncio
    async def test_zero_seconds_passive_does_not_return_without_event(self):
        """passive 模式下 sleep(0) 在无外部事件时不返回（验证无超时）"""
        event = asyncio.Event()
        inbox = MagicMock()
        inbox.message_event = event
        config = MagicMock()
        config.agent.passive_mode = True
        tool = SleepTool(inbox, config=config)
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(tool.execute(seconds=0), timeout=0.2)

    @pytest.mark.asyncio
    async def test_zero_seconds_passive_without_inbox_returns_immediately(self):
        """passive 模式但无 inbox（兜底）：立即返回"""
        config = MagicMock()
        config.agent.passive_mode = True
        tool = SleepTool(None, config=config)
        result = await tool.execute(seconds=0)
        assert not result.is_error
        assert "被动等待" in result.content


class TestSearchWebTool:
    @pytest.mark.asyncio
    async def test_retries_and_eventually_succeeds(self, monkeypatch):
        calls = {"count": 0}

        class FakeDDGS:
            def text(self, query, max_results, backend):
                calls["count"] += 1
                if calls["count"] < 3:
                    raise RuntimeError("temporary failure")
                return [{"title": "Result", "href": "https://example.com", "body": "summary"}]

        async def fake_sleep(delay):
            sleep_calls.append(delay)

        sleep_calls: list[float] = []

        monkeypatch.setattr("ddgs.DDGS", FakeDDGS)
        monkeypatch.setattr("coworker.tools.web_tools.asyncio.sleep", fake_sleep)

        tool = SearchWebTool()
        result = await tool.execute(query="test")

        assert not result.is_error
        assert "Result" in result.content
        assert calls["count"] == 3
        assert sleep_calls == [0.5, 1.0]

    @pytest.mark.asyncio
    async def test_returns_error_after_max_retries(self, monkeypatch):
        calls = {"count": 0}

        class FakeDDGS:
            def text(self, query, max_results, backend):
                calls["count"] += 1
                raise RuntimeError("permanent failure")

        async def fake_sleep(delay):
            sleep_calls.append(delay)

        sleep_calls: list[float] = []

        monkeypatch.setattr("ddgs.DDGS", FakeDDGS)
        monkeypatch.setattr("coworker.tools.web_tools.asyncio.sleep", fake_sleep)

        tool = SearchWebTool()
        result = await tool.execute(query="test")

        assert result.is_error
        assert "failed after 3 attempts" in result.content
        assert "permanent failure" in result.content
        assert calls["count"] == 3
        assert sleep_calls == [0.5, 1.0]


class TestFetchURLTool:
    @pytest.mark.asyncio
    async def test_retries_and_eventually_succeeds(self, monkeypatch):
        calls = {"count": 0}

        class FakeDDGS:
            def extract(self, url, fmt):
                calls["count"] += 1
                if calls["count"] < 3:
                    raise RuntimeError("temporary fetch failure")
                return {"content": "page content"}

        async def fake_sleep(delay):
            sleep_calls.append(delay)

        sleep_calls: list[float] = []

        monkeypatch.setattr("ddgs.DDGS", FakeDDGS)
        monkeypatch.setattr("coworker.tools.web_tools.asyncio.sleep", fake_sleep)

        tool = FetchURLTool()
        result = await tool.execute(url="https://example.com")

        assert not result.is_error
        assert result.content == "page content"
        assert calls["count"] == 3
        assert sleep_calls == [0.5, 1.0]

    @pytest.mark.asyncio
    async def test_returns_error_after_max_retries(self, monkeypatch):
        calls = {"count": 0}

        class FakeDDGS:
            def extract(self, url, fmt):
                calls["count"] += 1
                raise RuntimeError("permanent fetch failure")

        async def fake_sleep(delay):
            sleep_calls.append(delay)

        sleep_calls: list[float] = []

        monkeypatch.setattr("ddgs.DDGS", FakeDDGS)
        monkeypatch.setattr("coworker.tools.web_tools.asyncio.sleep", fake_sleep)

        tool = FetchURLTool()
        result = await tool.execute(url="https://example.com")

        assert result.is_error
        assert "failed after 3 attempts" in result.content
        assert "permanent fetch failure" in result.content
        assert calls["count"] == 3
        assert sleep_calls == [0.5, 1.0]

    @pytest.mark.asyncio
    async def test_returns_error_without_retry_when_no_content_extracted(self, monkeypatch):
        calls = {"count": 0}

        class FakeDDGS:
            def extract(self, url, fmt):
                calls["count"] += 1
                return {"content": ""}

        async def fake_sleep(delay):
            sleep_calls.append(delay)

        sleep_calls: list[float] = []

        monkeypatch.setattr("ddgs.DDGS", FakeDDGS)
        monkeypatch.setattr("coworker.tools.web_tools.asyncio.sleep", fake_sleep)

        tool = FetchURLTool()
        result = await tool.execute(url="https://example.com")

        assert result.is_error
        assert result.content == "No content extracted."
        assert calls["count"] == 1
        assert sleep_calls == []


class TestCommunicateToolCheckers:
    def test_structured_extra_capability_follows_selected_transport(self, tmp_path):
        tool = CommunicateTool(str(tmp_path / "outbox"))

        async def sender(request: CommunicateRequest):
            return ToolResult(tool_call_id="", content="ok")

        tool.register_sender("plain:", sender)
        tool.register_sender("rich:", sender, supports_extra=True)
        queue: asyncio.Queue = asyncio.Queue()
        tool.register_ws("stream-client", queue)

        assert not tool.supports_message_extra("plain:alice")
        assert tool.supports_message_extra("rich:alice")
        assert tool.supports_message_extra("stream-client")
        assert not tool.supports_message_extra("offline-client")

    def test_resolve_participant_id_expands_single_match_without_sending(self, tmp_path):
        tool = CommunicateTool(str(tmp_path / "outbox"))

        async def sender(request: CommunicateRequest):
            return ToolResult(tool_call_id="", content="ok")

        tool.register_sender(
            "chan:",
            sender,
            lambda pid: f"chan:single:{pid}" if pid == "alice" else None,
        )

        assert tool.resolve_participant_id("alice") == "chan:single:alice"
        assert tool.resolve_participant_id("chan:single:alice") == "chan:single:alice"
        assert tool.resolve_participant_id("unknown") == "unknown"

    @pytest.mark.asyncio
    async def test_prefix_match_bypasses_checker(self, tmp_path):
        tool = CommunicateTool(str(tmp_path / "outbox"))
        sent = []

        async def sender(request: CommunicateRequest):
            sent.append(request.participant_id)
            return ToolResult(tool_call_id="", content="ok")

        tool.register_sender("chan:", sender, lambda pid: f"chan:{pid}")
        result = await tool.execute(message="hi", participant_id="chan:alice")
        assert not result.is_error
        assert sent == ["chan:alice"]

    @pytest.mark.asyncio
    async def test_no_prefix_single_match_auto_routes(self, tmp_path):
        tool = CommunicateTool(str(tmp_path / "outbox"))
        sent = []

        async def sender(request: CommunicateRequest):
            sent.append(request.participant_id)
            return ToolResult(tool_call_id="", content="ok")

        tool.register_sender(
            "chan:",
            sender,
            lambda pid: f"chan:single:{pid}" if pid == "alice" else None,
        )
        result = await tool.execute(message="hi", participant_id="alice")
        assert not result.is_error
        assert sent == ["chan:single:alice"]

    @pytest.mark.asyncio
    async def test_no_prefix_multi_match_returns_error(self, tmp_path):
        tool = CommunicateTool(str(tmp_path / "outbox"))

        async def sender_a(request: CommunicateRequest):
            return ToolResult(tool_call_id="", content="ok")

        async def sender_b(request: CommunicateRequest):
            return ToolResult(tool_call_id="", content="ok")

        tool.register_sender("chan_a:", sender_a, lambda pid: f"chan_a:{pid}")
        tool.register_sender("chan_b:", sender_b, lambda pid: f"chan_b:{pid}")
        result = await tool.execute(message="hi", participant_id="alice")
        assert result.is_error
        assert "多个信道" in result.content
        assert "chan_a:alice" in result.content
        assert "chan_b:alice" in result.content

    @pytest.mark.asyncio
    async def test_request_sender_receives_extended_fields(self, tmp_path):
        tool = CommunicateTool(str(tmp_path / "outbox"))
        seen: list[CommunicateRequest] = []

        async def sender(request: CommunicateRequest):
            seen.append(request)
            return ToolResult(tool_call_id="", content="ok")

        tool.register_sender("rich:", sender)

        result = await tool.execute(
            participant_id="rich:alice",
            message="hi",
            conversation_id="conv_1",
            attachments=[{"path": "x.txt"}],
            extra={"mode": "plan"},
        )

        assert not result.is_error
        assert seen == [
            CommunicateRequest(
                participant_id="rich:alice",
                message="hi",
                conversation_id="conv_1",
                attachments=[{"path": "x.txt"}],
                extra={"mode": "plan"},
            )
        ]

    @pytest.mark.asyncio
    async def test_longest_registered_prefix_wins(self, tmp_path):
        tool = CommunicateTool(str(tmp_path / "outbox"))
        seen: list[str] = []

        async def generic_sender(request: CommunicateRequest):
            seen.append("generic")
            return ToolResult(tool_call_id="", content="generic")

        async def specific_sender(request: CommunicateRequest):
            seen.append("specific")
            return ToolResult(tool_call_id="", content="specific")

        tool.register_sender("rich:", generic_sender)
        tool.register_sender("rich:team:", specific_sender)

        result = await tool.execute(participant_id="rich:team:alice", message="hi")

        assert not result.is_error
        assert result.content == "specific"
        assert seen == ["specific"]

    @pytest.mark.asyncio
    async def test_no_prefix_no_match_falls_back_to_outbox(self, tmp_path):
        outbox = tmp_path / "outbox"
        tool = CommunicateTool(str(outbox))

        async def sender(request: CommunicateRequest):
            return ToolResult(tool_call_id="", content="ok")

        tool.register_sender("chan:", sender, lambda pid: None)
        result = await tool.execute(message="hello", participant_id="unknown_user")
        assert not result.is_error
        files = list(outbox.glob("*unknown_user*"))
        assert files

    @pytest.mark.asyncio
    async def test_connected_ws_target_receives_structured_payload(self, tmp_path):
        file_path = tmp_path / "note.txt"
        file_path.write_text("hello", encoding="utf-8")
        tool = CommunicateTool(str(tmp_path / "outbox"))
        queue: asyncio.Queue = asyncio.Queue()
        assert tool.register_ws("alice", queue) is True

        result = await tool.execute(
            participant_id="alice",
            conversation_id="thr_1",
            message="hi",
            attachments=[{"path": str(file_path)}],
            extra={"mode": "plan"},
        )

        assert not result.is_error
        request = await asyncio.wait_for(queue.get(), timeout=1)
        assert request == CommunicateRequest(
            participant_id="alice",
            message="hi",
            conversation_id="thr_1",
            attachments=[{"path": str(file_path)}],
            extra={"mode": "plan"},
        )

    @pytest.mark.asyncio
    async def test_desktop_sender_delivers_online_without_retry_queue(self, tmp_path):
        from coworker.channels.desktop import DESKTOP_PREFIX, DesktopCommunicateSender

        tool = CommunicateTool(str(tmp_path / "outbox"))
        sender = DesktopCommunicateSender(tool)
        tool.register_sender(DESKTOP_PREFIX, sender.send)
        queue: asyncio.Queue = asyncio.Queue()
        participant_id = "coworker-desktop:desk-1:claude:cw-1:abcd1234"
        assert tool.register_ws(participant_id, queue) is True

        result = await tool.execute(
            participant_id=participant_id,
            conversation_id="session-1",
            message="do the task",
        )

        assert not result.is_error
        request = await asyncio.wait_for(queue.get(), timeout=1)
        assert request.extra["request_id"]
        assert len(request.extra["request_id"]) == 16
        assert "request_id=" in result.content

        tool.unregister_ws(participant_id, queue)
        offline = await tool.execute(participant_id=participant_id, message="retry")
        assert offline.is_error
        assert "未连接" in offline.content

    @pytest.mark.asyncio
    async def test_unconnected_plain_target_rejects_structured_fields(self, tmp_path):
        file_path = tmp_path / "note.txt"
        file_path.write_text("hello", encoding="utf-8")
        tool = CommunicateTool(str(tmp_path / "outbox"))

        conversation_result = await tool.execute(
            participant_id="alice",
            conversation_id="thr_1",
            message="hi",
        )
        attachment_result = await tool.execute(
            participant_id="alice",
            attachments=[{"path": str(file_path)}],
        )
        extra_result = await tool.execute(
            participant_id="alice",
            message="hi",
            extra={"mode": "plan"},
        )

        assert conversation_result.is_error
        assert conversation_result.content.startswith("消息发送失败：")
        assert "不支持 conversation_id" in conversation_result.content
        assert attachment_result.is_error
        assert attachment_result.content.startswith("消息发送失败：")
        assert "不支持附件" in attachment_result.content
        assert extra_result.is_error
        assert extra_result.content.startswith("消息发送失败：")
        assert "不支持 extra" in extra_result.content


class TestVisualAnalysisTool:
    @staticmethod
    def _png_bytes() -> bytes:
        import io

        from PIL import Image as PILImage

        buf = io.BytesIO()
        PILImage.new("RGB", (1, 1), color=(255, 255, 255)).save(buf, format="PNG")
        return buf.getvalue()

    @staticmethod
    def _jpeg_bytes() -> bytes:
        import io

        from PIL import Image as PILImage

        buf = io.BytesIO()
        PILImage.new("RGB", (1, 1), color=(255, 255, 255)).save(buf, format="JPEG")
        return buf.getvalue()

    def _make_brain(self, response: str = "vision result"):
        brain = MagicMock()
        brain.query_with_vision = AsyncMock(return_value=response)
        brain.vision_provider_name = ""
        brain.vision_model = ""
        return brain

    def _make_inbox(self):
        inbox = MagicMock()
        inbox.push = AsyncMock()
        return inbox

    def _make_tool(self, brain=None, provider="anthropic", model="claude-sonnet-4-6", inbox=None):
        return VisualAnalysisTool(
            brain or self._make_brain(),
            provider,
            model,
            inbox=inbox or self._make_inbox(),
        )

    def test_available_to_text_and_vision_models(self):
        assert VisualAnalysisTool.text_model_only is False
        assert VisualAnalysisTool.vision_model_only is False

    def test_definition_name(self):
        definition = self._make_tool().definition
        assert definition.name == "visual_analyze"
        assert definition.parameters["required"] == ["media_path", "question"]

    @pytest.mark.asyncio
    async def test_execute_missing_file_returns_error_immediately(self, tmp_path):
        tool = self._make_tool()
        result = await tool.execute(media_path=str(tmp_path / "no_such.png"), question="what?")
        assert result.is_error
        assert "不存在" in result.content

    @pytest.mark.asyncio
    async def test_execute_returns_immediately_without_waiting(self, tmp_path):
        img = tmp_path / "test.png"
        img.write_bytes(self._png_bytes())
        tool = self._make_tool()
        result = await tool.execute(media_path=str(img), question="describe it")
        assert not result.is_error
        assert "后台" in result.content

    @pytest.mark.asyncio
    async def test_execute_local_file_pushes_result_to_inbox(self, tmp_path):
        img = tmp_path / "test.png"
        img.write_bytes(self._png_bytes())
        inbox = self._make_inbox()
        tool = self._make_tool(inbox=inbox)
        await tool.execute(media_path=str(img), question="describe it")
        await asyncio.sleep(0.05)
        inbox.push.assert_called_once()
        event = inbox.push.call_args[0][0]
        assert "vision result" in event.content
        assert "test.png" in event.content
        assert "describe it" in event.content

    @pytest.mark.asyncio
    async def test_execute_local_file_builds_correct_base64_block(self, tmp_path):
        import base64 as _b64
        png = self._png_bytes()
        img = tmp_path / "test.png"
        img.write_bytes(png)
        brain = self._make_brain()
        tool = self._make_tool(brain=brain)
        await tool.execute(media_path=str(img), question="describe it")
        await asyncio.sleep(0.05)
        messages = brain.query_with_vision.call_args[0][0]
        block = messages[0].content[0]
        assert block["type"] == "image"
        assert block["source"]["type"] == "base64"
        assert block["source"]["media_type"] == "image/png"
        assert block["source"]["data"] == _b64.standard_b64encode(png).decode()

    @pytest.mark.asyncio
    async def test_execute_local_file_passes_question(self, tmp_path):
        img = tmp_path / "test.png"
        img.write_bytes(self._png_bytes())
        brain = self._make_brain()
        tool = self._make_tool(brain=brain)
        await tool.execute(media_path=str(img), question="what color is it?")
        await asyncio.sleep(0.05)
        messages = brain.query_with_vision.call_args[0][0]
        assert messages[0].content[1] == {"type": "text", "text": "what color is it?"}

    @pytest.mark.asyncio
    async def test_execute_url_downloads_and_builds_base64_block(self, monkeypatch):
        raw = self._png_bytes()

        class FakeResponse:
            content = raw
            headers = {"content-type": "image/png"}
            def raise_for_status(self): pass
            async def __aenter__(self): return self
            async def __aexit__(self, *_): pass
            async def aiter_bytes(self): yield self.content

        class FakeClient:
            async def __aenter__(self): return self
            async def __aexit__(self, *_): pass
            def stream(self, method, url): return FakeResponse()

        monkeypatch.setattr("httpx.AsyncClient", lambda **kw: FakeClient())

        brain = self._make_brain()
        tool = self._make_tool(brain=brain)
        await tool.execute(media_path="https://example.com/img.png", question="what?")
        await asyncio.sleep(0.05)
        messages = brain.query_with_vision.call_args[0][0]
        block = messages[0].content[0]
        assert block["source"]["type"] == "base64"
        assert block["source"]["media_type"] == "image/png"

    @pytest.mark.asyncio
    async def test_execute_url_download_failure_returns_error(self, monkeypatch):
        class FakeClient:
            async def __aenter__(self): return self
            async def __aexit__(self, *_): pass
            def stream(self, method, url): raise RuntimeError("connection refused")

        monkeypatch.setattr("httpx.AsyncClient", lambda **kw: FakeClient())

        tool = self._make_tool()
        result = await tool.execute(media_path="http://internal.example/img.png", question="?")
        assert result.is_error
        assert "下载视觉媒体失败" in result.content

    @pytest.mark.asyncio
    async def test_execute_stops_streaming_video_when_source_limit_is_exceeded(
        self, monkeypatch
    ):
        class FakeResponse:
            headers = {"content-type": "video/mp4"}
            def raise_for_status(self): pass
            async def __aenter__(self): return self
            async def __aexit__(self, *_): pass
            async def aiter_bytes(self):
                yield b"1234"
                yield b"5678"

        class FakeClient:
            async def __aenter__(self): return self
            async def __aexit__(self, *_): pass
            def stream(self, method, url): return FakeResponse()

        monkeypatch.setattr("httpx.AsyncClient", lambda **kw: FakeClient())
        monkeypatch.setattr("coworker.tools.vision_tools._VIDEO_SOURCE_LIMIT", 5)

        result = await self._make_tool().execute(
            media_path="https://example.com/clip.mp4", question="?"
        )

        assert result.is_error
        assert "超过 100 MiB" in result.content

    @pytest.mark.asyncio
    async def test_execute_video_builds_native_base64_block_without_fps(self, tmp_path):
        import base64 as _b64

        raw = b"small-video"
        video = tmp_path / "clip.mp4"
        video.write_bytes(raw)
        brain = self._make_brain()
        tool = self._make_tool(brain=brain, provider="qwen", model="qwen3.7-plus")

        result = await tool.execute(media_path=str(video), question="发生了什么？")
        assert not result.is_error
        await asyncio.sleep(0.05)

        messages = brain.query_with_vision.call_args[0][0]
        block = messages[0].content[0]
        assert block == {
            "type": "video",
            "source": {
                "type": "base64",
                "media_type": "video/mp4",
                "data": _b64.standard_b64encode(raw).decode(),
            },
            "_filename": "clip.mp4",
        }
        assert "fps" not in block
        assert brain.query_with_vision.call_args.kwargs["require_video"] is True

    @pytest.mark.asyncio
    async def test_execute_oversize_video_compresses_then_sends(self, tmp_path, monkeypatch):
        raw = b"x" * 100
        compressed = b"small"
        video = tmp_path / "clip.mp4"
        video.write_bytes(raw)
        compress = AsyncMock(return_value=compressed)
        monkeypatch.setattr("coworker.tools.vision_tools._VIDEO_BASE64_LIMIT", 50)
        monkeypatch.setattr("coworker.tools.vision_tools._compress_video", compress)
        brain = self._make_brain()
        tool = self._make_tool(brain=brain, provider="qwen", model="qwen3.7-plus")

        await tool.execute(media_path=str(video), question="发生了什么？")
        await asyncio.sleep(0.05)

        compress.assert_awaited_once_with(raw, ".mp4")
        messages = brain.query_with_vision.call_args[0][0]
        assert messages[0].content[0]["source"]["media_type"] == "video/mp4"

    @pytest.mark.asyncio
    async def test_execute_video_pushes_failure_when_compressed_data_still_too_large(
        self, tmp_path, monkeypatch
    ):
        video = tmp_path / "clip.mp4"
        video.write_bytes(b"x" * 100)
        monkeypatch.setattr("coworker.tools.vision_tools._VIDEO_BASE64_LIMIT", 50)
        monkeypatch.setattr(
            "coworker.tools.vision_tools._compress_video",
            AsyncMock(return_value=b"y" * 80),
        )
        brain = self._make_brain()
        inbox = self._make_inbox()
        tool = self._make_tool(
            brain=brain, provider="qwen", model="qwen3.7-plus", inbox=inbox
        )

        result = await tool.execute(media_path=str(video), question="发生了什么？")
        assert not result.is_error
        await asyncio.sleep(0.05)

        brain.query_with_vision.assert_not_called()
        event = inbox.push.call_args[0][0]
        assert "视频分析失败" in event.content
        assert "仍达到或超过" in event.content

    @pytest.mark.asyncio
    async def test_execute_passes_provider_and_model_to_brain(self, tmp_path):
        img = tmp_path / "test.png"
        img.write_bytes(self._png_bytes())
        brain = self._make_brain()
        tool = self._make_tool(brain=brain, provider="my_provider", model="my_model")
        await tool.execute(media_path=str(img), question="?")
        await asyncio.sleep(0.05)
        kwargs = brain.query_with_vision.call_args[1]
        assert kwargs["vision_provider"] == "my_provider"
        assert kwargs["vision_model"] == "my_model"

    @pytest.mark.asyncio
    async def test_execute_uses_dynamic_brain_vision_config(self, tmp_path):
        img = tmp_path / "test.png"
        img.write_bytes(self._png_bytes())
        brain = self._make_brain()
        brain.vision_provider_name = "dynamic_provider"
        brain.vision_model = "dynamic_model"
        tool = VisualAnalysisTool(brain, inbox=self._make_inbox())
        await tool.execute(media_path=str(img), question="?")
        await asyncio.sleep(0.05)
        kwargs = brain.query_with_vision.call_args[1]
        assert kwargs["vision_provider"] == "dynamic_provider"
        assert kwargs["vision_model"] == "dynamic_model"

    @pytest.mark.asyncio
    async def test_execute_without_vision_config_returns_error(self, tmp_path):
        img = tmp_path / "test.png"
        img.write_bytes(self._png_bytes())
        brain = self._make_brain()
        tool = VisualAnalysisTool(brain, inbox=self._make_inbox())

        result = await tool.execute(media_path=str(img), question="?")

        assert result.is_error
        assert "未配置视觉模型" in result.content
        brain.query_with_vision.assert_not_called()

    @pytest.mark.asyncio
    async def test_execute_pushes_failure_to_inbox_on_brain_error(self, tmp_path):
        img = tmp_path / "test.png"
        img.write_bytes(self._png_bytes())
        brain = MagicMock()
        brain.query_with_vision = AsyncMock(side_effect=RuntimeError("no vision provider"))
        inbox = self._make_inbox()
        tool = self._make_tool(brain=brain, inbox=inbox)
        result = await tool.execute(media_path=str(img), question="?")
        assert not result.is_error
        await asyncio.sleep(0.05)
        inbox.push.assert_called_once()
        event = inbox.push.call_args[0][0]
        assert "失败" in event.content
        assert "no vision provider" in event.content
        assert "test.png" in event.content

    @pytest.mark.asyncio
    async def test_execute_unknown_extension_defaults_to_jpeg(self, tmp_path):
        img = tmp_path / "test.bmp"
        img.write_bytes(self._jpeg_bytes())
        brain = self._make_brain()
        tool = self._make_tool(brain=brain)
        await tool.execute(media_path=str(img), question="?")
        await asyncio.sleep(0.05)
        messages = brain.query_with_vision.call_args[0][0]
        assert messages[0].content[0]["source"]["media_type"] == "image/jpeg"


class TestClearShortTermMemoryTool:
    def _make_tool(self, msg_count: int = 5):
        from coworker.memory.short_term import ShortTermMemory
        short_term = ShortTermMemory()
        for i in range(msg_count):
            short_term.primary.append(Message(role="user", content=f"msg {i}"))
        brain = MagicMock()
        brain.summarize = AsyncMock(return_value="工具压缩摘要")
        return ClearShortTermMemoryTool(short_term, brain), short_term, brain

    @pytest.mark.asyncio
    async def test_compresses_messages_and_returns_count(self):
        tool, short_term, _ = self._make_tool(msg_count=5)
        result = await tool.execute()
        assert not result.is_error
        assert "5" in result.content
        assert "已压缩" in result.content
        assert "清空" not in result.content
        assert len(short_term.primary) == 0
        assert len(short_term.tree.nodes) == 1
        assert short_term.tree.nodes[0].summary == "工具压缩摘要"

    @pytest.mark.asyncio
    async def test_notifies_subconscious_before_compressing(self):
        tool, short_term, brain = self._make_tool(msg_count=3)
        original = list(short_term.primary)
        subconscious = MagicMock()

        async def notify_pre_compress(snapshot):
            assert snapshot == original
            assert short_term.primary == original
            assert short_term.tree.nodes == []

        subconscious.notify_pre_compress = AsyncMock(side_effect=notify_pre_compress)
        tool = ClearShortTermMemoryTool(short_term, brain, subconscious)

        result = await tool.execute()

        assert not result.is_error
        subconscious.notify_pre_compress.assert_awaited_once()
        assert short_term.primary == []
        assert len(short_term.tree.nodes) == 1

    @pytest.mark.asyncio
    async def test_subconscious_failure_does_not_block_compress(self):
        tool, short_term, brain = self._make_tool(msg_count=2)
        subconscious = MagicMock()
        subconscious.notify_pre_compress = AsyncMock(side_effect=RuntimeError("boom"))
        tool = ClearShortTermMemoryTool(short_term, brain, subconscious)

        result = await tool.execute()

        assert not result.is_error
        assert len(short_term.tree.nodes) == 1

    @pytest.mark.asyncio
    async def test_unexpected_legacy_arguments_are_ignored(self):
        tool, short_term, _ = self._make_tool()
        result = await tool.execute(leave_message="交接内容")
        assert not result.is_error
        assert len(short_term.tree.nodes) == 1

    @pytest.mark.asyncio
    async def test_no_messages_returns_noop(self):
        tool, short_term, _ = self._make_tool(msg_count=0)
        result = await tool.execute()
        assert not result.is_error
        assert result.content == "当前没有可压缩的短期记忆消息。"
        assert short_term.primary == []
        assert short_term.tree.nodes == []

    @pytest.mark.asyncio
    async def test_no_messages_does_not_notify_subconscious(self):
        tool, short_term, brain = self._make_tool(msg_count=0)
        subconscious = MagicMock()
        subconscious.notify_pre_compress = AsyncMock()
        tool = ClearShortTermMemoryTool(short_term, brain, subconscious)

        await tool.execute()

        subconscious.notify_pre_compress.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_pins_preserved_after_compress(self):
        tool, short_term, _ = self._make_tool()
        short_term.pin("rules", "规范", "内容")
        short_term.reinject_missing_pins()
        await tool.execute()
        assert len(short_term.pinned_items) == 1
        assert short_term.pinned_items[0].pin_id == "rules"
        assert short_term.primary == []
        reinjected = short_term.reinject_missing_pins()
        assert len(reinjected) == 1
        assert short_term.primary[0].pin_id == "rules"
