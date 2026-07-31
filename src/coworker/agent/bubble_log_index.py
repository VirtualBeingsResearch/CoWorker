"""Persistent indexes for completed Bubble and subconscious transcripts."""

from __future__ import annotations

import json
import os
import tempfile
import threading
from datetime import datetime
from functools import lru_cache
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from coworker.agent.bubble import Bubble

_INDEX_FILENAME = "bubble_index.json"
_INDEX_VERSION = 2
_WRITE_LOCK = threading.Lock()


def build_completed_bubble_summary(bubble: Bubble, log_path: Path) -> dict[str, object]:
    """Build the stable list-view fields persisted for a terminal Bubble."""
    log_id = log_path.stem
    mode = log_id[len(bubble.id) + 1 :] if log_id.startswith(f"{bubble.id}_") else ""
    finished_at = bubble.finished_at or datetime.now()
    return {
        "id": bubble.id,
        "log_id": log_id,
        "mode": mode,
        "goal": bubble.goal,
        "status": bubble.status,
        "provider": bubble.provider,
        "model": bubble.model,
        "cycles_used": bubble.cycles_used,
        "max_cycles": bubble.max_cycles,
        "participant_id": bubble.participant_id,
        "conversation_id": bubble.conversation_id,
        "handoff_transparency": bubble.handoff_transparency,
        "resume_count": bubble.resume_count,
        "palaces": bubble.palaces,
        "created_at": bubble.created_at.isoformat(),
        "finished_at": finished_at.isoformat(),
        "elapsed_seconds": (finished_at - bubble.created_at).total_seconds(),
        "result": bubble.result,
        "error": bubble.error,
    }


def load_completed_bubble_index(logs_root: Path) -> list[dict[str, object]] | None:
    """Return terminal-record summaries, or ``None`` when an index must be rebuilt."""
    path = logs_root / _INDEX_FILENAME
    try:
        stat = path.stat()
    except (OSError, TypeError, ValueError):
        return None
    return _load_completed_bubble_index_cached(str(path.resolve()), stat.st_mtime_ns, stat.st_size)


@lru_cache(maxsize=16)
def _load_completed_bubble_index_cached(
    path: str, _mtime_ns: int, _size: int
) -> list[dict[str, object]] | None:
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, TypeError, ValueError):
        return None
    if not isinstance(payload, dict) or payload.get("version") != _INDEX_VERSION:
        return None
    records = payload.get("records")
    if not isinstance(records, dict):
        return None
    return [record for record in records.values() if isinstance(record, dict)]


def synchronize_completed_bubble_index(
    logs_root: Path,
    log_dir: Path,
    discovered: list[dict[str, object]],
) -> list[dict[str, object]]:
    """Merge discovered records and discard entries whose transcript disappeared."""
    with _WRITE_LOCK:
        present_log_ids = {path.stem for path in log_dir.glob("*.jsonl")}
        existing = load_completed_bubble_index(logs_root) or []
        by_log_id = {
            str(record.get("log_id") or ""): record
            for record in existing
            if str(record.get("log_id") or "") in present_log_ids
        }
        by_log_id.update(
            {
                str(record.get("log_id") or ""): record
                for record in discovered
                if str(record.get("log_id") or "") in present_log_ids
            }
        )
        _write_index(logs_root, by_log_id)
        return list(by_log_id.values())


def upsert_completed_bubble_index(logs_root: Path, record: dict[str, object]) -> None:
    """Persist one terminal Bubble record without losing concurrent completions."""
    log_id = str(record.get("log_id") or "")
    if not log_id:
        return
    with _WRITE_LOCK:
        existing = load_completed_bubble_index(logs_root) or []
        by_log_id = {
            str(item.get("log_id") or ""): item
            for item in existing
            if str(item.get("log_id") or "")
        }
        by_log_id[log_id] = record
        _write_index(logs_root, by_log_id)


def _write_index(logs_root: Path, records: dict[str, dict[str, object]]) -> None:
    logs_root.mkdir(parents=True, exist_ok=True)
    target = logs_root / _INDEX_FILENAME
    fd, temporary = tempfile.mkstemp(prefix=f".{_INDEX_FILENAME}.", suffix=".tmp", dir=logs_root)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(
                {"version": _INDEX_VERSION, "records": records},
                handle,
                ensure_ascii=False,
                separators=(",", ":"),
            )
        Path(temporary).replace(target)
        _load_completed_bubble_index_cached.cache_clear()
    except Exception:
        Path(temporary).unlink(missing_ok=True)
        raise
