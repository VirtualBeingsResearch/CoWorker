from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import TYPE_CHECKING

import httpx

from coworker.core.config import Config
from coworker.core.types import IncomingEvent, ToolResult
from coworker.i18n import capture_locale, locale_context, tr
from coworker.tools.base import PAGE_CHAR_LIMIT, Tool, ToolDefinition, paginate_text
from coworker.web_search import SearchError, provider_choices, run_search

if TYPE_CHECKING:
    from coworker.agent.inbox_watcher import InboxWatcher
    from coworker.i18n import SupportedLocale

_WEB_TOOL_MAX_ATTEMPTS = 3
_WEB_TOOL_RETRY_DELAYS = (0.5, 1.0)

# 搜索/抓取在主线内最多等待这么久；超过就把工具调用交还给模型，
# 真实结果由后台任务完成后以入站事件送回主线，避免一次慢请求拖住整个回合。
_WEB_TOOL_INLINE_WAIT = 1.0
# 异步结果的入站来源：内容自带自描述前缀，供 incoming_content 原样透传。
_WEB_ASYNC_SOURCE = "web_async"
_REQUEST_LABEL_LIMIT = 200

# 持有后台投递任务的强引用，避免事件循环只保留弱引用导致任务被提前回收。
_PENDING_DELIVERIES: set[asyncio.Task] = set()


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


def _request_label(value: str) -> str:
    text = " ".join(value.split())
    if len(text) <= _REQUEST_LABEL_LIMIT:
        return text
    return text[:_REQUEST_LABEL_LIMIT] + "..."


async def _deliver_async_result(
    task: asyncio.Task[ToolResult],
    *,
    tool_name: str,
    request: str,
    inbox: InboxWatcher,
    locale: SupportedLocale,
) -> None:
    with locale_context(locale):
        try:
            result = await task
        except asyncio.CancelledError:
            raise
        except Exception as error:  # pragma: no cover - defensive: retries already normalize errors
            content = tr(
                "tool_result.web_async.failed",
                tool=tool_name,
                request=request,
                error=error,
            )
        else:
            content = (
                tr(
                    "tool_result.web_async.failed",
                    tool=tool_name,
                    request=request,
                    error=result.content,
                )
                if result.is_error
                else tr(
                    "tool_result.web_async.completed",
                    tool=tool_name,
                    request=request,
                    content=result.content,
                )
            )
        await inbox.push(
            IncomingEvent(
                participant_id=tool_name,
                content=content,
                timestamp=datetime.now(),
                source=_WEB_ASYNC_SOURCE,
            )
        )


async def _run_web_action(
    action: Callable[[], Awaitable[ToolResult]],
    *,
    tool_name: str,
    request: str,
    inbox: InboxWatcher | None,
) -> ToolResult:
    """Return a web result inline when it is ready within the wait budget.

    Without an inbound sink (bubbles, ad-hoc registries) there is nowhere to
    deliver a late result, so those callers keep the original blocking behavior.
    """

    if inbox is None:
        return await _execute_with_retries(action, tool_name=tool_name)

    task = asyncio.create_task(
        _execute_with_retries(action, tool_name=tool_name),
        name=f"{tool_name}-background",
    )
    try:
        done, _ = await asyncio.wait({task}, timeout=_WEB_TOOL_INLINE_WAIT)
    except asyncio.CancelledError:
        task.cancel()
        raise

    if task in done:
        return task.result()

    delivery = asyncio.create_task(
        _deliver_async_result(
            task,
            tool_name=tool_name,
            request=request,
            inbox=inbox,
            locale=capture_locale(),
        ),
        name=f"{tool_name}-delivery",
    )
    _PENDING_DELIVERIES.add(delivery)
    delivery.add_done_callback(_PENDING_DELIVERIES.discard)
    return ToolResult(
        tool_call_id="",
        content=tr(
            "tool_result.web_async.pending",
            tool=tool_name,
            seconds=f"{_WEB_TOOL_INLINE_WAIT:g}",
            request=request,
        ),
    )


class SearchWebTool(Tool):
    def __init__(
        self,
        config: Config | None = None,
        *,
        client: httpx.AsyncClient | None = None,
        inbox: InboxWatcher | None = None,
    ) -> None:
        self._config = config
        self._client = client
        self._inbox = inbox

    @property
    def definition(self) -> ToolDefinition:
        definition = ToolDefinition(
            name="search_web",
            description=(
                "搜索互联网并返回结果摘要。超过 1 秒仍未返回时转入后台，结果随后作为异步消息送达。"
            ),
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

    def fork(self, scope) -> SearchWebTool:
        return SearchWebTool(self._config, client=self._client, inbox=scope.inbox)

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

        return await _run_web_action(
            action,
            tool_name="search_web",
            request=_request_label(f"query={query}"),
            inbox=self._inbox,
        )


class FetchURLTool(Tool):
    def __init__(self, *, inbox: InboxWatcher | None = None) -> None:
        self._inbox = inbox

    @property
    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="fetch_url",
            description=(
                "获取指定 URL 的网页内容，支持多种输出格式。"
                f"默认每页最多返回 {PAGE_CHAR_LIMIT} 字符，超出部分用 offset 翻页。"
                "超过 1 秒仍未返回时转入后台，结果随后作为异步消息送达。"
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

    def fork(self, scope) -> FetchURLTool:
        return FetchURLTool(inbox=scope.inbox)

    async def execute(
        self, url: str, fmt: str = "text_markdown", offset: int = 0, limit: int | None = None, **_
    ) -> ToolResult:
        async def action() -> ToolResult:
            from ddgs import DDGS

            # ddgs 的 extract 是同步网络调用：放到线程里，避免慢网页冻结整个事件循环。
            result = await asyncio.to_thread(lambda: DDGS().extract(url, fmt=fmt))
            content = result.get("content", "") if result else ""
            if isinstance(content, bytes):
                content = content.decode(errors="replace")
            if content:
                return ToolResult(tool_call_id="", content=paginate_text(content, offset, limit))
            return ToolResult(tool_call_id="", content="No content extracted.", is_error=True)

        return await _run_web_action(
            action,
            tool_name="fetch_url",
            request=_request_label(f"url={url}"),
            inbox=self._inbox,
        )
