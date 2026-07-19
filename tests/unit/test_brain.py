from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from coworker.brain.brain import Brain
from coworker.core.exceptions import ModelNotSupportedError, ProviderNotFoundError
from coworker.core.types import LLMResponse, Message, SummaryResult
from tests.conftest import MockProvider


class TestBrain:
    def test_register_and_properties(self):
        brain = Brain("mock", "mock-model")
        brain.register_provider(MockProvider())
        assert brain.current_provider_name == "mock"
        assert brain.current_model == "mock-model"

    def test_set_max_tokens_applies_to_runtime(self):
        brain = Brain("mock", "mock-model", max_tokens=100)
        brain.set_max_tokens(2048)
        assert brain.max_tokens == 2048
        with pytest.raises(ValueError):
            brain.set_max_tokens(0)

    @pytest.mark.asyncio
    async def test_upsert_provider_replaces_connection_for_next_call(self):
        brain = Brain("mock", "mock-model")
        original = MockProvider()
        replacement = MockProvider()
        brain.register_provider(original)

        await brain.upsert_provider(replacement)

        assert brain.active_provider is replacement

    @pytest.mark.asyncio
    async def test_think_returns_response(self):
        brain = Brain("mock", "mock-model")
        brain.register_provider(MockProvider())
        response = await brain.think(
            messages=[Message(role="user", content="hi")],
            system_prompt="you are helpful",
            tools=[],
        )
        assert isinstance(response, LLMResponse)
        assert response.content == "mock response"

    @pytest.mark.asyncio
    async def test_think_uses_configured_default_max_tokens(self):
        class CapturingProvider(MockProvider):
            def __init__(self) -> None:
                super().__init__()
                self.seen_max_tokens: int | None = None

            async def complete(self, messages, system_prompt, tools, max_tokens=0, **_):
                self.seen_max_tokens = max_tokens
                return await super().complete(messages, system_prompt, tools, max_tokens)

        provider = CapturingProvider()
        brain = Brain("mock", "mock-model", max_tokens=12345)
        brain.register_provider(provider)

        await brain.think(messages=[], system_prompt="", tools=[])

        assert provider.seen_max_tokens == 12345

    @pytest.mark.asyncio
    async def test_think_max_tokens_override_wins(self):
        class CapturingProvider(MockProvider):
            def __init__(self) -> None:
                super().__init__()
                self.seen_max_tokens: int | None = None

            async def complete(self, messages, system_prompt, tools, max_tokens=0, **_):
                self.seen_max_tokens = max_tokens
                return await super().complete(messages, system_prompt, tools, max_tokens)

        provider = CapturingProvider()
        brain = Brain("mock", "mock-model", max_tokens=12345)
        brain.register_provider(provider)

        await brain.think(messages=[], system_prompt="", tools=[], max_tokens=678)

        assert provider.seen_max_tokens == 678

    @pytest.mark.asyncio
    async def test_think_raises_when_no_provider(self):
        brain = Brain("unknown", "x")
        with pytest.raises(ProviderNotFoundError):
            await brain.think(messages=[], system_prompt="", tools=[])

    @pytest.mark.asyncio
    async def test_switch_model(self):
        brain = Brain("mock", "mock-model")
        brain.register_provider(MockProvider())
        await brain.switch_model("mock", "mock-model")
        assert brain.current_provider_name == "mock"
        assert brain.current_model == "mock-model"

    @pytest.mark.asyncio
    async def test_switch_model_unknown_provider(self):
        brain = Brain("mock", "mock-model")
        brain.register_provider(MockProvider())
        with pytest.raises(ProviderNotFoundError):
            await brain.switch_model("nonexistent", "x")

    @pytest.mark.asyncio
    async def test_switch_model_unsupported_model(self):
        class NoToolProvider(MockProvider):
            provider_name = "notool"

            def supports_tool_use(self, model_id: str) -> bool:
                return False

        brain = Brain("notool", "x")
        brain.register_provider(NoToolProvider())
        with pytest.raises(ModelNotSupportedError):
            await brain.switch_model("notool", "unsupported-model")

    @pytest.mark.asyncio
    async def test_retry_on_failure(self):
        import asyncio
        call_count = 0

        class FlakyProvider(MockProvider):
            async def complete(self, messages, system_prompt, tools, max_tokens=4096, **_):
                nonlocal call_count
                call_count += 1
                if call_count < 3:
                    raise RuntimeError("transient error")
                return await super().complete(messages, system_prompt, tools, max_tokens)

        brain = Brain("mock", "mock-model")
        brain.register_provider(FlakyProvider())

        # patch sleep to avoid test slowness
        original_sleep = asyncio.sleep
        asyncio.sleep = AsyncMock()
        try:
            response = await brain.think(messages=[], system_prompt="", tools=[])
            assert response.content == "mock response"
            assert call_count == 3
        finally:
            asyncio.sleep = original_sleep

    @pytest.mark.asyncio
    async def test_summarize_calls_think(self):
        brain = Brain("mock", "mock-model")
        brain.register_provider(MockProvider())
        result = await brain.summarize(
            messages=[Message(role="user", content="some history")],
            context_hint="test",
        )
        assert result == "mock response"

    @pytest.mark.asyncio
    async def test_summarize_reports_usage_to_listener(self):
        brain = Brain("mock", "mock-model")
        brain.register_provider(MockProvider())
        seen = []
        brain.add_summary_usage_listener(lambda response, meta: seen.append((response, meta)))

        result = await brain.summarize(
            messages=[Message(role="user", content="some history")],
            context_hint="test hint",
        )

        assert result == "mock response"
        assert len(seen) == 1
        response, meta = seen[0]
        assert response.provider == "mock"
        assert response.model == "mock-model"
        assert response.usage == {"input_tokens": 10, "output_tokens": 5}
        assert meta["context_hint"] == "test hint"

    @pytest.mark.asyncio
    async def test_summarize_can_return_usage_result(self):
        brain = Brain("mock", "mock-model")
        brain.register_provider(MockProvider())

        result = await brain.summarize(
            messages=[Message(role="user", content="some history")],
            return_usage=True,
        )

        assert isinstance(result, SummaryResult)
        assert result.content == "mock response"
        assert result.output_tokens == 5

    @pytest.mark.asyncio
    async def test_summarize_disables_thinking_by_default(self):
        class CapturingProvider(MockProvider):
            def __init__(self) -> None:
                super().__init__()
                self.seen_thinking: bool | None = None

            async def complete(self, messages, system_prompt, tools, max_tokens=4096, thinking=True):
                self.seen_thinking = thinking
                return await super().complete(messages, system_prompt, tools, max_tokens, thinking)

        provider = CapturingProvider()
        brain = Brain("mock", "mock-model", thinking=True)
        brain.register_provider(provider)

        await brain.summarize([Message(role="user", content="history")])

        assert provider.seen_thinking is False

    @pytest.mark.asyncio
    async def test_summarize_can_enable_summary_thinking(self):
        class CapturingProvider(MockProvider):
            def __init__(self) -> None:
                super().__init__()
                self.seen_thinking: bool | None = None

            async def complete(self, messages, system_prompt, tools, max_tokens=4096, thinking=True):
                self.seen_thinking = thinking
                return await super().complete(messages, system_prompt, tools, max_tokens, thinking)

        provider = CapturingProvider()
        brain = Brain("mock", "mock-model", thinking=False, summary_thinking=True)
        brain.register_provider(provider)

        await brain.summarize([Message(role="user", content="history")])

        assert provider.seen_thinking is True

    @pytest.mark.asyncio
    async def test_summarize_uses_configured_summary_provider_and_model(self):
        class SummaryProvider(MockProvider):
            provider_name = "summary"
            default_model = "summary-default"

            def __init__(self) -> None:
                super().__init__()
                self.seen_tools: list | None = None

            async def complete(self, messages, system_prompt, tools, max_tokens=4096, **kw):
                self.seen_tools = tools
                return LLMResponse(
                    content=f"summary via {self._current_model}",
                    tool_calls=[],
                    stop_reason="end_turn",
                    model=self._current_model,
                    usage={"input_tokens": 1, "output_tokens": 2},
                )

            def supports_tool_use(self, model_id: str) -> bool:
                return False

        provider = SummaryProvider()
        brain = Brain(
            "mock",
            "mock-model",
            summary_provider="summary",
            summary_model="summary-fast",
        )
        brain.register_provider(MockProvider())
        brain.register_provider(provider)

        result = await brain.summarize([Message(role="user", content="history")])

        assert result == "summary via summary-fast"
        assert provider.seen_tools == []
        assert brain.current_provider_name == "mock"
        assert brain.current_model == "mock-model"

    @pytest.mark.asyncio
    async def test_summarize_uses_summary_provider_default_model(self):
        class SummaryProvider(MockProvider):
            provider_name = "summary"
            default_model = "summary-default"

            async def complete(self, messages, system_prompt, tools, max_tokens=4096, **kw):
                return LLMResponse(
                    content=f"summary via {self._current_model}",
                    tool_calls=[],
                    stop_reason="end_turn",
                    model=self._current_model,
                    usage={},
                )

        brain = Brain("mock", "mock-model", summary_provider="summary")
        brain.register_provider(MockProvider())
        brain.register_provider(SummaryProvider())

        result = await brain.summarize([Message(role="user", content="history")])

        assert result == "summary via summary-default"

    @pytest.mark.asyncio
    async def test_summarize_summary_model_reuses_current_provider(self):
        class CapturingProvider(MockProvider):
            async def complete(self, messages, system_prompt, tools, max_tokens=4096, **kw):
                return LLMResponse(
                    content=f"summary via {self._current_model}",
                    tool_calls=[],
                    stop_reason="end_turn",
                    model=self._current_model,
                    usage={},
                )

        brain = Brain("mock", "mock-model", summary_model="summary-lite")
        brain.register_provider(CapturingProvider())

        result = await brain.summarize([Message(role="user", content="history")])

        assert result == "summary via summary-lite"
        assert brain.current_model == "mock-model"

    @pytest.mark.asyncio
    async def test_summarize_summary_provider_without_model_or_default_raises(self):
        brain = Brain("mock", "mock-model", summary_provider="mock")
        brain.register_provider(MockProvider())

        with pytest.raises(ModelNotSupportedError, match="summary 模型"):
            await brain.summarize([Message(role="user", content="history")])

    @pytest.mark.asyncio
    async def test_summarize_objective_mode_uses_third_party_prompt(self):
        class CapturingProvider(MockProvider):
            def __init__(self) -> None:
                super().__init__()
                self.seen_system_prompt: str = ""

            async def complete(self, messages, system_prompt, tools, max_tokens=4096, **kw):
                self.seen_system_prompt = system_prompt
                return await super().complete(messages, system_prompt, tools, max_tokens, **kw)

        provider = CapturingProvider()
        brain = Brain("mock", "mock-model")
        brain.register_provider(provider)

        await brain.summarize(messages=[Message(role="user", content="history")])

        assert "记忆压缩助手" in provider.seen_system_prompt
        assert "第三方" in provider.seen_system_prompt
        assert "JSON" not in provider.seen_system_prompt or "不要" in provider.seen_system_prompt
        assert "memories" not in provider.seen_system_prompt

    @pytest.mark.asyncio
    async def test_summarize_subjective_mode_uses_agent_system_prompt(self):
        class CapturingProvider(MockProvider):
            def __init__(self) -> None:
                super().__init__()
                self.seen_system_prompt: str = ""
                self.seen_messages: list = []

            async def complete(self, messages, system_prompt, tools, max_tokens=4096, **kw):
                self.seen_system_prompt = system_prompt
                self.seen_messages = list(messages)
                return await super().complete(messages, system_prompt, tools, max_tokens, **kw)

        provider = CapturingProvider()
        brain = Brain("mock", "mock-model")
        brain.register_provider(provider)

        await brain.summarize(
            messages=[Message(role="user", content="history")],
            agent_system_prompt="你是一个专业的工程师助手。",
        )

        assert provider.seen_system_prompt == "你是一个专业的工程师助手。"
        # 最后一条是指令（user），倒数第二条是待压缩片段（system）
        last_msg = provider.seen_messages[-1]
        assert last_msg.role == "user"
        assert "第一人称" in last_msg.content
        slice_msg = provider.seen_messages[-2]
        assert slice_msg.role == "system"
        assert "history" in slice_msg.content

    @pytest.mark.asyncio
    async def test_summarize_subjective_mode_injects_stm_context(self):
        class CapturingProvider(MockProvider):
            def __init__(self) -> None:
                super().__init__()
                self.seen_messages: list = []

            async def complete(self, messages, system_prompt, tools, max_tokens=4096, **kw):
                self.seen_messages = list(messages)
                return await super().complete(messages, system_prompt, tools, max_tokens, **kw)

        provider = CapturingProvider()
        brain = Brain("mock", "mock-model")
        brain.register_provider(provider)

        stm = [Message(role="system", content="[记忆 L1] 过去的摘要")]
        await brain.summarize(
            messages=[Message(role="user", content="history")],
            agent_system_prompt="身份提示",
            stm_context=stm,
        )

        assert provider.seen_messages[0].role == "system"
        assert "过去的摘要" in provider.seen_messages[0].content
        assert provider.seen_messages[-1].role == "user"

    def test_current_model_has_vision_true(self):
        brain = Brain("mock", "mock-model")
        brain.register_provider(MockProvider())
        assert brain.current_model_has_vision is True

    def test_current_model_has_vision_false(self):
        class NoVisionProvider(MockProvider):
            provider_name = "novision"

            def supports_vision(self, model_id: str) -> bool:
                return False

        brain = Brain("novision", "mock-model")
        brain.register_provider(NoVisionProvider())
        assert brain.current_model_has_vision is False

    def test_current_model_has_vision_no_provider(self):
        brain = Brain("unknown", "x")
        assert brain.current_model_has_vision is False

    @pytest.mark.asyncio
    async def test_query_with_vision_success(self):
        brain = Brain("mock", "mock-model")
        brain.register_provider(MockProvider())
        result = await brain.query_with_vision(
            messages=[Message(role="user", content="describe this image")],
            vision_provider="mock",
            vision_model="mock-model",
        )
        assert result == "mock response"

    @pytest.mark.asyncio
    async def test_query_with_vision_reports_usage_to_listener(self):
        brain = Brain("mock", "mock-model")
        brain.register_provider(MockProvider())
        seen = []
        brain.add_vision_usage_listener(lambda response, meta: seen.append((response, meta)))

        result = await brain.query_with_vision(
            messages=[Message(role="user", content="describe this image")],
            vision_provider="mock",
            vision_model="mock-model",
            usage_context={"label": "screen.png"},
        )

        assert result == "mock response"
        assert len(seen) == 1
        response, meta = seen[0]
        assert response.provider == "mock"
        assert response.model == "mock-model"
        assert response.usage == {"input_tokens": 10, "output_tokens": 5}
        assert meta["label"] == "screen.png"

    @pytest.mark.asyncio
    async def test_query_with_vision_uses_configured_thinking_mode(self):
        class CapturingProvider(MockProvider):
            def __init__(self) -> None:
                super().__init__()
                self.seen_thinking: object | None = None

            async def complete(
                self,
                messages,
                system_prompt,
                tools,
                max_tokens=4096,
                thinking=True,
                **_,
            ):
                self.seen_thinking = thinking
                return await super().complete(messages, system_prompt, tools, max_tokens)

        provider = CapturingProvider()
        brain = Brain("mock", "mock-model")
        brain.register_provider(provider)

        await brain.query_with_vision(
            messages=[Message(role="user", content="describe this image")],
            vision_provider="mock",
            vision_model="mock-model",
        )

        assert provider.seen_thinking is True

        await brain.update_model_config(vision_thinking=False)
        await brain.query_with_vision(
            messages=[Message(role="user", content="describe this image")],
            vision_provider="mock",
            vision_model="mock-model",
        )

        assert provider.seen_thinking is False

    @pytest.mark.asyncio
    async def test_query_with_vision_unknown_provider(self):
        brain = Brain("mock", "mock-model")
        brain.register_provider(MockProvider())
        with pytest.raises(RuntimeError, match="未注册"):
            await brain.query_with_vision(
                messages=[],
                vision_provider="nonexistent",
                vision_model="x",
            )

    @pytest.mark.asyncio
    async def test_thinking_true_passed_to_provider(self):
        class CapturingProvider(MockProvider):
            def __init__(self) -> None:
                super().__init__()
                self.seen_thinking: bool | None = None

            async def complete(self, messages, system_prompt, tools, max_tokens=4096, thinking=True, **_):
                self.seen_thinking = thinking
                return await super().complete(messages, system_prompt, tools, max_tokens)

        provider = CapturingProvider()
        brain = Brain("mock", "mock-model", thinking=True)
        brain.register_provider(provider)
        await brain.think(messages=[], system_prompt="", tools=[])
        assert provider.seen_thinking is True

    @pytest.mark.asyncio
    async def test_thinking_false_passed_to_provider(self):
        class CapturingProvider(MockProvider):
            def __init__(self) -> None:
                super().__init__()
                self.seen_thinking: bool | None = None

            async def complete(self, messages, system_prompt, tools, max_tokens=4096, thinking=True, **_):
                self.seen_thinking = thinking
                return await super().complete(messages, system_prompt, tools, max_tokens)

        provider = CapturingProvider()
        brain = Brain("mock", "mock-model", thinking=False)
        brain.register_provider(provider)
        await brain.think(messages=[], system_prompt="", tools=[])
        assert provider.seen_thinking is False

    def test_thinking_default_is_true(self):
        brain = Brain("mock", "mock-model")
        assert brain._thinking is True


