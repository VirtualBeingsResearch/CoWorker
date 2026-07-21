from __future__ import annotations

import asyncio
import base64
import secrets
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from coworker.core.types import ToolResult
from coworker.tools.base import Tool, ToolDefinition

if TYPE_CHECKING:
    from playwright.async_api import Browser, BrowserContext, Page, Playwright


@dataclass
class BrowserSession:
    session_id: str
    url: str
    created_at: float = field(default_factory=time.monotonic)
    screenshot_count: int = 0
    browser: Browser | None = None
    context: BrowserContext | None = None
    page: Page | None = None


class BrowserSessionStore:
    """Holds all active browser sessions and a single shared Playwright instance.

    One Playwright process serves all sessions. Call stop() at application
    shutdown to cleanly terminate it.
    """

    def __init__(self) -> None:
        self._sessions: dict[str, BrowserSession] = {}
        self._playwright: Playwright | None = None

    async def get_playwright(self) -> Playwright:
        if self._playwright is None:
            from playwright.async_api import async_playwright
            self._playwright = await async_playwright().start()
        return self._playwright

    async def stop(self) -> None:
        """Stop the shared Playwright instance. Should be called once at shutdown."""
        if self._playwright is not None:
            await self._playwright.stop()
            self._playwright = None

    def create(self, url: str) -> BrowserSession:
        session_id = secrets.token_hex(4)
        session = BrowserSession(session_id=session_id, url=url)
        self._sessions[session_id] = session
        return session

    def get(self, session_id: str) -> BrowserSession | None:
        return self._sessions.get(session_id)

    def remove(self, session_id: str) -> None:
        self._sessions.pop(session_id, None)

    def all(self) -> list[BrowserSession]:
        return list(self._sessions.values())


_SCREENSHOTS_DIR = Path("data/browser_screenshots")
DEFAULT_BROWSER_LOCALE = "zh-CN"


class BrowserOpenTool(Tool):
    def __init__(self, store: BrowserSessionStore) -> None:
        self._store = store

    @property
    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="browser_open",
            description="启动浏览器并导航到指定 URL，返回 session_id 供后续工具使用",
            parameters={
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "要打开的网页 URL"},
                    "headless": {
                        "type": "boolean",
                        "description": "是否无头模式（默认 true）",
                        "default": True,
                    },
                    "viewport_width": {
                        "type": "integer",
                        "description": "窗口宽度（像素，默认 1280）",
                    },
                    "viewport_height": {
                        "type": "integer",
                        "description": "窗口高度（像素，默认 720）",
                    },
                    "locale": {
                        "type": "string",
                        "description": "浏览器语言区域，如 zh-CN、en-US（默认 zh-CN）",
                        "default": DEFAULT_BROWSER_LOCALE,
                    },
                    "timezone_id": {
                        "type": "string",
                        "description": "时区 ID，如 Asia/Shanghai、America/New_York",
                    },
                    "user_agent": {
                        "type": "string",
                        "description": "自定义 User-Agent 字符串",
                    },
                    "ignore_https_errors": {
                        "type": "boolean",
                        "description": "是否忽略 HTTPS 证书错误（默认 false）",
                        "default": False,
                    },
                    "extra_http_headers": {
                        "type": "object",
                        "description": "附加到每个请求的 HTTP 头，如 {\"Authorization\": \"Bearer token\"}",
                    },
                    "cookies": {
                        "type": "array",
                        "description": "预设 Cookie 列表，每项需包含 name、value，以及 domain 或 url",
                        "items": {
                            "type": "object",
                            "properties": {
                                "name": {"type": "string"},
                                "value": {"type": "string"},
                                "domain": {"type": "string"},
                                "path": {"type": "string"},
                                "url": {"type": "string"},
                            },
                            "required": ["name", "value"],
                        },
                    },
                },
                "required": ["url"],
            },
        )

    async def execute(
        self,
        url: str,
        headless: bool = True,
        viewport_width: int = 1280,
        viewport_height: int = 720,
        locale: str | None = DEFAULT_BROWSER_LOCALE,
        timezone_id: str | None = None,
        user_agent: str | None = None,
        ignore_https_errors: bool = False,
        extra_http_headers: dict | None = None,
        cookies: list | None = None,
        **_,
    ) -> ToolResult:
        session = self._store.create(url)
        browser: Browser | None = None
        try:
            pw = await self._store.get_playwright()
            browser = await pw.chromium.launch(headless=headless)
            ctx_kwargs: dict = {
                "viewport": {"width": viewport_width, "height": viewport_height},
                "ignore_https_errors": ignore_https_errors,
            }
            ctx_kwargs["locale"] = locale or DEFAULT_BROWSER_LOCALE
            if timezone_id:
                ctx_kwargs["timezone_id"] = timezone_id
            if user_agent:
                ctx_kwargs["user_agent"] = user_agent
            if extra_http_headers:
                ctx_kwargs["extra_http_headers"] = extra_http_headers
            context = await browser.new_context(**ctx_kwargs)
            if cookies:
                await context.add_cookies(cookies)
            page = await context.new_page()
            await page.goto(url, wait_until="domcontentloaded", timeout=30000)
            session.browser = browser
            session.context = context
            session.page = page
            session.url = page.url
            return ToolResult(
                tool_call_id="",
                content=f"session_id={session.session_id}\nurl={session.url}\ntitle={await page.title()}",
            )
        except Exception as e:
            if browser:
                await browser.close()
            self._store.remove(session.session_id)
            return ToolResult(tool_call_id="", content=str(e), is_error=True)


