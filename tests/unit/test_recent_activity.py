from __future__ import annotations

import asyncio
import json
import sys
import threading
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from coworker.i18n import locale_context
from coworker.memory import recent_activity as recent_activity_module
from coworker.memory.recent_activity import RecentActivityMemory, render_recent_activity_replay


def test_tool_exchange_wrappers_follow_locale_and_keep_bracket_shape():
    memory = object.__new__(RecentActivityMemory)
    entry = {
        "type": "tool_exchange",
        "arguments": {"path": "用户原文.txt"},
        "result": {"content": "source payload", "is_error": False},
    }

    with locale_context("zh-CN"):
        chinese = memory._body(entry)
    with locale_context("en"):
        english = memory._body(entry)

    assert chinese.startswith("[工具参数]")
    assert "[工具结果] source payload" in chinese
    assert english.startswith("[tool_args]")
    assert "[tool_result] source payload" in english
    assert "用户原文.txt" in chinese
    assert "用户原文.txt" in english


@pytest.mark.parametrize(
    "document",
    [
        "[tool_result] TARGET payload",
        "[工具结果] TARGET payload",
        "工具结果: TARGET payload",
    ],
)
def test_match_span_removes_structural_wrappers_without_locale_tables(document):
    text = "prefix " + "x" * 1000 + " TARGET payload " + "y" * 1000
    match = RecentActivityMemory._find_match_span(text, "", document)

    assert match is not None
    assert text[slice(*match)] == "TARGET payload"


class _Tokenizer:
    model_max_length = 128

    def encode(self, text, add_special_tokens=False):
        return [ord(c) for c in text]

    def decode(self, tokens, skip_special_tokens=True):
        return "".join(chr(t) for t in tokens)


class _Embedder:
    max_seq_length = 128

    def encode(self, texts, convert_to_numpy=True):
        return [[float(len(t))] for t in texts]


class _SpecialTokenizer(_Tokenizer):
    def encode(self, text, add_special_tokens=False):
        tokens = super().encode(text, add_special_tokens=False)
        return [-1, *tokens, -2] if add_special_tokens else tokens

    def decode(self, tokens, skip_special_tokens=True):
        if skip_special_tokens:
            tokens = [t for t in tokens if t >= 0]
        return super().decode(tokens, skip_special_tokens=skip_special_tokens)


class _VerboseTokenizer(_Tokenizer):
    def __init__(self):
        self.long_verbose_flags = []

    def encode(self, text, add_special_tokens=False, verbose=True):
        tokens = super().encode(text, add_special_tokens=add_special_tokens)
        if len(tokens) > self.model_max_length:
            self.long_verbose_flags.append(verbose)
            if verbose is not False:
                raise AssertionError("long encode should suppress tokenizer warning")
        return tokens


class _StrictEmbedder:
    max_seq_length = 128

    def __init__(self, tokenizer):
        self.tokenizer = tokenizer
        self.seen = []

    def encode(self, texts, convert_to_numpy=True):
        self.seen.extend(texts)
        for text in texts:
            token_count = len(self.tokenizer.encode(text, add_special_tokens=True))
            if token_count > self.max_seq_length:
                raise AssertionError(f"encoded text has {token_count} tokens")
        return [[float(len(t))] for t in texts]


class _Mem0EmbeddingWrapper:
    def __init__(self):
        self.model = _Embedder()
        self.model.tokenizer = _Tokenizer()


class _Collection:
    def __init__(self):
        self.rows = {}

    def upsert(self, ids, documents, metadatas, embeddings):
        for id_, doc, meta, emb in zip(ids, documents, metadatas, embeddings):
            self.rows[id_] = {"document": doc, "metadata": meta, "embedding": emb}

    def query(self, query_embeddings, n_results, include):
        rows = list(self.rows.values())[:n_results]
        return {
            "documents": [[r["document"] for r in rows]],
            "metadatas": [[r["metadata"] for r in rows]],
            "distances": [[0.1 for _ in rows]],
        }

    def get(self, include=None):
        return {
            "ids": list(self.rows),
            "metadatas": [r["metadata"] for r in self.rows.values()],
        }

    def delete(self, ids=None, where=None):
        if ids is None:
            self.rows.clear()
            return
        for id_ in ids:
            self.rows.pop(id_, None)


