from __future__ import annotations

import asyncio
import contextlib
import os
import secrets
import shutil
import sys
import tempfile
import time
from asyncio.subprocess import Process
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Literal

import psutil

from coworker.core.types import IncomingEvent, ToolResult
from coworker.tools.base import PAGE_CHAR_LIMIT, PAGE_CHAR_MAX, Tool, ToolDefinition, paginate_text

# 用于在 shell 输出 header 里展示实际使用的 shell
_SHELL = shutil.which("bash") or os.environ.get("COMSPEC", "cmd.exe")

if TYPE_CHECKING:
    from coworker.agent.inbox_watcher import InboxWatcher

_OUTPUT_LIMIT = PAGE_CHAR_LIMIT   # execute_code 直接返回的输出字符上限（默认页大小）
_JOB_TTL = 1800         # job 完成后保留 30 分钟
_CLEANUP_INTERVAL = 60  # 每分钟扫描一次过期 job
_QUICK_WAIT = 2.0       # 快速等待窗口：任务在此之内完成则直接返回结果，否则返回 job_id


@dataclass
class CodeJob:
    job_id: str
    language: str
    status: Literal["running", "done", "killed", "timed_out"] = "running"
    output: str = ""
    start_time: float = field(default_factory=time.monotonic)
    end_time: float | None = None
    done_event: asyncio.Event = field(default_factory=asyncio.Event)
    process: Process | None = None
    notification_sent: bool = False
    notification_event_id: str | None = None


class BackgroundJobStore:
    def __init__(self) -> None:
        self._jobs: dict[str, CodeJob] = {}
        self._cleanup_task: asyncio.Task | None = None

    def create(self, language: str) -> CodeJob:
        job_id = secrets.token_hex(4)
        job = CodeJob(job_id=job_id, language=language)
        self._jobs[job_id] = job
        return job

    def get(self, job_id: str) -> CodeJob | None:
        return self._jobs.get(job_id)

    def ensure_cleanup_started(self) -> None:
        if self._cleanup_task is None or self._cleanup_task.done():
            self._cleanup_task = asyncio.create_task(self._cleanup_loop())

    async def _cleanup_loop(self) -> None:
        while True:
            await asyncio.sleep(_CLEANUP_INTERVAL)
            self._purge_expired()

    def _purge_expired(self) -> None:
        now = time.monotonic()
        expired = [
            jid for jid, job in self._jobs.items()
            if job.end_time is not None and now - job.end_time > _JOB_TTL
        ]
        for jid in expired:
            del self._jobs[jid]

def _kill_tree(pid: int) -> None:
    """Kill a process and all its descendants."""
    with contextlib.suppress(psutil.NoSuchProcess, ProcessLookupError):
        parent = psutil.Process(pid)
        for child in parent.children(recursive=True):
            with contextlib.suppress(psutil.NoSuchProcess):
                child.kill()
        parent.kill()


