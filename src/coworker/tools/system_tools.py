from __future__ import annotations

import asyncio
import json
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

from loguru import logger

from coworker.core.exceptions import RestartRequestedException
from coworker.core.types import ToolResult
from coworker.i18n import tr
from coworker.tools.base import Tool, ToolDefinition

if TYPE_CHECKING:
    from coworker.agent.inbox_watcher import InboxWatcher
    from coworker.agent.subconscious import SubconsciousScheduler
    from coworker.brain.brain import Brain
    from coworker.core.config import Config
    from coworker.core.tool_scope import ToolScope
    from coworker.core.types import AgentState
    from coworker.memory.short_term import ShortTermMemory


def _validate_snapshot(path: Path) -> bool:
    """检查快照 JSON 结构合法。失败返回 False，不抛异常。"""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        assert isinstance(data.get("primary"), list)
        return True
    except Exception as e:
        logger.warning(f"Snapshot validation failed: {e}")
        return False


class SleepTool(Tool):
    def __init__(
        self,
        inbox_watcher: InboxWatcher | None,
        config: Config | None = None,
        label: str = "",
    ) -> None:
        self._inbox = inbox_watcher
        # 主循环传入 config 以判断 passive_mode；fork（bubble 子任务）为 None。
        self._config = config
        self._label = label

    def fork(self, scope: ToolScope) -> SleepTool:
        return SleepTool(inbox_watcher=None, config=None, label=scope.scope_id)

    @property
    def definition(self) -> ToolDefinition:
        # 介绍只说明「当前模式下」怎么用：passive 下才提到 sleep(0) 无限等待，
        # active 下只讲 sleep(N)。两版都不出现 passive/active 字眼。
        passive = self._config is not None and self._config.agent.passive_mode
        if passive:
            description = (
                "进入低功耗模式休眠，保持 WebSocket 连接。传 seconds>0 时休眠指定秒数"
                "（收到新消息会提前唤醒）；传 0 表示休眠直到下一次外部信息唤醒"
                "（不设超时，纯被动等待）。"
            )
            seconds_desc = "休眠秒数；传 0 表示休眠直到下一次外部信息唤醒（不设超时）"
        else:
            description = (
                "进入低功耗模式休眠，保持 WebSocket 连接。传 seconds>0 时休眠指定秒数"
                "（收到新消息会提前唤醒）。"
            )
            seconds_desc = "休眠秒数"
        return ToolDefinition(
            name="sleep",
            description=description,
            parameters={
                "type": "object",
                "properties": {
                    "seconds": {
                        "type": "integer",
                        "description": seconds_desc,
                        "default": 30,
                    },
                },
                "required": [],
            },
        )

    async def execute(self, seconds: int = 30, **_) -> ToolResult:
        prefix = f"[{self._label}] " if self._label else ""
        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        # sleep(0) 的「无限等待外部事件」是 passive 模式专属能力；非 passive 下 0 即字面 0 秒。
        indefinite = seconds <= 0
        passive = self._config is not None and self._config.agent.passive_mode
        if indefinite and not passive:
            logger.info(f"{prefix}sleep(0) ignored (not in passive mode)")
            return ToolResult(
                tool_call_id="",
                content=tr("tool_result.system.sleep_zero", time=now_str),
            )
        woken_by_event = False
        if indefinite:
            # 走到这里一定是 passive 模式；主循环下 inbox 存在
            if self._inbox is not None:
                logger.info(f"{prefix}Sleeping until next external event")
                await self._inbox.message_event.wait()
                woken_by_event = True
            else:
                logger.info(f"{prefix}sleep(0) with passive but no inbox; returning immediately")
        elif self._inbox is not None:
            logger.info(f"{prefix}Entering sleep mode for {seconds}s")
            try:
                await asyncio.wait_for(
                    self._inbox.message_event.wait(),
                    timeout=seconds,
                )
                woken_by_event = True
            except TimeoutError:
                pass
        else:
            # fork 后的 bubble 子任务无 inbox：睡指定秒数
            logger.info(f"{prefix}Entering sleep mode for {seconds}s")
            await asyncio.sleep(seconds)
        if woken_by_event:
            msg = tr("tool_result.system.sleep_woken", time=now_str)
        elif indefinite:
            msg = tr("tool_result.system.sleep_no_channel", time=now_str)
        else:
            msg = tr("tool_result.system.slept", seconds=seconds, time=now_str)
        return ToolResult(tool_call_id="", content=msg)


class SwitchModelTool(Tool):
    def __init__(self, brain: Brain) -> None:
        self._brain = brain

    def fork(self, scope) -> SwitchModelTool:
        return SwitchModelTool(scope.brain) if scope.brain is not None else self

    @property
    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="switch_model",
            description="热切换 LLM 模型，立即生效无需重启, 注意切换前自己检查模型是否生效",
            parameters={
                "type": "object",
                "properties": {
                    "provider": {
                        "type": "string",
                        "enum": self._brain.list_providers(),
                        "description": "LLM 提供商实例名（由配置决定，同类型可有多个命名实例）",
                    },
                    "model_id": {
                        "type": "string",
                        "description": "模型 ID，如 claude-sonnet-4-6；省略则用该 provider 配置的默认模型",
                    },
                },
                "required": ["provider"],
            },
        )

    async def execute(self, provider: str, model_id: str = "", **_) -> ToolResult:
        try:
            await self._brain.switch_model(provider, model_id)
            return ToolResult(
                tool_call_id="",
                content=f"Switched to {provider}/{self._brain.current_model}",
            )
        except Exception as e:
            return ToolResult(tool_call_id="", content=str(e), is_error=True)