class _FailingProvider(MockProvider):
    provider_name = "primary"

    async def complete(self, messages, system_prompt, tools, max_tokens=4096, **_):
        raise RuntimeError("primary down")


class _BackupProvider(MockProvider):
    provider_name = "backup"
    default_model = "backup-model"

    async def complete(self, messages, system_prompt, tools, max_tokens=4096, **_):
        return LLMResponse(
            content="backup response",
            tool_calls=[],
            stop_reason="end_turn",
            model="backup-model",
            usage={"input_tokens": 1, "output_tokens": 1},
        )


class TestBrainFallback:
    @pytest.mark.asyncio
    async def test_fallback_switches_to_backup(self):
        import asyncio
        brain = Brain("primary", "primary-model", fallbacks=["backup"])
        brain.register_provider(_FailingProvider())
        brain.register_provider(_BackupProvider())

        original_sleep = asyncio.sleep
        asyncio.sleep = AsyncMock()
        try:
            resp = await brain.think(messages=[], system_prompt="", tools=[])
        finally:
            asyncio.sleep = original_sleep

        assert resp.content == "backup response"
        # 降级后停在备用模型，并标记为失败降级
        assert brain.current_provider_name == "backup"
        assert brain.current_model == "backup-model"
        assert brain.consume_fallback_switch() is True
        # consume 后复位
        assert brain.consume_fallback_switch() is False

    @pytest.mark.asyncio
    async def test_fallback_with_explicit_model(self):
        import asyncio
        brain = Brain("primary", "primary-model", fallbacks=["backup/custom-model"])
        brain.register_provider(_FailingProvider())
        brain.register_provider(_BackupProvider())

        original_sleep = asyncio.sleep
        asyncio.sleep = AsyncMock()
        try:
            resp = await brain.think(messages=[], system_prompt="", tools=[])
        finally:
            asyncio.sleep = original_sleep

        assert resp.content == "backup response"
        assert brain.current_model == "custom-model"  # 用 entry 里显式指定的 model

    @pytest.mark.asyncio
    async def test_config_error_falls_through_to_fallback(self):
        # 主 provider 未注册（ProviderNotFoundError）也应穿透到 fallback，而非 fail-fast
        brain = Brain("ghost", "x", fallbacks=["backup"])
        brain.register_provider(_BackupProvider())
        resp = await brain.think(messages=[], system_prompt="", tools=[])
        assert resp.content == "backup response"
        assert brain.current_provider_name == "backup"

    @pytest.mark.asyncio
    async def test_summarize_fallback_does_not_persist(self):
        import asyncio
        brain = Brain("primary", "primary-model", fallbacks=["backup"])
        brain.register_provider(_FailingProvider())
        brain.register_provider(_BackupProvider())
        seen = []
        brain.add_summary_usage_listener(lambda response, meta: seen.append((response, meta)))

        original_sleep = asyncio.sleep
        asyncio.sleep = AsyncMock()
        try:
            result = await brain.summarize(
                messages=[Message(role="user", content="history")], context_hint="t"
            )
        finally:
            asyncio.sleep = original_sleep

        assert result == "backup response"
        # 后台摘要借用了 fallback，但不得劫持主 agent 的 active model
        assert brain.current_provider_name == "primary"
        assert brain.consume_fallback_switch() is False
        assert len(seen) == 1
        assert seen[0][0].provider == "backup"
        assert seen[0][0].model == "backup-model"

    @pytest.mark.asyncio
    async def test_all_candidates_fail_raises(self):
        import asyncio

        class _AlsoFail(_BackupProvider):
            async def complete(self, messages, system_prompt, tools, max_tokens=4096, **_):
                raise RuntimeError("backup down")

        brain = Brain("primary", "primary-model", fallbacks=["backup"])
        brain.register_provider(_FailingProvider())
        brain.register_provider(_AlsoFail())

        original_sleep = asyncio.sleep
        asyncio.sleep = AsyncMock()
        try:
            with pytest.raises(RuntimeError):
                await brain.think(messages=[], system_prompt="", tools=[])
        finally:
            asyncio.sleep = original_sleep


