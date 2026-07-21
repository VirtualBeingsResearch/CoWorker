from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from coworker.core.types import ToolResult
from coworker.i18n import SupportedLocale, get_locale, tr
from coworker.i18n.runtime import catalog

if TYPE_CHECKING:
    from coworker.core.tool_scope import ToolScope

PAGE_CHAR_LIMIT = 3_000  # 工具单次返回的默认页大小（字符）
PAGE_CHAR_MAX = 10_000  # 工具单次返回的硬上限（字符）：即便显式要求更多也不超过，防止上下文被冲爆


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
        notice = tr(
            "pagination.range",
            start=offset,
            end=next_offset - 1,
            total=total,
        )
        if remaining > 0:
            notice += tr("pagination.remaining", remaining=remaining)
        else:
            notice += tr("pagination.end")
        notice += "]"
    else:
        notice = tr("pagination.out_of_range", offset=offset, total=total)
    if remaining > 0:
        hint = (
            next_hint.format(
                offset=next_offset,
                total=total,
                remaining=remaining,
            )
            if next_hint
            else tr("pagination.next", offset=next_offset)
        )
        notice += f"  {hint}"
    return notice + "\n" + chunk


@dataclass
class ToolDefinition:
    name: str
    description: str
    parameters: dict[str, Any]

    def to_schema(self) -> dict[str, Any]:
        entries = catalog()

        def localized_description(original: str, path: tuple[str, ...]) -> str:
            if get_locale() is SupportedLocale.ZH_CN:
                return original
            if not path:
                return entries.get(
                    f"tool_schema.description.{self.name}",
                    tr("tool_schema.fallback.tool", name=self.name),
                )
            parameter = ".".join(path)
            return entries.get(
                f"tool_schema.parameter.{self.name}.{parameter}",
                tr(
                    "tool_schema.fallback.parameter",
                    parameter=parameter,
                    tool=self.name,
                ),
            )

        def localize(value: Any, path: tuple[str, ...] = ()) -> Any:
            if isinstance(value, list):
                return [localize(item, path) for item in value]
            if not isinstance(value, dict):
                return value
            localized: dict[str, Any] = {}
            for key, item in value.items():
                if key == "description" and isinstance(item, str):
                    localized[key] = localized_description(item, path)
                elif key == "properties" and isinstance(item, dict):
                    localized[key] = {
                        name: localize(child, (*path, name)) for name, child in item.items()
                    }
                elif key == "items":
                    localized[key] = localize(item, (*path, "item"))
                else:
                    localized[key] = localize(item, path)
            return localized

        return {
            "name": self.name,
            "description": localized_description(self.description, ()),
            "parameters": localize(self.parameters),
        }


class Tool(ABC):
    text_model_only: bool = False
    vision_model_only: bool = False

    @property
    @abstractmethod
    def definition(self) -> ToolDefinition: ...

    @abstractmethod
    async def execute(self, **kwargs: Any) -> ToolResult: ...

    def fork(self, scope: ToolScope) -> Tool:
        """Return a variant of this tool wired to the given scope.

        Override in subclasses that hold scope-sensitive resources (stores,
        inboxes).  The default returns self — safe for stateless tools.
        """
        return self