class BrowserScreenshotTool(Tool):
    def __init__(self, store: BrowserSessionStore) -> None:
        self._store = store

    @property
    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="browser_screenshot",
            description="对当前浏览器页面截图，保存为 PNG 文件并返回文件路径",
            parameters={
                "type": "object",
                "properties": {
                    "session_id": {"type": "string", "description": "browser_open 返回的 session_id"},
                    "full_page": {
                        "type": "boolean",
                        "description": "是否截取整页（默认 false，仅可视区域）",
                        "default": False,
                    },
                },
                "required": ["session_id"],
            },
        )

    async def execute(self, session_id: str, full_page: bool = False, **_) -> ToolResult:
        session = self._store.get(session_id)
        if not session or not session.page:
            return ToolResult(tool_call_id="", content=f"Session {session_id} not found", is_error=True)
        try:
            _SCREENSHOTS_DIR.mkdir(parents=True, exist_ok=True)
            session.screenshot_count += 1
            path = _SCREENSHOTS_DIR / f"{session_id}_{session.screenshot_count}.png"
            await session.page.screenshot(path=str(path), full_page=full_page)
            return ToolResult(tool_call_id="", content=f"截图已保存：{path.resolve()}")
        except Exception as e:
            return ToolResult(tool_call_id="", content=str(e), is_error=True)


