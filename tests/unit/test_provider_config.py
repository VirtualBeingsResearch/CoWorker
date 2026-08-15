from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from any_llm import AnyLLM

from coworker.application import _bind_memory_model_following, _register_providers
from coworker.brain.base import BaseLLMProvider
from coworker.brain.brain import Brain
from coworker.brain.factory import (
    api_dialect,
    available_models,
    available_types,
    build_provider,
    provider_catalog,
)
from coworker.brain.zhipu_provider import ZhipuProvider
from coworker.core.config import (
    Config,
    LLMConfig,
    ModelCapabilitySpec,
    ModelPriceSpec,
    ProviderSpec,
)
from coworker.core.exceptions import ModelNotSupportedError
from coworker.core.model_config import (
    RuntimeModelConfig,
    apply_runtime_model_config_file,
    load_runtime_model_config,
    write_runtime_model_config,
)
from coworker.memory.long_term import LongTermLLMConfig, build_memory_llm_config


def _llm(**kwargs) -> LLMConfig:
    """构造一个不读 .env / OS 环境的纯净 LLMConfig。"""
    kwargs.setdefault("providers_file", "")
    return LLMConfig(_env_file=None, **kwargs)


@pytest.fixture(autouse=True)
def stub_zhipu_sdk_client(monkeypatch):
    monkeypatch.setattr(
        "coworker.brain.any_llm_provider.AnyLLM.create",
        lambda *_args, **_kwargs: SimpleNamespace(SUPPORTS_LIST_MODELS=False),
    )


# ---- 类型表（__init_subclass__ 自动注册） ----

def test_type_registry_contains_all_builtins():
    types = available_types()
    for t in (
        "anthropic",
        "openai",
        "deepseek",
        "qwen",
        "zhipu",
        "minimax",
        "opencode-go",
    ):
        assert t in types
    assert "openrouter" in types
    assert "dashscope" not in types
    assert BaseLLMProvider._TYPE_REGISTRY["zhipu"] is ZhipuProvider


def test_opencode_go_catalog_keeps_openai_and_uses_go_metadata():
    catalog = {entry.type: entry for entry in provider_catalog()}

    assert "openai" in catalog
    assert catalog["opencode-go"].any_llm_provider == "openai"
    assert catalog["opencode-go"].default_base_url == "https://opencode.ai/zen/go/v1"
    assert catalog["opencode-go"].doc_url == "https://opencode.ai/docs/go/"
    assert catalog["opencode-go"].reasoning is True
    assert catalog["opencode-go"].image is False


def test_catalog_represents_every_any_llm_provider_key():
    catalog = provider_catalog(include_unavailable=True)

    assert {entry.any_llm_provider for entry in catalog} == set(
        AnyLLM.get_supported_providers()
    )


def test_model_catalog_does_not_construct_api_client(monkeypatch):
    def fail_init(*args, **kwargs):
        raise AssertionError("provider client should not be constructed")

    monkeypatch.setattr(ZhipuProvider, "__init__", fail_init)
    assert "glm-5.1" in available_models("zhipu")


# ---- build_provider ----

def test_build_provider_uses_name_as_registry_key():
    p = build_provider("zhipu", "k", name="zhipu-userA")
    assert isinstance(p, ZhipuProvider)
    assert p.provider_name == "zhipu-userA"
    assert p.provider_type == "zhipu"


def test_build_provider_defaults_name_to_type():
    p = build_provider("zhipu", "k")
    assert p.provider_name == "zhipu"


def test_build_provider_unknown_type_lists_available():
    with pytest.raises(ValueError) as ei:
        build_provider("nope", "k")
    msg = str(ei.value)
    assert "nope" in msg
    assert "zhipu" in msg  # 错误信息列出可用类型


def test_build_provider_sets_default_model():
    p = build_provider("zhipu", "k", name="z", default_model="glm-4.7")
    assert p.default_model == "glm-4.7"


def test_build_provider_no_default_model_is_empty():
    assert build_provider("zhipu", "k").default_model == ""


def test_build_provider_applies_declared_model_capabilities():
    model = "custom-omni-model"
    provider = build_provider(
        "zhipu",
        "k",
        model_capabilities=[
            ModelCapabilitySpec(
                model=model,
                tools=True,
                vision=True,
                video=True,
            )
        ],
    )

    assert provider.can_use_tools(model) is True
    assert provider.can_use_vision(model) is True
    assert provider.can_use_video(model) is True


