from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

import pytest

import coworker.tools.code_tools as code_tools_module
from coworker.core.tool_scope import ToolScope
from coworker.tools.code_tools import (
    BackgroundJobStore,
    ExecuteCodeTool,
    GetCodeResultTool,
    KillCodeJobTool,
)


@pytest.fixture(autouse=True)
async def cleanup_tasks(monkeypatch):
    """Cancel any background _run_job tasks left over after each test."""
    # Completion assertions use block=True; this only speeds up non-blocking paths.
    monkeypatch.setattr(code_tools_module, "_QUICK_WAIT", 0.25)
    yield
    current = asyncio.current_task()
    tasks = [t for t in asyncio.all_tasks() if t is not current and not t.done()]
    for task in tasks:
        task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)


def make_tools(hard_timeout: int = 10, *, allow_block: bool = False):
    store = BackgroundJobStore()
    execute = ExecuteCodeTool(
        store=store,
        hard_timeout=hard_timeout,
        allow_block=allow_block,
    )
    get_result = GetCodeResultTool(store)
    kill = KillCodeJobTool(store)
    return execute, get_result, kill, store


class TestBackgroundJobStore:
    def test_create_returns_unique_ids(self):
        store = BackgroundJobStore()
        ids = {store.create("python").job_id for _ in range(10)}
        assert len(ids) == 10

    def test_get_existing_job(self):
        store = BackgroundJobStore()
        job = store.create("shell")
        assert store.get(job.job_id) is job

    def test_get_missing_job_returns_none(self):
        store = BackgroundJobStore()
        assert store.get("nonexistent") is None


