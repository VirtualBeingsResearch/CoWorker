"""Contracts for the multi-provider search_web backend.

The response mapping follows the provider behavior used by CowAgent's
web_search tool: status codes, malformed envelopes, freshness translation,
and the auto/fixed resolver.
"""

from __future__ import annotations

import json
from datetime import datetime

import httpx
import pytest

from coworker.core.config import Config, LLMConfig, WebSearchConfig
from coworker.tools.web_tools import SearchWebTool
from coworker.web_search import (
    SearchError,
    configured_providers,
    resolve_provider,
    run_search,
)


def _config(**web: object) -> Config:
    zhipu_llm_key = str(web.pop("zhipu_llm_key", ""))
    return Config.model_construct(
        web_search=WebSearchConfig.model_validate(web),
        llm=LLMConfig.model_validate({"zhipu_api_key": zhipu_llm_key}),
    )


def _client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def test_auto_uses_ddgs_when_nothing_else_is_configured() -> None:
    config = _config()
    assert configured_providers(config) == ["ddgs"]
    assert resolve_provider(config, None) == ("ddgs", "auto-fallback")


def test_auto_prefers_configured_providers_in_canonical_order() -> None:
    config = _config(tavily_api_key="tv", bocha_api_key="bo", zhipu_llm_key="zk")
    assert configured_providers(config) == ["bocha", "zhipu", "tavily", "ddgs"]
    assert resolve_provider(config, None) == ("bocha", "auto-fallback")
    assert resolve_provider(config, "tavily") == ("tavily", "caller-requested")
    assert resolve_provider(config, "missing") == ("bocha", "auto-fallback")


def test_fixed_provider_falls_back_when_unconfigured() -> None:
    config = _config(strategy="fixed", provider="bocha")
    assert resolve_provider(config, None) == ("ddgs", "auto-fallback")

    pinned = _config(strategy="fixed", provider="tavily", tavily_api_key="tv")
    assert resolve_provider(pinned, "bocha") == ("tavily", "fixed-strategy")


def test_anonymous_and_searxng_count_as_configured() -> None:
    config = _config(
        anysearch_anonymous=True,
        keenable_anonymous=True,
        searxng_url="https://search.example",
    )
    assert configured_providers(config) == ["anysearch", "searxng", "keenable", "ddgs"]


def test_provider_field_is_hidden_until_there_is_a_choice() -> None:
    only_ddgs = SearchWebTool(_config())
    assert "provider" not in only_ddgs.definition.parameters["properties"]

    choices = SearchWebTool(_config(bocha_api_key="bo"))
    provider = choices.definition.parameters["properties"]["provider"]
    assert provider["enum"] == ["bocha", "ddgs"]

    pinned = SearchWebTool(_config(strategy="fixed", provider="ddgs", bocha_api_key="bo"))
    assert "provider" not in pinned.definition.parameters["properties"]


@pytest.mark.asyncio
async def test_bocha_maps_pages_and_does_not_retry_invalid_keys() -> None:
    calls = {"count": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["count"] += 1
        assert request.url == "https://api.bochaai.com/v1/web-search"
        body = json.loads(request.content)
        assert body == {
            "query": "天气",
            "count": 2,
            "freshness": "oneDay",
            "summary": True,
        }
        assert request.headers["authorization"] == "Bearer bo-key"
        return httpx.Response(
            200,
            json={
                "code": 200,
                "data": {
                    "webPages": {
                        "value": [
                            {
                                "name": "预报",
                                "url": "https://example.com/w",
                                "snippet": "晴",
                                "siteName": "示例",
                                "datePublished": "2026-10-10",
                                "summary": "更长的预报",
                            }
                        ]
                    }
                },
            },
        )

    tool = SearchWebTool(_config(bocha_api_key="bo-key"), client=_client(handler))
    result = await tool.execute(query="天气", max_results=2, freshness="oneDay", summary=True)
    assert not result.is_error
    assert "provider: Bocha" in result.content
    assert "预报" in result.content
    assert "https://example.com/w" in result.content
    assert "更长的预报" in result.content
    assert calls["count"] == 1

    def denied(_: httpx.Request) -> httpx.Response:
        calls["count"] += 1
        return httpx.Response(401)

    calls["count"] = 0
    denied_tool = SearchWebTool(_config(bocha_api_key="bad"), client=_client(denied))
    denied_result = await denied_tool.execute(query="天气")
    assert denied_result.is_error
    assert "Bocha API Key 无效" in denied_result.content
    assert calls["count"] == 1


@pytest.mark.asyncio
async def test_bocha_business_error_and_balance() -> None:
    def balance(_: httpx.Request) -> httpx.Response:
        return httpx.Response(403)

    result = await SearchWebTool(_config(bocha_api_key="bo"), client=_client(balance)).execute(
        query="x"
    )
    assert "Bocha 余额不足" in result.content

    def business(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"code": 500, "msg": "upstream"})

    result = await SearchWebTool(_config(bocha_api_key="bo"), client=_client(business)).execute(
        query="x"
    )
    assert "upstream" in result.content