def test_declared_capabilities_override_static_provider_catalog():
    model = "glm-5.1"
    provider = build_provider(
        "zhipu",
        "k",
        model_capabilities=[
            ModelCapabilitySpec(
                model=model,
                tools=False,
                vision=False,
                video=False,
            )
        ],
    )

    assert provider.supports_tool_use(model) is True
    assert provider.can_use_tools(model) is False
    assert provider.can_use_vision(model) is False


def test_provider_model_capabilities_require_unique_models_and_vision_for_video():
    with pytest.raises(ValueError, match="支持视觉"):
        ModelCapabilitySpec(model="video-only", video=True)

    with pytest.raises(ValueError, match="重复"):
        ProviderSpec(
            name="custom",
            type="openai",
            model_capabilities=[
                {"model": "same", "tools": True},
                {"model": "same", "vision": True},
            ],
        )


def test_model_prices_normalize_identity_and_currency():
    price = ModelPriceSpec(
        provider=" openai-work ",
        model=" gpt-5.2 ",
        currency=" usd ",
        input_per_million=1.75,
        output_per_million=14,
    )

    assert price.provider == "openai-work"
    assert price.model == "gpt-5.2"
    assert price.currency == "USD"
    assert price.cached_input_per_million is None


def test_model_prices_default_to_an_empty_table():
    assert _llm().model_prices == []


@pytest.mark.parametrize(
    "changes",
    [
        {"provider": ""},
        {"provider": None},
        {"model": "   "},
        {"currency": "US"},
        {"currency": 840},
        {"currency": "US1"},
        {"currency": "EURO"},
    ],
)
def test_model_prices_reject_blank_identity_and_invalid_currency(changes):
    data = {
        "provider": "openai",
        "model": "gpt-5.2",
        "currency": "USD",
        "input_per_million": 1,
        "output_per_million": 2,
        **changes,
    }

    with pytest.raises(ValueError):
        ModelPriceSpec.model_validate(data)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("input_per_million", -1),
        ("output_per_million", float("inf")),
        ("cached_input_per_million", float("nan")),
    ],
)
def test_model_prices_reject_invalid_rates(field, value):
    data = {
        "provider": "openai",
        "model": "gpt-5.2",
        "currency": "USD",
        "input_per_million": 1,
        "output_per_million": 2,
        field: value,
    }

    with pytest.raises(ValueError, match="非负数"):
        ModelPriceSpec.model_validate(data)


def test_model_prices_require_unique_provider_model_pairs():
    with pytest.raises(ValueError, match="模型定价重复"):
        _llm(
            model_prices=[
                {
                    "provider": "openai",
                    "model": "gpt-5.2",
                    "currency": "USD",
                    "input_per_million": 1,
                    "output_per_million": 2,
                },
                {
                    "provider": "openai",
                    "model": "gpt-5.2",
                    "currency": "CNY",
                    "input_per_million": 1,
                    "output_per_million": 2,
                },
            ]
        )


def test_model_prices_load_from_environment_json(monkeypatch):
    monkeypatch.setenv(
        "LLM__MODEL_PRICES",
        '[{"provider":"openai","model":"gpt-5.2","currency":"usd",'
        '"input_per_million":1.75,"output_per_million":14}]',
    )

    config = LLMConfig(_env_file=None, providers_file="")

    assert [price.model_dump() for price in config.model_prices] == [
        {
            "provider": "openai",
            "model": "gpt-5.2",
            "currency": "USD",
            "input_per_million": 1.75,
            "output_per_million": 14.0,
            "cached_input_per_million": None,
        }
    ]


# ---- resolved_providers 合并逻辑 ----

def test_resolved_flat_only():
    cfg = _llm(zhipu_api_key="zk", anthropic_api_key="ak")
    specs = {s.name: s for s in cfg.resolved_providers()}
    assert set(specs) == {"zhipu", "anthropic"}
    assert specs["zhipu"].type == "zhipu"
    assert specs["zhipu"].api_key == "zk"


def test_resolved_empty_when_nothing_configured():
    assert _llm().resolved_providers() == []


