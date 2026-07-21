from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from coworker.core.types import ToolResult

if TYPE_CHECKING:
    from coworker.core.tool_scope import ToolScope

PAGE_CHAR_LIMIT = 3_000   # 工具单次返回的默认页大小（字符）
PAGE_CHAR_MAX = 10_000    # 工具单次返回的硬上限（字符）：即便显式要求更多也不超过，防止上下文被冲爆


def paginate_text(
    text: str,
    offset: int = 0,
    limit: int | None = None,
    *,
    next_hint: str | None = None,
) -> str:
    """对长文本做字符级分页，供各工具复用，保证全项目一套阈值与行为。

    返回 ``"<分页提示行>\\n<chunk>"``；当从头读且没有剩余（无需分页）时只返回 chunk。

    limit 语义：未传走默认页 ``PAGE_CHAR_LIMIT``；显式传 0 表示尽量多取一页；
    任何情况下单次返回都不超过硬上限 ``PAGE_CHAR_MAX``。更多内容用 offset 继续翻页。
    若提供 ``next_hint``，会在仍有剩余内容时作为续页提示模板使用，可引用
    ``{offset}`` / ``{total}`` / ``{remaining}``。
    """
    total = len(text)
    if limit is None:
        page = PAGE_CHAR_LIMIT
    elif limit <= 0:
        page = PAGE_CHAR_MAX
    else:
        page = min(limit, PAGE_CHAR_MAX)

    chunk = text[offset : offset + page]
    next_offset = offset + len(chunk)
    remaining = total - next_offset

    if offset <= 0 and remaining <= 0:
        return chunk

    if chunk:
        notice = f"[字符 {offset}–{next_offset - 1} / 共 {total}"
        if remaining > 0:
            notice += f"；剩余 {remaining}"
        else:
            notice += "；已到末尾"
        notice += "]"
    else:
        notice = f"[offset={offset} 超出范围 / 共 {total} 字符]"
    if remaining > 0:
        hint = next_hint.format(
            offset=next_offset,
            total=total,
            remaining=remaining,
        ) if next_hint else f"如需后续内容，请在下次调用时传 offset={next_offset}"
        notice += f"  {hint}"
    return notice + "\n" + chunk


@dataclass
class ToolDefinition:
    name: str
    description: str
    parameters: dict[str, Any]

    def to_schema(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "parameters": self.parameters,
        }


class Tool(ABC):
    text_model_only: bool = False
    vision_model_only: bool = False

    @property
    @abstractmethod
    def definition(self) -> ToolDefinition:
        ...

    @abstractmethod
    async def execute(self, **kwargs: Any) -> ToolResult:
        ...

    def fork(self, scope: ToolScope) -> Tool:
        """Return a variant of this tool wired to the given scope.

        Override in subclasses that hold scope-sensitive resources (stores,
        inboxes).  The default returns self — safe for stateless tools.
        """
        return self
