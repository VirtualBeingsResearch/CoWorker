from __future__ import annotations

import json
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock

import pytest
from mem0.configs.base import MemoryConfig

from coworker.memory.long_term import LongTermLLMConfig, LongTermMemory


@pytest.fixture(autouse=True)
def isolate_relative_storage(tmp_path, monkeypatch):
    """Keep vector-store test paths out of the repository's ``data/`` tree."""
    monkeypatch.chdir(tmp_path)


def _mem(id_: str, tags: list[str], relevance: float) -> dict:
    return {
        "id": id_,
        "content": f"memory {id_}",
        "category": "experience",
        "tags": tags,
        "timestamp": "",
        "relevance": relevance,
    }


def test_embedder_returns_mem0_embedding_model():
    lt = LongTermMemory(db_path="data/_unused")
    embedder = object()
    lt._mem = MagicMock()
    lt._mem.embedding_model = embedder

    assert lt.embedder is embedder


def test_chroma_client_returns_mem0_vector_store_client():
    lt = LongTermMemory(db_path="data/_unused")
    client = object()
    lt._mem = MagicMock()
    lt._mem.vector_store.client = client

    assert lt.chroma_client is client


class TestQueryByTags:
    def _make(self) -> LongTermMemory:
        return LongTermMemory(db_path="data/_unused")

    async def test_empty_tags_returns_empty(self):
        lt = self._make()
        lt.query = AsyncMock()
        assert await lt.query_by_tags("q", []) == []
        lt.query.assert_not_awaited()

    async def test_filters_by_tag_intersection(self):
        lt = self._make()
        lt.query = AsyncMock(return_value=[
            _mem("a", ["product", "bug"], 0.9),
            _mem("b", ["other"], 0.8),
            _mem("c", ["bug"], 0.7),
        ])
        out = await lt.query_by_tags("q", ["bug"])
        ids = [m["id"] for m in out]
        assert ids == ["a", "c"]  # b filtered out; order by relevance desc

    async def test_sorted_by_relevance_desc(self):
        lt = self._make()
        lt.query = AsyncMock(return_value=[
            _mem("low", ["t"], 0.3),
            _mem("high", ["t"], 0.95),
            _mem("mid", ["t"], 0.6),
        ])
        out = await lt.query_by_tags("q", ["t"])
        assert [m["id"] for m in out] == ["high", "mid", "low"]

    async def test_respects_limit(self):
        lt = self._make()
        lt.query = AsyncMock(return_value=[_mem(str(i), ["t"], 1.0 - i * 0.01) for i in range(20)])
        out = await lt.query_by_tags("q", ["t"], limit=5)
        assert len(out) == 5


class TestQueryWithTags:
    def _search_item(self, id_: str, tags: list[str], score: float) -> dict:
        return {
            "id": id_,
            "memory": f"memory {id_}",
            "metadata": {"category": "experience", "tags": json.dumps(tags)},
            "score": score,
        }

    async def test_tags_post_filter_and_limit(self):
        lt = LongTermMemory(db_path="data/_unused")
        mem = MagicMock()
        mem.search = AsyncMock(return_value={"results": [
            self._search_item("a", ["product", "bug"], 0.9),
            self._search_item("b", ["other"], 0.8),
            self._search_item("c", ["bug"], 0.7),
        ]})
        lt._mem = mem
        out = await lt.query("q", tags=["bug"], limit=5)
        assert [m["id"] for m in out] == ["a", "c"]  # b filtered out
        # 有 tags 时宽召回：top_k 放大
        assert mem.search.await_args.kwargs["top_k"] == 20

    async def test_no_tags_uses_limit_as_top_k(self):
        lt = LongTermMemory(db_path="data/_unused")
        mem = MagicMock()
        mem.search = AsyncMock(return_value={"results": []})
        lt._mem = mem
        await lt.query("q", limit=5)
        assert mem.search.await_args.kwargs["top_k"] == 5

    async def test_time_filter_uses_source_timestamp_and_expands_top_k(self):
        lt = LongTermMemory(db_path="data/_unused")
        mem = MagicMock()
        mem.search = AsyncMock(return_value={"results": [
            {
                "id": "in",
                "memory": "inside",
                "metadata": {
                    "category": "task",
                    "tags": "[]",
                    "source_timestamp": "2026-06-02T12:00:00",
                },
                "score": 0.9,
            },
            {
                "id": "out",
                "memory": "outside",
                "metadata": {
                    "category": "task",
                    "tags": "[]",
                    "source_timestamp": "2026-06-05T12:00:00",
                },
                "score": 0.8,
            },
            {
                "id": "bad",
                "memory": "bad ts",
                "metadata": {"category": "task", "tags": "[]", "source_timestamp": "not-a-date"},
                "score": 0.7,
            },
        ]})
        lt._mem = mem

        out = await lt.query(
            "q",
            limit=2,
            start=datetime(2026, 6, 1),
            end=datetime(2026, 6, 3),
        )

        assert [m["id"] for m in out] == ["in"]
        assert mem.search.await_args.kwargs["top_k"] == 30