def test_summary_model_config_fields_are_loaded():
    cfg = _llm(summary_provider="zhipu-b", summary_model="glm-4.7", summary_thinking=True)
    assert cfg.summary_provider == "zhipu-b"
    assert cfg.summary_model == "glm-4.7"
    assert cfg.summary_thinking is True


def test_vision_thinking_defaults_to_enabled_and_can_be_disabled():
    assert _llm().vision_thinking is True
    assert _llm(vision_thinking=False).vision_thinking is False


def test_main_thinking_accepts_dynamic_effort_and_legacy_boolean():
    assert _llm().thinking is True
    assert _llm(thinking=False).thinking is False
    assert _llm(thinking="minimal").thinking == "minimal"


def test_legacy_runtime_vision_config_keeps_thinking_enabled():
    runtime = RuntimeModelConfig.model_validate({
        "vision": {"provider": "anthropic", "model": "claude-sonnet-4-6"},
    })

    assert runtime.vision.thinking is True


def test_runtime_model_config_file_applies_to_llm_config(tmp_path):
    path = tmp_path / "model_runtime_config.json"
    write_runtime_model_config(
        path,
        RuntimeModelConfig.model_validate({
            "thinking": "medium",
            "summary": {"provider": "zhipu-b", "model": "glm-4.7", "thinking": True},
            "fallbacks": ["zhipu-b", "deepseek/deepseek-chat"],
            "vision": {
                "provider": "anthropic",
                "model": "claude-sonnet-4-6",
                "thinking": False,
            },
        }),
    )
    cfg = _llm(runtime_config_file=str(path))

    runtime = apply_runtime_model_config_file(cfg)

    assert runtime is not None
    assert cfg.thinking == "medium"
    assert cfg.summary_provider == "zhipu-b"
    assert cfg.summary_model == "glm-4.7"
    assert cfg.summary_thinking is True
    assert cfg.fallbacks == ["zhipu-b", "deepseek/deepseek-chat"]
    assert cfg.vision_provider == "anthropic"
    assert cfg.vision_model == "claude-sonnet-4-6"
    assert cfg.vision_thinking is False


def test_runtime_model_config_missing_file_is_ignored(tmp_path):
    cfg = _llm(runtime_config_file=str(tmp_path / "missing.json"))

    assert apply_runtime_model_config_file(cfg) is None
    assert cfg.fallbacks == []


def test_runtime_model_config_bad_json_raises(tmp_path):
    path = tmp_path / "model_runtime_config.json"
    path.write_text("{not json", encoding="utf-8")

    with pytest.raises(ValueError, match="运行态模型配置"):
        load_runtime_model_config(path)


def test_resolved_file_adds_multiple_same_type(tmp_path):
    pf = tmp_path / "providers.json"
    pf.write_text(json.dumps([
        {"name": "zhipu-a", "type": "zhipu", "api_key": "ka"},
        {"name": "zhipu-b", "type": "zhipu", "api_key": "kb", "base_url": "https://b"},
    ]), encoding="utf-8")
    cfg = _llm(providers_file=str(pf))
    specs = {s.name: s for s in cfg.resolved_providers()}
    assert set(specs) == {"zhipu-a", "zhipu-b"}
    assert specs["zhipu-b"].base_url == "https://b"


def test_resolved_file_overrides_same_name_flat(tmp_path):
    pf = tmp_path / "providers.json"
    pf.write_text(json.dumps([
        {"name": "zhipu", "type": "zhipu", "api_key": "from-file"},
    ]), encoding="utf-8")
    cfg = _llm(zhipu_api_key="from-flat", providers_file=str(pf))
    specs = {s.name: s for s in cfg.resolved_providers()}
    assert specs["zhipu"].api_key == "from-file"


def test_resolved_missing_file_ignored(tmp_path):
    cfg = _llm(zhipu_api_key="zk", providers_file=str(tmp_path / "nope.json"))
    assert [s.name for s in cfg.resolved_providers()] == ["zhipu"]


def test_resolved_bad_json_raises(tmp_path):
    pf = tmp_path / "providers.json"
    pf.write_text("{not json", encoding="utf-8")
    with pytest.raises(ValueError):
        _llm(providers_file=str(pf)).resolved_providers()


def test_resolved_non_array_raises(tmp_path):
    pf = tmp_path / "providers.json"
    pf.write_text(json.dumps({"name": "x"}), encoding="utf-8")
    with pytest.raises(ValueError):
        _llm(providers_file=str(pf)).resolved_providers()


