from __future__ import annotations

from dataclasses import asdict, dataclass
from functools import lru_cache
from typing import Any, cast

from any_llm import AnyLLM
from any_llm.providers.anthropic.base import BaseAnthropicProvider
from any_llm.providers.openai.base import BaseOpenAIProvider

# 仅为触发各 provider 子类的 __init_subclass__，把 provider_type 登记进
# BaseLLMProvider._TYPE_REGISTRY。导入副作用即注册，无需手维护任何映射表。
from coworker.brain.anthropic_provider import AnthropicProvider  # noqa: F401
from coworker.brain.any_llm_provider import DynamicAnyLLMProvider
from coworker.brain.base import BaseLLMProvider
from coworker.brain.deepseek_provider import DeepSeekProvider  # noqa: F401
from coworker.brain.minimax_provider import MiniMaxProvider  # noqa: F401
from coworker.brain.openai_provider import OpenAIProvider  # noqa: F401
from coworker.brain.qwen_provider import QwenProvider  # noqa: F401
from coworker.brain.zhipu_provider import ZhipuProvider  # noqa: F401
from coworker.i18n import tr


@dataclass(frozen=True, slots=True)
class ProviderCatalogEntry:
    type: str
    any_llm_provider: str
    available: bool
    completion: bool
    customized: bool
    requires_api_key: bool
    api_dialect: str
    client_dialect: str
    default_base_url: str
    tier: str
    doc_url: str
    list_models: bool
    reasoning: bool
    image: bool
    pdf: bool
    unavailable_reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


_ANY_LLM_ALIASES = {
    "qwen": "dashscope",
    "zhipu": "zai",
}
_API_KEY_OPTIONAL_PROVIDERS = {
    "bedrock",
    "cascadia",
    "llamacpp",
    "llamafile",
    "lmstudio",
    "ollama",
    "sagemaker",
    "vertexai",
    "vertexaianthropic",
}


def _dialect_for_class(provider_class: type[Any]) -> str:
    if issubclass(provider_class, BaseAnthropicProvider):
        return "anthropic"
    if issubclass(provider_class, BaseOpenAIProvider):
        return "openai"
    return "any_llm"


@lru_cache(maxsize=1)
def _any_llm_catalog_by_key() -> dict[str, ProviderCatalogEntry]:
    catalog: dict[str, ProviderCatalogEntry] = {}
    for provider_key in AnyLLM.get_supported_providers():
        try:
            provider_class = AnyLLM.get_provider_class(provider_key)
            metadata = provider_class.get_provider_metadata()
        except (AttributeError, ImportError) as error:
            catalog[provider_key] = ProviderCatalogEntry(
                type=provider_key,
                any_llm_provider=provider_key,
                available=False,
                completion=False,
                customized=False,
                requires_api_key=True,
                api_dialect="any_llm",
                client_dialect="any_llm",
                default_base_url="",
                tier="community",
                doc_url="",
                list_models=False,
                reasoning=False,
                image=False,
                pdf=False,
                unavailable_reason=type(error).__name__,
            )
            continue
        missing_packages = getattr(provider_class, "MISSING_PACKAGES_ERROR", None)
        catalog[provider_key] = ProviderCatalogEntry(
            type=provider_key,
            any_llm_provider=provider_key,
            available=missing_packages is None,
            completion=bool(metadata.completion),
            customized=provider_key in BaseLLMProvider._TYPE_REGISTRY,
            requires_api_key=(
                provider_key not in _API_KEY_OPTIONAL_PROVIDERS
                and metadata.env_key not in {"", "None"}
            ),
            api_dialect="any_llm",
            client_dialect=_dialect_for_class(provider_class),
            default_base_url=str(getattr(provider_class, "API_BASE", None) or ""),
            tier=str(metadata.tier),
            doc_url=str(metadata.doc_url or ""),
            list_models=bool(metadata.list_models),
            reasoning=bool(metadata.reasoning),
            image=bool(metadata.image),
            pdf=bool(metadata.pdf),
            unavailable_reason=(
                type(missing_packages).__name__ if missing_packages is not None else ""
            ),
        )
    return catalog