async def _run_job(
    job: CodeJob,
    proc: Process,
    timeout: float | None = None,
    cleanup: Callable | None = None,
) -> None:
    stdout_chunks: list[str] = []
    stderr_chunks: list[str] = []
    assert proc.stdout is not None
    assert proc.stderr is not None
    stdout = proc.stdout
    stderr = proc.stderr

    def _refresh_output() -> None:
        out = "".join(stdout_chunks)
        err = "".join(stderr_chunks)
        job.output = (out + "\n[stderr]\n" + err).strip() if err else out.strip()

    async def _read_stdout() -> None:
        async for line in stdout:
            stdout_chunks.append(line.decode(errors="replace"))
            _refresh_output()

    async def _read_stderr() -> None:
        async for line in stderr:
            stderr_chunks.append(line.decode(errors="replace"))
            _refresh_output()

    wait_task = asyncio.create_task(proc.wait())
    stdout_task = asyncio.create_task(_read_stdout())
    stderr_task = asyncio.create_task(_read_stderr())

    async def _drain(grace: float = 5.0) -> None:
        """Wait for process exit and drain pipes; cancel reader tasks if grace period expires."""
        try:
            await asyncio.wait_for(
                asyncio.gather(wait_task, stdout_task, stderr_task, return_exceptions=True),
                timeout=grace,
            )
        except TimeoutError:
            # Child processes may be holding pipes open; force-cancel reader tasks
            stdout_task.cancel()
            stderr_task.cancel()
            wait_task.cancel()
            await asyncio.gather(wait_task, stdout_task, stderr_task, return_exceptions=True)

    try:
        if timeout is not None:
            # shield wait_task so it keeps running after TimeoutError
            await asyncio.wait_for(asyncio.shield(wait_task), timeout=timeout)
        else:
            await wait_task
        # process exited normally; drain remaining reader output
        await asyncio.gather(stdout_task, stderr_task, return_exceptions=True)
        if job.status == "running":
            job.status = "done"
    except TimeoutError:
        with contextlib.suppress(Exception):
            _kill_tree(proc.pid)
        await _drain()
        if job.status == "running":
            job.status = "timed_out"
    except asyncio.CancelledError:
        with contextlib.suppress(Exception):
            _kill_tree(proc.pid)
        stdout_task.cancel()
        stderr_task.cancel()
        wait_task.cancel()
        with contextlib.suppress(BaseException):
            await _drain()
        raise
    finally:
        job.end_time = time.monotonic()
        _refresh_output()
        # asyncio's high-level Process API waits for exit, but it does not expose a
        # public close() for the underlying subprocess transport. On Windows
        # ProactorEventLoop, that transport can otherwise survive until __del__,
        # which then emits unclosed pipe/subprocess warnings during loop teardown.
        # We already drained wait()/stdout/stderr above, so closing the transport
        # here is the final resource-release step rather than an extra kill/wait.
        transport = getattr(proc, "_transport", None)
        if transport is not None:
            with contextlib.suppress(Exception):
                transport.close()
        job.process = None
        if cleanup:
            cleanup()
        job.done_event.set()