@pytest.mark.asyncio
async def test_zhipu_trims_query_and_falls_back_to_model_key() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert request.url == "https://open.bigmodel.cn/api/paas/v4/web_search"
        assert request.headers["authorization"] == "Bearer llm-key"
        assert body["search_query"] == "q" * 70
        assert body["search_engine"] == "search_pro"
        assert body["search_intent"] is False
        assert body["search_recency_filter"] == "noLimit"
        return httpx.Response(
            200,
            json={
                "search_result": [
                    {
                        "title": "文章",
                        "link": "https://example.com/a",
                        "content": "摘要",
                        "media": "站点",
                        "publish_date": "2026-01-01",
                    }
                ]
            },
        )

    config = _config(zhipu_llm_key="llm-key", strategy="fixed", provider="zhipu")
    result = await SearchWebTool(config, client=_client(handler)).execute(query="q" * 90)
    assert "文章" in result.content
    assert "https://example.com/a" in result.content


@pytest.mark.asyncio
async def test_zhipu_error_object_is_a_failure() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"error": {"code": "1701", "message": "余额不足"}})

    result = await SearchWebTool(
        _config(zhipu_api_key="zk", strategy="fixed", provider="zhipu"),
        client=_client(handler),
    ).execute(query="新闻")
    assert result.is_error
    assert "余额不足" in result.content


@pytest.mark.asyncio
async def test_qianfan_translates_named_freshness_into_a_date_range() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        window = body["search_filter"]["range"]["page_time"]
        assert window["gte"] < window["lt"]
        datetime.strptime(window["gte"], "%Y-%m-%d")
        assert body["resource_type_filter"] == [{"type": "web", "top_k": 5}]
        assert request.headers["x-appbuilder-from"] == "coworker"
        return httpx.Response(
            200,
            json={
                "references": [
                    {
                        "title": "新闻",
                        "url": "https://example.com/n",
                        "content": "x" * 250,
                        "website": "新闻站",
                        "date": "2026-10-01",
                    }
                ]
            },
        )

    result = await SearchWebTool(
        _config(qianfan_api_key="qk", strategy="fixed", provider="qianfan"),
        client=_client(handler),
    ).execute(query="新闻", freshness="oneWeek")
    assert "新闻" in result.content
    assert "x" * 200 in result.content
    assert "x" * 201 not in result.content


@pytest.mark.asyncio
async def test_linkai_accepts_a_string_payload_and_bocha_shaped_pages() -> None:
    def text(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"success": True, "data": "纯文本结果"})

    result = await SearchWebTool(
        _config(linkai_api_key="lk", strategy="fixed", provider="linkai"),
        client=_client(text),
    ).execute(query="插件")
    assert "纯文本结果" in result.content

    def pages(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert body["code"] == "web-search"
        return httpx.Response(
            200,
            json={
                "success": True,
                "data": json.dumps(
                    {
                        "webPages": {
                            "value": [{"name": "页", "url": "https://example.com", "snippet": "摘"}]
                        }
                    }
                ),
            },
        )

    result = await SearchWebTool(
        _config(linkai_api_key="lk", strategy="fixed", provider="linkai"),
        client=_client(pages),
    ).execute(query="插件")
    assert "https://example.com" in result.content


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "body", "expected"),
    [
        (401, {"request_id": "req-1"}, "request_id: req-1"),
        (402, {}, "匿名额度已用尽"),
        (200, ["nope"], "无法解析"),
        (200, {"code": False}, "无法解析"),
        (200, {"code": 0, "data": None}, "无法解析"),
        (200, {"code": 0, "data": {"results": "nope"}}, "无法解析"),
        (200, {"code": 7, "message": "bad query"}, "bad query"),
        (200, {"code": 0, "data": {"results": [{"title": "x"}]}}, "没有返回可用"),
    ],
)
async def test_anysearch_rejects_malformed_envelopes(status, body, expected) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert "authorization" not in request.headers
        return httpx.Response(status, json=body)

    result = await SearchWebTool(
        _config(anysearch_anonymous=True, strategy="fixed", provider="anysearch"),
        client=_client(handler),
    ).execute(query="查询")
    assert result.is_error
    assert expected in result.content


@pytest.mark.asyncio
async def test_anysearch_keeps_usable_hits_and_trims_to_ten() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert body["max_results"] == 10
        assert body["zone"] == "cn"
        rows = [
            {"url": f"https://example.com/{i}", "title": str(i), "snippet": "s"} for i in range(12)
        ]
        rows.append({"title": "drop-me"})
        return httpx.Response(200, json={"code": 0, "data": {"results": rows}})

    result = await SearchWebTool(
        _config(
            anysearch_api_key="ak",
            anysearch_zone="cn",
            strategy="fixed",
            provider="anysearch",
        ),
        client=_client(handler),
    ).execute(query="查询", max_results=40, freshness="oneDay", summary=True)
    assert not result.is_error
    assert "https://example.com/9" in result.content
    assert "https://example.com/10" not in result.content
    assert "drop-me" not in result.content