# ---- Brain 多实例注册互不覆盖 ----

def test_brain_registers_multiple_same_type():
    brain = Brain("zhipu-a", "glm-5.1")
    brain.register_provider(build_provider("zhipu", "ka", name="zhipu-a"))
    brain.register_provider(build_provider("zhipu", "kb", name="zhipu-b"))
    assert brain.list_providers() == ["zhipu-a", "zhipu-b"]
    assert brain._providers["zhipu-a"] is not brain._providers["zhipu-b"]


def test_resolved_file_carries_default_model(tmp_path):
    pf = tmp_path / "providers.json"
    pf.write_text(json.dumps([
        {"name": "zhipu-b", "type": "zhipu", "api_key": "kb", "default_model": "glm-4.7"},
    ]), encoding="utf-8")
    specs = {s.name: s for s in _llm(providers_file=str(pf)).resolved_providers()}
    assert specs["zhipu-b"].default_model == "glm-4.7"


# ---- switch_model 不指定模型时回退到实例 default_model ----

@pytest.mark.asyncio
async def test_switch_model_falls_back_to_default_model():
    brain = Brain("anthropic", "claude-sonnet-4-6")
    brain.register_provider(build_provider("zhipu", "k", name="zhipu-b", default_model="glm-4.7"))
    await brain.switch_model("zhipu-b")
    assert brain.current_provider_name == "zhipu-b"
    assert brain.current_model == "glm-4.7"


@pytest.mark.asyncio
async def test_switch_model_no_model_and_no_default_raises():
    brain = Brain("anthropic", "claude-sonnet-4-6")
    brain.register_provider(build_provider("zhipu", "k", name="zhipu-b"))  # 无 default_model
    with pytest.raises(ModelNotSupportedError):
        await brain.switch_model("zhipu-b")


@pytest.mark.asyncio
async def test_declared_custom_model_can_be_selected_for_vision():
    model = "custom-vision-model"
    brain = Brain("zhipu", "glm-5.1")
    brain.register_provider(
        build_provider(
            "zhipu",
            "k",
            model_capabilities=[
                ModelCapabilitySpec(model=model, vision=True)
            ],
        )
    )

    snapshot = await brain.update_model_config(
        vision_provider="zhipu",
        vision_model=model,
    )

    assert snapshot["vision"]["provider"] == "zhipu"
    assert snapshot["vision"]["model"] == model


def test_register_providers_skips_empty_credentials():
    config = Config.model_validate(
        {
            "llm": {
                "default_provider": "anthropic",
                "default_model": "claude-sonnet-4-8",
                "providers_file": "",
                "managed_providers": [
                    {"name": "anthropic", "type": "anthropic", "api_key": ""}
                ],
            }
        }
    )
    brain = Brain("anthropic", "claude-sonnet-4-8")

    _register_providers(brain, config)

    assert brain.active_provider is None
    assert brain.list_providers() == []


def test_register_providers_accepts_keyless_local_backend(monkeypatch):
    config = Config.model_validate(
        {
            "llm": {
                "default_provider": "ollama",
                "default_model": "local-model",
                "providers_file": "",
                "managed_providers": [
                    {"name": "ollama", "type": "ollama", "api_key": ""}
                ],
            }
        }
    )
    brain = Brain("ollama", "local-model")
    provider = SimpleNamespace(provider_name="ollama")
    build = Mock(return_value=provider)
    monkeypatch.setattr(
        "coworker.brain.factory.provider_requires_api_key",
        lambda _type: False,
    )
    monkeypatch.setattr("coworker.application.build_provider", build)

    _register_providers(brain, config)

    assert brain.list_providers() == ["ollama"]
    build.assert_called_once()


def test_memory_llm_uses_default_named_provider_endpoint():
    config = Config.model_validate(
        {
            "llm": {
                "default_provider": "company-openai",
                "default_model": "company-model",
                "providers_file": "",
                "managed_providers": [
                    {
                        "name": "company-openai",
                        "type": "openai",
                        "api_key": "secret",
                        "base_url": "https://llm.example.test/v1",
                    }
                ],
            },
            "memory": {
                "mem0_llm_provider": "openai",
                "mem0_llm_model": "memory-model",
            },
        }
    )

    assert build_memory_llm_config(config) == LongTermLLMConfig(
        provider="openai",
        api_dialect="openai",
        api_key="secret",
        model="memory-model",
        base_url="https://llm.example.test/v1",
    )