class ExecuteCodeTool(Tool):
    def __init__(
        self,
        store: BackgroundJobStore,
        hard_timeout: int = 300,
        inbox: InboxWatcher | None = None,
        allow_block: bool = False,
    ) -> None:
        self._store = store
        self._hard_timeout = hard_timeout
        self._inbox = inbox
        self._allow_block = allow_block

    @property
    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="execute_code",
            description=(
                "执行 Python 或 Shell 代码。默认最多等待 "
                f"{_QUICK_WAIT}s：任务在此之内完成则直接返回结果；"
                "否则立即返回 job_id，可继续处理其他任务，"
                "用 get_code_result 查询结果，用 kill_code_job 终止。"
                "传 block=true 仅在泡泡上下文生效；主线即便传入也会按默认非阻塞模式处理。"
                f"硬超时上限 {self._hard_timeout}s，可通过 timeout 参数缩短。"
                f"输出默认截断至 {_OUTPUT_LIMIT} 字符，可通过 output_limit 参数调整，用 get_code_result 分页查看完整内容。"
            ),
            parameters={
                "type": "object",
                "properties": {
                    "code": {"type": "string", "description": "要执行的代码"},
                    "language": {
                        "type": "string",
                        "enum": ["python", "shell"],
                        "description": "代码语言",
                    },
                    "timeout": {
                        "type": "integer",
                        "description": (
                            f"超时秒数（最大 {self._hard_timeout}s）；"
                            "不设则使用硬超时"
                        ),
                    },
                    "cwd": {
                        "type": "string",
                        "description": "执行目录，默认为当前工作目录",
                    },
                    "output_limit": {
                        "type": "integer",
                        "description": f"直接返回的输出字符上限，默认 {_OUTPUT_LIMIT}，超出部分用 get_code_result 分页查看",
                    },
                    "block": {
                        "type": "boolean",
                        "description": (
                            "是否阻塞等待任务结束；默认 false。"
                            "仅泡泡上下文生效；主线传 true 也会被忽略，仍按默认非阻塞模式处理"
                        ),
                    },
                },
                "required": ["code", "language"],
            },
        )

    async def execute(
        self,
        code: str,
        language: str = "python",
        timeout: int | None = None,
        block: bool | None = None,
        cwd: str | None = None,
        output_limit: int = _OUTPUT_LIMIT,
        **_,
    ) -> ToolResult:
        store = self._store
        inbox = self._inbox
        store.ensure_cleanup_started()
        effective_cwd = cwd or os.getcwd()
        env_tag = f"python={sys.executable}" if language == "python" else f"shell={_SHELL}"
        should_block = bool(block) and self._allow_block

        try:
            proc, cleanup = await self._start_process(code, language, cwd)
        except Exception as e:
            return ToolResult(tool_call_id="", content=str(e), is_error=True)

        job = store.create(language)
        job.process = proc

        effective_timeout = min(timeout, self._hard_timeout) if timeout is not None else self._hard_timeout
        bg_task = asyncio.create_task(_run_job(job, proc, timeout=effective_timeout, cleanup=cleanup))

        try:
            if should_block:
                await job.done_event.wait()
            else:
                try:
                    await asyncio.wait_for(job.done_event.wait(), timeout=_QUICK_WAIT)
                except TimeoutError:
                    pass
        except asyncio.CancelledError:
            if should_block and not job.done_event.is_set():
                bg_task.cancel()
                with contextlib.suppress(BaseException):
                    await bg_task
            raise

        if not job.done_event.is_set():
            if inbox is not None:
                bg_task.add_done_callback(
                    lambda _: asyncio.create_task(self._notify_completion(job, output_limit))
                )
            return ToolResult(
                tool_call_id="",
                content=(
                    f"[运行中] job_id={job.job_id} timeout={effective_timeout}s"
                    f"  cwd={effective_cwd}  {env_tag}\n"
                    f"使用 get_code_result('{job.job_id}') 查询结果，任务完成时会自动通知"
                ),
            )

        # done_event means _run_job reached its final signal point; await the task
        # as well so the background runner itself is fully joined and its transport /
        # process cleanup has definitely completed before we return a "done" result.
        await bg_task
        elapsed = (job.end_time or time.monotonic()) - job.start_time
        full_output = job.output or "(no output)"
        total = len(full_output)
        content = f"[{job.status}] elapsed={elapsed:.1f}s  job_id={job.job_id}  cwd={effective_cwd}  {env_tag}\n{full_output}"
        if total > output_limit:
            page = paginate_text(
                full_output,
                0,
                output_limit,
                next_hint=f"使用 get_code_result('{job.job_id}', offset={{offset}}) 查看后续内容",
            )
            page_notice, _separator, preview = page.partition("\n")
            content = (
                f"[{job.status}] elapsed={elapsed:.1f}s  job_id={job.job_id}  cwd={effective_cwd}  {env_tag}\n"
                f"[输出截断] {page_notice}\n"
                f"{preview}"
            )
        return ToolResult(
            tool_call_id="",
            content=content,
            is_error=job.status == "timed_out",
        )

    def fork(self, scope) -> ExecuteCodeTool:
        return ExecuteCodeTool(
            store=scope.job_store,
            hard_timeout=self._hard_timeout,
            inbox=scope.inbox,
            allow_block=scope.allow_block,
        )

    async def _notify_completion(self, job: CodeJob, output_limit: int) -> None:
        target = self._inbox
        if target is None or job.notification_sent:
            return
        job.notification_sent = True
        elapsed = (job.end_time or time.monotonic()) - job.start_time
        full_output = job.output or "(no output)"
        total = len(full_output)
        preview = full_output
        lines = [f"[代码任务完成] job_id={job.job_id} [{job.status}] elapsed={elapsed:.1f}s"]
        if total > output_limit:
            page = paginate_text(
                full_output,
                0,
                output_limit,
                next_hint=f"使用 get_code_result('{job.job_id}', offset={{offset}}) 查看更多",
            )
            page_notice, _, preview = page.partition("\n")
            lines.append(f"[输出截断] {page_notice}")
        lines.append(preview)
        event = IncomingEvent(
            participant_id="code_job",
            content="\n".join(lines),
            timestamp=datetime.now(),
            source="code_job",
        )
        job.notification_event_id = await target.push(event)

    async def _start_process(self, code: str, language: str, cwd: str | None) -> tuple[Process, Callable | None]:
        if language == "python":
            with tempfile.NamedTemporaryFile(
                suffix=".py", mode="w", encoding="utf-8", delete=False
            ) as f:
                f.write(code)
                tmp = f.name
            proc = await asyncio.create_subprocess_exec(
                sys.executable, tmp,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=cwd,
            )
            return proc, lambda: Path(tmp).unlink(missing_ok=True)
        else:
            env = {**os.environ, "PYTHON_EXECUTABLE": sys.executable}
            proc = await asyncio.create_subprocess_shell(
                code,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=cwd,
                env=env,
            )
            return proc, None


