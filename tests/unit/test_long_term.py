from __future__ import annotations

import json
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock

from coworker.memory.backends.mem0 import Mem0Backend
from coworker.memory.base import (
    MemoryBackendConfig,
    MemoryQuery,
    MemoryRecord,
    MemoryWriteResult,
)
from coworker.memory.long_term import LongTermLLMConfig, LongTermMemory


class FakeBackend:
    """Minimal in-test backend for facade delegation checks."""

    def __init__(self) -> None:
        self.initialized = False
        self.records: dict[str, MemoryRecord] = {}
        self.listeners = []
        self.reconfigured_with = None

    async def initialize(self) -> None:
        self.initialized = True

    def is_ready(self) -> bool:
        return self.initialized

    async def write(
        self,
        content: str,
        *,
        category: str,
        tags: list[str],
        source_timestamp: datetime | None = None,
    ) -> MemoryWriteResult:
        record = MemoryRecord(
            id=f"id-{len(self.records) + 1}",
            content=content,
            category=category,
            tags=tags,
            timestamp=(source_timestamp or datetime.now()).isoformat(),
        )
        self.records[record.id] = record
        return MemoryWriteResult(status="written", memory_id=record.id)

    async def query(self, params: MemoryQuery) -> list[MemoryRecord]:
        return list(self.records.values())[: params.limit]

    async def update(
        self,
        memory_id: str,
        content: str,
        *,
        tags: list[str] | None = None,
    ) -> None:
        record = self.records[memory_id]
        self.records[memory_id] = MemoryRecord(
            id=record.id,
            content=content,
            category=record.category,
            tags=record.tags if tags is None else tags,
            timestamp=record.timestamp,
        )

    async def delete(self, memory_id: str) -> None:
        self.records.pop(memory_id, None)

    async def associate_tags(self, memory_id: str, tags: list[str]) -> list[str]:
        record = self.records[memory_id]
        merged = list(dict.fromkeys([*record.tags, *tags]))
        self.records[memory_id] = MemoryRecord(
            id=record.id,
            content=record.content,
            category=record.category,
            tags=merged,
            timestamp=record.timestamp,
        )
        return merged

    async def reconfigure(self, config: MemoryBackendConfig) -> None:
        self.reconfigured_with = config

    def add_usage_listener(self, listener) -> None:
        self.listeners.append(listener)

    async def count(self) -> int:
        return len(self.records)


class TestLongTermMemoryFacade:
    def _make(self) -> tuple[LongTermMemory, FakeBackend]:
        backend = FakeBackend()
        memory = LongTermMemory(backend=backend)
        return memory, backend

    async def test_initialize_and_is_ready(self):
        memory, backend = self._make()
        assert not memory.is_ready()
        await memory.initialize()
        assert memory.is_ready()
        assert backend.initialized

    async def test_write_delegates(self):
        memory, backend = self._make()
        result = await memory.write("hello", category="general", tags=["a"])
        assert result.status == "written"
        assert result.memory_id in backend.records

    async def test_query_delegates_and_returns_records(self):
        memory, backend = self._make()
        await memory.write("hello", category="general", tags=["a"])
        records = await memory.query("hello")
        assert isinstance(records[0], MemoryRecord)
        assert records[0].content == "hello"

    async def test_query_by_tags_uses_query_with_tags(self):
        memory, _ = self._make()
        calls = []

        async def fake_query(query_text, **kwargs):
            calls.append((query_text, kwargs))
            return [MemoryRecord(id="m1", content="x", category="general", tags=["t"])]

        memory.query = fake_query
        result = await memory.query_by_tags("goal", ["t"], limit=8)
        assert len(result) == 1
        assert calls == [("goal", {"tags": ["t"], "limit": 8})]

    async def test_update_delete_associate_count_delegate(self):
        memory, backend = self._make()
        result = await memory.write("content", category="general", tags=["a"])
        await memory.update(result.memory_id, "updated", tags=["b"])
        assert backend.records[result.memory_id].content == "updated"
        assert backend.records[result.memory_id].tags == ["b"]

        merged = await memory.associate_tags(result.memory_id, ["c"])
        assert merged == ["b", "c"]

        assert await memory.count() == 1
        await memory.delete(result.memory_id)
        assert await memory.count() == 0

    async def test_reconfigure_and_usage_listener_delegate(self):
        memory, backend = self._make()

        def listener(entry):
            return None

        memory.add_usage_listener(listener)
        config = LongTermLLMConfig(provider="openai", api_dialect="openai", model="m")
        await memory.reconfigure(config)
        assert backend.reconfigured_with is config
        assert listener in backend.listeners


def test_llm_config_preserves_provider_base_url():
    llm = LongTermLLMConfig(
        provider="deepseek",
        api_dialect="openai",
        api_key="secret",
        model="model-id",
        base_url="https://llm.example.test/v1",
    )

    resolved_provider, config = llm.as_mem0_config()

    assert resolved_provider == "openai"
    assert config == {
        "model": "model-id",
        "api_key": "secret",
        "openai_base_url": "https://llm.example.test/v1",
        "thinking": False,
        "coworker_provider": "deepseek",
    }

    from mem0.configs.base import MemoryConfig

    assert MemoryConfig(llm={"provider": resolved_provider, "config": config}).llm.provider == "openai"


