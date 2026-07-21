from __future__ import annotations

from loguru import logger

from coworker.core.types import ToolCall, ToolResult
from coworker.i18n import tr
from coworker.tools.base import Tool


class ToolRegistry:
    def __init__(self, intercepts: dict[str, str] | None = None) -> None:
        self._tools: dict[str, Tool] = {}
        # 可选拦截层：{工具名: 拦截原因}。仅作用于执行层——被拦截的工具一旦被调用，
        # 直接返回原因而不触达真实工具；schema 获取不受影响，工具对 LLM 仍可见。
        # 默认为空，对主线零影响。
        self._intercepts: dict[str, str] = dict(intercepts or {})

    def register(self, tool: Tool) -> None:
        self._tools[tool.definition.name] = tool
        logger.debug(f"Registered tool: {tool.definition.name}")

    def intercept(self, intercepts: dict[str, str]) -> ToolRegistry:
        """Return a new registry sharing the same tool instances but with the given
        tools intercepted (hidden from schemas and blocked at execute with a reason).

        Existing intercepts are kept; the passed-in ones are merged on top.
        Tool instances are shared by reference (no fork), so this is cheap.
        """
        r = ToolRegistry(intercepts={**self._intercepts, **intercepts})
        r._tools = self._tools
        return r

    def get_schemas(self, model_has_vision: bool = True) -> list[dict]:
        return [
            t.definition.to_schema()
            for t in self._tools.values()
            if not (t.text_model_only and model_has_vision)
            and not (t.vision_model_only and not model_has_vision)
        ]

    async def execute(self, tool_call: ToolCall) -> ToolResult:
        reason = self._intercepts.get(tool_call.name)
        if reason is not None:
            return ToolResult(
                tool_call_id=tool_call.id,
                content=reason,
                is_error=True,
            )
        tool = self._tools.get(tool_call.name)
        if not tool:
            return ToolResult(
                tool_call_id=tool_call.id,
                content=tr("tools.framework.unknown", name=tool_call.name),
                is_error=True,
            )
        try:
            return await tool.execute(**tool_call.arguments)
        except Exception as e:
            logger.error(f"Tool '{tool_call.name}' raised: {e}")
            return ToolResult(
                tool_call_id=tool_call.id,
                content=tr("tools.framework.execution_error", error=e),
                is_error=True,
            )

    def scoped(self, scope) -> ToolRegistry:
        """Return a new registry where each tool is forked with the given scope.

        Intercepts are carried over so scoping does not drop the interception layer.
        """
        r = ToolRegistry(intercepts=self._intercepts)
        for tool in self._tools.values():
            r.register(tool.fork(scope))
        return r

    def list_names(self) -> list[str]:
        return list(self._tools.keys())