def test_memory_llm_uses_same_default_endpoint_as_brain():
    config = Config.model_validate(
        {
            "llm": {
                "default_provider": "qwen",
                "default_model": "qwen-plus",
                "providers_file": "",
                "managed_providers": [
                    {
                        "name": "qwen",
                        "type": "qwen",
                        "api_key": "secret",
                    }
                ],
            },
            "memory": {
                "mem0_llm_provider": "qwen",
                "mem0_llm_model": "qwen-plus",
            },
        }
    )

    memory_llm = build_memory_llm_config(config)

    assert memory_llm.api_dialect == "openai"
    assert memory_llm.base_url == "https://dashscope.aliyuncs.com/compatible-mode/v1"


def test_memory_llm_follows_runtime_active_provider_when_unset():
    config = Config.model_validate(
        {
            "llm": {
                "default_provider": "deepseek",
                "default_model": "deepseek-v4-pro",
                "providers_file": "",
                "managed_providers": [
                    {
                        "name": "deepseek",
                        "type": "deepseek",
                        "api_key": "vllm-key",
                        "base_url": "http://vllm.internal.example:8000/v1",
                        "default_model": "self-hosted-vllm-model",
                    },
                    {
                        "name": "deepseek-official",
                        "type": "deepseek",
                        "api_key": "official-key",
                        "default_model": "official-default",
                    },
                ],
            },
            # mem0 未显式配置：跟随运行态 active provider/model，而非启动默认。
            "memory": {},
        }
    )

    llm = build_memory_llm_config(
        config,
        active_provider="deepseek-official",
        active_model="deepseek-v4-flash",
    )

    assert llm.provider == "deepseek"
    assert llm.api_key == "official-key"
    assert llm.base_url == "https://api.deepseek.com"
    assert llm.model == "deepseek-v4-flash"


@pytest.mark.asyncio
async def test_memory_model_binding_reconfigures_after_active_switch():
    config = Config.model_validate(
        {
            "llm": {
                "default_provider": "deepseek",
                "default_model": "deepseek-v4-pro",
                "providers_file": "",
                "managed_providers": [
                    {
                        "name": "deepseek-official",
                        "type": "deepseek",
                        "api_key": "official-key",
                        "default_model": "official-default",
                    }
                ],
            },
            "memory": {},
        }
    )
    brain = Brain("deepseek", "deepseek-v4-pro")
    brain.register_provider(
        build_provider(
            "deepseek",
            "official-key",
            name="deepseek-official",
            default_model="official-default",
        )
    )
    long_term = AsyncMock()
    _bind_memory_model_following(brain, long_term, config)

    await brain.switch_model("deepseek-official", "deepseek-v4-flash")

    long_term.reconfigure.assert_awaited_once()
    applied = long_term.reconfigure.await_args.args[0]
    assert applied.provider == "deepseek"
    assert applied.model == "deepseek-v4-flash"


@pytest.mark.parametrize(
    ("provider", "expected"),
    [
        ("anthropic", "anthropic"),
        ("openai", "openai"),
        ("deepseek", "openai"),
        ("qwen", "openai"),
        ("zhipu", "openai"),
        ("minimax", "openai"),
        ("openrouter", "any_llm"),
    ],
)
def test_provider_declares_mem0_compatible_api_dialect(
    provider: str,
    expected: str,
) -> None:
    assert api_dialect(provider) == expected


@pytest.mark.asyncio
async def test_confirmed_custom_model_survives_provider_hot_update():
    model = "custom-tool-model"
    brain = Brain("openai", model)
    original = build_provider(
        "openai",
        "sk-original",
        name="openai",
        default_model=model,
        model_capabilities=[ModelCapabilitySpec(model=model, tools=True, vision=True)],
    )
    brain.register_provider(original)
    replacement = build_provider(
        "openai",
        "sk-replacement",
        base_url="https://example.test/v1",
        name="openai",
        default_model=model,
        model_capabilities=[ModelCapabilitySpec(model=model, tools=True, vision=True)],
    )

    await brain.upsert_provider(replacement)

    assert brain.active_provider is replacement
    assert replacement.can_use_tools(model) is True
    assert replacement.can_use_vision(model) is True