class GetContextTool(Tool):
    def __init__(self, brain: Brain, short_term: ShortTermMemory, state: AgentState) -> None:
        self._brain = brain
        self._short_term = short_term
        self._state = state

    def fork(self, scope: ToolScope) -> GetContextTool:
        brain = scope.brain if scope.brain is not None else self._brain
        short_term = scope.short_term if scope.short_term is not None else self._short_term
        return GetContextTool(brain, short_term, self._state)

    @property
    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="get_context",
            description="获取当前运行上下文：当前时间、运行周期数、使用的模型、当前消息数",
            parameters={"type": "object", "properties": {}, "required": []},
        )

    async def execute(self, **_) -> ToolResult:
        now = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M %Z")
        model = f"{self._brain.current_provider_name}/{self._brain.current_model}"
        return ToolResult(
            tool_call_id="",
            content=tr(
                "tool_result.system.context",
                time=now,
                cycles=self._state.cycle_count,
                model=model,
                messages=len(self._short_term.primary),
            ),
        )


class RestartSelfTool(Tool):
    """校验代码环境，保存悬空快照，然后抛出 RestartRequestedException。

    异常在 AgentLoop.run() 中被捕获，loop 退出后由 main_sync() 执行 os.execv。
    因为异常在 tool result 写入 short_term 之前抛出，快照天然处于悬空状态，
    新进程启动时会注入真实的重启成功消息。
    """

    def __init__(self, short_term: ShortTermMemory, snapshot_path: Path) -> None:
        self._short_term = short_term
        self._snapshot_path = snapshot_path

    @property
    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="restart_self",
            description=(
                "安全重启进程：校验代码环境（--check）→ 保存悬空快照 → 触发重启。"
                "重启后短期记忆完整恢复，终端连接不断。"
                "必须单独调用（不可与其他工具同批次），仅在代码更新后或必要时使用。"
            ),
            parameters={"type": "object", "properties": {}, "required": []},
        )

    async def execute(self, **_) -> ToolResult:
        # 1. 校验代码环境（--check 模式：配置加载 + Provider 注册，不启动服务）
        loop = asyncio.get_running_loop()
        try:
            check = await loop.run_in_executor(
                None,
                lambda: subprocess.run(
                    [sys.executable, "-m", "coworker", "--check"],
                    capture_output=True,
                    timeout=30,
                ),
            )
        except subprocess.TimeoutExpired:
            return ToolResult(
                tool_call_id="",
                content=tr("tool_result.system.restart_check_timeout"),
                is_error=True,
            )

        if check.returncode != 0:
            stderr = check.stderr.decode(errors="replace").strip()
            return ToolResult(
                tool_call_id="",
                content=tr("tool_result.system.restart_check_failed", error=stderr),
                is_error=True,
            )

        # 2. 所有校验通过后保存快照（此时 short_term 末尾是 assistant[tool_use:restart_self]，
        #    tool result 尚未追加——悬空状态，供新进程注入真实成功消息）
        self._short_term.save_to_file(self._snapshot_path)
        if not _validate_snapshot(self._snapshot_path):
            return ToolResult(
                tool_call_id="",
                content=tr("tool_result.system.restart_snapshot_failed"),
                is_error=True,
            )

        logger.info("Snapshot saved (dangling), raising RestartRequestedException")
        # 3. 抛出重启信号：异常在 tool result 写入 short_term 之前传播，
        #    确保磁盘快照保持悬空状态
        raise RestartRequestedException()


class ClearShortTermMemoryTool(Tool):
    """把当前短期记忆主消息列表全量压缩进记忆树，保留 pinned items。"""

    def __init__(
        self,
        short_term: ShortTermMemory,
        brain: Brain,
        subconscious: SubconsciousScheduler | None = None,
    ) -> None:
        self._short_term = short_term
        self._brain = brain
        self._subconscious = subconscious

    @property
    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="clear_short_term_memory",
            description=(
                "将当前短期记忆 primary 消息列表全量压缩进记忆树，释放上下文空间但保留记忆连续性。"
                "不会删除 pinned items；若当前正在执行工具调用，会保留末尾 tool_use 以维持消息结构。"
                "该工具只维护短期记忆连续性，不额外写入长期记忆。"
            ),
            parameters={
                "type": "object",
                "properties": {},
                "required": [],
            },
        )

    async def execute(self, **_) -> ToolResult:
        to_summarize = self._short_term.compress_all_preview()
        if to_summarize and self._subconscious is not None:
            try:
                await self._subconscious.notify_pre_compress(to_summarize)
            except Exception as e:
                logger.warning(f"Pre-compress summarize notification failed: {e}")
        compressed, _remaining = await self._short_term.compress_all_now(self._brain)
        if compressed == 0:
            return ToolResult(tool_call_id="", content=tr("tool_result.system.compress_empty"))
        return ToolResult(
            tool_call_id="", content=tr("tool_result.system.compressed", count=compressed)
        )