def _lt_with_mem(get_return) -> tuple[LongTermMemory, MagicMock]:
    lt = LongTermMemory(db_path="data/_unused")
    mem = MagicMock()
    mem.get = AsyncMock(return_value=get_return)
    mem.update = AsyncMock()
    lt._mem = mem
    return lt, mem


def _get_item(memory: str, category: str, tags: list[str], ts: str | None = "2026-06-01T00:00:00") -> dict:
    meta: dict = {"category": category, "tags": json.dumps(tags)}
    if ts is not None:
        meta["source_timestamp"] = ts
    return {"id": "m1", "memory": memory, "metadata": meta}


class TestAssociateTags:
    async def test_appends_and_preserves_metadata(self):
        lt, mem = _lt_with_mem(_get_item("登录复现要点", "experience", ["product"]))
        merged = await lt.associate_tags("m1", ["bug", "product"])  # product already present → dedup
        assert merged == ["product", "bug"]
        mem.update.assert_awaited_once()
        kwargs = mem.update.await_args.kwargs
        assert kwargs["memory_id"] == "m1"
        assert kwargs["data"] == "登录复现要点"  # content untouched
        md = kwargs["metadata"]
        assert md["category"] == "experience"           # category preserved
        assert json.loads(md["tags"]) == ["product", "bug"]
        assert md["source_timestamp"] == "2026-06-01T00:00:00"  # source_timestamp preserved

    async def test_noop_when_all_tags_present(self):
        lt, mem = _lt_with_mem(_get_item("x", "general", ["a", "b"]))
        merged = await lt.associate_tags("m1", ["a"])
        assert merged == ["a", "b"]
        mem.update.assert_not_awaited()  # nothing new → no write

    async def test_missing_memory_raises(self):
        lt, _ = _lt_with_mem(None)
        with pytest.raises(ValueError):
            await lt.associate_tags("nope", ["a"])

    async def test_empty_tags_raises(self):
        lt, mem = _lt_with_mem(_get_item("x", "general", []))
        with pytest.raises(ValueError):
            await lt.associate_tags("m1", [])
        mem.get.assert_not_awaited()


class TestUpdatePreservesMetadata:
    async def test_update_carries_back_metadata(self):
        lt, mem = _lt_with_mem(_get_item("old", "knowledge", ["t1"]))
        await lt.update("m1", "new content")
        kwargs = mem.update.await_args.kwargs
        assert kwargs["data"] == "new content"
        md = kwargs["metadata"]
        assert md["category"] == "knowledge"          # not wiped
        assert json.loads(md["tags"]) == ["t1"]        # not wiped
        assert md["source_timestamp"] == "2026-06-01T00:00:00"

    async def test_update_missing_memory_passes_none_metadata(self):
        lt, mem = _lt_with_mem(None)
        await lt.update("gone", "new content")
        kwargs = mem.update.await_args.kwargs
        assert kwargs["metadata"] is None  # nothing to preserve

    async def test_update_can_replace_tags(self):
        lt, mem = _lt_with_mem(_get_item("old", "knowledge", ["old-tag"]))
        await lt.update("m1", "new content", tags=["project", "decision"])
        md = mem.update.await_args.kwargs["metadata"]
        assert md["category"] == "knowledge"
        assert json.loads(md["tags"]) == ["project", "decision"]
        assert md["source_timestamp"] == "2026-06-01T00:00:00"


