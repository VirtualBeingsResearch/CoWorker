from __future__ import annotations

from collections.abc import Iterable

from loguru import logger

from coworker.core.registration import RegistrationError
from coworker.core.types import ToolCall, ToolResult
from coworker.i18n import tr
from coworker.tools.base import Tool, ToolDefinition


class ToolRegistry:
    def __init__(self, intercepts: dict[str, str] | None = None) -> None:
        self._tools: dict[str, Tool] = {}
        # 可选拦截层：{工具名: 拦截原因}。仅作用于执行层——被拦截的工具一旦被调用，
        # 直接返回原因而不触达真实工具；schema 获取不受影响，工具对 LLM 仍可见。
        # 默认为空，对主线零影响。
        self._intercepts: dict[str, str] = dict(intercepts or {})

    def register(self, tool: Tool) -> None:
        entries = self._validated_entries((tool,), subject="tool")
        self._commit(entries)

    def register_many(self, tools: Iterable[Tool]) -> None:
        entries = self._validated_entries(tuple(tools), subject="tool batch")
        self._commit(entries)

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

    def scoped(
        self,
        scope,
        *,
        replacements: Iterable[Tool] = (),
    ) -> ToolRegistry:
        """Return a new registry where each tool is forked with the given scope.

        Same-named replacements replace scoped tools without changing their schema
        contract. Intercepts are preserved.
        """
        replacement_entries = ToolRegistry()._validated_entries(
            tuple(replacements),
            subject="tool replacements",
        )
        replacement_by_name = dict(replacement_entries)
        issues = self._replacement_issues(replacement_by_name)
        if issues:
            raise RegistrationError(
                "tool replacements",
                issues,
            )
        r = ToolRegistry(intercepts=self._intercepts)
        r.register_many(
            replacement_by_name.get(name, tool.fork(scope))
            for name, tool in self._tools.items()
        )
        return r

    def list_names(self) -> list[str]:
        return list(self._tools.keys())

    def _validated_entries(
        self,
        tools: tuple[Tool, ...],
        *,
        subject: str,
    ) -> list[tuple[str, Tool]]:
        issues: list[str] = []
        entries: list[tuple[str, Tool]] = []
        pending_names: dict[str, int] = {}
        for index, tool in enumerate(tools, start=1):
            label = f"item {index}"
            if not isinstance(tool, Tool):
                issues.append(f"{label} tool must inherit Tool")
            try:
                definition = tool.definition
            except Exception as error:
                issues.append(f"{label} definition could not be read: {error}")
                continue
            if not isinstance(definition, ToolDefinition):
                issues.append(f"{label} definition must be a ToolDefinition")
                continue
            name = definition.name
            if not isinstance(name, str):
                issues.append(f"{label} name must be a string")
                continue
            if not name.strip():
                issues.append(f"{label} name is required")
                continue
            if name != name.strip():
                issues.append(f"{label} name must not have surrounding whitespace")
                continue
            if name in self._tools:
                issues.append(f"{label} name {name!r} is already registered")
            first_index = pending_names.get(name)
            if first_index is not None:
                issues.append(
                    f"{label} name {name!r} duplicates item {first_index} in this batch"
                )
            else:
                pending_names[name] = index
            entries.append((name, tool))
        if issues:
            raise RegistrationError(subject, issues)
        return entries

    def _commit(self, entries: list[tuple[str, Tool]]) -> None:
        for name, tool in entries:
            self._tools[name] = tool
            logger.debug(f"Registered tool: {name}")

    def _replacement_issues(
        self,
        replacements: dict[str, Tool],
    ) -> list[str]:
        issues: list[str] = []
        for name, replacement in replacements.items():
            original = self._tools.get(name)
            if original is None:
                issues.append(f"tool {name!r} is not registered")
            elif replacement.definition != original.definition:
                issues.append(
                    f"tool {name!r} replacement must preserve its ToolDefinition exactly"
                )
        return issues
