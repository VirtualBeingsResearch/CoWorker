from __future__ import annotations

import asyncio
import json
import os
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from loguru import logger

from coworker.agent.log_store import LogStore
from coworker.i18n import tr

_SCHEMA_VERSION = 4
_COLLECTION_NAME = "recent_activity_v1"
_INDEXED_TYPES = {
    "message_in",
    "llm_response",
    "tool_call",
    "tool_result",
    "tool_exchange",
    "task_reminder",
    "subconscious_done",
}
_STATUS_OK = "ok"
_STATUS_ERROR = "error"
_STATUS_UNKNOWN = "unknown"
_DEFAULT_BATCH_SIZE = 32
_SNIPPET_CHARS = 700
_META_SNIPPET_CHARS = 350
_REPLAY_SEQ_BEFORE = 24
_REPLAY_SEQ_AFTER = 40
_REPLAY_MAX_EVENTS = 14
_REPLAY_EVENT_CHARS = 1200
_REPLAY_MATCH_CONTEXT_LINES = 1
_REPLAY_MATCH_SIDE_CHARS = 180
_REPLAY_CONTEXT_LINE_CHARS = 240
_MAX_REASONABLE_MODEL_TOKENS = 100_000
_USER_MESSAGE_SOURCES = {"file", "rest", "websocket", "wecom", "codex"}
_AUTOMATION_MESSAGE_SOURCES = {"alarm", "code_job"}
_SYSTEM_MESSAGE_SOURCES = {"system", "compress_memory"}
_MESSAGE_KIND_USER = "user_message"
_MESSAGE_KIND_SYSTEM = "system_notice"
_MESSAGE_KIND_AUTOMATION = "automation_signal"
_MESSAGE_KIND_BUBBLE = "bubble_coordination"
_MESSAGE_KIND_EXTERNAL = "external_message"
_INTENT_ROLE_USER = "user_intent"
_INTENT_ROLE_CONTEXT = "context_update"
_INTENT_ROLE_AUTOMATION = "automation_update"
_INTENT_ROLE_COORDINATION = "coordination"


@dataclass
class RecentActivityResult:
    id: str
    seq: int
    timestamp: str
    event_type: str
    tool_name: str
    status: str
    is_error: bool
    activity_description: str
    snippet: str
    matched_chunk_index: int
    chunk_count: int
    relevance: float
    raw_available: bool = True
    message_kind: str = ""
    intent_role: str = ""


def render_recent_activity_replay(
    results: list[dict[str, Any]],
    *,
    title: str | None = None,
    include_evidence: bool = False,
) -> str:
    """Render recalled activity as past observable facts, not retrieval internals."""
    lines = [
        title or tr("recent_activity.default_title"),
        tr("recent_activity.intro"),
    ]
    seen_episodes: set[tuple[Any, ...]] = set()
    rendered = 0
    for item in results:
        replay = str(item.get("activity_replay") or "").strip()
        episode_key = (
            item.get("replay_start_seq"),
            item.get("replay_end_seq"),
        )
        if replay and all(v is not None for v in episode_key):
            if episode_key in seen_episodes:
                continue
            seen_episodes.add(episode_key)
        if not replay:
            description = str(item.get("activity_description") or "").strip()
            snippet = str(item.get("snippet") or "").strip()
            timestamp = _display_timestamp(str(item.get("timestamp") or ""))
            replay = "\n".join(
                part
                for part in (
                    timestamp,
                    description,
                    snippet,
                )
                if part
            )
        if not replay:
            continue
        rendered += 1
        lines.extend(["", tr("recent_activity.activity", index=rendered), replay])
        if include_evidence and item.get("id"):
            lines.append(tr("recent_activity.evidence", id=item["id"]))
    return "\n".join(lines)