class BrowserActionTool(Tool):
    def __init__(self, store: BrowserSessionStore) -> None:
        self._store = store

    @property
    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="browser_action",
            description=(
                "在浏览器页面执行交互操作。\n"
                "action 支持：click（点击）、type（输入文本）、select（下拉选择）、"
                "hover（悬停）、press_key（按键）、navigate（跳转 URL）。\n"
                "selector 支持 CSS 选择器或 Playwright 定位器（如 text=提交、role=button[name=登录]）。"
            ),
            parameters={
                "type": "object",
                "properties": {
                    "session_id": {
                        "type": "string",
                        "description": "browser_open 返回的 session_id",
                    },
                    "action": {
                        "type": "string",
                        "enum": [
                            "click",
                            "type",
                            "select",
                            "hover",
                            "press_key",
                            "navigate",
                            "evaluate",
                        ],
                        "description": "要执行的操作类型",
                    },
                    "selector": {
                        "type": "string",
                        "description": "目标元素选择器（navigate 操作时填写 URL）",
                    },
                    "value": {
                        "type": "string",
                        "description": "type 时填写输入文本，select 时填写选项值，press_key 时填写按键名（如 Enter、Tab）",
                    },
                    "script": {
                        "type": "string",
                        "description": "evaluate时需要执行的代码，不能执行耗时太长的代码",
                    },
                },
                "required": ["session_id", "action", "selector"],
            },
        )

    async def execute(self, session_id: str, action: str, selector: str = "", value: str = "", script: str = "", **_) -> ToolResult:
        session = self._store.get(session_id)
        if not session or not session.page:
            return ToolResult(tool_call_id="", content=f"Session {session_id} not found", is_error=True)
        page = session.page
        try:
            if action == "navigate":
                await page.goto(selector, wait_until="domcontentloaded", timeout=30000)
                session.url = page.url
                result = f"已导航到 {page.url}"
            elif action == "click":
                await page.locator(selector).click(timeout=10000)
                result = f"已点击 {selector}"
            elif action == "type":
                await page.locator(selector).fill(value, timeout=10000)
                result = f"已在 {selector} 输入文本"
            elif action == "select":
                await page.locator(selector).select_option(value, timeout=10000)
                result = f"已选择 {selector} 的选项 {value}"
            elif action == "hover":
                await page.locator(selector).hover(timeout=10000)
                result = f"已悬停在 {selector}"
            elif action == "press_key":
                await page.locator(selector).press(value, timeout=10000)
                result = f"已在 {selector} 按下 {value}"
            elif action == "evaluate":
                try:
                    result = await asyncio.wait_for(page.evaluate(script), timeout=10)
                except TimeoutError:
                    return ToolResult(tool_call_id="", content="脚本执行超时", is_error=True)
                return ToolResult(tool_call_id="", content=str(result))
            else:
                return ToolResult(tool_call_id="", content=f"未知 action: {action}", is_error=True)
            return ToolResult(tool_call_id="", content=result)
        except Exception as e:
            return ToolResult(tool_call_id="", content=str(e), is_error=True)


class BrowserGetContentTool(Tool):
    def __init__(self, store: BrowserSessionStore) -> None:
        self._store = store

    _MAX_CHARS = 3000

    @property
    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="browser_get_content",
            description=(
                "获取当前浏览器页面的内容（文本或 HTML）。"
                f"每次最多返回 {self._MAX_CHARS} 字符；若内容被截断，返回末尾会注明总长度，"
                "可通过 start 参数分页获取后续内容。"
            ),
            parameters={
                "type": "object",
                "properties": {
                    "session_id": {"type": "string", "description": "browser_open 返回的 session_id"},
                    "fmt": {
                        "type": "string",
                        "enum": ["text", "html"],
                        "description": "输出格式：text（默认，提取可见文本）或 html（原始 HTML）",
                        "default": "text",
                    },
                    "selector": {
                        "type": "string",
                        "description": "可选，限定提取范围的 CSS 选择器（不填则提取整页）",
                    },
                    "start": {
                        "type": "integer",
                        "description": "分页起始字符偏移量（默认 0）",
                        "default": 0,
                    },
                },
                "required": ["session_id"],
            },
        )

    async def execute(self, session_id: str, fmt: str = "text", selector: str = "", start: int = 0, **_) -> ToolResult:
        session = self._store.get(session_id)
        if not session or not session.page:
            return ToolResult(tool_call_id="", content=f"Session {session_id} not found", is_error=True)
        page = session.page
        try:
            if selector:
                locator = page.locator(selector)
                content = await locator.inner_text() if fmt == "text" else await locator.inner_html()
            else:
                content = await page.inner_text("body") if fmt == "text" else await page.content()
            total = len(content)
            chunk = content[start : start + self._MAX_CHARS]
            end = start + len(chunk)
            if end < total:
                chunk += f"\n\n[内容已截断：共 {total} 字符，当前显示第 {start}~{end} 字符。如需继续，使用 start={end}]"
            return ToolResult(tool_call_id="", content=chunk)
        except Exception as e:
            return ToolResult(tool_call_id="", content=str(e), is_error=True)