def provider_catalog(*, include_unavailable: bool = False) -> list[ProviderCatalogEntry]:
    """Return Coworker provider types backed by the installed Any-LLM runtime."""

    by_type: dict[str, ProviderCatalogEntry] = {}
    any_llm_catalog = _any_llm_catalog_by_key()
    alias_targets = set(_ANY_LLM_ALIASES.values())
    for provider_key, catalog_entry in any_llm_catalog.items():
        if provider_key in alias_targets:
            continue
        if include_unavailable or (catalog_entry.available and catalog_entry.completion):
            by_type[provider_key] = catalog_entry

    for provider_type, provider_class in BaseLLMProvider._TYPE_REGISTRY.items():
        provider_key = _ANY_LLM_ALIASES.get(provider_type, provider_type)
        entry = any_llm_catalog.get(provider_key)
        if entry is None:
            continue
        by_type[provider_type] = ProviderCatalogEntry(
            **{
                **entry.to_dict(),
                "type": provider_type,
                "customized": True,
                "available": True,
                "completion": True,
                "api_dialect": provider_class.api_dialect,
                "client_dialect": provider_class.api_dialect,
                "default_base_url": str(provider_class.default_base_url or entry.default_base_url),
                "unavailable_reason": "",
            }
        )
    return sorted(by_type.values(), key=lambda entry: entry.type)


def provider_catalog_entry(type_: str) -> ProviderCatalogEntry | None:
    normalized = type_.strip().lower()
    return next(
        (entry for entry in provider_catalog(include_unavailable=True) if entry.type == normalized),
        None,
    )


def available_types() -> list[str]:
    """Provider types usable by the installed Any-LLM runtime."""

    return [entry.type for entry in provider_catalog()]


def _unknown_provider_error(type_: str) -> ValueError:
    return ValueError(
        tr(
            "config.provider.unknown",
            provider=type_,
            available=", ".join(available_types()),
        )
    )


def available_models(type_: str) -> list[str]:
    """Return a provider's static model catalog without constructing an API client."""

    normalized = type_.strip().lower()
    cls = BaseLLMProvider._TYPE_REGISTRY.get(normalized)
    if cls is None and provider_catalog_entry(normalized) is None:
        raise _unknown_provider_error(type_)
    if cls is None:
        return []
    # Every built-in list_models implementation reads a module-level static set.
    # Skipping __init__ keeps first-run metadata local and instant.
    provider = cls.__new__(cls)
    return provider.list_models()


def resolve_base_url(type_: str, configured_base_url: str | None = None) -> str | None:
    normalized = type_.strip().lower()
    cls = BaseLLMProvider._TYPE_REGISTRY.get(normalized)
    entry = provider_catalog_entry(normalized)
    if cls is None and entry is None:
        raise _unknown_provider_error(type_)
    if cls is not None:
        return cls.resolve_base_url(configured_base_url)
    return configured_base_url or (entry.default_base_url if entry else None) or None


def api_dialect(type_: str) -> str:
    normalized = type_.strip().lower()
    cls = BaseLLMProvider._TYPE_REGISTRY.get(normalized)
    entry = provider_catalog_entry(normalized)
    if cls is None and entry is None:
        raise _unknown_provider_error(type_)
    return cls.api_dialect if cls is not None else "any_llm"


def provider_requires_api_key(type_: str) -> bool:
    entry = provider_catalog_entry(type_)
    if entry is None:
        raise _unknown_provider_error(type_)
    return entry.requires_api_key


def build_provider(
    type_: str,
    api_key: str,
    base_url: str | None = None,
    name: str | None = None,
    default_model: str | None = None,
    tool_use_models: list[str] | None = None,
    model_capabilities: list[Any] | None = None,
) -> BaseLLMProvider:
    """按类型实例化一个 provider，并以 name 作为注册名（缺省等于类型名）。

    同一类型可用不同 name 多次调用，得到互不覆盖的多个实例。
    default_model 会记到实例上，供 switch_model 在不指定模型时使用。
    """
    normalized = type_.strip().lower()
    cls = BaseLLMProvider._TYPE_REGISTRY.get(normalized)
    entry = provider_catalog_entry(normalized)
    if cls is None and entry is None:
        raise _unknown_provider_error(type_)
    if entry is not None and (not entry.available or not entry.completion):
        reason = entry.unavailable_reason or "completion unavailable"
        raise ValueError(
            tr(
                "config.provider.unavailable",
                provider=normalized,
                reason=reason,
            )
        )
    if cls is not None:
        provider_factory = cast(Any, cls)
        provider = provider_factory(
            api_key,
            base_url=resolve_base_url(normalized, base_url),
            name=name,
        )
    else:
        provider = DynamicAnyLLMProvider(
            normalized,
            api_key,
            base_url=resolve_base_url(normalized, base_url),
            name=name or normalized,
            client_dialect=entry.client_dialect if entry else "any_llm",
            supports_reasoning=entry.reasoning if entry else False,
        )
    if default_model:
        provider.default_model = default_model
    for model_id in tool_use_models or []:
        provider.allow_tool_use_model(model_id)
    for capability in model_capabilities or []:
        provider.declare_model_capabilities(
            capability.model,
            tools=capability.tools,
            vision=capability.vision,
            video=capability.video,
        )
    return provider