class _KeywordCollection(_Collection):
    """Test collection that ranks the chunk containing a known semantic hit."""

    def __init__(self, needle):
        super().__init__()
        self.needle = needle

    def query(self, query_embeddings, n_results, include):
        rows = sorted(
            self.rows.values(),
            key=lambda row: self.needle not in row["document"],
        )[:n_results]
        return {
            "documents": [[r["document"] for r in rows]],
            "metadatas": [[r["metadata"] for r in rows]],
            "distances": [[0.01 if self.needle in r["document"] else 1.0 for r in rows]],
        }


class _ChromaClient:
    def __init__(self, collection):
        self.collection = collection
        self.requested = []

    def get_or_create_collection(self, name):
        self.requested.append(name)
        return self.collection


class _LogStore:
    def iter_entries_after(self, seq):
        return []

    def read_recent_days(self, days, now=None):
        return [], True


class _ReplayLogStore(_LogStore):
    def __init__(self, entries):
        self.entries = entries

    def read_seq_range(self, seq_start, seq_end):
        return [
            entry for entry in self.entries
            if seq_start <= int(entry.get("seq", -1)) <= seq_end
        ], True


def _memory(collection=None, days=7, chunk_tokens=40, overlap_tokens=8):
    return RecentActivityMemory(
        db_path="data/_unused",
        log_store=_LogStore(),  # type: ignore[arg-type]
        embedder_model="fake",
        days=days,
        chunk_tokens=chunk_tokens,
        overlap_tokens=overlap_tokens,
        collection=collection or _Collection(),
        embedder=_Embedder(),
        tokenizer=_Tokenizer(),
    )


def _entry(seq, ts, type_="message_in", **extra):
    out = {"seq": seq, "ts": ts.isoformat(), "type": type_}
    out.update(extra)
    return out


@pytest.mark.asyncio
async def test_initialize_reuses_mem0_embedding_wrapper():
    wrapper = _Mem0EmbeddingWrapper()
    mem = RecentActivityMemory(
        db_path="data/_unused",
        log_store=_LogStore(),  # type: ignore[arg-type]
        embedder_model="fake",
        collection=_Collection(),
        embedder=wrapper,
    )

    await mem.initialize()

    assert mem.enabled is True
    assert mem._embedder is wrapper
    assert mem._tokenizer is wrapper.model.tokenizer
    assert mem._encode(["abc"]) == [[3.0]]


@pytest.mark.asyncio
async def test_initialize_uses_injected_chroma_client():
    collection = _Collection()
    client = _ChromaClient(collection)
    mem = RecentActivityMemory(
        db_path="data/_unused",
        log_store=_LogStore(),  # type: ignore[arg-type]
        embedder_model="fake",
        chroma_client=client,
        embedder=_Embedder(),
        tokenizer=_Tokenizer(),
    )

    await mem.initialize()

    assert mem.enabled is True
    assert mem._collection is collection
    assert client.requested == ["recent_activity_v1"]


@pytest.mark.asyncio
async def test_initialize_moves_synchronous_setup_off_event_loop():
    collection = _Collection()
    main_thread_id = threading.get_ident()

    class _ThreadCapturingClient(_ChromaClient):
        def get_or_create_collection(self, name):
            self.thread_id = threading.get_ident()
            return super().get_or_create_collection(name)

    client = _ThreadCapturingClient(collection)
    mem = RecentActivityMemory(
        db_path="data/_unused",
        log_store=_LogStore(),  # type: ignore[arg-type]
        embedder_model="fake",
        chroma_client=client,
        embedder=_Embedder(),
        tokenizer=_Tokenizer(),
    )

    await mem.initialize()

    assert client.thread_id != main_thread_id
    assert mem.enabled is True


