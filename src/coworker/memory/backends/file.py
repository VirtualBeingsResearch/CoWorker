from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from typing import ClassVar
from uuid import uuid4

from loguru import logger

from coworker.memory.base import (
    MemoryBackendConfig,
    MemoryQuery,
    MemoryRecord,
    MemoryWriteResult,
    UsageListener,
)


class FileBackend:
    """Minimal file-backed long-term memory backend.

    Each memory is stored as one human-readable JSON file under ``directory``.
    This backend is intentionally simple: it does not perform semantic search or
    expose relevance scores. ``query`` uses substring/category/tag/time filtering.
    """

    backend_id: ClassVar[str] = "file"

    @classmethod
    def required_modules(cls) -> tuple[str, ...]:
        """报错明细用：列出缺少的可导入顶层模块。

        file 后端仅依赖标准库与 loguru，无额外第三方模型库。
        """
        return ()

    @classmethod
    def available(cls) -> bool:
        """file 后端仅依赖标准库与 loguru，恒可用。"""
        return True

    def __init__(self, directory: str) -> None:
        self._dir = Path(directory)
        self._records: dict[str, MemoryRecord] = {}
        self._lock = asyncio.Lock()
        self._usage_listeners: list[UsageListener] = []
        self._config: MemoryBackendConfig | None = None
        self._ready = False

    async def initialize(self) -> None:
        self._dir.mkdir(parents=True, exist_ok=True)
        self._records.clear()

        for path in self._dir.glob("*.json"):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                record = MemoryRecord(
                    id=data["id"],
                    content=data["content"],
                    category=data.get("category", "general"),
                    tags=list(data.get("tags", [])),
                    timestamp=data.get("timestamp"),
                )
                self._records[record.id] = record
            except Exception:
                logger.warning(f"Skipping unreadable memory file: {path}")
                continue

        self._ready = True

    def is_ready(self) -> bool:
        return self._ready

    def _write_record(self, record: MemoryRecord) -> None:
        path = self._dir / f"{record.id}.json"
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(
            json.dumps(
                {
                    "id": record.id,
                    "content": record.content,
                    "category": record.category,
                    "tags": record.tags,
                    "timestamp": record.timestamp,
                },
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        tmp.replace(path)

    async def write(
        self,
        content: str,
        *,
        category: str,
        tags: list[str] | None = None,
        source_timestamp: datetime | None = None,
    ) -> MemoryWriteResult:
        if not content.strip():
            return MemoryWriteResult(status="empty")

        async with self._lock:
            if any(record.content == content for record in self._records.values()):
                return MemoryWriteResult(status="empty")

            record = MemoryRecord(
                id=uuid4().hex,
                content=content,
                category=category,
                tags=list(dict.fromkeys(tags or [])),
                timestamp=(source_timestamp or datetime.now()).isoformat(),
            )
            self._records[record.id] = record
            self._write_record(record)
            return MemoryWriteResult(status="written", memory_id=record.id)

    async def query(self, params: MemoryQuery) -> list[MemoryRecord]:
        async with self._lock:
            matched = [record for record in self._records.values() if self._matches(record, params)]
            return matched[: params.limit]

    @staticmethod
    def _matches(record: MemoryRecord, params: MemoryQuery) -> bool:
        if params.category and record.category != params.category:
            return False
        if params.tags and not (set(record.tags) & set(params.tags)):
            return False
        if params.start is not None or params.end is not None:
            ts = FileBackend._parse_timestamp(record.timestamp)
            if ts is None:
                return False
            if params.start is not None and ts < params.start:
                return False
            if params.end is not None and ts > params.end:
                return False
        if params.text:
            text = params.text.strip().lower()
            if text and text not in record.content.lower():
                return False
        return True

    @staticmethod
    def _parse_timestamp(value: str | None) -> datetime | None:
        if not value:
            return None
        try:
            return datetime.fromisoformat(value)
        except (TypeError, ValueError):
            return None

    async def update(
        self,
        memory_id: str,
        content: str,
        *,
        tags: list[str] | None = None,
    ) -> None:
        async with self._lock:
            record = self._records.get(memory_id)
            if record is None:
                raise ValueError(f"Memory does not exist: {memory_id}")

            if tags is not None:
                record = replace(record, content=content, tags=list(dict.fromkeys(tags)))
            else:
                record = replace(record, content=content)

            self._records[memory_id] = record
            self._write_record(record)

    async def delete(self, memory_id: str) -> None:
        async with self._lock:
            record = self._records.pop(memory_id, None)
            if record is not None:
                (self._dir / f"{memory_id}.json").unlink(missing_ok=True)

    async def associate_tags(self, memory_id: str, tags: list[str]) -> list[str]:
        if not tags:
            raise ValueError("associate_tags requires non-empty tags")

        async with self._lock:
            record = self._records.get(memory_id)
            if record is None:
                raise ValueError(f"Memory does not exist: {memory_id}")

            merged = list(dict.fromkeys([*record.tags, *tags]))
            updated = replace(record, tags=merged)
            self._records[memory_id] = updated
            self._write_record(updated)
            return merged

    async def reconfigure(self, config: MemoryBackendConfig) -> None:
        # FileBackend has no external LLM or connection to hot-swap.
        # Keep the reference so tests can verify reconfigure was called.
        self._config = config

    def add_usage_listener(self, listener: UsageListener) -> None:
        self._usage_listeners.append(listener)

    async def count(self) -> int:
        return len(self._records)
