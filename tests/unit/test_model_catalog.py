from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from coworker.brain.anthropic_provider import AnthropicProvider
from coworker.brain.base import BaseLLMProvider
from coworker.brain.brain import Brain
from coworker.brain.openai_chat import OpenAIChatCompletionsProvider
from coworker.brain.openai_provider import OpenAIProvider
from tests.conftest import MockProvider


class _CatalogProvider(BaseLLMProvider):
    provider_type = "catalog-test"

    def __init__(self) -> None:
        super().__init__()

    async def complete(
        self,
        messages,
        system_prompt,
        tools,
        max_tokens=4096,
        thinking=True,
        thinking_effort=None,
        tool_choice_required=True,
    ):
        raise NotImplementedError

    def set_model(self, model_id: str) -> None:
        pass

    def list_models(self) -> list[str]:
        return ["static-a", "static-b"]

    def supports_tool_use(self, model_id: str) -> bool:
        return True

    def supports_vision(self, model_id: str) -> bool:
        return False


class _ChatProvider(OpenAIChatCompletionsProvider):
    provider_type = "chat-test"

    def list_models(self) -> list[str]:
        return []

    def supports_tool_use(self, model_id: str) -> bool:
        return True

    def supports_vision(self, model_id: str) -> bool:
        return False


def _page(models: list[str], has_next: bool = False):
    page = SimpleNamespace(
        data=[SimpleNamespace(id=model) for model in models],
        has_next_page=lambda: has_next,
    )
    if has_next:
        next_page = SimpleNamespace(
            data=[SimpleNamespace(id="tail-model")],
            has_next_page=lambda: False,
        )
        page.get_next_page = AsyncMock(return_value=next_page)
    else:
        page.get_next_page = None
    return page


class TestProviderModelCatalog:
    def test_known_models_merges_static_and_remote(self):
        provider = _CatalogProvider()
        assert provider.known_models() == ["static-a", "static-b"]
        provider.mark_remote_models(["remote-a", "static-a"])
        assert provider.known_models() == ["remote-a", "static-a", "static-b"]
        assert provider.remote_models_error() is None

    def test_failed_fetch_records_error_without_losing_previous_cache(self):
        provider = _CatalogProvider()
        provider.mark_remote_models(["remote-a"])
        provider.mark_remote_models_error("boom")
        assert provider.remote_models_error() == "boom"
        assert provider.remote_models() == ["remote-a"]
        assert provider.known_models() == ["remote-a", "static-a", "static-b"]

    @pytest.mark.asyncio
    async def test_openai_chat_provider_fetches_model_pages(self):
        provider = _ChatProvider.__new__(_ChatProvider)
        first = _page(["gpt-new", "gpt-old"], has_next=True)
        provider._client = SimpleNamespace(models=SimpleNamespace(list=AsyncMock(return_value=first)))

        models = await provider.fetch_models()

        assert models == ["gpt-new", "gpt-old", "tail-model"]
        assert provider.known_models() == models
        assert provider.remote_models_error() is None

    @pytest.mark.asyncio
    async def test_openai_provider_fetches_model_pages(self):
        provider = OpenAIProvider.__new__(OpenAIProvider)
        first = _page(["gpt-new"], has_next=False)
        provider._client = SimpleNamespace(models=SimpleNamespace(list=AsyncMock(return_value=first)))

        assert await provider.fetch_models() == ["gpt-new"]

    @pytest.mark.asyncio
    async def test_anthropic_provider_fetches_model_pages(self):
        provider = AnthropicProvider.__new__(AnthropicProvider)
        first = _page(["claude-new"], has_next=True)
        provider._client = SimpleNamespace(
            models=SimpleNamespace(list=AsyncMock(return_value=first))
        )

        models = await provider.fetch_models()

        assert models == ["claude-new", "tail-model"]

    @pytest.mark.asyncio
    async def test_fetch_failure_is_cached_as_error(self):
        provider = _ChatProvider.__new__(_ChatProvider)

        async def fail():
            raise RuntimeError("no models endpoint")

        provider._client = SimpleNamespace(models=SimpleNamespace(list=fail))

        assert await provider.fetch_models() == []
        assert provider.remote_models_error() == "no models endpoint"


class TestBrainModelCatalog:
    def test_snapshot_contains_registered_providers(self):
        brain = Brain("mock", "mock-model")
        provider = MockProvider()
        provider.mark_remote_models(["remote-model"])
        brain.register_provider(provider)

        snapshot = brain.model_catalog_snapshot()

        assert snapshot["providers"] == [
            {
                "name": "mock",
                "type": "",
                "static_models": ["mock-model"],
                "remote_models": ["remote-model"],
                "models": ["mock-model", "remote-model"],
                "error": None,
                "fetched_at": snapshot["providers"][0]["fetched_at"],
            }
        ]

    @pytest.mark.asyncio
    async def test_refresh_refreshes_all_providers(self):
        class RefreshingProvider(MockProvider):
            def __init__(self, name: str) -> None:
                super().__init__()
                self.provider_name = name
                self.fetch_models = AsyncMock(
                    side_effect=lambda: self.mark_remote_models([f"{name}-remote"])
                )

        first = RefreshingProvider("first")
        second = RefreshingProvider("second")
        brain = Brain("first", "mock-model")
        brain.register_provider(first)
        brain.register_provider(second)

        snapshot = await brain.refresh_model_catalog()

        assert first.fetch_models.await_count == 1
        assert second.fetch_models.await_count == 1
        assert snapshot["providers"][0]["remote_models"] == ["first-remote"]
        assert snapshot["providers"][1]["remote_models"] == ["second-remote"]

    @pytest.mark.asyncio
    async def test_refresh_unknown_provider_raises(self):
        brain = Brain("mock", "mock-model")
        brain.register_provider(MockProvider())
        with pytest.raises(Exception, match="mock-unknown"):
            await brain.refresh_model_catalog("mock-unknown")