def test_initialize_scales_cpu_threads_and_batch_to_host(monkeypatch):
    calls = []

    class _BatchEmbedder(_Embedder):
        device = "cpu"

        def encode(self, texts, batch_size=None, convert_to_numpy=True):
            self.batch_size = batch_size
            return super().encode(texts, convert_to_numpy=convert_to_numpy)

    embedder = _BatchEmbedder()
    monkeypatch.setattr(recent_activity_module.os, "process_cpu_count", lambda: 12)
    monkeypatch.setitem(sys.modules, "torch", SimpleNamespace(
        get_num_threads=lambda: 12,
        set_num_threads=calls.append,
    ))
    mem = RecentActivityMemory(
        db_path="data/_unused",
        log_store=_LogStore(),  # type: ignore[arg-type]
        embedder_model="fake",
        collection=_Collection(),
        embedder=embedder,
        tokenizer=_Tokenizer(),
    )

    mem._initialize_sync()
    mem._encode(["abc"])

    assert calls == [3]
    assert embedder.batch_size == 48


@pytest.mark.asyncio
async def test_query_moves_embedding_and_chroma_off_event_loop():
    main_thread_id = threading.get_ident()

    class _ThreadCapturingEmbedder(_Embedder):
        def encode(self, texts, convert_to_numpy=True):
            self.thread_id = threading.get_ident()
            return super().encode(texts, convert_to_numpy=convert_to_numpy)

    class _ThreadCapturingCollection(_Collection):
        def query(self, query_embeddings, n_results, include):
            self.thread_id = threading.get_ident()
            return super().query(query_embeddings, n_results, include)

    embedder = _ThreadCapturingEmbedder()
    collection = _ThreadCapturingCollection()
    mem = RecentActivityMemory(
        db_path="data/_unused",
        log_store=_LogStore(),  # type: ignore[arg-type]
        embedder_model="fake",
        collection=collection,
        embedder=embedder,
        tokenizer=_Tokenizer(),
    )

    assert await mem.query("needle") == []

    assert embedder.thread_id != main_thread_id
    assert collection.thread_id != main_thread_id


@pytest.mark.asyncio
async def test_index_moves_embedding_and_chroma_off_event_loop():
    main_thread_id = threading.get_ident()

    class _ThreadCapturingEmbedder(_Embedder):
        def encode(self, texts, convert_to_numpy=True):
            self.thread_id = threading.get_ident()
            return super().encode(texts, convert_to_numpy=convert_to_numpy)

    class _ThreadCapturingCollection(_Collection):
        def upsert(self, ids, documents, metadatas, embeddings):
            self.thread_id = threading.get_ident()
            return super().upsert(ids, documents, metadatas, embeddings)

    embedder = _ThreadCapturingEmbedder()
    collection = _ThreadCapturingCollection()
    mem = RecentActivityMemory(
        db_path="data/_unused",
        log_store=_LogStore(),  # type: ignore[arg-type]
        embedder_model="fake",
        collection=collection,
        embedder=embedder,
        tokenizer=_Tokenizer(),
    )

    await mem._index_entries([_entry(1, datetime.now(), content="needle")])

    assert embedder.thread_id != main_thread_id
    assert collection.thread_id != main_thread_id


@pytest.mark.asyncio
async def test_query_is_not_starved_by_multi_batch_background_index():
    first_upsert_started = threading.Event()
    release_first_upsert = threading.Event()
    order = []

    class _CoordinatedCollection(_Collection):
        def upsert(self, ids, documents, metadatas, embeddings):
            order.append("upsert")
            if len(order) == 1:
                first_upsert_started.set()
                release_first_upsert.wait(timeout=2)
            return super().upsert(ids, documents, metadatas, embeddings)

        def query(self, query_embeddings, n_results, include):
            order.append("query")
            return super().query(query_embeddings, n_results, include)

    collection = _CoordinatedCollection()
    mem = _memory(collection)
    now = datetime.now()
    entries = [
        _entry(i, now, content=f"entry {i}")
        for i in range(mem._embedding_batch_size + 1)
    ]

    index_task = asyncio.create_task(mem._index_entries(entries, now=now))
    assert await asyncio.to_thread(first_upsert_started.wait, 1)
    query_task = asyncio.create_task(mem.query("entry"))
    await asyncio.sleep(0)
    release_first_upsert.set()

    await asyncio.wait_for(asyncio.gather(index_task, query_task), timeout=2)

    assert order[:3] == ["upsert", "query", "upsert"]


