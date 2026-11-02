from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import httpx
import openai
import pytest
from mem0.utils.factory import LlmFactory

from coworker.brain.brain import Brain
from coworker.brain.factory import build_provider
from coworker.core.config import Config, LLMConfig, MemoryConfig, ProviderSpec
from coworker.core.llm_errors import FailureCategory, ModelRoutingUnavailable, ProviderCallError
from coworker.core.model_routing import ModelRoutingConfig
from coworker.core.types import LLMResponse
from coworker.memory.backends.mem0 import Mem0Backend
from coworker.memory.mem0_adapters import CoworkerOpenAILLM, RoutedMem0LLM


def chat_response(*, finish_reason="stop", refusal=None):
    return SimpleNamespace(
        choices=[
            SimpleNamespace(
                finish_reason=finish_reason,
                message=SimpleNamespace(content="{}", tool_calls=[], refusal=refusal),
            )
        ],
        usage=SimpleNamespace(
            prompt_tokens=17,
            completion_tokens=3,
            prompt_tokens_details=SimpleNamespace(cached_tokens=9),
        ),
    )


@pytest.fixture
def memory_routing(tmp_path, monkeypatch):
    specs = [
        ProviderSpec(
            name="glm",
            type="zhipu",
            api_key="demo-key-not-valid",
            base_url="https://example.invalid/v1",
            default_model="glm-5.3",
        ),
        ProviderSpec(
            name="go",
            type="opencode-go",
            api_key="demo-key-not-valid",
            base_url="https://example.invalid/v1",
            default_model="gpt-5.6-luna",
        ),
    ]
    config = Config(_env_file=None)
    config.llm = LLMConfig(
        _env_file=None,
        managed_providers=specs,
        providers_file="",
        default_provider="glm",
        default_model="glm-5.3",
        fallbacks=["go"],
    )
    config.memory = MemoryConfig(_env_file=None, mem0_llm_provider="", mem0_llm_model="")
    brain = Brain(
        "glm", "glm-5.3", fallbacks=["go"], routing=ModelRoutingConfig(retry_delay_seconds=0)
    )
    for spec in specs:
        provider = build_provider(
            spec.type, spec.api_key, spec.base_url, spec.name, spec.default_model
        )
        provider.complete = AsyncMock(
            return_value=LLMResponse("ok", [], "end_turn", spec.default_model, {})
        )
        brain.register_provider(provider)
    clients = {}

    def create(_provider, options):
        llm = CoworkerOpenAILLM(options)
        llm.client.chat.completions.create = MagicMock(return_value=chat_response())
        llm.client.responses.create = MagicMock(
            return_value=SimpleNamespace(
                status="completed",
                output_text="{}",
                output=[],
                usage=SimpleNamespace(
                    input_tokens=17,
                    output_tokens=3,
                    input_tokens_details=SimpleNamespace(cached_tokens=9),
                ),
            )
        )
        clients[(options["coworker_provider"], options["model"], options["api_key"])] = llm
        return llm

    factory = MagicMock(side_effect=create)
    monkeypatch.setattr(LlmFactory, "create", factory)
    backend = Mem0Backend(str(tmp_path))
    # Only the production LLM binding is under test; no embedder/index/network is initialized.
    backend._mem = SimpleNamespace(llm=SimpleNamespace(config=SimpleNamespace()))
    backend.bind_model_routing(brain, config)
    yield backend, brain, config, clients, factory
    for client in clients.values():
        client.client.close()