class TestUsageHook:
    def test_mem0_generate_response_notifies_usage_listener(self):
        class FakeLlm:
            class Config:
                model = "mem-model"

            config = Config()

            def generate_response(self, messages, **kwargs):
                return "extracted memory"

        lt = LongTermMemory(
            db_path="data/_unused",
            llm=LongTermLLMConfig(
                provider="mock-provider",
                api_dialect="mock-provider",
                model="fallback-model",
            ),
        )
        lt._mem = MagicMock()
        lt._mem.llm = FakeLlm()
        seen = []
        lt.add_usage_listener(seen.append)

        lt._install_usage_hook()
        out = lt._mem.llm.generate_response(messages=[
            {"role": "system", "content": "Extract facts."},
            {"role": "user", "content": "用户喜欢喝咖啡"},
        ])

        assert out == "extracted memory"
        assert len(seen) == 1
        assert seen[0]["provider"] == "mock-provider"
        assert seen[0]["model"] == "mem-model"
        assert seen[0]["operation"] == "generate_response"
        assert seen[0]["usage_source"] == "estimated"
        assert seen[0]["usage"]["input_tokens"] > 0
        assert seen[0]["usage"]["output_tokens"] > 0

    def test_mem0_usage_hook_prefers_raw_provider_usage(self):
        class Usage:
            prompt_tokens = 123
            completion_tokens = 45
            prompt_tokens_details = type("Details", (), {"cached_tokens": 67})()

        class Response:
            usage = Usage()
            choices = [type("Choice", (), {"message": type("Message", (), {"content": "ok"})()})()]

        class Completions:
            def create(self, **kwargs):
                return Response()

        class FakeLlm:
            class Config:
                model = "mem-model"

            config = Config()
            client = type("Client", (), {
                "chat": type("Chat", (), {"completions": Completions()})()
            })()

            def generate_response(self, messages, **kwargs):
                response = self.client.chat.completions.create(messages=messages)
                return response.choices[0].message.content

        lt = LongTermMemory(
            db_path="data/_unused",
            llm=LongTermLLMConfig(
                provider="mock-provider",
                api_dialect="mock-provider",
            ),
        )
        lt._mem = MagicMock()
        lt._mem.llm = FakeLlm()
        seen = []
        lt.add_usage_listener(seen.append)

        lt._install_usage_hook()
        lt._mem.llm.generate_response(messages=[{"role": "user", "content": "tiny"}])

        assert seen[0]["usage"] == {
            "input_tokens": 123,
            "output_tokens": 45,
            "cached_tokens": 67,
        }
        assert seen[0]["usage_source"] == "provider"


@pytest.mark.parametrize(
    ("provider", "api_dialect", "base_url_key"),
    [
        ("openai", "openai", "openai_base_url"),
        ("deepseek", "openai", "openai_base_url"),
        ("minimax", "openai", "openai_base_url"),
        ("qwen", "openai", "openai_base_url"),
        ("zhipu", "openai", "openai_base_url"),
    ],
)
def test_llm_config_preserves_provider_base_url(
    provider: str,
    api_dialect: str,
    base_url_key: str,
) -> None:
    llm = LongTermLLMConfig(
        provider=provider,
        api_dialect=api_dialect,
        api_key="secret",
        model="model-id",
        base_url="https://llm.example.test/v1",
    )

    resolved_provider, config = llm.as_mem0_config()

    assert resolved_provider == api_dialect
    # openai 方言额外携带 thinking/coworker_provider，供 CoworkerOpenAILLM 注入思考参数。
    assert config == {
        "model": "model-id",
        "api_key": "secret",
        base_url_key: "https://llm.example.test/v1",
        "thinking": False,
        "coworker_provider": provider,
    }
    assert MemoryConfig(
        llm={"provider": resolved_provider, "config": config}
    ).llm.provider == api_dialect