@pytest.mark.asyncio
async def test_does_not_index_message_tick():
    collection = _Collection()
    mem = _memory(collection)
    now = datetime(2026, 7, 8, 12)

    await mem._index_entries([
        _entry(1, now, "message_tick", content="<tick>"),
        _entry(2, now, "message_in", participant_id="u", source="rest", content="hello"),
    ], now=now)

    assert all(row["metadata"]["event_type"] != "message_tick" for row in collection.rows.values())
    assert {row["metadata"]["evidence_id"] for row in collection.rows.values()} == {"recent:2"}


@pytest.mark.asyncio
async def test_only_indexes_recent_days():
    collection = _Collection()
    mem = _memory(collection, days=7)
    now = datetime(2026, 7, 8, 12)

    await mem._index_entries([
        _entry(1, now - timedelta(days=8), content="old"),
        _entry(2, now - timedelta(days=1), content="recent"),
    ], now=now)

    assert {row["metadata"]["evidence_id"] for row in collection.rows.values()} == {"recent:2"}


def test_long_tool_result_chunks_share_evidence_id_and_stay_under_token_limit():
    mem = _memory(chunk_tokens=120, overlap_tokens=24)
    now = datetime(2026, 7, 8, 12)
    entry = _entry(
        9,
        now,
        "tool_result",
        name="execute_code",
        content="结果" * 100 + "尾部关键词",
        is_error=False,
    )

    docs = mem._documents_for_entry(entry)

    assert len(docs) > 1
    assert {meta["evidence_id"] for _id, _doc, meta in docs} == {"recent:9"}
    assert {meta["chunk_count"] for _id, _doc, meta in docs} == {len(docs)}
    assert all(len(_Tokenizer().encode(doc)) <= 120 for _id, doc, _meta in docs)


def test_long_token_counting_suppresses_transformers_length_warning():
    tokenizer = _VerboseTokenizer()
    mem = RecentActivityMemory(
        db_path="data/_unused",
        log_store=_LogStore(),  # type: ignore[arg-type]
        embedder_model="fake",
        days=7,
        chunk_tokens=200,
        overlap_tokens=24,
        collection=_Collection(),
        embedder=_Embedder(),
        tokenizer=tokenizer,
    )
    now = datetime(2026, 7, 8, 12)

    docs = mem._documents_for_entry(_entry(10, now, content="x" * 339))

    assert docs
    assert tokenizer.long_verbose_flags
    assert set(tokenizer.long_verbose_flags) == {False}


@pytest.mark.asyncio
async def test_index_truncates_documents_before_embedding_model_limit():
    collection = _Collection()
    tokenizer = _SpecialTokenizer()
    embedder = _StrictEmbedder(tokenizer)
    mem = RecentActivityMemory(
        db_path="data/_unused",
        log_store=_LogStore(),  # type: ignore[arg-type]
        embedder_model="fake",
        days=7,
        chunk_tokens=200,
        overlap_tokens=24,
        collection=collection,
        embedder=embedder,
        tokenizer=tokenizer,
    )
    now = datetime(2026, 7, 8, 12)

    await mem._index_entries([
        _entry(
            1,
            now,
            "message_in",
            participant_id="u" * 160,
            source="rest",
            conversation_id="room",
            content="正文" * 160,
        ),
    ], now=now)

    assert collection.rows
    assert embedder.seen
    assert all(
        len(tokenizer.encode(text, add_special_tokens=True)) <= 128
        for text in embedder.seen
    )


@pytest.mark.asyncio
async def test_query_truncates_text_before_embedding_model_limit():
    collection = _Collection()
    tokenizer = _SpecialTokenizer()
    embedder = _StrictEmbedder(tokenizer)
    mem = RecentActivityMemory(
        db_path="data/_unused",
        log_store=_LogStore(),  # type: ignore[arg-type]
        embedder_model="fake",
        collection=collection,
        embedder=embedder,
        tokenizer=tokenizer,
    )

    out = await mem.query("查询" * 120, limit=5)

    assert out == []
    assert embedder.seen
    assert len(tokenizer.encode(embedder.seen[0], add_special_tokens=True)) <= 128