class TestBrainModelConfig:
    @pytest.mark.asyncio
    async def test_update_summary_config_changes_summarize_model_and_thinking(self):
        class SummaryProvider(MockProvider):
            provider_name = "summary"
            default_model = "summary-default"

            def __init__(self) -> None:
                super().__init__()
                self.seen_thinking: bool | None = None

            async def complete(self, messages, system_prompt, tools, max_tokens=4096, thinking=True):
                self.seen_thinking = thinking
                return LLMResponse(
                    content=f"summary via {self._current_model}",
                    tool_calls=[],
                    stop_reason="end_turn",
                    model=self._current_model,
                    usage={},
                    provider="summary",
                )

        provider = SummaryProvider()
        brain = Brain("mock", "mock-model")
        brain.register_provider(MockProvider())
        brain.register_provider(provider)

        await brain.update_model_config(
            summary_provider="summary",
            summary_model="summary-fast",
            summary_thinking=True,
        )
        result = await brain.summarize([Message(role="user", content="history")])

        assert result == "summary via summary-fast"
        assert provider.seen_thinking is True

    @pytest.mark.asyncio
    async def test_update_fallbacks_changes_candidate_chain(self):
        import asyncio

        brain = Brain("primary", "primary-model")
        brain.register_provider(_FailingProvider())
        brain.register_provider(_BackupProvider())

        await brain.update_model_config(fallbacks=["backup"])

        original_sleep = asyncio.sleep
        asyncio.sleep = AsyncMock()
        try:
            resp = await brain.think(messages=[], system_prompt="", tools=[])
        finally:
            asyncio.sleep = original_sleep

        assert resp.content == "backup response"
        assert brain.current_provider_name == "backup"

    @pytest.mark.asyncio
    async def test_update_vision_config_sets_default_query_model(self):
        provider = MockProvider()
        brain = Brain("mock", "mock-model")
        brain.register_provider(provider)

        snapshot = await brain.update_model_config(
            vision_provider="mock",
            vision_model="vision-model",
            vision_thinking=False,
        )
        result = await brain.query_with_vision([Message(role="user", content="look")])

        assert result == "mock response"
        assert provider._current_model == "vision-model"
        assert brain.vision_thinking is False
        assert snapshot["vision"]["thinking"] is False

    @pytest.mark.asyncio
    async def test_query_with_vision_rejects_video_when_provider_lacks_capability(self):
        brain = Brain("mock", "mock-model")
        brain.register_provider(MockProvider())

        with pytest.raises(ModelNotSupportedError, match="native video"):
            await brain.query_with_vision(
                messages=[Message(role="user", content="describe this video")],
                vision_provider="mock",
                vision_model="mock-model",
                require_video=True,
            )

    @pytest.mark.asyncio
    async def test_update_rejects_invalid_fallback_provider(self):
        brain = Brain("mock", "mock-model")
        brain.register_provider(MockProvider())

        with pytest.raises(ProviderNotFoundError):
            await brain.update_model_config(fallbacks=["missing"])

    @pytest.mark.asyncio
    async def test_update_rejects_fallback_without_model_or_default(self):
        class NoDefaultProvider(MockProvider):
            provider_name = "nodefault"
            default_model = ""

        brain = Brain("mock", "mock-model")
        brain.register_provider(MockProvider())
        brain.register_provider(NoDefaultProvider())

        with pytest.raises(ModelNotSupportedError, match="fallback 模型"):
            await brain.update_model_config(fallbacks=["nodefault"])

    @pytest.mark.asyncio
    async def test_update_rejects_fallback_without_tool_use(self):
        class NoToolProvider(MockProvider):
            provider_name = "notool"
            default_model = "notool-model"

            def supports_tool_use(self, model_id: str) -> bool:
                return False

        brain = Brain("mock", "mock-model")
        brain.register_provider(MockProvider())
        brain.register_provider(NoToolProvider())

        with pytest.raises(ModelNotSupportedError, match="tool use"):
            await brain.update_model_config(fallbacks=["notool"])

    @pytest.mark.asyncio
    async def test_update_rejects_unsupported_vision_model(self):
        class NoVisionProvider(MockProvider):
            provider_name = "novision"

            def supports_vision(self, model_id: str) -> bool:
                return False

        brain = Brain("mock", "mock-model")
        brain.register_provider(MockProvider())
        brain.register_provider(NoVisionProvider())

        with pytest.raises(ModelNotSupportedError, match="does not support vision"):
            await brain.update_model_config(vision_provider="novision", vision_model="text-only")