def _display_timestamp(value: str) -> str:
    try:
        return datetime.fromisoformat(value).strftime("%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError):
        return value


class RecentActivityMemory:
    """A short-lived semantic index over recent interaction-log events.

    Evidence is event-level (``recent:{seq}``) even when a long event is represented
    by multiple internal vector chunks.
    """

    def __init__(
        self,
        *,
        db_path: str | Path,
        log_store: LogStore,
        embedder_model: str,
        days: int = 7,
        chunk_tokens: int = 120,
        overlap_tokens: int = 24,
        collection: Any | None = None,
        chroma_client: Any | None = None,
        embedder: Any | None = None,
        tokenizer: Any | None = None,
        state_path: str | Path | None = None,
    ) -> None:
        self._db_path = Path(db_path)
        self._log_store = log_store
        self._embedder_model = embedder_model
        self._days = max(1, int(days))
        self._chunk_tokens = max(8, int(chunk_tokens))
        self._overlap_tokens = max(0, int(overlap_tokens))
        if self._overlap_tokens >= self._chunk_tokens:
            self._overlap_tokens = max(0, self._chunk_tokens // 5)
        self._state_path = (
            Path(state_path)
            if state_path is not None
            else (self._db_path / "recent_activity_state.json")
        )
        self._collection = collection
        self._chroma_client = chroma_client
        self._embedder = embedder
        self._tokenizer = tokenizer
        self._max_seq_length = self._infer_max_seq_length(include_cache=False)
        self._encoding_token_budget = self._encoding_budget(self._max_seq_length)
        available_cpus = os.process_cpu_count() or 1
        self._embedding_threads = max(1, min(8, available_cpus // 4))
        self._embedding_batch_size = min(
            64,
            max(
                _DEFAULT_BATCH_SIZE,
                self._embedding_threads * 16,
            ),
        )
        self._pending_boundary: datetime | None = None
        self._running_boundary: datetime | None = None
        self._sync_task: asyncio.Task | None = None
        self._initialization_boundary: datetime | None = None
        self._initialization_task: asyncio.Task | None = None
        # SentenceTransformer and Chroma expose synchronous APIs and share mutable
        # client/model state.  Serialize their use while running the blocking work
        # in a worker thread so they never stall the application's event loop.
        self._backend_async_lock = asyncio.Lock()
        self._backend_lock = threading.Lock()
        self._enabled = True

    @property
    def enabled(self) -> bool:
        return self._enabled and self._collection is not None

    def _require_collection(self) -> Any:
        if self._collection is None:
            raise RuntimeError("Recent activity collection is not initialized")
        return self._collection

    async def initialize(self) -> None:
        """Initialize synchronous model/vector dependencies off the event loop."""
        await self._call_backend(self._initialize_sync)

    async def _call_backend(self, fn: Any, *args: Any, **kwargs: Any) -> Any:
        """Queue one backend call without occupying multiple executor threads."""
        async with self._backend_async_lock:
            return await asyncio.to_thread(self._run_backend, fn, *args, **kwargs)

    def _run_backend(self, fn: Any, *args: Any, **kwargs: Any) -> Any:
        """Run one synchronous backend operation without concurrent model access."""
        with self._backend_lock:
            return fn(*args, **kwargs)

    def _initialize_sync(self) -> None:
        try:
            if self._tokenizer is None:
                self._tokenizer = self._tokenizer_from_embedder(self._embedder)
            if self._tokenizer is None:
                from transformers import AutoTokenizer

                self._tokenizer = AutoTokenizer.from_pretrained(self._embedder_model)
            self._refresh_encoding_limits(include_cache=True, fallback=512)
            if (
                self._encoding_token_budget is not None
                and self._chunk_tokens > self._encoding_token_budget
            ):
                clamped = max(8, self._encoding_token_budget)
                logger.warning(
                    f"recent_activity_chunk_tokens={self._chunk_tokens} exceeds "
                    f"embedder max_seq_length={self._max_seq_length}; clamped to {clamped}"
                )
                self._chunk_tokens = clamped
            if self._overlap_tokens >= self._chunk_tokens:
                self._overlap_tokens = max(0, self._chunk_tokens // 5)

            if self._embedder is None:
                from sentence_transformers import SentenceTransformer

                self._embedder = SentenceTransformer(self._embedder_model)
            self._limit_cpu_embedding_threads()
            self._refresh_encoding_limits(include_cache=False, fallback=512)
            if (
                self._encoding_token_budget is not None
                and self._chunk_tokens > self._encoding_token_budget
            ):
                clamped = max(8, self._encoding_token_budget)
                logger.warning(
                    f"recent_activity_chunk_tokens={self._chunk_tokens} exceeds "
                    f"SentenceTransformer max_seq_length={self._max_seq_length}; "
                    f"clamped to {clamped}"
                )
                self._chunk_tokens = clamped
                if self._overlap_tokens >= self._chunk_tokens:
                    self._overlap_tokens = max(0, self._chunk_tokens // 5)
            if self._collection is None:
                client = self._chroma_client or self._create_chroma_client()
                self._collection = client.get_or_create_collection(_COLLECTION_NAME)
            self._enabled = True
        except Exception as e:
            self._enabled = False
            logger.warning(f"Recent activity memory disabled: {e}")

    def start_background_initialization(
        self,
        raw_primary_boundary: datetime | None,
    ) -> bool:
        """Start optional index setup without making application readiness wait."""
        self._remember_initialization_boundary(raw_primary_boundary)
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return False
        if self._initialization_task is None or self._initialization_task.done():
            self._initialization_task = loop.create_task(
                self._initialize_and_schedule_sync(),
                name="recent-activity-initialize",
            )
        return True

    async def _initialize_and_schedule_sync(self) -> None:
        await self.initialize()
        boundary = self._initialization_boundary
        self._initialization_boundary = None
        if self.enabled:
            self.schedule_sync_compressed_from_log(boundary)

    def _remember_initialization_boundary(self, boundary: datetime | None) -> None:
        if boundary is not None and (
            self._initialization_boundary is None or boundary > self._initialization_boundary
        ):
            self._initialization_boundary = boundary

    def _create_chroma_client(self) -> Any:
        import chromadb
        from chromadb.config import Settings

        settings = Settings(anonymized_telemetry=False)
        settings.persist_directory = str(self._db_path)
        settings.is_persistent = True
        return chromadb.Client(settings)

    def schedule_sync_compressed_from_log(
        self,
        raw_primary_boundary: datetime | None,
    ) -> bool:
        """Schedule background indexing up to the newest compressed boundary."""
        if raw_primary_boundary is None:
            return False
        if not self.enabled:
            task = self._initialization_task
            if task is not None and not task.done():
                self._remember_initialization_boundary(raw_primary_boundary)
                return True
            return False
        latest = max(
            (b for b in (self._pending_boundary, self._running_boundary) if b is not None),
            default=None,
        )
        if latest is None or raw_primary_boundary > latest:
            self._pending_boundary = raw_primary_boundary
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return False
        if self._sync_task is None or self._sync_task.done():
            self._sync_task = loop.create_task(
                self._sync_pending_boundaries(),
                name="recent-activity-compressed-sync",
            )
        return True

    async def wait_for_pending_sync(self) -> None:
        initialization_task = self._initialization_task
        if initialization_task is not None:
            await initialization_task
        task = self._sync_task
        if task is not None:
            await task

    async def _sync_pending_boundaries(self) -> None:
        while self._pending_boundary is not None:
            boundary = self._pending_boundary
            self._pending_boundary = None
            self._running_boundary = boundary
            try:
                await self.sync_compressed_from_log(boundary)
            except Exception as e:
                logger.warning(f"Recent activity compressed sync failed: {e}")
            finally:
                self._running_boundary = None

    async def sync_compressed_from_log(
        self,
        raw_primary_boundary: datetime | None,
        now: datetime | None = None,
    ) -> None:
        """Index recent log events that are no longer raw in short-term memory.

        ``raw_primary_boundary`` is the timestamp of the oldest still-verbatim
        primary message. Events before it have been compressed into summaries/tree
        nodes, so this semantic index becomes the fine-grained lookup surface for
        those otherwise-lost details.
        """
        if not self.enabled or raw_primary_boundary is None:
            return
        state, needs_rebuild, indexed_entries = await asyncio.to_thread(
            self._prepare_compressed_sync,
            raw_primary_boundary,
            now,
        )
        if needs_rebuild:
            await self._call_backend(self._clear_collection)
        await self._index_entries(indexed_entries, now=now, checkpoint=True)
        await self._call_backend(self.prune, now)
        await asyncio.to_thread(
            self._save_state_for_entries,
            indexed_entries,
            fallback_last_seq=int(state.get("indexed_until_seq", -1)),
            rebuilt=needs_rebuild,
            compressed_until_ts=raw_primary_boundary.isoformat(),
        )

    def _prepare_compressed_sync(
        self,
        raw_primary_boundary: datetime,
        now: datetime | None = None,
    ) -> tuple[dict[str, Any], bool, list[dict[str, Any]]]:
        state = self._load_state()
        needs_rebuild = self._needs_rebuild(state)
        if needs_rebuild:
            entries, _complete = self._log_store.read_recent_days(self._days, now=now)
        else:
            entries = self._entries_not_yet_indexed(state, now=now)
        indexed_entries = [e for e in entries if self._is_before_boundary(e, raw_primary_boundary)]
        return state, needs_rebuild, indexed_entries

    def _entries_not_yet_indexed(
        self,
        state: dict[str, Any],
        *,
        now: datetime | None = None,
    ) -> list[dict[str, Any]]:
        """Read only the new log tail after a completed incremental sync.

        Replaying every recent log entry at each restart made embedding happen in
        one long synchronous burst, despite Chroma upserts being idempotent.  The
        persisted sequence watermark lets normal operation process only entries
        that became eligible since the last compression boundary.
        """
        try:
            last_seq = int(state.get("indexed_until_seq", -1))
        except (TypeError, ValueError):
            last_seq = -1
        iter_after = getattr(self._log_store, "iter_entries_after", None)
        if last_seq >= 0 and callable(iter_after):
            try:
                return [
                    entry for entry in iter_after(last_seq) if self._should_index(entry, now=now)
                ]
            except Exception as e:
                logger.warning(f"Recent activity incremental log read failed: {e}")
        entries, _complete = self._log_store.read_recent_days(self._days, now=now)
        return [entry for entry in entries if int(entry.get("seq", -1)) > last_seq]

    async def query(
        self,
        query_text: str,
        *,
        limit: int = 5,
        start: datetime | None = None,
        end: datetime | None = None,
        min_relevance: float | None = None,
    ) -> list[dict[str, Any]]:
        if not self.enabled or not query_text.strip():
            return []
        if limit <= 0:
            return []
        return await self._call_backend(
            self._query_sync,
            query_text,
            limit=limit,
            start=start,
            end=end,
            min_relevance=min_relevance,
        )

    def _query_sync(
        self,
        query_text: str,
        *,
        limit: int,
        start: datetime | None,
        end: datetime | None,
        min_relevance: float | None,
    ) -> list[dict[str, Any]]:
        try:
            query_embedding = self._encode([query_text])[0]
            n_results = max(limit * 4, 20)
            raw = self._require_collection().query(
                query_embeddings=[query_embedding],
                n_results=n_results,
                include=["documents", "metadatas", "distances"],
            )
        except Exception as e:
            logger.warning(f"Recent activity query failed: {e}")
            return []

        docs = (raw.get("documents") or [[]])[0]
        metas = (raw.get("metadatas") or [[]])[0]
        distances = (raw.get("distances") or [[]])[0]
        best_by_evidence: dict[str, dict[str, Any]] = {}
        for doc, meta, distance in zip(docs, metas, distances):
            if not isinstance(meta, dict):
                continue
            ts = str(meta.get("ts") or "")
            if not self._in_time_range(ts, start, end):
                continue
            relevance = self._similarity(distance)
            if min_relevance is not None and relevance < min_relevance:
                continue
            evidence_id = str(meta.get("evidence_id") or "")
            if not evidence_id:
                continue
            item = self._result_from_match(
                meta,
                str(doc),
                relevance,
                query_text=query_text,
            )
            existing = best_by_evidence.get(evidence_id)
            if existing is None or item["relevance"] > existing["relevance"]:
                best_by_evidence[evidence_id] = item
        out = list(best_by_evidence.values())
        out.sort(key=lambda m: (m["relevance"], m.get("timestamp") or ""), reverse=True)
        out = out[:limit]
        self._attach_activity_replays(out)
        return out

    async def rebuild_recent(self, now: datetime | None = None) -> None:
        if not self.enabled:
            return
        entries, _complete = await asyncio.to_thread(
            self._log_store.read_recent_days,
            self._days,
            now=now,
        )
        await self._call_backend(self._clear_collection)
        await self._index_entries(entries, now=now)
        await asyncio.to_thread(self._save_state_for_entries, entries, rebuilt=True)

    def prune(self, now: datetime | None = None) -> None:
        if not self.enabled:
            return
        cutoff = (now or datetime.now()) - timedelta(days=self._days)
        collection = self._require_collection()
        try:
            raw = collection.get(include=["metadatas"])
        except Exception as e:
            logger.debug(f"Recent activity prune skipped: {e}")
            return
        ids = raw.get("ids") or []
        metas = raw.get("metadatas") or []
        stale: list[str] = []
        for id_, meta in zip(ids, metas):
            if not isinstance(meta, dict):
                continue
            ts = self._parse_dt(str(meta.get("ts") or ""))
            if ts is not None and ts < cutoff:
                stale.append(str(id_))
        if stale:
            collection.delete(ids=stale)

    def _clear_collection(self) -> None:
        collection = self._require_collection()
        try:
            raw = collection.get()
            ids = raw.get("ids") or []
            if ids:
                collection.delete(ids=[str(i) for i in ids])
        except Exception as e:
            logger.warning(f"Failed to clear recent activity collection before rebuild: {e}")

    async def _index_entries(
        self,
        entries: list[dict[str, Any]],
        now: datetime | None = None,
        *,
        checkpoint: bool = False,
    ) -> None:
        if not self.enabled or not entries:
            return
        coalesced = await asyncio.to_thread(self._coalesce_tool_exchanges, entries)
        batch_count = (
            len(coalesced) + self._embedding_batch_size - 1
        ) // self._embedding_batch_size
        total_docs = 0
        started_at = time.perf_counter()
        logger.info(
            f"Recent activity indexing started: entries={len(entries)}, "
            f"batches={batch_count}, batch_size={self._embedding_batch_size}"
        )
        for entry_offset in range(0, len(coalesced), self._embedding_batch_size):
            batch_number = entry_offset // self._embedding_batch_size + 1
            batch_entries = coalesced[entry_offset : entry_offset + self._embedding_batch_size]
            ids, docs, metas = await self._call_backend(
                self._prepare_index_batch_sync,
                batch_entries,
                now,
            )
            total_docs += len(docs)
            for doc_offset in range(0, len(docs), self._embedding_batch_size):
                await self._call_backend(
                    self._upsert_batch_sync,
                    ids[doc_offset : doc_offset + self._embedding_batch_size],
                    docs[doc_offset : doc_offset + self._embedding_batch_size],
                    metas[doc_offset : doc_offset + self._embedding_batch_size],
                )
            if checkpoint:
                next_offset = entry_offset + self._embedding_batch_size
                next_seq = (
                    int(coalesced[next_offset].get("seq", -1))
                    if next_offset < len(coalesced)
                    else None
                )
                completed = (
                    entries
                    if next_seq is None
                    else [entry for entry in entries if int(entry.get("seq", -1)) < next_seq]
                )
                await asyncio.to_thread(self._save_state_for_entries, completed)
            else:
                completed = []
            checkpoint_seq = max(
                (int(entry["seq"]) for entry in completed if isinstance(entry.get("seq"), int)),
                default=None,
            )
            logger.debug(
                f"Recent activity index batch {batch_number}/{batch_count}: "
                f"entries={len(batch_entries)}, documents={len(docs)}, "
                f"checkpoint_seq={checkpoint_seq if checkpoint_seq is not None else '-'}"
            )
            # Release the fair async admission lock between batches so foreground
            # recall queries don't wait behind an entire startup indexing backlog.
            await asyncio.sleep(0)
        logger.info(
            f"Recent activity indexing completed: entries={len(entries)}, "
            f"documents={total_docs}, batches={batch_count}, "
            f"elapsed={time.perf_counter() - started_at:.1f}s"
        )

    def _prepare_index_batch_sync(
        self,
        entries: list[dict[str, Any]],
        now: datetime | None = None,
    ) -> tuple[list[str], list[str], list[dict[str, Any]]]:
        docs: list[str] = []
        ids: list[str] = []
        metas: list[dict[str, Any]] = []
        for entry in entries:
            if not self._should_index(entry, now=now):
                continue
            for doc_id, doc, meta in self._documents_for_entry(entry):
                ids.append(doc_id)
                docs.append(doc)
                metas.append(meta)
        return ids, docs, metas

    def _upsert_batch_sync(
        self,
        ids: list[str],
        docs: list[str],
        metas: list[dict[str, Any]],
    ) -> None:
        if not docs:
            return
        embeddings = self._encode(docs)
        self._require_collection().upsert(
            ids=ids,
            documents=docs,
            metadatas=metas,
            embeddings=embeddings,
        )

    def _documents_for_entry(self, entry: dict[str, Any]) -> list[tuple[str, str, dict[str, Any]]]:
        seq = int(entry.get("seq", -1))
        event_type = str(entry.get("type") or "")
        tool_name = str(entry.get("name") or "")
        status = self._status(entry)
        header = self._header(entry, compact=False)
        body = self._body(entry)
        chunks = self._chunk_text(header, body)
        evidence_id = self._evidence_id(entry)
        out: list[tuple[str, str, dict[str, Any]]] = []
        for i, chunk in enumerate(chunks):
            meta = {
                "evidence_id": evidence_id,
                "seq": seq,
                "ts": self._timestamp(entry),
                "event_type": event_type,
                "source": str(entry.get("source") or ""),
                "participant_id": str(entry.get("participant_id") or ""),
                "conversation_id": str(entry.get("conversation_id") or ""),
                "tool_name": tool_name,
                "status": status,
                "has_error": self._is_error(entry),
                "raw_available": True,
                "chunk_index": i,
                "chunk_count": len(chunks),
            }
            if event_type == "message_in":
                meta["message_kind"] = self._message_kind(entry)
                meta["intent_role"] = self._intent_role(entry)
            if event_type == "tool_exchange":
                meta.update(self._tool_exchange_meta(entry))
            out.append((f"{evidence_id}:chunk:{i}", chunk, meta))
        return out

    def _coalesce_tool_exchanges(self, entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Pair tool_call with its tool_result so recent-memory evidence is complete."""
        results_by_id: dict[str, dict[str, Any]] = {}
        for entry in entries:
            if entry.get("type") != "tool_result":
                continue
            call_id = str(entry.get("id") or "")
            if call_id and call_id not in results_by_id:
                results_by_id[call_id] = entry

        paired_result_seqs: set[int] = set()
        out: list[dict[str, Any]] = []
        for entry in entries:
            if entry.get("type") == "tool_call":
                call_id = str(entry.get("id") or "")
                result = results_by_id.get(call_id)
                if result is not None:
                    merged = dict(entry)
                    merged["type"] = "tool_exchange"
                    merged["result"] = result
                    paired_result_seqs.add(int(result.get("seq", -1)))
                    out.append(merged)
                    continue
            if (
                entry.get("type") == "tool_result"
                and int(entry.get("seq", -1)) in paired_result_seqs
            ):
                continue
            out.append(entry)
        return out

    @staticmethod
    def _evidence_id(entry: dict[str, Any]) -> str:
        return f"recent:{int(entry.get('seq', -1))}"

    @staticmethod
    def _timestamp(entry: dict[str, Any]) -> str:
        if entry.get("type") == "tool_exchange" and isinstance(entry.get("result"), dict):
            return str(entry["result"].get("ts") or entry.get("ts") or "")
        return str(entry.get("ts") or "")

    @staticmethod
    def _is_error(entry: dict[str, Any]) -> bool:
        if entry.get("type") == "tool_exchange" and isinstance(entry.get("result"), dict):
            return bool(entry["result"].get("is_error"))
        return bool(entry.get("is_error"))

    def _tool_exchange_meta(self, entry: dict[str, Any]) -> dict[str, Any]:
        raw_result = entry.get("result")
        result = raw_result if isinstance(raw_result, dict) else {}
        args = json.dumps(entry.get("arguments", {}), ensure_ascii=False, default=str)
        return {
            "call_context": self._meta_snippet(f"[tool_args] {args}"),
            "result_preview": self._meta_snippet(
                f"[{'tool_error' if result.get('is_error') else 'tool_result'}] "
                f"{str(result.get('content') or '')}"
            ),
        }

    def _chunk_text(self, header: str, body: str) -> list[str]:
        tokenizer = self._tokenizer
        if tokenizer is None:
            raise RuntimeError("Recent activity tokenizer is not initialized")
        chunk_limit = self._chunk_token_limit()
        header_tokens = self._tokenizer_encode(tokenizer, header, add_special_tokens=False)
        if len(header_tokens) > chunk_limit - 34:
            header = self._compact_header(header)
            header_tokens = self._tokenizer_encode(tokenizer, header, add_special_tokens=False)
        body_tokens = self._tokenizer_encode(tokenizer, body or "", add_special_tokens=False)
        body_budget = chunk_limit - len(header_tokens) - 2
        if len(body_tokens) > body_budget:
            compact_header = self._compact_header(header)
            compact_tokens = self._tokenizer_encode(
                tokenizer, compact_header, add_special_tokens=False
            )
            compact_body_budget = chunk_limit - len(compact_tokens) - 2
            if len(body_tokens) <= compact_body_budget:
                header = compact_header
                header_tokens = compact_tokens
                body_budget = compact_body_budget
        if body_budget < 32:
            header = self._compact_header(header)
            header_tokens = self._tokenizer_encode(tokenizer, header, add_special_tokens=False)
            body_budget = chunk_limit - len(header_tokens) - 2
        if body_budget < 1:
            return [self._truncate_for_encoding(header)]
        if not body_tokens:
            return [self._truncate_for_encoding(header)]
        overlap = min(self._overlap_tokens, max(0, body_budget - 1))
        step = max(1, body_budget - overlap)
        chunks: list[str] = []
        start = 0
        while start < len(body_tokens):
            end = min(start + body_budget, len(body_tokens))
            text = tokenizer.decode(body_tokens[start:end], skip_special_tokens=True)
            chunks.append(self._truncate_for_encoding(f"{header}\n{text}".strip()))
            if end >= len(body_tokens):
                break
            start += step
        return chunks or [header]

    def _encode(self, texts: list[str]) -> list[list[float]]:
        texts = [self._truncate_for_encoding(text) for text in texts]
        encoder = self._sentence_transformer_from_embedder(self._embedder)
        if encoder is not None:
            try:
                vectors = encoder.encode(
                    texts,
                    batch_size=self._embedding_batch_size,
                    convert_to_numpy=True,
                )
            except TypeError as e:
                if "batch_size" not in str(e):
                    raise
                vectors = encoder.encode(texts, convert_to_numpy=True)
            return vectors.tolist() if hasattr(vectors, "tolist") else vectors
        embed_batch = getattr(self._embedder, "embed_batch", None)
        if callable(embed_batch):
            try:
                return embed_batch(texts, memory_action="search")
            except TypeError:
                return embed_batch(texts)
        embed = getattr(self._embedder, "embed", None)
        if callable(embed):
            try:
                return [embed(text, memory_action="search") for text in texts]
            except TypeError:
                return [embed(text) for text in texts]
        embedder = self._embedder
        if embedder is None:
            raise RuntimeError("Recent activity embedder is not initialized")
        vectors = embedder.encode(texts, convert_to_numpy=True)
        return vectors.tolist() if hasattr(vectors, "tolist") else vectors

    def _limit_cpu_embedding_threads(self) -> None:
        encoder = self._sentence_transformer_from_embedder(self._embedder)
        if not str(getattr(encoder, "device", "")).startswith("cpu"):
            return
        try:
            import torch

            if torch.get_num_threads() > self._embedding_threads:
                torch.set_num_threads(self._embedding_threads)
                logger.info(
                    f"CPU embedding: {self._embedding_threads} threads, "
                    f"batch size {self._embedding_batch_size}"
                )
        except Exception as e:
            logger.debug(f"Could not limit CPU embedding threads: {e}")

    def _chunk_token_limit(self) -> int:
        if self._encoding_token_budget is None:
            return self._chunk_tokens
        return max(1, min(self._chunk_tokens, self._encoding_token_budget))

    def _truncate_for_encoding(self, text: str) -> str:
        if not text:
            return ""
        tokenizer = self._tokenizer
        budget = self._encoding_token_budget
        if tokenizer is None or budget is None:
            return text
        try:
            tokens = self._tokenizer_encode(tokenizer, text, add_special_tokens=False)
        except Exception:
            return text
        if len(tokens) <= budget:
            return text
        truncated = tokenizer.decode(tokens[:budget], skip_special_tokens=True).strip()
        try:
            guard = self._tokenizer_encode(tokenizer, truncated, add_special_tokens=False)
        except Exception:
            return truncated
        while len(guard) > budget and guard:
            guard = guard[:budget]
            truncated = tokenizer.decode(guard, skip_special_tokens=True).strip()
            guard = self._tokenizer_encode(tokenizer, truncated, add_special_tokens=False)
        return truncated

    def _refresh_encoding_limits(
        self,
        *,
        include_cache: bool,
        fallback: int | None = None,
    ) -> None:
        self._max_seq_length = self._infer_max_seq_length(include_cache=include_cache)
        if self._max_seq_length is None:
            self._max_seq_length = fallback
        self._encoding_token_budget = self._encoding_budget(self._max_seq_length)

    def _infer_max_seq_length(self, *, include_cache: bool) -> int | None:
        candidates: list[int] = []
        tokenizer_max = self._valid_max_length(getattr(self._tokenizer, "model_max_length", None))
        if tokenizer_max is not None:
            candidates.append(tokenizer_max)
        encoder = self._sentence_transformer_from_embedder(self._embedder)
        encoder_max = self._valid_max_length(getattr(encoder, "max_seq_length", None))
        if encoder_max is not None:
            candidates.append(encoder_max)
        embedder_max = self._valid_max_length(getattr(self._embedder, "max_seq_length", None))
        if embedder_max is not None:
            candidates.append(embedder_max)
        if include_cache:
            sentence_max = self._read_sentence_transformer_max_seq_length()
            if sentence_max is not None:
                candidates.append(sentence_max)
        return min(candidates) if candidates else None

    def _encoding_budget(self, max_seq_length: int | None) -> int | None:
        if max_seq_length is None:
            return None
        return max(1, max_seq_length - self._special_token_overhead())

    def _special_token_overhead(self) -> int:
        tokenizer = self._tokenizer
        if tokenizer is None:
            return 2
        try:
            plain = len(self._tokenizer_encode(tokenizer, "", add_special_tokens=False))
            with_special = len(self._tokenizer_encode(tokenizer, "", add_special_tokens=True))
        except Exception:
            return 2
        return max(0, with_special - plain)

    @staticmethod
    def _tokenizer_encode(
        tokenizer: Any,
        text: str,
        *,
        add_special_tokens: bool,
    ) -> list[int]:
        try:
            return tokenizer.encode(
                text,
                add_special_tokens=add_special_tokens,
                verbose=False,
            )
        except TypeError:
            return tokenizer.encode(text, add_special_tokens=add_special_tokens)

    @staticmethod
    def _valid_max_length(value: Any) -> int | None:
        try:
            length = int(value)
        except (TypeError, ValueError, OverflowError):
            return None
        if length <= 0 or length > _MAX_REASONABLE_MODEL_TOKENS:
            return None
        return length

    def _read_sentence_transformer_max_seq_length(self) -> int | None:
        try:
            encoder = self._sentence_transformer_from_embedder(self._embedder)
            if encoder is not None and getattr(encoder, "max_seq_length", None):
                return int(encoder.max_seq_length)
            config_path = self._find_cached_file("sentence_bert_config.json")
            if config_path and config_path.exists():
                data = json.loads(config_path.read_text(encoding="utf-8"))
                return int(data.get("max_seq_length") or 0) or None
        except Exception:
            return None
        return None

    @classmethod
    def _sentence_transformer_from_embedder(cls, embedder: Any | None) -> Any | None:
        if embedder is None:
            return None
        if callable(getattr(embedder, "encode", None)):
            return embedder
        model = getattr(embedder, "model", None)
        if callable(getattr(model, "encode", None)):
            return model
        return None

    @classmethod
    def _tokenizer_from_embedder(cls, embedder: Any | None) -> Any | None:
        encoder = cls._sentence_transformer_from_embedder(embedder)
        if encoder is None:
            return None
        tokenizer = getattr(encoder, "tokenizer", None)
        if cls._looks_like_tokenizer(tokenizer):
            return tokenizer
        modules = getattr(encoder, "_modules", None)
        values = modules.values() if modules is not None and hasattr(modules, "values") else []
        for module in values:
            tokenizer = getattr(module, "tokenizer", None)
            if cls._looks_like_tokenizer(tokenizer):
                return tokenizer
        return None

    @staticmethod
    def _looks_like_tokenizer(tokenizer: Any | None) -> bool:
        return callable(getattr(tokenizer, "encode", None)) and callable(
            getattr(tokenizer, "decode", None)
        )

    def _find_cached_file(self, name: str) -> Path | None:
        try:
            from huggingface_hub import try_to_load_from_cache

            path = try_to_load_from_cache(self._embedder_model, name)
            return Path(path) if path and isinstance(path, str) else None
        except Exception:
            return None

    def _should_index(self, entry: dict[str, Any], now: datetime | None = None) -> bool:
        if entry.get("type") not in _INDEXED_TYPES:
            return False
        ts = self._parse_dt(str(entry.get("ts") or ""))
        if ts is None:
            return False
        current = now or datetime.now()
        return current - timedelta(days=self._days) <= ts <= current

    def _is_before_boundary(self, entry: dict[str, Any], boundary: datetime) -> bool:
        ts = self._parse_dt(str(entry.get("ts") or ""))
        return ts is not None and ts < boundary

    @staticmethod
    def _parse_dt(value: str) -> datetime | None:
        try:
            return datetime.fromisoformat(value)
        except (TypeError, ValueError):
            return None

    @classmethod
    def _in_time_range(cls, value: str, start: datetime | None, end: datetime | None) -> bool:
        if start is None and end is None:
            return True
        dt = cls._parse_dt(value)
        if dt is None:
            return False
        return (start is None or dt >= start) and (end is None or dt <= end)

    @staticmethod
    def _similarity(distance: Any) -> float:
        try:
            d = float(distance)
        except (TypeError, ValueError):
            d = 1.0
        return round(1.0 / (1.0 + max(0.0, d)), 4)

    def _result_from_match(
        self,
        meta: dict[str, Any],
        document: str,
        relevance: float,
        *,
        query_text: str = "",
    ) -> dict[str, Any]:
        chunk_count = int(meta.get("chunk_count") or 1)
        chunk_index = int(meta.get("chunk_index") or 0)
        event_type = str(meta.get("event_type") or "")
        tool_name = str(meta.get("tool_name") or "")
        status = str(meta.get("status") or _STATUS_UNKNOWN)
        is_error = bool(meta.get("has_error"))
        snippet = self._snippet(document)
        if event_type == "tool_exchange":
            snippet = self._tool_exchange_snippet(meta, snippet)
        message_kind = str(meta.get("message_kind") or "")
        intent_role = str(meta.get("intent_role") or "")
        return {
            "id": str(meta.get("evidence_id") or ""),
            "seq": int(meta.get("seq") or -1),
            "timestamp": str(meta.get("ts") or ""),
            "event_type": event_type,
            "message_kind": message_kind,
            "intent_role": intent_role,
            "source": str(meta.get("source") or ""),
            "participant_id": str(meta.get("participant_id") or ""),
            "conversation_id": str(meta.get("conversation_id") or ""),
            "tool_name": tool_name,
            "status": status,
            "is_error": is_error,
            "activity_description": self._activity_description(
                event_type=event_type,
                source=str(meta.get("source") or ""),
                participant_id=str(meta.get("participant_id") or ""),
                conversation_id=str(meta.get("conversation_id") or ""),
                tool_name=tool_name,
                status=status,
                is_error=is_error,
                message_kind=message_kind,
                intent_role=intent_role,
            ),
            "snippet": snippet,
            "matched_chunk_index": chunk_index,
            "chunk_count": chunk_count,
            "relevance": relevance,
            "raw_available": bool(meta.get("raw_available", True)),
            # Retrieval-only fields used to focus the raw activity replay.  They
            # are intentionally not rendered as retrieval metadata.
            "_matched_document": document,
            "_query_text": query_text,
        }

    def _attach_activity_replays(self, results: list[dict[str, Any]]) -> None:
        """Expand semantic hits back into bounded, chronological raw-log episodes.

        Vector chunks are only locators.  The replay deliberately uses observable
        records (incoming messages, visible replies, tool calls/results) and never
        exposes ``reasoning_content``.
        """
        if not results or not callable(getattr(self._log_store, "read_seq_range", None)):
            return
        seqs = [int(item.get("seq", -1)) for item in results if int(item.get("seq", -1)) >= 0]
        if not seqs:
            return
        state = self._load_state()
        try:
            compressed_max_seq = int(state.get("indexed_until_seq", -1))
        except (TypeError, ValueError):
            compressed_max_seq = -1
        range_start = max(0, min(seqs) - _REPLAY_SEQ_BEFORE)
        range_end = max(seqs) + _REPLAY_SEQ_AFTER
        if compressed_max_seq >= 0:
            range_end = min(range_end, compressed_max_seq)
        try:
            entries, _complete = self._log_store.read_seq_range(range_start, range_end)
        except Exception as e:
            logger.debug(f"Recent activity replay expansion failed: {e}")
            return
        if compressed_max_seq >= 0:
            entries = [e for e in entries if int(e.get("seq", -1)) <= compressed_max_seq]
        for item in results:
            episode = self._episode_for_anchor(
                entries,
                anchor_seq=int(item.get("seq", -1)),
                anchor_type=str(item.get("event_type") or ""),
            )
            if not episode:
                continue
            replay = self._render_episode(
                episode,
                anchor_seq=int(item.get("seq", -1)),
                query_text=str(item.get("_query_text") or ""),
                matched_document=str(item.get("_matched_document") or ""),
            )
            if not replay:
                continue
            item["activity_replay"] = replay
            item["replay_start_seq"] = int(episode[0].get("seq", -1))
            item["replay_end_seq"] = int(episode[-1].get("seq", -1))

    def _episode_for_anchor(
        self,
        entries: list[dict[str, Any]],
        *,
        anchor_seq: int,
        anchor_type: str,
    ) -> list[dict[str, Any]]:
        window = [
            entry
            for entry in entries
            if anchor_seq - _REPLAY_SEQ_BEFORE
            <= int(entry.get("seq", -1))
            <= anchor_seq + _REPLAY_SEQ_AFTER
        ]
        anchor_idx = next(
            (i for i, entry in enumerate(window) if int(entry.get("seq", -1)) == anchor_seq),
            None,
        )
        if anchor_idx is None:
            return []
        if anchor_type in {"task_reminder", "subconscious_done"}:
            return [window[anchor_idx]]

        start = anchor_idx
        for i in range(anchor_idx, -1, -1):
            if window[i].get("type") == "message_in":
                start = i
                break
        end = len(window)
        for i in range(anchor_idx + 1, len(window)):
            if window[i].get("type") == "message_in":
                end = i
                break

        observable = []
        for entry in window[start:end]:
            event_type = str(entry.get("type") or "")
            if event_type not in {
                "message_in",
                "llm_response",
                "tool_call",
                "tool_result",
            }:
                continue
            if event_type == "llm_response" and not str(entry.get("content") or "").strip():
                continue
            observable.append(entry)

        if len(observable) <= _REPLAY_MAX_EVENTS:
            return observable
        anchor_pos = next(
            (i for i, entry in enumerate(observable) if int(entry.get("seq", -1)) == anchor_seq),
            len(observable) // 2,
        )
        left = max(0, anchor_pos - (_REPLAY_MAX_EVENTS // 2))
        right = min(len(observable), left + _REPLAY_MAX_EVENTS)
        left = max(0, right - _REPLAY_MAX_EVENTS)
        selected = observable[left:right]
        omitted_trigger = (
            observable
            and observable[0].get("type") == "message_in"
            and selected[0] is not observable[0]
        )
        if omitted_trigger:
            selected = [observable[0], *selected[-(_REPLAY_MAX_EVENTS - 1) :]]
        return selected

    def _render_episode(
        self,
        entries: list[dict[str, Any]],
        *,
        anchor_seq: int = -1,
        query_text: str = "",
        matched_document: str = "",
    ) -> str:
        if not entries:
            return ""
        lines = [tr("recent_activity.time", time=self._episode_time_label(entries))]
        results_by_id = {
            str(entry.get("id")): entry
            for entry in entries
            if entry.get("type") == "tool_result" and entry.get("id")
        }
        paired_result_ids: set[str] = set()
        for entry in entries:
            event_type = str(entry.get("type") or "")
            is_anchor = int(entry.get("seq", -1)) == anchor_seq
            if event_type == "message_in":
                lines.extend(
                    self._render_replay_message(
                        entry,
                        query_text=query_text if is_anchor else "",
                        matched_document=matched_document if is_anchor else "",
                    )
                )
            elif event_type == "llm_response":
                content, focused = self._replay_text_with_match(
                    entry.get("content"),
                    query_text=query_text if is_anchor else "",
                    matched_document=matched_document if is_anchor else "",
                )
                if content:
                    label = tr(
                        "recent_activity.assistant",
                        focus=tr("recent_activity.focus") if focused else "",
                    )
                    lines.extend([label, self._indent_replay(content)])
            elif event_type == "tool_call":
                call_id = str(entry.get("id") or "")
                result = results_by_id.get(call_id)
                if result is not None:
                    paired_result_ids.add(call_id)
                lines.extend(
                    self._render_replay_tool(
                        entry,
                        result,
                        query_text=query_text if is_anchor else "",
                        matched_document=matched_document if is_anchor else "",
                    )
                )
            elif event_type == "tool_result":
                call_id = str(entry.get("id") or "")
                if call_id in paired_result_ids:
                    continue
                content, focused = self._replay_text_with_match(
                    entry.get("content"),
                    query_text=query_text if is_anchor else "",
                    matched_document=matched_document if is_anchor else "",
                )
                if entry.get("is_error"):
                    label = tr(
                        "recent_activity.tool_error",
                        focus=tr("recent_activity.focus") if focused else "",
                    )
                else:
                    label = tr(
                        "recent_activity.tool_seen",
                        focus=tr("recent_activity.focus") if focused else "",
                    )
                lines.extend(
                    [
                        label,
                        self._indent_replay(content or tr("recent_activity.empty_result")),
                    ]
                )
            elif event_type == "task_reminder":
                raw_tasks = entry.get("tasks")
                tasks = raw_tasks if isinstance(raw_tasks, list) else []
                descriptions = [
                    str(task.get("description") or "").strip()
                    for task in tasks
                    if isinstance(task, dict) and str(task.get("description") or "").strip()
                ]
                content = "\n".join(f"- {description}" for description in descriptions)
                detail = self._replay_text(content) or tr("recent_activity.empty_task")
                lines.extend([tr("recent_activity.task_notice"), self._indent_replay(detail)])
            elif event_type == "subconscious_done":
                content = self._replay_text(entry.get("result")) or tr(
                    "recent_activity.empty_conclusion"
                )
                mode = str(entry.get("mode") or tr("recent_activity.background"))
                lines.extend(
                    [
                        tr("recent_activity.background_done", mode=mode),
                        self._indent_replay(content),
                    ]
                )
        return "\n".join(lines)

    def _render_replay_message(
        self,
        entry: dict[str, Any],
        *,
        query_text: str = "",
        matched_document: str = "",
    ) -> list[str]:
        content, focused = self._replay_text_with_match(
            entry.get("content"),
            query_text=query_text,
            matched_document=matched_document,
        )
        content = content or tr("recent_activity.empty_message")
        kind = self._message_kind(entry)
        sender = str(entry.get("participant_id") or tr("recent_activity.external_participant"))
        source = str(entry.get("source") or "")
        conversation = str(entry.get("conversation_id") or "")
        location = (
            tr("recent_activity.conversation", conversation=conversation) if conversation else ""
        )
        focus = tr("recent_activity.focus") if focused else ""
        if kind == _MESSAGE_KIND_SYSTEM:
            label = tr("recent_activity.system_notice", focus=focus)
        elif kind == _MESSAGE_KIND_AUTOMATION:
            label = tr(
                "recent_activity.automation",
                source=source or tr("source.unknown"),
                focus=focus,
            )
        elif kind == _MESSAGE_KIND_BUBBLE:
            label = tr("recent_activity.bubble_message", sender=sender, focus=focus)
        else:
            label = tr(
                "recent_activity.participant_message",
                sender=sender,
                source=source or tr("recent_activity.external"),
                conversation=location,
                focus=focus,
            )
        lines = [label, self._indent_replay(content)]
        for attachment in entry.get("files") or []:
            if isinstance(attachment, dict):
                detail = " ".join(
                    str(attachment.get(key) or "") for key in ("filename", "saved_path")
                ).strip()
                if detail:
                    lines.append(
                        self._indent_replay(tr("recent_activity.attachment", detail=detail))
                    )
        return lines

    def _render_replay_tool(
        self,
        call: dict[str, Any],
        result: dict[str, Any] | None,
        *,
        query_text: str = "",
        matched_document: str = "",
    ) -> list[str]:
        name = str(call.get("name") or tr("recent_activity.unknown_tool"))
        args = json.dumps(call.get("arguments", {}), ensure_ascii=False, default=str)
        rendered_args, args_focused = self._replay_text_with_match(
            args,
            query_text=query_text,
            matched_document=matched_document,
        )
        call_label = tr(
            "recent_activity.executed",
            name=name,
            focus=tr("recent_activity.focus") if args_focused else "",
        )
        lines = [call_label, self._indent_replay(rendered_args)]
        if result is not None:
            content, focused = self._replay_text_with_match(
                result.get("content"),
                query_text=query_text,
                matched_document=matched_document,
            )
            content = content or tr("recent_activity.empty_result")
            if result.get("is_error"):
                label = tr(
                    "recent_activity.tool_error",
                    focus=tr("recent_activity.focus") if focused else "",
                )
            else:
                label = tr(
                    "recent_activity.tool_return",
                    focus=tr("recent_activity.focus") if focused else "",
                )
            lines.extend([label, self._indent_replay(content)])
        return lines

    @staticmethod
    def _episode_time_label(entries: list[dict[str, Any]]) -> str:
        start = _display_timestamp(str(entries[0].get("ts") or ""))
        end = _display_timestamp(str(entries[-1].get("ts") or ""))
        if not end or start == end:
            return start
        if start[:10] == end[:10]:
            return tr("recent_activity.time_span", start=start, end=end[11:])
        return tr("recent_activity.time_span", start=start, end=end)

    @staticmethod
    def _indent_replay(text: str) -> str:
        return "\n".join(f"  {line}" for line in text.splitlines())

    @staticmethod
    def _replay_text(value: Any) -> str:
        text = str(value or "").strip()
        if len(text) <= _REPLAY_EVENT_CHARS:
            return text
        return text[:_REPLAY_EVENT_CHARS] + tr("recent_activity.truncated")

    @classmethod
    def _replay_text_with_match(
        cls,
        value: Any,
        *,
        query_text: str = "",
        matched_document: str = "",
    ) -> tuple[str, bool]:
        """Render a long event around its retrieval hit instead of its prefix."""
        text = str(value or "").strip()
        if len(text) <= _REPLAY_EVENT_CHARS:
            return text, False
        match = cls._find_match_span(text, query_text, matched_document)
        if match is None:
            return cls._replay_text(text), False
        return cls._match_context(text, *match), True

    @classmethod
    def _find_match_span(
        cls,
        text: str,
        query_text: str,
        matched_document: str,
    ) -> tuple[int, int] | None:
        folded = text.casefold()
        document = matched_document.strip()
        if document.startswith("[") and "\n" in document:
            document = document.split("\n", 1)[1]
        candidates: list[str] = [document]
        candidates.extend(line.strip() for line in document.splitlines())
        prefixes = (
            "[tool_args]",
            "[tool_result]",
            "[tool_error]",
            "调用参数:",
            "工具结果:",
            "工具错误:",
        )
        candidates.extend(
            candidate[len(prefix) :].strip()
            for candidate in list(candidates)
            for prefix in prefixes
            if candidate.startswith(prefix)
        )
        document_match: tuple[int, int] | None = None
        for candidate in sorted({c for c in candidates if c}, key=len, reverse=True):
            probes = [candidate]
            if len(candidate) > 160:
                width = 160
                probes.extend(
                    [
                        candidate[:width],
                        candidate[(len(candidate) - width) // 2 : (len(candidate) + width) // 2],
                        candidate[-width:],
                    ]
                )
            for probe in probes:
                start = folded.find(probe.casefold())
                if start >= 0:
                    document_match = (start, start + len(probe))
                    break
            if document_match is not None:
                break

        query = query_text.strip()
        if query:
            query_folded = query.casefold()
            occurrences: list[int] = []
            cursor = 0
            while True:
                start = folded.find(query_folded, cursor)
                if start < 0:
                    break
                occurrences.append(start)
                cursor = start + max(1, len(query_folded))
            if occurrences:
                if document_match is None:
                    start = occurrences[0]
                else:
                    anchor = (document_match[0] + document_match[1]) // 2
                    start = min(occurrences, key=lambda value: abs(value - anchor))
                return start, start + len(query)
        return document_match

    @classmethod
    def _match_context(cls, text: str, match_start: int, match_end: int) -> str:
        raw_lines = text.splitlines(keepends=True) or [text]
        offsets: list[tuple[int, int, str]] = []
        cursor = 0
        for raw_line in raw_lines:
            line = raw_line.rstrip("\r\n")
            offsets.append((cursor, cursor + len(line), line))
            cursor += len(raw_line)

        first = next(
            (i for i, (_start, end, _line) in enumerate(offsets) if match_start <= end),
            len(offsets) - 1,
        )
        last = next(
            (i for i in range(first, len(offsets)) if match_end <= offsets[i][1]),
            first,
        )
        shown_start = max(0, first - _REPLAY_MATCH_CONTEXT_LINES)
        shown_end = min(len(offsets), last + _REPLAY_MATCH_CONTEXT_LINES + 1)
        rendered: list[str] = []
        if shown_start > 0:
            rendered.append(tr("recent_activity.omitted_before"))
        for i in range(shown_start, shown_end):
            line_start, _line_end, line = offsets[i]
            if first <= i <= last:
                local_start = max(0, match_start - line_start)
                local_end = min(len(line), max(local_start, match_end - line_start))
                left = max(0, local_start - _REPLAY_MATCH_SIDE_CHARS)
                right = min(len(line), local_end + _REPLAY_MATCH_SIDE_CHARS)
                excerpt = line[left:right]
                if left > 0:
                    excerpt = "…" + excerpt
                if right < len(line):
                    excerpt += "…"
                rendered.append(f"> {excerpt}")
            else:
                if len(line) > _REPLAY_CONTEXT_LINE_CHARS:
                    if i < first:
                        line = "…" + line[-_REPLAY_CONTEXT_LINE_CHARS:]
                    else:
                        line = line[:_REPLAY_CONTEXT_LINE_CHARS] + "…"
                rendered.append(f"  {line}")
        if shown_end < len(offsets):
            rendered.append(tr("recent_activity.omitted_after"))
        return "\n".join(rendered)

    @staticmethod
    def _activity_description(
        *,
        event_type: str,
        source: str = "",
        participant_id: str = "",
        conversation_id: str = "",
        tool_name: str = "",
        status: str = _STATUS_UNKNOWN,
        is_error: bool = False,
        message_kind: str = "",
        intent_role: str = "",
    ) -> str:
        if event_type == "message_in":
            sender = participant_id or tr("recent_activity.external_participant")
            source_part = (
                tr("recent_activity.source_named", source=source)
                if source
                else tr("recent_activity.source_external")
            )
            conv_part = (
                tr("recent_activity.conversation", conversation=conversation_id)
                if conversation_id
                else ""
            )
            if message_kind == _MESSAGE_KIND_SYSTEM:
                return tr(
                    "recent_activity.description_system",
                    source=source or tr("source.unknown"),
                    sender=sender,
                    conversation=conv_part,
                )
            if message_kind == _MESSAGE_KIND_AUTOMATION:
                return tr(
                    "recent_activity.description_automation",
                    source=source or tr("source.unknown"),
                    sender=sender,
                    conversation=conv_part,
                )
            if message_kind == _MESSAGE_KIND_BUBBLE:
                return tr(
                    "recent_activity.description_bubble",
                    sender=sender,
                    conversation=conv_part,
                )
            if message_kind == _MESSAGE_KIND_USER:
                return tr(
                    "recent_activity.description_user",
                    source=source_part,
                    sender=sender,
                    conversation=conv_part,
                )
            if intent_role:
                return tr(
                    "recent_activity.description_message_role",
                    source=source_part,
                    sender=sender,
                    conversation=conv_part,
                    role=intent_role,
                )
            return tr(
                "recent_activity.description_message",
                source=source_part,
                sender=sender,
                conversation=conv_part,
            )
        if event_type == "llm_response":
            return tr("recent_activity.thinking")
        if event_type == "tool_exchange":
            name = tool_name or tr("recent_activity.unknown_tool")
            result = tr(
                "recent_activity.failure"
                if is_error or status == _STATUS_ERROR
                else "recent_activity.success"
            )
            return tr("recent_activity.description_exchange", name=name, result=result)
        if event_type == "tool_call":
            name = tool_name or tr("recent_activity.unknown_tool")
            return tr("recent_activity.description_call", name=name)
        if event_type == "tool_result":
            name = tool_name or tr("recent_activity.unknown_tool")
            result = tr(
                "recent_activity.failure"
                if is_error or status == _STATUS_ERROR
                else "recent_activity.success"
            )
            return tr("recent_activity.description_result", name=name, result=result)
        if event_type == "task_reminder":
            return tr("recent_activity.description_task")
        if event_type == "subconscious_done":
            return tr("recent_activity.description_subconscious")
        return tr(
            "recent_activity.description_event",
            type=event_type or tr("recent_activity.unknown_type"),
        )

    @staticmethod
    def _snippet(text: str) -> str:
        text = text.strip()
        return text if len(text) <= _SNIPPET_CHARS else text[:_SNIPPET_CHARS] + "…"

    @staticmethod
    def _meta_snippet(text: str) -> str:
        text = text.strip()
        return text if len(text) <= _META_SNIPPET_CHARS else text[:_META_SNIPPET_CHARS] + "…"

    def _tool_exchange_snippet(self, meta: dict[str, Any], document: str) -> str:
        parts: list[str] = []
        call_context = str(meta.get("call_context") or "").strip()
        result_preview = str(meta.get("result_preview") or "").strip()
        # Keep the actual matched vector chunk first so supplemental metadata
        # cannot push a late hit beyond the snippet limit.
        parts.append(document.strip())
        if call_context and call_context not in document:
            parts.append(call_context)
        if result_preview and result_preview not in "\n".join(parts):
            parts.append(result_preview)
        return self._snippet("\n".join(p for p in parts if p))

    @staticmethod
    def _status(entry: dict[str, Any]) -> str:
        if entry.get("type") == "tool_exchange" and isinstance(entry.get("result"), dict):
            return _STATUS_ERROR if entry["result"].get("is_error") else _STATUS_OK
        if entry.get("type") == "tool_result":
            return _STATUS_ERROR if entry.get("is_error") else _STATUS_OK
        return _STATUS_UNKNOWN

    @staticmethod
    def _compact_header(header: str) -> str:
        # Headers are already compact; this drops verbose optional fields if a tool
        # argument or conversation id made the header unexpectedly long.
        parts = header.strip("[]").split()
        keep = [
            p
            for p in parts
            if p.startswith(
                (
                    "seq=",
                    "ts=",
                    "kind=",
                    "role=",
                    "call_seq=",
                    "result_seq=",
                    "name=",
                    "status=",
                )
            )
        ]
        return "[" + " ".join(keep[:6]) + "]"

    def _header(self, entry: dict[str, Any], *, compact: bool) -> str:
        t = entry.get("type")
        seq = entry.get("seq", "?")
        ts = entry.get("ts", "?")
        if t == "message_in":
            parts = [
                "message_in",
                f"seq={seq}",
                f"ts={ts}",
                f"kind={self._message_kind(entry)}",
                f"role={self._intent_role(entry)}",
                f"participant={entry.get('participant_id', '')}",
                f"source={entry.get('source', '')}",
            ]
            if entry.get("conversation_id"):
                parts.append(f"conversation={entry.get('conversation_id')}")
            return "[" + " ".join(parts) + "]"
        if t == "llm_response":
            return (
                f"[assistant seq={seq} ts={ts} provider={entry.get('provider', '')} "
                f"model={entry.get('model', '')} stop_reason={entry.get('stop_reason', '')}]"
            )
        if t == "tool_call":
            return f"[tool_call seq={seq} ts={ts} name={entry.get('name', '')}]"
        if t == "tool_result":
            return (
                f"[tool_result seq={seq} ts={ts} name={entry.get('name', '')} "
                f"status={self._status(entry)}]"
            )
        if t == "tool_exchange":
            raw_result = entry.get("result")
            result = raw_result if isinstance(raw_result, dict) else {}
            return (
                f"[tool_exchange seq={seq} result_seq={result.get('seq', '?')} "
                f"ts={result.get('ts') or ts} "
                f"name={entry.get('name', '')} status={self._status(entry)}]"
            )
        if t == "task_reminder":
            return f"[task_reminder seq={seq} ts={ts}]"
        if t == "subconscious_done":
            return f"[subconscious_done seq={seq} ts={ts} mode={entry.get('mode', '')}]"
        return f"[{t} seq={seq} ts={ts}]"

    def _body(self, entry: dict[str, Any]) -> str:
        t = entry.get("type")
        if t == "message_in":
            parts = [str(entry.get("content") or "")]
            for f in entry.get("files") or []:
                if isinstance(f, dict):
                    parts.append(
                        tr(
                            "recent_activity.body_attachment",
                            filename=f.get("filename", ""),
                            path=f.get("saved_path", ""),
                        )
                    )
            return "\n".join(p for p in parts if p)
        if t == "llm_response":
            parts = [str(entry.get("content") or "")]
            for tc in entry.get("tool_calls") or []:
                if isinstance(tc, dict):
                    args = json.dumps(tc.get("arguments", {}), ensure_ascii=False, default=str)
                    parts.append(
                        tr(
                            "recent_activity.body_tool_call",
                            name=tc.get("name", "?"),
                            arguments=args,
                        )
                    )
            return "\n".join(p for p in parts if p)
        if t == "tool_call":
            return json.dumps(entry.get("arguments", {}), ensure_ascii=False, default=str)
        if t == "tool_result":
            return str(entry.get("content") or "")
        if t == "tool_exchange":
            raw_result = entry.get("result")
            result = raw_result if isinstance(raw_result, dict) else {}
            args = json.dumps(entry.get("arguments", {}), ensure_ascii=False, default=str)
            return "\n".join(
                [
                    f"[tool_args] {args}",
                    f"[{'tool_error' if result.get('is_error') else 'tool_result'}] "
                    f"{str(result.get('content') or '')}",
                ]
            )
        if t == "task_reminder":
            return json.dumps(entry.get("tasks", []), ensure_ascii=False, default=str)
        if t == "subconscious_done":
            return str(entry.get("result") or "")
        return json.dumps(entry, ensure_ascii=False, default=str)

    @staticmethod
    def _message_kind(entry: dict[str, Any]) -> str:
        source = str(entry.get("source") or "")
        participant_id = str(entry.get("participant_id") or "")
        if source == "bubble":
            return _MESSAGE_KIND_BUBBLE
        if source in _AUTOMATION_MESSAGE_SOURCES:
            return _MESSAGE_KIND_AUTOMATION
        if source in _SYSTEM_MESSAGE_SOURCES or participant_id == "system":
            return _MESSAGE_KIND_SYSTEM
        if source in _USER_MESSAGE_SOURCES:
            return _MESSAGE_KIND_USER
        return _MESSAGE_KIND_EXTERNAL

    @classmethod
    def _intent_role(cls, entry: dict[str, Any]) -> str:
        kind = cls._message_kind(entry)
        if kind == _MESSAGE_KIND_USER:
            return _INTENT_ROLE_USER
        if kind == _MESSAGE_KIND_AUTOMATION:
            return _INTENT_ROLE_AUTOMATION
        if kind == _MESSAGE_KIND_BUBBLE:
            return _INTENT_ROLE_COORDINATION
        return _INTENT_ROLE_CONTEXT

    def _load_state(self) -> dict[str, Any]:
        try:
            return json.loads(self._state_path.read_text(encoding="utf-8"))
        except Exception:
            return {}

    def _needs_rebuild(self, state: dict[str, Any]) -> bool:
        return (
            state.get("schema_version") != _SCHEMA_VERSION
            or state.get("collection_name") != _COLLECTION_NAME
            or state.get("embedder_model") != self._embedder_model
            or int(state.get("days") or 0) != self._days
            or int(state.get("chunk_tokens") or 0) != self._chunk_tokens
            or int(state.get("overlap_tokens") or -1) != self._overlap_tokens
        )

    def _save_state_for_entries(
        self,
        entries: list[dict[str, Any]],
        *,
        fallback_last_seq: int = -1,
        rebuilt: bool = False,
        compressed_until_ts: str | None = None,
    ) -> None:
        indexed = [e for e in entries if isinstance(e.get("seq"), int)]
        last_seq = max([int(e["seq"]) for e in indexed], default=fallback_last_seq)
        last_ts = max([str(e.get("ts") or "") for e in indexed], default="")
        state = {
            "schema_version": _SCHEMA_VERSION,
            "collection_name": _COLLECTION_NAME,
            "embedder_model": self._embedder_model,
            "days": self._days,
            "chunk_tokens": self._chunk_tokens,
            "overlap_tokens": self._overlap_tokens,
            "indexed_until_seq": last_seq,
            "indexed_until_ts": last_ts,
            "last_pruned_at": datetime.now().isoformat(),
        }
        if compressed_until_ts is not None:
            state["compressed_until_ts"] = compressed_until_ts
        old = self._load_state()
        if rebuilt:
            state["rebuilt_at"] = datetime.now().isoformat()
        elif old.get("rebuilt_at"):
            state["rebuilt_at"] = old["rebuilt_at"]
        if compressed_until_ts is None and old.get("compressed_until_ts"):
            state["compressed_until_ts"] = old["compressed_until_ts"]
        try:
            self._state_path.parent.mkdir(parents=True, exist_ok=True)
            temp_path = self._state_path.with_suffix(self._state_path.suffix + ".tmp")
            temp_path.write_text(
                json.dumps(state, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            temp_path.replace(self._state_path)
        except Exception as e:
            logger.warning(f"Failed to save recent activity state: {e}")