class TestExecuteCodeTool:
    async def test_python_stdout(self):
        execute, *_ = make_tools(allow_block=True)
        result = await execute.execute(code='print("hello")', language="python", block=True)
        assert not result.is_error
        assert "hello" in result.content
        assert "done" in result.content

    async def test_python_stderr(self):
        execute, *_ = make_tools(allow_block=True)
        result = await execute.execute(
            code="import sys; sys.stderr.write('err\\n')", language="python", block=True
        )
        assert not result.is_error
        assert "err" in result.content

    async def test_stderr_only_output(self):
        execute, *_ = make_tools(allow_block=True)
        result = await execute.execute(
            code="import sys; sys.stderr.write('only_err\\n')", language="python", block=True
        )
        assert not result.is_error
        assert "[stderr]" in result.content
        assert "only_err" in result.content

    async def test_shell_execution(self):
        execute, *_ = make_tools(allow_block=True)
        result = await execute.execute(code="echo shell_ok", language="shell", block=True)
        assert not result.is_error
        assert "shell_ok" in result.content

    async def test_timeout_kills_process(self):
        execute, *_ = make_tools(hard_timeout=10, allow_block=True)
        result = await execute.execute(
            code="import time; time.sleep(60)", language="python", timeout=1, block=True
        )
        assert result.is_error
        assert "timed_out" in result.content

    async def test_timeout_clamped_to_hard_timeout(self):
        execute, *_ = make_tools(hard_timeout=1, allow_block=True)
        result = await execute.execute(
            code="import time; time.sleep(60)", language="python", timeout=9999, block=True
        )
        assert result.is_error
        assert "timed_out" in result.content

    async def test_hard_timeout_applied_when_no_timeout_param(self):
        execute, *_ = make_tools(hard_timeout=1, allow_block=True)
        result = await execute.execute(
            code="import time; time.sleep(60)", language="python", block=True
        )
        assert result.is_error
        assert "timed_out" in result.content

    async def test_execute_returns_job_id_for_long_running(self):
        execute, *_ = make_tools()
        result = await execute.execute(code="import time; time.sleep(5)", language="python")
        assert not result.is_error
        assert "job_id=" in result.content
        assert "get_code_result" in result.content

    async def test_block_waits_past_quick_window(self, monkeypatch):
        monkeypatch.setattr(code_tools_module, "_QUICK_WAIT", 0.05)
        execute = ExecuteCodeTool(
            store=BackgroundJobStore(),
            allow_block=True,
        )
        result = await execute.execute(
            code='import time; time.sleep(0.2); print("blocked_done")',
            language="python",
            block=True,
        )
        assert not result.is_error
        assert "[done]" in result.content
        assert "blocked_done" in result.content
        assert "使用 get_code_result" not in result.content

    async def test_main_scope_ignores_block(self, monkeypatch):
        monkeypatch.setattr(code_tools_module, "_QUICK_WAIT", 0.05)
        execute, *_ = make_tools()
        result = await execute.execute(
            code='import time; time.sleep(0.2); print("nope")',
            language="python",
            block=True,
        )
        assert not result.is_error
        assert "job_id=" in result.content
        assert "get_code_result" in result.content

    def test_main_definition_exposes_block_parameter_with_bubble_note(self):
        execute, *_ = make_tools()
        schema = execute.definition.parameters["properties"]
        assert "block" in schema
        assert "仅泡泡上下文生效" in schema["block"]["description"]
        assert "主线即便传入也会按默认非阻塞模式处理" in execute.definition.description

    def test_bubble_fork_exposes_block_parameter(self):
        execute, *_ = make_tools()
        scope = ToolScope(
            task_store=object(),
            job_store=BackgroundJobStore(),
            inbox=None,
            scope_id="bbl_test",
            allow_block=True,
        )
        forked = execute.fork(scope)
        assert "block" in forked.definition.parameters["properties"]

    async def test_default_cwd_is_process_cwd(self):
        execute, *_ = make_tools(allow_block=True)
        result = await execute.execute(
            code="import os; print(os.getcwd())", language="python", block=True
        )
        assert not result.is_error
        assert os.getcwd() in result.content

    async def test_cwd_parameter(self, tmp_path):
        execute, *_ = make_tools(allow_block=True)
        result = await execute.execute(
            code="import os; print(os.getcwd())",
            language="python",
            cwd=str(tmp_path),
            block=True,
        )
        assert not result.is_error
        assert str(tmp_path) in result.content

    async def test_shell_cwd(self, tmp_path: Path):
        execute, *_ = make_tools(allow_block=True)
        # cmd.exe uses "cd" to print cwd; Unix shells use "pwd"
        cmd = "cd" if sys.platform == "win32" else "pwd"
        result = await execute.execute(code=cmd, language="shell", cwd=str(tmp_path), block=True)
        assert not result.is_error
        assert tmp_path.name in result.content

    async def test_python_exit_nonzero(self):
        execute, *_ = make_tools(allow_block=True)
        result = await execute.execute(code="raise SystemExit(1)", language="python", block=True)
        assert "done" in result.content

    async def test_elapsed_time_in_result(self):
        execute, *_ = make_tools(allow_block=True)
        result = await execute.execute(code="pass", language="python", block=True)
        assert "elapsed=" in result.content

    async def test_output_limit_truncates(self):
        execute, *_ = make_tools(allow_block=True)
        result = await execute.execute(
            code="print('x' * 200)", language="python", output_limit=50, block=True
        )
        assert "输出截断" in result.content
        assert "get_code_result" in result.content

    async def test_output_limit_default_no_truncation(self):
        execute, *_ = make_tools(allow_block=True)
        result = await execute.execute(code="print('hello')", language="python", block=True)
        assert "输出截断" not in result.content
        assert "hello" in result.content

    async def test_completed_job_releases_process_handle(self):
        execute, _, _, store = make_tools()
        await execute.execute(code='print("done")', language="python")
        job = list(store._jobs.values())[-1]
        await asyncio.wait_for(job.done_event.wait(), timeout=10)
        assert job.process is None