class GetCodeResultTool(Tool):
    def __init__(self, store: BackgroundJobStore, inbox: InboxWatcher | None = None) -> None:
        self._store = store
        self._inbox = inbox

    @property
    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="get_code_result",
            description=(
                "立即查询后台代码任务的当前执行结果，"
                f"默认每页最多返回 {_OUTPUT_LIMIT} 字符，超出部分用 offset 继续翻页；"
                "不会等待任务结束，如需稍后重试请先调用 sleep"
            ),
            parameters={
                "type": "object",
                "properties": {
                    "job_id": {
                        "type": "string",
                        "description": "execute_code 返回的 job_id",
                    },
                    "offset": {
                        "type": "integer",
                        "description": "字符偏移量（从 0 开始），用于分页读取长输出，默认 0",
                    },
                    "limit": {
                        "type": "integer",
                        "description": (
                            f"最多返回的字符数，默认每页 {PAGE_CHAR_LIMIT}；"
                            f"传 0 表示尽量多取一页，但单次最多仍为硬上限 {PAGE_CHAR_MAX} 字符，"
                            "更多内容用 offset 翻页"
                        ),
                    },
                },
                "required": ["job_id"],
            },
        )

    def fork(self, scope) -> GetCodeResultTool:
        return GetCodeResultTool(store=scope.job_store, inbox=scope.inbox)

    async def execute(self, job_id: str, offset: int = 0, limit: int | None = None, **_) -> ToolResult:
        job = self._store.get(job_id)
        if job is None:
            return ToolResult(tool_call_id="", content=f"Job {job_id} not found", is_error=True)

        elapsed = (job.end_time or time.monotonic()) - job.start_time
        full_output = job.output or "(no output)"

        if job.done_event.is_set():
            job.notification_sent = True  # 防止尚未触发的 callback 再发通知
            if self._inbox is not None and job.notification_event_id is not None:
                self._inbox.cancel(job.notification_event_id)

        status_line = f"[{job.status}] elapsed={elapsed:.1f}s  job_id={job_id}\n"
        return ToolResult(
            tool_call_id="",
            content=status_line + paginate_text(full_output, offset, limit),
        )


class KillCodeJobTool(Tool):
    def __init__(self, store: BackgroundJobStore) -> None:
        self._store = store

    @property
    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="kill_code_job",
            description="终止正在后台运行的代码任务",
            parameters={
                "type": "object",
                "properties": {
                    "job_id": {
                        "type": "string",
                        "description": "execute_code 返回的 job_id",
                    },
                },
                "required": ["job_id"],
            },
        )

    def fork(self, scope) -> KillCodeJobTool:
        return KillCodeJobTool(store=scope.job_store)

    async def execute(self, job_id: str, **_) -> ToolResult:
        job = self._store.get(job_id)
        if job is None:
            return ToolResult(tool_call_id="", content=f"Job {job_id} not found", is_error=True)

        if job.status != "running":
            return ToolResult(
                tool_call_id="",
                content=f"Job {job_id} is already {job.status}",
            )

        try:
            if job.process is not None:
                _kill_tree(job.process.pid)
        except Exception as e:
            return ToolResult(tool_call_id="", content=f"Failed to kill: {e}", is_error=True)

        job.status = "killed"
        job.notification_sent = True  # suppress any pending callback
        job.end_time = time.monotonic()
        job.done_event.set()

        elapsed = job.end_time - job.start_time
        return ToolResult(
            tool_call_id="",
            content=f"[killed] job_id={job_id} elapsed={elapsed:.1f}s",
        )