class TestMem0Backend:
    def _make(self) -> Mem0Backend:
        return Mem0Backend(db_path="data/_unused")

    def test_embedder_returns_mem0_embedding_model(self):
        backend = self._make()
        embedder = object()
        backend._mem = MagicMock()
        backend._mem.embedding_model = embedder
        assert backend.embedder is embedder

    def test_chroma_client_returns_mem0_vector_store_client(self):
        backend = self._make()
        client = object()
        backend._mem = MagicMock()
        backend._mem.vector_store.client = client
        assert backend.chroma_client is client

    def test_is_ready_reflects_mem(self):
        backend = self._make()
        assert not backend.is_ready()
        backend._mem = MagicMock()
        assert backend.is_ready()

    async def test_write_returns_written_and_empty(self):
        backend = self._make()
        backend._mem = MagicMock()
        backend._mem.get_all = AsyncMock(return_value={"results": []})
        backend._mem.add = AsyncMock(return_value={"results": [{"id": "new-1"}]})

        result = await backend.write("新内容", category="knowledge", tags=["a"])
        assert result.status == "written"
        assert result.memory_id == "new-1"
        assert backend._mem.add.await_args.kwargs["infer"] is False

        backend._mem.get_all = AsyncMock(
            return_value={"results": [{"id": "old-1", "memory": "已有内容"}]}
        )
        backend._mem.add = AsyncMock()
        result = await backend.write("已有内容", category="knowledge")
        assert result == MemoryWriteResult(status="empty")
        backend._mem.add.assert_not_awaited()

    async def test_query_returns_memory_records(self):
        backend = self._make()
        backend._mem = MagicMock()
        backend._mem.search = AsyncMock(
            return_value={
                "results": [
                    {
                        "id": "m1",
                        "memory": "content",
                        "metadata": {
                            "category": "experience",
                            "tags": json.dumps(["a"]),
                            "source_timestamp": "2026-06-01T00:00:00",
                        },
                        "score": 0.9,
                    }
                ]
            }
        )
        records = await backend.query(MemoryQuery("content", tags=["a"], limit=5))
        assert len(records) == 1
        assert isinstance(records[0], MemoryRecord)
        assert records[0].id == "m1"
        assert records[0].tags == ["a"]
        assert records[0].timestamp == "2026-06-01T00:00:00"

    async def test_update_preserves_metadata(self):
        backend = self._make()
        backend._mem = MagicMock()
        backend._mem.get = AsyncMock(
            return_value={
                "id": "m1",
                "memory": "old",
                "metadata": {
                    "category": "knowledge",
                    "tags": json.dumps(["t1"]),
                    "source_timestamp": "2026-06-01T00:00:00",
                },
            }
        )
        backend._mem.update = AsyncMock()
        await backend.update("m1", "new content")
        kwargs = backend._mem.update.await_args.kwargs
        assert kwargs["data"] == "new content"
        assert json.loads(kwargs["metadata"]["tags"]) == ["t1"]
        assert kwargs["metadata"]["category"] == "knowledge"

    async def test_associate_tags_preserves_metadata(self):
        backend = self._make()
        backend._mem = MagicMock()
        backend._mem.get = AsyncMock(
            return_value={
                "id": "m1",
                "memory": "content",
                "metadata": {
                    "category": "experience",
                    "tags": json.dumps(["product"]),
                    "source_timestamp": "2026-06-01T00:00:00",
                },
            }
        )
        backend._mem.update = AsyncMock()
        merged = await backend.associate_tags("m1", ["bug", "product"])
        assert merged == ["product", "bug"]
        kwargs = backend._mem.update.await_args.kwargs
        assert kwargs["data"] == "content"
        assert json.loads(kwargs["metadata"]["tags"]) == ["product", "bug"]

    async def test_reconfigure_replaces_mem0_llm_instance(self, monkeypatch):
        import mem0.utils.factory as mem0_factory

        backend = self._make()
        fake_llm = MagicMock()
        fake_llm.generate_response = MagicMock()
        monkeypatch.setattr(mem0_factory.LlmFactory, "create", lambda provider, cfg: fake_llm)
        backend._mem = MagicMock()

        new_cfg = LongTermLLMConfig(
            provider="deepseek",
            api_dialect="openai",
            api_key="k",
            model="deepseek-v4-pro",
            base_url="",
            thinking=True,
        )
        await backend.reconfigure(new_cfg)

        assert backend._mem.llm is fake_llm
        assert backend._mem.config.llm.provider == "openai"
        assert backend._mem.config.llm.config["model"] == "deepseek-v4-pro"
        assert backend._mem.config.llm.config["thinking"] is True

    async def test_reconfigure_deferred_when_not_initialized(self):
        backend = self._make()
        new_cfg = LongTermLLMConfig(provider="deepseek", api_dialect="openai", model="m")
        await backend.reconfigure(new_cfg)
        assert backend._llm is new_cfg

    def test_usage_hook_notifies_listener(self):
        class FakeLlm:
            class Config:
                model = "mem-model"

            config = Config()

            def generate_response(self, messages, **kwargs):
                return "extracted memory"

        backend = self._make()
        backend._mem = MagicMock()
        backend._mem.llm = FakeLlm()
        seen = []
        backend.add_usage_listener(seen.append)

        backend._install_usage_hook()
        backend._mem.llm.generate_response(messages=[{"role": "user", "content": "用户喜欢喝咖啡"}])

        assert len(seen) == 1
        assert seen[0]["provider"] == "anthropic"
        assert seen[0]["model"] == "mem-model"
        assert seen[0]["operation"] == "generate_response"
        assert seen[0]["usage_source"] == "estimated"
        assert seen[0]["usage"]["input_tokens"] > 0
        assert seen[0]["usage"]["output_tokens"] > 0
