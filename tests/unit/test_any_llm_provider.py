from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from coworker.brain import factory
from coworker.brain.anthropic_provider import AnthropicProvider
from coworker.brain.any_llm_provider import AnyLLMProvider, DynamicAnyLLMProvider
from coworker.brain.base import BaseLLMProvider
from coworker.brain.deepseek_provider import DeepSeekProvider
from coworker.brain.factory import ProviderCatalogEntry, build_provider
from coworker.brain.minimax_provider import MiniMaxProvider
from coworker.brain.qwen_provider import QwenProvider
from coworker.brain.zhipu_provider import ZhipuProvider
from coworker.core.types import Message


class _TestProvider(AnyLLMProvider):
    def list_models(self) -> list[str]:
        return ["catalog-model"]

    def supports_tool_use(self, model_id: str) -> bool:
        return True

    def supports_vision(self, model_id: str) -> bool:
        return False


def _provider(response=None) -> tuple[_TestProvider, AsyncMock]:
    complete = AsyncMock(return_value=response)
    provider = _TestProvider.__new__(_TestProvider)
    BaseLLMProvider.__init__(provider, "test")
    provider._current_model = "model-1"
    provider._llm = SimpleNamespace(
        SUPPORTS_LIST_MODELS=True,
        acompletion=complete,
        alist_models=AsyncMock(),
    )
    provider._client = SimpleNamespace(close=AsyncMock())
    return provider, complete


def _response(*, arguments: str = '{"query":"hello"}'):
    function = SimpleNamespace(name="search", arguments=arguments)
    message = SimpleNamespace(
        content="",
        reasoning=SimpleNamespace(content="checked sources"),
        tool_calls=[SimpleNamespace(id="call_1", function=function)],
    )
    usage = SimpleNamespace(
        prompt_tokens=12,
        completion_tokens=7,
        prompt_tokens_details=SimpleNamespace(cached_tokens=3),
    )
    return SimpleNamespace(
        choices=[SimpleNamespace(message=message, finish_reason="tool_calls")],
        model="model-1",
        usage=usage,
    )


def _catalog_entry(**changes) -> ProviderCatalogEntry:
    values = {
        "type": "openrouter",
        "any_llm_provider": "openrouter",
        "available": True,
        "completion": True,
        "customized": False,
        "requires_api_key": True,
        "api_dialect": "any_llm",
        "client_dialect": "openai",
        "default_base_url": "https://openrouter.example/v1",
        "tier": "verified",
        "doc_url": "https://openrouter.example/docs",
        "list_models": True,
        "reasoning": True,
        "image": True,
        "pdf": False,
        "unavailable_reason": "",
    }
    values.update(changes)
    return ProviderCatalogEntry(**values)


@pytest.mark.asyncio
async def test_completion_normalizes_coworker_messages_tools_and_response():
    provider, complete = _provider(_response())

    result = await provider.complete(
        [Message(role="user", content="hello")],
        "system",
        [{"name": "search", "parameters": {"type": "object"}}],
        thinking=True,
    )

    kwargs = complete.await_args.kwargs
    assert kwargs["messages"] == [
        {"role": "system", "content": "system"},
        {"role": "user", "content": "hello"},
    ]
    assert kwargs["tools"][0]["type"] == "function"
    assert kwargs["reasoning_effort"] == "high"
    assert result.stop_reason == "tool_use"
    assert result.tool_calls[0].arguments == {"query": "hello"}
    assert result.reasoning_content == "checked sources"
    assert result.usage == {"input_tokens": 12, "output_tokens": 7, "cached_tokens": 3}


@pytest.mark.asyncio
async def test_completion_preserves_malformed_tool_arguments_for_diagnostics():
    provider, _ = _provider(_response(arguments="not-json"))

    result = await provider.complete([Message(role="user", content="hello")], "system", [])

    assert result.tool_calls[0].arguments["__raw_arguments__"] == "not-json"
    assert "__parse_error__" in result.tool_calls[0].arguments


