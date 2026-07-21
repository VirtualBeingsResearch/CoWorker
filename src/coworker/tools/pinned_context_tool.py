from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

from coworker.core.token_utils import estimate_text_tokens
from coworker.core.types import ToolResult
from coworker.i18n import tr
from coworker.tools.base import Tool, ToolDefinition

if TYPE_CHECKING:
    from coworker.core.tool_scope import ToolScope

_MAX_SINGLE_PIN_TOKENS = 5_000
_MAX_TOTAL_PINNED_TOKENS = 15_000


class ManagePinnedContextTool(Tool):
    def __init__(self, short_term: Any) -> None:  # ShortTermMemory | PinnedItems
        self._short_term = short_term

    def fork(self, scope: ToolScope) -> ManagePinnedContextTool:
        if scope.short_term is not None:
            return ManagePinnedContextTool(scope.short_term)
        return self

    @property
    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="manage_pinned_context",
            description=(
                "管理 pin 消息：将重要文本或文件内容固定在对话中。"
                "Pin 的内容以真实消息形式存在于对话流中，被压缩后自动重新出现在最新输入，"
                "起到缓存命中保留和定期强调的作用。"
            ),
            parameters={
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["pin", "unpin", "list"],
                        "description": (
                            "操作类型：\n"
                            "- pin：添加或更新一条 pin 内容\n"
                            "- unpin：取消一条 pin\n"
                            "- list：查看当前所有 pin"
                        ),
                    },
                    "pin_id": {
                        "type": "string",
                        "description": (
                            "pin 的唯一标识，建议使用语义化名称如 'coding_rules'、'project_goal'。"
                            "pin_id 已存在时覆盖更新。pin 和 unpin 时必填。"
                        ),
                    },
                    "label": {
                        "type": "string",
                        "description": "pin 内容的标题，展示在消息内容头部。pin 时必填。",
                    },
                    "content": {
                        "type": "string",
                        "description": "要 pin 的文本内容。pin 时与 file_path 二选一。",
                    },
                    "file_path": {
                        "type": "string",
                        "description": (
                            "要 pin 的文件路径。pin 时与 content 二选一。"
                            "每次该 pin 被压缩后重新注入时，会重新读取文件最新内容。"
                        ),
                    },
                },
                "required": ["action"],
            },
        )

    async def execute(
        self,
        action: str,
        pin_id: str | None = None,
        label: str | None = None,
        content: str | None = None,
        file_path: str | None = None,
        **_,
    ) -> ToolResult:
        if action == "pin":
            return self._do_pin(pin_id, label, content, file_path)
        elif action == "unpin":
            return self._do_unpin(pin_id)
        elif action == "list":
            return self._do_list()
        return ToolResult(
            tool_call_id="",
            content=tr("tool_result.common.unknown_action", action=action),
            is_error=True,
        )

    def _do_pin(
        self,
        pin_id: str | None,
        label: str | None,
        content: str | None,
        file_path: str | None,
    ) -> ToolResult:
        if not pin_id:
            return ToolResult(
                tool_call_id="", content=tr("tool_result.pin.needs_id"), is_error=True
            )
        if not label:
            return ToolResult(
                tool_call_id="", content=tr("tool_result.pin.needs_label"), is_error=True
            )
        if not content and not file_path:
            return ToolResult(
                tool_call_id="", content=tr("tool_result.pin.needs_content"), is_error=True
            )

        if file_path:
            try:
                content = Path(file_path).read_text(encoding="utf-8")
            except Exception as e:
                return ToolResult(
                    tool_call_id="",
                    content=tr("tool_result.pin.read_failed", error=e),
                    is_error=True,
                )

        assert content is not None
        single_tokens = estimate_text_tokens(content)
        if single_tokens > _MAX_SINGLE_PIN_TOKENS:
            return ToolResult(
                tool_call_id="",
                content=tr(
                    "tool_result.pin.single_too_large",
                    tokens=single_tokens,
                    limit=_MAX_SINGLE_PIN_TOKENS,
                ),
                is_error=True,
            )

        # 计算更新后的总 token（如已存在则先减去旧占用）
        old_item = next(
            (item for item in self._short_term.pinned_items if item.pin_id == pin_id), None
        )
        existing_tokens = sum(
            estimate_text_tokens(item.content) for item in self._short_term.pinned_items
        )
        if old_item is not None:
            existing_tokens -= estimate_text_tokens(old_item.content)
        projected_total = existing_tokens + single_tokens
        if projected_total > _MAX_TOTAL_PINNED_TOKENS:
            return ToolResult(
                tool_call_id="",
                content=tr(
                    "tool_result.pin.total_too_large",
                    tokens=projected_total,
                    limit=_MAX_TOTAL_PINNED_TOKENS,
                ),
                is_error=True,
            )

        is_update = old_item is not None
        self._short_term.pin(pin_id=pin_id, label=label, content=content, file_path=file_path)
        total_count = len(self._short_term.pinned_items)
        action_word = tr("tool_result.pin.updated" if is_update else "tool_result.pin.added")
        return ToolResult(
            tool_call_id="",
            content=tr(
                "tool_result.pin.saved",
                action=action_word,
                label=label,
                id=pin_id,
                tokens=single_tokens,
                count=total_count,
                total=projected_total,
            ),
        )

    def _do_unpin(self, pin_id: str | None) -> ToolResult:
        if not pin_id:
            return ToolResult(
                tool_call_id="",
                content=tr("tool_result.pin.unpin_needs_id"),
                is_error=True,
            )
        found = self._short_term.unpin(pin_id)
        if not found:
            existing_ids = [item.pin_id for item in self._short_term.pinned_items]
            hint = (
                tr("tool_result.pin.current", ids=existing_ids)
                if existing_ids
                else tr("tool_result.pin.none")
            )
            return ToolResult(
                tool_call_id="",
                content=tr("tool_result.pin.missing", id=pin_id, hint=hint),
                is_error=True,
            )
        remaining = len(self._short_term.pinned_items)
        return ToolResult(
            tool_call_id="", content=tr("tool_result.pin.unpinned", id=pin_id, count=remaining)
        )

    def _do_list(self) -> ToolResult:
        items = self._short_term.list_pinned()
        if not items:
            return ToolResult(tool_call_id="", content=tr("tool_result.pin.none"))
        lines = [tr("tool_result.pin.list_title", count=len(items))]
        for item in items:
            tokens = estimate_text_tokens(item.content)
            preview = item.content[:80].replace("\n", " ")
            if len(item.content) > 80:
                preview += "..."
            source = tr("tool_result.pin.source", path=item.file_path) if item.file_path else ""
            lines.append(
                tr(
                    "tool_result.pin.list_item",
                    id=item.pin_id,
                    label=item.label,
                    tokens=tokens,
                    time=item.created_at.strftime("%m-%d %H:%M"),
                    source=source,
                )
            )
            lines.append(f"  {preview}")
        total = sum(estimate_text_tokens(item.content) for item in items)
        lines.append(tr("tool_result.pin.total", tokens=total))
        return ToolResult(tool_call_id="", content="\n".join(lines))
