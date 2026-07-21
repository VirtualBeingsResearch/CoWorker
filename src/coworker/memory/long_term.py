from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from datetime import datetime
from functools import wraps
from pathlib import Path
from typing import Any

from loguru import logger

from coworker.core.token_utils import estimate_content_tokens, estimate_text_tokens
from coworker.i18n import tr

_AGENT_USER_ID = "agent"
_DEFAULT_EMBEDDER = "sentence-transformers/paraphrase-multilingual-mpnet-base-v2"
_UsageListener = Callable[[dict[str, Any]], None]


class LongTermMemory:
    def __init__(
        self,
        db_path: str,
        llm_provider: str = "anthropic",
        llm_api_key: str = "",
        llm_model: str = "claude-haiku-4-5-20251001",
        embedder_model: str = _DEFAULT_EMBEDDER,
    ) -> None:
        self._db_path = Path(db_path)
        self._mem = None
        self._llm_provider = llm_provider
        self._llm_api_key = llm_api_key
        self._llm_model = llm_model
        self._embedder_model = embedder_model
        self._write_lock = asyncio.Lock()
        self._usage_listeners: list[_UsageListener] = []
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

    async def initialize(self) -> None:
        from mem0 import AsyncMemory

        config = {
            "custom_instructions": tr("mem0.custom_instructions"),
            "llm": {
                "provider": self._llm_provider,
                "config": {"model": self._llm_model, "api_key": self._llm_api_key},
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
            f"embedder={self._embedder_model}, device={device}"
        )

    def add_usage_listener(self, fn: _UsageListener) -> None:
        self._usage_listeners.append(fn)

    @staticmethod
    def _estimate_mem0_messages_tokens(messages: Any) -> int:
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
                    "provider": self._llm_provider,
                    "model": getattr(getattr(llm, "config", None), "model", None)
                    or self._llm_model,
                    "usage": usage,
                    "usage_source": usage_source,
                    "operation": "generate_response",
                }
            )
            return response

        setattr(llm, "generate_response", tracked_generate_response)
        self._usage_hook_installed = True

    async def migrate_embeddings(self, new_model: str) -> int:
        """Rebuild all memories under a new embedding model via mem0 API.

        Creates a backup of db_path before touching anything. On failure the
        backup is automatically restored and the original model stays active.
        Returns the number of rebuilt entries.
        """
        import shutil
        from datetime import datetime

        if self._mem is None:
            raise RuntimeError("LongTermMemory not initialized")

        result = await self._mem.get_all(filters={"user_id": _AGENT_USER_ID})
        memories = result.get("results", [])

        if not memories:
            logger.info("No memories to migrate — switching embedder model")
            self._embedder_model = new_model
            self._mem = None
            await self.initialize()
            return 0

        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        backup_path = self._db_path.parent / f"{self._db_path.name}_backup_{timestamp}"
        shutil.copytree(self._db_path, backup_path)
        logger.info(f"Backup created at {backup_path}")

        original_model = self._embedder_model
        logger.info(f"Rebuilding {len(memories)} memories: {original_model} → {new_model}")

        try:
            await self._mem.delete_all(user_id=_AGENT_USER_ID)
            self._mem.vector_store.delete_col()

            self._embedder_model = new_model
            self._mem = None
            await self.initialize()

            for item in memories:
                memory_text = item.get("memory", "")
                if not memory_text:
                    continue
                metadata = item.get("metadata") or {}
                category = metadata.get("category", "general")
                tags = json.loads(metadata.get("tags", "[]"))
                await self.write(memory_text, category=category, tags=tags)

        except Exception:
            logger.error("Migration failed — restoring backup")
            self._mem = None
            if self._db_path.exists():
                shutil.rmtree(self._db_path)
            shutil.copytree(backup_path, self._db_path)
            self._embedder_model = original_model
            await self.initialize()
            logger.info("Backup restored successfully")
            raise

        logger.info(f"Rebuild complete: {len(memories)} memories re-added with {new_model}")
        return len(memories)

    async def write(
        self,
        content: str,
        category: str = "general",
        tags: list[str] | None = None,
        source_timestamp: datetime | None = None,
    ) -> str:
        if self._mem is None:
            raise RuntimeError("LongTermMemory not initialized")
        metadata: dict = {
            "category": category,
            "tags": json.dumps(tags or []),
            "source_timestamp": (source_timestamp or datetime.now()).isoformat(),
        }
        async with self._write_lock:
            result = await self._mem.add(
                messages=[{"role": "user", "content": content}],
                user_id=_AGENT_USER_ID,
                metadata=metadata,
            )
        ids = [r["id"] for r in result.get("results", []) if "id" in r]
        memory_id = ids[0] if ids else ""
        logger.debug(f"Memory written [{category}]: {content[:60]}...")
        return memory_id

    async def add_conversation(self, messages: list) -> None:
        """将一段对话批量传给 mem0，由其自动提炼并存储事实。"""
        if self._mem is None:
            raise RuntimeError("LongTermMemory not initialized")
        # TODO: 当 mem0 支持多模态时，对 image/document 类型的 content block 也一并传入
        formatted = []
        for m in messages:
            if m.role in ("user", "assistant"):
                parts = []
                text = (
                    m.content_text()
                    if hasattr(m, "content_text")
                    else (m.content if isinstance(m.content, str) else "")
                )
                if text.strip():
                    ts = m.timestamp.strftime("%Y-%m-%d %H:%M") if hasattr(m, "timestamp") else ""
                    parts.append(f"[{ts}] {text}" if ts else text)
                for tc in m.tool_calls or []:
                    func = tc.get("function", {})
                    name = func.get("name", "")
                    args = func.get("arguments", "")
                    if isinstance(args, str):
                        try:
                            args = json.loads(args)
                        except Exception:
                            pass
                    args_str = (
                        json.dumps(args, ensure_ascii=False)[:200]
                        if isinstance(args, dict)
                        else str(args)[:200]
                    )
                    parts.append(tr("mem0.tool_call", name=name, arguments=args_str))
                if parts:
                    formatted.append({"role": m.role, "content": "\n".join(parts)})
            elif m.role == "tool" and isinstance(m.content, str) and m.content.strip():
                formatted.append(
                    {
                        "role": "user",
                        "content": tr("mem0.tool_result", content=m.content[:500]),
                    }
                )
        if not formatted:
            return
        async with self._write_lock:
            await self._mem.add(messages=formatted, user_id=_AGENT_USER_ID)
        logger.debug(f"Conversation batch ({len(formatted)} msgs) added to mem0")

    async def _read_memory(self, memory_id: str) -> dict | None:
        """读取一条记忆的正文与自定义 metadata（category/tags/source_timestamp）。

        mem0 的 update 会用传入的 metadata **全置换** payload（仅保留 user_id/created_at
        等会话标识），因此更新前必须先取回这些字段一并回填，否则会被抹掉。memory 不存在
        返回 None。
        """
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
    def _metadata_payload(category: str, tags: list[str], source_timestamp) -> dict:
        """组装 update 用的完整 metadata，避免漏字段被 mem0 全置换抹掉。"""
        md: dict = {"category": category, "tags": json.dumps(tags or [])}
        if source_timestamp:
            md["source_timestamp"] = source_timestamp
        return md

    async def update(self, memory_id: str, content: str, *, tags: list[str] | None = None) -> None:
        if self._mem is None:
            raise RuntimeError("LongTermMemory not initialized")
        # 取回原 metadata 一并回填，否则 mem0 的全置换会抹掉 category/tags/source_timestamp。
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
        """给已有记忆追加标签（不改正文），返回合并去重后的完整标签列表。

        供宫殿园丁把语义上属于某宫殿、却未挂该宫殿标签的「孤儿记忆」关联进宫殿。已含全部
        目标标签则视为无操作直接返回；写入时带上完整 metadata，避免抹掉 category/source_timestamp。
        """
        if self._mem is None:
            raise RuntimeError("LongTermMemory not initialized")
        if not tags:
            raise ValueError(tr("memory.manage.associate_tags_empty"))
        existing = await self._read_memory(memory_id)
        if existing is None:
            raise ValueError(tr("memory.manage.missing", id=memory_id))
        merged = list(existing["tags"])
        added = [t for t in tags if t not in merged]
        if not added:
            return merged  # 已含全部目标标签，无需写
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

    async def query(
        self,
        query_text: str,
        category: str | None = None,
        tags: list[str] | None = None,
        limit: int = 10,
        start: datetime | None = None,
        end: datetime | None = None,
    ) -> list[dict]:
        """语义检索。category 走 mem0 原生过滤；tags 因以 JSON 串存于 metadata 无法直接过滤，
        故在有 tags 时宽召回后于 Python 侧按标签交集后置过滤，再截断到 limit。"""
        if self._mem is None:
            raise RuntimeError("LongTermMemory not initialized")
        filters: dict = {"user_id": _AGENT_USER_ID}
        if category:
            filters["metadata.category"] = category
        # 标签需后置过滤，会筛掉一部分，故预取更多以保证过滤后仍有足量结果。
        top_k = (
            max(limit * 6, 30)
            if (start is not None or end is not None)
            else (max(limit * 4, 20) if tags else limit)
        )
        results = await self._mem.search(query=query_text, filters=filters, top_k=top_k)
        memories = []
        for item in results.get("results", []):
            meta = item.get("metadata") or {}
            memories.append(
                {
                    "id": item.get("id", ""),
                    "content": item.get("memory", ""),
                    "category": meta.get("category", "general"),
                    "tags": json.loads(meta.get("tags", "[]")),
                    "timestamp": meta.get("source_timestamp") or item.get("created_at", ""),
                    "relevance": round(item.get("score", 1.0), 4),
                }
            )
        if tags:
            tag_set = set(tags)
            memories = [m for m in memories if tag_set.intersection(m.get("tags") or [])]
        if start is not None or end is not None:
            memories = [
                m for m in memories if self._timestamp_in_range(m.get("timestamp"), start, end)
            ]
        return memories[:limit]

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

    async def query_by_tags(
        self,
        query_text: str,
        tags: list[str],
        limit: int = 8,
    ) -> list[dict]:
        """语义检索后按标签交集过滤，按相关度降序返回前 limit 条。

        mem0 的 tags 以 JSON 字符串存于 metadata，无法直接作为过滤器，故先做一次
        宽召回（top_k 放大）再在 Python 侧按标签交集后置过滤。tags 为空则直接返回 []。
        """
        if not tags:
            return []
        # 宽召回：标签过滤会筛掉一部分，预取更多以保证过滤后仍有足量结果。
        results = await self.query(query_text, limit=max(limit * 4, 20))
        tag_set = set(tags)
        matched = [m for m in results if tag_set.intersection(m.get("tags") or [])]
        matched.sort(key=lambda m: m.get("relevance", 0.0), reverse=True)
        return matched[:limit]

    async def count(self) -> int:
        """返回当前存储的记忆总数（不加载具体内容）。"""
        if self._mem is None:
            return 0
        result = await self._mem.get_all(filters={"user_id": _AGENT_USER_ID})
        return len(result.get("results", []))
