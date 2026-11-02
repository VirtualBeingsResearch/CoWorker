from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any

from coworker.brain.factory import api_dialect, resolve_base_url
from coworker.core.config import Config, ProviderSpec
from coworker.memory.base import (
    LongTermMemoryBackend,
    MemoryBackendConfig,
    MemoryQuery,
    MemoryRecord,
    MemoryWriteResult,
    UsageListener,
)

if TYPE_CHECKING:
    from coworker.brain.brain import Brain

__all__ = [
    "LongTermLLMConfig",
    "LongTermMemory",
    "MemoryRecord",
    "MemoryWriteResult",
    "build_memory_llm_config",
    "resolve_memory_provider_name",
]


@dataclass(frozen=True, slots=True)
class LongTermLLMConfig:
    """LLM settings used by long-term memory backends that perform extraction.

    This remains structurally compatible with :class:`MemoryBackendConfig`; the
    mem0-specific conversion is provided by ``as_mem0_config`` for Mem0Backend.
    """

    provider: str = "anthropic"
    api_dialect: str = "anthropic"
    api_key: str = ""
    model: str = "claude-haiku-4-5-20251001"
    base_url: str = ""
    # 独立 thinking 开关（默认关闭）。mem0 原生不转发思考参数，由
    # CoworkerOpenAILLM 按 provider 类型注入 extra_body。
    thinking: bool = False

    def as_mem0_config(self) -> tuple[str, dict[str, Any]]:
        config: dict[str, Any] = {"model": self.model, "api_key": self.api_key}
        if self.base_url:
            config[f"{self.api_dialect}_base_url"] = self.base_url
        # thinking/coworker_provider 仅 openai 方言使用（CoworkerOpenAIConfig 才有
        # 这些字段）；anthropic 用 AnthropicConfig，混入未知字段会校验失败。
        if self.api_dialect == "openai":
            config["thinking"] = self.thinking
            config["coworker_provider"] = self.provider
        return self.api_dialect, config


def resolve_memory_provider_name(
    provider: str, provider_types: Mapping[str, str], default_provider: str
) -> str | None:
    """Use one name/type-alias contract for configuration and live routing."""
    if provider in provider_types:
        return provider
    if provider_types.get(default_provider) == provider:
        return default_provider
    matches = [name for name, kind in provider_types.items() if kind == provider]
    return matches[0] if len(matches) == 1 else None


def _resolve_memory_provider(llm_config: Any, provider: str) -> ProviderSpec | None:
    by_name = {spec.name: spec for spec in llm_config.resolved_providers()}
    name = resolve_memory_provider_name(
        provider, {name: spec.type for name, spec in by_name.items()}, llm_config.default_provider
    )
    return by_name.get(name) if name is not None else None


def build_memory_llm_config(
    config: Config,
    *,
    active_provider: str = "",
    active_model: str = "",
) -> LongTermLLMConfig:
    """按「跟随主线」语义解析长期记忆的 LLM 配置。

    - mem0_llm_provider 为空 → active_provider（运行态主线），无则 llm.default_provider。
    - provider/model 都为空 → 跟随运行态 active provider/model。
    - 显式配置 provider 但 model 为空 → 该 provider 的 default_model，
      无则 llm.default_model。

    两项都留空时与 Brain 的 summary 跟随语义一致：使用当前主线模型，
    而不是 provider 的启动默认值。
    """
    provider_name = (
        config.memory.mem0_llm_provider
        or active_provider
        or config.llm.default_provider
    )
    provider = _resolve_memory_provider(config.llm, provider_name)
    provider_type = provider.type if provider is not None else provider_name
    configured_base_url = provider.base_url if provider is not None else ""
    model = config.memory.mem0_llm_model
    if not model:
        follows_active_model = not config.memory.mem0_llm_provider and bool(
            active_provider and active_model
        )
        if follows_active_model:
            model = active_model
        else:
            model = (
                (provider.default_model if provider is not None else None)
                or config.llm.default_model
            )
    return LongTermLLMConfig(
        provider=provider_type,
        api_dialect=api_dialect(provider_type),
        api_key=provider.api_key if provider is not None else "",
        model=model,
        base_url=resolve_base_url(provider_type, configured_base_url) or "",
        thinking=config.memory.mem0_llm_thinking,
    )


class LongTermMemory:
    """Facade over a :class:`LongTermMemoryBackend`.

    It preserves the historical method signatures used by tools, the agent loop,
    and the admin API, while delegating all backend-specific work.
    """

    def __init__(self, backend: LongTermMemoryBackend) -> None:
        self._backend = backend

    @property
    def backend(self) -> LongTermMemoryBackend:
        return self._backend

    async def initialize(self) -> None:
        await self._backend.initialize()

    def is_ready(self) -> bool:
        return self._backend.is_ready()

    async def reconfigure(self, llm: MemoryBackendConfig) -> None:
        await self._backend.reconfigure(llm)

    def bind_model_routing(self, brain: Brain, config: Config) -> None:
        bind = getattr(self._backend, "bind_model_routing", None)
        if callable(bind):
            bind(brain, config)

    def add_usage_listener(self, fn: UsageListener) -> None:
        self._backend.add_usage_listener(fn)

    async def write(
        self,
        content: str,
        category: str = "general",
        tags: list[str] | None = None,
        source_timestamp: datetime | None = None,
    ) -> MemoryWriteResult:
        return await self._backend.write(
            content,
            category=category,
            tags=tags or [],
            source_timestamp=source_timestamp,
        )

    async def query(
        self,
        query_text: str,
        category: str | None = None,
        tags: list[str] | None = None,
        limit: int = 10,
        start: datetime | None = None,
        end: datetime | None = None,
        manual: bool = False,
    ) -> list[MemoryRecord]:
        return await self._backend.query(
            MemoryQuery(
                text=query_text,
                category=category,
                tags=tags,
                limit=limit,
                start=start,
                end=end,
                manual=manual,
            )
        )

    async def query_by_tags(
        self,
        query_text: str,
        tags: list[str],
        limit: int = 8,
    ) -> list[MemoryRecord]:
        if not tags:
            return []
        return await self.query(query_text, tags=tags, limit=limit)

    async def update(
        self,
        memory_id: str,
        content: str,
        *,
        tags: list[str] | None = None,
    ) -> None:
        await self._backend.update(memory_id, content, tags=tags)

    async def associate_tags(self, memory_id: str, tags: list[str]) -> list[str]:
        return await self._backend.associate_tags(memory_id, tags)

    async def delete(self, memory_id: str) -> None:
        await self._backend.delete(memory_id)

    async def count(self) -> int:
        return await self._backend.count()
