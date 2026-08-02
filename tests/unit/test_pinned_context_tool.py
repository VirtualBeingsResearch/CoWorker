from __future__ import annotations

import pytest

from coworker.memory.short_term import ShortTermMemory
from coworker.tools.pinned_context_tool import (
    _MAX_SINGLE_PIN_TOKENS,
    ManagePinnedContextTool,
)


@pytest.fixture
def mem():
    return ShortTermMemory()


@pytest.fixture
def tool(mem):
    return ManagePinnedContextTool(mem)


class TestManagePinnedContextToolPin:
    @pytest.mark.asyncio
    async def test_pin_text_content(self, tool, mem):
        result = await tool.execute(action="pin", pin_id="rules", label="规范", content="用 logging")
        assert not result.is_error
        assert "rules" in result.content
        assert len(mem.pinned_items) == 1
        assert len(mem.primary) == 0  # 新 pin 不立即写入 primary

    @pytest.mark.asyncio
    async def test_pin_missing_pin_id(self, tool):
        result = await tool.execute(action="pin", label="规范", content="内容")
        assert result.is_error
        assert "pin_id" in result.content

    @pytest.mark.asyncio
    async def test_pin_missing_label(self, tool):
        result = await tool.execute(action="pin", pin_id="rules", content="内容")
        assert result.is_error
        assert "label" in result.content

    @pytest.mark.asyncio
    async def test_pin_missing_content_and_file_path(self, tool):
        result = await tool.execute(action="pin", pin_id="rules", label="规范")
        assert result.is_error

    @pytest.mark.asyncio
    async def test_pin_file_path(self, tool, mem, tmp_path):
        f = tmp_path / "rules.txt"
        f.write_text("文件中的规范", encoding="utf-8")
        result = await tool.execute(action="pin", pin_id="rules", label="规范", file_path=str(f))
        assert not result.is_error
        assert mem.pinned_items[0].content == "文件中的规范"
        assert mem.pinned_items[0].file_path == str(f)

    @pytest.mark.asyncio
    async def test_pin_file_path_not_found(self, tool):
        result = await tool.execute(action="pin", pin_id="rules", label="规范", file_path="/nonexistent/file.txt")
        assert result.is_error
        assert "读取文件失败" in result.content

    @pytest.mark.asyncio
    async def test_pin_exceeds_single_token_limit(self, tool):
        big_content = "x " * (_MAX_SINGLE_PIN_TOKENS + 100)
        result = await tool.execute(action="pin", pin_id="rules", label="规范", content=big_content)
        assert result.is_error
        assert "过大" in result.content

    @pytest.mark.asyncio
    async def test_pin_exceeds_total_token_limit(self, tool, mem):
        # 直接向 mem 添加 3 个 pin，每个约 4000 tokens（低于单条上限 5000），合计 ~12000
        chunk = "word " * 4000  # ~4000 tokens
        mem.pin("first", "第一", chunk)
        mem.pin("second", "第二", chunk)
        mem.pin("third", "第三", chunk)
        # 再尝试添加第 4 个：合计将超过 15000
        result = await tool.execute(action="pin", pin_id="fourth", label="第四", content=chunk)
        assert result.is_error
        assert "合计" in result.content

    @pytest.mark.asyncio
    async def test_pin_update_shows_update_word(self, tool, mem):
        mem.pin("rules", "旧标题", "旧内容")
        result = await tool.execute(action="pin", pin_id="rules", label="新标题", content="新内容")
        assert not result.is_error
        assert "更新" in result.content
        assert len(mem.pinned_items) == 1

    @pytest.mark.asyncio
    async def test_pin_budget_excludes_system_pins(self, tool, mem):
        # system_managed pin 不占模型手动 pin 预算：14k 系统 pin + 2k 文档应成功
        # （若不排除，合计 16k 会超 15k 上限被拒）
        mem.pin("task_board", "未完成任务", "word " * 14000, system_managed=True)
        result = await tool.execute(
            action="pin", pin_id="model_doc", label="文档", content="doc " * 2000
        )
        assert not result.is_error
        assert len(mem.pinned_items) == 2

    @pytest.mark.asyncio
    async def test_pin_rejects_overwrite_system_pin(self, tool, mem):
        mem.pin("task_board", "未完成任务", "系统内容", system_managed=True)
        result = await tool.execute(
            action="pin", pin_id="task_board", label="覆盖", content="新内容"
        )
        assert result.is_error
        assert "系统管理" in result.content
        assert mem.pinned_items[0].content == "系统内容"


class TestManagePinnedContextToolUnpin:
    @pytest.mark.asyncio
    async def test_unpin_existing(self, tool, mem):
        mem.pin("rules", "规范", "内容")
        mem.reinject_missing_pins()
        visible_pin = mem.primary[0]

        result = await tool.execute(action="unpin", pin_id="rules")

        assert not result.is_error
        assert len(mem.pinned_items) == 0
        assert mem.primary == [visible_pin]

    @pytest.mark.asyncio
    async def test_unpin_nonexistent(self, tool, mem):
        mem.pin("rules", "规范", "内容")
        result = await tool.execute(action="unpin", pin_id="no-such-id")
        assert result.is_error
        assert "rules" in result.content  # 现有 ID 列表提示

    @pytest.mark.asyncio
    async def test_unpin_missing_pin_id(self, tool):
        result = await tool.execute(action="unpin")
        assert result.is_error
        assert "pin_id" in result.content

    @pytest.mark.asyncio
    async def test_unpin_rejects_system_pin(self, tool, mem):
        mem.pin("task_board", "未完成任务", "系统内容", system_managed=True)
        result = await tool.execute(action="unpin", pin_id="task_board")
        assert result.is_error
        assert "系统管理" in result.content
        assert len(mem.pinned_items) == 1


class TestManagePinnedContextToolList:
    @pytest.mark.asyncio
    async def test_list_empty(self, tool):
        result = await tool.execute(action="list")
        assert not result.is_error
        assert "没有" in result.content

    @pytest.mark.asyncio
    async def test_list_shows_items(self, tool, mem):
        mem.pin("a", "标题A", "内容A")
        mem.pin("b", "标题B", "内容B")
        result = await tool.execute(action="list")
        assert not result.is_error
        assert "标题A" in result.content
        assert "标题B" in result.content
        assert "2 条" in result.content

    @pytest.mark.asyncio
    async def test_list_shows_file_path(self, tool, mem, tmp_path):
        f = tmp_path / "spec.txt"
        f.write_text("内容", encoding="utf-8")
        mem.pin("spec", "规格", "内容", file_path=str(f))
        result = await tool.execute(action="list")
        assert str(f) in result.content

    @pytest.mark.asyncio
    async def test_list_marks_system_pins(self, tool, mem):
        mem.pin("task_board", "未完成任务", "系统内容", system_managed=True)
        mem.pin("model", "模型pin", "模型内容")
        result = await tool.execute(action="list")
        assert not result.is_error
        assert "系统管理" in result.content  # system_suffix 标记
        assert "未完成任务" in result.content


class TestManagePinnedContextToolUnknownAction:
    @pytest.mark.asyncio
    async def test_unknown_action(self, tool):
        result = await tool.execute(action="delete")
        assert result.is_error
        assert "未知" in result.content