class TestChromaAccessGuard:
    def test_guard_chroma_access_wraps_vector_store(self):
        from coworker.memory.chroma_guard import _ChromaClientProxy, _ChromaCollectionProxy

        lt = LongTermMemory(db_path="data/_unused")
        vector_store = MagicMock()
        vector_store.client = MagicMock()
        vector_store.collection = MagicMock()
        lt._mem = MagicMock()
        lt._mem.vector_store = vector_store

        lt._guard_chroma_access()

        assert isinstance(vector_store.client, _ChromaClientProxy)
        assert isinstance(vector_store.collection, _ChromaCollectionProxy)
        # RecentActivityMemory consumes chroma_client; it must be the guarded proxy.
        assert lt.chroma_client is vector_store.client

    async def test_initialize_guards_shared_chroma_access(self, monkeypatch):
        import mem0 as mem0_module

        import coworker.memory.mem0_adapters as adapters
        from coworker.memory.chroma_guard import _ChromaClientProxy, _ChromaCollectionProxy

        class FakeVectorStore:
            client = MagicMock()
            collection = MagicMock()

        class FakeMem:
            vector_store = FakeVectorStore()

        class FakeAsyncMemory:
            @classmethod
            def from_config(cls, config):
                return FakeMem()

        monkeypatch.setattr(mem0_module, "AsyncMemory", FakeAsyncMemory)
        monkeypatch.setattr(adapters, "register_mem0_adapters", lambda: None)

        lt = LongTermMemory(db_path="data/_unused")
        await lt.initialize()

        assert isinstance(lt.chroma_client, _ChromaClientProxy)
        assert isinstance(lt._mem.vector_store.collection, _ChromaCollectionProxy)
        # Collections returned through the guarded client stay guarded.
        collection = lt.chroma_client.get_or_create_collection("memories")
        assert isinstance(collection, _ChromaCollectionProxy)


class TestWriteResult:
    def _make(self) -> LongTermMemory:
        lt = LongTermMemory(db_path="data/_unused")
        lt._mem = MagicMock()
        return lt

    async def test_written_returns_first_id(self):
        lt = self._make()
        lt._mem.add = AsyncMock(
            return_value={"results": [{"id": "new-1"}, {"id": "new-2"}]}
        )

        result = await lt.write("新内容", category="knowledge", tags=["a"])

        assert result.status == "written"
        assert result.memory_id == "new-1"
        lt._mem.add.assert_awaited_once()

    async def test_empty_extraction_returns_empty(self):
        lt = self._make()
        lt._mem.add = AsyncMock(return_value={"results": []})

        result = await lt.write("无可提取内容")

        assert result.status == "empty"
        assert result.memory_id == ""


class TestReconfigure:
    async def test_replaces_mem0_llm_instance(self, monkeypatch):
        import mem0.utils.factory as mem0_factory

        lt = LongTermMemory(db_path="data/_unused")
        fake_llm = MagicMock()
        fake_llm.generate_response = MagicMock()
        monkeypatch.setattr(
            mem0_factory.LlmFactory, "create", lambda provider, cfg: fake_llm
        )
        lt._mem = MagicMock()

        new_cfg = LongTermLLMConfig(
            provider="deepseek",
            api_dialect="openai",
            api_key="k",
            model="deepseek-v4-pro",
            base_url="",
            thinking=True,
        )
        await lt.reconfigure(new_cfg)

        assert lt._mem.llm is fake_llm
        assert lt._mem.config.llm.provider == "openai"
        assert lt._mem.config.llm.config["model"] == "deepseek-v4-pro"
        assert lt._mem.config.llm.config["thinking"] is True
        assert lt._llm is new_cfg

    async def test_deferred_when_not_initialized(self):
        lt = LongTermMemory(db_path="data/_unused")
        new_cfg = LongTermLLMConfig(
            provider="deepseek", api_dialect="openai", model="m"
        )

        await lt.reconfigure(new_cfg)

        assert lt._llm is new_cfg
