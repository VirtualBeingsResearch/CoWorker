from __future__ import annotations

import asyncio
import hashlib
import json
from datetime import datetime
from functools import wraps
from pathlib import Path
from typing import Any, ClassVar

from loguru import logger

from coworker.memory.base import (
    MemoryBackendConfig,
    MemoryQuery,
    MemoryQuerySettings,
    MemoryRecord,
    MemoryWriteResult,
    UsageListener,
)

_AGENT_USER_ID = "agent"
_DEFAULT_EMBEDDER = "sentence-transformers/paraphrase-multilingual-mpnet-base-v2"


def _as_mem0_config(config: MemoryBackendConfig) -> tuple[str, dict[str, Any]]:
    """Convert a backend-agnostic memory LLM config into mem0's factory config.

    ``LongTermLLMConfig`` provides ``as_mem0_config``; the fallback below keeps
    Mem0Backend decoupled from that concrete class.
    """
    as_mem0 = getattr(config, "as_mem0_config", None)
    if callable(as_mem0):
        return as_mem0()

    config_dict: dict[str, Any] = {"model": config.model, "api_key": config.api_key}
    if config.base_url:
        config_dict[f"{config.api_dialect}_base_url"] = config.base_url
    if config.api_dialect == "openai":
        config_dict["thinking"] = config.thinking
        config_dict["coworker_provider"] = config.provider
    return config.api_dialect, config_dict