@pytest.mark.asyncio
async def test_tool_call_and_result_are_indexed_together():
    collection = _Collection()
    mem = _memory(collection, chunk_tokens=160, overlap_tokens=24)
    now = datetime(2026, 7, 8, 12)

    await mem._index_entries([
        _entry(
            1,
            now,
            "tool_call",
            id="tc1",
            name="search_web",
            arguments={"query": "alpha"},
        ),
        _entry(
            2,
            now + timedelta(seconds=1),
            "tool_result",
            id="tc1",
            name="search_web",
            content="beta result",
            is_error=False,
        ),
    ], now=now + timedelta(seconds=2))

    assert {row["metadata"]["evidence_id"] for row in collection.rows.values()} == {"recent:1"}
    row = next(iter(collection.rows.values()))
    assert row["metadata"]["event_type"] == "tool_exchange"
    assert row["metadata"]["status"] == "ok"
    assert row["metadata"]["ts"] == (now + timedelta(seconds=1)).isoformat()
    assert "alpha" in row["document"]
    assert "beta result" in row["document"]


@pytest.mark.asyncio
async def test_query_returns_tool_result_with_call_context():
    collection = _Collection()
    mem = _memory(collection, chunk_tokens=160, overlap_tokens=24)
    now = datetime(2026, 7, 8, 12)

    await mem._index_entries([
        _entry(
            1,
            now,
            "tool_call",
            id="tc1",
            name="execute_code",
            arguments={"code": "print('needle')"},
        ),
        _entry(
            2,
            now + timedelta(seconds=1),
            "tool_result",
            id="tc1",
            name="execute_code",
            content="needle output",
            is_error=False,
        ),
    ], now=now + timedelta(seconds=2))

    out = await mem.query("needle", limit=5)

    assert [r["id"] for r in out] == ["recent:1"]
    assert out[0]["event_type"] == "tool_exchange"
    assert out[0]["activity_description"] == "调用工具 execute_code 并收到成功结果。"
    assert "print('needle')" in out[0]["snippet"]
    assert "needle output" in out[0]["snippet"]


@pytest.mark.asyncio
async def test_query_replays_observable_activity_chain_without_reasoning(tmp_path):
    collection = _Collection()
    now = datetime(2026, 7, 8, 12)
    entries = [
        _entry(
            1,
            now,
            "message_in",
            participant_id="alice",
            source="wecom",
            conversation_id="room-1",
            content="请检查部署状态",
        ),
        _entry(
            2,
            now + timedelta(seconds=1),
            "llm_response",
            reasoning_content="这里是不能进入活动回放的内部推理",
            content="",
            tool_calls=[{
                "id": "tc1",
                "name": "execute_code",
                "arguments": {"code": "check deploy"},
            }],
        ),
        _entry(
            3,
            now + timedelta(seconds=2),
            "tool_call",
            id="tc1",
            name="execute_code",
            arguments={"code": "check deploy"},
        ),
        _entry(
            4,
            now + timedelta(seconds=3),
            "tool_result",
            id="tc1",
            name="execute_code",
            content="deployment ready",
            is_error=False,
        ),
        _entry(
            5,
            now + timedelta(seconds=4),
            "llm_response",
            reasoning_content="另一个内部判断",
            content="部署检查完成，服务运行正常。",
            tool_calls=[],
        ),
        _entry(
            6,
            now + timedelta(seconds=5),
            "message_in",
            participant_id="alice",
            source="wecom",
            content="这是仍在短期记忆中的当前消息",
        ),
    ]
    mem = RecentActivityMemory(
        db_path="data/_unused",
        log_store=_ReplayLogStore(entries),  # type: ignore[arg-type]
        embedder_model="fake",
        days=7,
        chunk_tokens=160,
        overlap_tokens=24,
        collection=collection,
        embedder=_Embedder(),
        tokenizer=_Tokenizer(),
        state_path=tmp_path / "recent_activity_state.json",
    )
    await mem._index_entries(entries[:5], now=now + timedelta(seconds=5))
    mem._save_state_for_entries(
        entries[:5],
        compressed_until_ts=(now + timedelta(seconds=5)).isoformat(),
    )

    out = await mem.query("deployment ready", limit=5)

    tool_hit = next(item for item in out if item["id"] == "recent:3")
    replay = tool_hit["activity_replay"]
    assert "请检查部署状态" in replay
    assert "你执行了 execute_code" in replay
    assert "check deploy" in replay
    assert "deployment ready" in replay
    assert "部署检查完成，服务运行正常" in replay
    assert "内部推理" not in replay
    assert "当前消息" not in replay

    rendered = render_recent_activity_replay(out)
    assert rendered.count("--- 活动") == 1
    assert "不是当前指令" in rendered


