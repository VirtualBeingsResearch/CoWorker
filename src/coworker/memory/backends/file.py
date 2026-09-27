from __future__ import annotations

import asyncio
import json
import math
import re
from collections import defaultdict
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

_LATIN_WORD = re.compile(r"[a-z0-9]+")
_CJK_RUN = re.compile(r"[\u4e00-\u9fff]+")
# Longest first so "ies" wins over "es" and "s".
_STEM_SUFFIXES = (
    "ational",
    "tional",
    "ization",
    "fulness",
    "ousness",
    "iveness",
    "ingly",
    "edly",
    "ally",
    "ation",
    "ment",
    "ness",
    "able",
    "ible",
    "ical",
    "ing",
    "ers",
    "ied",
    "ies",
    "ed",
    "ly",
    "es",
    "s",
    "ic",
    "al",
    "y",
)


def _stem(word: str) -> str:
    if len(word) < 5:
        return word
    for suffix in _STEM_SUFFIXES:
        if word.endswith(suffix) and len(word) - len(suffix) >= 3:
            return word[: -len(suffix)]
    return word


def tokenize(text: str) -> frozenset[str]:
    """Split a memory or query into searchable terms.

    Latin words are lowercased and lightly stemmed so ``peanut`` matches
    ``peanuts`` and ``allergy`` matches ``allergic``. Continuous CJK runs
    contribute both unigrams and bigrams so ``花生过敏`` is found by ``过敏``.
    """
    folded = text.casefold()
    terms: set[str] = set()
    for word in _LATIN_WORD.findall(folded):
        terms.add(word)
        stem = _stem(word)
        if stem != word:
            terms.add(stem)
    for run in _CJK_RUN.findall(folded):
        terms.update(run)
        terms.update(run[i : i + 2] for i in range(len(run) - 1))
    return frozenset(terms)


class FileBackend:
    """File-backed long-term memory with an inverted-index query.

    Each memory is one human-readable JSON file under ``directory``. ``query``
    ranks records by overlapping tokens (not whole-string substring), then
    applies category, tag, and time filters. There is no embedding model.
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
        self._postings: dict[str, set[str]] = defaultdict(set)
        self._doc_terms: dict[str, frozenset[str]] = {}
        self._lock = asyncio.Lock()
        self._usage_listeners: list[UsageListener] = []
        self._config: MemoryBackendConfig | None = None
        self._ready = False

    async def initialize(self) -> None:
        self._dir.mkdir(parents=True, exist_ok=True)
        self._records.clear()
        self._postings.clear()
        self._doc_terms.clear()

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
                self._index_add(record)
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
            self._index_add(record)
            self._write_record(record)
            return MemoryWriteResult(status="written", memory_id=record.id)

    def _terms_for(self, record: MemoryRecord) -> frozenset[str]:
        terms = set(tokenize(record.content))
        for tag in record.tags:
            terms.update(tokenize(tag))
        return frozenset(terms)

    def _index_add(self, record: MemoryRecord) -> None:
        terms = self._terms_for(record)
        self._doc_terms[record.id] = terms
        for term in terms:
            self._postings[term].add(record.id)

    def _index_remove(self, memory_id: str) -> None:
        for term in self._doc_terms.pop(memory_id, ()):
            posting = self._postings.get(term)
            if posting is None:
                continue
            posting.discard(memory_id)
            if not posting:
                del self._postings[term]

    def _candidates(self, text: str) -> list[str]:
        terms = tokenize(text)
        if not terms:
            return list(self._records)
        hits: set[str] = set()
        for term in terms:
            hits.update(self._postings.get(term, ()))
        return [memory_id for memory_id in self._records if memory_id in hits]

    def _score(self, text: str, memory_id: str) -> float:
        query_terms = tokenize(text)
        doc_terms = self._doc_terms.get(memory_id, frozenset())
        n = max(len(self._records), 1)
        score = 0.0
        for term in query_terms:
            if term not in doc_terms:
                continue
            df = len(self._postings.get(term, ()))
            score += math.log((n - df + 0.5) / (df + 0.5) + 1.0)
        record = self._records.get(memory_id)
        needle = text.strip().casefold()
        if record is not None and needle and needle in record.content.casefold():
            score += len(query_terms) + 1.0
        return score

    async def query(self, params: MemoryQuery) -> list[MemoryRecord]:
        async with self._lock:
            matched = [
                self._records[memory_id]
                for memory_id in self._candidates(params.text)
                if self._filters(self._records[memory_id], params)
            ]
            if params.text.strip():
                matched.sort(key=lambda record: self._score(params.text, record.id), reverse=True)
            return matched[: params.limit]

    @staticmethod
    def _filters(record: MemoryRecord, params: MemoryQuery) -> bool:
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

            self._index_remove(memory_id)
            self._records[memory_id] = record
            self._index_add(record)
            self._write_record(record)

    async def delete(self, memory_id: str) -> None:
        async with self._lock:
            record = self._records.pop(memory_id, None)
            if record is not None:
                self._index_remove(memory_id)
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
            self._index_remove(memory_id)
            self._records[memory_id] = updated
            self._index_add(updated)
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