@pytest.mark.asyncio
async def test_fetch_models_uses_metadata_endpoint_only():
    provider, complete = _provider()
    provider._llm.alist_models.return_value = [
        SimpleNamespace(id="model-b"),
        SimpleNamespace(id="model-a"),
        SimpleNamespace(id="model-b"),
    ]

    assert await provider.fetch_models() == ["model-a", "model-b"]
    provider._llm.alist_models.assert_awaited_once_with()
    complete.assert_not_awaited()


@pytest.mark.asyncio
async def test_close_releases_any_llm_client():
    provider, _ = _provider()

    await provider.close()

    provider._client.close.assert_awaited_once_with()


def test_catalog_maps_aliases_and_filters_missing_optional_dependencies(monkeypatch):
    metadata = SimpleNamespace(
        completion=True,
        env_key="OPENROUTER_API_KEY",
        tier="verified",
        doc_url="https://example.test/docs",
        list_models=True,
        reasoning=True,
        image=True,
        pdf=False,
    )

    class AvailableProvider:
        API_BASE = "https://example.test/v1"
        MISSING_PACKAGES_ERROR = None

        @classmethod
        def get_provider_metadata(cls):
            return metadata

    class MissingProvider(AvailableProvider):
        MISSING_PACKAGES_ERROR = ImportError("optional SDK is missing")

    classes = {
        "openrouter": AvailableProvider,
        "dashscope": AvailableProvider,
        "missing": MissingProvider,
    }
    monkeypatch.setattr(
        factory.AnyLLM,
        "get_supported_providers",
        lambda: list(classes),
    )
    monkeypatch.setattr(
        factory.AnyLLM,
        "get_provider_class",
        lambda key: classes[key],
    )
    monkeypatch.setattr(factory, "_dialect_for_class", lambda _class: "openai")
    factory._any_llm_catalog_by_key.cache_clear()
    try:
        catalog = {entry.type: entry for entry in factory.provider_catalog()}
        all_entries = {
            entry.type: entry for entry in factory.provider_catalog(include_unavailable=True)
        }
    finally:
        factory._any_llm_catalog_by_key.cache_clear()

    assert "openrouter" in catalog
    assert catalog["openrouter"].api_dialect == "any_llm"
    assert catalog["openrouter"].client_dialect == "openai"
    assert "qwen" in catalog
    assert catalog["qwen"].any_llm_provider == "dashscope"
    assert "dashscope" not in all_entries
    assert "missing" not in catalog
    assert all_entries["missing"].available is False


@pytest.mark.asyncio
async def test_factory_builds_conservative_dynamic_provider(monkeypatch):
    entry = _catalog_entry()
    created = Mock()

    def fake_create(name, **kwargs):
        created(name, **kwargs)
        return SimpleNamespace(
            SUPPORTS_LIST_MODELS=True,
            client=kwargs.get("http_client"),
        )

    monkeypatch.setattr(factory, "provider_catalog_entry", lambda _type: entry)
    monkeypatch.setattr(
        "coworker.brain.any_llm_provider.AnyLLM.create",
        Mock(side_effect=fake_create),
    )

    provider = build_provider("openrouter", "offline-key")
    try:
        assert isinstance(provider, DynamicAnyLLMProvider)
        assert provider.provider_type == "openrouter"
        assert provider.api_dialect == "any_llm"
        assert provider.can_use_tools("unknown-model") is False
        assert provider.can_use_vision("unknown-model") is False
        assert provider._completion_options("medium") == {"reasoning_effort": "medium"}
        assert created.call_args.args == ("openrouter",)
        assert created.call_args.kwargs["api_base"] == "https://openrouter.example/v1"
    finally:
        await provider.close()