class BrowserCloseTool(Tool):
    def __init__(self, store: BrowserSessionStore) -> None:
        self._store = store

    @property
    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="browser_close",
            description=(
                "关闭指定浏览器 session，并返回其中所有页面的标题和链接"
                "（共享的 Playwright 进程保持运行）"
            ),
            parameters={
                "type": "object",
                "properties": {
                    "session_id": {"type": "string", "description": "要关闭的 session_id"},
                },
                "required": ["session_id"],
            },
        )

    async def execute(self, session_id: str, **_) -> ToolResult:
        session = self._store.get(session_id)
        if not session:
            return ToolResult(tool_call_id="", content=f"Session {session_id} not found", is_error=True)
        try:
            pages = []
            for page in session.context.pages if session.context else []:
                pages.append((await page.title(), page.url))
            if session.browser:
                await session.browser.close()
            self._store.remove(session_id)
            page_summary = "\n".join(
                f"{index}. title={title}\n   url={url}"
                for index, (title, url) in enumerate(pages, 1)
            )
            content = f"Session {session_id} 已关闭"
            if page_summary:
                content += f"\n会话页面：\n{page_summary}"
            return ToolResult(tool_call_id="", content=content)
        except Exception as e:
            return ToolResult(tool_call_id="", content=str(e), is_error=True)


class BrowserViewTool(Tool):
    """截图并将图片直接返回给视觉模型查看。"""

    vision_model_only = True

    def __init__(self, store: BrowserSessionStore, max_dimension: int = 960) -> None:
        self._store = store
        self._max_dimension = max_dimension

    @property
    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="browser_view",
            description="对当前浏览器页面截图，直接返回图片供视觉模型查看",
            parameters={
                "type": "object",
                "properties": {
                    "session_id": {"type": "string", "description": "browser_open 返回的 session_id"},
                    "full_page": {
                        "type": "boolean",
                        "description": "是否截取整页（默认 false，仅可视区域）",
                        "default": False,
                    },
                    "full_resolution": {
                        "type": "boolean",
                        "description": "是否使用原始分辨率，不进行缩放（默认 false）",
                        "default": False,
                    },
                },
                "required": ["session_id"],
            },
        )

    async def execute(self, session_id: str, full_page: bool = False, full_resolution: bool = False, **_) -> ToolResult:
        session = self._store.get(session_id)
        if not session or not session.page:
            return ToolResult(tool_call_id="", content=f"Session {session_id} not found", is_error=True)
        try:
            from coworker.tools.vision_tools import _resize_image
            raw = await session.page.screenshot(full_page=full_page)
            if full_resolution:
                resize_note = ""
            else:
                raw, _media_type, resize_note = _resize_image(raw, "image/png", self._max_dimension)
            note = f"（{resize_note}）" if resize_note else ""
            desc = f"浏览器截图 session={session_id} url={session.url}{note}"
            content_blocks: list[dict[str, Any]] = [
                {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": base64.standard_b64encode(raw).decode()}},
                {"type": "text", "text": desc},
            ]
            return ToolResult(tool_call_id="", content=desc, content_blocks=content_blocks)
        except Exception as e:
            return ToolResult(tool_call_id="", content=str(e), is_error=True)


class BrowserListSessionsTool(Tool):
    def __init__(self, store: BrowserSessionStore) -> None:
        self._store = store

    @property
    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="browser_list_sessions",
            description="列出当前所有活跃的浏览器 session 及其 URL",
            parameters={"type": "object", "properties": {}},
        )

    async def execute(self, **_) -> ToolResult:
        sessions = self._store.all()
        if not sessions:
            return ToolResult(tool_call_id="", content="当前没有活跃的浏览器 session")
        lines = [f"session_id={s.session_id}  url={s.url}" for s in sessions]
        return ToolResult(tool_call_id="", content="\n".join(lines))
