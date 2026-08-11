"""Environment sensing tools — agent-facing actions.

Three tools:
- ``manage_environment`` — list / enable / disable / reload / run_now sources
- ``get_system_status`` — real-time CPU/memory/disk snapshot (psutil)
- ``get_runtime_context`` — container/cloud/host detection

``manage_environment`` mirrors the ``manage_memory`` / ``persona`` action-enum
pattern.  It injects the ``EnvironmentRuntime`` + ``EnvironmentLoader`` so the
agent never touches files directly — the source lifecycle is managed via
``write_file`` (create ``SOURCE.md`` + ``source.py``) then ``reload`` to
discover, exactly like skills.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import psutil
from loguru import logger

from coworker.core.types import ToolResult
from coworker.i18n import tr
from coworker.tools.base import Tool, ToolDefinition

if TYPE_CHECKING:
    from coworker.channels.environment.loader import EnvironmentLoader
    from coworker.channels.environment.runtime import EnvironmentRuntime


# ---------------------------------------------------------------------------
# manage_environment
# ---------------------------------------------------------------------------


class ManageEnvironmentTool(Tool):
    """Manage environment sensing sources."""

    def __init__(self, runtime: EnvironmentRuntime, loader: EnvironmentLoader) -> None:
        self._runtime = runtime
        self._loader = loader

    @property
    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="manage_environment",
            description=(
                "管理环境感知源：列出所有源、启停某个源、重新扫描目录发现新源、"
                "或立即触发某源的一次采集。源脚本通过 write_file 创建到 "
                ".coworker/environment/<name>/ 目录后，用 reload 发现。"
            ),
            parameters={
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["list", "enable", "disable", "reload", "run_now"],
                        "description": (
                            "操作类型：list（列出所有源及运行状态）、enable/disable"
                            "（启停某源，立即生效）、reload（重新扫描源目录）、"
                            "run_now（立即触发某源的一次采集）"
                        ),
                    },
                    "source_id": {
                        "type": "string",
                        "description": "源名称（enable/disable/run_now 时必填）",
                    },
                },
                "required": ["action"],
            },
        )

    async def execute(
        self,
        action: str,
        source_id: str | None = None,
        **_: Any,
    ) -> ToolResult:
        try:
            if action == "list":
                return self._list_sources()
            if action == "reload":
                return await self._reload()
            if action in ("enable", "disable"):
                if not source_id:
                    return ToolResult(
                        tool_call_id="",
                        content=tr("environment.manage.needs_source_id", action=action),
                        is_error=True,
                    )
                return await self._set_enabled(source_id, enabled=(action == "enable"))
            if action == "run_now":
                if not source_id:
                    return ToolResult(
                        tool_call_id="",
                        content=tr("environment.manage.needs_source_id", action=action),
                        is_error=True,
                    )
                return await self._run_now(source_id)
            return ToolResult(
                tool_call_id="",
                content=tr("tool_result.common.unknown_action", action=action),
                is_error=True,
            )
        except Exception as exc:
            logger.exception(f"manage_environment failed (action={action!r}): {exc}")
            return ToolResult(tool_call_id="", content=str(exc), is_error=True)

    def _list_sources(self) -> ToolResult:
        self._loader.load_all()
        sources = self._loader.list_all()
        if not sources:
            return ToolResult(
                tool_call_id="",
                content=tr("environment.manage.empty"),
            )
        lines = [tr("environment.manage.list_header", count=len(sources))]
        for src in sources:
            schedule = _describe_schedule_brief(src)
            status = "✓" if src.enabled else "✗"
            lines.append(
                tr(
                    "environment.manage.list_entry",
                    status=status,
                    name=src.name,
                    mode=src.mode,
                    schedule=schedule,
                )
            )
        lines.append("")
        lines.append(tr("environment.manage.list_tail"))
        return ToolResult(tool_call_id="", content="\n".join(lines))

    async def _reload(self) -> ToolResult:
        self._loader.load_all()
        warnings = self._loader.consume_load_warnings()
        count = len(self._loader.list_all())
        if warnings:
            return ToolResult(
                tool_call_id="",
                content=tr(
                    "environment.manage.reloaded_with_warnings",
                    count=count,
                    warnings="\n".join(warnings),
                ),
            )
        return ToolResult(
            tool_call_id="",
            content=tr("environment.manage.reloaded", count=count),
        )

    async def _set_enabled(self, source_id: str, *, enabled: bool) -> ToolResult:
        self._loader.load_all()
        definition = self._loader.get(source_id)
        if definition is None:
            return ToolResult(
                tool_call_id="",
                content=tr("environment.manage.not_found", source_id=source_id),
                is_error=True,
            )
        # Persist the override in the runtime's state store.
        from coworker.channels.environment.state import SourceStateStore

        # The runtime holds the authoritative state store; we reach it via
        # the _state_store attribute (set in __init__).  This keeps the tool
        # decoupled from file I/O.
        store: SourceStateStore = self._runtime._state_store
        await store.load()
        state = store.get(source_id)
        state.enabled_override = enabled
        await store.save()
        action_word = tr("environment.manage.enabled") if enabled else tr("environment.manage.disabled")
        return ToolResult(
            tool_call_id="",
            content=tr(
                "environment.manage.set_enabled",
                action=action_word,
                source_id=source_id,
            ),
        )

    async def _run_now(self, source_id: str) -> ToolResult:
        self._loader.load_all()
        ok = await self._runtime.run_source_now(source_id)
        if not ok:
            return ToolResult(
                tool_call_id="",
                content=tr("environment.manage.not_found", source_id=source_id),
                is_error=True,
            )
        return ToolResult(
            tool_call_id="",
            content=tr("environment.manage.triggered", source_id=source_id),
        )


# ---------------------------------------------------------------------------
# get_system_status
# ---------------------------------------------------------------------------


class GetSystemStatusTool(Tool):
    """Return a real-time snapshot of system resource usage."""

    @property
    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="get_system_status",
            description=(
                "获取当前系统资源状态快照：CPU 使用率、内存占用、磁盘空间、进程数。"
                "用于了解自身运行环境的资源水位。"
            ),
            parameters={
                "type": "object",
                "properties": {},
            },
        )

    async def execute(self, **_: Any) -> ToolResult:
        try:
            cpu_percent = psutil.cpu_percent(interval=0.5)
            memory = psutil.virtual_memory()
            disk = psutil.disk_usage("/")
            process_count = len(psutil.pids())
            # Own process memory
            own_process = psutil.Process()
            own_mem = own_process.memory_info()

            lines = [
                tr("environment.system_status.header"),
                tr(
                    "environment.system_status.cpu",
                    percent=f"{cpu_percent:.1f}",
                ),
                tr(
                    "environment.system_status.memory",
                    used=f"{memory.used / (1024**3):.2f}",
                    total=f"{memory.total / (1024**3):.2f}",
                    percent=f"{memory.percent:.1f}",
                ),
                tr(
                    "environment.system_status.disk",
                    used=f"{disk.used / (1024**3):.2f}",
                    total=f"{disk.total / (1024**3):.2f}",
                    percent=f"{disk.percent:.1f}",
                ),
                tr("environment.system_status.processes", count=process_count),
                tr(
                    "environment.system_status.own_memory",
                    rss=f"{own_mem.rss / (1024**2):.1f}",
                ),
            ]
            return ToolResult(tool_call_id="", content="\n".join(lines))
        except Exception as exc:
            logger.exception(f"get_system_status failed: {exc}")
            return ToolResult(tool_call_id="", content=str(exc), is_error=True)


# ---------------------------------------------------------------------------
# get_runtime_context
# ---------------------------------------------------------------------------


class GetRuntimeContextTool(Tool):
    """Return container/cloud/host runtime information."""

    @property
    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="get_runtime_context",
            description=(
                "获取运行时环境信息：是否在容器中、容器运行时（docker/kubernetes 等）、"
                "云厂商、主机名、平台。用于了解自己身处何处。"
            ),
            parameters={
                "type": "object",
                "properties": {},
            },
        )

    async def execute(self, **_: Any) -> ToolResult:
        from coworker.channels.environment.detect import get_runtime_context

        ctx = get_runtime_context()
        container: dict[str, object] = dict(ctx["container"])
        host: dict[str, object] = dict(ctx["host"])
        lines = [tr("environment.runtime_context.header")]
        if container.get("in_container"):
            runtime = container.get("runtime") or "unknown"
            lines.append(
                tr("environment.runtime_context.container", runtime=str(runtime))
            )
        else:
            lines.append(tr("environment.runtime_context.no_container"))
        cloud = container.get("cloud_provider")
        if cloud:
            lines.append(tr("environment.runtime_context.cloud", provider=str(cloud)))
        lines.append(
            tr("environment.runtime_context.hostname", name=str(host.get("hostname", "")))
        )
        lines.append(
            tr("environment.runtime_context.platform", platform=str(host.get("platform", "")))
        )
        lines.append(
            tr("environment.runtime_context.python", version=str(host.get("python_version", "")))
        )
        return ToolResult(tool_call_id="", content="\n".join(lines))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _describe_schedule_brief(src: Any) -> str:
    """Brief schedule description for tool output."""
    if src.interval_seconds:
        return f"every {int(src.interval_seconds)}s"
    if src.every_seconds:
        return f"every {src.every_seconds}s"
    if src.every_n_cycles:
        return f"every {src.every_n_cycles} cycles"
    if src.cron:
        return f"cron {src.cron}"
    if src.cold_floor_seconds:
        return f"cold_floor {src.cold_floor_seconds}s"
    return "manual"
