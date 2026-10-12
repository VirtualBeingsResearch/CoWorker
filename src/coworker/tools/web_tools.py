from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

import httpx

from coworker.core.config import Config
from coworker.core.types import ToolResult
from coworker.i18n import tr
from coworker.tools.base import PAGE_CHAR_LIMIT, Tool, ToolDefinition, paginate_text
from coworker.web_search import SearchError, provider_choices, run_search

_WEB_TOOL_MAX_ATTEMPTS = 3
_WEB_TOOL_RETRY_DELAYS = (0.5, 1.0)


async def _execute_with_retries(
    action: Callable[[], Awaitable[ToolResult]],
    *,
    tool_name: str,
    max_attempts: int = _WEB_TOOL_MAX_ATTEMPTS,
    retry_delays: tuple[float, ...] = _WEB_TOOL_RETRY_DELAYS,
) -> ToolResult:
    last_error: Exception | None = None

    for attempt in range(max_attempts):
        try:
            return await action()
        except SearchError as error:
            last_error = error
            if not error.retryable or attempt == max_attempts - 1:
                break
            await asyncio.sleep(retry_delays[min(attempt, len(retry_delays) - 1)])
        except Exception as e:
            last_error = e
            if attempt == max_attempts - 1:
                break
            await asyncio.sleep(retry_delays[min(attempt, len(retry_delays) - 1)])

    assert last_error is not None
    if isinstance(last_error, SearchError):
        content = str(last_error)
    else:
        content = tr(
            "tool_result.web_search.failed",
            tool=tool_name,
            attempts=max_attempts,
            error=last_error,
        )
    return ToolResult(tool_call_id="", content=content, is_error=True)


class SearchWebTool(Tool):
    def __init__(
        self,
        config: Config | None = None,
        *,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._config = config
        self._client = client

    @property
    def definition(self) -> ToolDefinition:
        definition = ToolDefinition(
            name="search_web",
            description="搜索互联网并返回结果摘要。",
            parameters={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "搜索关键词。"},
                    "max_results": {
                        "type": "integer",
                        "description": "最多返回的结果数，默认 5。",
                        "default": 5,
                    },
                    "freshness": {
                        "type": "string",
                        "description": (
                            "时间范围：noLimit（默认）、oneDay、oneWeek、oneMonth、oneYear，"
                            "或 2025-01-01..2025-02-01。部分后端会忽略。"
                        ),
                        "default": "noLimit",
                    },
                    "summary": {
                        "type": "boolean",
                        "description": "是否为每条结果附带更长摘要，默认 false。仅部分后端支持。",
                        "default": False,
                    },
                    "provider": {
                        "type": "string",
                        "description": "可选的搜索后端。仅在自动模式且配置了多个后端时生效。",
                    },
                },
                "required": ["query"],
            },
        )
        choices = provider_choices(self._config)
        properties = definition.parameters["properties"]
        if choices is None:
            properties.pop("provider", None)
        else:
            properties["provider"]["enum"] = choices
        return definition

    async def execute(
        self,
        query: str,
        max_results: int = 5,
        freshness: str = "noLimit",
        summary: bool = False,
        provider: str | None = None,
        **_: object,
    ) -> ToolResult:
        async def action() -> ToolResult:
            content = await run_search(
                self._config,
                query=query,
                max_results=max_results,
                freshness=freshness,
                summary=summary,
                provider=provider,
                client=self._client,
            )
            return ToolResult(tool_call_id="", content=content)

        return await _execute_with_retries(action, tool_name="search_web")


class FetchURLTool(Tool):
    @property
    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="fetch_url",
            description=(
                "获取指定 URL 的网页内容，支持多种输出格式。"
                f"默认每页最多返回 {PAGE_CHAR_LIMIT} 字符，超出部分用 offset 翻页。"
            ),
            parameters={
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "要获取的 URL"},
                    "fmt": {
                        "type": "string",
                        "enum": ["text_markdown", "text_plain", "text_rich", "text"],
                        "description": "输出格式：text_markdown（默认）、text_plain、text_rich、text（原始 HTML）",
                        "default": "text_markdown",
                    },
                    "offset": {
                        "type": "integer",
                        "description": "字符偏移量（从 0 开始），用于翻页读取长网页，默认 0",
                    },
                    "limit": {
                        "type": "integer",
                        "description": f"最多返回的字符数，默认每页 {PAGE_CHAR_LIMIT}",
                    },
                },
                "required": ["url"],
            },
        )

    async def execute(
        self, url: str, fmt: str = "text_markdown", offset: int = 0, limit: int | None = None, **_
    ) -> ToolResult:
        async def action() -> ToolResult:
            from ddgs import DDGS

            result = DDGS().extract(url, fmt=fmt)
            content = result.get("content", "") if result else ""
            if isinstance(content, bytes):
                content = content.decode(errors="replace")
            if content:
                return ToolResult(tool_call_id="", content=paginate_text(content, offset, limit))
            return ToolResult(tool_call_id="", content="No content extracted.", is_error=True)

        return await _execute_with_retries(action, tool_name="fetch_url")
