from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import httpx
import pytest

from coworker.brain import factory
from coworker.brain.anthropic_provider import AnthropicProvider
from coworker.brain.any_llm_provider import AnyLLMProvider, DynamicAnyLLMProvider
from coworker.brain.base import BaseLLMProvider
from coworker.brain.deepseek_provider import DeepSeekProvider
from coworker.brain.factory import ProviderCatalogEntry, build_provider
from coworker.brain.minimax_provider import MiniMaxProvider
from coworker.brain.opencode_go_provider import OpenCodeGoProvider
from coworker.brain.qwen_provider import QwenProvider
from coworker.brain.zhipu_provider import ZhipuProvider
from coworker.core.exceptions import ProviderError
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
async def test_completion_replays_opaque_message_and_tool_state_on_next_turn():
    first_wire_response = _response()
    first_wire_message = first_wire_response.choices[0].message
    first_wire_message.reasoning = SimpleNamespace(content="")
    first_wire_message.extra_content = {
        "anthropic": {"signature": "signed-thinking-block"}
    }
    first_wire_message.tool_calls[0].extra_content = {
        "google": {"thought_signature": "signed-tool-call"}
    }
    provider, complete = _provider()
    complete.side_effect = [first_wire_response, _response()]

    first = await provider.complete(
        [Message(role="user", content="first")],
        "system",
        [],
    )
    call = first.tool_calls[0]
    assistant = Message(
        role="assistant",
        content=first.content,
        provider_state=first.provider_state,
        tool_calls=[
            {
                "id": call.id,
                "type": "function",
                "function": {
                    "name": call.name,
                    "arguments": json.dumps(call.arguments),
                },
                "extra_content": call.extra_content,
            }
        ],
    )
    await provider.complete(
        [
            Message(role="user", content="first"),
            assistant,
            Message(role="tool", content="result", tool_call_id=call.id),
        ],
        "system",
        [],
    )

    replayed = complete.await_args_list[1].kwargs["messages"]
    assert replayed[2]["extra_content"] == {
        "anthropic": {"signature": "signed-thinking-block"}
    }
    assert replayed[2]["tool_calls"][0]["extra_content"] == {
        "google": {"thought_signature": "signed-tool-call"}
    }


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
async def test_factory_constructs_opencode_go_without_model_request(monkeypatch):
    close = AsyncMock()
    llm = SimpleNamespace(
        PROVIDER_NAME="opencode-go",
        SUPPORTS_LIST_MODELS=True,
        client=SimpleNamespace(close=close),
    )
    create = Mock(return_value=llm)
    monkeypatch.setattr(
        "coworker.brain.opencode_go_provider.AnyLLM.create_openai_compatible",
        create,
    )

    provider = build_provider("opencode-go", "offline-test-key")
    try:
        assert isinstance(provider, OpenCodeGoProvider)
        assert provider.list_models()
        assert create.call_args.args == ("opencode-go",)
        assert create.call_args.kwargs["api_base"] == "https://opencode.ai/zen/go/v1"
        assert create.call_args.kwargs["api_key"] == "offline-test-key"
    finally:
        await provider.close()

    close.assert_awaited_once_with()


@pytest.mark.parametrize(
    ("model_id", "dialect"),
    [
        ("deepseek-v4-pro", "chat"),
        ("opencode-go/kimi-k3", "chat"),
        ("glm-5.3", "chat"),
        ("grok-4.5", "responses"),
        ("gpt-5.6-luna", "responses"),
        ("minimax-m3", "anthropic"),
        ("qwen3.7-plus", "anthropic"),
        ("qwen3.8-max", "anthropic"),
        ("future-model", "chat"),
    ],
)
def test_opencode_go_routes_official_model_dialects(model_id, dialect):
    assert OpenCodeGoProvider.endpoint_dialect(model_id) == dialect


@pytest.mark.asyncio
async def test_opencode_go_model_discovery_normalizes_provider_prefix():
    provider = OpenCodeGoProvider.__new__(OpenCodeGoProvider)
    BaseLLMProvider.__init__(provider, "opencode-go")
    provider._llm = SimpleNamespace(
        SUPPORTS_LIST_MODELS=True,
        alist_models=AsyncMock(
            return_value=[
                SimpleNamespace(id="opencode-go/kimi-k3"),
                SimpleNamespace(id="deepseek-v4-pro"),
            ]
        ),
    )

    assert await provider.fetch_models() == ["deepseek-v4-pro", "kimi-k3"]


@pytest.mark.asyncio
async def test_opencode_go_uses_expected_metadata_and_inference_paths_with_mock_transport(
    monkeypatch,
):
    paths: list[str] = []
    payloads: list[dict] = []

    def handle(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        if request.url.path == "/v1/models":
            return httpx.Response(
                200,
                json={
                    "object": "list",
                    "data": [
                        {
                            "id": "opencode-go/kimi-k3",
                            "object": "model",
                            "created": 0,
                            "owned_by": "opencode-go",
                        }
                    ],
                },
            )
        payloads.append(json.loads(request.content))
        return httpx.Response(
            400,
            json={
                "type": "error",
                "error": {"type": "invalid_request_error", "message": "offline mock"},
            },
        )

    transport = httpx.MockTransport(handle)
    monkeypatch.setattr(
        OpenCodeGoProvider,
        "_http_client",
        staticmethod(lambda: httpx.AsyncClient(transport=transport)),
    )
    provider = OpenCodeGoProvider(
        "offline-test-key",
        base_url="https://mock-provider.example/v1",
    )
    try:
        assert await provider.fetch_models() == ["kimi-k3"]
        assert paths == ["/v1/models"]

        for model_id, expected_path, expected_effort in (
            ("deepseek-v4-pro", "/v1/chat/completions", "low"),
            ("grok-4.5", "/v1/responses", "low"),
            ("gpt-5.6-luna", "/v1/responses", "low"),
            ("qwen3.8-max", "/v1/messages", "low"),
        ):
            paths.clear()
            payloads.clear()
            provider.set_model(model_id)
            with pytest.raises(ProviderError, match="offline mock"):
                await provider.complete(
                    [Message(role="user", content="offline")],
                    "",
                    [],
                    thinking="low",
                )
            assert paths == [expected_path]
            assert len(payloads) == 1
            if expected_path == "/v1/chat/completions":
                assert payloads[0]["reasoning_effort"] == expected_effort
            elif expected_path == "/v1/responses":
                assert payloads[0]["reasoning"] == {
                    "effort": expected_effort,
                    "summary": "auto",
                }
            else:
                assert payloads[0]["thinking"] == {"type": "adaptive"}
                assert payloads[0]["output_config"]["effort"] == expected_effort
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
