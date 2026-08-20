from __future__ import annotations

import json
from datetime import datetime

import pytest_asyncio

from coworker.memory.backends.file import FileBackend
from coworker.memory.base import MemoryQuery


@pytest_asyncio.fixture
async def backend(tmp_path):
    backend = FileBackend(directory=str(tmp_path / "long_term"))
    await backend.initialize()
    return backend


async def test_initialize_creates_directory(tmp_path):
    directory = tmp_path / "long_term"
    backend = FileBackend(directory=str(directory))
    assert not directory.exists()
    await backend.initialize()
    assert directory.is_dir()
    assert backend.is_ready()


async def test_write_and_query(backend):
    result = await backend.write(
        "用户喜欢简洁的中文回复",
        category="preference",
        tags=["user", "preference"],
    )
    assert result.status == "written"
    assert result.memory_id

    records = await backend.query(MemoryQuery(text="简洁", limit=10))
    assert len(records) == 1
    assert records[0].id == result.memory_id
    assert records[0].content == "用户喜欢简洁的中文回复"
    assert records[0].category == "preference"
    assert records[0].tags == ["user", "preference"]


async def test_write_exact_duplicate_returns_empty(backend):
    await backend.write("same", category="general", tags=[])
    result = await backend.write("same", category="knowledge", tags=["x"])
    assert result.status == "empty"
    assert await backend.count() == 1


async def test_query_filters_by_category_tags_and_time(backend):
    await backend.write(
        "alpha",
        category="knowledge",
        tags=["a", "b"],
        source_timestamp=datetime(2026, 1, 1),
    )
    await backend.write(
        "beta",
        category="experience",
        tags=["b"],
        source_timestamp=datetime(2026, 2, 1),
    )

    assert [r.content for r in await backend.query(MemoryQuery("", category="knowledge"))] == ["alpha"]
    assert [r.content for r in await backend.query(MemoryQuery("", tags=["a"]))] == ["alpha"]
    assert [r.content for r in await backend.query(MemoryQuery("", tags=["b"]))] == ["alpha", "beta"]
    assert [
        r.content
        for r in await backend.query(
            MemoryQuery(
                "",
                start=datetime(2026, 1, 15),
                end=datetime(2026, 2, 15),
            )
        )
    ] == ["beta"]


async def test_update_preserves_category_and_timestamp(backend):
    result = await backend.write(
        "old",
        category="knowledge",
        tags=["a"],
        source_timestamp=datetime(2026, 1, 1),
    )
    await backend.update(result.memory_id, "new", tags=["b"])

    records = await backend.query(MemoryQuery("new"))
    assert len(records) == 1
    assert records[0].category == "knowledge"
    assert records[0].timestamp == "2026-01-01T00:00:00"
    assert records[0].tags == ["b"]


async def test_associate_tags_merges_and_deduplicates(backend):
    result = await backend.write("content", category="general", tags=["a"])
    merged = await backend.associate_tags(result.memory_id, ["b", "a"])
    assert merged == ["a", "b"]
    records = await backend.query(MemoryQuery("content"))
    assert records[0].tags == ["a", "b"]


async def test_delete_removes_memory(backend):
    result = await backend.write("delete me", category="general", tags=[])
    await backend.delete(result.memory_id)
    assert await backend.count() == 0
    assert await backend.query(MemoryQuery("delete me")) == []


async def test_persists_across_initialize(tmp_path):
    directory = tmp_path / "long_term"
    first = FileBackend(directory=str(directory))
    await first.initialize()
    result = await first.write("持久化内容", category="knowledge", tags=["k"])

    second = FileBackend(directory=str(directory))
    await second.initialize()
    records = await second.query(MemoryQuery("持久化内容"))
    assert len(records) == 1
    assert records[0].id == result.memory_id
    assert records[0].content == "持久化内容"


async def test_files_are_human_readable(backend, tmp_path):
    await backend.write("中文记忆内容", category="general", tags=["tag"])
    files = list((tmp_path / "long_term").glob("*.json"))
    assert len(files) == 1
    data = json.loads(files[0].read_text(encoding="utf-8"))
    assert data["content"] == "中文记忆内容"
    assert data["category"] == "general"
    assert data["tags"] == ["tag"]
    assert "\n" in files[0].read_text(encoding="utf-8")


async def test_skips_unreadable_file(tmp_path):
    directory = tmp_path / "long_term"
    directory.mkdir(parents=True)
    (directory / "bad.json").write_text("{not json", encoding="utf-8")
    (directory / "good.json").write_text(
        json.dumps(
            {
                "id": "good",
                "content": "ok",
                "category": "general",
                "tags": [],
                "timestamp": None,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    backend = FileBackend(directory=str(directory))
    await backend.initialize()
    assert await backend.count() == 1
    records = await backend.query(MemoryQuery("ok"))
    assert len(records) == 1
    assert records[0].id == "good"