class TestGetCodeResultTool:
    async def test_returns_done_result(self):
        execute, get_result, _, store = make_tools()
        await execute.execute(code='print("x")', language="python")
        job = list(store._jobs.values())[-1]
        await asyncio.wait_for(job.done_event.wait(), timeout=10)

        result = await get_result.execute(job_id=job.job_id)
        assert not result.is_error
        assert "done" in result.content
        assert "x" in result.content
        assert "无需连续查询" not in result.content

    async def test_running_job_returns_running_status(self):
        execute, get_result, _, store = make_tools()
        await execute.execute(code="import time; time.sleep(10)", language="python")
        job = list(store._jobs.values())[-1]

        result = await get_result.execute(job_id=job.job_id)
        assert not result.is_error
        assert "running" in result.content
        assert "无需连续查询" in result.content
        assert "自动推送" in result.content

    async def test_missing_job_returns_error(self):
        _, get_result, *_ = make_tools()
        result = await get_result.execute(job_id="doesnotexist")
        assert result.is_error
        assert "not found" in result.content

    async def test_default_pagination_caps_output(self):
        """get_code_result 不传 limit 时应按默认页大小截断，并提示翻页 offset。"""
        from coworker.tools.code_tools import _OUTPUT_LIMIT

        execute, get_result, _, store = make_tools()
        await execute.execute(code=f"print('x' * {_OUTPUT_LIMIT * 2})", language="python")
        job = list(store._jobs.values())[-1]
        await asyncio.wait_for(job.done_event.wait(), timeout=10)

        result = await get_result.execute(job_id=job.job_id)
        assert not result.is_error
        # body（去掉 header 行）不应超过默认页大小
        body = result.content.split("\n", 2)[-1]
        assert len(body) <= _OUTPUT_LIMIT
        assert "如需后续内容" in result.content
        assert f"offset={_OUTPUT_LIMIT}" in result.content

    async def test_limit_zero_still_capped_by_hard_max(self):
        """limit=0 不是无界逃生口：仍被硬上限 PAGE_CHAR_MAX 钳住。"""
        from coworker.tools.base import PAGE_CHAR_MAX as _MAX_OUTPUT

        execute, get_result, _, store = make_tools()
        n = _MAX_OUTPUT * 2
        await execute.execute(code=f"print('x' * {n})", language="python")
        job = list(store._jobs.values())[-1]
        await asyncio.wait_for(job.done_event.wait(), timeout=10)

        result = await get_result.execute(job_id=job.job_id, limit=0)
        assert not result.is_error
        body = result.content.split("\n", 2)[-1]
        assert len(body) <= _MAX_OUTPUT
        assert "如需后续内容" in result.content

    async def test_large_limit_clamped_to_hard_max(self):
        """显式传一个很大的 limit 也会被钳到 PAGE_CHAR_MAX。"""
        from coworker.tools.base import PAGE_CHAR_MAX as _MAX_OUTPUT

        execute, get_result, _, store = make_tools()
        n = _MAX_OUTPUT * 2
        await execute.execute(code=f"print('x' * {n})", language="python")
        job = list(store._jobs.values())[-1]
        await asyncio.wait_for(job.done_event.wait(), timeout=10)

        result = await get_result.execute(job_id=job.job_id, limit=n)
        assert not result.is_error
        body = result.content.split("\n", 2)[-1]
        assert len(body) <= _MAX_OUTPUT

    async def test_get_result_returns_immediately(self):
        execute, get_result, _, store = make_tools()
        await execute.execute(code="import time; time.sleep(10)", language="python")
        job = list(store._jobs.values())[-1]

        result = await asyncio.wait_for(get_result.execute(job_id=job.job_id), timeout=1)
        assert "running" in result.content


class TestKillCodeJobTool:
    async def test_kills_running_job(self):
        execute, get_result, kill, store = make_tools()
        await execute.execute(code="import time; time.sleep(60)", language="python")
        job = list(store._jobs.values())[-1]

        kill_result = await kill.execute(job_id=job.job_id)
        assert not kill_result.is_error
        assert "killed" in kill_result.content

        check = await get_result.execute(job_id=job.job_id)
        assert "killed" in check.content

    async def test_kill_status_not_overridden_by_run_job(self):
        """_run_job should not overwrite 'killed' status with 'done' after process exits."""
        execute, _, kill, store = make_tools()
        await execute.execute(code="import time; time.sleep(60)", language="python")
        job = list(store._jobs.values())[-1]

        await kill.execute(job_id=job.job_id)
        assert job.status == "killed"

        # Wait for _run_job to finish draining after the kill.
        async with asyncio.timeout(10):
            while job.process is not None:
                await asyncio.sleep(0.01)
        assert job.status == "killed"

    async def test_kill_already_done_job(self):
        execute, _, kill, store = make_tools(allow_block=True)
        await execute.execute(code="pass", language="python", block=True)
        job = list(store._jobs.values())[-1]

        kill_result = await kill.execute(job_id=job.job_id)
        assert not kill_result.is_error
        assert "already" in kill_result.content

    async def test_kill_missing_job_returns_error(self):
        _, _, kill, _ = make_tools()
        result = await kill.execute(job_id="ghost")
        assert result.is_error
        assert "not found" in result.content


class TestEndToEndFlow:
    async def test_background_then_get_result(self):
        execute, get_result, _, store = make_tools()

        await execute.execute(
            code='import time; time.sleep(0.1); print("bg_done")',
            language="python",
        )
        job = list(store._jobs.values())[-1]

        await asyncio.wait_for(job.done_event.wait(), timeout=15)
        result = await asyncio.wait_for(get_result.execute(job_id=job.job_id), timeout=15)
        assert "done" in result.content
        assert "bg_done" in result.content

    async def test_background_then_kill(self):
        execute, get_result, kill, store = make_tools()

        await execute.execute(code="import time; time.sleep(60)", language="python")
        job = list(store._jobs.values())[-1]

        await kill.execute(job_id=job.job_id)
        result = await get_result.execute(job_id=job.job_id)
        assert "killed" in result.content
