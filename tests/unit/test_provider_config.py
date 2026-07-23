from __future__ import annotations

import json

import pytest

from coworker.__main__ import _register_providers
from coworker.brain.base import BaseLLMProvider
from coworker.brain.brain import Brain
from coworker.brain.factory import available_models, available_types, build_provider
from coworker.brain.zhipu_provider import ZhipuProvider
from coworker.core.config import Config, LLMConfig
from coworker.core.exceptions import ModelNotSupportedError
from coworker.core.model_config import (
    RuntimeModelConfig,
    apply_runtime_model_config_file,
    load_runtime_model_config,
    write_runtime_model_config,
)


def _llm(**kwargs) -> LLMConfig:
    """构造一个不读 .env / OS 环境的纯净 LLMConfig。"""
    kwargs.setdefault("providers_file", "")
    return LLMConfig(_env_file=None, **kwargs)


@pytest.fixture(autouse=True)
def stub_zhipu_sdk_client(monkeypatch):
    monkeypatch.setattr("coworker.brain.zhipu_provider.openai.AsyncOpenAI", lambda **_: object())
    monkeypatch.setattr(
        "coworker.brain.zhipu_provider.openai.DefaultAsyncHttpxClient",
        lambda **_: object(),
    )


# ---- 类型表（__init_subclass__ 自动注册） ----

def test_type_registry_contains_all_builtins():
    types = available_types()
    for t in ("anthropic", "openai", "deepseek", "qwen", "zhipu", "minimax"):
        assert t in types
    assert BaseLLMProvider._TYPE_REGISTRY["zhipu"] is ZhipuProvider


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


@pytest.mark.asyncio
async def test_confirmed_custom_model_survives_provider_hot_update():
    model = "custom-tool-model"
    brain = Brain("openai", model)
    original = build_provider(
        "openai",
        "sk-original",
        name="openai",
        default_model=model,
        tool_use_models=[model],
    )
    brain.register_provider(original)
    replacement = build_provider(
        "openai",
        "sk-replacement",
        base_url="https://example.test/v1",
        name="openai",
        default_model=model,
        tool_use_models=[model],
    )

    await brain.upsert_provider(replacement)

    assert brain.active_provider is replacement
    assert replacement.can_use_tools(model) is True