@pytest.mark.asyncio
async def test_serply_tavily_searxng_and_keenable_map_their_result_fields() -> None:
    def serply(request: httpx.Request) -> httpx.Response:
        assert request.headers["x-api-key"] == "sk"
        assert request.headers["user-agent"] == "Coworker"
        assert "q=%E4%BB%A3%E7%A0%81" in str(request.url) or "q=代码" in str(request.url)
        return httpx.Response(
            200,
            json={
                "results": [
                    {"title": "仓库", "link": "https://example.com/r", "description": "说明"}
                ]
            },
        )

    result = await SearchWebTool(
        _config(serply_api_key="sk", strategy="fixed", provider="serply"),
        client=_client(serply),
    ).execute(query="代码")
    assert "https://example.com/r" in result.content

    def tavily(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert body["max_results"] == 20
        assert body["search_depth"] == "advanced"
        return httpx.Response(
            200,
            json={
                "results": [{"title": "文档", "url": "https://example.com/d", "content": "正文"}]
            },
        )

    result = await SearchWebTool(
        _config(
            tavily_api_key="tk", tavily_search_depth="advanced", strategy="fixed", provider="tavily"
        ),
        client=_client(tavily),
    ).execute(query="文档", max_results=50)
    assert "正文" in result.content

    def searxng(request: httpx.Request) -> httpx.Response:
        assert request.url.params["format"] == "json"
        assert request.url.params["categories"] == "general"
        return httpx.Response(
            200,
            json={
                "results": [
                    {
                        "title": "条目",
                        "url": "https://example.com/s",
                        "content": "片段",
                        "engine": "bing",
                    }
                ]
            },
        )

    result = await SearchWebTool(
        _config(searxng_url="https://sx.example", strategy="fixed", provider="searxng"),
        client=_client(searxng),
    ).execute(query="条目", max_results=1)
    assert "bing" in result.content

    def keenable(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == "https://api.keenable.ai/v1/search/public"
        body = json.loads(request.content)
        assert body["published_after"]
        assert "published_before" not in body
        assert body["snippet_max_length"] == 300
        return httpx.Response(
            200,
            json={"results": [{"title": "卡", "url": "https://example.com/k", "snippet": "内容"}]},
        )

    result = await SearchWebTool(
        _config(keenable_anonymous=True, strategy="fixed", provider="keenable"),
        client=_client(keenable),
    ).execute(query="油价", freshness="oneMonth")
    assert "https://example.com/k" in result.content


@pytest.mark.asyncio
async def test_keenable_date_range_and_keyed_endpoint() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == "https://api.keenable.ai/v1/search"
        body = json.loads(request.content)
        assert body["published_after"] == "2025-01-01"
        assert body["published_before"] == "2025-02-01"
        assert request.headers["x-api-key"] == "kk"
        return httpx.Response(429)

    result = await SearchWebTool(
        _config(keenable_api_key="kk", strategy="fixed", provider="keenable"),
        client=_client(handler),
    ).execute(query="油价", freshness="2025-01-01..2025-02-01")
    assert result.is_error
    assert "速率限制" in result.content
    assert "API Key" not in result.content


@pytest.mark.asyncio
async def test_server_errors_retry_and_empty_query_does_not(monkeypatch) -> None:
    calls = {"count": 0}

    def handler(_: httpx.Request) -> httpx.Response:
        calls["count"] += 1
        return httpx.Response(503)

    async def no_sleep(_: float) -> None:
        return None

    monkeypatch.setattr("coworker.tools.web_tools.asyncio.sleep", no_sleep)
    result = await SearchWebTool(
        _config(bocha_api_key="bo", strategy="fixed", provider="bocha"),
        client=_client(handler),
    ).execute(query="重试")
    assert result.is_error
    assert calls["count"] == 3
    assert "HTTP 503" in result.content

    empty = await SearchWebTool(_config()).execute(query="  ")
    assert empty.is_error
    assert "query" in empty.content


def test_invalid_search_url_is_rejected() -> None:
    with pytest.raises(ValueError, match="http"):
        WebSearchConfig.model_validate({"searxng_url": "ftp://search.example"})


@pytest.mark.asyncio
async def test_run_search_reports_unknown_provider(monkeypatch) -> None:
    config = _config()
    monkeypatch.setattr(
        "coworker.web_search.service.resolve_provider",
        lambda *_args, **_kwargs: ("other", "auto-fallback"),
    )
    with pytest.raises(SearchError, match="other"):
        await run_search(config, query="x")