class Mem0Backend:
    """Long-term memory backend backed by mem0 + ChromaDB.

    This is the default production backend. It intentionally implements the
    backend-agnostic contract and keeps mem0-specific details inside this class.
    """

    backend_id: ClassVar[str] = "mem0"

    # 仅列出 Coworker 直接 import 的顶层模块；mem0 栈其余（chromadb、
    # sentence-transformers、torch、spacy 等）由 mem0ai[nlp] 作为传递依赖一并安装，
    # 不在此单列，避免因传递依赖差异误报缺失。
    _MEM0_REQUIRED: ClassVar[tuple[str, ...]] = ("mem0",)

    @classmethod
    def required_modules(cls) -> tuple[str, ...]:
        """报错明细用：列出缺少的可导入顶层模块。

        mem0 后端运行时直接 import 的顶层模块是 ``mem0``；其余为 ``mem0ai[nlp]``
        的传递依赖，不单列。
        """
        return cls._MEM0_REQUIRED

    @classmethod
    def available(cls) -> bool:
        """仅用 ``find_spec`` 探测，不触发真实 import；缺依赖时返回 False。"""
        import importlib.util

        return all(
            importlib.util.find_spec(module) is not None
            for module in cls._MEM0_REQUIRED
        )

    def __init__(
        self,
        db_path: str,
        llm: MemoryBackendConfig | None = None,
        embedder_model: str = _DEFAULT_EMBEDDER,
        query_settings: MemoryQuerySettings | None = None,
    ) -> None:
        self._db_path = Path(db_path)
        self._mem: Any | None = None
        self._llm = llm or _EmptyMemoryConfig()
        self._embedder_model = embedder_model
        self._query_settings = query_settings
        self._write_lock = asyncio.Lock()
        self._usage_listeners: list[UsageListener] = []
        self._usage_hook_installed = False

    @property
    def embedder(self) -> Any | None:
        """Return mem0's initialized embedding object so nearby indexes can reuse it."""
        return getattr(self._mem, "embedding_model", None) if self._mem is not None else None

    @property
    def chroma_client(self) -> Any | None:
        """Return mem0's Chroma client when the configured vector store exposes one."""
        vector_store = getattr(self._mem, "vector_store", None)
        return getattr(vector_store, "client", None)

    def is_ready(self) -> bool:
        return self._mem is not None

    @property
    def relevance_threshold(self) -> float:
        if self._query_settings is None:
            return 0.5
        return self._query_settings.auto_recall_relevance_threshold

    async def initialize(self) -> None:
        from mem0 import AsyncMemory

        from coworker.memory.mem0_adapters import register_mem0_adapters

        register_mem0_adapters()
        llm_provider, llm_config = _as_mem0_config(self._llm or _EmptyMemoryConfig())
        config = {
            "custom_instructions": _long_term_custom_instructions(),
            "llm": {
                "provider": llm_provider,
                "config": llm_config,
            },
            "vector_store": {
                "provider": "chroma",
                "config": {"collection_name": "memories", "path": str(self._db_path)},
            },
            "embedder": {
                "provider": "huggingface",
                "config": {"model": self._embedder_model},
            },
        }
        self._mem = AsyncMemory.from_config(config)
        self._usage_hook_installed = False
        self._install_usage_hook()
        encoder = getattr(self.embedder, "model", self.embedder)
        device = getattr(encoder, "device", "unknown")
        logger.info(
            f"Long-term memory (mem0) initialized at {self._db_path}, "
            f"embedder={self._embedder_model}, device={device}, "
            f"relevance_threshold={self.relevance_threshold}"
        )

    async def reconfigure(self, config: MemoryBackendConfig) -> None:
        """Replace mem0's LLM instance at runtime without rebuilding the vector store."""
        if self._mem is None:
            self._llm = config
            logger.info("Long-term memory not initialized; deferred LLM reconfiguration")
            return

        from mem0.utils.factory import LlmFactory

        from coworker.memory.mem0_adapters import register_mem0_adapters

        register_mem0_adapters()
        provider, config_dict = _as_mem0_config(config)
        new_llm = LlmFactory.create(provider, config_dict)
        async with self._write_lock:
            self._mem.llm = new_llm
            self._mem.config.llm.provider = provider
            self._mem.config.llm.config = config_dict
            self._llm = config
            self._usage_hook_installed = False
            self._install_usage_hook()
        logger.info(
            f"Long-term memory LLM reconfigured: provider={config.provider} model={config.model}"
        )

    def add_usage_listener(self, listener: UsageListener) -> None:
        self._usage_listeners.append(listener)

    @staticmethod
    def _estimate_mem0_messages_tokens(messages: Any) -> int:
        from coworker.core.token_utils import estimate_content_tokens, estimate_text_tokens

        if not isinstance(messages, list):
            return estimate_content_tokens(str(messages))
        total = 0
        for message in messages:
            if not isinstance(message, dict):
                total += estimate_content_tokens(str(message))
                continue
            total += estimate_text_tokens(str(message.get("role") or ""))
            total += estimate_content_tokens(message.get("content") or "")
        return total

    @staticmethod
    def _estimate_mem0_response_tokens(response: Any) -> int:
        from coworker.core.token_utils import estimate_text_tokens

        if isinstance(response, str):
            return estimate_text_tokens(response)
        return estimate_text_tokens(json.dumps(response, ensure_ascii=False, default=str))

    @staticmethod
    def _extract_response_usage(response: Any) -> dict[str, int] | None:
        usage = getattr(response, "usage", None)
        if usage is None:
            return None
        input_tokens = (
            getattr(usage, "input_tokens", None) or getattr(usage, "prompt_tokens", None) or 0
        )
        output_tokens = (
            getattr(usage, "output_tokens", None) or getattr(usage, "completion_tokens", None) or 0
        )
        token_details = getattr(usage, "input_tokens_details", None) or getattr(
            usage, "prompt_tokens_details", None
        )
        cached_tokens = (
            getattr(token_details, "cached_tokens", 0)
            if token_details is not None
            else getattr(usage, "cache_read_input_tokens", 0) or 0
        )
        try:
            return {
                "input_tokens": max(0, int(input_tokens or 0)),
                "output_tokens": max(0, int(output_tokens or 0)),
                "cached_tokens": max(0, int(cached_tokens or 0)),
            }
        except (TypeError, ValueError):
            return None

    def _notify_usage_listeners(self, entry: dict[str, Any]) -> None:
        for fn in self._usage_listeners:
            try:
                fn(entry)
            except Exception as e:
                logger.warning(f"LongTermMemory usage listener raised, ignored: {e}")

    def _install_raw_usage_hook(self, llm: Any) -> None:
        def wrap_create(owner: Any) -> None:
            original = getattr(owner, "create", None)
            if not callable(original) or getattr(original, "_coworker_usage_wrapped", False):
                return

            @wraps(original)
            def tracked_create(*args, **kwargs):
                response = original(*args, **kwargs)
                usage = self._extract_response_usage(response)
                if usage is not None:
                    setattr(llm, "_coworker_last_usage", usage)
                    setattr(llm, "_coworker_last_usage_source", "provider")
                return response

            setattr(tracked_create, "_coworker_usage_wrapped", True)
            try:
                setattr(owner, "create", tracked_create)
            except Exception as e:
                logger.debug(f"Could not install mem0 raw usage hook: {e}")

        client = getattr(llm, "client", None)
        if client is None:
            return
        chat_completions = getattr(getattr(client, "chat", None), "completions", None)
        if chat_completions is not None:
            wrap_create(chat_completions)
        messages = getattr(client, "messages", None)
        if messages is not None:
            wrap_create(messages)

    def _install_usage_hook(self) -> None:
        if self._mem is None or self._usage_hook_installed:
            return
        llm = getattr(self._mem, "llm", None)
        generate = getattr(llm, "generate_response", None)
        if llm is None or not callable(generate):
            return
        self._install_raw_usage_hook(llm)

        def tracked_generate_response(*args, **kwargs):
            messages = kwargs.get("messages")
            if messages is None and args:
                messages = args[0]
            setattr(llm, "_coworker_last_usage", None)
            setattr(llm, "_coworker_last_usage_source", None)
            response = generate(*args, **kwargs)
            usage = getattr(llm, "_coworker_last_usage", None)
            usage_source = getattr(llm, "_coworker_last_usage_source", None)
            if usage is None:
                usage = {
                    "input_tokens": self._estimate_mem0_messages_tokens(messages),
                    "output_tokens": self._estimate_mem0_response_tokens(response),
                    "cached_tokens": 0,
                }
                usage_source = "estimated"
            self._notify_usage_listeners(
                {
                    "provider": getattr(self._llm, "provider", "unknown"),
                    "model": getattr(getattr(llm, "config", None), "model", None)
                    or getattr(self._llm, "model", "unknown"),
                    "usage": usage,
                    "usage_source": usage_source,
                    "operation": "generate_response",
                }
            )
            return response

        setattr(llm, "generate_response", tracked_generate_response)
        self._usage_hook_installed = True

    async def write(
        self,
        content: str,
        *,
        category: str,
        tags: list[str] | None = None,
        source_timestamp: datetime | None = None,
    ) -> MemoryWriteResult:
        if self._mem is None:
            raise RuntimeError("LongTermMemory not initialized")
        if not content.strip():
            return MemoryWriteResult(status="empty")
        metadata: dict = {
            "category": category,
            "tags": json.dumps(tags or []),
            "source_timestamp": (source_timestamp or datetime.now()).isoformat(),
        }
        content_hash = hashlib.md5(content.encode(), usedforsecurity=False).hexdigest()
        async with self._write_lock:
            existing = await self._mem.get_all(
                filters={"user_id": _AGENT_USER_ID, "hash": content_hash}
            )
            if any(item.get("memory") == content for item in existing.get("results", [])):
                logger.debug(f"Memory already exists [{category}]: {content[:60]}...")
                return MemoryWriteResult(status="empty")
            result = await self._mem.add(
                messages=[{"role": "user", "content": content}],
                user_id=_AGENT_USER_ID,
                metadata=metadata,
                infer=False,
            )
        ids = [r["id"] for r in result.get("results", []) if "id" in r]
        memory_id = ids[0] if ids else ""
        if memory_id:
            logger.debug(f"Memory written [{category}]: {content[:60]}...")
            return MemoryWriteResult(status="written", memory_id=memory_id)
        logger.debug(f"Memory not stored [{category}]: {content[:60]}...")
        return MemoryWriteResult(status="empty")

    async def query(self, params: MemoryQuery) -> list[MemoryRecord]:
        if self._mem is None:
            raise RuntimeError("LongTermMemory not initialized")
        filters: dict = {"user_id": _AGENT_USER_ID}
        if params.category:
            filters["metadata.category"] = params.category
        top_k = (
            max(params.limit * 6, 30)
            if (params.start is not None or params.end is not None)
            else (max(params.limit * 4, 20) if params.tags else params.limit)
        )
        results = await self._mem.search(query=params.text, filters=filters, top_k=top_k)
        memories: list[MemoryRecord] = []
        relevance_threshold = self.relevance_threshold
        for item in results.get("results", []):
            meta = item.get("metadata") or {}
            raw_score = item.get("score")
            score = (
                float(raw_score)
                if isinstance(raw_score, (int, float)) and not isinstance(raw_score, bool)
                else None
            )
            if score is None or score < relevance_threshold:
                continue
            raw_tags = meta.get("tags", "[]")
            try:
                tags = json.loads(raw_tags) if isinstance(raw_tags, str) else list(raw_tags or [])
            except (ValueError, TypeError):
                tags = []
            memories.append(
                MemoryRecord(
                    id=item.get("id", ""),
                    content=item.get("memory", ""),
                    category=meta.get("category", "general"),
                    tags=tags,
                    timestamp=meta.get("source_timestamp") or item.get("created_at", "") or None,
                    extra={"score": score},
                )
            )
        if params.tags:
            tag_set = set(params.tags)
            memories = [m for m in memories if tag_set.intersection(m.tags)]
        if params.start is not None or params.end is not None:
            memories = [
                m for m in memories if self._timestamp_in_range(m.timestamp, params.start, params.end)
            ]
        return memories[: params.limit]

    @staticmethod
    def _timestamp_in_range(
        value: str | None, start: datetime | None, end: datetime | None
    ) -> bool:
        if not value:
            return False
        try:
            ts = datetime.fromisoformat(str(value))
        except (TypeError, ValueError):
            return False
        return (start is None or ts >= start) and (end is None or ts <= end)

    async def _read_memory(self, memory_id: str) -> dict | None:
        if self._mem is None:
            raise RuntimeError("LongTermMemory not initialized")
        item = await self._mem.get(memory_id)
        if not item:
            return None
        meta = item.get("metadata") or {}
        raw_tags = meta.get("tags", "[]")
        try:
            tags = json.loads(raw_tags) if isinstance(raw_tags, str) else list(raw_tags or [])
        except (ValueError, TypeError):
            tags = []
        return {
            "content": item.get("memory", ""),
            "category": meta.get("category", "general"),
            "tags": tags,
            "source_timestamp": meta.get("source_timestamp"),
        }

    @staticmethod
    def _metadata_payload(category: str, tags: list[str], source_timestamp: Any) -> dict:
        md: dict = {"category": category, "tags": json.dumps(tags or [])}
        if source_timestamp:
            md["source_timestamp"] = source_timestamp
        return md

    async def update(
        self,
        memory_id: str,
        content: str,
        *,
        tags: list[str] | None = None,
    ) -> None:
        if self._mem is None:
            raise RuntimeError("LongTermMemory not initialized")
        existing = await self._read_memory(memory_id)
        metadata = (
            self._metadata_payload(
                existing["category"],
                existing["tags"] if tags is None else tags,
                existing["source_timestamp"],
            )
            if existing is not None
            else None
        )
        async with self._write_lock:
            await self._mem.update(memory_id=memory_id, data=content, metadata=metadata)
        logger.debug(f"Memory updated [{memory_id}]: {content[:60]}...")

    async def associate_tags(self, memory_id: str, tags: list[str]) -> list[str]:
        if self._mem is None:
            raise RuntimeError("LongTermMemory not initialized")
        if not tags:
            raise ValueError("associate_tags requires non-empty tags")
        existing = await self._read_memory(memory_id)
        if existing is None:
            raise ValueError(f"Memory does not exist: {memory_id}")
        merged = list(existing["tags"])
        added = [t for t in tags if t not in merged]
        if not added:
            return merged
        merged.extend(added)
        metadata = self._metadata_payload(
            existing["category"], merged, existing["source_timestamp"]
        )
        async with self._write_lock:
            await self._mem.update(memory_id=memory_id, data=existing["content"], metadata=metadata)
        logger.debug(f"Memory tags associated [{memory_id}]: +{added} → {merged}")
        return merged

    async def delete(self, memory_id: str) -> None:
        if self._mem is None:
            raise RuntimeError("LongTermMemory not initialized")
        async with self._write_lock:
            await self._mem.delete(memory_id=memory_id)
        logger.debug(f"Memory deleted [{memory_id}]")

    async def count(self) -> int:
        if self._mem is None:
            return 0
        result = await self._mem.get_all(filters={"user_id": _AGENT_USER_ID})
        return len(result.get("results", []))


class _EmptyMemoryConfig:
    """Fallback matching the historical default when no memory LLM config is supplied."""

    provider = "anthropic"
    api_dialect = "anthropic"
    api_key = ""
    model = "claude-haiku-4-5-20251001"
    base_url = ""
    thinking = False


def _long_term_custom_instructions() -> str:
    from coworker.i18n import tr

    return tr("long_term.custom_instructions")