async def generate(backend):
    return await asyncio.to_thread(
        backend._mem.llm.generate_response,
        messages=[{"role": "user", "content": "demo memory"}],
        response_format={"type": "json_object"},
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("responses", [False, True])
async def test_routed_memory_preserves_thinking_and_resets_it_for_the_next_call(memory_routing, responses):
    backend, brain, _, _, factory = memory_routing
    original_factory = factory.side_effect

    def create(provider, options):
        llm = original_factory(provider, options)
        if responses:
            first = SimpleNamespace(
                status="completed", output_text="{}",
                output=[SimpleNamespace(type="reasoning", summary=[SimpleNamespace(text="demo reasoning")])],
                usage=SimpleNamespace(input_tokens=17, output_tokens=3,
                                      output_tokens_details=SimpleNamespace(reasoning_tokens=2)),
            )
            second = SimpleNamespace(status="completed", output_text="{}", output=[],
                                     usage=SimpleNamespace(input_tokens=17, output_tokens=3))
            llm.client.responses.create.side_effect = [first, second]
        else:
            first, second = chat_response(), chat_response()
            first.choices[0].message.reasoning_content = "demo reasoning"
            first.usage.completion_tokens_details = SimpleNamespace(reasoning_tokens=2)
            llm.client.chat.completions.create.side_effect = [first, second]
        return llm

    factory.side_effect = create
    if responses:
        await brain.switch_model("go", "gpt-5.6-luna")
    events = []
    backend.add_usage_listener(events.append)
    backend._install_usage_hook()

    def twice():
        for _ in range(2):
            backend._mem.llm.generate_response(messages=[{"role": "user", "content": "demo"}])

    await asyncio.to_thread(twice)
    assert events[0]["reasoning_content"] == "demo reasoning"
    assert events[0]["usage"]["reasoning_tokens"] == 2
    assert events[1]["reasoning_content"] is None
    assert "reasoning_tokens" not in events[1]["usage"]


@pytest.mark.asyncio
async def test_mem0_authentication_fault_is_shared_with_main_and_usage_names_actual_target(
    memory_routing,
):
    backend, brain, _, clients, factory = memory_routing
    raw_factory = factory.side_effect

    def create(provider, options):
        llm = raw_factory(provider, options)
        if options["coworker_provider"] == "zhipu":
            response = httpx.Response(401, request=httpx.Request("POST", "https://example.invalid"))
            llm.client.chat.completions.create.side_effect = openai.AuthenticationError(
                "private response", response=response, body={"error": {"code": "invalid_api_key"}}
            )
        return llm

    factory.side_effect = create
    usages, routes = [], []
    backend.add_usage_listener(usages.append)
    brain.router.add_listener(routes.append)
    assert await generate(backend) == "{}"
    assert await generate(backend) == "{}"
    assert (
        clients[
            ("zhipu", "glm-5.3", "demo-key-not-valid")
        ].client.chat.completions.create.call_count
        == 1
    )
    assert usages[-1]["provider"] == "go"
    assert usages[-1]["model"] == "gpt-5.6-luna"
    assert usages[-1]["usage"] == {"input_tokens": 17, "output_tokens": 3, "cached_tokens": 9}
    assert usages[-1]["usage_source"] == "provider"
    assert any(
        route.get("category") == "authentication" and route["role"] == "long_term"
        for route in routes
    )
    assert (await brain.think([], "", [])).provider == "go"
    assert brain.router.providers["glm"].complete.await_count == 0
    assert brain.current_provider_name == "glm"


@pytest.mark.asyncio
async def test_mem0_fixed_target_has_no_implicit_fallback(memory_routing):
    backend, brain, config, _, factory = memory_routing
    config.memory.mem0_llm_provider = "glm"
    raw_factory = factory.side_effect

    def create(provider, options):
        llm = raw_factory(provider, options)
        llm.client.chat.completions.create.side_effect = ProviderCallError(
            FailureCategory.QUOTA, connection_scope=True
        )
        return llm

    factory.side_effect = create
    with pytest.raises(ModelRoutingUnavailable):
        await generate(backend)
    assert factory.call_count == 1
    assert brain.current_provider_name == "glm"


@pytest.mark.parametrize("finish,refusal", [("sensitive", None), ("stop", "provider refusal")])
@pytest.mark.asyncio
async def test_mem0_http_success_with_policy_rejection_does_not_fallback(
    memory_routing, finish, refusal
):
    backend, brain, _, _, factory = memory_routing
    raw_factory = factory.side_effect

    def create(provider, options):
        llm = raw_factory(provider, options)
        llm.client.chat.completions.create.return_value = chat_response(
            finish_reason=finish, refusal=refusal
        )
        return llm

    factory.side_effect = create
    routes = []
    brain.router.add_listener(routes.append)
    with pytest.raises(ProviderCallError) as raised:
        await generate(backend)
    assert raised.value.category == FailureCategory.CONTENT_POLICY
    assert factory.call_count == 1
    assert brain.router.snapshot() == []
    assert routes[0]["usage"] == {"input_tokens": 17, "output_tokens": 3, "cached_tokens": 9}
    assert routes[0]["usage_source"] == "provider"
    assert routes[0]["provider"] == "glm"
    assert routes[0]["role"] == "long_term"


@pytest.mark.asyncio
async def test_mem0_follows_live_preference_and_uses_go_responses_wire_protocol(memory_routing):
    backend, brain, _, clients, _ = memory_routing
    assert isinstance(backend._mem.llm, RoutedMem0LLM)
    assert await generate(backend) == "{}"
    glm = clients[("zhipu", "glm-5.3", "demo-key-not-valid")]
    assert (
        glm.client.chat.completions.create.call_args.kwargs["extra_body"]["thinking"]["type"]
        == "enabled"
    )
    assert 0 < glm.client.chat.completions.create.call_args.kwargs["timeout"] <= 90
    await brain.switch_model("go", "gpt-5.6-luna")
    assert await generate(backend) == "{}"
    go = clients[("opencode-go", "gpt-5.6-luna", "demo-key-not-valid")]
    go.client.chat.completions.create.assert_not_called()
    options = go.client.responses.create.call_args.kwargs
    assert options["model"] == "gpt-5.6-luna"
    assert options["text"] == {"format": {"type": "json_object"}}
    assert options["extra_headers"]["x-opencode-session"].startswith("coworker:")
    assert 0 < options["timeout"] <= 90
    assert options["reasoning"] == {"effort": "none"}


@pytest.mark.asyncio
async def test_mem0_replaces_cached_client_after_credentials_change(memory_routing):
    backend, brain, _, clients, factory = memory_routing
    await generate(backend)
    replacement = build_provider(
        "zhipu", "demo-replacement-key-not-valid", "https://example.invalid/v1", "glm", "glm-5.3"
    )
    await brain.upsert_provider(replacement)
    await generate(backend)
    assert factory.call_count == 2
    assert ("zhipu", "glm-5.3", "demo-replacement-key-not-valid") in clients


@pytest.mark.asyncio
async def test_mem0_provider_type_alias_prefers_configured_default_with_multiple_connections(
    memory_routing,
):
    backend, brain, config, clients, _ = memory_routing
    second = ProviderSpec(
        name="glm-other", type="zhipu", api_key="demo-other-key-not-valid",
        base_url="https://other.invalid/v1", default_model="other-model",
    )
    config.llm.managed_providers.append(second)
    brain.register_provider(build_provider(
        second.type, second.api_key, second.base_url, second.name, second.default_model
    ))
    await brain.switch_model("go", "gpt-5.6-luna")
    config.memory.mem0_llm_provider = "zhipu"
    assert await generate(backend) == "{}"
    assert ("zhipu", "glm-5.3", "demo-key-not-valid") in clients
    assert not any(key[2] == "demo-other-key-not-valid" for key in clients)
    config.memory.mem0_llm_provider = "glm-other"
    assert await generate(backend) == "{}"
    assert ("zhipu", "other-model", "demo-other-key-not-valid") in clients


@pytest.mark.parametrize("reason", ["content_filter", "refusal", "failed"])
@pytest.mark.asyncio
async def test_mem0_responses_rejections_keep_usage_for_the_shared_collector(memory_routing, reason):
    from coworker.agent.usage_stats import UsageStatsCollector

    backend, brain, config, _, factory = memory_routing
    config.memory.mem0_llm_provider = "go"
    raw_factory = factory.side_effect

    def create(provider, options):
        llm = raw_factory(provider, options)
        response = llm.client.responses.create.return_value
        response.model = "actual-go-model"
        if reason == "content_filter":
            response.incomplete_details = SimpleNamespace(reason=reason)
        elif reason == "refusal":
            response.output = [SimpleNamespace(type="message", content=[SimpleNamespace(type=reason)])]
        else:
            response.status = "failed"
            response.error = SimpleNamespace(model_dump=lambda: {"code": "content_policy_violation"})
        return llm

    factory.side_effect = create
    collector, success_usages = UsageStatsCollector(), []
    brain.router.add_listener(lambda event: collector.on_entry({"type": "model_route", **event}))
    backend.add_usage_listener(success_usages.append)
    with pytest.raises(ProviderCallError):
        await generate(backend)
    stats = collector.snapshot()["lifetime"]
    assert stats["llm_calls"] == stats["exact_calls"] == 1
    assert stats["total_tokens"] == 20
    assert stats["by_scope"]["long_term"]["cached_tokens"] == 9
    assert stats["by_provider_model"]["go/actual-go-model"]["total_tokens"] == 20
    assert success_usages == []


@pytest.mark.asyncio
async def test_mem0_success_does_not_reuse_usage_from_a_failed_attempt(memory_routing):
    backend, brain, _, _, factory = memory_routing
    raw_factory = factory.side_effect

    def create(provider, options):
        llm = raw_factory(provider, options)
        if options["coworker_provider"] == "zhipu":
            llm.client.chat.completions.create.return_value = chat_response(finish_reason="network_error")
        else:
            llm.client.responses.create.return_value.usage = None
        return llm

    factory.side_effect = create
    usages, routes = [], []
    backend.add_usage_listener(usages.append)
    brain.router.add_listener(routes.append)
    assert await generate(backend) == "{}"
    assert len([event for event in routes if event.get("usage_source") == "provider"]) == 2
    assert len(usages) == 1
    assert usages[0]["provider"] == "go"
    assert usages[0]["usage_source"] == "estimated"


@pytest.mark.asyncio
async def test_mem0_anthropic_rejection_preserves_measured_usage(memory_routing):
    from coworker.memory.mem0_adapters import CoworkerAnthropicLLM

    backend, brain, config, _, factory = memory_routing
    provider = build_provider(
        "anthropic", "demo-key-not-valid", "https://example.invalid/v1", "demo-anthropic", "demo-model"
    )
    brain.register_provider(provider)
    config.memory.mem0_llm_provider = "demo-anthropic"
    clients = []

    def create(_provider, options):
        llm = CoworkerAnthropicLLM(options)
        llm.client.messages.create = MagicMock(return_value=SimpleNamespace(
            stop_reason="refusal", model="actual-anthropic-model",
            usage=SimpleNamespace(input_tokens=17, output_tokens=3, cache_read_input_tokens=9),
        ))
        clients.append(llm.client)
        return llm

    factory.side_effect = create
    routes = []
    brain.router.add_listener(routes.append)
    try:
        with pytest.raises(ProviderCallError):
            await generate(backend)
        assert routes[0]["usage"] == {"input_tokens": 17, "output_tokens": 3, "cached_tokens": 9}
        assert routes[0]["usage_model"] == "actual-anthropic-model"
        assert routes[0]["provider"] == "demo-anthropic"
    finally:
        for client in clients:
            client.close()