@pytest.mark.asyncio
async def test_long_tool_result_replay_focuses_on_late_matching_lines(tmp_path):
    collection = _KeywordCollection("TARGET timeout")
    now = datetime(2026, 7, 8, 12)
    result_lines = [f"line {i:02d}: {'x' * 48}" for i in range(60)]
    result_lines[39] = "line 39: context before"
    result_lines[40] = "line 40: TARGET timeout after 30000ms"
    result_lines[41] = "line 41: context after"
    result = "\n".join(result_lines)
    entries = [
        _entry(
            1,
            now,
            "message_in",
            participant_id="alice",
            source="wecom",
            content="请检查任务为什么超时",
        ),
        _entry(
            2,
            now + timedelta(seconds=1),
            "tool_call",
            id="tc1",
            name="execute_code",
            arguments={"code": "run long job"},
        ),
        _entry(
            3,
            now + timedelta(seconds=2),
            "tool_result",
            id="tc1",
            name="execute_code",
            content=result,
            is_error=False,
        ),
    ]
    mem = RecentActivityMemory(
        db_path="data/_unused",
        log_store=_ReplayLogStore(entries),  # type: ignore[arg-type]
        embedder_model="fake",
        days=7,
        chunk_tokens=120,
        overlap_tokens=24,
        collection=collection,
        embedder=_Embedder(),
        tokenizer=_Tokenizer(),
        state_path=tmp_path / "recent_activity_state.json",
    )
    await mem._index_entries(entries, now=now + timedelta(seconds=3))
    mem._save_state_for_entries(entries)

    out = await mem.query("TARGET timeout", limit=1)

    replay = out[0]["activity_replay"]
    assert "工具返回（命中上下文）" in replay
    assert "line 39: context before" in replay
    assert "> line 40: TARGET timeout after 30000ms" in replay
    assert "line 41: context after" in replay
    assert "line 00:" not in replay
    assert "line 59:" not in replay
    assert len(replay) < 1000


@pytest.mark.asyncio
async def test_long_single_line_replay_centers_on_semantic_chunk(tmp_path):
    collection = _KeywordCollection("connection timeout")
    now = datetime(2026, 7, 8, 12)
    result = "P" * 1500 + " connection timeout after 30000ms " + "S" * 1500
    entries = [
        _entry(
            1,
            now,
            "tool_call",
            id="tc1",
            name="execute_code",
            arguments={"code": "run long job"},
        ),
        _entry(
            2,
            now + timedelta(seconds=1),
            "tool_result",
            id="tc1",
            name="execute_code",
            content=result,
            is_error=False,
        ),
    ]
    mem = RecentActivityMemory(
        db_path="data/_unused",
        log_store=_ReplayLogStore(entries),  # type: ignore[arg-type]
        embedder_model="fake",
        days=7,
        chunk_tokens=120,
        overlap_tokens=24,
        collection=collection,
        embedder=_Embedder(),
        tokenizer=_Tokenizer(),
        state_path=tmp_path / "recent_activity_state.json",
    )
    await mem._index_entries(entries, now=now + timedelta(seconds=2))
    mem._save_state_for_entries(entries)

    out = await mem.query("数据库连接失败", limit=1)

    replay = out[0]["activity_replay"]
    assert "工具返回（命中上下文）" in replay
    assert "connection timeout after 30000ms" in replay
    assert "> …" in replay
    assert replay.count("P") < 300
    assert replay.count("S") < 300
    assert len(replay) < 1000


@pytest.mark.asyncio
async def test_query_returns_message_activity_description():
    collection = _Collection()
    mem = _memory(collection)
    now = datetime(2026, 7, 8, 12)

    await mem._index_entries([
        _entry(
            1,
            now,
            "message_in",
            participant_id="alice",
            source="wecom",
            conversation_id="room-1",
            content="请检查部署状态",
        ),
    ], now=now)

    out = await mem.query("部署状态", limit=5)

    assert out[0]["activity_description"] == "收到用户消息，来自 wecom，发送者 alice，会话 room-1。"
    assert out[0]["message_kind"] == "user_message"
    assert out[0]["intent_role"] == "user_intent"
    assert out[0]["source"] == "wecom"
    assert out[0]["participant_id"] == "alice"
    assert out[0]["conversation_id"] == "room-1"


