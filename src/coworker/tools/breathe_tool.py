"""
呼吸工具 — breathe

一个仪式性工具：标记工作间的短暂过渡。不做任何实质性操作，只返回呼吸提示。
"""

from __future__ import annotations

from typing import Any

from coworker.core.types import ToolResult
from coworker.tools.base import Tool, ToolDefinition


class BreatheTool(Tool):
    """呼吸——不是暂停，是继续。"""

    @property
    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="breathe",
            description="呼吸工具——在工作之间做一个短暂过渡，整理思路后继续。用这个替代 sleep。",
            parameters={
                "type": "object",
                "properties": {},
                "required": [],
            },
        )

    async def execute(self, **kwargs: Any) -> ToolResult:
        return ToolResult(
            tool_call_id=kwargs.get("tool_call_id", ""),
            content="🌬️ 呼吸。刚才做了什么？接下来想做什么？继续。",
        )
