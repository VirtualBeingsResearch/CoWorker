"""Multi-provider web search behind one result shape.

Provider order and request contracts follow the backends Coworker exposes
for ``search_web``: Bocha, Qianfan, Zhipu, LinkAI, AnySearch, Serply,
Tavily, SearXNG, Keenable, and keyless DDGS.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any
from urllib.parse import urlencode

import httpx
from loguru import logger

from coworker.core.config import (
    WEB_SEARCH_PROVIDER_ORDER,
    Config,
    LLMConfig,
    WebSearchConfig,
    WebSearchProviderName,
)
from coworker.i18n import tr

_FRESHNESS_DAYS = {"oneDay": 1, "oneWeek": 7, "oneMonth": 30, "oneYear": 365}
_NAMED_FRESHNESS = frozenset((*_FRESHNESS_DAYS, "noLimit"))
_LABELS: dict[str, str] = {
    "ddgs": "DDGS",
    "bocha": "Bocha",
    "zhipu": "Zhipu",
    "qianfan": "Qianfan",
    "linkai": "LinkAI",
    "anysearch": "AnySearch",
    "serply": "Serply",
    "tavily": "Tavily",
    "searxng": "SearXNG",
    "keenable": "Keenable",
}
_DEFAULT_ZHIPU_BASE = "https://open.bigmodel.cn/api/paas/v4"
_DEFAULT_QIANFAN_BASE = "https://qianfan.baidubce.com/v2"
_DEFAULT_LINKAI_BASE = "https://api.link-ai.tech"


class SearchError(Exception):
    def __init__(self, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.retryable = retryable


@dataclass(frozen=True)
class SearchHit:
    title: str
    url: str
    snippet: str
    site_name: str = ""
    published: str = ""
    summary: str = ""


def configured_providers(config: Config) -> list[WebSearchProviderName]:
    return [name for name in WEB_SEARCH_PROVIDER_ORDER if _is_configured(config, name)]


def provider_choices(config: Config | None) -> list[str] | None:
    """Backends the model may pick. Hidden unless auto mode has a real choice."""

    if config is None or config.web_search.strategy != "auto":
        return None
    available = configured_providers(config)
    if len(available) < 2:
        return None
    return list(available)


def resolve_provider(config: Config, requested: str | None) -> tuple[str, str]:
    available = configured_providers(config)
    if requested:
        name = requested.strip().lower()
        if name in available:
            return name, "caller-requested"
        logger.warning("search_web requested provider {} is unavailable", requested)

    settings = config.web_search
    if settings.strategy == "fixed":
        if settings.provider in available:
            return settings.provider, "fixed-strategy"
        logger.warning(
            "search_web pinned provider {} is unavailable; falling back",
            settings.provider,
        )
    return available[0], "auto-fallback"


async def run_search(
    config: Config | None,
    *,
    query: str,
    max_results: object = 5,
    freshness: object = "noLimit",
    summary: object = False,
    provider: str | None = None,
    client: httpx.AsyncClient | None = None,
) -> str:
    text = query.strip()
    if not text:
        raise SearchError(tr("tool_result.web_search.query_required"))

    bound = config or _default_config()
    count = _count(max_results)
    window = _freshness(freshness)
    want_summary = summary is True
    chosen, reason = resolve_provider(bound, provider)
    preview = text if len(text) <= 60 else text[:57] + "..."
    logger.info(
        "search_web provider={} reason={} query={!r} count={} freshness={}",
        chosen,
        reason,
        preview,
        count,
        window,
    )
    hits = await _dispatch(
        bound,
        chosen,
        query=text,
        count=count,
        freshness=window,
        summary=want_summary,
        client=client,
    )
    return format_hits(chosen, hits)


def format_hits(provider: str, hits: list[SearchHit]) -> str:
    if not hits:
        return tr("tool_result.web_search.empty")
    blocks = [tr("tool_result.web_search.header", provider=_LABELS.get(provider, provider))]
    for index, hit in enumerate(hits, start=1):
        lines = [f"[{index}] {hit.title}".rstrip()]
        if hit.url:
            lines.append(hit.url)
        if hit.snippet:
            lines.append(hit.snippet)
        meta = " · ".join(part for part in (hit.site_name, hit.published) if part)
        if meta:
            lines.append(meta)
        if hit.summary and hit.summary != hit.snippet:
            lines.append(hit.summary)
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks)


def _default_config() -> Config:
    return Config.model_construct(
        web_search=WebSearchConfig.model_construct(),
        llm=LLMConfig.model_construct(),
    )


def _is_configured(config: Config, name: WebSearchProviderName) -> bool:
    settings = config.web_search
    if name == "ddgs":
        return True
    if name == "bocha":
        return bool(settings.bocha_api_key)
    if name == "zhipu":
        return bool(settings.zhipu_api_key or config.llm.zhipu_api_key)
    if name == "qianfan":
        return bool(settings.qianfan_api_key)
    if name == "linkai":
        return bool(settings.linkai_api_key)
    if name == "anysearch":
        return bool(settings.anysearch_api_key or settings.anysearch_anonymous)
    if name == "serply":
        return bool(settings.serply_api_key)
    if name == "tavily":
        return bool(settings.tavily_api_key)
    if name == "searxng":
        return bool(settings.searxng_url)
    if name == "keenable":
        return bool(settings.keenable_api_key or settings.keenable_anonymous)
    return False


def _zhipu_key(config: Config) -> str:
    return config.web_search.zhipu_api_key or config.llm.zhipu_api_key


def _count(value: object, *, default: int = 5, upper: int = 50) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        return default
    if value < 1:
        return default
    return min(value, upper)


def _freshness(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        return "noLimit"
    return value.strip()


def _label(provider: str) -> str:
    return _LABELS.get(provider, provider)


async def _dispatch(
    config: Config,
    provider: str,
    *,
    query: str,
    count: int,
    freshness: str,
    summary: bool,
    client: httpx.AsyncClient | None,
) -> list[SearchHit]:
    if provider == "ddgs":
        return await _search_ddgs(config, query, count)
    if client is None:
        async with httpx.AsyncClient() as owned:
            return await _dispatch_http(
                owned,
                config,
                provider,
                query=query,
                count=count,
                freshness=freshness,
                summary=summary,
            )
    return await _dispatch_http(
        client, config, provider, query=query, count=count, freshness=freshness, summary=summary
    )


async def _dispatch_http(
    client: httpx.AsyncClient,
    config: Config,
    provider: str,
    *,
    query: str,
    count: int,
    freshness: str,
    summary: bool,
) -> list[SearchHit]:
    settings = config.web_search
    if provider == "bocha":
        return await _search_bocha(client, settings, query, count, freshness, summary)
    if provider == "zhipu":
        return await _search_zhipu(client, settings, _zhipu_key(config), query, count, freshness)
    if provider == "qianfan":
        return await _search_qianfan(client, settings, query, count, freshness)
    if provider == "linkai":
        return await _search_linkai(client, settings, query, count, freshness)
    if provider == "anysearch":
        return await _search_anysearch(client, settings, query, count, freshness, summary)
    if provider == "serply":
        return await _search_serply(client, settings, query, count)
    if provider == "tavily":
        return await _search_tavily(client, settings, query, count)
    if provider == "searxng":
        return await _search_searxng(client, settings, query, count)
    if provider == "keenable":
        return await _search_keenable(client, settings, query, count, freshness, summary)
    raise SearchError(tr("tool_result.web_search.unknown", provider=provider))


async def _search_ddgs(config: Config, query: str, count: int) -> list[SearchHit]:
    from ddgs import DDGS

    backend = config.web_search.ddgs_backend
    timeout = config.web_search.timeout_seconds

    def call() -> list[dict[str, Any]]:
        rows = DDGS(timeout=timeout).text(query, max_results=count, backend=backend)
        return list(rows or [])

    rows = await asyncio.to_thread(call)
    return [
        SearchHit(
            title=_text(row.get("title")),
            url=_text(row.get("href")),
            snippet=_text(row.get("body")),
        )
        for row in rows
        if isinstance(row, dict)
    ]


async def _search_bocha(
    client: httpx.AsyncClient,
    settings: WebSearchConfig,
    query: str,
    count: int,
    freshness: str,
    summary: bool,
) -> list[SearchHit]:
    response = await _request(
        client,
        "POST",
        "https://api.bochaai.com/v1/web-search",
        provider="bocha",
        timeout=settings.timeout_seconds,
        headers=_bearer(settings.bocha_api_key),
        payload={"query": query, "count": count, "freshness": freshness, "summary": summary},
    )
    if response.status_code == 403:
        raise SearchError(tr("tool_result.web_search.bocha_balance"))
    _raise_for_status(response, "bocha")
    data = _json_object(response, "bocha")
    api_code = data.get("code")
    if api_code is not None and api_code != 200:
        raise SearchError(
            tr(
                "tool_result.web_search.business",
                provider=_label("bocha"),
                message=_text(data.get("msg")) or "unknown",
            )
        )
    pages = _dig(data, "data", "webPages", "value")
    hits = [_bocha_hit(page) for page in pages if isinstance(page, dict)]
    return hits


def _bocha_hit(page: Mapping[str, Any]) -> SearchHit:
    return SearchHit(
        title=_text(page.get("name")),
        url=_text(page.get("url")),
        snippet=_text(page.get("snippet")),
        site_name=_text(page.get("siteName")),
        published=_text(page.get("datePublished") or page.get("dateLastCrawled")),
        summary=_text(page.get("summary")),
    )


async def _search_zhipu(
    client: httpx.AsyncClient,
    settings: WebSearchConfig,
    api_key: str,
    query: str,
    count: int,
    freshness: str,
) -> list[SearchHit]:
    base = settings.zhipu_api_base or _DEFAULT_ZHIPU_BASE
    payload: dict[str, Any] = {
        "search_engine": settings.zhipu_search_engine,
        "search_query": query[:70],
        "search_intent": False,
        "count": count,
        "search_recency_filter": freshness if freshness in _NAMED_FRESHNESS else "noLimit",
    }
    if settings.zhipu_content_size:
        payload["content_size"] = settings.zhipu_content_size
    response = await _request(
        client,
        "POST",
        f"{base}/web_search",
        provider="zhipu",
        timeout=settings.timeout_seconds,
        headers=_bearer(api_key),
        payload=payload,
    )
    _raise_for_status(response, "zhipu")
    data = _json_object(response, "zhipu")
    error = data.get("error")
    if isinstance(error, dict):
        message = _text(error.get("message")) or _text(error.get("code")) or "unknown"
        raise SearchError(
            tr("tool_result.web_search.business", provider=_label("zhipu"), message=message)
        )
    items = data.get("search_result")
    if not isinstance(items, list):
        items = _dig(data, "data", "search_result")
    hits: list[SearchHit] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        hits.append(
            SearchHit(
                title=_text(item.get("title")),
                url=_text(item.get("link") or item.get("url")),
                snippet=_text(item.get("content") or item.get("snippet")),
                site_name=_text(item.get("media") or item.get("siteName")),
                published=_text(item.get("publish_date") or item.get("datePublished")),
            )
        )
    return hits


async def _search_qianfan(
    client: httpx.AsyncClient,
    settings: WebSearchConfig,
    query: str,
    count: int,
    freshness: str,
) -> list[SearchHit]:
    base = settings.qianfan_api_base or _DEFAULT_QIANFAN_BASE
    payload: dict[str, Any] = {
        "messages": [{"role": "user", "content": query}],
        "search_source": "baidu_search_v2",
        "resource_type_filter": [{"type": "web", "top_k": count}],
    }
    window = _qianfan_freshness(freshness)
    if window:
        payload["search_filter"] = window
    headers = _bearer(settings.qianfan_api_key)
    headers["X-Appbuilder-From"] = "coworker"
    response = await _request(
        client,
        "POST",
        f"{base}/ai_search/web_search",
        provider="qianfan",
        timeout=settings.timeout_seconds,
        headers=headers,
        payload=payload,
    )
    _raise_for_status(response, "qianfan")
    data = _json_object(response, "qianfan")
    if data.get("code"):
        raise SearchError(
            tr(
                "tool_result.web_search.business",
                provider=_label("qianfan"),
                message=_text(data.get("message")) or _text(data.get("code")),
            )
        )
    refs = data.get("references")
    hits: list[SearchHit] = []
    if isinstance(refs, list):
        for item in refs:
            if not isinstance(item, dict):
                continue
            content = _text(item.get("content"))
            hits.append(
                SearchHit(
                    title=_text(item.get("title")),
                    url=_text(item.get("url")),
                    snippet=content[:200],
                    site_name=_text(item.get("web_anchor") or item.get("website")),
                    published=_text(item.get("date")),
                )
            )
    return hits


def _qianfan_freshness(freshness: str) -> dict[str, Any] | None:
    days = _FRESHNESS_DAYS.get(freshness)
    if days is None:
        return None
    now = datetime.now()
    return {
        "range": {
            "page_time": {
                "gte": (now - timedelta(days=days)).strftime("%Y-%m-%d"),
                "lt": (now + timedelta(days=1)).strftime("%Y-%m-%d"),
            }
        }
    }


async def _search_linkai(
    client: httpx.AsyncClient,
    settings: WebSearchConfig,
    query: str,
    count: int,
    freshness: str,
) -> list[SearchHit]:
    base = settings.linkai_api_base or _DEFAULT_LINKAI_BASE
    response = await _request(
        client,
        "POST",
        f"{base}/v1/plugin/execute",
        provider="linkai",
        timeout=settings.timeout_seconds,
        headers=_bearer(settings.linkai_api_key),
        payload={
            "code": "web-search",
            "args": {"query": query, "count": count, "freshness": freshness},
        },
    )
    _raise_for_status(response, "linkai")
    data = _json_object(response, "linkai")
    if not data.get("success"):
        raise SearchError(
            tr(
                "tool_result.web_search.business",
                provider=_label("linkai"),
                message=_text(data.get("message")) or "unknown",
            )
        )
    raw = data.get("data", "")
    if isinstance(raw, str):
        try:
            import json

            raw = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return [SearchHit(title="", url="", snippet=raw)]
    if isinstance(raw, dict):
        pages = _dig(raw, "webPages", "value")
        hits = [_bocha_hit(page) for page in pages if isinstance(page, dict)]
        if hits:
            return hits
    return [SearchHit(title="", url="", snippet=str(raw))]


async def _search_anysearch(
    client: httpx.AsyncClient,
    settings: WebSearchConfig,
    query: str,
    count: int,
    freshness: str,
    summary: bool,
) -> list[SearchHit]:
    if freshness != "noLimit":
        logger.warning("anysearch ignores freshness={!r}", freshness)
    if summary:
        logger.warning("anysearch ignores summary")
    max_results = max(1, min(count, 10))
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    if settings.anysearch_api_key:
        headers["Authorization"] = f"Bearer {settings.anysearch_api_key}"
    payload: dict[str, Any] = {"query": query, "max_results": max_results, "format": "json"}
    if settings.anysearch_zone:
        payload["zone"] = settings.anysearch_zone
    if settings.anysearch_language:
        payload["language"] = settings.anysearch_language
    response = await _request(
        client,
        "POST",
        "https://api.anysearch.com/v1/search",
        provider="anysearch",
        timeout=settings.timeout_seconds,
        headers=headers,
        payload=payload,
    )
    request_id = _request_id(response)
    if response.status_code == 401:
        key = "invalid_key" if settings.anysearch_api_key else "anysearch_anonymous_auth"
        raise _with_request_id(
            tr(f"tool_result.web_search.{key}", provider=_label("anysearch")),
            request_id,
        )
    if response.status_code == 402:
        key = "quota" if settings.anysearch_api_key else "anysearch_anonymous_quota"
        raise _with_request_id(
            tr(f"tool_result.web_search.{key}", provider=_label("anysearch")),
            request_id,
        )
    if response.status_code == 429:
        raise _with_request_id(
            tr("tool_result.web_search.rate_limit", provider=_label("anysearch")),
            request_id,
        )
    if response.status_code != 200:
        raise _with_request_id(
            tr(
                "tool_result.web_search.http",
                provider=_label("anysearch"),
                status=response.status_code,
            ),
            request_id,
        )
    try:
        data = response.json()
    except ValueError as exc:
        raise _with_request_id(
            tr("tool_result.web_search.malformed", provider=_label("anysearch")),
            request_id,
        ) from exc
    if not isinstance(data, dict):
        raise _with_request_id(
            tr("tool_result.web_search.malformed", provider=_label("anysearch")),
            request_id,
        )
    request_id = request_id or _clean_id(data.get("request_id"))
    api_code = data.get("code")
    if isinstance(api_code, bool) or not isinstance(api_code, int):
        raise _with_request_id(
            tr("tool_result.web_search.malformed", provider=_label("anysearch")),
            request_id,
        )
    if api_code != 0:
        raise _with_request_id(
            tr(
                "tool_result.web_search.business",
                provider=_label("anysearch"),
                message=_text(data.get("message")) or "unknown",
            ),
            request_id,
        )
    body = data.get("data")
    if not isinstance(body, dict) or not isinstance(body.get("results"), list):
        raise _with_request_id(
            tr("tool_result.web_search.malformed", provider=_label("anysearch")),
            request_id,
        )
    raw_results = body["results"]
    hits: list[SearchHit] = []
    for item in raw_results:
        hit = _anysearch_hit(item)
        if hit is not None:
            hits.append(hit)
    if raw_results and not hits:
        raise _with_request_id(
            tr("tool_result.web_search.no_usable", provider=_label("anysearch")),
            request_id,
        )
    return hits[:max_results]


def _anysearch_hit(item: object) -> SearchHit | None:
    if not isinstance(item, dict):
        return None
    url = item.get("url")
    if not isinstance(url, str) or not url.strip():
        return None
    title = item.get("title")
    snippet = item.get("snippet")
    if not isinstance(snippet, str) or not snippet:
        content = item.get("content")
        snippet = content[:200] if isinstance(content, str) else ""
    return SearchHit(
        title=title if isinstance(title, str) else "",
        url=url,
        snippet=snippet,
    )


async def _search_serply(
    client: httpx.AsyncClient,
    settings: WebSearchConfig,
    query: str,
    count: int,
) -> list[SearchHit]:
    path = urlencode({"q": query, "num": count})
    response = await _request(
        client,
        "GET",
        f"https://api.serply.io/v1/search/{path}",
        provider="serply",
        timeout=settings.timeout_seconds,
        headers={
            "X-Api-Key": settings.serply_api_key,
            "Accept": "application/json",
            "User-Agent": "Coworker",
        },
    )
    _raise_for_status(response, "serply")
    data = _json_object(response, "serply")
    items = data.get("results")
    hits: list[SearchHit] = []
    if isinstance(items, list):
        for item in items:
            if not isinstance(item, dict):
                continue
            hits.append(
                SearchHit(
                    title=_text(item.get("title")),
                    url=_text(item.get("link")),
                    snippet=_text(item.get("description")),
                )
            )
    return hits


async def _search_tavily(
    client: httpx.AsyncClient,
    settings: WebSearchConfig,
    query: str,
    count: int,
) -> list[SearchHit]:
    response = await _request(
        client,
        "POST",
        "https://api.tavily.com/search",
        provider="tavily",
        timeout=settings.timeout_seconds,
        headers={"Content-Type": "application/json", "Accept": "application/json"},
        payload={
            "api_key": settings.tavily_api_key,
            "query": query,
            "max_results": max(1, min(count, 20)),
            "search_depth": settings.tavily_search_depth,
            "include_answer": False,
        },
    )
    if response.status_code == 402:
        raise SearchError(tr("tool_result.web_search.quota", provider=_label("tavily")))
    _raise_for_status(response, "tavily")
    data = _json_object(response, "tavily")
    items = data.get("results")
    hits: list[SearchHit] = []
    if isinstance(items, list):
        for item in items:
            if not isinstance(item, dict):
                continue
            hits.append(
                SearchHit(
                    title=_text(item.get("title")),
                    url=_text(item.get("url")),
                    snippet=_text(item.get("content") or item.get("snippet")),
                    site_name=_text(item.get("source")),
                )
            )
    return hits


async def _search_searxng(
    client: httpx.AsyncClient,
    settings: WebSearchConfig,
    query: str,
    count: int,
) -> list[SearchHit]:
    if not settings.searxng_url:
        raise SearchError(tr("tool_result.web_search.searxng_missing"))
    params: dict[str, str] = {"q": query, "format": "json", "pageno": "1"}
    if settings.searxng_language:
        params["language"] = settings.searxng_language
    if settings.searxng_categories:
        params["categories"] = settings.searxng_categories
    response = await _request(
        client,
        "GET",
        f"{settings.searxng_url}/search",
        provider="searxng",
        timeout=settings.timeout_seconds,
        headers={"Accept": "application/json", "User-Agent": "Coworker"},
        params=params,
    )
    if response.status_code == 401:
        raise SearchError(tr("tool_result.web_search.searxng_auth"))
    if response.status_code == 403:
        raise SearchError(tr("tool_result.web_search.searxng_forbidden"))
    _raise_for_status(response, "searxng")
    data = _json_object(response, "searxng")
    items = data.get("results")
    hits: list[SearchHit] = []
    if isinstance(items, list):
        for item in items[:count]:
            if not isinstance(item, dict):
                continue
            hits.append(
                SearchHit(
                    title=_text(item.get("title")),
                    url=_text(item.get("url")),
                    snippet=_text(item.get("content") or item.get("snippet")),
                    site_name=_text(item.get("engine") or item.get("source")),
                )
            )
    return hits


async def _search_keenable(
    client: httpx.AsyncClient,
    settings: WebSearchConfig,
    query: str,
    count: int,
    freshness: str,
    summary: bool,
) -> list[SearchHit]:
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "X-Keenable-Title": "coworker",
    }
    if settings.keenable_api_key:
        url = "https://api.keenable.ai/v1/search"
        headers["X-API-Key"] = settings.keenable_api_key
    else:
        url = "https://api.keenable.ai/v1/search/public"
    payload: dict[str, Any] = {
        "query": query,
        "max_results": count,
        "snippet_max_length": 1000 if summary else 300,
    }
    payload.update(_keenable_freshness(freshness))
    response = await _request(
        client,
        "POST",
        url,
        provider="keenable",
        timeout=settings.timeout_seconds,
        headers=headers,
        payload=payload,
    )
    if response.status_code == 429 and not settings.keenable_api_key:
        raise SearchError(tr("tool_result.web_search.keenable_rate_limit"))
    _raise_for_status(response, "keenable")
    data = _json_object(response, "keenable")
    items = data.get("results")
    hits: list[SearchHit] = []
    if isinstance(items, list):
        for item in items:
            if not isinstance(item, dict):
                continue
            hits.append(
                SearchHit(
                    title=_text(item.get("title")),
                    url=_text(item.get("url")),
                    snippet=_text(item.get("snippet") or item.get("description")),
                    published=_text(item.get("published_at")),
                )
            )
    return hits


def _keenable_freshness(freshness: str) -> dict[str, str]:
    if not freshness or freshness == "noLimit":
        return {}
    days = _FRESHNESS_DAYS.get(freshness)
    if days is not None:
        start = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
        return {"published_after": start}
    start, sep, end = freshness.partition("..")
    if sep and start.strip() and end.strip():
        return {"published_after": start.strip(), "published_before": end.strip()}
    return {}


async def _request(
    client: httpx.AsyncClient,
    method: str,
    url: str,
    *,
    provider: str,
    timeout: int,
    headers: Mapping[str, str],
    payload: Mapping[str, Any] | None = None,
    params: Mapping[str, str] | None = None,
) -> httpx.Response:
    try:
        return await client.request(
            method,
            url,
            headers=dict(headers),
            json=dict(payload) if payload is not None else None,
            params=dict(params) if params is not None else None,
            timeout=timeout,
        )
    except httpx.TimeoutException as exc:
        raise SearchError(
            tr(
                "tool_result.web_search.timeout",
                provider=_label(provider),
                seconds=timeout,
            ),
            retryable=True,
        ) from exc
    except httpx.HTTPError as exc:
        raise SearchError(
            tr("tool_result.web_search.connect", provider=_label(provider)),
            retryable=True,
        ) from exc


def _raise_for_status(response: httpx.Response, provider: str) -> None:
    label = _label(provider)
    if response.status_code == 401:
        raise SearchError(tr("tool_result.web_search.invalid_key", provider=label))
    if response.status_code == 403:
        raise SearchError(tr("tool_result.web_search.forbidden", provider=label))
    if response.status_code == 429:
        raise SearchError(
            tr("tool_result.web_search.rate_limit", provider=label),
            retryable=True,
        )
    if response.status_code >= 500:
        raise SearchError(
            tr("tool_result.web_search.http", provider=label, status=response.status_code),
            retryable=True,
        )
    if response.status_code != 200:
        excerpt = _excerpt(response)
        message = excerpt or str(response.status_code)
        raise SearchError(tr("tool_result.web_search.business", provider=label, message=message))


def _json_object(response: httpx.Response, provider: str) -> dict[str, Any]:
    try:
        data = response.json()
    except ValueError as exc:
        raise SearchError(
            tr("tool_result.web_search.malformed", provider=_label(provider))
        ) from exc
    if not isinstance(data, dict):
        raise SearchError(tr("tool_result.web_search.malformed", provider=_label(provider)))
    return data


def _bearer(api_key: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }


def _request_id(response: httpx.Response) -> str:
    header = _clean_id(response.headers.get("X-Request-ID"))
    if header:
        return header
    try:
        peek = response.json()
    except ValueError:
        return ""
    if isinstance(peek, dict):
        return _clean_id(peek.get("request_id"))
    return ""


def _with_request_id(message: str, request_id: str) -> SearchError:
    if not request_id:
        return SearchError(message)
    return SearchError(
        tr(
            "tool_result.web_search.with_request_id",
            message=message,
            request_id=request_id,
        )
    )


def _clean_id(value: object) -> str:
    if isinstance(value, str):
        return value.strip()
    return ""


def _text(value: object) -> str:
    if isinstance(value, str):
        return value
    return ""


def _excerpt(response: httpx.Response) -> str:
    return " ".join(response.text.split())[:200]


def _dig(data: Mapping[str, Any], *keys: str) -> list[Any]:
    current: object = data
    for key in keys:
        if not isinstance(current, dict):
            return []
        current = current.get(key)
    return current if isinstance(current, list) else []