@pytest.mark.asyncio
async def test_message_in_kind_is_indexed_and_described():
    collection = _Collection()
    mem = _memory(collection, chunk_tokens=160, overlap_tokens=24)
    now = datetime(2026, 7, 8, 12)

    await mem._index_entries([
        _entry(
            1,
            now,
            "message_in",
            participant_id="system",
            source="system",
            content="记忆树回溯完成",
        ),
        _entry(
            2,
            now,
            "message_in",
            participant_id="system",
            source="alarm",
            content="[闹钟提醒] 检查部署状态",
        ),
        _entry(
            3,
            now,
            "message_in",
            participant_id="bbl_1234",
            source="bubble",
            content="阶段结论已完成",
        ),
    ], now=now)

    rows = {row["metadata"]["seq"]: row for row in collection.rows.values()}

    assert rows[1]["metadata"]["message_kind"] == "system_notice"
    assert rows[1]["metadata"]["intent_role"] == "context_update"
    assert "kind=system_notice" in rows[1]["document"]
    assert "role=context_update" in rows[1]["document"]

    assert rows[2]["metadata"]["message_kind"] == "automation_signal"
    assert rows[2]["metadata"]["intent_role"] == "automation_update"
    assert "kind=automation_signal" in rows[2]["document"]
    assert "role=automation_update" in rows[2]["document"]

    assert rows[3]["metadata"]["message_kind"] == "bubble_coordination"
    assert rows[3]["metadata"]["intent_role"] == "coordination"
    assert "kind=bubble_coordination" in rows[3]["document"]
    assert "role=coordination" in rows[3]["document"]

    out = await mem.query("部署 状态", limit=5)
    descriptions = {item["message_kind"]: item["activity_description"] for item in out}
    assert descriptions["system_notice"].startswith("收到系统通知")
    assert descriptions["automation_signal"].startswith("收到自动化信号")
    assert descriptions["bubble_coordination"].startswith("收到泡泡协作消息")


@pytest.mark.asyncio
async def test_query_time_filter_and_evidence_dedup():
    collection = _Collection()
    mem = _memory(collection)
    now = datetime(2026, 7, 8, 12)

    await mem._index_entries([
        _entry(1, now - timedelta(hours=3), "tool_result", name="run", content="alpha" * 40),
        _entry(2, now - timedelta(hours=1), "tool_result", name="run", content="beta"),
    ], now=now)

    out = await mem.query(
        "alpha",
        limit=5,
        start=now - timedelta(hours=4),
        end=now - timedelta(hours=2),
    )

    assert [r["id"] for r in out] == ["recent:1"]


@pytest.mark.asyncio
async def test_sync_compressed_from_log_indexes_only_before_primary_boundary(tmp_path):
    collection = _Collection()
    now = datetime(2026, 7, 8, 12)
    messages = []
    sink_id = recent_activity_module.logger.add(
        lambda message: messages.append(message.record["message"]),
        level="DEBUG",
    )

    class _Store:
        def read_recent_days(self, days, now=None):
            return [
                _entry(1, datetime(2026, 7, 8, 9), content="compressed"),
                _entry(2, datetime(2026, 7, 8, 11), content="still primary"),
            ], True

    mem = RecentActivityMemory(
        db_path="data/_unused",
        log_store=_Store(),  # type: ignore[arg-type]
        embedder_model="fake",
        days=7,
        chunk_tokens=40,
        overlap_tokens=8,
        collection=collection,
        embedder=_Embedder(),
        tokenizer=_Tokenizer(),
        state_path=tmp_path / "recent_activity_state.json",
    )

    try:
        await mem.sync_compressed_from_log(datetime(2026, 7, 8, 10), now=now)
    finally:
        recent_activity_module.logger.remove(sink_id)

    assert {row["metadata"]["evidence_id"] for row in collection.rows.values()} == {"recent:1"}
    assert any("Recent activity indexing started: entries=1" in message for message in messages)
    assert any(
        "Recent activity index batch 1/1: entries=1, documents=1, checkpoint_seq=1"
        in message
        for message in messages
    )
    assert any("Recent activity indexing completed: entries=1" in message for message in messages)


