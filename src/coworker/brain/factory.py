from __future__ import annotations

from typing import Any, cast

# 仅为触发各 provider 子类的 __init_subclass__，把 provider_type 登记进
# BaseLLMProvider._TYPE_REGISTRY。导入副作用即注册，无需手维护任何映射表。
from coworker.brain.anthropic_provider import AnthropicProvider  # noqa: F401
from coworker.brain.base import BaseLLMProvider
from coworker.brain.deepseek_provider import DeepSeekProvider  # noqa: F401
from coworker.brain.minimax_provider import MiniMaxProvider  # noqa: F401
from coworker.brain.openai_provider import OpenAIProvider  # noqa: F401
from coworker.brain.qwen_provider import QwenProvider  # noqa: F401
from coworker.brain.zhipu_provider import ZhipuProvider  # noqa: F401


def available_types() -> list[str]:
    """已注册的 provider 类型名（如 anthropic / zhipu / ...）。"""
    return sorted(BaseLLMProvider._TYPE_REGISTRY)


def available_models(type_: str) -> list[str]:
    """Return a provider's static model catalog without constructing an API client."""

    cls = BaseLLMProvider._TYPE_REGISTRY.get(type_)
    if cls is None:
        raise ValueError(
            f"未知 provider 类型 {type_!r}，可用类型：{', '.join(available_types())}"
        )
    # Every built-in list_models implementation reads a module-level static set.
    # Skipping __init__ keeps first-run metadata local and instant.
    provider = cls.__new__(cls)
    return provider.list_models()


def build_provider(
    type_: str,
    api_key: str,
    base_url: str | None = None,
    name: str | None = None,
    default_model: str | None = None,
    tool_use_models: list[str] | None = None,
) -> BaseLLMProvider:
    """按类型实例化一个 provider，并以 name 作为注册名（缺省等于类型名）。

    同一类型可用不同 name 多次调用，得到互不覆盖的多个实例。
    default_model 会记到实例上，供 switch_model 在不指定模型时使用。
    """
    cls = BaseLLMProvider._TYPE_REGISTRY.get(type_)
    if cls is None:
        raise ValueError(
            f"未知 provider 类型 {type_!r}，可用类型：{', '.join(available_types())}"
        )
    provider_factory = cast(Any, cls)
    provider = provider_factory(api_key, base_url=base_url or None, name=name)
    if default_model:
        provider.default_model = default_model
    for model_id in tool_use_models or []:
        provider.allow_tool_use_model(model_id)
    return provider