def test_dynamic_provider_omits_reasoning_for_unsupported_backend():
    provider = DynamicAnyLLMProvider.__new__(DynamicAnyLLMProvider)
    provider._supports_reasoning = False

    assert provider._completion_options("high") == {"reasoning_effort": None}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("provider_type", "any_llm_name"),
    [
        ("openai", "openai"),
        ("anthropic", "anthropic"),
        ("deepseek", "deepseek"),
        ("qwen", "dashscope"),
        ("zhipu", "zai"),
        ("minimax", "minimax"),
    ],
)
async def test_factory_maps_builtin_provider_to_any_llm(
    monkeypatch,
    provider_type,
    any_llm_name,
):
    created = Mock()

    def fake_create(name, **kwargs):
        created(name, **kwargs)
        return SimpleNamespace(client=kwargs["http_client"])

    monkeypatch.setattr(
        "coworker.brain.any_llm_provider.AnyLLM.create",
        Mock(side_effect=fake_create),
    )

    provider = build_provider(provider_type, "offline-test-key")
    await provider.close()

    assert created.call_args.args == (any_llm_name,)
    assert created.call_args.kwargs["api_key"] == "offline-test-key"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("provider_type", "any_llm_name"),
    [
        ("openai", "openai"),
        ("anthropic", "anthropic"),
        ("deepseek", "deepseek"),
        ("qwen", "dashscope"),
        ("zhipu", "zai"),
        ("minimax", "minimax"),
    ],
)
async def test_builtin_provider_constructs_real_any_llm_client_without_request(
    provider_type,
    any_llm_name,
):
    provider = build_provider(provider_type, "offline-construction-only")
    try:
        assert provider._llm.PROVIDER_NAME == any_llm_name
    finally:
        await provider.close()


@pytest.mark.asyncio
async def test_anthropic_native_token_count_preserves_tool_history_shape():
    count_tokens = AsyncMock(return_value=SimpleNamespace(input_tokens=42))
    provider = AnthropicProvider.__new__(AnthropicProvider)
    BaseLLMProvider.__init__(provider, "anthropic")
    provider._current_model = "claude-sonnet-4-6"
    provider._client = SimpleNamespace(
        messages=SimpleNamespace(count_tokens=count_tokens),
    )
    messages = [
        Message(role="system", content="historical system note"),
        Message(
            role="assistant",
            content="checking",
            tool_calls=[
                {
                    "id": "call-1",
                    "function": {
                        "name": "lookup",
                        "arguments": '{"query":"hello"}',
                    },
                }
            ],
        ),
        Message(role="tool", content="one", tool_call_id="call-1"),
        Message(role="tool", content="two", tool_call_id="call-2"),
    ]

    assert await provider.count_tokens(messages, "claude-sonnet-4-6") == 42
    assert count_tokens.await_args.kwargs["messages"] == [
        {"role": "user", "content": "historical system note"},
        {
            "role": "assistant",
            "content": [
                {"type": "text", "text": "checking"},
                {
                    "type": "tool_use",
                    "id": "call-1",
                    "name": "lookup",
                    "input": {"query": "hello"},
                },
            ],
        },
        {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": "call-1",
                    "content": [{"type": "text", "text": "one"}],
                },
                {
                    "type": "tool_result",
                    "tool_use_id": "call-2",
                    "content": [{"type": "text", "text": "two"}],
                },
            ],
        },
    ]


def test_provider_specific_thinking_options_are_preserved():
    assert QwenProvider._completion_options(None, True) == {
        "reasoning_effort": None,
        "extra_body": {"enable_thinking": True},
    }
    assert QwenProvider._completion_options(None, "low")["extra_body"] == {
        "enable_thinking": True,
        "thinking_budget": 2048,
    }
    assert QwenProvider._completion_options(None, "auto")["extra_body"] == {
        "enable_thinking": True,
    }
    assert ZhipuProvider._completion_options(None, False)["extra_body"]["thinking"] == {
        "type": "disabled",
        "clear_thinking": False,
    }
    assert MiniMaxProvider._completion_options(None, True)["extra_body"] == {
        "reasoning_split": True,
        "thinking": "adaptive",
    }
    assert DeepSeekProvider._completion_options(None, False) == {"reasoning_effort": "none"}
    assert DeepSeekProvider._completion_options(None, "medium") == {
        "reasoning_effort": "medium"
    }
    assert DeepSeekProvider._completion_options(None, "auto") == {
        "reasoning_effort": "auto",
        "extra_body": {"thinking": {"type": "enabled"}},
    }