@pytest.mark.asyncio
async def test_sync_compressed_from_log_only_embeds_new_entries_after_watermark(tmp_path):
    collection = _Collection()
    now = datetime(2026, 7, 8, 12)

    class _CountingEmbedder(_Embedder):
        def __init__(self):
            self.batches = []

        def encode(self, texts, convert_to_numpy=True):
            self.batches.append(list(texts))
            return super().encode(texts, convert_to_numpy=convert_to_numpy)

    class _Store:
        def read_recent_days(self, days, now=None):
            return [
                _entry(1, datetime(2026, 7, 8, 9), content="first"),
                _entry(2, datetime(2026, 7, 8, 10), content="second"),
            ], True

        def iter_entries_after(self, seq):
            return iter([
                _entry(1, datetime(2026, 7, 8, 9), content="first"),
                _entry(2, datetime(2026, 7, 8, 10), content="second"),
            ][seq:])

    embedder = _CountingEmbedder()
    mem = RecentActivityMemory(
        db_path="data/_unused",
        log_store=_Store(),  # type: ignore[arg-type]
        embedder_model="fake",
        days=7,
        chunk_tokens=40,
        overlap_tokens=8,
        collection=collection,
        embedder=embedder,
        tokenizer=_Tokenizer(),
        state_path=tmp_path / "recent_activity_state.json",
    )

    await mem.sync_compressed_from_log(datetime(2026, 7, 8, 9, 30), now=now)
    first_batch_count = len(embedder.batches)
    await mem.sync_compressed_from_log(datetime(2026, 7, 8, 11), now=now)

    assert {row["metadata"]["evidence_id"] for row in collection.rows.values()} == {
        "recent:1", "recent:2",
    }
    assert len(embedder.batches) == first_batch_count + 1
    assert any("seq=2" in document for document in embedder.batches[-1])


@pytest.mark.asyncio
async def test_sync_checkpoints_each_completed_batch(tmp_path, monkeypatch):
    monkeypatch.setattr(recent_activity_module.os, "process_cpu_count", lambda: 8)
    now = datetime(2026, 7, 8, 12)
    entries = [_entry(i, now, content=f"entry {i}") for i in range(1, 34)]

    class _Store:
        def read_recent_days(self, days, now=None):
            return entries, True

    class _FailSecondBatchCollection(_Collection):
        def __init__(self):
            super().__init__()
            self.calls = 0

        def upsert(self, ids, documents, metadatas, embeddings):
            self.calls += 1
            if self.calls == 2:
                raise RuntimeError("stop after first batch")
            return super().upsert(ids, documents, metadatas, embeddings)

    state_path = tmp_path / "recent_activity_state.json"
    mem = RecentActivityMemory(
        db_path="data/_unused",
        log_store=_Store(),  # type: ignore[arg-type]
        embedder_model="fake",
        days=7,
        chunk_tokens=40,
        overlap_tokens=8,
        collection=_FailSecondBatchCollection(),
        embedder=_Embedder(),
        tokenizer=_Tokenizer(),
        state_path=state_path,
    )

    with pytest.raises(RuntimeError, match="stop after first batch"):
        await mem.sync_compressed_from_log(now + timedelta(seconds=1), now=now)

    assert json.loads(state_path.read_text(encoding="utf-8"))["indexed_until_seq"] == 32


@pytest.mark.asyncio
async def test_schedule_sync_runs_in_background(tmp_path):
    collection = _Collection()
    reference_now = datetime.now()

    class _Store:
        def read_recent_days(self, days, now=None):
            return [
                _entry(1, reference_now - timedelta(hours=3), content="compressed"),
            ], True

    mem = RecentActivityMemory(
        db_path="data/_unused",
        log_store=_Store(),  # type: ignore[arg-type]
        embedder_model="fake",
        days=7,
        chunk_tokens=40,
        overlap_tokens=8,
        collection=collection,
        embedder=_Embedder(),
        tokenizer=_Tokenizer(),
        state_path=tmp_path / "recent_activity_state.json",
    )

    assert mem.schedule_sync_compressed_from_log(reference_now) is True
    await mem.wait_for_pending_sync()

    assert {row["metadata"]["evidence_id"] for row in collection.rows.values()} == {"recent:1"}
